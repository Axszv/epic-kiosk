import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SOURCE = Path(__file__).resolve().parents[1] / "scripts/install_camoufox_browser.py"


class CamoufoxInstallTests(unittest.TestCase):
    def setUp(self):
        self.fetcher = types.SimpleNamespace(arch="x86_64", install=Mock())
        self.version = Mock(side_effect=[FileNotFoundError(), "156.0.1-beta.36"])
        spec = importlib.util.spec_from_file_location("camoufox_install_test", SOURCE)
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
            "camoufox.addons": types.SimpleNamespace(DefaultAddons=[], maybe_download_addons=Mock()),
            "camoufox.geolocation": types.SimpleNamespace(ALLOW_GEOIP=False, download_mmdb=Mock()),
            "camoufox.pkgman": types.SimpleNamespace(
                CamoufoxFetcher=lambda: self.fetcher, OS_NAME="lin", installed_verstr=self.version,
            ),
        }):
            spec.loader.exec_module(self.module)
        self.fetcher.verstr = self.module.PINNED_VERSION
        self.fetcher.installed_sha256 = self.module.PINNED_SHA256

    def test_installs_the_verified_release_when_browser_is_missing(self):
        self.module.main()
        self.fetcher.install.assert_called_once()

    def test_reuses_the_paired_version_without_downloading_again(self):
        self.version.side_effect = None
        self.version.return_value = self.module.PINNED_VERSION
        self.module.main()
        self.fetcher.install.assert_not_called()

    def test_release_metadata_drift_is_rejected_before_installation(self):
        for field, invalid in (("verstr", "999-beta.99"), ("installed_sha256", "wrong")):
            with self.subTest(field=field), patch.object(self.fetcher, field, invalid):
                with self.assertRaisesRegex(RuntimeError, "expected version and checksum"):
                    self.module.main()
        self.fetcher.install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
