import ast
import asyncio
import importlib
import sys
import types
import unittest
from enum import Enum
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


ROOT = Path(__file__).resolve().parents[1]


class HcaptchaAgentLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app_path = str(ROOT / "app")
        if app_path not in sys.path:
            sys.path.insert(0, app_path)
        sys.modules.setdefault("hcaptcha_challenger", MagicMock())
        sys.modules.setdefault("hcaptcha_challenger.agent", MagicMock())
        if "hcaptcha_challenger.models" not in sys.modules:
            class ChallengeSignal(str, Enum):
                SUCCESS = "success"
                FAILURE = "failure"
                EXECUTION_TIMEOUT = "challenge_execution_timeout"
                RESPONSE_TIMEOUT = "challenge_response_timeout"

            sys.modules["hcaptcha_challenger.models"] = types.SimpleNamespace(
                ChallengeSignal=ChallengeSignal
            )
        sys.modules.setdefault("playwright", MagicMock())
        sys.modules.setdefault("playwright.async_api", MagicMock())

    def test_runtime_services_use_page_scoped_agent_factory(self):
        for relative in (
            "app/services/epic_authorization_service.py",
            "app/services/epic_games_service.py",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            tree = ast.parse(source)
            direct_constructors = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AgentV"
            ]
            self.assertEqual(direct_constructors, [], relative)
            self.assertIn("get_hcaptcha_agent", source)

    def test_factory_has_one_page_scoped_cache(self):
        source = (ROOT / "app/services/hcaptcha_agent_service.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("WeakKeyDictionary", source)
        self.assertIn("_AGENTS.get(page)", source)
        self.assertIn("class EpicAgentV(AgentV)", source)
        self.assertIn("load_hsw_script", source)
        self.assertIn('"Accept-Encoding": "identity"', source)
        self.assertIn("self._hsw_lock = asyncio.Lock()", source)
        self.assertIn("if document_key == self._hsw_document_key:", source)
        self.assertIn("_get_nested_challenge_frame_locator", source)
        self.assertIn('"frame=challenge" in frame.url', source)
        self.assertIn("HSW payload was not decoded", source)
        self.assertIn("prepare_for_new_challenge", source)
        self.assertIn("_single_crumb_count", source)
        self.assertIn("_keep_latest_payload", source)
        self.assertIn("wait_for_challenge_start", source)
        self.assertIn("hCaptcha issued another round after submission", source)
        self.assertIn("ChallengeSignal.RESPONSE_TIMEOUT", source)
        self.assertIn("frame closed without a validated server response", source)
        self.assertIn("_perform_drag_drop_with_dom_source", source)
        self.assertIn("Corrected hCaptcha drag source", source)
        self.assertIn("EpicAgentV(page=page, agent_config=settings)", source)

    def test_checkout_resets_agent_before_submitting_order(self):
        source = (ROOT / "app/services/epic_games_service.py").read_text(
            encoding="utf-8"
        )
        prepare_index = source.index("agent.prepare_for_new_challenge()")
        click_index = source.index("await payment_btn.click(force=True)", prepare_index)

        self.assertLess(prepare_index, click_index)

    def test_drag_uses_humanized_trajectory_by_default(self):
        source = (ROOT / "app/settings.py").read_text(encoding="utf-8")

        self.assertIn('os.getenv("DISABLE_BEZIER_TRAJECTORY", "false")', source)

    def test_agent_continues_when_hcaptcha_issues_another_round(self):
        module = importlib.import_module("services.hcaptcha_agent_service")
        agent = module.EpicAgentV.__new__(module.EpicAgentV)
        agent.config = types.SimpleNamespace(EXECUTION_TIMEOUT=2, RESPONSE_TIMEOUT=0.2)
        agent._captcha_payload_queue = asyncio.Queue()
        agent._captcha_response_queue = asyncio.Queue()
        agent._captcha_payload_queue.put_nowait(object())
        agent._has_visible_challenge_frame = AsyncMock(return_value=True)
        agent._cache_validated_captcha_response = MagicMock()
        agent.page = types.SimpleNamespace(wait_for_timeout=AsyncMock())

        response = types.SimpleNamespace(is_pass=True)
        calls = 0

        async def solve_round():
            nonlocal calls
            calls += 1
            agent._captcha_payload_queue.get_nowait()
            if calls == 1:
                agent._captcha_payload_queue.put_nowait(object())
            else:
                agent._captcha_response_queue.put_nowait(response)

        agent._solve_captcha = AsyncMock(side_effect=solve_round)

        result = asyncio.run(agent.wait_for_challenge())

        self.assertEqual(result.value, "success")
        self.assertEqual(calls, 2)
        agent._cache_validated_captcha_response.assert_called_once_with(response)

    def test_agent_keeps_only_latest_pending_payload(self):
        module = importlib.import_module("services.hcaptcha_agent_service")
        agent = module.EpicAgentV.__new__(module.EpicAgentV)
        agent._captcha_payload_queue = asyncio.Queue()
        agent._captcha_payload_queue.put_nowait("old")
        agent._captcha_payload_queue.put_nowait("new")

        agent._keep_latest_payload()

        self.assertEqual(agent._captcha_payload_queue.qsize(), 1)
        self.assertEqual(agent._captcha_payload_queue.get_nowait(), "new")

    def test_agent_selects_nearest_dom_drag_source(self):
        module = importlib.import_module("services.hcaptcha_agent_service")
        candidates = [
            {"x": 955, "y": 460, "score": 360, "reason": "move-container"},
            {"x": 1135, "y": 560, "score": 280, "reason": "cursor:grab"},
        ]

        selected = module.EpicAgentV._nearest_drag_source(900, 465, candidates)

        self.assertEqual(selected["x"], 955)
        self.assertEqual(selected["reason"], "move-container")

    def test_agent_prefers_higher_score_for_equal_drag_sources(self):
        module = importlib.import_module("services.hcaptcha_agent_service")
        candidates = [
            {"x": 950, "y": 460, "score": 180, "reason": "move-label"},
            {"x": 950, "y": 460, "score": 360, "reason": "move-container"},
        ]

        selected = module.EpicAgentV._nearest_drag_source(900, 465, candidates)

        self.assertEqual(selected["reason"], "move-container")

    def test_agent_corrects_drag_path_before_upstream_execution(self):
        module = importlib.import_module("services.hcaptcha_agent_service")
        agent = module.EpicAgentV.__new__(module.EpicAgentV)
        agent._drag_source_candidates = AsyncMock(
            return_value=[
                {"x": 955.4, "y": 460.4, "score": 360, "reason": "move-container"}
            ]
        )
        agent._original_perform_drag_drop = AsyncMock(return_value="dragged")
        path = types.SimpleNamespace(
            start_point=types.SimpleNamespace(x=900, y=465),
            end_point=types.SimpleNamespace(x=1115, y=540),
        )

        result = asyncio.run(agent._perform_drag_drop_with_dom_source(path))

        self.assertEqual(result, "dragged")
        self.assertEqual((path.start_point.x, path.start_point.y), (955, 460))
        agent._original_perform_drag_drop.assert_awaited_once_with(
            path,
            steps=25,
            delay_ms=15,
        )

    def test_authorization_module_imports_with_runtime_annotations(self):
        source = (ROOT / "app/services/epic_authorization_service.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imported_names = {
            alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        annotation_names = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        }
        self.assertIn("AgentV", imported_names)
        self.assertIn("AgentV", annotation_names)

    def test_deploy_normalizes_empty_authentication_result(self):
        source = (ROOT / "app/deploy.py").read_text(encoding="utf-8")

        self.assertIn("if auth_result is None:", source)
        self.assertIn("auth_result = ErrorType.UNKNOWN", source)


if __name__ == "__main__":
    unittest.main()
