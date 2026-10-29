from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from urllib.parse import unquote

from loguru import logger
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from services.epic_games_service import EpicGames, REGION_UNAVAILABLE_MARKERS
from services.epic_store_page import is_login_url, product_cta, store_signed_out
from settings import RUNTIME_DIR


FREE_MARKERS = ("free", "$0.00", "\u20ac0.00", "\u00a30.00", "-100%")
OWNED_MARKERS = ("in library", "owned")
CLAIM_MARKERS = ("get", "install", "claim")


def load_mobile_offers(raw: str) -> list[dict]:
    if not raw.strip():
        return []
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("MOBILE_OFFERS_JSON must contain a JSON list")

    offers = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        platform = str(item.get("platform") or "Mobile").strip()
        title = str(item.get("title") or "Unknown mobile offer").strip()
        url = str(item.get("url") or "").strip()
        if url:
            offers.append({**item, "platform": platform, "title": title, "url": url})
    return offers


async def save_debug_page(page, label: str) -> None:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with suppress(Exception):
        RUNTIME_DIR.joinpath(f"{safe}.html").write_text(
            await page.content(),
            encoding="utf-8",
        )
    with suppress(Exception):
        await page.screenshot(
            path=str(RUNTIME_DIR.joinpath(f"{safe}.png")),
            full_page=True,
        )
    with suppress(Exception):
        RUNTIME_DIR.joinpath(f"{safe}.json").write_text(
            json.dumps(
                {"url": page.url, "title": await page.title()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


async def body_text(page) -> str:
    with suppress(Exception):
        return await page.locator("body").inner_text(timeout=10000)
    return ""


async def button_texts(page) -> list[str]:
    texts = []
    with suppress(Exception):
        for text in await page.locator("button").all_text_contents():
            cleaned = " ".join(text.split())
            if cleaned:
                texts.append(cleaned)
    return texts


async def wait_for_mobile_ownership(page, attempts: int = 3) -> dict | None:
    """Wait for an accepted checkout to appear in the product CTA."""
    for _ in range(attempts):
        with suppress(Exception):
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
        cta = await product_cta(page)
        if cta is None:
            continue
        text = " ".join(((await cta.text_content()) or "").split())
        if any(marker in text.lower() for marker in OWNED_MARKERS):
            return {"text": text, "disabled": await cta.is_disabled()}
    return None


def offer_identity(offer: dict) -> str:
    sandbox_id = str(offer.get("sandboxId") or "").strip()
    offer_id = str(offer.get("offerId") or "").strip()
    if sandbox_id and offer_id:
        return f"{sandbox_id}:{offer_id}"
    return str(offer.get("url") or "").strip().lower()


def debug_suffix(offer: dict) -> str:
    value = f"{offer.get('platform')}:{offer_identity(offer)}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def emit_result(result: dict) -> None:
    marker = (
        "MOBILE_RESULT:"
        f"{result['platform']}:{result['title']}:{result['status']}"
    )
    # The parent process parses stdout. Loguru's console filter intentionally
    # hides most INFO messages, so emit the machine-readable marker directly.
    print(marker, flush=True)
    logger.info(marker)
    notes = result.get("notes") or []
    if notes:
        # The notes carry why a checkout did not go through.
        print(f"MOBILE_NOTES:{result['platform']}:{result['status']}:{notes}", flush=True)


def order_history_has_offer_id(data: dict, offer_id: str) -> bool:
    """Match a mobile purchase by offer ID, not the shared sandbox ID."""
    if not offer_id:
        return False
    for order in data.get("orders", []):
        if order.get("orderType") != "PURCHASE":
            continue
        for item in order.get("items") or []:
            if str(item.get("offerId") or "") == offer_id:
                return True
    return False


def emit_order_check(phase: str, offer: dict, status: str) -> None:
    marker = (
        "MOBILE_ORDER_CHECK:"
        f"{phase}:{offer.get('platform')}:{offer.get('title')}:{status}"
    )
    print(marker, flush=True)
    logger.info(marker)


MOBILE_USER_AGENTS = {
    "iOS": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Android": (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
    ),
}

MOBILE_VIEWPORTS = {
    "iOS": {"width": 430, "height": 932},
    "Android": {"width": 412, "height": 915},
}


async def use_platform_browser(page, platform: str) -> bool:
    """Open a mobile offer as the device it is sold for.

    Epic refuses an iOS or Android offer on a desktop browser with "Device
    not supported — This product is not compatible with your current device",
    and the claim stops there: run 40883991382 lost `Home` on iOS that way,
    and every iOS offer of run 40904600195 ended `web_claim_not_available`.
    The page is opened with the platform's own user agent and viewport
    instead, so the Store sees a phone.

    `Accept-Encoding: identity` is kept: Camoufox fails to read the
    compressed hCaptcha scripts without it.
    """
    user_agent = MOBILE_USER_AGENTS.get(platform)
    if not user_agent:
        return False
    applied = False
    with suppress(Exception):
        await page.set_extra_http_headers(
            {"Accept-Encoding": "identity", "User-Agent": user_agent}
        )
        applied = True
    with suppress(Exception):
        await page.set_viewport_size(MOBILE_VIEWPORTS[platform])
        applied = True
    print(f"MOBILE_BROWSER:{platform}:ua_applied={applied}", flush=True)
    return applied


async def collect_mobile_offer(page, offer: dict) -> dict:
    platform = str(offer.get("platform") or "Mobile")
    title = str(offer.get("title") or "Unknown mobile offer")
    url = str(offer["url"])
    suffix = debug_suffix(offer)
    result = {
        "platform": platform,
        "title": title,
        "url": url,
        "status": "unknown",
        "attempted_claim": False,
        "checkout_submitted": False,
        "notes": [],
    }

    logger.info(f"MOBILE_OFFER_URL:{platform}:{title}:{url}")
    await use_platform_browser(page, platform)
    await page.goto(url, wait_until="load")
    await page.wait_for_timeout(3000)
    await save_debug_page(page, f"mobile_initial_{suffix}")

    initial_text = await body_text(page)
    normalized = initial_text.lower()
    result["initial_buttons"] = await button_texts(page)
    if any(marker in normalized for marker in REGION_UNAVAILABLE_MARKERS):
        result["status"] = "region_unavailable"
        emit_result(result)
        return result

    if await store_signed_out(page):
        result["status"] = "login_required"
        emit_result(result)
        return result

    cta = await product_cta(page)
    if cta is None:
        result["status"] = "no_purchase_cta"
        emit_result(result)
        return result

    cta_text = " ".join(((await cta.text_content()) or "").split())
    cta_normalized = cta_text.lower()
    cta_disabled = await cta.is_disabled()
    result["initial_cta"] = {"text": cta_text, "disabled": cta_disabled}
    logger.info(f"MOBILE_CTA:{platform}:{title}:{cta_text!r}:disabled={cta_disabled}")

    if any(marker in cta_normalized for marker in OWNED_MARKERS):
        result["status"] = "already_owned"
        emit_result(result)
        return result
    if cta_disabled:
        result["status"] = "cta_disabled"
        emit_result(result)
        return result
    if not any(marker in normalized for marker in FREE_MARKERS):
        result["status"] = "not_confirmed_free"
        emit_result(result)
        return result
    if not any(marker in cta_normalized for marker in CLAIM_MARKERS):
        result["status"] = "unsupported_cta"
        emit_result(result)
        return result

    result["attempted_claim"] = True
    await cta.click()
    with suppress(PlaywrightTimeoutError):
        await page.wait_for_url(is_login_url, timeout=15000)
    await page.wait_for_timeout(3000)
    await save_debug_page(page, f"mobile_after_cta_{suffix}")

    if is_login_url(page.url):
        result["status"] = "login_required"
        result["purchase_intent_created"] = "purchaseIntentId=" in unquote(page.url)
        emit_result(result)
        return result

    epic = EpicGames(page)
    with suppress(Exception):
        if await epic._handle_device_not_supported_modal(page):
            result["notes"].append("continued_past_device_modal")

    try:
        result["checkout_submitted"] = await epic._handle_instant_checkout(page)
    except Exception as err:
        result["notes"].append(f"checkout_error:{type(err).__name__}")

    # A completed checkout can take several seconds to reach the product page.
    # Refresh for evidence without submitting another order.
    owned_cta = await wait_for_mobile_ownership(page)
    if owned_cta is not None:
        result["final_cta"] = owned_cta
        result["status"] = "verified_owned"
        emit_result(result)
        return result

    final_text = await body_text(page)
    result["final_buttons"] = await button_texts(page)
    result["final_url"] = page.url
    await save_debug_page(page, f"mobile_final_{suffix}")

    final_cta = await product_cta(page)
    if final_cta is not None:
        result["final_cta"] = {
            "text": " ".join(((await final_cta.text_content()) or "").split()),
            "disabled": await final_cta.is_disabled(),
        }

    offer_id = str(offer.get("offerId") or "").strip()
    if offer_id:
        try:
            order_history = await EpicGames.fetch_order_history(page)
            if order_history_has_offer_id(order_history, offer_id):
                emit_order_check("post", offer, "owned")
                result["status"] = "verified_owned"
                result["notes"].append("verified_by_order_history")
                emit_result(result)
                return result
            emit_order_check("post", offer, "missing")
        except Exception as err:
            emit_order_check("post", offer, "unavailable")
            result["notes"].append(
                f"order_history_error:{type(err).__name__}"
            )

    if is_login_url(page.url):
        result["status"] = "login_required"
    elif any(marker in final_text.lower() for marker in REGION_UNAVAILABLE_MARKERS):
        result["status"] = "region_unavailable"
    elif "download the epic games app" in final_text.lower():
        result["status"] = "mobile_app_required"
    elif result["checkout_submitted"]:
        result["status"] = "checkout_submitted_unverified"
    else:
        result["status"] = "web_claim_not_available"
        # Nothing proves what the page offered instead, and the page dump is
        # 100 MB away in the artifact. The controls and the first line of the
        # page say it in the log.
        print(
            f"MOBILE_UNCLAIMED:{platform}:{title}:"
            f"url={page.url}:"
            f"buttons={result.get('final_buttons')}:"
            f"cta={result.get('final_cta')}:"
            f"text={' '.join(final_text.split())[:200]!r}",
            flush=True,
        )

    emit_result(result)
    return result


async def collect_mobile_offers(
    page,
    offers: list[dict],
    session_cookies: list[dict] | None = None,
) -> list[dict]:
    results = []
    completed_by_identity: dict[str, dict] = {}
    initial_order_history = None
    try:
        initial_order_history = await EpicGames.fetch_order_history(page)
    except Exception as err:
        logger.warning(
            f"Mobile order-history precheck failed: {type(err).__name__}: {err}"
        )

    for offer in offers:
        # The account login page that a mobile offer opens without a session
        # replaces the session cookies of the profile. The desktop session is
        # put back before every offer, so a mobile offer never inherits the
        # login page of the one before it.
        if session_cookies:
            with suppress(Exception):
                await page.context.add_cookies(session_cookies)
        identity = offer_identity(offer)
        if identity and identity in completed_by_identity:
            original = completed_by_identity[identity]
            result = {
                **original,
                "platform": offer["platform"],
                "title": offer["title"],
                "duplicate_of": original["platform"],
            }
            emit_result(result)
            results.append(result)
            continue

        offer_id = str(offer.get("offerId") or "").strip()
        if initial_order_history is not None and offer_id:
            if order_history_has_offer_id(initial_order_history, offer_id):
                emit_order_check("pre", offer, "owned")
                result = {
                    "platform": str(offer.get("platform") or "Mobile"),
                    "title": str(offer.get("title") or "Unknown mobile offer"),
                    "url": str(offer.get("url") or ""),
                    "status": "already_owned",
                    "attempted_claim": False,
                    "checkout_submitted": False,
                    "notes": ["verified_by_order_history"],
                }
                emit_result(result)
                if identity:
                    completed_by_identity[identity] = result
                results.append(result)
                continue
            emit_order_check("pre", offer, "missing")

        try:
            result = await collect_mobile_offer(page, offer)
        except Exception as err:
            logger.exception(
                f"MOBILE_ERROR:{offer.get('platform')}:{offer.get('title')}:"
                f"{type(err).__name__}"
            )
            result = {
                "platform": str(offer.get("platform") or "Mobile"),
                "title": str(offer.get("title") or "Unknown mobile offer"),
                "url": str(offer.get("url") or ""),
                "status": "error",
                "error": f"{type(err).__name__}: {err}",
            }
            emit_result(result)

        if identity:
            completed_by_identity[identity] = result
        results.append(result)
    # The offers are the last thing a run does, but the page outlives them in
    # a process that keeps going: the phone user agent is put back so nothing
    # after this reads the Store as an iPhone.
    with suppress(Exception):
        await page.set_extra_http_headers({"Accept-Encoding": "identity"})
    return results
