import ast
import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUTHORIZATION_SOURCE = ROOT / "app" / "services" / "epic_authorization_service.py"


def load_detectors():
    tree = ast.parse(AUTHORIZATION_SOURCE.read_text(encoding="utf-8"))
    selected = []
    wanted = {
        "CLOUDFLARE_TITLE_MARKERS",
        "CLOUDFLARE_BODY_MARKERS",
        "EPIC_HCAPTCHA_HTML_MARKERS",
        "EPIC_EMAIL_TRANSACTION_ERROR_MARKERS",
        "EPIC_DIAGNOSTIC_SECRET_MARKERS",
        "is_cloudflare_security_check",
        "is_epic_hcaptcha_challenge",
        "is_epic_email_transaction_failure",
        "is_recoverable_epic_captcha_error",
        "sanitize_epic_response_payload",
        "diagnostic_value_fingerprint",
        "summarize_epic_request_secrets",
    }
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if names & wanted:
                selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in wanted:
            selected.append(node)
    module = ast.Module(body=selected, type_ignores=[])
    namespace = {"hashlib": hashlib, "json": json}
    exec(compile(module, str(AUTHORIZATION_SOURCE), "exec"), namespace)
    return (
        namespace["is_cloudflare_security_check"],
        namespace["is_epic_hcaptcha_challenge"],
        namespace["is_epic_email_transaction_failure"],
        namespace["is_recoverable_epic_captcha_error"],
        namespace["sanitize_epic_response_payload"],
        namespace["diagnostic_value_fingerprint"],
        namespace["summarize_epic_request_secrets"],
    )


(
    is_cloudflare_security_check,
    is_epic_hcaptcha_challenge,
    is_epic_email_transaction_failure,
    is_recoverable_epic_captcha_error,
    sanitize_epic_response_payload,
    diagnostic_value_fingerprint,
    summarize_epic_request_secrets,
) = load_detectors()


class CloudflareDetectionTests(unittest.TestCase):
    def test_detects_title_interstitial(self):
        self.assertTrue(is_cloudflare_security_check("Just a moment...", ""))

    def test_epic_one_more_step_text_alone_is_not_cloudflare(self):
        self.assertFalse(
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


class EpicHcaptchaDetectionTests(unittest.TestCase):
    def test_detects_email_exists_hcaptcha_container(self):
        self.assertTrue(
            is_epic_hcaptcha_challenge(
                '<div id="h_captcha_challenge_email_exists_prod" '
                'class="h_captcha_challenge"><iframe title="hCaptcha challenge"></iframe></div>'
            )
        )

    def test_detects_hcaptcha_iframe_url(self):
        self.assertTrue(
            is_epic_hcaptcha_challenge(
                '<iframe src="https://newassets.hcaptcha.com/captcha/v1/hash/static/hcaptcha.html">'
            )
        )

    def test_does_not_match_normal_login_form(self):
        self.assertFalse(
            is_epic_hcaptcha_challenge('<input id="email"><input id="password">')
        )

    def test_post_login_validation_is_bounded_and_discards_stale_challenge(self):
        source = AUTHORIZATION_SOURCE.read_text(encoding="utf-8")
        start = source.index("async def _handle_right_account_validation")
        end = source.index("async def _login", start)
        branch = source[start:end]
        self.assertIn("prepare_for_new_challenge = getattr", branch)
        self.assertIn("validation_deadline = time.monotonic() + 30", branch)
        self.assertIn("time.monotonic() < validation_deadline", branch)

    def test_email_continue_waits_until_enabled_without_navigation_wait(self):
        source = AUTHORIZATION_SOURCE.read_text(encoding="utf-8")
        self.assertGreaterEqual(
            source.count('expect(continue_button).to_be_enabled(timeout=30000)'),
            2,
        )
        self.assertGreaterEqual(
            source.count('continue_button.click(timeout=5000, no_wait_after=True)'),
            2,
        )

    def test_email_continue_timeout_defers_to_captcha_state_observer(self):
        source = AUTHORIZATION_SOURCE.read_text(encoding="utf-8")
        start = source.index("# 2. 点击继续按钮")
        end = source.index("if not await self._solve_pre_password_hcaptcha", start)
        branch = source[start:end]

        self.assertIn("except PlaywrightTimeoutError:", branch)
        self.assertIn("继续观察 hCaptcha 或密码页状态", branch)
        self.assertNotIn("if not await self._has_hcaptcha_challenge()", branch)

    def test_email_transaction_recovery_resets_hcaptcha_agent(self):
        source = AUTHORIZATION_SOURCE.read_text(encoding="utf-8")
        start = source.index("async def recover_email_transaction")
        end = source.index("# Keep this coordinator alive", start)
        branch = source[start:end]

        self.assertIn("prepare_for_new_challenge = getattr", branch)
        self.assertIn("prepare_for_new_challenge()", branch)
        self.assertIn("恢复邮箱事务的继续按钮点击等待超时", branch)


class EpicEmailTransactionTests(unittest.TestCase):
    def test_detects_epic_rejection_statuses(self):
        self.assertTrue(is_epic_email_transaction_failure(409, ""))
        self.assertTrue(is_epic_email_transaction_failure(400, ""))

    def test_detects_rendered_incorrect_response_message(self):
        self.assertTrue(
            is_epic_email_transaction_failure(
                200, "Incorrect response. Please refresh the page."
            )
        )

    def test_does_not_match_successful_email_response(self):
        self.assertFalse(is_epic_email_transaction_failure(200, '{"exists":true}'))

    def test_recovers_login_captcha_and_csrf_rejections(self):
        self.assertTrue(
            is_recoverable_epic_captcha_error(
                "errors.com.epicgames.accountportal.captcha_invalid"
            )
        )
        self.assertTrue(
            is_recoverable_epic_captcha_error(
                "errors.com.epicgames.accountportal.csrf_token_invalid"
            )
        )
        self.assertFalse(
            is_recoverable_epic_captcha_error(
                "errors.com.epicgames.account.invalid_account_credentials"
            )
        )


class EpicResponseDiagnosticTests(unittest.TestCase):
    def test_redacts_nested_credentials_and_captcha_values(self):
        payload = {
            "errorCode": "errors.com.epicgames.purchase.invalid_captcha",
            "errorMessage": "The order was rejected",
            "captchaToken": "captcha-value",
            "details": {
                "authorization": "Bearer value",
                "cookieValue": "session=value",
                "reason": "captcha validation failed",
            },
        }

        sanitized = sanitize_epic_response_payload(payload)

        self.assertEqual(
            sanitized["errorCode"],
            "errors.com.epicgames.purchase.invalid_captcha",
        )
        self.assertEqual(sanitized["captchaToken"], "***")
        self.assertEqual(sanitized["details"]["authorization"], "***")
        self.assertEqual(sanitized["details"]["cookieValue"], "***")
        self.assertEqual(
            sanitized["details"]["reason"], "captcha validation failed"
        )

    def test_truncates_unbounded_response_strings(self):
        sanitized = sanitize_epic_response_payload(
            {"errorMessage": "x" * 20}, max_string_length=8
        )
        self.assertEqual(sanitized["errorMessage"], "xxxxxxxx...<truncated>")

    def test_request_secret_summary_keeps_only_fingerprints(self):
        payload = {
            "syncToken": "sync-value",
            "order": {
                "captchaResponse": "P1_secret-value",
                "offerId": "offer-value",
            },
        }

        summary = summarize_epic_request_secrets(payload)

        self.assertEqual(
            summary["syncToken"], diagnostic_value_fingerprint("sync-value")
        )
        self.assertEqual(
            summary["order.captchaResponse"],
            diagnostic_value_fingerprint("P1_secret-value"),
        )
        self.assertNotIn("order.offerId", summary)
        self.assertNotIn("P1_secret-value", json.dumps(summary))

    def test_talon_execute_is_included_in_sanitized_diagnostics(self):
        source = (ROOT / "app/services/epic_authorization_service.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"talon-service-prod" in r.url', source)
        self.assertIn('"/execute" in r.url', source)
        self.assertIn("Epic Talon execute response", source)
        self.assertNotIn('or "talon" in r.url', source)


if __name__ == "__main__":
    unittest.main()
