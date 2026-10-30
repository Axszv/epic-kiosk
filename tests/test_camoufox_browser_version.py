"""Epic only accepts a login from a browser it recognises."""

import unittest
from pathlib import Path


INSTALLER = (
    Path(__file__).resolve().parents[1] / "scripts/install_camoufox_browser.py"
)


class CamoufoxBrowserVersionTests(unittest.TestCase):
    def setUp(self):
        self.installer = INSTALLER.read_text(encoding="utf-8")

    def test_the_browser_build_is_the_one_epic_answers_with_a_captcha(self):
        # 158.0.1-beta.41 opens the checkout without ever starting the hCaptcha
        # widget: run 41053940945 logged 0 challenge tasks across eight
        # accounts, where 158.0-beta.40 logged 14 in a single run. The newest
        # release is therefore not the one to install.
        self.assertIn('PINNED_RELEASE = "158.0-beta.40"', self.installer)
        self.assertIn("fetcher._url = PINNED_URL", self.installer)

    def test_the_pinned_release_is_the_one_the_library_serves(self):
        # A tag that does not exist on the release page fails the install
        # rather than silently running an older browser.
        self.assertIn(
            "releases/download/", self.installer
        )
        self.assertIn("camoufox-{PINNED_RELEASE}-lin.x86_64.zip", self.installer)

    def test_the_august_build_never_comes_back(self):
        # 152.0.4-beta.29 left the page without a captcha plan, so Epic never
        # called Talon and every transaction was refused with `captcha_invalid`.
        # Pinning by URL is deliberate now, but only to 158.0-beta.40.
        self.assertNotIn("152.0.4-beta.29", self.installer)
        self.assertIn('PINNED_RELEASE = "158.0-beta.40"', self.installer)

    def test_the_wrong_platform_is_refused_outright(self):
        # A silent fallback would run the weekly claim against a browser
        # Epic refuses, so the mismatch has to stop the run.
        self.assertIn('if OS_NAME != "lin" or fetcher.arch != "x86_64":', self.installer)
        self.assertIn("raise RuntimeError(", self.installer)


if __name__ == "__main__":
    unittest.main()
