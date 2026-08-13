import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUTHORIZATION_SOURCE = ROOT / "app" / "services" / "epic_authorization_service.py"


def load_cloudflare_detector():
    tree = ast.parse(AUTHORIZATION_SOURCE.read_text(encoding="utf-8"))
    selected = []
    wanted = {
        "CLOUDFLARE_TITLE_MARKERS",
        "CLOUDFLARE_BODY_MARKERS",
        "is_cloudflare_security_check",
    }
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if names & wanted:
                selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in wanted:
            selected.append(node)
    module = ast.Module(body=selected, type_ignores=[])
    namespace = {}
    exec(compile(module, str(AUTHORIZATION_SOURCE), "exec"), namespace)
    return namespace["is_cloudflare_security_check"]


is_cloudflare_security_check = load_cloudflare_detector()


class CloudflareDetectionTests(unittest.TestCase):
    def test_detects_title_interstitial(self):
        self.assertTrue(is_cloudflare_security_check("Just a moment...", ""))

    def test_detects_epic_one_more_step_page(self):
        self.assertTrue(
            is_cloudflare_security_check(
                "Epic Games",
                "One more step. Please complete a security check to continue. Verify you are human",
            )
        )

    def test_does_not_match_normal_login_page(self):
        self.assertFalse(
            is_cloudflare_security_check("Sign in to Epic Games", "Email address Password")
        )

    def test_does_not_treat_generic_verify_text_as_cloudflare(self):
        self.assertFalse(
            is_cloudflare_security_check("Epic Games", "Verify you are human")
        )


if __name__ == "__main__":
    unittest.main()
