import ast
import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE_SOURCE = ROOT / "app" / "services" / "epic_games_service.py"
WORKFLOW_SOURCE = ROOT / ".github" / "workflows" / "epic-claim.yml"


class CheckoutRecoveryTests(unittest.TestCase):
    @staticmethod
    def _load_diagnostic_helpers():
        tree = ast.parse(SERVICE_SOURCE.read_text(encoding="utf-8"))
        names = {
            "CHECKOUT_DIAGNOSTIC_URL_MARKERS",
            "CHECKOUT_DIAGNOSTIC_SECRET_MARKERS",
            "is_checkout_diagnostic_url",
            "diagnostic_fingerprint",
            "summarize_checkout_secrets",
            "parse_checkout_diagnostic_body",
            "checkout_body_summary",
        }
        selected = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in names
                for target in node.targets
            ):
                selected.append(node)
            elif isinstance(node, ast.FunctionDef) and node.name in names:
                selected.append(node)
        namespace = {
            "Any": object,
            "hashlib": hashlib,
            "json": json,
            "parse_qsl": __import__("urllib.parse", fromlist=["parse_qsl"]).parse_qsl,
            "urlsplit": __import__("urllib.parse", fromlist=["urlsplit"]).urlsplit,
            "suppress": __import__("contextlib", fromlist=["suppress"]).suppress,
        }
        exec(
            compile(
                ast.Module(body=selected, type_ignores=[]),
                str(SERVICE_SOURCE),
                "exec",
            ),
            namespace,
        )
        return namespace

    def test_checkout_waits_for_delayed_hcaptcha_signals(self):
        raw_source = SERVICE_SOURCE.read_text(encoding="utf-8")
        source = ast.unparse(ast.parse(raw_source))
        self.assertIn("wait_for_challenge_start", source)
        self.assertIn("timeout_seconds=45", source)
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
        self.assertIn("fresh_profile:", workflow)
        self.assertIn("EPIC_FRESH_PROFILE:", workflow)
        self.assertIn("github.event.inputs.fresh_profile || 'false'", workflow)
        self.assertIn("confirm_delay_ms:", workflow)
        self.assertIn("CHECKOUT_CONFIRM_DELAY_MS:", workflow)
        self.assertIn("github.event.inputs.confirm_delay_ms || '0'", workflow)

    def test_workflow_repairs_incomplete_camoufox_addon_cache(self):
        workflow = WORKFLOW_SOURCE.read_text(encoding="utf-8")
        self.assertIn('CAMOUFOX_UBO_DIR="${HOME}/.cache/camoufox/addons/UBO"', workflow)
        self.assertIn('! -f "${CAMOUFOX_UBO_DIR}/manifest.json"', workflow)
        self.assertIn('rm -rf "${CAMOUFOX_UBO_DIR}"', workflow)
        self.assertIn('test -f "${CAMOUFOX_UBO_DIR}/manifest.json"', workflow)

    def test_fresh_profile_diagnostic_uses_an_uncached_override(self):
        settings_source = (ROOT / "app" / "settings.py").read_text(encoding="utf-8")
        runner_source = (
            ROOT / "scripts" / "github_actions_claim_once.py"
        ).read_text(encoding="utf-8")

        self.assertIn('os.getenv("EPIC_USER_DATA_DIR", "")', settings_source)
        self.assertIn('os.getenv("EPIC_FRESH_PROFILE", "false")', runner_source)
        self.assertIn('env["EPIC_USER_DATA_DIR"] = str(profile)', runner_source)
        self.assertIn("tempfile.gettempdir()", runner_source)
        self.assertNotIn("app/volumes/user_data", runner_source[runner_source.index("if fresh_profile:"):runner_source.index("timeout_seconds", runner_source.index("if fresh_profile:"))])

    def test_checkout_builds_request_after_validated_captcha_callback(self):
        source = SERVICE_SOURCE.read_text(encoding="utf-8")
        start = source.index("async def _handle_instant_checkout")
        end = source.index("async def add_promotion_to_cart", start)
        branch = source[start:end]

        self.assertIn("replace_hcaptcha_agent(page)", branch)
        self.assertIn("confirm_order_responses", branch)
        self.assertIn("agent.prepare_for_new_challenge()", branch)
        self.assertIn("await page.wait_for_timeout(3000)", branch)
        self.assertNotIn("hold_first_confirm_order", branch)
        self.assertNotIn("captcha_validation_done", branch)
        self.assertIn("Submitting checkout [initial]", branch)
        self.assertIn("Submitting checkout [validated]", branch)
        self.assertIn('"/purchase/confirm-order"', branch)
        prepare_index = branch.index("agent.prepare_for_new_challenge()")
        initial_click_index = branch.index(
            "await payment_btn.click(force=True)", prepare_index
        )
        settle_index = branch.index(
            "await page.wait_for_timeout(3000)", initial_click_index
        )
        self.assertLess(prepare_index, initial_click_index)
        self.assertLess(initial_click_index, settle_index)
        self.assertEqual(branch.count("await payment_btn.click(force=True)"), 2)
        self.assertIn("confirm_order_request_counter", branch)
        self.assertIn("payment_btn.is_visible(timeout=500)", branch)
        self.assertIn("CHECKOUT_BUTTON_STATE", source)
        self.assertIn("await self._active_purchase_container(page)", branch)
        self.assertIn("Reacquire both the frame and button", branch)
        self.assertIn("Discarded stale automatic confirm-order", branch)

    def test_mobile_checkout_does_not_repeat_an_expensive_rejected_transaction(self):
        source = (ROOT / "app" / "services" / "epic_mobile_service.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("_allow_captcha_retry", source)
        self.assertNotIn("retried_after_explicit_captcha_rejection", source)
        self.assertEqual(source.count("await epic._handle_instant_checkout(page)"), 1)

    def test_checkout_scans_rendered_buttons_before_selector_fallbacks(self):
        source = SERVICE_SOURCE.read_text(encoding="utf-8")
        start = source.index("async def _active_purchase_container")
        end = source.index("async def _handle_device_not_supported_modal", start)
        branch = source[start:end]
        rendered_scan = branch.index('container.locator("button").all()')
        text_fallback = branch.index('container.locator("button", has_text=text_value)')
        self.assertLess(rendered_scan, text_fallback)
        self.assertIn("timeout: int = 500", branch)

    def test_checkout_does_not_hold_the_stale_confirm_order_request(self):
        source = SERVICE_SOURCE.read_text(encoding="utf-8")
        start = source.index("async def _handle_instant_checkout")
        end = source.index("async def add_promotion_to_cart", start)
        branch = source[start:end]

        self.assertNotIn("await page.route(URL_CONFIRM_ORDER", branch)
        self.assertNotIn("await route.continue_()", branch)
        self.assertIn("Submitting checkout [validated]", branch)

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

    def test_checkout_network_diagnostics_cover_epic_risk_endpoints(self):
        helpers = self._load_diagnostic_helpers()
        matches = helpers["is_checkout_diagnostic_url"]

        self.assertTrue(matches("https://talon-service-prod.ecosec.on.epicgames.com/v1/execute"))
        self.assertTrue(matches("https://payment-website-pci.ol.epicgames.com/purchase/confirm-order"))
        self.assertTrue(matches("https://payment.epicgames.com/proxy?target=/purchase/confirm-order"))
        self.assertTrue(matches("https://store.epicgames.com/api/risk/evaluate"))
        self.assertFalse(matches("https://store.epicgames.com/en-US/p/example"))

    def test_checkout_route_matches_query_string_variants(self):
        source = SERVICE_SOURCE.read_text(encoding="utf-8")
        self.assertIn('URL_CONFIRM_ORDER = "**/purchase/confirm-order**"', source)
        self.assertIn('"/purchase/confirm-order" in request.url', source)

    def test_checkout_network_diagnostics_only_emit_secret_fingerprints(self):
        helpers = self._load_diagnostic_helpers()
        summarize = helpers["checkout_body_summary"]
        token = "captcha-secret-value"

        summary = summarize(
            json.dumps(
                {
                    "offerId": "visible-offer-id",
                    "security": {"captchaToken": token},
                }
            )
        )

        self.assertEqual(set(summary), {"security.captchaToken"})
        self.assertEqual(summary["security.captchaToken"]["length"], len(token))
        self.assertNotIn(token, json.dumps(summary))


if __name__ == "__main__":
    unittest.main()
