"""The Store navigation must not be the only witness of a session.

The navigation writes `isloggedin` from the auth state its client fetched
after the page started, so the first read of a page that follows a login
still says "false". A run that trusted that read threw a working session
away and solved another captcha for it.
"""

import asyncio
import unittest
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services.epic_store_page import (  # noqa: E402
    ORDER_HISTORY_URL,
    account_session_alive,
    store_login_state,
)


class FakeResponse:
    def __init__(self, status, url):
        self.status = status
        self.url = url


class FakeRequest:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    async def get(self, url, headers=None, timeout=None):
        self.calls += 1
        return self.response


class FakeContext:
    def __init__(self, response):
        self.request = FakeRequest(response)


class FakeLocator:
    def __init__(self, page):
        self.page = page

    async def get_attribute(self, name, timeout=0):
        self.page.reads += 1
        if self.page.states:
            return self.page.states.pop(0)
        return self.page.state


class FakePage:
    def __init__(self, states, response=None):
        self.states = list(states)
        self.state = states[-1] if states else "false"
        self.reads = 0
        self.reloads = 0
        self.context = FakeContext(response or FakeResponse(401, "https://login"))

    def locator(self, selector):
        return FakeLocator(self)

    async def wait_for_timeout(self, ms):
        return None

    async def reload(self, wait_until=None):
        self.reloads += 1


def run(coro):
    return asyncio.run(coro)


class StoreLoginStateTests(unittest.TestCase):
    def test_a_signed_in_navigation_is_reported_as_it_is(self):
        page = FakePage(["true"])

        self.assertEqual(run(store_login_state(page)), "true")
        self.assertEqual(page.reloads, 0)

    def test_a_lagging_navigation_is_read_again_before_a_login(self):
        page = FakePage(["false", "true"])

        self.assertEqual(run(store_login_state(page)), "true")

    def test_the_account_api_decides_when_the_navigation_never_agrees(self):
        # This is the run of 2026-10-23: the login was accepted with HTTP 204
        # and the mobile claim of the same session succeeded, while the
        # navigation of the freshly loaded claim page still said "false".
        page = FakePage(
            ["false"],
            FakeResponse(200, ORDER_HISTORY_URL),
        )

        self.assertEqual(run(store_login_state(page)), "true")
        self.assertEqual(page.context.request.calls, 1)

    def test_a_dead_session_is_still_reported_as_signed_out(self):
        page = FakePage(["false"], FakeResponse(401, "https://www.epicgames.com/id/login"))

        self.assertEqual(run(store_login_state(page)), "false")

    def test_a_page_without_a_navigation_is_not_proof_of_a_dead_session(self):
        # The navigation is a rendered element. A page that never finished
        # loading must not be reported as a signed-out profile, or the claim
        # would start a captcha login for a session it cannot see.
        page = FakePage([""], FakeResponse(401, "https://www.epicgames.com/id/login"))

        self.assertEqual(run(store_login_state(page)), "")

    def test_the_probe_needs_the_account_api_itself(self):
        # A redirect to the login page is not an authenticated answer, even
        # when it arrives with HTTP 200.
        page = FakePage(
            ["false"],
            FakeResponse(200, "https://www.epicgames.com/id/login"),
        )

        self.assertFalse(run(account_session_alive(page)))


if __name__ == "__main__":
    unittest.main()
