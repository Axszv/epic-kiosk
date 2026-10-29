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


class ProductCtaScopeTests(unittest.TestCase):
    """The page repeats `Add to Cart` on everything it sells.

    `Sker Ritual` and its `Shrunken Head DLC` both offer that label, and the
    add-on's card can be on screen before the game's own purchase area has
    hydrated. On 2026-10-28 at 21:40 the run clicked the first match, the
    store answered `POST /proxy/api/cart` with 201, and the cart's only line
    was the DLC — which then disabled the checkout for every run that
    followed, because Epic refuses a DLC whose base game is missing.
    """

    def setUp(self):
        self.source = (
            Path(__file__).resolve().parents[1]
            / "app/services/epic_store_page.py"
        ).read_text(encoding="utf-8")
        start = self.source.index("async def product_cta")
        self.body = self.source[start: start + 4000]

    def test_an_add_on_card_is_not_the_product_cta(self):
        self.assertIn("const own = node =>", self.body)
        self.assertIn("a[href*='/p/']", self.body)
        self.assertIn("other !== slug", self.body)
        self.assertIn("if (!own(node)) continue;", self.body)

    def test_the_labelled_lookup_stays_as_the_fallback(self):
        # A page this cannot read has to behave exactly as it did before.
        self.assertIn("get_by_role(\"button\", name=PRODUCT_CTA_TEXT)", self.body)
        self.assertIn("button[data-testid='purchase-cta-button']", self.body)
