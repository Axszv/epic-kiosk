"""Epic only accepts a login from a browser it recognises."""

import unittest
from pathlib import Path


INSTALLER = (
    Path(__file__).resolve().parents[1] / "scripts/install_camoufox_browser.py"
)


class CamoufoxBrowserVersionTests(unittest.TestCase):
    def setUp(self):
        self.installer = INSTALLER.read_text(encoding="utf-8")

    def test_the_browser_build_is_the_one_the_library_pairs_with(self):
        # The build that logged in on 2026-10-20 was v158.0-beta.40. Pinning
        # that string was right until the library moved to 158.0.1-beta.41,
        # and then every run stopped at startup with
        # `Camoufox 158.0-beta.40 is required, but the library offers …`.
        # The library cannot drive a build it does not pair with, so its own
        # version is what gets installed.
        self.assertIn("required_version = fetcher.verstr", self.installer)
        self.assertNotIn("REQUIRED_VERSION =", self.installer)

    def test_the_old_build_is_not_pinned_any_more(self):
        self.assertNotIn("152.0.4-beta.29", self.installer)
        self.assertNotIn("PINNED_URL", self.installer)

    def test_the_wrong_platform_is_refused_outright(self):
        # A silent fallback would run the weekly claim against a browser
        # Epic refuses, so the mismatch has to stop the run.
        self.assertIn('if OS_NAME != "lin" or fetcher.arch != "x86_64":', self.installer)
        self.assertIn("raise RuntimeError(", self.installer)


if __name__ == "__main__":
    unittest.main()
