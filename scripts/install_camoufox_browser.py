"""Install the official browser paired with the pinned Camoufox Python release."""

from camoufox.addons import DefaultAddons, maybe_download_addons
from camoufox.geolocation import ALLOW_GEOIP, download_mmdb
from camoufox.pkgman import CamoufoxFetcher, OS_NAME, installed_verstr


PINNED_VERSION = "156.0.1-beta.36"
PINNED_SHA256 = "72637885ee67c93466ad377831c647192f8f08fb5c6fd9279de1320b49dd2b53"


def main():
    # 0.5.8 ships browser-pin.json for this exact build. Its installer verifies
    # the archive digest before extraction and selects the versioned install.
    fetcher = CamoufoxFetcher()
    if OS_NAME != "lin" or fetcher.arch != "x86_64":
        raise RuntimeError("This pinned Camoufox build requires Linux x86_64")
    if fetcher.verstr != PINNED_VERSION or fetcher.installed_sha256 != PINNED_SHA256:
        raise RuntimeError("Camoufox release does not match the expected version and checksum")
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
