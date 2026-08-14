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
        self.assertIn("_is_main_page_product_cta", branch)
        self.assertIn("Skipping the product-page CTA", branch)

    def test_checkout_reopens_a_missing_purchase_iframe(self):
        source = SERVICE_SOURCE.read_text(encoding="utf-8")
        self.assertIn("async def _ensure_purchase_checkout_open", source)
        self.assertIn("Purchase iframe is not open", source)
        modal_start = source.index("async def _handle_device_not_supported_modal")
        reopen_start = source.index("async def _ensure_purchase_checkout_open")
        uk_start = source.index("async def _uk_confirm_order")
        self.assertLess(modal_start, reopen_start)
        self.assertLess(reopen_start, uk_start)
        start = source.index("async def _handle_instant_checkout")
        end = source.index("async def add_promotion_to_cart", start)
        branch = source[start:end]
        self.assertIn("await self._ensure_purchase_checkout_open(page)", branch)

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
        self.assertIn('default: "300"', workflow)
        self.assertIn("github.event.inputs.execution_timeout || '300'", workflow)
        self.assertIn('CHECKOUT_CAPTCHA_TIMEOUT_SECONDS: "375"', workflow)
        self.assertIn("Collect hCaptcha debug artifacts", workflow)
        self.assertIn("cp -a /tmp/hcaptcha/.cache/.", workflow)
        self.assertIn("cp -a /tmp/hcaptcha/.challenge/.", workflow)

    def test_checkout_retries_the_validated_transaction_before_a_fresh_one(self):
        source = SERVICE_SOURCE.read_text(encoding="utf-8")
        start = source.index("async def _handle_instant_checkout")
        end = source.index("async def add_promotion_to_cart", start)
        branch = source[start:end]

        self.assertIn("for transaction_attempt in range(1, 3)", branch)
        self.assertIn("confirm_order_responses", branch)
        self.assertIn("is_retryable_confirm_order_failure", branch)
        self.assertIn("no_submission_seen = not outcome", branch)
        self.assertIn("agent.prepare_for_new_challenge()", branch)
        self.assertIn("challenge_succeeded", branch)
        self.assertIn(
            "Retrying Epic confirm-order with the validated ",
            branch,
        )
        self.assertIn("await page.wait_for_timeout(2500)", branch)
        self.assertGreaterEqual(
            branch.count("await self._active_purchase_container(page)"),
            2,
        )
        self.assertEqual(branch.count("await payment_btn.click(force=True)"), 2)

    def test_captcha_failure_detector_is_exact(self):
        tree = ast.parse(SERVICE_SOURCE.read_text(encoding="utf-8"))
        selected = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name)
                and target.id == "EPIC_CAPTCHA_CHALLENGE_FAILED"
                for target in node.targets
            ):
                selected.append(node)
            elif (
                isinstance(node, ast.FunctionDef)
                and node.name == "is_retryable_confirm_order_failure"
            ):
                selected.append(node)
        namespace = {"Any": object}
        exec(
            compile(
                ast.Module(body=selected, type_ignores=[]),
                str(SERVICE_SOURCE),
                "exec",
            ),
            namespace,
        )
        detector = namespace["is_retryable_confirm_order_failure"]

        self.assertTrue(
            detector(
                400,
                {"errorCode": "epic.error.captcha.challenge.failed"},
            )
        )
        self.assertFalse(
            detector(
                409,
                {"errorCode": "epic.error.captcha.challenge.failed"},
            )
        )
        self.assertFalse(detector(400, {"errorCode": "some.other.error"}))


if __name__ == "__main__":
    unittest.main()
