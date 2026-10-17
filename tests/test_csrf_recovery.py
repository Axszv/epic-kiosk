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
from urllib.parse import unquote


SOURCE = Path(__file__).resolve().parents[1] / "app/services/epic_authorization_service.py"
tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
selected = []
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in {
        "epic_transaction_error_code", "diagnostic_value_fingerprint", "csrf_request_diagnostic",
        "is_epic_email_transaction_failure",
    }:
        selected.append(node)
    elif isinstance(node, ast.ClassDef) and node.name == "ErrorType":
        selected.append(node)
    elif isinstance(node, ast.ClassDef) and node.name == "EpicAuthorization":
        node.body = [method for method in node.body if isinstance(method, ast.AsyncFunctionDef)
                     and method.name in {"_take_email_transaction_failure", "_login"}]
        selected.append(node)
namespace = dict(
    suppress=suppress, hashlib=hashlib, json=json, unquote=unquote, Enum=Enum,
    asyncio=asyncio, AgentV=object, logger=Mock(), PlaywrightTimeoutError=TimeoutError,
    get_login_hcaptcha_agent=lambda page: object(),
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

    async def test_email_csrf_exits_before_solving_another_challenge(self):
        self.auth._email_transaction_failure_signal.put_nowait(self.csrf)
        self.auth._email_transaction_failure_signal.put_nowait(self.captcha)
        node = SimpleNamespace(wait_for=AsyncMock(), get_attribute=AsyncMock(return_value=None),
                               clear=AsyncMock(), type=AsyncMock(), click=AsyncMock())
        self.auth.page = SimpleNamespace(goto=AsyncMock(), wait_for_timeout=AsyncMock(),
                                         locator=Mock(return_value=node))
        self.auth._has_refresh_csrf_session = lambda: False
        self.auth._wait_for_cloudflare_auto_pass = AsyncMock(return_value=True)
        self.auth._solve_pre_password_hcaptcha = AsyncMock(return_value=True)
        result = await self.auth._login()
        self.assertEqual(result, (False, namespace["ErrorType"].COOKIE_INVALID))
        self.auth.page.goto.assert_awaited_once()
        self.auth._solve_pre_password_hcaptcha.assert_awaited_once()
        node.click.assert_awaited_once()


class CsrfDiagnosticTests(unittest.TestCase):
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
