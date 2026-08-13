"""Page-scoped hCaptcha agent lifecycle."""

import asyncio
import time
from contextlib import suppress
from weakref import WeakKeyDictionary

from hcaptcha_challenger.agent import AgentV
from hcaptcha_challenger.models import ChallengeSignal
from loguru import logger
from playwright.async_api import Page

from settings import settings


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

    async def _single_crumb_count(self) -> int:
        return 1

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
                    logger.success("Challenge success")
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
