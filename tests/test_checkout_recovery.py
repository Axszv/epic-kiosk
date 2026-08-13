import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE_SOURCE = ROOT / "app" / "services" / "epic_games_service.py"
WORKFLOW_SOURCE = ROOT / ".github" / "workflows" / "epic-claim.yml"


class CheckoutRecoveryTests(unittest.TestCase):
    def test_checkout_only_waits_for_visible_hcaptcha(self):
        source = ast.unparse(ast.parse(SERVICE_SOURCE.read_text(encoding="utf-8")))
        self.assertIn("_has_visible_hcaptcha", source)
        self.assertIn("CHECKOUT_CAPTCHA_TIMEOUT_SECONDS", source)

    def test_uncertain_checkout_checks_strict_ownership_evidence(self):
        source = SERVICE_SOURCE.read_text(encoding="utf-8")
        marker = "if not checkout_submitted:"
        branch = source[source.index(marker):source.index("if await self._wait_until_owned", source.index(marker))]
        self.assertIn("_owned_from_order_history", branch)
        self.assertIn("_owned_from_product_page", branch)
        self.assertIn('emit_desktop_result(promotion.title, "verified_owned")', branch)

    def test_workflow_limits_internal_checkout_attempts(self):
        workflow = WORKFLOW_SOURCE.read_text(encoding="utf-8")
        self.assertIn('CHECKOUT_MAX_ATTEMPTS: "2"', workflow)
        self.assertIn('CHECKOUT_CAPTCHA_TIMEOUT_SECONDS: "75"', workflow)


if __name__ == "__main__":
    unittest.main()
