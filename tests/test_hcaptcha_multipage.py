import asyncio
import importlib.util
import sys
import types
import unittest
from enum import Enum
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


SOURCE = Path(__file__).resolve().parents[1] / "app/services/hcaptcha_agent_service.py"


class Signal(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    EXECUTION_TIMEOUT = "challenge_execution_timeout"
    RESPONSE_TIMEOUT = "challenge_response_timeout"


class UpstreamAgent:
    """Model the upstream contract: page turns do not enqueue new payloads."""

    def __init__(self, page, agent_config):
        self.page = page
        self.config = agent_config
        self._captcha_payload_queue = asyncio.Queue()
        self._captcha_response_queue = asyncio.Queue()
        self.robotic_arm = MagicMock()
        self.robotic_arm.check_crumb_count = AsyncMock(
            side_effect=lambda: self.robotic_arm.signal_crumb_count
        )
        self.robotic_arm._capture_spatial_mapping = AsyncMock(return_value=("image", "grid"))
        self._cache_validated_captcha_response = MagicMock()
        self.solved_pages = []
        self.classified_types = []

    async def _review_challenge_type(self):
        self._captcha_payload = await self._captcha_payload_queue.get()
        self.robotic_arm.captcha_payload = self._captcha_payload
        self.robotic_arm.signal_crumb_count = self._captcha_payload.pages
        self.classified_types.append(self._captcha_payload.kind)
        return types.SimpleNamespace(value=self._captcha_payload.kind)

    async def _solve_captcha(self):
        await self._review_challenge_type()
        payload = self._captcha_payload
        for page_index in range(await self.robotic_arm.check_crumb_count()):
            await self.robotic_arm._capture_spatial_mapping(None, "cache", page_index)
            self.solved_pages.append((payload.kind, page_index))
            payload.remaining -= 1
            if payload.remaining > 0:
                # hCaptcha renders the next page of a payload as a fresh
                # challenge, and the task type can change between pages.
                self._captcha_payload_queue.put_nowait(payload)
            elif payload.next_payload is not None:
                self._captcha_payload_queue.put_nowait(payload.next_payload)
            else:
                self._captcha_response_queue.put_nowait(
                    types.SimpleNamespace(is_pass=payload.accepted)
                )


def payload(pages=2, kind="image_label_multi_select", accepted=True, next_payload=None):
    return types.SimpleNamespace(
        pages=pages, kind=kind, accepted=accepted, next_payload=next_payload,
        remaining=pages,
    )


class MultipageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("multipage_service_test", SOURCE)
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
            "hcaptcha_challenger.agent": types.SimpleNamespace(AgentV=UpstreamAgent),
            "hcaptcha_challenger.models": types.SimpleNamespace(ChallengeSignal=Signal),
            "loguru": types.SimpleNamespace(logger=MagicMock()),
            "playwright.async_api": types.SimpleNamespace(Page=object, TimeoutError=TimeoutError),
            "settings": types.SimpleNamespace(settings=object()),
        }):
            spec.loader.exec_module(self.module)
        self.page = types.SimpleNamespace(wait_for_timeout=AsyncMock(), frames=[])
        self.agent = self.module.EpicAgentV(
            self.page, types.SimpleNamespace(EXECUTION_TIMEOUT=0.1, RESPONSE_TIMEOUT=0.01)
        )
        self.agent._has_visible_challenge_frame = AsyncMock(return_value=True)
        self.agent._retry_unanswered_challenge = AsyncMock(return_value=False)

    async def test_two_pages_finish_without_new_network_payload_between_them(self):
        self.agent._captcha_payload_queue.put_nowait(payload())

        result = await self.agent.wait_for_challenge()

        self.assertEqual(result, Signal.SUCCESS)
        # One page per round: the second page arrives as a fresh challenge and
        # is classified again, because the task type can change between pages.
        self.assertEqual(self.agent.solved_pages, [
            ("image_label_multi_select", 0), ("image_label_multi_select", 0)
        ])
        self.assertEqual(self.agent.classified_types, [
            "image_label_multi_select", "image_label_multi_select"
        ])
        self.agent._cache_validated_captcha_response.assert_called_once()
        self.agent._retry_unanswered_challenge.assert_not_awaited()

    async def test_new_payload_reclassifies_after_finishing_all_pages(self):
        next_group = payload(pages=1, kind="image_drag_multi")
        self.agent._captcha_payload_queue.put_nowait(payload(next_payload=next_group))

        result = await self.agent.wait_for_challenge()

        self.assertEqual(result, Signal.SUCCESS)
        self.assertEqual(self.agent.classified_types, [
            "image_label_multi_select", "image_label_multi_select", "image_drag_multi"
        ])
        self.assertEqual(len(self.agent.solved_pages), 3)
        self.assertEqual(self.agent._challenge_page_index, 0)

    async def test_answering_both_pages_without_a_pass_is_not_success(self):
        self.agent._captcha_payload_queue.put_nowait(payload(accepted=False))

        result = await self.agent.wait_for_challenge()

        self.assertNotEqual(result, Signal.SUCCESS)
        self.assertEqual(len(self.agent.solved_pages), 2)
        self.agent._cache_validated_captcha_response.assert_not_called()

    async def test_drag_source_uses_the_current_page_entities(self):
        self.agent.robotic_arm.captcha_payload = types.SimpleNamespace(tasklist=[
            types.SimpleNamespace(entities=[types.SimpleNamespace(coords=[10, 20])]),
            types.SimpleNamespace(entities=[types.SimpleNamespace(coords=[240, 160])]),
        ])
        self.agent._challenge_drag_canvas_box = AsyncMock(return_value={
            "x": 100, "y": 200, "width": 480, "height": 320,
        })
        await self.agent._capture_challenge_page(None, "cache", 1)

        result = await self.agent._payload_drag_source(340, 360)

        self.assertEqual((result["x"], result["y"]), (340, 360))
        await self.agent._capture_challenge_page(None, "cache", 2)
        self.assertIsNone(await self.agent._payload_drag_source(340, 360))


if __name__ == "__main__":
    unittest.main()
