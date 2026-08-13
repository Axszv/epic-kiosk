import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE_SOURCE = ROOT / "app" / "services" / "epic_games_service.py"
WORKFLOW_SOURCE = ROOT / ".github" / "workflows" / "epic-claim.yml"


class CheckoutRecoveryTests(unittest.TestCase):
    def test_checkout_waits_for_delayed_hcaptcha_signals(self):
        raw_source = SERVICE_SOURCE.read_text(encoding="utf-8")
        source = ast.unparse(ast.parse(raw_source))
        self.assertIn("wait_for_challenge_start", source)
        self.assertIn("timeout_seconds=15", source)
        self.assertIn("CHECKOUT_CAPTCHA_TIMEOUT_SECONDS", source)
        self.assertIn("EXECUTION_TIMEOUT + settings.RESPONSE_TIMEOUT + 15", source)

    def test_purchase_frames_are_scanned_before_the_main_page(self):
        source = SERVICE_SOURCE.read_text(encoding="utf-8")
        start = source.index("async def _active_purchase_container")
        end = source.index("async def _handle_device_not_supported_modal", start)
        branch = source[start:end]

        self.assertIn('if "/purchase" in frame.url:', branch)
        self.assertIn("*purchase_frames", branch)
        self.assertLess(branch.index("*purchase_frames"), branch.index('(\"page\", page)'))

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
        self.assertIn('CHECKOUT_CAPTCHA_TIMEOUT_SECONDS: "255"', workflow)


if __name__ == "__main__":
    unittest.main()
