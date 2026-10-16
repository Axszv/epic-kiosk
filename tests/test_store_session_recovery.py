import ast
import asyncio
import sys
import types
import unittest
from contextlib import suppress
from enum import Enum
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from services.epic_store_page import STORE_LOGIN_URL, is_login_url, store_signed_out


class Result(Enum):
    SUCCESS = "success"
    COOKIE_INVALID = "cookie_invalid"
    CAPTCHA_FAILED = "captcha_failed"
    UNKNOWN = "unknown"


def load_function(path, name, namespace, class_name=None):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = tree.body
    if class_name:
        nodes = next(node for node in nodes if isinstance(node, ast.ClassDef) and node.name == class_name).body
    function = next(node for node in nodes if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


class StoreSessionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.page = types.SimpleNamespace(
            url="https://store.epicgames.com/p/test?lang=en-US",
            goto=AsyncMock(), wait_for_timeout=AsyncMock(),
        )
        self.auth = types.SimpleNamespace(reauthenticate_store=AsyncMock(return_value=Result.SUCCESS))
        self.desktop = AsyncMock()
        self.mobile = AsyncMock()
        self.reset = MagicMock()
        self.collect = load_function(ROOT / "app/deploy.py", "collect_with_session_recovery", {
            "EpicAgent": lambda page: types.SimpleNamespace(collect_epic_games=self.desktop),
            "EpicGames": types.SimpleNamespace(reset_order_history_session=self.reset),
            "ErrorType": Result, "GameCollectResult": Result,
            "collect_mobile_offers": self.mobile, "logger": MagicMock(),
        })

    async def test_desktop_signed_out_recovers_before_mobile_and_retries_once(self):
        self.desktop.side_effect = [Result.COOKIE_INVALID, Result.SUCCESS]
        self.mobile.return_value = [{"status": "verified_owned"}]
        result = await self.collect(self.page, self.auth, [{"title": "Mobile"}])
        self.assertEqual(result[2], Result.SUCCESS)
        self.auth.reauthenticate_store.assert_awaited_once()
        self.assertEqual(self.desktop.await_count, 2)
        self.mobile.assert_awaited_once()
        self.reset.assert_called_once_with(self.page)

    async def test_mobile_login_failure_gets_fresh_results_after_recovery(self):
        self.desktop.return_value = Result.SUCCESS
        self.mobile.side_effect = [[{"status": "login_required"}], [{"status": "already_owned"}]]
        result = await self.collect(self.page, self.auth, [{"title": "Mobile"}])
        self.assertEqual(result[1], [{"status": "already_owned"}])
        self.assertEqual(result[2], Result.SUCCESS)
        self.auth.reauthenticate_store.assert_awaited_once()

    async def test_repeated_login_failure_does_not_loop_or_report_success(self):
        self.desktop.return_value = Result.COOKIE_INVALID
        result = await self.collect(self.page, self.auth, [])
        self.assertEqual(result[2], Result.COOKIE_INVALID)
        self.assertEqual(self.desktop.await_count, 2)
        self.auth.reauthenticate_store.assert_awaited_once()

    async def test_failed_authentication_is_preserved_without_retrying_checkout(self):
        self.desktop.return_value = Result.COOKIE_INVALID
        self.auth.reauthenticate_store.return_value = Result.CAPTCHA_FAILED
        result = await self.collect(self.page, self.auth, [])
        self.assertEqual(result[2], Result.CAPTCHA_FAILED)
        self.desktop.assert_awaited_once()
        self.reset.assert_not_called()

    async def test_owned_accounts_do_not_reauthenticate(self):
        self.desktop.return_value = Result.SUCCESS
        await self.collect(self.page, self.auth, [])
        self.auth.reauthenticate_store.assert_not_awaited()

    async def test_store_callback_must_be_verified_even_after_successful_login(self):
        path = ROOT / "app/services/epic_authorization_service.py"
        recover = load_function(path, "reauthenticate_store", {
            "ErrorType": Result, "STORE_LOGIN_URL": STORE_LOGIN_URL, "logger": MagicMock(),
        }, "EpicAuthorization")
        auth = types.SimpleNamespace(
            page=self.page, _refresh_csrf_seen=True, _login_error_code="old-error",
            _is_login_success_signal=asyncio.Queue(), _is_refresh_csrf_signal=asyncio.Queue(),
            _email_transaction_failure_signal=asyncio.Queue(),
            _login=AsyncMock(return_value=(True, Result.SUCCESS)),
            _store_session_ready=AsyncMock(return_value=False), _save_page_debug=AsyncMock(),
        )
        auth._is_refresh_csrf_signal.put_nowait({"csrf": "stale"})
        result = await recover(auth)
        self.assertEqual(result, Result.COOKIE_INVALID)
        self.assertFalse(auth._refresh_csrf_seen)
        self.assertTrue(auth._is_refresh_csrf_signal.empty())
        auth._login.assert_awaited_once_with(login_url=STORE_LOGIN_URL)
        self.page.goto.assert_awaited_once_with(STORE_LOGIN_URL, wait_until="domcontentloaded")

    async def test_visible_sign_in_overrides_a_stale_navigation_flag(self):
        self.page.get_by_role = MagicMock(return_value=types.SimpleNamespace(
            first=types.SimpleNamespace(is_visible=AsyncMock(return_value=True))
        ))
        self.assertTrue(await store_signed_out(self.page))

    async def test_rejected_challenge_can_resume_email_instead_of_waiting_for_password(self):
        nodes = {}
        for selector in ("#email", "#password", "#continue", "#sign-in"):
            node = types.SimpleNamespace(
                is_visible=AsyncMock(return_value=selector in {"#email", "#continue"}),
                is_disabled=AsyncMock(return_value=False), fill=AsyncMock(), click=AsyncMock(),
            )
            node.first = node
            nodes[selector] = node
        self.page.locator = lambda selector: nodes[selector]
        resume = load_function(ROOT / "app/services/epic_authorization_service.py", "_resume_login_form", {
            "suppress": suppress, "logger": MagicMock(),
            "settings": types.SimpleNamespace(
                EPIC_EMAIL="test@example.com",
                EPIC_PASSWORD=types.SimpleNamespace(get_secret_value=lambda: "test-password"),
            ),
        }, "EpicAuthorization")
        self.assertTrue(await resume(types.SimpleNamespace(page=self.page)))
        nodes["#email"].fill.assert_awaited_once_with("test@example.com")
        nodes["#continue"].click.assert_awaited_once()
        nodes["#password"].fill.assert_not_awaited()

        nodes["#continue"].is_disabled.return_value = True
        nodes["#continue"].click.reset_mock()
        self.assertFalse(await resume(types.SimpleNamespace(page=self.page)))
        nodes["#continue"].click.assert_not_awaited()

    def test_login_url_supports_both_store_generations(self):
        for url in (
            "https://www.epicgames.com/id/login?redirect_uri=abc",
            "https://store.epicgames.com/login?state=%2F",
        ):
            self.assertTrue(is_login_url(url))
        for url in (
            "https://store.epicgames.com/p/test?redirect=/id/login",
            "https://notepicgames.com/id/login",
            "https://epicgames.com.example.org/login",
        ):
            self.assertFalse(is_login_url(url))


if __name__ == "__main__":
    unittest.main()
