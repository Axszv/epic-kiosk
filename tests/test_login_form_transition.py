"""Regressions for Epic's off-screen password field on the email form."""

import ast
import asyncio
import time
import types
import unittest
from contextlib import suppress
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


SOURCE = Path(__file__).resolve().parents[1] / "app/services/epic_authorization_service.py"


def load_auth_methods():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "EpicAuthorization")
    names = {"_password_form_ready", "_solve_pre_password_hcaptcha", "_resume_login_form"}
    cls.body = [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name in names]
    namespace = {
        "asyncio": asyncio, "time": time, "suppress": suppress,
        "AgentV": object, "logger": MagicMock(),
        "settings": types.SimpleNamespace(
            LOGIN_RESULT_TIMEOUT_SECONDS=1, EXECUTION_TIMEOUT=120,
            EPIC_EMAIL="test@example.com",
            EPIC_PASSWORD=types.SimpleNamespace(get_secret_value=lambda: "test-password"),
        ),
    }
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace["EpicAuthorization"]


class LoginFormTransitionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.auth = load_auth_methods()()
        self.nodes = {}
        for selector in ("#email", "#password", "#continue", "#sign-in"):
            node = types.SimpleNamespace(
                # This matches the real email form: is_visible returns True
                # for the off-screen password field, but Sign in is absent.
                is_visible=AsyncMock(return_value=selector != "#sign-in"),
                is_disabled=AsyncMock(return_value=False),
                fill=AsyncMock(), click=AsyncMock(),
            )
            node.first = node
            self.nodes[selector] = node
        self.auth.page = types.SimpleNamespace(
            locator=lambda selector: self.nodes[selector], wait_for_timeout=AsyncMock(),
        )
        self.auth._email_transaction_failure_signal = asyncio.Queue()
        self.auth._has_refresh_csrf_session = lambda: False
        self.auth._has_hcaptcha_challenge = AsyncMock(return_value=False)
        self.auth._save_page_debug = AsyncMock()
        self.agent = types.SimpleNamespace(wait_for_challenge=AsyncMock(return_value="success"))

    async def test_offscreen_password_is_not_the_password_step(self):
        self.assertFalse(await self.auth._password_form_ready())
        self.assertTrue(await self.auth._resume_login_form())
        self.nodes["#password"].fill.assert_not_awaited()
        self.nodes["#continue"].click.assert_awaited_once()

    async def test_email_challenge_is_solved_before_real_password_form(self):
        self.auth._has_hcaptcha_challenge.side_effect = [False, True]

        async def solve():
            self.nodes["#sign-in"].is_visible.return_value = True
            return "success"

        self.agent.wait_for_challenge.side_effect = solve
        self.assertTrue(await self.auth._solve_pre_password_hcaptcha(self.agent))
        self.agent.wait_for_challenge.assert_awaited_once()
        self.assertTrue(await self.auth._password_form_ready())
        self.nodes["#password"].fill.assert_not_awaited()

    async def test_epic_rejection_after_captcha_success_is_left_for_recovery(self):
        self.auth._has_hcaptcha_challenge.return_value = True

        async def solve():
            self.auth._email_transaction_failure_signal.put_nowait({"status": 400})
            return "success"

        self.agent.wait_for_challenge.side_effect = solve
        self.assertTrue(await self.auth._solve_pre_password_hcaptcha(self.agent))
        self.assertFalse(self.auth._email_transaction_failure_signal.empty())
        self.assertFalse(await self.auth._password_form_ready())

    async def test_returned_challenge_timeout_does_not_advance_to_password(self):
        self.auth._has_hcaptcha_challenge.return_value = True
        self.agent.wait_for_challenge.return_value = "challenge_execution_timeout"
        self.assertFalse(await self.auth._solve_pre_password_hcaptcha(self.agent))
        self.nodes["#password"].fill.assert_not_awaited()
        self.auth._save_page_debug.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
