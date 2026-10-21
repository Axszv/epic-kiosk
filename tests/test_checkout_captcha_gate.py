"""The checkout answers its security check before it looks for the order button."""

import ast
import asyncio
import types
import unittest
from contextlib import suppress
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


SOURCE = Path(__file__).resolve().parents[1] / "app/services/epic_games_service.py"

tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "EpicGames")
cls.body = [
    n
    for n in cls.body
    if isinstance(n, ast.AsyncFunctionDef)
    and n.name
    in {
        "_handle_legacy_instant_checkout",
        "_solve_checkout_captcha",
        "_checkout_has_captcha",
        "_retry_talon_execution",
        "_wait_for_order_button",
    }
]
namespace = {
    "suppress": suppress,
    "asyncio": asyncio,
    "re": __import__("re"),
    "logger": MagicMock(),
    "settings": MagicMock(CHECKOUT_CAPTCHA_TIMEOUT_SECONDS=5),
    "print": lambda *a, **k: None,
    "Page": object,
    "Any": object,
    "get_legacy_checkout_agent": lambda page: MagicMock(),
}
exec(compile(ast.Module(body=[cls], type_ignores=[]), str(SOURCE), "exec"), namespace)


class CheckoutCaptchaGateTests(unittest.IsolatedAsyncioTestCase):
    def build(self, captcha_present: bool):
        auth = namespace["EpicGames"]()
        agent = MagicMock()
        agent.wait_for_challenge = AsyncMock(return_value="success")
        auth._handle_device_not_supported_modal = AsyncMock(return_value=False)
        auth._ensure_purchase_checkout_open = AsyncMock(return_value=True)
        auth._solve_checkout_captcha = AsyncMock(return_value=captcha_present)
        button = MagicMock()
        button.text_content = AsyncMock(return_value="Get")
        button.click = AsyncMock()
        button.is_visible = AsyncMock(return_value=True)
        calls = {"count": 0}

        async def container(page):
            calls["count"] += 1
            if calls["count"] == 1:
                raise AssertionError("Could not find Place Order button")
            return page, button

        auth._active_purchase_container = container
        page = MagicMock()
        page.frames = []
        auth.page = page
        return auth, agent, button, calls

    async def test_the_challenge_is_answered_before_the_button_is_looked_for(self):
        auth, agent, button, calls = self.build(captcha_present=True)

        result = await auth._handle_legacy_instant_checkout(MagicMock())

        auth._solve_checkout_captcha.assert_awaited_once()
        self.assertEqual(calls["count"], 2)
        # The order button is clicked once the challenge is answered.
        button.click.assert_awaited()

    async def test_the_order_form_is_looked_for_again_after_the_challenge(self):
        auth, agent, button, calls = self.build(captcha_present=True)
        auth._wait_for_order_button = AsyncMock(
            return_value=(MagicMock(), button)
        )

        await auth._handle_legacy_instant_checkout(MagicMock())

        auth._wait_for_order_button.assert_awaited_once()

    async def test_without_a_challenge_the_failure_is_reported(self):
        auth, agent, button, calls = self.build(captcha_present=False)

        result = await auth._handle_legacy_instant_checkout(MagicMock())

        auth._solve_checkout_captcha.assert_awaited_once()
        self.assertFalse(result)

    async def test_the_challenge_is_found_in_the_frames_of_the_page(self):
        auth = namespace["EpicGames"]()
        frame = types.SimpleNamespace(url="https://newassets.hcaptcha.com/captcha/v1/abc")
        page = types.SimpleNamespace(frames=[frame], locator=AsyncMock())

        self.assertTrue(await auth._checkout_has_captcha(page))

    async def test_a_page_without_a_challenge_is_recognised(self):
        auth = namespace["EpicGames"]()
        page = types.SimpleNamespace(
            frames=[types.SimpleNamespace(url="https://store.epicgames.com/p/game")],
            locator=AsyncMock(),
        )
        page.locator.return_value.count = AsyncMock(return_value=0)

        self.assertFalse(await auth._checkout_has_captcha(page))


if __name__ == "__main__":
    unittest.main()
