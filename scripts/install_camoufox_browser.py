"""Install the Camoufox binary used by the verified August claim runs."""

import hashlib

from camoufox.addons import DefaultAddons, maybe_download_addons
from camoufox.locale import ALLOW_GEOIP, download_mmdb
from camoufox.pkgman import CamoufoxFetcher, OS_NAME, Version, installed_verstr


PINNED_VERSION = "152.0.4-beta.29"
PINNED_SHA256 = "1bea4b55a51c88e82dc7d426d9c75093d942d2afc8c911cb8fc78ebf723d686c"
PINNED_URL = (
    "https://github.com/daijro/camoufox/releases/download/"
    "v152.0.4-beta.29/camoufox-152.0.4-beta.29-lin.x86_64.zip"
)


class PinnedCamoufoxFetcher(CamoufoxFetcher):
    def get_asset(self):
        if OS_NAME != "lin" or self.arch != "x86_64":
            raise RuntimeError("This pinned Camoufox build requires Linux x86_64")
        return Version(release="beta.29", version="152.0.4"), PINNED_URL

    @staticmethod
    def download_file(file, url):
        CamoufoxFetcher.download_file(file, url)
        digest = hashlib.file_digest(file, "sha256").hexdigest()
        if digest != PINNED_SHA256:
            raise RuntimeError(f"Camoufox archive checksum mismatch: {digest}")
        file.seek(0)
        return file


def main():
    fetcher = PinnedCamoufoxFetcher()
    try:
        current_version = installed_verstr()
    except FileNotFoundError:
        current_version = None
    if current_version != PINNED_VERSION:
        fetcher.install()
    if ALLOW_GEOIP:
        download_mmdb()
    maybe_download_addons(list(DefaultAddons))
    print(f"Pinned Camoufox binary: {installed_verstr()}", flush=True)


if __name__ == "__main__":
    main()
