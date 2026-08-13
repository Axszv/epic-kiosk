import ast
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
        "is_cloudflare_security_check",
        "is_epic_hcaptcha_challenge",
        "is_epic_email_transaction_failure",
        "is_recoverable_epic_captcha_error",
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
    return (
        namespace["is_cloudflare_security_check"],
        namespace["is_epic_hcaptcha_challenge"],
        namespace["is_epic_email_transaction_failure"],
        namespace["is_recoverable_epic_captcha_error"],
    )


(
    is_cloudflare_security_check,
    is_epic_hcaptcha_challenge,
    is_epic_email_transaction_failure,
    is_recoverable_epic_captcha_error,
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


if __name__ == "__main__":
    unittest.main()
