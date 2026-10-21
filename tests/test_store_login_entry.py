"""The login has to start from the Store's client/redirect context."""

import unittest
from pathlib import Path


SOURCE = (
    Path(__file__).resolve().parents[1]
    / "app/services/epic_authorization_service.py"
)


class StoreLoginEntryTests(unittest.TestCase):
    def setUp(self):
        self.source = SOURCE.read_text(encoding="utf-8")

    def test_the_login_starts_at_the_store(self):
        # A bare /id/login creates an account-portal session that still needs
        # a second Store login, and the captcha of such a session is never
        # executed by Epic's Talon service.
        self.assertIn("self._login(login_url=STORE_LOGIN_URL)", self.source)
        self.assertIn("point_url = login_url or ", self.source)

    def test_the_store_session_is_checked_after_the_login_url_opens(self):
        self.assertIn("if login_url and await self._store_session_ready():", self.source)
        self.assertIn("async def _store_session_ready(self)", self.source)

    def test_the_store_login_url_is_the_signed_out_entry(self):
        store_page = (
            Path(__file__).resolve().parents[1] / "app/services/epic_store_page.py"
        ).read_text(encoding="utf-8")
        self.assertIn("STORE_LOGIN_URL", store_page)
        self.assertIn("https://store.epicgames.com/login", store_page)


if __name__ == "__main__":
    unittest.main()
