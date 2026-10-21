"""Install the Camoufox browser that the library pairs with its release.

Epic only accepts a login from a browser it recognises. The build that
accepted the login on 2026-10-20 was the one the Camoufox library ships
with, v158.0-beta.40. The project had pinned an older build from August,
and every login since then was refused with `captcha_invalid`: the page
never called Epic's Talon service, so the captcha it submitted was never
validated.
"""

from camoufox.addons import DefaultAddons, maybe_download_addons
from camoufox.locale import ALLOW_GEOIP, download_mmdb
from camoufox.pkgman import CamoufoxFetcher, OS_NAME, installed_verstr


# The build that Epic accepted a login with. The library refuses a browser
# it cannot drive, so this is the newest build it supports.
REQUIRED_VERSION = "158.0-beta.40"


def main():
    fetcher = CamoufoxFetcher()
    if OS_NAME != "lin" or fetcher.arch != "x86_64":
        raise RuntimeError("This Camoufox build requires Linux x86_64")
    if fetcher.verstr != REQUIRED_VERSION:
        raise RuntimeError(
            f"Camoufox {REQUIRED_VERSION} is required, but the library offers "
            f"{fetcher.verstr}"
        )
    try:
        current_version = installed_verstr()
    except FileNotFoundError:
        current_version = None
    if current_version != REQUIRED_VERSION:
        fetcher.install()
    if ALLOW_GEOIP:
        download_mmdb()
    maybe_download_addons(list(DefaultAddons))
    print(f"Camoufox binary: {installed_verstr()}", flush=True)


if __name__ == "__main__":
    main()
