"""Shared controls for legacy and current Epic Store product pages."""

import re
from contextlib import suppress
from urllib.parse import urlsplit


PRODUCT_CTA_TEXT = re.compile(
    r"^(Get|Install|Claim|In Library|Owned|Buy Now|Purchase|Pre-Purchase"
    r"|Add to Cart|View in Cart|In Cart|Go to Cart|Cart)$",
    re.I,
)
SIGN_IN_TEXT = re.compile(r"^(Sign in|Log in)$", re.I)
STORE_LOGIN_URL = "https://store.epicgames.com/login?state=%2F&lang=en-US"

NAVIGATION_SELECTOR = "egs-navigation"
ORDER_HISTORY_URL = (
    "https://accounts.epicgames.com/account/v2/payment/ajaxGetOrderHistory"
)
ORDER_HISTORY_REFERER = "https://accounts.epicgames.com/account/transactions?lang=en-US"

SWITCH_ACCOUNT_MARKERS = ("/id/login/switch-account", "/id/login/switch")
SWITCH_CONFIRM_SELECTORS = (
    # The "Welcome back" card lists the account as a list item that behaves
    # like a button: `<div role="button" id="account-…" data-account-id="…">`
    # around the avatar and the masked address. It is the control to press;
    # `main button` used to match `Use another account` instead, which sends
    # the run to a login form for an account it already has a session for.
    "[data-account-id]",
    "[role=button][id^='account-']",
    "[data-testid='switch-account-continue']",
    "button[data-testid*='continue']",
    "button:has-text('Continue')",
    "button:has-text('继续')",
    "#continue",
    "#link-success",
    "a:has-text('Continue')",
)


def is_switch_account_url(url: str) -> bool:
    return any(marker in (url or "") for marker in SWITCH_ACCOUNT_MARKERS)


async def _dump_page(page, label: str) -> None:
    """Save the page of a flow that has no selector yet."""
    with suppress(Exception):
        from settings import RUNTIME_DIR

        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        RUNTIME_DIR.joinpath(f"{label}.html").write_text(
            await page.content(), encoding="utf-8"
        )
        await page.screenshot(path=str(RUNTIME_DIR.joinpath(f"{label}.png")))
        print(f"PAGE_DUMP:{label}:{page.url}", flush=True)


async def complete_account_switch(page) -> bool:
    """Confirm the signed-in account on Epic's switch-account page.

    A Store session does not give the account portal a session of its own.
    `accounts.epicgames.com` answers 401 and sends the browser through
    `/id/login/switch-account`, which asks which account to continue with.
    The order history and the checkout run on that portal session, so until
    this page is confirmed every claim reports `cookie_invalid` while the
    Store itself still says the visitor is signed in.
    """
    if not is_switch_account_url(page.url):
        return False
    await _dump_page(page, "account_switch")
    for selector in SWITCH_CONFIRM_SELECTORS:
        with suppress(Exception):
            candidate = page.locator(selector).first
            if await candidate.count() == 0:
                continue
            if not await candidate.is_visible(timeout=1500):
                continue
            await candidate.click(timeout=5000)
            await page.wait_for_timeout(2500)
            print(
                f"ACCOUNT_SWITCH:clicked:{selector}:{page.url}",
                flush=True,
            )
            if not is_switch_account_url(page.url):
                return True
    print("ACCOUNT_SWITCH:no_confirm_control", flush=True)
    return False


class StoreLoginRequired(Exception):
    """The product session needs authentication, not another checkout click."""


def is_login_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return (host == "epicgames.com" or host.endswith(".epicgames.com")) and (
        parsed.path.rstrip("/") == "/login" or parsed.path.startswith("/id/login")
    )


async def store_signed_out(page) -> bool:
    """Whether the visitor is signed out, confirmed by the session itself.

    The header paints `Sign In` before the client has fetched the auth state,
    so a read taken right after navigation says "signed out" while the
    session is perfectly alive: run 40875112742 lost `Sker Ritual` that way,
    two seconds after the product page opened and long after the login had
    succeeded. The account API answers from the session itself, so it decides
    before the run throws a working session away.
    """
    if is_login_url(page.url):
        return True
    visible = False
    with suppress(Exception):
        visible = await page.get_by_role("link", name=SIGN_IN_TEXT).first.is_visible()
    if not visible:
        return False
    if await account_session_alive(page):
        print("SESSION_PROBE:sign_in_link_is_a_render_race", flush=True)
        return False
    return True


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
