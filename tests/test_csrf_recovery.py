"""Preserve the first CSRF failure through Epic's automatic captcha retry."""

import ast
import asyncio
import hashlib
import json
import unittest
from contextlib import suppress
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import unquote, urlsplit


SOURCE = Path(__file__).resolve().parents[1] / "app/services/epic_authorization_service.py"
tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
selected = []
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "EPIC_EMAIL_TRANSACTION_ERROR_MARKERS"
        for target in node.targets
    ):
        selected.append(node)
    elif isinstance(node, ast.FunctionDef) and node.name in {
        "epic_transaction_error_code", "diagnostic_value_fingerprint", "csrf_request_diagnostic",
        "is_epic_email_transaction_failure",
        "auth_diagnostic_endpoint",
    }:
        selected.append(node)
    elif isinstance(node, ast.ClassDef) and node.name == "ErrorType":
        selected.append(node)
    elif isinstance(node, ast.ClassDef) and node.name == "EpicAuthorization":
        node.body = [method for method in node.body if isinstance(method, ast.AsyncFunctionDef)
                     and method.name in {"_take_email_transaction_failure", "_login",
                                         "_install_live_xsrf_route"}]
        selected.append(node)
namespace = dict(
    suppress=suppress, hashlib=hashlib, json=json, unquote=unquote, urlsplit=urlsplit, Enum=Enum,
    asyncio=asyncio, AgentV=object, logger=Mock(), PlaywrightTimeoutError=TimeoutError,
    get_login_hcaptcha_agent=lambda page: object(),
    replace_login_hcaptcha_agent=lambda page: object(),
    settings=SimpleNamespace(EPIC_EMAIL="test@example.com"),
    expect=lambda node: SimpleNamespace(to_be_enabled=AsyncMock()),
)
exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), namespace)
diagnostic = namespace["csrf_request_diagnostic"]


class CsrfRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.auth = namespace["EpicAuthorization"]()
        self.auth._email_transaction_failure_signal = asyncio.Queue()
        self.auth._password_form_ready = AsyncMock(return_value=False)
        self.csrf = {"status": 409, "code": "errors.com.epicgames.accountportal.csrf_token_invalid",
                     "source": "email"}
        self.captcha = {"status": 400, "code": "errors.com.epicgames.accountportal.captcha_invalid",
                        "source": "email"}

    async def test_native_retry_cannot_hide_csrf_failure(self):
        for rejection in [self.csrf, self.captcha]:
            self.auth._email_transaction_failure_signal.put_nowait(rejection)
        self.assertEqual(await self.auth._take_email_transaction_failure(), self.csrf)
        self.assertTrue(self.auth._email_transaction_failure_signal.empty())

    async def test_response_callback_completion_order_does_not_hide_csrf(self):
        for rejection in [self.captcha, self.csrf]:
            self.auth._email_transaction_failure_signal.put_nowait(rejection)
        self.assertEqual(await self.auth._take_email_transaction_failure(), self.csrf)

    async def test_non_csrf_errors_keep_the_latest_rejection(self):
        latest = dict(self.captcha, source="login")
        for rejection in [self.captcha, latest]:
            self.auth._email_transaction_failure_signal.put_nowait(rejection)
        self.auth._password_form_ready.return_value = True
        self.assertEqual(await self.auth._take_email_transaction_failure(), latest)

    async def test_successful_native_email_retry_wins_over_transient_csrf(self):
        self.auth._email_transaction_failure_signal.put_nowait(self.csrf)
        self.auth._password_form_ready.return_value = True
        self.assertIsNone(await self.auth._take_email_transaction_failure())

    async def test_password_form_does_not_hide_login_api_rejection(self):
        rejection = dict(self.csrf, source="login")
        self.auth._email_transaction_failure_signal.put_nowait(rejection)
        self.auth._password_form_ready.return_value = True
        self.assertEqual(await self.auth._take_email_transaction_failure(), rejection)

    def configure_login_page(self):
        node = SimpleNamespace(wait_for=AsyncMock(), get_attribute=AsyncMock(return_value=None),
                               clear=AsyncMock(), type=AsyncMock(), click=AsyncMock())
        self.auth.page = SimpleNamespace(goto=AsyncMock(), wait_for_timeout=AsyncMock(),
                                         locator=Mock(return_value=node))
        self.auth._has_refresh_csrf_session = lambda: False
        self.auth._wait_for_cloudflare_auto_pass = AsyncMock(return_value=True)
        self.auth._save_page_debug = AsyncMock()
        return node

    async def test_email_csrf_recovers_once_without_resetting_cookies(self):
        node = self.configure_login_page()
        self.auth._email_transaction_failure_signal.put_nowait(self.csrf)
        self.auth._email_transaction_failure_signal.put_nowait(self.captcha)
        attempts = 0

        async def solve(agent):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                self.auth._has_refresh_csrf_session = lambda: True
            return True

        self.auth._solve_pre_password_hcaptcha = AsyncMock(side_effect=solve)
        result = await self.auth._login()
        self.assertEqual(result, (True, namespace["ErrorType"].SUCCESS))
        self.assertEqual(self.auth.page.goto.await_count, 2)
        self.assertEqual(self.auth._solve_pre_password_hcaptcha.await_count, 2)
        self.assertEqual(node.click.await_count, 2)

    async def test_repeated_csrf_stops_after_one_same_session_recovery(self):
        self.configure_login_page()

        async def solve(agent):
            self.auth._email_transaction_failure_signal.put_nowait(self.csrf)
            self.auth._email_transaction_failure_signal.put_nowait(self.captcha)
            return True

        self.auth._solve_pre_password_hcaptcha = AsyncMock(side_effect=solve)
        result = await self.auth._login()
        self.assertEqual(result, (False, namespace["ErrorType"].COOKIE_INVALID))
        self.assertEqual(self.auth.page.goto.await_count, 2)
        self.assertEqual(self.auth._solve_pre_password_hcaptcha.await_count, 2)


class LiveXsrfRouteTests(unittest.IsolatedAsyncioTestCase):
    """The login transaction must carry Epic's live XSRF cookie."""

    def build_page(self, cookies, body="{\"captcha\": \"first\"}"):
        route = SimpleNamespace(
            continue_=AsyncMock(),
            abort=AsyncMock(),
            request=SimpleNamespace(
                url="https://www.epicgames.com/id/api/email/exists",
                headers={"x-xsrf-token": "stale-value"},
                post_data=body,
            ),
        )
        context = SimpleNamespace(cookies=AsyncMock(return_value=cookies))
        return SimpleNamespace(
            context=context,
            route=AsyncMock(),
            url="https://www.epicgames.com/id/login",
        ), route

    async def test_stale_header_is_replaced_with_the_live_cookie(self):
        auth = namespace["EpicAuthorization"]()
        page, route = self.build_page([{"name": "XSRF-TOKEN", "value": "live-value"}])
        auth.page = page

        await auth._install_live_xsrf_route()

        page.route.assert_awaited_once()
        handler = page.route.await_args.args[1]
        await handler(route)
        headers = route.continue_.await_args.kwargs["headers"]
        self.assertEqual(headers["x-xsrf-token"], "live-value")

    async def test_matching_header_is_left_alone(self):
        auth = namespace["EpicAuthorization"]()
        page, route = self.build_page([{"name": "XSRF-TOKEN", "value": "stale-value"}])
        auth.page = page

        await auth._install_live_xsrf_route()

        handler = page.route.await_args.args[1]
        await handler(route)
        route.continue_.assert_awaited_once_with()

    async def test_repeated_transaction_is_dropped(self):
        auth = namespace["EpicAuthorization"]()
        page, route = self.build_page([{"name": "XSRF-TOKEN", "value": "stale-value"}])
        auth.page = page

        await auth._install_live_xsrf_route()

        handler = page.route.await_args.args[1]
        await handler(route)
        # The portal repeats the same transaction with the same captcha token.
        await handler(route)
        route.abort.assert_awaited_once()
        self.assertEqual(route.continue_.await_count, 1)

    async def test_a_new_transaction_is_allowed(self):
        auth = namespace["EpicAuthorization"]()
        page, route = self.build_page([{"name": "XSRF-TOKEN", "value": "live-value"}])
        auth.page = page

        await auth._install_live_xsrf_route()

        handler = page.route.await_args.args[1]
        await handler(route)
        route.request.post_data = "{\"captcha\": \"second\"}"
        await handler(route)
        route.abort.assert_not_awaited()
        self.assertEqual(route.continue_.await_count, 2)


class CsrfDiagnosticTests(unittest.TestCase):
    def test_auth_endpoint_excludes_tokens_and_unrelated_hosts(self):
        endpoint = namespace["auth_diagnostic_endpoint"]
        self.assertEqual(endpoint("https://www.epicgames.com/id/login?state=private"), "/id/login")
        self.assertEqual(endpoint("https://www.epicgames.com/id/api/authenticate"), "/id/api/authenticate")
        self.assertEqual(endpoint("https://www.epicgames.com/id/api/exchange/private"), "")
        self.assertEqual(endpoint("https://unrelated.example/id/api/login"), "")

    def test_compares_decoded_cookie_and_omits_secrets(self):
        result = diagnostic(
            {"X-XSRF-TOKEN": "csrf/value", "Cookie": "other=private; XSRF-TOKEN=csrf%2Fvalue"},
            [{"name": "set-cookie", "value": "XSRF-TOKEN=replacement; Path=/; Secure"},
             {"name": "Set-Cookie", "value": "EPIC_SESSION=private-session; HttpOnly"}],
        )
        self.assertTrue(result["header_matches_cookie"])
        self.assertEqual(result["cookie_count"], 1)
        self.assertEqual(len(result["response_cookies"]), 1)
        for secret in ("csrf/value", "csrf%2Fvalue", "replacement", "private"):
            self.assertNotIn(secret, json.dumps(result))

    def test_duplicate_cookies_remain_observable(self):
        result = diagnostic({"x-xsrf-token": "second", "cookie": "XSRF-TOKEN=first; XSRF-TOKEN=second"}, [])
        self.assertEqual(result["cookie_count"], 2)
        self.assertTrue(result["header_matches_cookie"])
        self.assertNotEqual(result["cookies"][0], result["cookies"][1])

    def test_missing_header_is_not_treated_as_matching_empty_cookie(self):
        result = diagnostic({"cookie": "XSRF-TOKEN="}, [])
        self.assertFalse(result["header_present"])
        self.assertFalse(result["header_matches_cookie"])

    def test_stale_header_is_detected(self):
        result = diagnostic({"x-xsrf-token": "old", "cookie": "XSRF-TOKEN=new"}, [])
        self.assertTrue(result["header_present"])
        self.assertFalse(result["header_matches_cookie"])


if __name__ == "__main__":
    unittest.main()
