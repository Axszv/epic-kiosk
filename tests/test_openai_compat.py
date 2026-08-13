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
        "captcha_output_budget",
        "captcha_temperature",
        "captcha_drag_response_is_valid",
    }
    nodes = [
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name in wanted
    ]
    namespace = {"Any": Any}
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


if __name__ == "__main__":
    unittest.main()
