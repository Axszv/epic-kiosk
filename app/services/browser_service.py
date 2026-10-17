"""Browser selection with separate profiles for incompatible engines."""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

from camoufox import AsyncCamoufox
from camoufox.addons import DefaultAddons
from camoufox.fingerprints import Screen
from loguru import logger
from playwright.async_api import async_playwright


def browser_proxy_config():
    """Keep an explicit browser-only proxy separate from model API traffic."""
    value = (os.getenv("EPIC_BROWSER_PROXY") or os.getenv("HTTP_PROXY")
             or os.getenv("HTTPS_PROXY"))
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        defaults = {"http": 80, "https": 443, "socks5": 1080}
        if parsed.scheme not in defaults or not parsed.hostname:
            raise ValueError
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        result = {"server": f"{parsed.scheme}://{host}:{parsed.port or defaults[parsed.scheme]}"}
        if parsed.username:
            result["username"] = unquote(parsed.username)
        if parsed.password:
            result["password"] = unquote(parsed.password)
        return result
    except ValueError:
        raise ValueError("Invalid browser proxy URL; expected http, https or socks5 with a host") from None


@asynccontextmanager
async def claim_browser(user_data_dir, *, headless=True, proxy=None):
    engine = os.getenv("EPIC_BROWSER_ENGINE", "camoufox").strip().lower()
    headless = os.getenv("EPIC_BROWSER_HEADLESS", str(headless)).lower() == "true"
    logger.warning(f"BROWSER_ENGINE:{engine}:headless={headless}")
    if engine == "chromium":
        # Chromium cannot read a Firefox profile. Keep both caches intact.
        profile = Path(user_data_dir)
        profile = profile.with_name(profile.name + "-chromium")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=headless,
                proxy=proxy,
                viewport={"width": 1920, "height": 1080},
            )
            try:
                yield browser
            finally:
                await browser.close()
        return
    if engine != "camoufox":
        raise ValueError(f"Unsupported Epic browser engine: {engine}")
    async with AsyncCamoufox(
        persistent_context=True,
        user_data_dir=user_data_dir,
        screen=Screen(max_width=1920, max_height=1080, min_height=1080, min_width=1920),
        # Match the upstream Epic compatibility patch: account/checkout
        # scripts must not be filtered by the browser's bundled ad blocker.
        exclude_addons=[DefaultAddons.UBO],
        humanize=0.2,
        headless=headless,
        proxy=proxy,
        # Resolve through the same proxy so timezone, locale and WebRTC use
        # its network identity instead of the hosted runner's location/IP.
        geoip=bool(proxy),
    ) as browser:
        yield browser
