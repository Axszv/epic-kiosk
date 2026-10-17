import asyncio
import importlib.util
import os
import sys
import tempfile
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
            if page_index + 1 == payload.pages:
                if payload.next_payload is not None:
                    self._captcha_payload_queue.put_nowait(payload.next_payload)
                else:
                    self._captcha_response_queue.put_nowait(
                        types.SimpleNamespace(is_pass=payload.accepted)
                    )


def payload(pages=2, kind="image_label_multi_select", accepted=True, next_payload=None):
    return types.SimpleNamespace(
        pages=pages, kind=kind, accepted=accepted, next_payload=next_payload
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
        self.assertEqual(self.agent.solved_pages, [
            ("image_label_multi_select", 0), ("image_label_multi_select", 1)
        ])
        self.agent._cache_validated_captcha_response.assert_called_once()
        self.agent._retry_unanswered_challenge.assert_not_awaited()

    async def test_new_payload_reclassifies_after_finishing_all_pages(self):
        next_group = payload(pages=1, kind="image_drag_multi")
        self.agent._captcha_payload_queue.put_nowait(payload(next_payload=next_group))

        result = await self.agent.wait_for_challenge()

        self.assertEqual(result, Signal.SUCCESS)
        self.assertEqual(self.agent.classified_types, ["image_label_multi_select", "image_drag_multi"])
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

    async def test_point_clicks_use_local_scaled_positions_after_layout_changes(self):
        view = types.SimpleNamespace(
            bounding_box=AsyncMock(side_effect=[
                {"x": 710, "y": 373, "width": 1000, "height": 940},
                {"x": 30, "y": 40, "width": 250, "height": 235},
            ]), click=AsyncMock(),
        )
        frame = types.SimpleNamespace(locator=MagicMock(return_value=view))
        await self.agent._click_image_points(frame, [
            types.SimpleNamespace(x=250, y=235),
            types.SimpleNamespace(x=100, y=200),
        ], (500, 470))
        # Locator coordinates stay local despite the changed page offsets.
        self.assertEqual([call.kwargs["position"] for call in view.click.await_args_list], [
            {"x": 500, "y": 470}, {"x": 50, "y": 100},
        ])

    async def test_model_images_share_the_normalized_pixel_coordinate_system(self):
        from PIL import Image

        async def screenshot(*, path, **kwargs):
            with Image.new("RGB", (500, 470), "red") as native:
                native.save(path)
        frame = types.SimpleNamespace(locator=MagicMock(return_value=types.SimpleNamespace(
            screenshot=AsyncMock(side_effect=screenshot),
        )))
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
            raw, grid, size = await self.agent._capture_point_image(frame, Path(folder), 0)
            self.assertEqual(size, (1000, 1000))
            for path in (raw, grid):
                with Image.open(path) as supplied:
                    self.assertEqual(supplied.size, (1000, 1000))
                    self.assertEqual(supplied.getpixel((555, 555)), (255, 0, 0))
            self.assertEqual(self.agent._local_point_positions([
                types.SimpleNamespace(x=786, y=694),
            ], size), [(0.786, 0.694)])

    async def test_any_invalid_point_prevents_all_clicks(self):
        view = types.SimpleNamespace(click=AsyncMock())
        frame = types.SimpleNamespace(locator=MagicMock(return_value=view))
        for x, y in [(295, 530), (1067, 307), (500, 200), (-1, 50), (float("nan"), 1), (1, float("inf"))]:
            with self.subTest(point=(x, y)):
                with self.assertRaises(self.module.InvalidChallengeCoordinates):
                    await self.agent._click_image_points(frame, [
                        types.SimpleNamespace(x=200, y=200), types.SimpleNamespace(x=x, y=y),
                    ], (500, 470))
        view.click.assert_not_awaited()

    async def test_point_pages_retry_invalid_answer_before_clicking_and_submit_every_page(self):
        arm = self.agent.robotic_arm
        arm.signal_crumb_count = 2
        frames = [MagicMock(), MagicMock()]
        arm.get_challenge_frame_locator = AsyncMock(side_effect=frames)
        arm.click_by_mouse = AsyncMock()
        self.agent.config.create_cache_key = MagicMock(return_value=Path("cache"))
        self.agent.config.WAIT_FOR_CHALLENGE_VIEW_TO_RENDER_MS = 0
        self.agent._capture_point_image = AsyncMock(return_value=("raw", "grid", (500, 470)))
        self.agent._click_image_points = AsyncMock()
        def answer(x, y):
            return types.SimpleNamespace(points=[types.SimpleNamespace(x=x, y=y)], log_message="points")
        arm._spatial_point_reasoner = AsyncMock(side_effect=[
            answer(295, 530), answer(200, 200), answer(300, 300),
        ])
        arm._spatial_point_reasoner.cache_response = MagicMock()

        await arm.challenge_image_label_select("image_label_multi_select")

        self.assertEqual(arm._spatial_point_reasoner.await_count, 3)
        self.assertIn("previous answer was invalid", arm._spatial_point_reasoner.await_args_list[1].kwargs["auxiliary_information"])
        self.assertEqual([call.args[0] for call in self.agent._click_image_points.await_args_list], frames)
        self.assertEqual(arm.click_by_mouse.await_count, 2)

    async def test_repeated_invalid_answer_is_failure_without_a_click_or_submit(self):
        arm = self.agent.robotic_arm
        arm.signal_crumb_count = 1
        arm.get_challenge_frame_locator = AsyncMock(return_value=MagicMock())
        arm.click_by_mouse = AsyncMock()
        self.agent.config.create_cache_key = MagicMock(return_value=Path("cache"))
        self.agent.config.WAIT_FOR_CHALLENGE_VIEW_TO_RENDER_MS = 0
        self.agent._capture_point_image = AsyncMock(return_value=("raw", "grid", (500, 470)))
        self.agent._click_image_points = AsyncMock()
        arm._spatial_point_reasoner = AsyncMock(return_value=types.SimpleNamespace(
            points=[types.SimpleNamespace(x=295, y=530)], log_message="invalid",
        ))
        arm._spatial_point_reasoner.cache_response = MagicMock()
        arm.refresh_challenge = AsyncMock(side_effect=ValueError("Element is not visible or does not exist"))
        async def upstream_dispatch():
            # The real upstream dispatcher catches all Exceptions, then tries
            # to refresh the frame. A rejected answer must not enter that path.
            try:
                return await arm.challenge_image_label_select("image_label_multi_select")
            except Exception:
                await arm.refresh_challenge()
        self.agent._solve_captcha = upstream_dispatch

        result = await self.agent.wait_for_challenge()

        self.assertEqual(result, Signal.FAILURE)
        self.agent._click_image_points.assert_not_awaited()
        arm.click_by_mouse.assert_not_awaited()
        arm.refresh_challenge.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
