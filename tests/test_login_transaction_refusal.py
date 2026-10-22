"""A refused login transaction must not end the login of the account."""

import ast
import asyncio
import json
import os
import re
import types
import unittest
from contextlib import suppress
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


SOURCE = Path(__file__).resolve().parents[1] / "app/services/epic_authorization_service.py"

tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "EpicAuthorization")
cls.body = [n for n in cls.body
            if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
            and n.name in {"__init__", "_take_login_transaction_failure",
                           "_take_email_transaction_failure"}]
namespace = {
    "asyncio": asyncio,
    "Page": object,
    "suppress": suppress,
    "logger": MagicMock(),
    "json": json,
    "os": os,
    "re": re,
}
exec(compile(ast.Module(body=[cls], type_ignores=[]), str(SOURCE), "exec"), namespace)


class LoginTransactionRefusalTests(unittest.IsolatedAsyncioTestCase):
    def build(self):
        page = types.SimpleNamespace(
            locator=MagicMock(return_value=types.SimpleNamespace(
                inner_text=AsyncMock(return_value="")
            ))
        )
        return namespace["EpicAuthorization"](page)

    async def test_the_refusal_of_the_login_transaction_is_its_own(self):
        auth = self.build()
        auth._login_transaction_failure_signal.put_nowait({"status": 400})
        auth._email_transaction_failure_signal.put_nowait({"status": 409})

        refusal = await auth._take_login_transaction_failure()

        self.assertEqual(refusal["status"], 400)
        # The email transaction keeps its own refusal.
        self.assertEqual(
            (await auth._take_email_transaction_failure())["status"], 409
        )

    async def test_a_login_without_a_refusal_reports_none(self):
        auth = self.build()

        self.assertIsNone(await auth._take_login_transaction_failure())

    def test_the_refusal_of_the_login_does_not_end_the_login(self):
        source = SOURCE.read_text(encoding="utf-8")

        # The refusal of the login transaction is handled by the captcha
        # coordinator, which submits the password again.
        self.assertIn("_take_login_transaction_failure()", source)
        self.assertIn("retrigger_security_check()", source)
        # A wrong answer is a lost round, not a lost login.
        self.assertIn("Epic 拒绝了验证码答案，等待下一轮挑战", source)
        # Only the refusals of the email transaction count against the login.
        self.assertIn('"source": "login",', source)


if __name__ == "__main__":
    unittest.main()
