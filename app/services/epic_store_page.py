"""Shared controls for legacy and current Epic Store product pages."""

import re
from contextlib import suppress
from urllib.parse import urlsplit


PRODUCT_CTA_TEXT = re.compile(
    r"^(Get|Install|Claim|In Library|Owned|Buy Now|Purchase|Add to Cart|View in Cart)$",
    re.I,
)
SIGN_IN_TEXT = re.compile(r"^(Sign in|Log in)$", re.I)
STORE_LOGIN_URL = "https://store.epicgames.com/login?state=%2F&lang=en-US"


class StoreLoginRequired(Exception):
    """The product session needs authentication, not another checkout click."""


def is_login_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return (host == "epicgames.com" or host.endswith(".epicgames.com")) and (
        parsed.path.rstrip("/") == "/login" or parsed.path.startswith("/id/login")
    )


async def store_signed_out(page) -> bool:
    if is_login_url(page.url):
        return True
    with suppress(Exception):
        return await page.get_by_role("link", name=SIGN_IN_TEXT).first.is_visible()
    return False


async def product_cta(page, timeout: int = 10000):
    # CSS/role locators pierce open shadow roots; XPath does not. Restrict the
    # text fallback to main content so global navigation cannot be selected.
    legacy = page.locator("button[data-testid='purchase-cta-button']:visible")
    current = page.locator("main").get_by_role("button", name=PRODUCT_CTA_TEXT)
    candidate = legacy.or_(current).filter(visible=True).first
    with suppress(Exception):
        await candidate.wait_for(state="visible", timeout=timeout)
        return candidate
    return None
