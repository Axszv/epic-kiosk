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

NAVIGATION_SELECTOR = "egs-navigation"
ORDER_HISTORY_URL = (
    "https://accounts.epicgames.com/account/v2/payment/ajaxGetOrderHistory"
)
ORDER_HISTORY_REFERER = "https://accounts.epicgames.com/account/transactions?lang=en-US"


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


async def read_store_login_state(page) -> str:
    """The `isloggedin` attribute of the Store navigation, when it exists."""
    with suppress(Exception):
        state = await page.locator(NAVIGATION_SELECTOR).get_attribute(
            "isloggedin", timeout=15000
        )
        if state is not None:
            return str(state).strip().lower()
    return ""


async def account_session_alive(page) -> bool:
    """Whether Epic still answers the account API as a signed-in visitor.

    The Store navigation is a rendered element, and its `isloggedin`
    attribute is what the client wrote into it. The account API answers from
    the session itself, so it is the authority when the two disagree.
    """
    with suppress(Exception):
        response = await page.context.request.get(
            ORDER_HISTORY_URL,
            headers={
                "accept": "application/json, text/plain, */*",
                "referer": ORDER_HISTORY_REFERER,
                "x-requested-with": "XMLHttpRequest",
            },
            timeout=30000,
        )
        alive = 200 <= int(response.status) < 300 and response.url.startswith(
            ORDER_HISTORY_URL
        )
        print(f"SESSION_PROBE:{response.status}:alive={alive}", flush=True)
        return alive
    print("SESSION_PROBE:request_failed", flush=True)
    return False


async def store_login_state(page, reloads: int = 2, wait_ms: int = 2500) -> str:
    """Report the Store login state without trusting a single DOM read.

    The Store fills `isloggedin` from the auth state its client fetches after
    the page starts. On a page that follows a login, the first read still
    says "false": the run then throws a working session away and solves
    another captcha for it. The read is repeated with a reload, and the
    account API decides when the navigation never agrees.
    """
    state = await read_store_login_state(page)
    for _ in range(max(0, reloads)):
        if state == "true":
            return state
        await page.wait_for_timeout(wait_ms)
        state = await read_store_login_state(page)
        if state == "true":
            return state
        with suppress(Exception):
            await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(wait_ms)
        state = await read_store_login_state(page)
    if state == "true":
        return "true"
    if await account_session_alive(page):
        print("SESSION_PROBE:navigation_disagrees_with_the_session", flush=True)
        return "true"
    # An empty state means the navigation was never found, which is a page
    # that did not finish loading and not a signed-out profile.
    return "false" if state == "false" else ""


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
