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
from camoufox.pkgman import CamoufoxFetcher, OS_NAME, Version, installed_verstr


# The build that Epic answers with a captcha. The newest release of the
# library is not it: 158.0.1-beta.41 opens the checkout without ever starting
# the hCaptcha widget — run 41053940945 logged 0 challenge tasks across eight
# accounts, where 158.0-beta.40 logged 14 in a single run. `install()` reads
# `url` and `version`, so the release is pinned to the one that works rather
# than the one that is newest.
PINNED_RELEASE = "158.0-beta.40"
PINNED_VERSION = "158.0"
PINNED_URL = (
    "https://github.com/daijro/camoufox/releases/download/"
    f"v{PINNED_RELEASE}/camoufox-{PINNED_RELEASE}-lin.x86_64.zip"
)


def main():
    fetcher = CamoufoxFetcher()
    if OS_NAME != "lin" or fetcher.arch != "x86_64":
        raise RuntimeError("This Camoufox build requires Linux x86_64")

    try:
        current_version = installed_verstr()
    except FileNotFoundError:
        current_version = None
    if current_version != PINNED_RELEASE:
        fetcher._version_obj = Version(
            release=PINNED_RELEASE[len(PINNED_VERSION) + 1 :],
            version=PINNED_VERSION,
        )
        fetcher._url = PINNED_URL
        fetcher.install()
    if ALLOW_GEOIP:
        download_mmdb()
    maybe_download_addons(list(DefaultAddons))
    print(
        f"Camoufox binary: {installed_verstr()} "
        f"(pinned; the library offers {fetcher.verstr})",
        flush=True,
    )


if __name__ == "__main__":
    main()
