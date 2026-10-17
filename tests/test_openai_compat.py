import unittest
import ast
import base64
from types import SimpleNamespace
from typing import Any, List
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
    @classmethod
    def setUpClass(cls):
        tree = ast.parse(SETTINGS_SOURCE.read_text(encoding="utf-8"))
        wanted = {"_convert_gemini_to_openai", "gemini_system_instruction_text"}
        nodes = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in wanted]
        namespace = {"Any": Any, "List": List, "base64": base64}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SETTINGS_SOURCE), "exec"), namespace)
        cls.convert_messages = staticmethod(namespace["_convert_gemini_to_openai"])
        cls.system_text = staticmethod(namespace["gemini_system_instruction_text"])

    def test_single_image_remains_a_valid_content_array(self):
        image = SimpleNamespace(inline_data=SimpleNamespace(data=b"image-bytes", mime_type="image/png"))
        messages, has_images = self.convert_messages([SimpleNamespace(role="user", parts=[image])], "flash")
        self.assertTrue(has_images)
        self.assertIsInstance(messages[0]["content"], list)
        block = messages[0]["content"][0]
        self.assertEqual(block["type"], "image_url")
        self.assertEqual(block["image_url"]["detail"], "high")
        self.assertEqual(base64.b64decode(block["image_url"]["url"].split(",")[1]), b"image-bytes")

    def test_string_system_instruction_is_not_lost(self):
        self.assertEqual(self.system_text("Use image-local coordinates"), "Use image-local coordinates")
        content = SimpleNamespace(parts=[SimpleNamespace(text="Inspect all objects"), SimpleNamespace(text="Then answer")])
        self.assertEqual(self.system_text(content), "Inspect all objects\nThen answer")
        self.assertIsNone(self.system_text(None))

    def test_model_content_keeps_assistant_role(self):
        messages, has_images = self.convert_messages([
            SimpleNamespace(role="model", parts=[SimpleNamespace(text="previous answer")]),
        ], "flash")
        self.assertFalse(has_images)
        self.assertEqual(messages, [{"role": "assistant", "content": [{"type": "text", "text": "previous answer"}]}])

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


if __name__ == "__main__":
    unittest.main()
