from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timezone

import requests


STORE_ORIGIN = "https://store.epicgames.com"
DISCOVER_HOME_URL = (
    "https://egs-platform-service.store.epicgames.com/api/v2/public/discover/home"
)
DEFAULT_PLATFORMS = ("android", "ios")


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    with suppress(ValueError):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


def platform_display_name(platform: str) -> str:
    return "iOS" if platform.strip().lower() == "ios" else platform.strip().title()


def content_categories(content: dict) -> set[str]:
    return {
        str(category)
        for category in content.get("categories", [])
        if isinstance(category, str) and category
    }


def claim_purchases(content: dict) -> list[dict]:
    return [
        purchase
        for purchase in content.get("purchase") or []
        if purchase.get("purchaseType") == "Claim"
    ]


def claim_discount_end_dates(content: dict) -> list[str]:
    dates = []
    for purchase in claim_purchases(content):
        end_date = (purchase.get("discount") or {}).get("discountEndDate")
        if end_date:
            dates.append(end_date)
    return sorted(set(dates))


def is_weekly_free_offer(content: dict, now: datetime) -> bool:
    if "freegames" not in content_categories(content):
        return False
    if not claim_purchases(content):
        return False

    end_dates = [parse_date(value) for value in claim_discount_end_dates(content)]
    if any(end_date and end_date > now for end_date in end_dates):
        return True

    # Some responses omit the Claim end date but include the paid offer that
    # resumes after the giveaway. The lab workflow verified this fallback.
    return any(
        purchase.get("purchaseType") == "Purchase"
        and ((purchase.get("price") or {}).get("decimalPrice") or 0) > 0
        for purchase in content.get("purchase") or []
    )


def purchase_payload(content: dict) -> dict:
    for purchase in claim_purchases(content):
        payload = purchase.get("purchasePayload") or {}
        if payload.get("offerId") and payload.get("sandboxId"):
            return payload
    return {}


def normalize_offer(
    module: dict,
    raw_offer: dict,
    platform: str,
    country: str,
    locale: str,
) -> dict:
    content = raw_offer.get("content") or {}
    payload = purchase_payload(content)
    slug = (content.get("mapping") or {}).get("slug")
    end_dates = claim_discount_end_dates(content)
    return {
        "platform": platform_display_name(platform),
        "title": content.get("title"),
        "url": f"{STORE_ORIGIN}/p/{slug}" if slug else None,
        "offerId": payload.get("offerId") or raw_offer.get("offerId"),
        "sandboxId": payload.get("sandboxId") or raw_offer.get("sandboxId"),
        "availableUntil": end_dates[0] if end_dates else None,
        "country": country,
        "locale": locale,
        "source": "epic_platform_service",
        "moduleType": module.get("type"),
        "topicId": module.get("topicId"),
    }


def discover_platform(
    platform: str,
    country: str = "US",
    locale: str = "en-US",
    *,
    session=requests,
    now: datetime | None = None,
) -> dict:
    normalized_platform = platform.strip().lower()
    response = session.get(
        DISCOVER_HOME_URL,
        params={
            "count": 10,
            "country": country,
            "locale": locale,
            "platform": normalized_platform,
            "start": 0,
            "store": "EGS",
        },
        headers={
            "accept": "application/json",
            "user-agent": "EpicGamesApp/1.5.1 Android",
        },
        timeout=45,
    )
    if not response.ok:
        raise RuntimeError(
            f"mobile discovery failed for {normalized_platform} with HTTP "
            f"{response.status_code}: {response.text[:300]}"
        )

    data = response.json()
    modules = data.get("data") or []
    free_modules = [
        module
        for module in modules
        if module.get("type") == "freeGame"
        or module.get("topicId") == f"mobile-{normalized_platform}-free-game"
    ]
    if not free_modules:
        free_modules = [
            module
            for module in modules
            if str(module.get("title", "")).strip().lower() == "free games"
        ]

    current_time = now or datetime.now(timezone.utc)
    offers = []
    for module in free_modules:
        for raw_offer in module.get("offers") or []:
            content = raw_offer.get("content") or {}
            if not is_weekly_free_offer(content, current_time):
                continue
            offer = normalize_offer(
                module,
                raw_offer,
                normalized_platform,
                country,
                locale,
            )
            if offer["title"] and offer["url"]:
                offers.append(offer)

    return {
        "platform": platform_display_name(normalized_platform),
        "moduleCount": len(modules),
        "freeModuleCount": len(free_modules),
        "offers": offers,
    }


def discover_mobile_offers(
    platforms: tuple[str, ...] = DEFAULT_PLATFORMS,
    country: str = "US",
    locale: str = "en-US",
    *,
    session=requests,
    now: datetime | None = None,
) -> dict:
    platform_results = [
        discover_platform(
            platform,
            country,
            locale,
            session=session,
            now=now,
        )
        for platform in platforms
    ]
    offers = [offer for result in platform_results for offer in result["offers"]]
    return {
        "country": country,
        "locale": locale,
        "platforms": platform_results,
        "offers": offers,
    }
