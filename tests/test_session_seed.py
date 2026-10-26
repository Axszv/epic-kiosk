"""The exported session is what takes the captcha out of the weekly run."""

import asyncio
import json
import time
import unittest
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services.epic_session_seed import (  # noqa: E402
    cookies_for,
    has_epic_session,
    load_sessions,
    normalise_cookie,
    seed_epic_session,
)


def cookie(name, value="v", domain=".epicgames.com", **extra):
    data = {"name": name, "value": value, "domain": domain, "path": "/"}
    data.update(extra)
    return data


def document(accounts):
    return json.dumps({"version": 1, "accounts": accounts})


class FakeContext:
    def __init__(self, existing=None):
        self.existing = list(existing or [])
        self.added = []

    async def cookies(self):
        return list(self.existing)

    async def add_cookies(self, cookies):
        self.added.extend(cookies)


def run(coro):
    return asyncio.run(coro)


class LoadSessionTests(unittest.TestCase):
    def test_reads_one_entry_per_account(self):
        raw = document(
            [
                {"email": "A@Example.com", "cookies": [cookie("EPIC_SSO")]},
                {"email": "b@example.com", "cookies": [cookie("EPIC_SSO")]},
            ]
        )

        sessions = load_sessions(raw)

        self.assertEqual(set(sessions), {"a@example.com", "b@example.com"})
        self.assertEqual(len(sessions["a@example.com"]), 1)

    def test_an_empty_secret_is_not_an_error(self):
        self.assertEqual(load_sessions(""), {})
        self.assertEqual(load_sessions("not json"), {})

    def test_cookies_of_other_sites_are_dropped(self):
        raw = document(
            [
                {
                    "email": "a@example.com",
                    "cookies": [
                        cookie("EPIC_SSO"),
                        cookie("_ga", domain=".google.com"),
                        cookie("tracker", domain=".example.com"),
                    ],
                }
            ]
        )

        cookies = cookies_for(raw, "a@example.com")

        self.assertEqual([item["name"] for item in cookies], ["EPIC_SSO"])

    def test_expired_cookies_are_dropped(self):
        raw = document(
            [
                {
                    "email": "a@example.com",
                    "cookies": [
                        cookie("old", expires=time.time() - 60),
                        cookie("live", expires=time.time() + 3600),
                    ],
                }
            ]
        )

        cookies = cookies_for(raw, "a@example.com")

        self.assertEqual([item["name"] for item in cookies], ["live"])

    def test_the_lookup_ignores_the_case_of_the_email(self):
        raw = document([{"email": "A@Example.com", "cookies": [cookie("EPIC_SSO")]}])

        self.assertTrue(cookies_for(raw, "a@example.COM"))


class NormaliseCookieTests(unittest.TestCase):
    def test_keeps_the_fields_the_browser_accepts(self):
        normalised = normalise_cookie(
            cookie("EPIC_SSO", "abc", httpOnly=True, secure=True, sameSite="Lax")
        )

        self.assertEqual(normalised["name"], "EPIC_SSO")
        self.assertTrue(normalised["httpOnly"])
        self.assertEqual(normalised["sameSite"], "Lax")

    def test_an_unknown_same_site_value_is_left_out(self):
        normalised = normalise_cookie(cookie("EPIC_SSO", sameSite="weird"))

        self.assertNotIn("sameSite", normalised)

    def test_a_cookie_without_a_name_is_ignored(self):
        self.assertIsNone(normalise_cookie({"value": "v", "domain": ".epicgames.com"}))


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.raw = document(
            [{"email": "a@example.com", "cookies": [cookie("EPIC_SSO", "abc")]}]
        )

    def test_an_empty_profile_is_seeded(self):
        context = FakeContext()

        written = run(seed_epic_session(context, self.raw, "a@example.com"))

        self.assertEqual(written, 1)
        self.assertEqual(context.added[0]["name"], "EPIC_SSO")

    def test_a_signed_in_profile_keeps_its_own_cookies(self):
        # The profile refreshes its cookies on every response, so an older
        # copy of the seed must not replace them.
        context = FakeContext(existing=[cookie("EPIC_SSO", "fresher")])

        written = run(seed_epic_session(context, self.raw, "a@example.com"))

        self.assertEqual(written, 0)
        self.assertEqual(context.added, [])

    def test_a_bootstrap_run_replaces_a_dead_session(self):
        context = FakeContext(existing=[cookie("EPIC_SSO", "dead")])

        written = run(
            seed_epic_session(context, self.raw, "a@example.com", mode="always")
        )

        self.assertEqual(written, 1)

    def test_off_writes_nothing(self):
        context = FakeContext()

        self.assertEqual(
            run(seed_epic_session(context, self.raw, "a@example.com", mode="off")), 0
        )
        self.assertEqual(context.added, [])

    def test_an_account_without_a_seed_writes_nothing(self):
        context = FakeContext()

        self.assertEqual(
            run(seed_epic_session(context, self.raw, "other@example.com")), 0
        )

    def test_has_epic_session_only_looks_at_epic_cookies(self):
        self.assertTrue(has_epic_session([cookie("EPIC_SSO")]))
        self.assertFalse(has_epic_session([cookie("_ga", domain=".google.com")]))
        self.assertFalse(has_epic_session([]))


if __name__ == "__main__":
    unittest.main()
