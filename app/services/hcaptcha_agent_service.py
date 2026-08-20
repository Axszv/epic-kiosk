"""Page-scoped hCaptcha agent lifecycle."""

import asyncio
import hashlib
import time
from contextlib import suppress
from typing import Any
from weakref import WeakKeyDictionary

from hcaptcha_challenger.agent import AgentV
from hcaptcha_challenger.models import ChallengeSignal
from loguru import logger
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from settings import settings


DRAG_PROMPT_TRANSLATION = str.maketrans(
    {
        "а": "a",
        "ԁ": "d",
        "е": "e",
        "һ": "h",
        "і": "i",
        "ο": "o",
        "р": "p",
        "ѕ": "s",
    }
)
PIPE_ROUTE_DRAG_PROMPT = """
This is a missing-pipe placement puzzle. The only draggable choices are the
objects in the dark tray on the RIGHT, each marked Move. Existing pipes,
animals, barrels, circles, and scenery in the LEFT play area are never drag
sources. Select the green pipe piece from the right tray that exactly bridges
the single open gap in the route. Return exactly ONE path: start at the center
of that right-tray pipe piece and end at the center of the EMPTY GAP between
the two disconnected pipe ends. Do not drop onto an existing pipe segment and
do not return one path for every tray choice.
""".strip()
SHAPE_FIT_DRAG_PROMPT = """
This is an exact silhouette-matching puzzle. The only drag source is the clear
object inside the Move card at the bottom of the play area. First compare its
complete outline, rotation, outer corners, inner cutouts, bars, and protrusions
against EVERY candidate shape. Ignore candidates that share only a broad square
or diamond silhouette. Return exactly ONE path: start at the center of the
Move-card object and end at the geometric center of the one candidate whose
full internal and external geometry matches exactly. Do not choose the nearest
candidate and do not choose before checking them all.
""".strip()
FLYING_ANIMALS_PROMPT = """
Classify each pictured animal by real-world biological ability before selecting
coordinates. Select only animals that can propel themselves through the air by
flying, including birds capable of short powered flight such as chickens or
roosters. Do not select horses, cattle, reptiles, amphibians, fish, sharks, or
other terrestrial or marine animals. Inspect every animal once, then return one
point at the visual center of each qualifying animal and no points on background.
""".strip()
ANIMAL_SILHOUETTE_PROMPT = """
This is an exact animal-to-silhouette matching puzzle. The draggable animal
cards are in the gray Move cards, and the silhouettes are in the play area.
Choose exactly ONE animal card and exactly ONE silhouette. Compare the animal's
full body outline, ears, legs, tail, head, and orientation against every
silhouette before choosing. Do not choose a silhouette merely because it is
nearby or has a similar size. Return exactly ONE path from the center of the
chosen animal card to the center of its exact matching silhouette.
""".strip()
COUNTED_ANIMALS_PROMPT = """
The left column contains instruction cards showing an animal and a required
count. Do NOT click or return points on those instruction cards. The selectable
animals are only the animals inside the large gridded play area on the RIGHT.
For each instruction card, identify the matching animal type and select exactly
the requested number of matching animals in the grid. Count rows and columns
carefully, and return one point at the visual center of every selected grid
animal, with no extra points. The output must contain exactly the total number
of points implied by the instruction cards.
""".strip()


async def load_hsw_script(page: Page, url: str) -> str:
    """Load hsw.js without Camoufox/Juggler response-text decoding."""
    response = await page.context.request.get(
        url,
        headers={
            "Accept-Encoding": "identity",
            "Referer": page.url,
        },
        timeout=10000,
    )
    if not response.ok:
        raise RuntimeError(f"hsw request returned HTTP {response.status}")
    return await response.text()


class EpicAgentV(AgentV):
    """AgentV with a Camoufox-safe hsw.js loader."""

    HCAPTCHA_DRAG_CANVAS_WIDTH = 480
    HCAPTCHA_DRAG_CANVAS_HEIGHT = 320
    HCAPTCHA_CHALLENGE_VIEW_WIDTH = 500
    HCAPTCHA_CHALLENGE_VIEW_HEIGHT = 470
    HCAPTCHA_DRAG_CANVAS_LEFT = 10
    HCAPTCHA_DRAG_CANVAS_TOP = 135

    def __init__(self, *args, **kwargs):
        self._hsw_lock = asyncio.Lock()
        self._hsw_document_key = ""
        super().__init__(*args, **kwargs)
        # Upstream requires an exact visible `.challenge-view`, which misses
        # Epic's hCaptcha when it is nested inside the purchase iframe.
        self.robotic_arm.get_challenge_frame_locator = (
            self._get_nested_challenge_frame_locator
        )
        # hCaptcha can change task type between pages. Upstream processes every
        # crumb with the first page's type, so return to AgentV after each page
        # and classify the newly rendered challenge again.
        self.robotic_arm.check_crumb_count = self._single_crumb_count
        # Upstream executes the model's start coordinate verbatim. Visual
        # models often identify the correct target while missing the small
        # draggable tile by tens of pixels, so align the start with the actual
        # DOM element before pressing the mouse button.
        self._original_perform_drag_drop = self.robotic_arm._perform_drag_drop
        self.robotic_arm._perform_drag_drop = self._perform_drag_drop_with_dom_source
        self._original_match_user_prompt = self.robotic_arm._match_user_prompt
        self.robotic_arm._match_user_prompt = self._match_drag_user_prompt

    async def _single_crumb_count(self) -> int:
        return 1

    @staticmethod
    def _captcha_pass_metadata(response) -> str:
        token = str(getattr(response, "generated_pass_UUID", "") or "")
        fingerprint = (
            hashlib.sha256(token.encode("utf-8")).hexdigest()[:12] if token else ""
        )
        expiration = getattr(response, "expiration", None)
        return (
            f"token_present={bool(token)}, token_length={len(token)}, "
            f"token_sha256={fingerprint}, expiration={expiration}"
        )

    @staticmethod
    def _normalize_drag_prompt(value: str) -> str:
        return " ".join((value or "").translate(DRAG_PROMPT_TRANSLATION).casefold().split())

    @classmethod
    def _specialized_visual_prompt(cls, challenge_prompt: str) -> str | None:
        normalized = cls._normalize_drag_prompt(challenge_prompt)
        if "route" in normalized and "pipe" in normalized:
            return PIPE_ROUTE_DRAG_PROMPT
        if "where it fits" in normalized and (
            "drag" in normalized or "move" in normalized
        ):
            return SHAPE_FIT_DRAG_PROMPT
        if "matching silhouette" in normalized and "animal" in normalized:
            return ANIMAL_SILHOUETTE_PROMPT
        if "animals capable of flying" in normalized:
            return FLYING_ANIMALS_PROMPT
        if "find all animals based on the number provided" in normalized:
            return COUNTED_ANIMALS_PROMPT
        return None

    def _match_drag_user_prompt(self, job_type) -> str:
        base_prompt = self._original_match_user_prompt(job_type)
        payload = getattr(self.robotic_arm, "captcha_payload", None)
        challenge_prompt = ""
        if payload is not None:
            with suppress(Exception):
                challenge_prompt = payload.get_requester_question() or ""
        specialized = self._specialized_visual_prompt(challenge_prompt)
        if specialized is None:
            return base_prompt
        logger.debug(f"Using specialized hCaptcha visual guidance: {challenge_prompt}")
        return (
            f"{base_prompt}\n\n{specialized}\n\n"
            f"Challenge instruction: {challenge_prompt}"
        )

    @staticmethod
    def _nearest_drag_source(
        model_x: float,
        model_y: float,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda candidate: (
                (float(candidate["x"]) - model_x) ** 2
                + (float(candidate["y"]) - model_y) ** 2,
                -float(candidate.get("score", 0)),
            ),
        )

    @staticmethod
    def _project_payload_coordinate(
        coords: list[int | float],
        task_box: dict[str, float],
        canvas_width: float = HCAPTCHA_DRAG_CANVAS_WIDTH,
        canvas_height: float = HCAPTCHA_DRAG_CANVAS_HEIGHT,
    ) -> dict[str, Any] | None:
        if len(coords) < 2 or canvas_width <= 0 or canvas_height <= 0:
            return None
        try:
            source_x = float(coords[0])
            source_y = float(coords[1])
            return {
                "x": float(task_box["x"]) + source_x * float(task_box["width"]) / canvas_width,
                "y": float(task_box["y"]) + source_y * float(task_box["height"]) / canvas_height,
                "score": 1000,
                "reason": "payload-entity-coords",
            }
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _select_drag_canvas_box(
        candidates: list[dict[str, Any]],
    ) -> dict[str, float] | None:
        viable = []
        target_ratio = (
            EpicAgentV.HCAPTCHA_DRAG_CANVAS_WIDTH
            / EpicAgentV.HCAPTCHA_DRAG_CANVAS_HEIGHT
        )
        for candidate in candidates:
            try:
                width = float(candidate["width"])
                height = float(candidate["height"])
                if width < 200 or height < 120:
                    continue
                ratio_error = abs(width / height - target_ratio)
                if ratio_error > 0.15:
                    continue
                tag_bonus = 2 if str(candidate.get("tag", "")).lower() in {"canvas", "img"} else 0
                viable.append(
                    (
                        ratio_error,
                        -tag_bonus,
                        -(width * height),
                        candidate,
                    )
                )
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue
        if not viable:
            return None
        return min(viable, key=lambda item: item[:3])[3]

    @staticmethod
    def _derive_drag_canvas_box(
        challenge_box: dict[str, float],
    ) -> dict[str, float] | None:
        try:
            scale_x = (
                float(challenge_box["width"])
                / EpicAgentV.HCAPTCHA_CHALLENGE_VIEW_WIDTH
            )
            scale_y = (
                float(challenge_box["height"])
                / EpicAgentV.HCAPTCHA_CHALLENGE_VIEW_HEIGHT
            )
            if scale_x <= 0 or scale_y <= 0:
                return None
            return {
                "tag": "challenge-view-crop",
                "x": float(challenge_box["x"])
                + EpicAgentV.HCAPTCHA_DRAG_CANVAS_LEFT * scale_x,
                "y": float(challenge_box["y"])
                + EpicAgentV.HCAPTCHA_DRAG_CANVAS_TOP * scale_y,
                "width": EpicAgentV.HCAPTCHA_DRAG_CANVAS_WIDTH * scale_x,
                "height": EpicAgentV.HCAPTCHA_DRAG_CANVAS_HEIGHT * scale_y,
            }
        except (KeyError, TypeError, ValueError):
            return None

    async def _challenge_drag_canvas_box(self) -> dict[str, float] | None:
        frame = await self._get_nested_challenge_frame_locator()
        if frame is None:
            return None
        try:
            candidates = await frame.locator(".challenge-view *").evaluate_all(
                """elements => elements.map(element => {
                    const style = getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return {
                        tag: element.tagName.toLowerCase(),
                        x: rect.x,
                        y: rect.y,
                        width: rect.width,
                        height: rect.height,
                        visible: style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            Number(style.opacity || 1) > 0
                    };
                }).filter(item => item.visible)"""
            )
        except Exception as err:
            logger.debug(f"Could not inspect hCaptcha drag canvas: {err}")
            return None
        canvas_box = self._select_drag_canvas_box(candidates or [])
        if canvas_box is not None:
            return canvas_box

        try:
            challenge_box = await frame.locator(".challenge-view").bounding_box(timeout=1000)
        except Exception as err:
            logger.debug(f"Could not locate hCaptcha challenge view: {err}")
            return None
        if not challenge_box:
            return None
        return self._derive_drag_canvas_box(challenge_box)

    async def _payload_drag_source(
        self,
        model_x: float,
        model_y: float,
    ) -> dict[str, Any] | None:
        payload = getattr(self.robotic_arm, "captcha_payload", None)
        tasklist = getattr(payload, "tasklist", None) or []
        if not tasklist:
            return None
        entities = getattr(tasklist[0], "entities", None) or []
        if not entities:
            return None

        canvas_box = await self._challenge_drag_canvas_box()
        if not canvas_box:
            logger.debug("Could not locate hCaptcha drag canvas for payload coordinates")
            return None

        candidates = []
        for entity in entities:
            coords = getattr(entity, "coords", None) or []
            candidate = self._project_payload_coordinate(coords, canvas_box)
            if candidate is not None:
                candidates.append(candidate)
        return self._nearest_drag_source(model_x, model_y, candidates)

    async def _drag_source_candidates(self) -> list[dict[str, Any]]:
        frame = await self._get_nested_challenge_frame_locator()
        if frame is None:
            return []

        elements = frame.locator(".challenge-view *")
        try:
            metadata = await elements.evaluate_all(
                """elements => {
                    const root = document.querySelector('.challenge-view');
                    if (!root) return [];
                    const rootRect = root.getBoundingClientRect();
                    const rootArea = Math.max(1, rootRect.width * rootRect.height);
                    const nodes = Array.from(elements);
                    const indexByNode = new Map(nodes.map((node, index) => [node, index]));
                    const found = new Map();

                    const visibleBox = element => {
                        const style = getComputedStyle(element);
                        const rect = element.getBoundingClientRect();
                        if (
                            style.display === 'none' ||
                            style.visibility === 'hidden' ||
                            Number(style.opacity || 1) <= 0 ||
                            rect.width < 20 ||
                            rect.height < 20 ||
                            rect.width * rect.height > rootArea * 0.55
                        ) return null;
                        return {style, rect};
                    };

                    const add = (element, score, reason) => {
                        const index = indexByNode.get(element);
                        const box = visibleBox(element);
                        if (index === undefined || !box) return;
                        const current = found.get(index);
                        if (!current || score > current.score) {
                            found.set(index, {index, score, reason});
                        }
                    };

                    for (const element of nodes) {
                        const style = getComputedStyle(element);
                        const cursor = (style.cursor || '').toLowerCase();
                        const directText = Array.from(element.childNodes)
                            .filter(node => node.nodeType === Node.TEXT_NODE)
                            .map(node => node.textContent || '')
                            .join(' ')
                            .trim()
                            .toLowerCase();
                        const exactMove = directText === 'move' ||
                            (element.textContent || '').trim().toLowerCase() === 'move';
                        const draggable = element.draggable ||
                            element.getAttribute('draggable') === 'true';
                        const moveCursor = ['move', 'grab', 'grabbing'].includes(cursor);

                        if (draggable) add(element, 320, 'draggable');
                        if (moveCursor) add(element, 280, `cursor:${cursor}`);

                        if (exactMove) {
                            add(element, 180, 'move-label');
                            let parent = element.parentElement;
                            let depth = 0;
                            while (parent && parent !== root && depth < 5) {
                                const box = visibleBox(parent);
                                if (box && box.rect.width >= 55 && box.rect.height >= 55) {
                                    add(parent, 360 - depth * 20, 'move-container');
                                    break;
                                }
                                parent = parent.parentElement;
                                depth += 1;
                            }
                        }
                    }

                    return Array.from(found.values())
                        .sort((left, right) => right.score - left.score)
                        .slice(0, 8);
                }"""
            )
        except Exception as err:
            logger.debug(f"Could not inspect hCaptcha drag source elements: {err}")
            return []

        candidates: list[dict[str, Any]] = []
        for item in metadata or []:
            try:
                box = await elements.nth(int(item["index"])).bounding_box()
            except Exception:
                continue
            if not box:
                continue
            candidate = {
                "x": box["x"] + box["width"] / 2,
                "y": box["y"] + box["height"] / 2,
                "score": item.get("score", 0),
                "reason": item.get("reason", "dom"),
            }
            if any(
                abs(candidate["x"] - existing["x"]) < 12
                and abs(candidate["y"] - existing["y"]) < 12
                for existing in candidates
            ):
                continue
            candidates.append(candidate)
        return candidates

    async def _move_card_centers(self) -> list[dict[str, float]]:
        """Return visible centers of the hCaptcha cards marked `Move`."""
        frame = await self._get_nested_challenge_frame_locator()
        if frame is None:
            return []
        elements = frame.locator(".challenge-view *")
        try:
            boxes = await elements.evaluate_all(
                """elements => {
                    const root = document.querySelector('.challenge-view');
                    if (!root) return [];
                    const rootRect = root.getBoundingClientRect();
                    const result = [];
                    const seen = [];
                    const visible = node => {
                        const style = getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        return style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            Number(style.opacity || 1) > 0 &&
                            rect.width >= 45 && rect.height >= 45 &&
                            rect.width < rootRect.width * 0.45 &&
                            rect.height < rootRect.height * 0.7;
                    };
                    for (const element of elements) {
                        const text = (element.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                        if (!text || !text.includes('move')) continue;
                        let node = element;
                        for (let depth = 0; node && node !== root && depth < 6; depth++, node = node.parentElement) {
                            if (!visible(node)) continue;
                            const rect = node.getBoundingClientRect();
                            const center = {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
                            if (seen.some(item => Math.hypot(item.x - center.x, item.y - center.y) < 18)) break;
                            seen.push(center);
                            result.push(center);
                            break;
                        }
                    }
                    return result;
                }"""
            )
        except Exception as err:
            logger.debug(f"Could not inspect hCaptcha Move cards: {err}")
            return []
        return [
            {"x": float(item["x"]), "y": float(item["y"]), "score": 2000, "reason": "move-card"}
            for item in boxes or []
            if isinstance(item, dict) and "x" in item and "y" in item
        ]

    async def _perform_drag_drop_with_dom_source(
        self,
        path,
        steps: int = 25,
        delay_ms: int = 15,
    ):
        model_x = float(path.start_point.x)
        model_y = float(path.start_point.y)
        model_end_x = float(path.end_point.x)
        model_end_y = float(path.end_point.y)
        move_cards = []
        with suppress(Exception):
            move_cards = await self._move_card_centers()
        candidate = None
        if move_cards:
            nearest_start = self._nearest_drag_source(model_x, model_y, move_cards)
            nearest_end = self._nearest_drag_source(model_end_x, model_end_y, move_cards)
            start_distance = (
                (nearest_start["x"] - model_x) ** 2
                + (nearest_start["y"] - model_y) ** 2
            ) ** 0.5 if nearest_start else float("inf")
            end_distance = (
                (nearest_end["x"] - model_end_x) ** 2
                + (nearest_end["y"] - model_end_y) ** 2
            ) ** 0.5 if nearest_end else float("inf")
            if end_distance + 30 < start_distance:
                path.start_point.x, path.end_point.x = path.end_point.x, path.start_point.x
                path.start_point.y, path.end_point.y = path.end_point.y, path.start_point.y
                model_x, model_y = model_end_x, model_end_y
                nearest_start = nearest_end
                logger.info(
                    "Corrected hCaptcha drag direction: model started outside Move card "
                    f"and ended near one (start_distance={start_distance:.1f}, "
                    f"end_distance={end_distance:.1f})"
                )
            if nearest_start is not None:
                candidate = nearest_start
                path.start_point.x = int(round(nearest_start["x"]))
                path.start_point.y = int(round(nearest_start["y"]))
                logger.info(
                    "Corrected hCaptcha drag source to Move card: "
                    f"({model_x:.0f},{model_y:.0f}) -> "
                    f"({path.start_point.x},{path.start_point.y})"
                )
        if candidate is None:
            candidate = await self._payload_drag_source(model_x, model_y)
        if candidate is None:
            candidates = await self._drag_source_candidates()
            candidate = self._nearest_drag_source(model_x, model_y, candidates)
        if candidate is not None:
            corrected_x = int(round(candidate["x"]))
            corrected_y = int(round(candidate["y"]))
            distance = ((corrected_x - model_x) ** 2 + (corrected_y - model_y) ** 2) ** 0.5
            path.start_point.x = corrected_x
            path.start_point.y = corrected_y
            logger.info(
                "Corrected hCaptcha drag source: "
                f"model=({model_x:.0f},{model_y:.0f}), "
                f"dom=({corrected_x},{corrected_y}), "
                f"distance={distance:.1f}, reason={candidate['reason']}"
            )
        else:
            logger.debug("No DOM hCaptcha drag source found; using model start coordinate")

        return await self._original_perform_drag_drop(
            path,
            steps=steps,
            delay_ms=delay_ms,
        )

    @staticmethod
    def _drain_queue(queue) -> int:
        drained = 0
        while not queue.empty():
            queue.get_nowait()
            drained += 1
        return drained

    def prepare_for_new_challenge(self) -> None:
        """Discard signals belonging to a previous login or checkout challenge."""
        payloads = self._drain_queue(self._captcha_payload_queue)
        responses = self._drain_queue(self._captcha_response_queue)
        self._captcha_payload = None
        self.robotic_arm.captcha_payload = None
        self.robotic_arm.signal_crumb_count = None
        logger.debug(
            "Prepared hCaptcha agent for a new challenge: "
            f"discarded_payloads={payloads}, discarded_responses={responses}"
        )

    def _keep_latest_payload(self) -> None:
        if self._captcha_payload_queue.qsize() <= 1:
            return
        latest = None
        discarded = 0
        while not self._captcha_payload_queue.empty():
            item = self._captcha_payload_queue.get_nowait()
            if latest is not None:
                discarded += 1
            latest = item
        self._captcha_payload_queue.put_nowait(latest)
        logger.debug(f"Discarded {discarded} stale hCaptcha payload(s)")

    async def _get_nested_challenge_frame_locator(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            candidates = [
                frame
                for frame in self.page.frames
                if "hcaptcha.com/captcha/" in frame.url
                and "frame=challenge" in frame.url
            ]
            for frame in candidates:
                with suppress(Exception):
                    if await frame.locator("body").is_visible(timeout=500):
                        return frame
            if candidates:
                return candidates[0]
            await self.page.wait_for_timeout(250)
        logger.error("Cannot find a nested hCaptcha challenge frame")
        return None

    async def _has_visible_challenge_frame(self) -> bool:
        for frame in self.page.frames:
            if (
                "hcaptcha.com/captcha/" not in frame.url
                or "frame=challenge" not in frame.url
            ):
                continue
            with suppress(Exception):
                challenge_view = frame.locator(".challenge-view")
                if await challenge_view.is_visible(timeout=250):
                    return True
        return False

    async def wait_for_challenge_start(self, timeout_seconds: float = 15) -> bool:
        """Wait for a fresh checkout challenge payload or visible challenge frame."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if (
                not self._captcha_payload_queue.empty()
                or not self._captcha_response_queue.empty()
                or await self._has_visible_challenge_frame()
            ):
                return True
            await self.page.wait_for_timeout(250)
        return False

    async def wait_for_challenge(self) -> ChallengeSignal:
        """Solve every round emitted by one Epic checkout hCaptcha transaction."""
        deadline = time.monotonic() + self.config.EXECUTION_TIMEOUT
        round_number = 0

        while time.monotonic() < deadline:
            while not self._captcha_response_queue.empty():
                response = self._captcha_response_queue.get_nowait()
                if response and response.is_pass:
                    logger.success(
                        f"Challenge success: {self._captcha_pass_metadata(response)}"
                    )
                    self._cache_validated_captcha_response(response)
                    return ChallengeSignal.SUCCESS
                logger.warning("hCaptcha rejected the submitted round; checking for the next round")

            round_number += 1
            logger.debug(
                "Solving hCaptcha round "
                f"{round_number}: payloads={self._captcha_payload_queue.qsize()}, "
                f"responses={self._captcha_response_queue.qsize()}"
            )
            remaining = deadline - time.monotonic()
            try:
                await asyncio.wait_for(
                    self._solve_captcha(),
                    timeout=min(self.config.EXECUTION_TIMEOUT, remaining),
                )
            except asyncio.TimeoutError:
                logger.error(
                    f"Challenge execution timed out after {self.config.EXECUTION_TIMEOUT}s"
                )
                return ChallengeSignal.EXECUTION_TIMEOUT
            except PlaywrightTimeoutError as err:
                # hCaptcha can close or replace the challenge DOM after the
                # payload was queued. Upstream then waits 30 seconds for a
                # stale `.challenge-view` screenshot and leaks the exception.
                # Let any response/new payload win; otherwise finish this
                # transaction without trapping the caller in a long timeout.
                logger.warning(f"hCaptcha challenge DOM changed while solving: {err}")
                if not self._captcha_response_queue.empty():
                    continue
                if not await self._has_visible_challenge_frame():
                    logger.warning("hCaptcha challenge closed before it could be captured")
                    return ChallengeSignal.FAILURE
                if not self._captcha_payload_queue.empty():
                    self._keep_latest_payload()
                continue

            response_deadline = min(
                deadline,
                time.monotonic() + self.config.RESPONSE_TIMEOUT,
            )
            while time.monotonic() < response_deadline:
                if not self._captcha_response_queue.empty():
                    break
                if not self._captcha_payload_queue.empty():
                    logger.info("hCaptcha issued another round after submission; continuing")
                    break
                if not await self._has_visible_challenge_frame():
                    logger.debug(
                        "hCaptcha challenge frame closed; waiting for the server result"
                    )
                    closed_deadline = min(
                        response_deadline,
                        time.monotonic() + 10,
                    )
                    while time.monotonic() < closed_deadline:
                        if not self._captcha_response_queue.empty():
                            break
                        await self.page.wait_for_timeout(250)
                    else:
                        logger.warning(
                            "hCaptcha frame closed without a validated server response"
                        )
                        return ChallengeSignal.FAILURE
                    break
                await self.page.wait_for_timeout(250)
            else:
                logger.error(
                    f"Wait for captcha response timeout {self.config.RESPONSE_TIMEOUT}s"
                )
                return ChallengeSignal.RESPONSE_TIMEOUT

        logger.error(f"Challenge execution timed out after {self.config.EXECUTION_TIMEOUT}s")
        return ChallengeSignal.EXECUTION_TIMEOUT

    async def _task_handler(self, response):
        if (
            "/getcaptcha/" in response.url
            and response.headers.get("content-type", "") != "application/json"
        ):
            queue_size = self._captcha_payload_queue.qsize()
            await super()._task_handler(response)
            if self._captcha_payload_queue.qsize() == queue_size:
                logger.warning(
                    "HSW payload was not decoded; falling back to visual challenge detection"
                )
                self._captcha_payload_queue.put_nowait(None)
            self._keep_latest_payload()
            return

        if not response.url.endswith("/hsw.js"):
            result = await super()._task_handler(response)
            if "/getcaptcha/" in response.url:
                self._keep_latest_payload()
            return result

        document_key = self.page.url
        async with self._hsw_lock:
            if document_key == self._hsw_document_key:
                return
            try:
                hsw_text = await load_hsw_script(self.page, response.url)
                await self.page.evaluate(hsw_text)
                injected = await self.page.evaluate("typeof hsw === 'function'")
                if not injected:
                    raise RuntimeError("hsw function was not installed")
                self._hsw_document_key = document_key
                logger.debug("Injected hsw.js through the resilient loader")
            except Exception as err:
                # Mark this document as attempted so a burst of identical hsw
                # responses cannot create a new task for every response.
                self._hsw_document_key = document_key
                logger.warning(f"Resilient hsw.js injection failed: {err}")


_AGENTS: WeakKeyDictionary[Page, AgentV] = WeakKeyDictionary()


def _detach_hcaptcha_agent(page: Page, agent: AgentV) -> None:
    with suppress(Exception):
        page.remove_listener("response", agent._task_handler)


def get_hcaptcha_agent(page: Page) -> AgentV:
    """Return the one hCaptcha agent attached to a browser page.

    AgentV registers a response listener when constructed. Recreating it for
    every checkout leaves old listeners active and lets multiple agents race
    over the same hCaptcha payload, so its lifetime must match the page.
    """
    agent = _AGENTS.get(page)
    if agent is None:
        agent = EpicAgentV(page=page, agent_config=settings)
        _AGENTS[page] = agent
    return agent


def get_legacy_checkout_agent(page: Page) -> AgentV:
    """Switch to the single-agent checkout flow used by historical successes."""
    previous = _AGENTS.pop(page, None)
    if previous is not None:
        _detach_hcaptcha_agent(page, previous)
        logger.debug("Detached page-scoped Epic hCaptcha agent for legacy checkout")
    return AgentV(page=page, agent_config=settings)


def replace_hcaptcha_agent(page: Page) -> AgentV:
    """Replace the page agent before a new checkout transaction.

    Epic's Talon checkout state is transaction-scoped. Reusing the login agent
    across multiple checkout documents can retain stale challenge queues and
    script state, while constructing agents without detaching them leaves
    duplicate response listeners behind.
    """
    previous = _AGENTS.pop(page, None)
    if previous is not None:
        _detach_hcaptcha_agent(page, previous)

    agent = EpicAgentV(page=page, agent_config=settings)
    _AGENTS[page] = agent
    logger.debug("Replaced hCaptcha agent for a fresh checkout transaction")
    return agent
