"""The account portal asks for a confirmation that the Store session lacks.

`accounts.epicgames.com` answers 401 to a Store-only session and sends the
browser to `/id/login/switch-account`. Until that page is confirmed, the
order history and the checkout fail with `cookie_invalid` while the Store
still reports the visitor as signed in, which is what the 2026-10-25 run of
account 3 showed.
"""

import asyncio
import unittest
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services import epic_store_page  # noqa: E402
from services.epic_store_page import (  # noqa: E402
    complete_account_switch,
    is_switch_account_url,
)


OBSERVED = (
    "https://www.epicgames.com/id/login/switch-account?redirect_uri=https%3A%2F"
    "%2Faccounts.epicgames.com%2Fauth%2Fcallback&response_type=code"
    "&client_id=007c0bfe154c4f5396648f013c641dcf&lang=en-US&state=dXYnVMtz"
)
ACCOUNT_PAGE = "https://accounts.epicgames.com/account/transactions?lang=en-US"


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self.selector in self.page.controls else 0

    async def is_visible(self, timeout=0):
        return True

    async def click(self, timeout=0):
        self.page.clicked.append(self.selector)
        if self.page.controls[self.selector] == "continue":
            self.page.url = ACCOUNT_PAGE


class FakePage:
    def __init__(self, url, controls=None):
        self.url = url
        self.controls = controls or {}
        self.clicked = []

    def locator(self, selector):
        return FakeLocator(self, selector)

    async def wait_for_timeout(self, ms):
        return None

    async def content(self):
        return "<html></html>"

    async def screenshot(self, path):
        return None


def run(coro):
    return asyncio.run(coro)


class SwitchAccountTests(unittest.TestCase):
    def setUp(self):
        # The dump writes into the run directory; the test only needs the flow.
        self.original_dump = epic_store_page._dump_page

        async def no_dump(page, label):
            return None

        epic_store_page._dump_page = no_dump

    def tearDown(self):
        epic_store_page._dump_page = self.original_dump

    def test_the_observed_redirect_is_recognised(self):
        self.assertTrue(is_switch_account_url(OBSERVED))
        self.assertFalse(is_switch_account_url(ACCOUNT_PAGE))
        self.assertFalse(is_switch_account_url("https://www.epicgames.com/id/login"))

    def test_the_confirmation_is_clicked_and_reported(self):
        page = FakePage(OBSERVED, {"button:has-text('Continue')": "continue"})

        self.assertTrue(run(complete_account_switch(page)))
        self.assertEqual(page.url, ACCOUNT_PAGE)

    def test_an_unknown_page_is_reported_without_clicking(self):
        page = FakePage(OBSERVED)

        self.assertFalse(run(complete_account_switch(page)))
        self.assertEqual(page.clicked, [])

    def test_a_page_that_is_not_the_switch_page_is_left_alone(self):
        page = FakePage(ACCOUNT_PAGE, {"button:has-text('Continue')": "continue"})

        self.assertFalse(run(complete_account_switch(page)))
        self.assertEqual(page.clicked, [])


if __name__ == "__main__":
    unittest.main()
