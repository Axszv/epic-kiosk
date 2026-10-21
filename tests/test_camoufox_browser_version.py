"""Epic only accepts a login from a browser it recognises."""

import unittest
from pathlib import Path


INSTALLER = (
    Path(__file__).resolve().parents[1] / "scripts/install_camoufox_browser.py"
)


class CamoufoxBrowserVersionTests(unittest.TestCase):
    def setUp(self):
        self.installer = INSTALLER.read_text(encoding="utf-8")

    def test_the_browser_build_is_the_one_epic_accepted_a_login_with(self):
        # The build that logged in on 2026-10-20 was v158.0-beta.40. The
        # August build that was pinned instead leaves the page without a
        # captcha plan, so it never calls Epic's Talon service and every
        # transaction is refused with `captcha_invalid`.
        self.assertIn('REQUIRED_VERSION = "158.0-beta.40"', self.installer)

    def test_the_old_build_is_not_pinned_any_more(self):
        self.assertNotIn("152.0.4-beta.29", self.installer)
        self.assertNotIn("PINNED_URL", self.installer)

    def test_a_different_build_is_refused_outright(self):
        # A silent fallback would run the weekly claim against a browser
        # Epic refuses, so the mismatch has to stop the run.
        self.assertIn("if fetcher.verstr != REQUIRED_VERSION:", self.installer)
        self.assertIn("raise RuntimeError(", self.installer)


if __name__ == "__main__":
    unittest.main()
