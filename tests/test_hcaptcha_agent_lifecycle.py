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
        self.assertIn("hCaptcha issued another round after submission", source)
        self.assertIn("ChallengeSignal.RESPONSE_TIMEOUT", source)
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
