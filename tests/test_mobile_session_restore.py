"""A mobile offer never inherits the login page of the one before it."""

import ast
import asyncio
import types
import unittest
from contextlib import suppress
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


SOURCE = Path(__file__).resolve().parents[1] / "app/services/epic_mobile_service.py"

tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
func = next(
    n
    for n in tree.body
    if isinstance(n, ast.AsyncFunctionDef) and n.name == "collect_mobile_offers"
)
namespace = {
    "suppress": suppress,
    "asyncio": asyncio,
    "logger": MagicMock(),
    "EpicGames": MagicMock(),
    "emit_result": lambda result: None,
    "emit_order_check": lambda *a, **k: None,
    "offer_identity": lambda offer: str(offer.get("title") or ""),
    "order_history_has_offer_id": lambda data, offer_id: False,
    "collect_mobile_offer": AsyncMock(),
    "load_mobile_offers": lambda raw: [],
}
exec(compile(ast.Module(body=[func], type_ignores=[]), str(SOURCE), "exec"), namespace)


class MobileSessionRestoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_session_is_put_back_before_every_offer(self):
        # The account login page that a mobile offer opens without a session
        # replaces the session cookies of the profile.
        context = types.SimpleNamespace(add_cookies=AsyncMock())
        page = types.SimpleNamespace(context=context)
        namespace["collect_mobile_offer"] = AsyncMock(
            return_value={"platform": "Mobile", "title": "x", "status": "already_owned"}
        )
        namespace["EpicGames"].fetch_order_history = AsyncMock(return_value={})
        offers = [
            {"platform": "Android", "title": "a", "url": "https://x/a"},
            {"platform": "iOS", "title": "b", "url": "https://x/b"},
        ]
        cookies = [{"name": "refresh_csrf", "value": "v"}]

        await namespace["collect_mobile_offers"](page, offers, session_cookies=cookies)

        self.assertEqual(context.add_cookies.await_count, len(offers))

    async def test_the_collection_works_without_a_session(self):
        page = types.SimpleNamespace(
            context=types.SimpleNamespace(add_cookies=AsyncMock())
        )
        namespace["collect_mobile_offer"] = AsyncMock(
            return_value={"platform": "Mobile", "title": "x", "status": "already_owned"}
        )
        namespace["EpicGames"].fetch_order_history = AsyncMock(return_value={})
        offers = [{"platform": "Android", "title": "a", "url": "https://x/a"}]

        results = await namespace["collect_mobile_offers"](page, offers)

        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
