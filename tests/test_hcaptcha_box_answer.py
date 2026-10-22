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
    """The parts of the upstream contract this test needs."""

    def __init__(self, page, agent_config):
        self.page = page
        self.config = agent_config
        self.robotic_arm = MagicMock()


class HcaptchaBoxAnswerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("box_answer_service_test", SOURCE)
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
            self.page,
            types.SimpleNamespace(
                EXECUTION_TIMEOUT=0.1,
                RESPONSE_TIMEOUT=0.01,
                create_cache_key=lambda payload: Path("cache"),
                WAIT_FOR_CHALLENGE_VIEW_TO_RENDER_MS=0,
            ),
        )
        self.agent.robotic_arm = MagicMock()
        self.agent.robotic_arm.check_crumb_count = AsyncMock(return_value=1)
        self.agent.robotic_arm.get_challenge_frame_locator = AsyncMock(
            return_value=types.SimpleNamespace(locator=MagicMock())
        )
        self.agent.robotic_arm.click_by_mouse = AsyncMock()
        self.agent._capture_point_image = AsyncMock(
            return_value=(Path("raw.png"), Path("grid.png"), (1000, 1000))
        )

    def test_a_box_becomes_the_center_of_the_object(self):
        boxes = [self.module.AreaBox(x1=430, y1=480, x2=530, y2=600)]

        centers = self.agent._box_centers(boxes, (1000, 1000))

        self.assertEqual(centers, [(480.0, 540.0)])

    def test_a_box_with_swapped_corners_still_yields_its_center(self):
        boxes = [self.module.AreaBox(x1=530, y1=600, x2=430, y2=480)]

        centers = self.agent._box_centers(boxes, (1000, 1000))

        self.assertEqual(centers, [(480.0, 540.0)])

    def test_a_normalized_or_tiny_box_is_refused(self):
        boxes = [
            self.module.AreaBox(x1=0.43, y1=0.48, x2=0.53, y2=0.60),
            self.module.AreaBox(x1=100, y1=100, x2=103, y2=140),
        ]

        self.assertEqual(self.agent._box_centers(boxes, (1000, 1000)), [])

    def test_a_box_outside_the_picture_is_pulled_back_into_it(self):
        boxes = [self.module.AreaBox(x1=900, y1=900, x2=1200, y2=1400)]

        centers = self.agent._box_centers(boxes, (1000, 1000))

        self.assertEqual(centers, [(949.5, 949.5)])

    def test_the_instruction_is_read_in_the_language_of_the_challenge(self):
        self.agent.robotic_arm.captcha_payload = types.SimpleNamespace(
            requester_question={"ja": "最も低い塔を2つクリックしてください"},
        )

        instruction = self.agent._challenge_instruction()

        self.assertEqual(instruction, "最も低い塔を2つクリックしてください")

    def test_the_english_instruction_wins_when_the_challenge_has_one(self):
        self.agent.robotic_arm.captcha_payload = types.SimpleNamespace(
            requester_question={
                "en": "Click all incorrectly parked cars",
                "ja": "不正に駐車された車をすべてクリックしてください",
            },
        )

        self.assertEqual(
            self.agent._challenge_instruction(), "Click all incorrectly parked cars"
        )

    async def test_the_box_answer_is_clicked_instead_of_a_point(self):
        view = types.SimpleNamespace(
            bounding_box=AsyncMock(return_value={"x": 0, "y": 0, "width": 500, "height": 471}),
            click=AsyncMock(),
        )
        frame = types.SimpleNamespace(locator=MagicMock(return_value=view))
        self.agent.robotic_arm.get_challenge_frame_locator = AsyncMock(return_value=frame)
        self.agent._answer_with_boxes = AsyncMock(return_value=[(480.0, 540.0)])
        self.agent.robotic_arm._spatial_point_reasoner = AsyncMock()

        await self.agent._select_image_local_points_impl("image_label_multi_select")

        # The box center lands on the car: 480/1000 of the width, 540/1000 of
        # the height of the challenge view.
        self.assertEqual([call.kwargs["position"] for call in view.click.await_args_list], [
            {"x": 240, "y": 254.34},
        ])
        self.agent.robotic_arm._spatial_point_reasoner.assert_not_awaited()
        self.agent.robotic_arm.click_by_mouse.assert_awaited_once()

    async def test_an_empty_box_answer_falls_back_to_the_point_answer(self):
        view = types.SimpleNamespace(
            bounding_box=AsyncMock(return_value={"x": 0, "y": 0, "width": 500, "height": 471}),
            click=AsyncMock(),
        )
        frame = types.SimpleNamespace(locator=MagicMock(return_value=view))
        self.agent.robotic_arm.get_challenge_frame_locator = AsyncMock(return_value=frame)
        self.agent._answer_with_boxes = AsyncMock(return_value=[])
        self.agent.robotic_arm._spatial_point_reasoner = AsyncMock(
            return_value=types.SimpleNamespace(
                points=[types.SimpleNamespace(x=250, y=235)],
                log_message="point answer",
            )
        )
        self.agent.robotic_arm._spatial_point_reasoner.cache_response = MagicMock()

        await self.agent._select_image_local_points_impl("image_label_multi_select")

        self.agent.robotic_arm._spatial_point_reasoner.assert_awaited_once()
        position = view.click.await_args_list[0].kwargs["position"]
        self.assertAlmostEqual(position["x"], 125, places=3)
        self.assertAlmostEqual(position["y"], 235 / 1000 * 471, places=3)


if __name__ == "__main__":
    unittest.main()
