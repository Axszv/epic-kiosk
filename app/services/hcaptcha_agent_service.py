"""Page-scoped hCaptcha agent lifecycle."""

import asyncio
import hashlib
import json
import math
import os
import re
import time
from contextlib import suppress
from typing import Any
from weakref import WeakKeyDictionary

from hcaptcha_challenger.agent import AgentV
from hcaptcha_challenger.models import ChallengeSignal, PointCoordinate, SpatialPath
from loguru import logger
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel

from settings import settings

# The last token hCaptcha issued, whoever produced it. A login replaces its
# agent between rounds, so the token cannot live on one agent: the request
# that carries it is handled by the authorization service, which has no way
# to know which agent answered.
LAST_PASS_TOKEN = ""


class AreaBox(BaseModel):
    """A rectangle of the challenge image, in image pixels."""

    x1: float
    y1: float
    x2: float
    y2: float


class ImageAreaBoxChallenge(BaseModel):
    """One box per object that the challenge instruction asks for."""

    challenge_prompt: str = ""
    boxes: list[AreaBox] = []


class DragBoxPath(BaseModel):
    """The object that has to be dragged and the place it has to end."""

    source_box: AreaBox
    target_box: AreaBox


class ImageDragBoxChallenge(BaseModel):
    """The drags of a challenge, as boxes of the challenge picture."""

    challenge_prompt: str = ""
    paths: list[DragBoxPath] = []


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
FOOD_INGREDIENT_PROMPT = """
The small reference picture at the TOP RIGHT shows the target dish. First
identify that reference dish; do not use one of the large answer tiles as the
reference. Then inspect EVERY selectable tile and select only foods that are
ingredients of that dish. For example, a fruit salad needs fruit, not pizza,
pasta, or a glass of drink. Different prepared meals are distractors. In your
challenge_prompt field, briefly name the reference dish and the selected
ingredient types so the identification is explicit. Return one point at the
center of EACH matching tile, including repeated matching ingredients.
""".strip()
AREA_BOX_DESCRIPTION = """
You are shown one challenge picture and the same picture with a coordinate grid
drawn on top of it. You find the objects a challenge instruction asks for and
report a tight box around each of them, in the pixel coordinates of the
pictures.
""".strip()


class InvalidChallengeCoordinates(ValueError):
    """A visual answer cannot safely be clicked inside the captured image."""


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
    POINT_IMAGE_SIZE = 1000

    def __init__(self, *args, **kwargs):
        self._decode_payloads = os.getenv("HCAPTCHA_VISUAL_ONLY", "false").lower() != "true"
        self._hsw_lock = asyncio.Lock()
        self._hsw_document_key = ""
        self._reported_sitekeys: set[tuple[str, str]] = set()
        self._challenge_widget_id = ""
        self._challenge_sitekey = ""
        self._last_pass_token = ""
        super().__init__(*args, **kwargs)
        # Upstream requires an exact visible `.challenge-view`, which misses
        # Epic's hCaptcha when it is nested inside the purchase iframe.
        self.robotic_arm.get_challenge_frame_locator = (
            self._get_nested_challenge_frame_locator
        )
        # Pages in one payload must all be answered before awaiting its result.
        # Keep upstream page counting and track the page for drag coordinates.
        self._challenge_page_index = 0
        self._original_capture_spatial_mapping = self.robotic_arm._capture_spatial_mapping
        self.robotic_arm._capture_spatial_mapping = self._capture_challenge_page
        # Upstream executes the model's start coordinate verbatim. Visual
        # models often identify the correct target while missing the small
        # draggable tile by tens of pixels, so align the start with the actual
        # DOM element before pressing the mouse button.
        self._original_perform_drag_drop = self.robotic_arm._perform_drag_drop
        self.robotic_arm._perform_drag_drop = self._perform_drag_drop_with_dom_source
        self._original_match_user_prompt = self.robotic_arm._match_user_prompt
        self.robotic_arm._match_user_prompt = self._match_drag_user_prompt
        self.robotic_arm.challenge_image_label_select = self._select_image_local_points
        # The upstream counts the pages of the challenge with a locator on the
        # challenge frame, and the frame is gone when the portal replaces the
        # challenge. The count then has to be one.
        self._original_check_crumb_count = self.robotic_arm.check_crumb_count
        self.robotic_arm.check_crumb_count = self._safe_check_crumb_count
        # The drag challenges are the most frequent ones, and the upstream
        # answer asks for page coordinates that the model reads off a plotted
        # axis. The drag is answered in the pixels of the challenge picture.
        self._original_challenge_image_drag_drop = self.robotic_arm.challenge_image_drag_drop
        self.robotic_arm.challenge_image_drag_drop = self._drag_image_local

    async def _safe_check_crumb_count(self) -> int:
        """The number of pages of the challenge, also without its frame.

        The upstream counts the pages with a locator on the challenge frame,
        and the frame is gone when the portal replaces the challenge. The
        count is one then, and the round is answered with the picture that
        is still there.
        """
        with suppress(Exception):
            count = await self._original_check_crumb_count()
            if isinstance(count, int) and count >= 1:
                return count
        return 1

    async def _capture_challenge_page(self, frame, cache_key, crumb_id):
        self._challenge_page_index = int(crumb_id)
        print(f"HCAPTCHA_PAGE:{self._challenge_page_index + 1}", flush=True)
        return await self._original_capture_spatial_mapping(frame, cache_key, crumb_id)

    async def _wait_for_challenge_images(self, frame, timeout_ms: int = 20000) -> bool:
        """Wait until hCaptcha's tile images are really rendered.

        The challenge view is visible before its images arrive, and a
        screenshot of that moment is blank. The model then answers an empty
        grid, hCaptcha rejects the answer, and Epic refuses the token.

        A challenge whose picture is a background image has nothing to wait
        for, and waiting for it anyway costs twenty seconds of every round.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        reported = False
        if frame is None:
            return True
        while time.monotonic() < deadline:
            state = {}
            with suppress(Exception):
                state = await frame.evaluate(
                    """() => {
                        const view = document.querySelector('.challenge-view');
                        const tiles = Array.from(document.querySelectorAll(
                            '.challenge-view .task-image, .challenge-view img,'
                            + ' .challenge-view canvas'
                        ));
                        const loaded = tiles.filter(tile => {
                            if (tile.tagName === 'IMG') {
                                return tile.complete && tile.naturalWidth > 0;
                            }
                            if (tile.tagName === 'CANVAS') {
                                return tile.width > 0 && tile.height > 0;
                            }
                            const background = getComputedStyle(tile).backgroundImage;
                            return background && background !== 'none';
                        });
                        let painted = 0;
                        if (view) {
                            painted = Array.from(view.querySelectorAll('*')).filter(el => {
                                const background = getComputedStyle(el).backgroundImage;
                                return background && background.indexOf('url(') >= 0;
                            }).length;
                        }
                        return {
                            tiles: tiles.length,
                            loaded: loaded.length,
                            painted: painted,
                        };
                    }"""
                )
            tiles = int(state.get("tiles") or 0)
            loaded = int(state.get("loaded") or 0)
            painted = int(state.get("painted") or 0)
            if tiles and loaded >= tiles:
                if reported:
                    logger.info(f"HCAPTCHA_IMAGES:ready:tiles={tiles}")
                return True
            if not tiles and painted:
                # The picture of this challenge is a background image, so the
                # screenshot has everything it needs right away.
                logger.info(f"HCAPTCHA_IMAGES:ready:painted={painted}")
                return True
            if not reported:
                logger.warning(
                    f"HCAPTCHA_IMAGES:waiting:tiles={tiles}:loaded={loaded}"
                )
                reported = True
            await self.page.wait_for_timeout(250)
        logger.warning("HCAPTCHA_IMAGES:timeout:the challenge tiles are still empty")
        return False

    async def _capture_point_image(self, frame, cache_key, crumb_id):
        # Keep the two model images pixel-aligned. Upstream's matplotlib plot
        # adds margins and page-coordinate axes to a differently sized canvas.
        from PIL import Image, ImageDraw, ImageFont

        self._challenge_page_index = crumb_id
        print(f"HCAPTCHA_PAGE:{crumb_id + 1}", flush=True)
        await self._wait_for_challenge_images(frame)
        cache_key.mkdir(parents=True, exist_ok=True)
        native = cache_key / f"{cache_key.name}_{crumb_id}_native.png"
        raw = cache_key / f"{cache_key.name}_{crumb_id}_challenge_view.png"
        grid = cache_key / f"{cache_key.name}_{crumb_id}_spatial_helper.png"
        await frame.locator(".challenge-view").screenshot(path=native, type="png", scale="css")
        with Image.open(native) as original:
            native_size = original.size
            # Flash sometimes returns normalized 0..1000 coordinates despite
            # the pixel prompt. Make both conventions identical in its inputs.
            size = (self.POINT_IMAGE_SIZE, self.POINT_IMAGE_SIZE)
            overlay = original.convert("RGB").resize(size, Image.Resampling.LANCZOS)
        self._native_image_size = native_size
        overlay.save(raw)
        draw = ImageDraw.Draw(overlay)
        font = ImageFont.load_default(size=18)
        width, height = size
        for x in range(100, width, 100):
            draw.line((x, 0, x, height - 1), fill=(135, 135, 135), width=1)
            draw.text((x + 2, 2), str(x), font=font, fill="black", stroke_width=1, stroke_fill="white")
        for y in range(100, height, 100):
            draw.line((0, y, width - 1, y), fill=(135, 135, 135), width=1)
            draw.text((2, y + 2), str(y), font=font, fill="black", stroke_width=1, stroke_fill="white")
        overlay.save(grid)
        print(f"HCAPTCHA_IMAGE:{raw.name}:source={native_size}:input={size}", flush=True)
        if os.getenv("GITHUB_ACTIONS") == "true":
            # Only puzzle images go into the existing Actions artifact, never
            # the dependency's raw payload/token cache.
            import shutil
            from settings import RUNTIME_DIR

            with suppress(Exception):
                RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
                for source in (native, raw, grid):
                    shutil.copyfile(source, RUNTIME_DIR / source.name)
        return raw, grid, size

    @staticmethod
    def _local_point_positions(points, image_size):
        width, height = image_size
        if width <= 0 or height <= 0 or not points:
            raise InvalidChallengeCoordinates("Missing image dimensions or answer points")
        positions = []
        for point in points:
            try:
                x, y = float(point.x), float(point.y)
            except (AttributeError, TypeError, ValueError) as err:
                raise InvalidChallengeCoordinates("Non-numeric answer point") from err
            if not (math.isfinite(x) and math.isfinite(y) and 0 <= x < width and 0 <= y < height):
                raise InvalidChallengeCoordinates(
                    f"Point ({x}, {y}) is outside the {width} x {height} image"
                )
            positions.append((x / width, y / height))
        return positions

    async def _click_image_points(self, frame, points, image_size):
        # Validate the entire answer before clicking anything: a partial answer
        # must not toggle already selected objects when the model is retried.
        positions = self._local_point_positions(points, image_size)
        await self._click_local_positions(frame, positions)

    async def _click_local_positions(self, frame, positions):
        view = frame.locator(".challenge-view")
        for x, y in positions:
            box = await view.bounding_box(timeout=1000)
            if not box or box["width"] <= 0 or box["height"] <= 0:
                raise InvalidChallengeCoordinates("Challenge image is no longer visible")
            # Locator clicks handle iframe offsets and scrolling; re-read size
            # after inference and before each click in case the layout moved.
            await view.click(position={"x": x * box["width"], "y": y * box["height"]},
                             delay=180, timeout=3000)
            await self.page.wait_for_timeout(500)

    def _challenge_instruction(self) -> str:
        """The challenge's own instruction, in the language it uses.

        The payload carries the instruction once per language, and the
        accessor of the dependency only ever reads the English copy. A
        challenge in another language therefore arrives as the word
        "unknown", which leaves the model with the picture alone.
        """
        instruction = str(getattr(self.robotic_arm, "_challenge_prompt", "") or "")
        payload = getattr(self.robotic_arm, "captcha_payload", None)
        if payload is None:
            return instruction
        questions = getattr(payload, "requester_question", None) or {}
        if not isinstance(questions, dict) or not questions:
            with suppress(Exception):
                instruction = payload.get_requester_question() or instruction
            return str(instruction or "")
        english = str(questions.get("en") or "").strip()
        if english and english != "unknown":
            return english
        for value in questions.values():
            native = str(value or "").strip()
            if native and native != "unknown":
                return native
        return instruction

    def _challenge_count_hint(self) -> str:
        """Tell the model how many objects the challenge wants, when it knows.

        The payload says how many points the answer of one picture needs. An
        answer with one object too many is a wrong answer, and the model has
        no way of knowing the number on its own.
        """
        payload = getattr(self.robotic_arm, "captcha_payload", None)
        config = getattr(payload, "request_config", None)
        if not config:
            return ""

        def read(name):
            if isinstance(config, dict):
                return config.get(name)
            return getattr(config, name, None)

        for minimum, maximum in (
            (read("min_points"), read("max_points")),
            (read("min_shapes_per_image"), read("max_shapes_per_image")),
        ):
            if (
                isinstance(minimum, int)
                and isinstance(maximum, int)
                and minimum == maximum
                and minimum > 0
            ):
                noun = "object" if minimum == 1 else "objects"
                return f"Select exactly {minimum} {noun} in this picture."
        return ""

    def _challenge_config_log(self) -> str:
        """The counts of the challenge, for the comparison of a later run."""
        payload = getattr(self.robotic_arm, "captcha_payload", None)
        config = getattr(payload, "request_config", None)
        if not config:
            return ""
        names = (
            "shape_type",
            "answer_type",
            "min_points",
            "max_points",
            "min_shapes_per_image",
            "max_shapes_per_image",
        )

        def read(name):
            if isinstance(config, dict):
                return config.get(name)
            return getattr(config, name, None)

        values = {}
        for name in names:
            value = read(name)
            if isinstance(value, (int, float, str, bool)):
                values[name] = value
        return json.dumps(values, ensure_ascii=False) if values else ""

    async def _answer_with_boxes(self, raw, grid, size, instruction, cache_key=None, crumb_id=None):
        """Ask the model for a box around every object the instruction names.

        A single point asks the model to do two hard things at once: find the
        object and place the pixel exactly on it. Its points land a hundred
        pixels off, on the background, and hCaptcha rejects the round. A box
        is much easier to place, and the center of a box that is a little too
        large is still inside the object.
        """
        width, height = size
        if not instruction:
            return []
        count_hint = self._challenge_count_hint()
        config_log = self._challenge_config_log()
        if config_log:
            logger.debug(f"hCaptcha challenge config: {config_log}")
        reasoner = getattr(self.robotic_arm, "_spatial_point_reasoner", None)
        provider = getattr(reasoner, "_provider", None)
        if provider is None:
            return []
        prompt = (
            f"The first image is the challenge picture. The second image is the "
            f"same picture with a coordinate grid drawn on it. Both images are "
            f"exactly {width} pixels wide and {height} pixels high, the origin "
            f"(0,0) is the top left corner, x grows to the right and y grows "
            f"downwards.\n\n"
            f"Challenge instruction: {instruction}\n\n"
            "The instruction may be written in any language. Translate it into "
            "English first, then look for every object the English instruction "
            "asks for.\n"
            "Write in the challenge_prompt field which objects the picture "
            "holds and which one of them the instruction asks for, and only "
            "then report the box. The picture often holds more than one "
            "candidate object, and the most obvious one is not always the one "
            "the instruction names. When the instruction names a group, a "
            "colour, a shape or a letter, check every candidate against that "
            "property and report the one that has it.\n"
            "Report one box per answer object, tight around the object, as "
            '{"x1": <left edge>, "y1": <top edge>, "x2": <right edge>, '
            '"y2": <bottom edge>} in the pixel coordinates of the images. Read '
            "the coordinates off the grid image, and report a box for every "
            "matching object and for nothing else.\n"
            "When the instruction names a number of objects, report exactly "
            "that many boxes. The small example picture in the instruction "
            "banner is not an answer object, and neither is anything outside "
            "the picture below the banner.\n"
            f"{count_hint}"
        )
        try:
            response = await provider.generate_with_images(
                images=[raw, grid],
                user_prompt=prompt,
                description=AREA_BOX_DESCRIPTION,
                response_schema=ImageAreaBoxChallenge,
            )
        except Exception as err:
            logger.warning(f"hCaptcha box answer failed: {err}")
            return []
        boxes = list(getattr(response, "boxes", []) or [])
        centers = self._box_centers(boxes, size)
        # The answer has to be readable in the run log: a rejected round is
        # only diagnosable against what the model was asked and what it
        # pointed at.
        logger.info(
            "HCAPTCHA_BOX_ANSWER:"
            f"instruction={instruction[:120]!r}:"
            f"boxes={len(boxes)}:"
            f"centers={[(round(x), round(y)) for x, y in centers]}"
        )
        if cache_key is not None:
            with suppress(Exception):
                cache_key.mkdir(parents=True, exist_ok=True)
                (cache_key / f"{cache_key.name}_{crumb_id}_box_model_answer.json").write_text(
                    json.dumps(
                        {
                            "instruction": instruction,
                            "boxes": [box.model_dump() for box in boxes],
                            "centers": centers,
                        },
                        indent=2,
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
        return centers

    async def _drag_image_local(self, job_type):
        """Answer a drag challenge in the pixels of the challenge picture."""
        arm = self.robotic_arm
        crumb_count = await arm.check_crumb_count()
        cache_key = self.config.create_cache_key(arm.captcha_payload)
        for cid in range(crumb_count):
            frame = await arm.get_challenge_frame_locator()
            if frame is None:
                raise InvalidChallengeCoordinates("Challenge frame is no longer visible")
            await self.page.wait_for_timeout(self.config.WAIT_FOR_CHALLENGE_VIEW_TO_RENDER_MS)
            raw, grid, size = await self._capture_point_image(frame, cache_key, cid)
            view = frame.locator(".challenge-view")
            box = await view.bounding_box(timeout=2000)
            if not box or box["width"] <= 0 or box["height"] <= 0:
                raise InvalidChallengeCoordinates("Challenge image is no longer visible")
            if not self._view_matches_the_picture(box):
                # The view is drawn with another shape than the picture, so a
                # pixel of the picture is not a pixel of the page.
                logger.info("Falling back to the upstream hCaptcha drag answer")
                return await self._original_challenge_image_drag_drop(job_type)
            instruction = self._challenge_instruction()
            paths = await self._answer_with_drag_boxes(
                raw, grid, size, instruction, job_type,
                view_box=box, cache_key=cache_key, crumb_id=cid,
            )
            if not paths:
                # The upstream answer still gets its chance.
                logger.info("Falling back to the upstream hCaptcha drag answer")
                return await self._original_challenge_image_drag_drop(job_type)
            width, height = size
            for source, target in paths:
                page_path = SpatialPath(
                    start_point=PointCoordinate(
                        x=int(round(box["x"] + source[0] / width * box["width"])),
                        y=int(round(box["y"] + source[1] / height * box["height"])),
                    ),
                    end_point=PointCoordinate(
                        x=int(round(box["x"] + target[0] / width * box["width"])),
                        y=int(round(box["y"] + target[1] / height * box["height"])),
                    ),
                )
                await arm._perform_drag_drop(page_path)
            await arm.click_by_mouse(frame.locator("div.button-submit.button"))

    def _view_matches_the_picture(self, box) -> bool:
        """Whether a pixel of the picture is a pixel of the page.

        The picture of the challenge is the view of the challenge, so both
        have the same shape. When they do not, the browser draws the view
        with another shape and the coordinates of the model cannot be used.
        """
        native_size = getattr(self, "_native_image_size", None)
        if not native_size:
            return True
        width, height = native_size
        if width <= 0 or height <= 0:
            return False
        picture_ratio = width / height
        view_ratio = box["width"] / box["height"]
        return abs(picture_ratio - view_ratio) / picture_ratio < 0.05

    async def _drag_source_image(self, raw, cache_key, crumb_id, view_box):
        """An enlarged picture of the element that has to be dragged.

        The element in the Move card is small and drawn with a thin line, and
        the model has to compare its outline with the shapes of the play
        area. An enlarged picture of it makes that comparison possible.
        """
        from PIL import Image

        if not view_box or view_box.get("width", 0) <= 0:
            return None
        centers = []
        with suppress(Exception):
            centers = await self._move_card_centers()
        if not centers:
            return None
        center = centers[0]
        scale_x = 1000.0 / max(view_box["width"], 1)
        scale_y = 1000.0 / max(view_box["height"], 1)
        cx = (center["x"] - view_box["x"]) * scale_x
        cy = (center["y"] - view_box["y"]) * scale_y
        half = 150
        left = int(max(0, min(1000 - 2 * half, cx - half)))
        top = int(max(0, min(1000 - 2 * half, cy - half)))
        path = cache_key / f"{cache_key.name}_{crumb_id}_drag_source.png"
        try:
            with Image.open(raw) as image:
                crop = image.crop((left, top, left + 2 * half, top + 2 * half))
                crop = crop.resize((600, 600), Image.Resampling.LANCZOS)
                crop.save(path)
        except Exception as err:
            logger.debug(f"Could not cut the drag source out of the picture: {err}")
            return None
        if os.getenv("GITHUB_ACTIONS") == "true":
            import shutil

            from settings import RUNTIME_DIR

            with suppress(Exception):
                RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, RUNTIME_DIR / path.name)
        return path

    async def _answer_with_drag_boxes(self, raw, grid, size, instruction, job_type, view_box=None, cache_key=None, crumb_id=None):
        """Ask the model for the source and the target of every drag.

        The upstream asks for page coordinates, which the model has to read
        off a plotted axis that carries margins. Its drag ended a hundred
        pixels off the place the instruction names, and the round was
        rejected. A box of the picture is the same question that the select
        challenges ask, and it is answered far more precisely.
        """
        width, height = size
        reasoner = getattr(self.robotic_arm, "_spatial_path_reasoner", None)
        provider = getattr(reasoner, "_provider", None)
        if provider is None:
            return []
        source_image = None
        with suppress(Exception):
            source_image = await self._drag_source_image(
                raw, cache_key, crumb_id, view_box
            )
        images = [raw, grid]
        if source_image is not None:
            images = [source_image, raw, grid]
        prompt = (
            f"{self.robotic_arm._match_user_prompt(job_type)}\n\n"
            + (
                "The first image is the element that has to be dragged, "
                "enlarged. The second image is the challenge picture, the "
                "third image is the same picture with a coordinate grid "
                "drawn on it. "
                if source_image is not None
                else "The first image is the challenge picture. The second "
                "image is the same picture with a coordinate grid drawn on it. "
            )
            + f"Both pictures of the challenge are exactly {width} pixels "
            f"wide and {height} pixels high, the origin (0,0) is the top left "
            f"corner, x grows to the right and y grows downwards.\n"
            "This is a drag and drop puzzle. For every drag the challenge "
            "asks for, report the box of the object that has to be dragged and "
            "the box of the place where it has to end, as "
            '{"source_box": {"x1": <left edge>, "y1": <top edge>, '
            '"x2": <right edge>, "y2": <bottom edge>}, "target_box": '
            '{"x1": <left edge>, "y1": <top edge>, "x2": <right edge>, '
            '"y2": <bottom edge>}} in the pixel coordinates of the pictures. '
            "The target is the empty place the object has to be dropped on, "
            "not an object that is already there."
        )
        try:
            response = await provider.generate_with_images(
                images=images,
                user_prompt=prompt,
                description=AREA_BOX_DESCRIPTION,
                response_schema=ImageDragBoxChallenge,
            )
        except Exception as err:
            logger.warning(f"hCaptcha drag box answer failed: {err}")
            return []
        paths = []
        for entry in list(getattr(response, "paths", []) or []):
            source = self._box_center(getattr(entry, "source_box", None), size)
            target = self._box_center(getattr(entry, "target_box", None), size)
            if source is None or target is None:
                continue
            paths.append((source, target))
        logger.info(
            "HCAPTCHA_DRAG_BOX_ANSWER:"
            f"drags={len(paths)}:"
            f"{[(tuple(round(v) for v in s), tuple(round(v) for v in t)) for s, t in paths]}"
        )
        return paths

    @classmethod
    def _box_center(cls, box, image_size):
        """The center of one box, or nothing when the box cannot be used."""
        centers = cls._box_centers([box] if box is not None else [], image_size)
        return centers[0] if centers else None

    @staticmethod
    def _box_centers(boxes, image_size):
        """Turn the model's boxes into the pixel centers to click."""
        width, height = image_size
        centers = []
        for box in boxes or []:
            try:
                x1, y1 = float(box.x1), float(box.y1)
                x2, y2 = float(box.x2), float(box.y2)
            except (AttributeError, TypeError, ValueError):
                continue
            if not all(math.isfinite(value) for value in (x1, y1, x2, y2)):
                continue
            if x2 < x1:
                x1, x2 = x2, x1
            if y2 < y1:
                y1, y2 = y2, y1
            x1 = min(max(x1, 0.0), width - 1.0)
            x2 = min(max(x2, 0.0), width - 1.0)
            y1 = min(max(y1, 0.0), height - 1.0)
            y2 = min(max(y2, 0.0), height - 1.0)
            if x2 - x1 < 8 or y2 - y1 < 8:
                # A box that is a few pixels wide cannot hold an object, and a
                # normalized answer would land here. Let the point answer try.
                continue
            centers.append(((x1 + x2) / 2.0, (y1 + y2) / 2.0))
        return centers

    async def _select_image_local_points(self, job_type):
        try:
            return await self._select_image_local_points_impl(job_type)
        except PlaywrightTimeoutError:
            # The challenge may expire while the model is answering. Returning
            # through the upstream dispatcher avoids its unconditional refresh
            # of a detached frame. Let a queued result or replacement round win.
            logger.warning("hCaptcha point challenge changed during inference")
            if (not self._captcha_response_queue.empty()
                    or not self._captcha_payload_queue.empty()):
                return None
            return ChallengeSignal.FAILURE
        except InvalidChallengeCoordinates as err:
            # Upstream catches every Exception and blindly refreshes, which can
            # itself fail on a closed iframe and hide our validation failure.
            # Its dispatcher forwards return values unchanged.
            logger.error(f"hCaptcha answer was not clicked: {err}")
            return ChallengeSignal.FAILURE

    async def _select_image_local_points_impl(self, job_type):
        arm = self.robotic_arm
        crumb_count = await arm.check_crumb_count()
        cache_key = self.config.create_cache_key(arm.captcha_payload)
        for cid in range(crumb_count):
            frame = await arm.get_challenge_frame_locator()
            if frame is None:
                raise InvalidChallengeCoordinates("Challenge frame is no longer visible")
            await self.page.wait_for_timeout(self.config.WAIT_FOR_CHALLENGE_VIEW_TO_RENDER_MS)
            raw, grid, size = await self._capture_point_image(frame, cache_key, cid)
            width, height = size
            coordinate_prompt = (
                f"Both images are exactly {width} pixels wide and {height} pixels high. "
                "Return ONLY image-local pixel coordinates: origin (0,0) is the TOP LEFT "
                "of either image, x increases rightwards, y increases downwards. "
                f"Every point must satisfy 0 <= x < {width} and 0 <= y < {height}. "
                "The grid labels are these same pixel coordinates, with no image margins. "
                "Do not use browser/page coordinates. "
                "Select the centers of the answer objects, not the instruction examples. "
                'Each point must have two integer fields, for example {"x":125,"y":240}. '
                "Never put a coordinate pair in the x field. "
                "The challenge instruction below may be written in any language. "
                "Translate it into English first, then pick the objects the "
                "English instruction asks for. Answer the instruction, not the "
                "examples that are shown in the image."
            )
            prompt = f"{arm._match_user_prompt(job_type)}\n\n{coordinate_prompt}"
            instruction = self._challenge_instruction()
            centers = await self._answer_with_boxes(
                raw, grid, size, instruction, cache_key=cache_key, crumb_id=cid
            )
            if centers:
                await self._click_local_positions(
                    frame, [(x / width, y / height) for x, y in centers]
                )
                await arm.click_by_mouse(frame.locator("div.button-submit.button"))
                continue
            for attempt in range(2):
                response = await arm._spatial_point_reasoner(
                    challenge_screenshot=raw, grid_divisions=grid,
                    auxiliary_information=prompt,
                )
                arm._spatial_point_reasoner.cache_response(
                    path=cache_key / f"{cache_key.name}_{cid}_{attempt}_model_answer.json"
                )
                try:
                    self._local_point_positions(response.points, size)
                except InvalidChallengeCoordinates as err:
                    logger.warning(f"Rejected hCaptcha image coordinates: {err}")
                    if attempt:
                        raise
                    prompt += f"\nYour previous answer was invalid: {err}. Re-read the grid. {coordinate_prompt}"
                    continue
                break
            logger.info(f"HCAPTCHA_POINT_ANSWER:[{cid + 1}/{crumb_count}] {response.log_message}")
            await self._click_image_points(frame, response.points, size)
            await arm.click_by_mouse(frame.locator("div.button-submit.button"))

    def _captcha_pass_metadata(self, response) -> str:
        global LAST_PASS_TOKEN
        token = str(getattr(response, "generated_pass_UUID", "") or "")
        # The login transaction is checked against the token hCaptcha issued,
        # so the token itself has to outlive this log line.
        self._last_pass_token = token
        LAST_PASS_TOKEN = token
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
        if "ingredients" in normalized and "dish" in normalized:
            return FOOD_INGREDIENT_PROMPT
        return None

    def _match_drag_user_prompt(self, job_type) -> str:
        base_prompt = self._original_match_user_prompt(job_type)
        # Visual-only classification stores the question on the arm rather
        # than in a decoded payload. It needs the same task-specific guidance.
        challenge_prompt = self._challenge_instruction()
        specialized = self._specialized_visual_prompt(challenge_prompt)
        if specialized is None:
            if not challenge_prompt:
                return base_prompt
            # The skill manager only knows the English instructions, so a
            # challenge in any other language reaches the model as a generic
            # task without its instruction. The instruction itself is what
            # tells the model which object to pick.
            logger.debug(f"Using the raw hCaptcha visual guidance: {challenge_prompt}")
            return (
                f"{base_prompt}\n\n"
                f"Challenge instruction: {challenge_prompt}\n\n"
                "The instruction may be written in any language. Translate it into "
                "English first, then pick the object the English instruction names."
            )
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
        elements = frame.locator(".challenge-view *")
        try:
            candidates = await elements.evaluate_all(
                """elements => elements.map((element, index) => {
                    const style = getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return {
                        index,
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
            # DOM client rects are iframe-local; Playwright's bounding_box
            # includes all ancestor frame offsets, as page.mouse requires.
            with suppress(Exception):
                page_box = await elements.nth(canvas_box["index"]).bounding_box(timeout=1000)
                if page_box:
                    return page_box

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
        page_index = getattr(self, "_challenge_page_index", 0)
        if page_index < 0 or page_index >= len(tasklist):
            return None
        entities = getattr(tasklist[page_index], "entities", None) or []
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
                            result.push({index: elements.indexOf(node)});
                            break;
                        }
                    }
                    return result;
                }"""
            )
        except Exception as err:
            logger.debug(f"Could not inspect hCaptcha Move cards: {err}")
            return []
        centers = []
        for item in boxes or []:
            with suppress(Exception):
                index = int(item["index"])
                if index < 0:
                    continue
                box = await elements.nth(index).bounding_box(timeout=1000)
                if box:
                    centers.append({
                        "x": box["x"] + box["width"] / 2,
                        "y": box["y"] + box["height"] / 2,
                        "score": 2000, "reason": "move-card",
                    })
        return centers

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
        self._challenge_page_index = 0
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

    async def _find_visible_challenge_frame(self):
        viewport = self.page.viewport_size
        if not viewport:
            viewport = await self.page.evaluate("({width: innerWidth, height: innerHeight})")
        for frame in self.page.frames:
            if (
                "hcaptcha.com/captcha/" not in frame.url
                or "frame=challenge" not in frame.url
            ):
                continue
            with suppress(Exception):
                owner = await frame.frame_element()
                if not await owner.evaluate("""el => {
                    for (let node = el; node; node = node.parentElement) {
                        const style = getComputedStyle(node);
                        if (style.display === 'none' || style.visibility === 'hidden' ||
                            Number(style.opacity) === 0) return false;
                    }
                    return true;
                }"""):
                    continue
                box = await owner.bounding_box()
                if not box or box["width"] <= 1 or box["height"] <= 1:
                    continue
                if (
                    box["x"] + box["width"] <= 0
                    or box["y"] + box["height"] <= 0
                    or box["x"] >= viewport["width"]
                    or box["y"] >= viewport["height"]
                ):
                    continue
                challenge_view = frame.locator(".challenge-view")
                if await challenge_view.is_visible(timeout=250):
                    self._report_challenge_sitekey(frame)
                    return frame
        return None

    def _report_challenge_sitekey(self, frame) -> None:
        """Name the widget whose challenge is on screen, once per widget.

        Epic renders one invisible hCaptcha per step, and a step that is
        already finished can leave its frame behind. The claim then answers
        the challenge of a widget the portal is no longer checking, and the
        token it produces is refused as `captcha_invalid` while hCaptcha
        itself reports a success. The sitekey is what tells the two widgets
        apart, so it goes to the run log.

        The challenge frame keeps its parameters in the URL fragment
        (`#frame=challenge&id=...&sitekey=...`), so the whole URL is searched
        and not only its query.

        The widget id is also kept on the agent: it names the textarea the
        token has to be handed to.
        """
        with suppress(Exception):
            url = frame.url
            sitekey = ""
            for name in ("sitekey", "siteKey"):
                found = re.search(rf"[?#&]{name}=([^&#]+)", url)
                if found:
                    sitekey = found.group(1)
                    break
            widget = re.search(r"[?#&]id=([^&#]+)", url)
            widget_id = widget.group(1) if widget else ""
            if widget_id:
                self._challenge_widget_id = widget_id
            self._challenge_sitekey = sitekey
            key = (sitekey, widget_id)
            if not sitekey or key in self._reported_sitekeys:
                return
            self._reported_sitekeys.add(key)
            print(
                f"HCAPTCHA_CHALLENGE_SITEKEY:{sitekey}:widget={widget_id[:12]}:"
                f"page={self.page.url[:80]}",
                flush=True,
            )

    async def _hand_over_pass_token(self, token: str) -> None:
        """Put the pass token where the portal reads it.

        hCaptcha answers with `generated_pass_UUID`, and the page is supposed
        to learn it from the widget callback: the widget writes it into
        `#h-captcha-response-<widget>` and Epic submits the login with it.

        Run 40517281544 showed that the token never reaches Epic. hCaptcha
        issued 5266, 5324, 4578, 5291 and 5362 characters, and none of them
        appears in the transaction, in the body as sent or in its decoded
        form; the portal answered `captcha_invalid` every time. When the page
        obtained the token itself, the same portal answered 204.

        So the token is handed over here, in all three places the widget
        keeps it. `hcaptcha.setResponse` is the documented way and fires the
        widget callback as well; the textareas and the frame attribute are
        the state that callback would have written.
        """
        if not token:
            return
        widget_id = getattr(self, "_challenge_widget_id", "") or ""
        try:
            report = await self.page.evaluate(
                """({token, widgetId}) => {
                    const out = {api: false, textareas: 0, frames: 0};
                    const api = window.hcaptcha;
                    if (api && typeof api.setResponse === 'function') {
                        try {
                            if (widgetId) { api.setResponse(token, widgetId); }
                            else { api.setResponse(token); }
                            out.api = true;
                        } catch (err) {
                            out.apiError = String(err).slice(0, 80);
                        }
                    }
                    for (const node of document.querySelectorAll(
                        'textarea[id^="h-captcha-response-"], textarea[id^="g-recaptcha-response-"]'
                    )) {
                        const id = node.id.replace(/^(h|g)-captcha-response-/, '');
                        if (widgetId && id !== widgetId) { continue; }
                        node.value = token;
                        node.dispatchEvent(new Event('input', {bubbles: true}));
                        node.dispatchEvent(new Event('change', {bubbles: true}));
                        out.textareas += 1;
                    }
                    for (const frame of document.querySelectorAll(
                        'iframe[data-hcaptcha-widget-id]'
                    )) {
                        const id = frame.getAttribute('data-hcaptcha-widget-id') || '';
                        if (widgetId && id !== widgetId) { continue; }
                        frame.setAttribute('data-hcaptcha-response', token);
                        out.frames += 1;
                    }
                    return out;
                }""",
                {"token": token, "widgetId": widget_id},
            )
            print(
                f"HCAPTCHA_TOKEN_HANDOVER:widget={widget_id[:12]}:"
                f"api={report.get('api')}:textareas={report.get('textareas')}:"
                f"frames={report.get('frames')}",
                flush=True,
            )
        except Exception as err:
            logger.warning(f"Could not hand the captcha token to the page: {err}")

    async def _get_nested_challenge_frame_locator(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            frame = await self._find_visible_challenge_frame()
            if frame is not None:
                return frame
            await self.page.wait_for_timeout(250)
        logger.error("Cannot find a visible nested hCaptcha challenge frame")
        return None

    async def _has_visible_challenge_frame(self) -> bool:
        return await self._find_visible_challenge_frame() is not None

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

    async def _retry_unanswered_challenge(self) -> bool:
        frame = await self._get_nested_challenge_frame_locator()
        if frame is not None:
            submit = frame.locator("div.button-submit.button").first
            with suppress(Exception):
                if await submit.is_visible(timeout=1000):
                    print("HCAPTCHA_RETRY:submit", flush=True)
                    await submit.click(force=True, timeout=3000)
                    for _ in range(40):
                        if not self._captcha_response_queue.empty():
                            return True
                        await self.page.wait_for_timeout(250)
                    return True
        try:
            await self.robotic_arm.refresh_challenge()
        except Exception as err:
            logger.warning(f"hCaptcha refresh control is unavailable: {err}")
            return False
        print("HCAPTCHA_RETRY:refresh", flush=True)
        return True

    async def _review_challenge_type(self):
        self._challenge_page_index = 0
        challenge_type = await super()._review_challenge_type()
        print(
            f"HCAPTCHA_TASK:{getattr(challenge_type, 'value', challenge_type)}",
            flush=True,
        )
        return challenge_type

    async def wait_for_challenge(self) -> ChallengeSignal:
        """Solve every round emitted by one Epic checkout hCaptcha transaction."""
        deadline = time.monotonic() + self.config.EXECUTION_TIMEOUT
        round_number = 0
        response_retries = 0

        while time.monotonic() < deadline:
            while not self._captcha_response_queue.empty():
                response = self._captcha_response_queue.get_nowait()
                if response and response.is_pass:
                    logger.success(
                        f"Challenge success: {self._captcha_pass_metadata(response)}"
                    )
                    await self._hand_over_pass_token(
                        str(getattr(response, "generated_pass_UUID", "") or "")
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
                round_result = await asyncio.wait_for(
                    self._solve_captcha(),
                    timeout=min(self.config.EXECUTION_TIMEOUT, remaining),
                )
                if round_result == ChallengeSignal.FAILURE:
                    return ChallengeSignal.FAILURE
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
                if response_retries == 0 and await self._has_visible_challenge_frame():
                    response_retries += 1
                    if await self._retry_unanswered_challenge():
                        continue
                return ChallengeSignal.RESPONSE_TIMEOUT

        logger.error(f"Challenge execution timed out after {self.config.EXECUTION_TIMEOUT}s")
        return ChallengeSignal.EXECUTION_TIMEOUT

    async def _task_handler(self, response):
        if not getattr(self, "_decode_payloads", True):
            if response.url.endswith("/hsw.js"):
                print("HCAPTCHA_HSW:visual_only", flush=True)
                return
            if (
                "/getcaptcha/" in response.url
                and response.headers.get("content-type", "").split(";", 1)[0] != "application/json"
            ):
                # Let the site's own scripts process its challenge. Upstream
                # can classify the visible task when no decoded payload exists.
                self._captcha_payload = None
                self._captcha_payload_queue.put_nowait(None)
                self._keep_latest_payload()
                return
        if "/checkcaptcha/" in response.url:
            print(f"HCAPTCHA_CHECK_RESPONSE:{response.status}", flush=True)
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
                print("HCAPTCHA_HSW:injected", flush=True)
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


def get_login_hcaptcha_agent(page: Page) -> AgentV:
    """Use the multi-page, stale-DOM recovery shared by Epic challenges."""
    agent = _AGENTS.get(page)
    if agent is None:
        agent = EpicAgentV(page=page, agent_config=settings)
        _AGENTS[page] = agent
        logger.debug("Created the multi-round hCaptcha agent for Epic login")
    return agent


def replace_login_hcaptcha_agent(page: Page) -> AgentV:
    """Reset hCaptcha state after Epic restarts a login transaction."""
    previous = _AGENTS.pop(page, None)
    if previous is not None:
        _detach_hcaptcha_agent(page, previous)

    agent = EpicAgentV(page=page, agent_config=settings)
    _AGENTS[page] = agent
    logger.debug("Replaced multi-round hCaptcha agent for a fresh Epic login transaction")
    return agent


def get_legacy_checkout_agent(page: Page) -> AgentV:
    """Switch to the single-agent checkout flow used by historical successes."""
    previous = _AGENTS.pop(page, None)
    if previous is not None:
        _detach_hcaptcha_agent(page, previous)
        logger.debug("Detached page-scoped Epic hCaptcha agent for legacy checkout")

    agent = EpicAgentV(page=page, agent_config=settings)
    _AGENTS[page] = agent
    return agent


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
