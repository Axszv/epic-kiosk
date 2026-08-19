from __future__ import annotations

import hashlib
import json
import re
from contextlib import suppress
from urllib.parse import unquote, urlparse

from loguru import logger
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from services.epic_games_service import EpicGames, REGION_UNAVAILABLE_MARKERS
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


async def product_cta(page):
    cta = page.locator("//button[@data-testid='purchase-cta-button']").first
    with suppress(Exception):
        if await cta.is_visible(timeout=5000):
            return cta

    fallback = page.locator(
        "button",
        has_text=re.compile(r"^(Get|Install|Claim|In Library|Owned)$", re.I),
    ).first
    with suppress(Exception):
        if await fallback.is_visible(timeout=5000):
            return fallback
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
        await page.wait_for_url(re.compile(r".*epicgames\.com/id/login.*"), timeout=15000)
    await page.wait_for_timeout(3000)
    await save_debug_page(page, f"mobile_after_cta_{suffix}")

    parsed_after_click = urlparse(page.url)
    if (
        "epicgames.com" in parsed_after_click.netloc
        and parsed_after_click.path.startswith("/id/login")
    ):
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

    # Never submit this offer again in the same account attempt. Refresh once
    # and require the product CTA to prove ownership; account-level retries
    # handle an unverified submission with a fresh browser process.
    with suppress(Exception):
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

    final_text = await body_text(page)
    result["final_buttons"] = await button_texts(page)
    result["final_url"] = page.url
    await save_debug_page(page, f"mobile_final_{suffix}")

    final_cta = await product_cta(page)
    if final_cta is not None:
        final_cta_text = " ".join(((await final_cta.text_content()) or "").split())
        result["final_cta"] = {
            "text": final_cta_text,
            "disabled": await final_cta.is_disabled(),
        }
        if any(marker in final_cta_text.lower() for marker in OWNED_MARKERS):
            result["status"] = "verified_owned"
            emit_result(result)
            return result

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

    parsed_final = urlparse(page.url)
    if "epicgames.com" in parsed_final.netloc and parsed_final.path.startswith("/id/login"):
        result["status"] = "login_required"
    elif any(marker in final_text.lower() for marker in REGION_UNAVAILABLE_MARKERS):
        result["status"] = "region_unavailable"
    elif "download the epic games app" in final_text.lower():
        result["status"] = "mobile_app_required"
    elif result["checkout_submitted"]:
        result["status"] = "checkout_submitted_unverified"
    else:
        result["status"] = "web_claim_not_available"

    emit_result(result)
    return result


async def collect_mobile_offers(page, offers: list[dict]) -> list[dict]:
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
    return results
