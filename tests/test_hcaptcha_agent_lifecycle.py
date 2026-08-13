import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class HcaptchaAgentLifecycleTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
