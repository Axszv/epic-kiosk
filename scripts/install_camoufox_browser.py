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


def main():
    fetcher = CamoufoxFetcher()
    if OS_NAME != "lin" or fetcher.arch != "x86_64":
        raise RuntimeError("This Camoufox build requires Linux x86_64")

    # Install the build this library release pairs with. A pinned string is
    # only correct until the library moves, and then it stops every run at
    # startup: 2026-10-29 died on `Camoufox 158.0-beta.40 is required, but
    # the library offers 158.0.1-beta.41` before a browser had started. The
    # library cannot drive a build it does not pair with, so its own version
    # is the only one worth installing.
    required_version = fetcher.verstr
    try:
        current_version = installed_verstr()
    except FileNotFoundError:
        current_version = None
    if current_version != required_version:
        fetcher.install()
    if ALLOW_GEOIP:
        download_mmdb()
    maybe_download_addons(list(DefaultAddons))
    print(
        f"Camoufox binary: {installed_verstr()} "
        f"(the library pairs {required_version})",
        flush=True,
    )


if __name__ == "__main__":
    main()
