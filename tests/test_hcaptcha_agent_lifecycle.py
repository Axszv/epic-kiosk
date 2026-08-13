import ast
import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock


ROOT = Path(__file__).resolve().parents[1]


class HcaptchaAgentLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app_path = str(ROOT / "app")
        if app_path not in sys.path:
            sys.path.insert(0, app_path)
        sys.modules.setdefault("hcaptcha_challenger", MagicMock())
        sys.modules.setdefault("hcaptcha_challenger.agent", MagicMock())
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
        self.assertIn('headers={"Accept-Encoding": "identity"}', source)
        self.assertIn("EpicAgentV(page=page, agent_config=settings)", source)

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
