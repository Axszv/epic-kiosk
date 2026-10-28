import re
import unittest
from typing import Any
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETTINGS_SOURCE = ROOT / "app" / "settings.py"


def load_helpers():
    import ast

    tree = ast.parse(SETTINGS_SOURCE.read_text(encoding="utf-8"))
    wanted = {
        "extract_chat_completion_text",
        "strip_code_fence",
        "captcha_output_budget",
        "captcha_temperature",
        "captcha_drag_response_is_valid",
    }
    nodes = [
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name in wanted
    ]
    # The fence pattern is a module constant next to the helper.
    nodes += [
        item
        for item in tree.body
        if isinstance(item, ast.Assign)
        and any(getattr(t, "id", "") == "_CODE_FENCE" for t in item.targets)
    ]
    namespace = {"Any": Any, "re": re}
    exec(
        compile(ast.Module(body=nodes, type_ignores=[]), str(SETTINGS_SOURCE), "exec"),
        namespace,
    )
    return (
        namespace["extract_chat_completion_text"],
        namespace["captcha_output_budget"],
        namespace["captcha_temperature"],
        namespace["captcha_drag_response_is_valid"],
    )


(
    extract_chat_completion_text,
    captcha_output_budget,
    captcha_temperature,
    captcha_drag_response_is_valid,
) = load_helpers()


class OpenAICompatibilityTests(unittest.TestCase):
    def test_extracts_string_content(self):
        result = {"choices": [{"message": {"content": "answer"}}]}
        self.assertEqual(extract_chat_completion_text(result), "answer")

    def test_extracts_text_content_blocks(self):
        result = {
            "choices": [
                {"message": {"content": [{"type": "text", "text": "answer"}]}}
            ]
        }
        self.assertEqual(extract_chat_completion_text(result), "answer")

    def test_empty_completion_has_no_text(self):
        result = {
            "choices": [{"message": {"content": ""}, "finish_reason": "stop"}]
        }
        self.assertEqual(extract_chat_completion_text(result), "")

    def test_captcha_budget_raises_4096_to_8192(self):
        self.assertEqual(captcha_output_budget(4096), 8192)

    def test_captcha_budget_preserves_larger_request(self):
        self.assertEqual(captcha_output_budget(16384), 16384)

    def test_captcha_temperature_is_deterministic(self):
        self.assertEqual(captcha_temperature(0.7), 0.2)
        self.assertEqual(captcha_temperature(None), 0.2)

    def test_captcha_model_does_not_downgrade_by_call_count(self):
        source = (ROOT / "app/settings.py").read_text(encoding="utf-8")

        self.assertNotIn("CAPTCHA_FAILURE_THRESHOLD", source)
        self.assertNotIn("captcha_call_state", source)
        self.assertIn("selected_model = settings.CAPTCHA_MODEL", source)
        self.assertIn("fallback_model = settings.CAPTCHA_MODEL_FALLBACK", source)

    def test_rejects_zero_distance_drag_path(self):
        response = {
            "paths": [
                {
                    "start_point": {"x": 880, "y": 539},
                    "end_point": {"x": 880, "y": 539},
                }
            ]
        }

        self.assertFalse(captcha_drag_response_is_valid(response))

    def test_rejects_multiple_paths_for_single_gap_drag_puzzles(self):
        response = {
            "challenge_prompt": "Move the pipe into the grid to finish the route",
            "paths": [
                {
                    "start_point": {"x": 1100, "y": 440},
                    "end_point": {"x": 900, "y": 520},
                },
                {
                    "start_point": {"x": 1100, "y": 528},
                    "end_point": {"x": 960, "y": 560},
                },
            ],
        }

        self.assertFalse(captcha_drag_response_is_valid(response))

    def test_accepts_distinct_drag_path(self):
        response = {
            "paths": [
                {
                    "start_point": {"x": 759, "y": 494},
                    "end_point": {"x": 1067, "y": 589},
                }
            ]
        }

        self.assertTrue(captcha_drag_response_is_valid(response))


class ApiVersionTests(unittest.TestCase):
    def test_the_version_segment_is_configurable(self):
        """The gateway's path is not always `/v1`.

        Z.AI serves the same OpenAI-compatible shape under `/v4`, and the
        hardcoded `/v1` asked it for `/v4/v1/chat/completions`: run
        40617588808 got 404 on every call.
        """
        source = SETTINGS_SOURCE.read_text(encoding="utf-8")

        self.assertIn('os.getenv("API_VERSION", "v1")', source)
        self.assertIn('url = f"{base_url}/{api_version}/chat/completions"', source)
        self.assertNotIn('url = f"{base_url}/v1/chat/completions"', source)


class CodeFenceTests(unittest.TestCase):
    def test_a_fenced_answer_keeps_only_its_payload(self):
        """Gemini answers a JSON request inside a ```json fence.

        The library strips it in its own Gemini provider, but the
        OpenAI-compatible layer hands the text to the parser unchanged, and
        a fenced answer loses the round.
        """
        payload = {"choices": [{"message": {"content": '```json\n{"point": [521, 747]}\n```'}}]}

        self.assertEqual(extract_chat_completion_text(payload), '{"point": [521, 747]}')

    def test_a_bare_answer_is_left_alone(self):
        payload = {"choices": [{"message": {"content": '{"point": [1, 2]}'}}]}

        self.assertEqual(extract_chat_completion_text(payload), '{"point": [1, 2]}')


class ThinkingKwargTests(unittest.TestCase):
    def test_the_thinking_switch_can_be_left_out(self):
        """Google rejects the whole request over `chat_template_kwargs`.

        A rejected call still spends one of 500 daily requests, so the field
        has to be left out rather than sent and retried.
        """
        source = SETTINGS_SOURCE.read_text(encoding="utf-8")

        self.assertIn('os.getenv(\n                "API_SEND_THINKING_KWARG", "true"\n            )', source)
        self.assertIn('payload.pop("chat_template_kwargs", None)', source)


if __name__ == "__main__":
    unittest.main()
