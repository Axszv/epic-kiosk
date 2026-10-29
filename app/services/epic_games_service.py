# -*- coding: utf-8 -*-
# Time       : 2022/1/16 0:25
# Author     : QIN2DIM
# GitHub     : https://github.com/QIN2DIM
# Description: 游戏商城控制句柄

import asyncio
import hashlib
import json
import re
from contextlib import suppress
from enum import Enum
from json import JSONDecodeError
from typing import Any, List
from urllib.parse import parse_qsl, urlsplit
from weakref import WeakSet

import httpx
from loguru import logger
from playwright.async_api import Page
from playwright.async_api import expect, TimeoutError, FrameLocator
from tenacity import retry, retry_if_exception_type, stop_after_attempt

from models import OrderItem, Order
from models import PromotionGame
from settings import settings, RUNTIME_DIR
from services.epic_store_page import (
    StoreLoginRequired, complete_account_switch, is_login_url,
    is_switch_account_url, product_cta, store_login_state, store_signed_out,
)
from services.hcaptcha_agent_service import (
    get_hcaptcha_agent,
    get_legacy_checkout_agent,
    replace_hcaptcha_agent,
)

URL_CLAIM = "https://store.epicgames.com/en-US/free-games"
URL_LOGIN = (
    f"https://www.epicgames.com/id/login?lang=en-US&noHostRedirect=true&redirectUrl={URL_CLAIM}"
)
URL_CART = "https://store.epicgames.com/en-US/cart"
URL_CART_SUCCESS = "https://store.epicgames.com/en-US/cart/success"


URL_PROMOTIONS = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
URL_PRODUCT_PAGE = "https://store.epicgames.com/en-US/p/"
URL_PRODUCT_BUNDLES = "https://store.epicgames.com/en-US/bundles/"
URL_ACCOUNT_ORIGIN = "https://accounts.epicgames.com/"
URL_ACCOUNT_TRANSACTIONS = f"{URL_ACCOUNT_ORIGIN}account/transactions?lang=en-US"
URL_ORDER_HISTORY = f"{URL_ACCOUNT_ORIGIN}account/v2/payment/ajaxGetOrderHistory"
URL_CONFIRM_ORDER = "**/purchase/confirm-order**"
_ORDER_HISTORY_UNAUTHORIZED_CONTEXTS = WeakSet()

class CartNotPurchasable(RuntimeError):
    """The cart holds an item the account cannot check out.

    A free game that the account's region can buy always enables the cart's
    checkout control. When the control stays disabled with the item in the
    cart, the item is not purchasable for this account — `Sker Ritual` in a
    CN account, whose cart page carries
    `egstore.cart.validation.card_level.error.region_restricted`.
    """


REGION_UNAVAILABLE_MARKERS = (
    "currently unavailable in your platform or region",
    "unavailable in your platform or region",
    "not available in your region",
    "unavailable in your region",
)
EPIC_CAPTCHA_CHALLENGE_FAILED = "epic.error.captcha.challenge.failed"
CHECKOUT_DIAGNOSTIC_URL_MARKERS = (
    "talon",
    "ecosec",
    "captcha",
    "challenge",
    "risk",
    "confirm-order",
)
CHECKOUT_DIAGNOSTIC_SECRET_MARKERS = (
    "captcha",
    "challenge",
    "proof",
    "response",
    "token",
)


def emit_desktop_result(title: str, status: str) -> None:
    marker = f"DESKTOP_RESULT:{title}:{status}"
    print(marker, flush=True)
    logger.info(marker)


def is_retryable_confirm_order_failure(status: int, payload: dict[str, Any]) -> bool:
    error_code = str(payload.get("errorCode") or payload.get("message") or "")
    return status == 400 and error_code == EPIC_CAPTCHA_CHALLENGE_FAILED


def is_checkout_diagnostic_url(url: str) -> bool:
    parsed = urlsplit(url)
    normalized_url = url.casefold()
    host = parsed.netloc.casefold()
    if "talon" in normalized_url or "ecosec" in normalized_url:
        return True
    if "/purchase/confirm-order" in normalized_url:
        return True
    return "epic" in host and any(
        marker in normalized_url for marker in CHECKOUT_DIAGNOSTIC_URL_MARKERS
    )


def diagnostic_fingerprint(value: Any) -> dict[str, int | str]:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return {
        "length": len(value),
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()[:12],
    }


def summarize_checkout_secrets(value: Any, path: str = "") -> dict[str, dict]:
    summary = {}
    if isinstance(value, dict):
        for key, item in value.items():
            item_path = f"{path}.{key}" if path else str(key)
            normalized_key = str(key).casefold()
            if any(
                marker in normalized_key
                for marker in CHECKOUT_DIAGNOSTIC_SECRET_MARKERS
            ):
                summary[item_path] = diagnostic_fingerprint(item)
            elif isinstance(item, (dict, list)):
                summary.update(summarize_checkout_secrets(item, item_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            summary.update(summarize_checkout_secrets(item, f"{path}[{index}]"))
    return summary


def parse_checkout_diagnostic_body(body: str | None) -> Any:
    if not body:
        return {}
    with suppress(Exception):
        return json.loads(body)
    with suppress(Exception):
        values = dict(parse_qsl(body, keep_blank_values=True))
        if values:
            return values
    return body


def checkout_body_summary(body: str | None) -> dict[str, dict]:
    parsed = parse_checkout_diagnostic_body(body)
    summary = summarize_checkout_secrets(parsed)
    if summary:
        return summary
    if isinstance(parsed, str) and parsed:
        return {"raw_body": diagnostic_fingerprint(parsed)}
    return {}


class GameCollectResult(Enum):
    """
    游戏收集结果枚举

    用于区分不同的执行结果，便于上层调用者判断是否成功
    """
    # 成功：所有游戏已在库中
    ALL_OWNED = "all_owned"

    # 成功：游戏领取成功
    SUCCESS = "success"

    # 失败：EULA 协议未接受
    EULA_FAILED = "eula_failed"

    # 失败：Cookie 无效
    COOKIE_INVALID = "cookie_invalid"

    # 失败：未知错误
    UNKNOWN_ERROR = "unknown_error"


def get_promotions() -> List[PromotionGame]:
    """获取周免游戏数据"""
    def is_discount_game(prot: dict) -> bool | None:
        with suppress(KeyError, IndexError, TypeError):
            offers = prot["promotions"]["promotionalOffers"][0]["promotionalOffers"]
            for i, offer in enumerate(offers):
                if offer["discountSetting"]["discountPercentage"] == 0:
                    return True

    promotions: List[PromotionGame] = []

    resp = httpx.get(URL_PROMOTIONS, params={"local": "zh-CN"})

    try:
        data = resp.json()
    except JSONDecodeError as err:
        logger.error(f"获取促销信息失败: {err}")
        return []

    with suppress(Exception):
        cache_key = RUNTIME_DIR.joinpath("promotions.json")
        cache_key.parent.mkdir(parents=True, exist_ok=True)
        cache_key.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    # Get store promotion data and <this week free> games
    for e in data["data"]["Catalog"]["searchStore"]["elements"]:
        if not is_discount_game(e):
            continue

        # -----------------------------------------------------------
        # 🟢 智能 URL 识别逻辑
        # -----------------------------------------------------------
        is_bundle = False
        if e.get("offerType") == "BUNDLE":
            is_bundle = True
        
        # 补充检测：分类和标题
        if not is_bundle:
            for cat in e.get("categories", []):
                if "bundle" in cat.get("path", "").lower():
                    is_bundle = True
                    break
        if not is_bundle and "Collection" in e.get("title", ""):
             is_bundle = True

        base_url = URL_PRODUCT_BUNDLES if is_bundle else URL_PRODUCT_PAGE

        try:
            if e.get('offerMappings'):
                slug = e['offerMappings'][0]['pageSlug']
                e["url"] = f"{base_url.rstrip('/')}/{slug}"
            elif e.get("productSlug"):
                e["url"] = f"{base_url.rstrip('/')}/{e['productSlug']}"
            else:
                 e["url"] = f"{base_url.rstrip('/')}/{e.get('urlSlug', 'unknown')}"
        except (KeyError, IndexError):
            logger.debug(f"Failed to get URL: {e}")
            continue

        logger.debug(f"发现周免游戏: {e['url']}")
        promotions.append(PromotionGame(**e))

    return promotions


class EpicAgent:
    def __init__(self, page: Page):
        self.page = page
        self.epic_games = EpicGames(self.page)
        self._promotions: List[PromotionGame] = []
        self._ctx_cookies_is_available: bool = False
        self._orders: List[OrderItem] = []
        self._namespaces: List[str] = []
        self._reported_owned_promotions: set[tuple[str, str]] = set()
        self._cookies = None

    async def _save_page_debug(self, label: str):
        """Save the current page state for GitHub Actions artifacts."""
        with suppress(Exception):
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)
            debug_info = {
                "url": self.page.url,
                "title": await self.page.title(),
            }
            RUNTIME_DIR.joinpath(f"{safe_label}.json").write_text(
                json.dumps(debug_info, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            await self.page.evaluate(
                """([email, password]) => {
                    for (const input of document.querySelectorAll('input, textarea')) {
                        if (input.value) input.value = '***';
                    }
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
                        if (email) node.nodeValue = node.nodeValue.split(email).join('***');
                        if (password) node.nodeValue = node.nodeValue.split(password).join('***');
                    }
                }""",
                [settings.EPIC_EMAIL, settings.EPIC_PASSWORD.get_secret_value()],
            )
            html = await self.page.content()
            html = html.replace(settings.EPIC_EMAIL, "***")
            html = html.replace(settings.EPIC_PASSWORD.get_secret_value(), "***")
            RUNTIME_DIR.joinpath(f"{safe_label}.html").write_text(
                html,
                encoding="utf-8",
            )
            await self.page.screenshot(
                path=str(RUNTIME_DIR.joinpath(f"{safe_label}.png")),
                full_page=True,
            )

    async def _handle_eula_correction(self) -> bool:
        """
        处理 EULA 修正页面

        Epic Games 在某些情况下会将用户重定向到 EULA 修正页面：
        - 新注册账号首次登录
        - Epic 更新服务条款
        - 账号长期未登录
        - 账号在新设备/地区登录

        页面特点：
        - SPA 单页应用（React + Material UI），内容动态渲染
        - 只有"拒绝"和"接受"两个按钮，无复选框
        - 接受按钮特征：id="accept", type="submit"

        Returns:
            bool: True 表示成功处理 EULA，False 表示无需处理或处理失败
        """
        current_url = self.page.url

        # 检测是否在 EULA 修正页面
        if "correction/eula" not in current_url:
            return False

        logger.warning("⚠️ 检测到 EULA 修正页面，尝试自动接受协议...")

        try:
            # SPA 页面需要等待网络完全空闲
            await self.page.wait_for_load_state("networkidle")

            # 额外等待 React 渲染完成
            await self.page.wait_for_timeout(2000)

            # ============================================================
            # EULA 接受按钮选择器（按优先级排序）
            # 按钮特征: <button id="accept" type="submit">接受</button>
            # ============================================================
            accept_selectors = [
                # 最精确：通过 ID 选择（最稳定）
                "#accept",
                "button#accept",
                "//button[@id='accept']",

                # 通过 type=submit（次优）
                "//button[@type='submit']",

                # 通过文本匹配（多语言）
                "//button[normalize-space(text())='Accept']",
                "//button[normalize-space(text())='接受']",
                "//button[normalize-space(text())='Akzeptieren']",
                "//button[normalize-space(text())='Accepter']",
            ]

            # 尝试点击接受按钮
            for selector in accept_selectors:
                try:
                    btn = self.page.locator(selector).first
                    # 增加等待时间，因为 SPA 需要渲染
                    if await btn.is_visible(timeout=5000):
                        btn_text = await btn.text_content()
                        logger.info(f"📋 点击 EULA 接受按钮: '{btn_text}' | 选择器: {selector}")
                        await btn.click()

                        # 等待页面跳转
                        await self.page.wait_for_load_state("networkidle", timeout=15000)

                        # 验证是否成功跳转
                        new_url = self.page.url
                        if "correction/eula" not in new_url:
                            logger.success("✅ EULA 协议已接受，页面已跳转")
                            return True
                        else:
                            logger.warning("⚠️ 点击后仍在 EULA 页面，尝试下一个选择器")
                except Exception as e:
                    logger.debug(f"EULA 选择器 '{selector}' 失败: {e}")
                    continue

            logger.error("❌ 未能找到 EULA 接受按钮")
            return False

        except Exception as e:
            logger.error(f"❌ 处理 EULA 页面异常: {e}")
            return False

    async def _sync_order_history(self):
        if self._orders:
            return
        completed_orders: List[OrderItem] = []
        try:
            data = await EpicGames.fetch_order_history(self.page)
            for _order in data["orders"]:
                order = Order(**_order)
                if order.orderType != "PURCHASE":
                    continue
                for item in order.items:
                    if not item.namespace or len(item.namespace) != 32:
                        continue
                    completed_orders.append(item)
        except Exception as err:
            logger.warning(err)
        self._orders = completed_orders

    async def _check_orders(self):
        await self._sync_order_history()
        self._namespaces = self._namespaces or [order.namespace for order in self._orders]
        owned_offer_ids = {order.offerId for order in self._orders}
        pending_promotions = []
        for promotion in get_promotions():
            if (
                promotion.namespace in self._namespaces
                or promotion.id in owned_offer_ids
            ):
                promotion_key = (promotion.namespace, promotion.id)
                if promotion_key not in self._reported_owned_promotions:
                    emit_desktop_result(promotion.title, "already_owned")
                    self._reported_owned_promotions.add(promotion_key)
                continue
            pending_promotions.append(promotion)
        self._promotions = pending_promotions

    async def _should_ignore_task(self) -> tuple[bool, GameCollectResult]:
        """
        检查是否应该忽略任务

        Returns:
            tuple[bool, GameCollectResult]:
                - (True, ALL_OWNED): 所有游戏已在库中，无需领取
                - (False, SUCCESS): 有游戏需要领取
                - (False, EULA_FAILED): EULA 处理失败
                - (False, COOKIE_INVALID): Cookie 无效
                - (False, UNKNOWN_ERROR): 未知错误
        """
        self._ctx_cookies_is_available = False
        await self.page.goto(URL_CLAIM, wait_until="domcontentloaded")

        # ============================================================
        # 🔥 关键修复：等待页面稳定，防止 JS 重定向导致检测遗漏
        # Epic Games 可能会通过 JS 异步重定向到 EULA 页面
        # domcontentloaded 触发时重定向可能还没完成
        # ============================================================
        await self.page.wait_for_timeout(2000)  # 等待 JS 执行完成

        # ============================================================
        # 🔥 EULA 修正页面检测与处理
        # Epic Games 可能会重定向到 EULA 页面，需要自动接受协议
        # ============================================================
        max_eula_attempts = 3
        for attempt in range(max_eula_attempts):
            current_url = self.page.url
            logger.debug(f"📍 当前页面 URL: {current_url}")
            if "correction/eula" in current_url or "corrective=" in current_url:
                logger.warning(f"⚠️ 检测到修正页面（尝试 {attempt + 1}/{max_eula_attempts}）")
                if await self._handle_eula_correction():
                    # EULA 处理成功后，重新导航到目标页面
                    await self.page.goto(URL_CLAIM, wait_until="domcontentloaded")
                    await self.page.wait_for_timeout(2000)  # 再次等待稳定
                else:
                    logger.error("❌ EULA 处理失败，跳过此账号")
                    return False, GameCollectResult.EULA_FAILED
            else:
                break

        # 尝试获取登录状态，增加超时处理
        try:
            status = await store_login_state(self.page)
        except Exception as e:
            # 如果超时，可能还在修正页面或有其他问题
            current_url = self.page.url
            await self._save_page_debug("collect_nav_timeout")
            if "correction" in current_url or "eula" in current_url:
                logger.error("❌ 仍在修正页面，无法继续")
                return False, GameCollectResult.EULA_FAILED
            logger.warning(f"⚠️ 未找到 egs-navigation，认证流程已通过，继续检查订单: {e}")
            status = "true"

        if status == "false":
            logger.error("❌ Cookie 无效，账号未登录")
            return False, GameCollectResult.COOKIE_INVALID
        self._ctx_cookies_is_available = True
        await self._check_orders()
        if not self._promotions:
            return True, GameCollectResult.ALL_OWNED
        return False, GameCollectResult.SUCCESS

    async def collect_epic_games(self) -> GameCollectResult:
        """
        收集 Epic Games 周免游戏

        Returns:
            GameCollectResult: 执行结果
        """
        should_ignore, result = await self._should_ignore_task()

        # 所有游戏已在库中
        if should_ignore:
            logger.success("✅ 所有周免游戏已在库中")
            return GameCollectResult.ALL_OWNED

        # 处理错误情况
        if result != GameCollectResult.SUCCESS:
            # 输出特定格式的错误日志，便于 worker.py 解析
            if result != GameCollectResult.COOKIE_INVALID:
                logger.error(f"❌ GAME_ERROR:{result.value}")
            return result

        # 检查是否有游戏需要领取
        if not self._promotions:
            await self._check_orders()

        if not self._promotions:
            logger.success("✅ 所有周免游戏已在库中")
            return GameCollectResult.ALL_OWNED

        # 输出游戏信息供 worker.py 解析（必须用 INFO 级别）
        for p in self._promotions:
            pj = json.dumps({"title": p.title, "url": p.url}, ensure_ascii=False)
            logger.info(f"发现: {pj}")

        # 执行领取
        if self._promotions:
            try:
                await self.epic_games.collect_weekly_games(self._promotions)
                return GameCollectResult.SUCCESS
            except StoreLoginRequired:
                logger.warning("Store product session requires reauthentication")
                return GameCollectResult.COOKIE_INVALID
            except Exception as e:
                logger.exception(e)
                return GameCollectResult.UNKNOWN_ERROR

        logger.debug("All tasks in the workflow have been completed")
        return GameCollectResult.SUCCESS


class EpicGames:
    def __init__(self, page: Page):
        self.page = page
        self._promotions: List[PromotionGame] = []

    @staticmethod
    def reset_order_history_session(page: Page) -> None:
        _ORDER_HISTORY_UNAUTHORIZED_CONTEXTS.discard(page.context)

    @staticmethod
    async def _save_debug_page(page: Page, label: str):
        with suppress(Exception):
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)
            RUNTIME_DIR.joinpath(f"{safe_label}.json").write_text(
                json.dumps(
                    {"url": page.url, "title": await page.title()},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            await page.evaluate(
                """([email, password]) => {
                    for (const input of document.querySelectorAll('input, textarea')) {
                        if (input.value) input.value = '***';
                    }
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
                        if (email) node.nodeValue = node.nodeValue.split(email).join('***');
                        if (password) node.nodeValue = node.nodeValue.split(password).join('***');
                    }
                }""",
                [settings.EPIC_EMAIL, settings.EPIC_PASSWORD.get_secret_value()],
            )
            html = await page.content()
            html = html.replace(settings.EPIC_EMAIL, "***")
            html = html.replace(settings.EPIC_PASSWORD.get_secret_value(), "***")
            RUNTIME_DIR.joinpath(f"{safe_label}.html").write_text(html, encoding="utf-8")
            await page.screenshot(
                path=str(RUNTIME_DIR.joinpath(f"{safe_label}.png")),
                full_page=True,
            )

    @staticmethod
    async def _fetch_order_history_in_page(page: Page) -> dict | None:
        """Fetch order history from an authenticated account-origin page."""
        result = await page.evaluate(
            """async (endpoint) => {
                const resp = await fetch(endpoint, {
                    credentials: "include",
                    headers: {
                        "accept": "application/json, text/plain, */*",
                        "x-requested-with": "XMLHttpRequest"
                    }
                });
                return {
                    status: resp.status,
                    url: resp.url,
                    text: await resp.text()
                };
            }""",
            URL_ORDER_HISTORY,
        )
        print(
            "ORDER_HISTORY_RESPONSE:in-page:"
            f"{result['status']}:{result['url']}",
            flush=True,
        )
        if (
            200 <= int(result["status"]) < 300
            and result["url"].startswith(URL_ORDER_HISTORY)
        ):
            return json.loads(result["text"])
        logger.warning(
            "In-page order API returned HTTP "
            f"{result['status']}: {result['url']}"
        )
        return None

    @staticmethod
    async def fetch_order_history(page: Page) -> dict:
        """Fetch Epic order history with the browser session."""
        if page.context in _ORDER_HISTORY_UNAUTHORIZED_CONTEXTS:
            raise RuntimeError("Order history is unauthorized in this browser context")
        endpoint = URL_ORDER_HISTORY
        request_headers = {
            "accept": "application/json, text/plain, */*",
            "referer": URL_ACCOUNT_TRANSACTIONS,
            "x-requested-with": "XMLHttpRequest",
        }

        try:
            resp = await page.context.request.get(
                endpoint,
                headers=request_headers,
                timeout=30000,
            )
            print(
                f"ORDER_HISTORY_RESPONSE:playwright:{resp.status}:{resp.url}",
                flush=True,
            )
            if 200 <= int(resp.status) < 300 and resp.url.startswith(endpoint):
                return await resp.json()
            logger.warning(
                f"Playwright order API returned HTTP {resp.status}: {resp.url}"
            )
        except Exception as err:
            logger.warning(f"Playwright order API failed: {err}")

        account_page = None
        try:
            account_page = await page.context.new_page()
            await account_page.goto(
                URL_ACCOUNT_TRANSACTIONS,
                wait_until="domcontentloaded",
                timeout=45000,
            )
            await account_page.wait_for_timeout(3000)
            print(f"ORDER_HISTORY_SESSION:{account_page.url}", flush=True)
            if is_switch_account_url(account_page.url):
                # The portal answers 401 and sends the browser through its
                # switch-account page. Confirming it is what gives the
                # account portal the session that the order history, the
                # checkout and the mobile precheck all run on.
                if await complete_account_switch(account_page):
                    print(
                        f"ORDER_HISTORY_SESSION:after_switch:{account_page.url}",
                        flush=True,
                    )
            if account_page.url.startswith(URL_ACCOUNT_ORIGIN):
                data = await EpicGames._fetch_order_history_in_page(account_page)
                if data is not None:
                    return data
            else:
                logger.warning(
                    "Epic account session was not established; "
                    f"final URL: {account_page.url}"
                )
        except Exception as err:
            logger.warning(f"Epic account session bootstrap failed: {err}")
        finally:
            if account_page is not None:
                with suppress(Exception):
                    await account_page.close()

        cookies = await page.context.cookies()
        headers = dict(request_headers)
        with suppress(Exception):
            headers["user-agent"] = await page.evaluate("navigator.userAgent")

        async with httpx.AsyncClient(
            cookies={cookie["name"]: cookie["value"] for cookie in cookies},
            headers=headers,
            follow_redirects=True,
            timeout=30,
        ) as client:
            resp = await client.get(endpoint)
            print(
                f"ORDER_HISTORY_RESPONSE:httpx:{resp.status_code}:{resp.url}",
                flush=True,
            )
            if resp.status_code == 401:
                _ORDER_HISTORY_UNAUTHORIZED_CONTEXTS.add(page.context)
            resp.raise_for_status()
            if not str(resp.url).startswith(endpoint):
                raise RuntimeError(f"Order API redirected to {resp.url}")
            return resp.json()

    @staticmethod
    async def _agree_license(page: Page):
        logger.debug("Agree license")
        with suppress(TimeoutError):
            await page.click("//label[@for='agree']", timeout=4000)
            accept = page.locator("//button//span[text()='Accept']")
            if await accept.is_enabled():
                await accept.click()

    @staticmethod
    async def _active_purchase_container(page: Page):
        logger.debug("Scanning for purchase container...")

        # Epic 的新结账页不稳定：确认按钮可能在 webPurchase iframe、
        # 其它 purchase iframe、甚至主页面弹层里。这里不再只选第一个 iframe，
        # 而是扫描主页面和所有 frame，避免命中无关 iframe 后误报。
        await page.wait_for_timeout(3000)

        button_texts = [
            "PLACE ORDER",
            "Place Order",
            "GET",
            "Get",
            "ADD TO LIBRARY",
            "Add to library",
            "Add To Library",
            "BUY NOW",
            "Buy Now",
            "CONFIRM",
            "Confirm",
            "Confirm Order",
            "Complete Order",
            "Submit Order",
        ]
        css_selectors = [
            "button[data-testid='purchase-button']",
            "button[data-testid='place-order-button']",
            "button[data-testid='confirm-order-button']",
            "button[data-testid*='purchase']",
            "button[data-testid*='order']",
            "button[data-testid*='confirm']",
            "button.payment-btn",
            "button[class*='payment-confirm']",
            "button[class*='confirm']",
            "button[type='submit']",
        ]

        purchase_frames: list[tuple[str, Any]] = []
        other_frames: list[tuple[str, Any]] = []
        for idx, frame in enumerate(page.frames):
            if frame == page.main_frame:
                continue
            item = (f"frame[{idx}] {frame.url[:180]}", frame)
            if "/purchase" in frame.url:
                purchase_frames.append(item)
            else:
                other_frames.append(item)
        containers: list[tuple[str, Any]] = [
            *purchase_frames,
            ("page", page),
            *other_frames,
        ]

        logger.info(f"🔎 扫描结账容器: {len(containers)} 个候选")

        async def _button_is_usable(btn, timeout: int = 500) -> bool:
            try:
                if not await btn.is_visible(timeout=timeout):
                    return False
                if await btn.is_disabled(timeout=timeout):
                    return False
                return True
            except Exception:
                return False

        async def _is_main_page_product_cta(label: str, btn) -> bool:
            if label != "page":
                return False
            with suppress(Exception):
                return await btn.get_attribute("data-testid") == "purchase-cta-button"
            return False

        async def _describe_buttons(label: str, container: Any):
            try:
                buttons = await container.locator("button").all()
                logger.warning(f"🔍 {label} 按钮数量: {len(buttons)}")
                for i, btn in enumerate(buttons[:12]):
                    try:
                        text = (await btn.text_content(timeout=1000) or "").strip()
                        aria = await btn.get_attribute("aria-label", timeout=1000)
                        testid = await btn.get_attribute("data-testid", timeout=1000)
                        disabled = await btn.is_disabled(timeout=1000)
                        logger.warning(
                            f"🔍 {label} button[{i}]: text={text!r}, aria={aria!r}, "
                            f"testid={testid!r}, disabled={disabled}"
                        )
                    except Exception as e:
                        logger.warning(f"🔍 {label} button[{i}] inspect failed: {e}")
            except Exception as e:
                logger.warning(f"🔍 {label} list buttons failed: {e}")

        for label, container in containers:
            logger.info(f"🔎 检查结账容器: {label}")

            # Read the currently rendered buttons once. Sequentially waiting on
            # every possible text/selector can take several minutes when an
            # hCaptcha frame is open, even though Add to library is already in
            # the purchase iframe.
            with suppress(Exception):
                for btn in await container.locator("button").all():
                    if not await _button_is_usable(btn):
                        continue
                    btn_text = " ".join(
                        ((await btn.text_content(timeout=500)) or "").split()
                    )
                    if not btn_text:
                        continue
                    normalized = btn_text.casefold()
                    if not any(
                        text_value.casefold() in normalized
                        for text_value in button_texts
                    ):
                        continue
                    if await _is_main_page_product_cta(label, btn):
                        logger.debug(
                            "Skipping the product-page CTA while locating checkout"
                        )
                        continue
                    logger.info(
                        f"✅ 找到结账按钮: {btn_text!r} | 容器: {label} | rendered button"
                    )
                    return container, btn

            for text_value in button_texts:
                try:
                    btn = container.locator("button", has_text=text_value).first
                    if await _button_is_usable(btn):
                        if await _is_main_page_product_cta(label, btn):
                            logger.debug("Skipping the product-page CTA while locating checkout")
                            continue
                        btn_text = (await btn.text_content(timeout=1000) or "").strip()
                        logger.info(f"✅ 找到结账按钮: {btn_text!r} | 容器: {label} | 文本: {text_value}")
                        return container, btn
                except Exception as e:
                    logger.debug(f"Button text {text_value!r} failed in {label}: {e}")

            for selector in css_selectors:
                try:
                    btn = container.locator(selector).first
                    if await _button_is_usable(btn):
                        if await _is_main_page_product_cta(label, btn):
                            logger.debug("Skipping the product-page CTA while locating checkout")
                            continue
                        btn_text = (await btn.text_content(timeout=1000) or "").strip()
                        logger.info(f"✅ 找到结账按钮: {btn_text!r} | 容器: {label} | 选择器: {selector}")
                        return container, btn
                except Exception as e:
                    logger.debug(f"Button selector {selector!r} failed in {label}: {e}")

        logger.warning("Primary buttons not found. Debugging checkout containers...")
        for label, container in containers:
            await _describe_buttons(label, container)

        with suppress(Exception):
            debug_path = RUNTIME_DIR.joinpath("checkout_debug_last.html")
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            debug_path.write_text(await page.content(), encoding="utf-8")
            logger.warning(f"🧾 已保存结账页调试 HTML: {debug_path}")

        raise AssertionError("Could not find Place Order button in checkout containers")

    @staticmethod
    async def _handle_device_not_supported_modal(
        page: Page, timeout_ms: int = 3000
    ) -> bool:
        """Continue past Epic's intermediate unsupported-device modal."""
        dialog = page.locator("[role='dialog']").filter(has_text="Device not supported").first

        try:
            await dialog.wait_for(state="visible", timeout=timeout_ms)
        except Exception:
            return False

        body_text = ""
        with suppress(Exception):
            body_text = (await dialog.text_content(timeout=1000) or "").strip()

        if "not compatible with your current device" not in body_text:
            return False

        continue_btn = dialog.locator("button", has_text="Continue").first
        try:
            await continue_btn.wait_for(state="visible", timeout=3000)
            if await continue_btn.is_disabled(timeout=1000):
                logger.warning("⚠️ Epic 设备不支持弹窗的 Continue 按钮不可点击")
                return False

            logger.info("ℹ️ Epic 显示设备不支持提示，点击 Continue 继续领取流程")
            await continue_btn.click(force=True)
            await page.wait_for_timeout(3000)
            return True
        except Exception as err:
            logger.warning(f"⚠️ 处理 Epic 设备不支持弹窗失败: {err}")
            return False

    @classmethod
    async def _ensure_purchase_checkout_open(cls, page: Page) -> bool:
        if any("/purchase" in frame.url for frame in page.frames if frame != page.main_frame):
            return False

        purchase_btn = await product_cta(page, timeout=2000)
        if purchase_btn is None:
            return False
        try:
            if await purchase_btn.is_disabled(timeout=1000):
                return False
            text = " ".join((await purchase_btn.text_content() or "").split()).casefold()
            if text not in {"get", "install", "claim", "buy now", "purchase"}:
                return False
        except Exception:
            return False

        logger.warning(
            "Purchase iframe is not open; clicking the product CTA again before checkout"
        )
        await purchase_btn.click(force=True)
        await cls._handle_device_not_supported_modal(page, timeout_ms=20000)
        await page.wait_for_timeout(2500)
        return True

    @staticmethod
    async def _uk_confirm_order(wpc: Any):
        logger.debug("UK confirm order")
        with suppress(TimeoutError):
            accept = wpc.locator("//button[contains(@class, 'payment-confirm__btn')]")
            if await accept.is_enabled(timeout=5000):
                await accept.click()
                return True

    @staticmethod
    async def _store_refusal_reason(page: Page) -> str:
        """What the store says when it will not hand the game over.

        Epic states the reason on the page it refuses on: a region notice for
        a game the account's country cannot buy, and
        "Purchasing Shrunken Head DLC requires: Sker Ritual" for a promotion
        whose base game the account does not own. Either way the run has
        nothing left to try, and the reason belongs in the log.
        """
        body = ""
        with suppress(Exception):
            body = await page.locator("body").inner_text(timeout=3000)
        normalized = " ".join(body.lower().split())
        if any(marker in normalized for marker in REGION_UNAVAILABLE_MARKERS):
            return normalized[:200]
        for marker in ("requires:", "requires :", "prerequisite"):
            position = normalized.find(marker)
            if position >= 0:
                return normalized[max(0, position - 80): position + 120]
        return ""

    @staticmethod
    async def _is_region_unavailable_page(page: Page) -> bool:
        with suppress(Exception):
            body_text = await page.locator("body").inner_text(timeout=3000)
            normalized = " ".join(body_text.lower().split())
            return any(marker in normalized for marker in REGION_UNAVAILABLE_MARKERS)
        return False

    async def _skip_region_unavailable(self, page: Page, promotion: PromotionGame) -> bool:
        if not await self._is_region_unavailable_page(page):
            return False

        logger.warning(f"REGION_UNAVAILABLE:{promotion.title}")
        await self._save_debug_page(page, f"region_unavailable_{promotion.namespace}")
        return True

    async def _owned_from_product_page(self, page: Page, promotion: PromotionGame) -> bool:
        with suppress(Exception):
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

        with suppress(Exception):
            purchase_btn = await product_cta(page)
            if purchase_btn is None:
                return False
            btn_text = (await purchase_btn.text_content(timeout=5000) or "").strip().upper()
            logger.info(f"Ownership CTA for {promotion.title}: {btn_text!r}")
            if any(marker in btn_text for marker in ("IN LIBRARY", "OWNED")):
                return True

        await self._save_debug_page(page, f"ownership_not_verified_{promotion.namespace}")
        return False

    @staticmethod
    async def _owned_from_order_history(page: Page, promotion: PromotionGame) -> bool:
        try:
            data = await EpicGames.fetch_order_history(page)
        except Exception as err:
            logger.warning(f"订单历史验证失败: {err}")
            return False

        for raw_order in data.get("orders", []):
            with suppress(Exception):
                order = Order(**raw_order)
                if order.orderType != "PURCHASE":
                    continue
                for item in order.items:
                    if item.namespace == promotion.namespace or item.offerId == promotion.id:
                        return True
        return False

    async def _wait_until_owned(self, page: Page, promotion: PromotionGame) -> bool:
        for attempt in range(1, 5):
            if await self._owned_from_order_history(page, promotion):
                logger.success(f"🎉 订单历史确认已领取: {promotion.title}")
                emit_desktop_result(promotion.title, "verified_owned")
                return True
            if await self._owned_from_product_page(page, promotion):
                logger.success(f"🎉 商品页确认已入库: {promotion.title}")
                emit_desktop_result(promotion.title, "verified_owned")
                return True
            logger.warning(f"⚠️ 未确认入库，等待后重试 [{attempt}/4]: {promotion.title}")
            await page.wait_for_timeout(5000)
        return False

    @staticmethod
    async def _has_visible_hcaptcha(page: Page) -> bool:
        selectors = (
            "iframe[title='hCaptcha challenge']:visible",
            "iframe[src*='hcaptcha.com/captcha/']:visible",
            ".h_captcha_challenge:visible iframe",
        )
        containers = [page, *[frame for frame in page.frames if frame != page.main_frame]]
        for container in containers:
            for selector in selectors:
                with suppress(Exception):
                    locator = container.locator(selector)
                    if await locator.count() > 0 and await locator.first.is_visible():
                        return True
        return False

    @staticmethod
    async def _log_checkout_button_state(button: Any, phase: str) -> None:
        try:
            state = await button.evaluate(
                """element => {
                    const rect = element.getBoundingClientRect();
                    const centerX = rect.left + rect.width / 2;
                    const centerY = rect.top + rect.height / 2;
                    const hit = document.elementFromPoint(centerX, centerY);
                    const style = window.getComputedStyle(element);
                    return {
                        tag: element.tagName,
                        text: (element.innerText || element.textContent || '').trim(),
                        type: element.getAttribute('type'),
                        testid: element.getAttribute('data-testid'),
                        className: typeof element.className === 'string'
                            ? element.className
                            : '',
                        disabled: Boolean(element.disabled),
                        ariaDisabled: element.getAttribute('aria-disabled'),
                        connected: element.isConnected,
                        pointerEvents: style.pointerEvents,
                        visibility: style.visibility,
                        opacity: style.opacity,
                        rect: {
                            x: Math.round(rect.x),
                            y: Math.round(rect.y),
                            width: Math.round(rect.width),
                            height: Math.round(rect.height),
                        },
                        hit: hit ? {
                            tag: hit.tagName,
                            text: (hit.innerText || hit.textContent || '').trim().slice(0, 120),
                            testid: hit.getAttribute('data-testid'),
                            className: typeof hit.className === 'string'
                                ? hit.className
                                : '',
                        } : null,
                    };
                }"""
            )
            logger.info(
                f"CHECKOUT_BUTTON_STATE:{phase}:"
                f"{json.dumps(state, ensure_ascii=False, sort_keys=True)}"
            )
        except Exception as err:
            logger.warning(f"CHECKOUT_BUTTON_STATE:{phase}:inspect_failed:{err}")

    async def _wait_for_order_button(self, page: Page, attempts: int = 4):
        """Wait for the order form that follows a solved challenge.

        Epic renders the order form once its Talon execution has accepted the
        challenge, and that takes a moment longer than the form of a checkout
        without a challenge.
        """
        last_error: AssertionError | None = None
        for attempt in range(attempts):
            try:
                return await self._active_purchase_container(page)
            except AssertionError as err:
                last_error = err
                logger.info(
                    f"等待下单按钮 ({attempt + 1}/{attempts})"
                )
                await page.wait_for_timeout(4000)
        raise last_error

    @staticmethod
    async def _checkout_has_captcha(page: Page) -> bool:
        """Return whether the checkout shows a hCaptcha challenge.

        Epic's "One more step" page keeps the challenge in a Talon container,
        and the frame of the widget is only there once Epic asks for it. The
        frame is not necessarily visible, so the frames of the page are
        searched as well.
        """
        for frame in page.frames:
            if "hcaptcha.com" in (frame.url or ""):
                return True
        for selector in (
            "iframe[src*='hcaptcha.com/captcha/']",
            "iframe[title='hCaptcha challenge']",
            ".h_captcha_challenge iframe",
            "[id^='h_captcha_challenge_'] iframe",
        ):
            with suppress(Exception):
                if await page.locator(selector).count() > 0:
                    return True
        return False

    async def _solve_checkout_captcha(self, page: Page, agent) -> bool:
        """Answer the hCaptcha the checkout shows before its order form.

        Epic's checkout puts a "One more step" security check in front of the
        order form. The page runs it through the Talon service, and a "TRY
        AGAIN" button re-runs it once the challenge has been answered.
        """
        if not await self._checkout_has_captcha(page):
            return False
        logger.info("结账页先出现验证码，解题后再找下单按钮")
        try:
            challenge_result = await asyncio.wait_for(
                agent.wait_for_challenge(),
                timeout=settings.CHECKOUT_CAPTCHA_TIMEOUT_SECONDS,
            )
            print(
                f"CHECKOUT_CAPTCHA_RESULT:{getattr(challenge_result, 'value', challenge_result)}",
                flush=True,
            )
            if str(getattr(challenge_result, "value", challenge_result)) != "success":
                # The plan of this widget holds a wrong answer now, so the
                # checkout is reopened for a widget with a clean one.
                logger.warning("结账验证码未通过，重新打开结账流程")
                return False
        except asyncio.TimeoutError:
            logger.warning("Checkout hCaptcha exceeded its time budget")
            return False
        except Exception as err:
            logger.warning(f"结账验证码未确认通过: {err}")
            return False
        await page.wait_for_timeout(2000)
        await self._retry_talon_execution(page)
        return True

    @staticmethod
    async def _retry_talon_execution(page: Page) -> bool:
        """Re-run Epic's Talon execution after the challenge was answered.

        The checkout's "TRY AGAIN" button calls `talon.execute` again, which
        is what turns the solved challenge into the order form.
        """
        retry = page.get_by_role("button", name=re.compile(r"try again", re.I))
        with suppress(Exception):
            if await retry.count() == 0 or not await retry.first.is_visible(
                timeout=1000
            ):
                return False
            await retry.first.click(timeout=5000)
            logger.info("已重新触发结账页的 Talon 校验")
            await page.wait_for_timeout(3000)
            return True
        return False

    async def _handle_instant_checkout(self, page: Page) -> bool:
        return await self._handle_legacy_instant_checkout(page)

    async def _handle_legacy_instant_checkout(self, page: Page) -> bool:
        """Use the checkout sequence proven by the successful August runs."""
        logger.info("🚀 开始即时结账流程...")
        agent = get_legacy_checkout_agent(page)
        prepare_for_new_challenge = getattr(agent, "prepare_for_new_challenge", None)
        if callable(prepare_for_new_challenge):
            prepare_for_new_challenge()

        try:
            await self._handle_device_not_supported_modal(page)
            await self._ensure_purchase_checkout_open(page)
            try:
                _, payment_btn = await self._active_purchase_container(page)
            except AssertionError:
                # The checkout puts its hCaptcha in front of the order form,
                # so the button is not there yet. Solve the challenge first
                # and look for the button again.
                if not await self._solve_checkout_captcha(page, agent):
                    raise
                _, payment_btn = await self._wait_for_order_button(page)
            if await self._handle_device_not_supported_modal(page):
                _, payment_btn = await self._active_purchase_container(page)

            logger.debug(f"点击支付按钮: {await payment_btn.text_content()}")
            await payment_btn.click(force=True)
            await page.wait_for_timeout(3000)

            try:
                logger.debug("检查验证码...")
                challenge_result = await asyncio.wait_for(
                    agent.wait_for_challenge(),
                    timeout=settings.CHECKOUT_CAPTCHA_TIMEOUT_SECONDS,
                )
                print(
                    f"CHECKOUT_CAPTCHA_RESULT:{getattr(challenge_result, 'value', challenge_result)}",
                    flush=True,
                )
                if str(getattr(challenge_result, "value", challenge_result)) != "success":
                    logger.warning("结账验证码未通过，重新打开结账流程")
                    return False
            except asyncio.TimeoutError:
                logger.warning("Checkout hCaptcha exceeded its time budget")
                return False
            except Exception as err:
                logger.warning(f"结账验证码未确认通过: {err}")

            with suppress(Exception):
                if not await payment_btn.is_visible():
                    logger.success("🎉 结账按钮已消失，等待入库验证")
                    return True

            with suppress(Exception):
                await payment_btn.click(force=True)
                await page.wait_for_timeout(2000)

            with suppress(Exception):
                if not await payment_btn.is_visible(timeout=3000):
                    logger.success("🎉 二次提交后结账按钮已消失，等待入库验证")
                    return True

            logger.warning("⚠️ 结账按钮仍可见，尚不能确认领取成功")
            return False
        except Exception as err:
            logger.warning(f"⚠️ 即时结账警告（游戏可能已领取）: {err}")
            with suppress(Exception):
                await page.reload()
            return False

    async def _handle_diagnostic_instant_checkout(self, page: Page) -> bool:
        logger.info("🚀 开始即时结账流程...")
        agent = replace_hcaptcha_agent(page)
        confirm_order_responses: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        checkout_request_ids: dict[int, int] = {}
        checkout_request_counter = 0
        confirm_order_request_counter = 0
        captcha_validation_done = asyncio.Event()
        captcha_validation_succeeded = False
        first_confirm_order_held = False

        async def hold_first_confirm_order(route) -> None:
            nonlocal first_confirm_order_held
            if not first_confirm_order_held:
                first_confirm_order_held = True
                logger.info(
                    "Holding the first confirm-order request until the "
                    "current hCaptcha transaction completes"
                )
                try:
                    await asyncio.wait_for(
                        captcha_validation_done.wait(),
                        timeout=max(
                            settings.CHECKOUT_CAPTCHA_TIMEOUT_SECONDS,
                            settings.EXECUTION_TIMEOUT
                            + settings.RESPONSE_TIMEOUT
                            + 15,
                        ),
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Timed out waiting for hCaptcha validation before "
                        "releasing confirm-order"
                    )
                if captcha_validation_succeeded and settings.CHECKOUT_CONFIRM_DELAY_MS > 0:
                    await page.wait_for_timeout(settings.CHECKOUT_CONFIRM_DELAY_MS)
                logger.info(
                    "Releasing the first confirm-order request after hCaptcha validation"
                )
            await route.continue_()

        async def capture_checkout_request(request) -> None:
            nonlocal checkout_request_counter, confirm_order_request_counter
            if not is_checkout_diagnostic_url(request.url):
                return
            checkout_request_counter += 1
            if "/purchase/confirm-order" in request.url:
                confirm_order_request_counter += 1
            trace_id = checkout_request_counter
            checkout_request_ids[id(request)] = trace_id
            parsed_url = urlsplit(request.url)
            summary = checkout_body_summary(request.post_data)
            with suppress(Exception):
                headers = await request.all_headers()
                for key, value in headers.items():
                    if any(
                        marker in key.casefold()
                        for marker in CHECKOUT_DIAGNOSTIC_SECRET_MARKERS
                    ):
                        summary[f"header.{key}"] = diagnostic_fingerprint(value)
            frame_url = ""
            with suppress(Exception):
                frame_url = request.frame.url if request.frame else ""
            logger.info(
                "CHECKOUT_NET_REQUEST:"
                f"{trace_id}:{request.method}:{parsed_url.netloc}{parsed_url.path}:"
                f"{json.dumps({
                    "url": request.url,
                    "resource_type": request.resource_type,
                    "frame_url": frame_url,
                    "body": summary,
                }, ensure_ascii=False, sort_keys=True)}"
            )

        async def capture_confirm_order_response(response) -> None:
            parsed_url = urlsplit(response.url)
            if is_checkout_diagnostic_url(response.url):
                trace_id = checkout_request_ids.get(id(response.request), 0)
                body_text = ""
                with suppress(Exception):
                    body_text = await response.text()
                logger.info(
                    "CHECKOUT_NET_RESPONSE:"
                    f"{trace_id}:{response.status}:"
                    f"{parsed_url.netloc}{parsed_url.path}:"
                    f"{json.dumps(checkout_body_summary(body_text), ensure_ascii=False, sort_keys=True)}"
                )
            if "/purchase/confirm-order" not in response.url:
                return
            payload = {}
            with suppress(Exception):
                payload = await response.json()
            confirm_order_responses.put_nowait(
                {
                    "status": response.status,
                    "payload": payload if isinstance(payload, dict) else {},
                }
            )

        async def wait_for_checkout_outcome(timeout_seconds: float = 15) -> dict[str, Any]:
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            while asyncio.get_running_loop().time() < deadline:
                if not confirm_order_responses.empty():
                    return confirm_order_responses.get_nowait()
                try:
                    if not await payment_btn.is_visible(timeout=500):
                        return {"button_hidden": True}
                except Exception:
                    return {"button_hidden": True}
                await page.wait_for_timeout(250)
            return {}

        async def retry_checkout_after_captcha_rejection() -> dict[str, Any]:
            """Use the historical one-time retry after Epic rejects the pass."""
            logger.info(
                "Retrying checkout once after Epic rejected the hCaptcha pass"
            )
            try:
                _, retry_button = await self._active_purchase_container(page)
                await retry_button.wait_for(state="visible", timeout=10000)
                await page.wait_for_timeout(1000)
                await retry_button.click(force=True)
            except Exception as err:
                logger.warning(f"Could not resubmit checkout after captcha rejection: {err}")
                return {}

            if await agent.wait_for_challenge_start(timeout_seconds=10):
                try:
                    retry_result = await asyncio.wait_for(
                        agent.wait_for_challenge(),
                        timeout=max(
                            settings.CHECKOUT_CAPTCHA_TIMEOUT_SECONDS,
                            settings.EXECUTION_TIMEOUT
                            + settings.RESPONSE_TIMEOUT
                            + 15,
                        ),
                    )
                except Exception as err:
                    logger.warning(f"Retry checkout hCaptcha was not confirmed: {err}")
                    return {}
                if getattr(retry_result, "value", retry_result) != "success":
                    logger.warning(f"Retry checkout hCaptcha result: {retry_result}")
                    return {}

            return await wait_for_checkout_outcome(timeout_seconds=20)

        page.on("request", capture_checkout_request)
        page.on("response", capture_confirm_order_response)
        await page.route(URL_CONFIRM_ORDER, hold_first_confirm_order)

        try:
            await self._handle_device_not_supported_modal(page)
            await self._ensure_purchase_checkout_open(page)
            wpc, payment_btn = await self._active_purchase_container(page)
            if await self._handle_device_not_supported_modal(page):
                wpc, payment_btn = await self._active_purchase_container(page)

            agent.prepare_for_new_challenge()
            logger.debug(f"点击支付按钮: {await payment_btn.text_content()}")
            await payment_btn.click(force=True)

            # The historical successful flow allowed Epic's Talon checkout
            # session to finish initializing before the solver interacted with
            # the challenge. Keep that transaction-scoped settling window.
            await page.wait_for_timeout(3000)

            challenge_started = await agent.wait_for_challenge_start(timeout_seconds=45)
            if challenge_started:
                try:
                    logger.debug("检查验证码...")
                    challenge_result = await asyncio.wait_for(
                        agent.wait_for_challenge(),
                        timeout=max(
                            settings.CHECKOUT_CAPTCHA_TIMEOUT_SECONDS,
                            settings.EXECUTION_TIMEOUT
                            + settings.RESPONSE_TIMEOUT
                            + 15,
                        ),
                    )
                except Exception as e:
                    captcha_validation_done.set()
                    logger.warning(f"结账验证码未确认通过: {e}")
                else:
                    captcha_validation_succeeded = (
                        getattr(challenge_result, "value", challenge_result)
                        == "success"
                    )
                    captcha_validation_done.set()
                    if not captcha_validation_succeeded:
                        logger.warning(f"结账验证码结果: {challenge_result}")
                    else:
                        logger.info(
                            "hCaptcha validated; the held confirm-order request "
                            "can now continue"
                        )
            else:
                captcha_validation_done.set()
                logger.debug("结账等待 45 秒后未出现 hCaptcha，检查提交结果")

            outcome = await wait_for_checkout_outcome()
            if outcome.get("button_hidden"):
                logger.success("🎉 结账按钮已消失，等待入库验证")
                return True

            status = int(outcome.get("status") or 0)
            payload = outcome.get("payload") or {}
            if 200 <= status < 300:
                logger.success(
                    f"🎉 Epic confirm-order 返回 HTTP {status}，等待入库验证"
                )
                return True

            if is_retryable_confirm_order_failure(status, payload):
                retry_outcome = await retry_checkout_after_captcha_rejection()
                if retry_outcome.get("button_hidden"):
                    logger.success("🎉 二次提交后结账按钮已消失，等待入库验证")
                    return True
                retry_status = int(retry_outcome.get("status") or 0)
                if 200 <= retry_status < 300:
                    logger.success(
                        f"🎉 二次提交后 Epic confirm-order 返回 HTTP {retry_status}，等待入库验证"
                    )
                    return True
                if retry_status:
                    retry_payload = retry_outcome.get("payload") or {}
                    logger.warning(
                        "Epic retry confirm-order rejected checkout: "
                        f"HTTP {retry_status}, errorCode={retry_payload.get('errorCode', '')}"
                    )

            if status:
                logger.warning(
                    "Epic confirm-order rejected checkout: "
                    f"HTTP {status}, errorCode={payload.get('errorCode', '')}"
                )

            logger.warning("⚠️ 结账按钮仍可见，尚不能确认领取成功")
            return False

        except Exception as err:
            logger.warning(f"⚠️ 即时结账警告（游戏可能已领取）: {err}")
            with suppress(Exception):
                await page.reload()
            return False
        finally:
            with suppress(Exception):
                page.remove_listener("request", capture_checkout_request)
            with suppress(Exception):
                page.remove_listener("response", capture_confirm_order_response)
            with suppress(Exception):
                await page.unroute(
                    URL_CONFIRM_ORDER, hold_first_confirm_order
                )

    async def add_promotion_to_cart(self, page: Page, promotions: List[PromotionGame]) -> bool:
        has_pending_cart_items = False

        for promotion in promotions:
            url = promotion.url
            await page.goto(url, wait_until="load")

            # 404 检测
            title = await page.title()
            if "404" in title or "Page Not Found" in title:
                logger.error(f"❌ Invalid URL (404 Page): {url}")
                continue

            if await self._skip_region_unavailable(page, promotion):
                continue

            if await store_signed_out(page):
                raise StoreLoginRequired()

            # 处理年龄限制弹窗
            try:
                continue_btn = page.locator("//button//span[text()='Continue']")
                if await continue_btn.is_visible(timeout=5000):
                    await continue_btn.click()
            except Exception:
                pass 

            # ------------------------------------------------------------
            # 🔥 按钮识别与状态判断
            # ------------------------------------------------------------

            # 1. 尝试找到主按钮
            purchase_btn = await product_cta(page)

            # 2. 检查按钮可见性
            try:
                if purchase_btn is None:
                    if await self._skip_region_unavailable(page, promotion):
                        continue
                    if await self._owned_from_product_page(page, promotion):
                         logger.success(f"✅ 游戏已在库中")
                         emit_desktop_result(promotion.title, "already_owned")
                         continue
                    # The page may still be loading, or the game may
                    # already sit in the cart from an earlier run, where the
                    # shelf shows a cart-shaped control instead of a price
                    # one. The cart step reports what it holds (`items=N`), so
                    # the run continues there instead of losing the game
                    # here. Run 40699854560 lost `Sker Ritual` this way after
                    # the previous run had already put it in the cart.
                    logger.warning(
                        f"⚠️ 未找到购买按钮，转入购物车确认: {promotion.title}"
                    )
                    has_pending_cart_items = True
                    continue
            except AssertionError:
                raise
            except Exception:
                pass

            # 3. 获取按钮信息
            btn_text = await purchase_btn.text_content()
            if not btn_text: btn_text = ""
            btn_text = btn_text.strip()
            btn_text_upper = btn_text.upper()
            is_disabled = await purchase_btn.is_disabled()

            # 4. 打印按钮状态（关键信息）
            logger.info(f"📋 按钮状态: '{btn_text}' | 禁用: {is_disabled}")

            # 5. 根据状态判断
            if is_disabled:
                if any(s in btn_text_upper for s in ["IN LIBRARY", "OWNED"]):
                    logger.success(f"✅ 游戏已在库中")
                    emit_desktop_result(promotion.title, "already_owned")
                    continue
                await self._save_debug_page(page, f"purchase_disabled_{promotion.namespace}")
                raise AssertionError(
                    f"Purchase button is disabled without ownership marker for {promotion.title}: {btn_text!r}"
                )

            if any(s in btn_text_upper for s in ["IN LIBRARY", "OWNED"]):
                logger.success(f"✅ 游戏已在库中")
                emit_desktop_result(promotion.title, "already_owned")
                continue

            if "ADD TO CART" in btn_text_upper or "PRE-PURCHASE" in btn_text_upper:
                # A click that lands before Epic's page is hydrated does
                # nothing at all, and the cart stays empty. Run 40696732890
                # clicked `Add to Cart` twice and reached the cart with
                # `items=0`, so the click is repeated until the page says the
                # game left the shelf, and the state after each click is
                # logged for the run that still fails.
                added = False
                for add_attempt in range(1, 4):
                    logger.info(f"🛒 加入购物车 [{add_attempt}/3]")
                    with suppress(Exception):
                        await purchase_btn.click()
                    await page.wait_for_timeout(2500)
                    with suppress(Exception):
                        after = (await purchase_btn.text_content() or "").strip()
                        dialog = await page.evaluate(
                            """() => {
                                const node = document.querySelector(
                                    '[role=dialog], [aria-modal=true]'
                                );
                                return node ? (node.innerText || '').trim().slice(0, 120) : '';
                            }"""
                        )
                        logger.info(
                            f"🛒 加购后按钮: {after!r} | 弹窗: {dialog!r}"
                        )
                        if after and "CART" not in after.upper():
                            added = True
                            break
                    with suppress(Exception):
                        await page.reload(wait_until="load")
                        await page.wait_for_timeout(3000)
                        purchase_btn = await product_cta(page)
                        if purchase_btn is None:
                            added = True
                            break
                        current = (await purchase_btn.text_content() or "").strip()
                        if "CART" not in current.upper():
                            added = True
                            break
                logger.info(f"🛒 加购结果: added={added}")
                has_pending_cart_items = True
                continue

            if "CART" in btn_text_upper:
                # `View in Cart` and `In Cart` are Epic's cart affordance, not
                # proof that this game is the thing in the cart: run 40724013740
                # read `View in Cart` on `Sker Ritual` while the cart held
                # `Shrunken Head DLC`, so the game was never added and the
                # checkout stayed disabled by a missing prerequisite. The cart
                # is read before it is trusted.
                if await self._cart_holds(promotion):
                    logger.info(f"🛒 游戏已在购物车，直接结账: {btn_text!r}")
                    has_pending_cart_items = True
                    continue
                logger.warning(
                    f"⚠️ 购物车里不是这个游戏，清空后重新加购: {promotion.title}"
                )
                await self._clear_cart(page)
                await page.goto(url, wait_until="load")
                await page.wait_for_timeout(3000)
                if await store_signed_out(page):
                    raise StoreLoginRequired()
                purchase_btn = await product_cta(page)
                if purchase_btn is not None:
                    with suppress(Exception):
                        await purchase_btn.click()
                    await page.wait_for_timeout(2500)
                has_pending_cart_items = True
                continue

            # 6. 尝试领取
            # 只要不是黑名单，也不是购物车，统统当做 "Get/Purchase" 直接点击！
            logger.debug(f"⚡️ 尝试点击按钮: {btn_text}")
            max_attempts = max(1, settings.CHECKOUT_MAX_ATTEMPTS)
            verified = False
            last_error = f"Checkout was not submitted for {promotion.title}"

            for checkout_attempt in range(1, max_attempts + 1):
                if checkout_attempt > 1:
                    logger.warning(
                        f"Retry checkout [{checkout_attempt}/{max_attempts}]: {promotion.title}"
                    )
                    with suppress(Exception):
                        await page.keyboard.press("Escape")
                    await page.goto(url, wait_until="load")
                    await page.wait_for_timeout(3000)
                    if await store_signed_out(page):
                        raise StoreLoginRequired()
                    purchase_btn = await product_cta(page)
                    if purchase_btn is None:
                        if await self._skip_region_unavailable(page, promotion):
                            verified = True
                            break
                        if await self._owned_from_product_page(page, promotion):
                            verified = True
                            break
                        last_error = f"Could not find purchase button for {promotion.title}"
                        continue

                    btn_text = (await purchase_btn.text_content() or "").strip()
                    btn_text_upper = btn_text.upper()
                    is_disabled = await purchase_btn.is_disabled()
                    logger.info(
                        f"Retry button state [{checkout_attempt}/{max_attempts}]: "
                        f"{btn_text!r} | disabled: {is_disabled}"
                    )
                    if any(s in btn_text_upper for s in ["IN LIBRARY", "OWNED"]):
                        verified = True
                        break
                    if is_disabled:
                        await self._save_debug_page(
                            page, f"retry_purchase_disabled_{promotion.namespace}"
                        )
                        last_error = (
                            "Purchase button is disabled without ownership marker "
                            f"for {promotion.title}: {btn_text!r}"
                        )
                        continue

                await purchase_btn.click()
                await page.wait_for_timeout(3000)
                if is_login_url(page.url):
                    raise StoreLoginRequired()

                # 点击后，转入即时结账流程
                checkout_submitted = await self._handle_instant_checkout(page)
                if is_login_url(page.url):
                    raise StoreLoginRequired()
                if not checkout_submitted:
                    logger.warning(
                        f"Checkout not submitted [{checkout_attempt}/{max_attempts}]: "
                        f"{promotion.title}"
                    )
                    if await self._owned_from_order_history(page, promotion):
                        logger.success(
                            f"🎉 结账结果不明确，但订单历史确认已领取: {promotion.title}"
                        )
                        emit_desktop_result(promotion.title, "verified_owned")
                        verified = True
                        break
                    if await self._owned_from_product_page(page, promotion):
                        logger.success(
                            f"🎉 结账结果不明确，但商品页确认已入库: {promotion.title}"
                        )
                        emit_desktop_result(promotion.title, "verified_owned")
                        verified = True
                        break
                    continue

                if await self._wait_until_owned(page, promotion):
                    verified = True
                    break

                last_error = f"Could not verify ownership after checkout: {promotion.title}"
                logger.warning(
                    f"Ownership not verified [{checkout_attempt}/{max_attempts}]: "
                    f"{promotion.title}"
                )

            if not verified:
                refusal = await self._store_refusal_reason(page)
                if refusal:
                    status = (
                        "region_unavailable"
                        if any(m in refusal for m in REGION_UNAVAILABLE_MARKERS)
                        else "not_purchasable"
                    )
                    logger.warning(f"{status.upper()}:{promotion.title}:{refusal}")
                    emit_desktop_result(promotion.title, status)
                    continue
                raise AssertionError(last_error)
            # ------------------------------------------------------------

        return has_pending_cart_items

    async def _empty_cart(self, page: Page, wait_rerender: int = 30) -> bool | None:
        has_paid_free = False
        try:
            cards = await page.query_selector_all("//div[@data-testid='offer-card-layout-wrapper']")
            for card in cards:
                is_free = await card.query_selector("//span[text()='Free']")
                if not is_free:
                    has_paid_free = True
                    wishlist_btn = await card.query_selector(
                        "//button//span[text()='Move to wishlist']"
                    )
                    await wishlist_btn.click()

            if has_paid_free and wait_rerender:
                wait_rerender -= 1
                await page.wait_for_timeout(2000)
                return await self._empty_cart(page, wait_rerender)
            return True
        except TimeoutError as err:
            logger.warning(f"清空购物车失败: {err}")
            return False

    CHECKOUT_LABELS = ("check out", "checkout", "continue to checkout")

    async def _cart_warnings(self) -> str:
        """Epic's own reason why the cart cannot be checked out.

        The cart renders its validation in `[data-testid='cart-warnings']`:
        "Purchasing Shrunken Head DLC requires: Sker Ritual" for a DLC whose
        base game is missing, and a region notice for a game the account's
        country cannot buy. Reading that is the answer; waiting and guessing
        is not.
        """
        text = ""
        with suppress(Exception):
            text = await self.page.locator(
                "[data-testid='cart-warnings']"
            ).first.inner_text(timeout=2000)
        return " ".join((text or "").split())

    @staticmethod
    def _promotion_slug(promotion) -> str:
        """The store slug of a promotion: `/p/sker-ritual-aac702` → its tail."""
        path = (getattr(promotion, "url", "") or "").split("?")[0]
        return path.split("/p/", 1)[1].strip("/") if "/p/" in path else ""

    async def _cart_lines(self, page: Page | None = None) -> list[dict]:
        """What the cart actually holds, one row per line item.

        Each line is an `offer-card-layout-wrapper` whose first `/p/` link
        names the product. Reading that is the only honest answer to "is the
        game I want in the cart": the product page shows `View in Cart` for a
        cart that holds something else entirely — run 40724013740 saw it on
        `Sker Ritual` while the cart held `Shrunken Head DLC`, so the game was
        never added and the checkout stayed disabled by a missing
        prerequisite.
        """
        page = page or self.page
        lines = []
        with suppress(Exception):
            lines = await page.evaluate(
                """() => Array.from(
                        document.querySelectorAll(
                            "[data-testid='offer-card-layout-wrapper']"
                        )
                    ).map((card, index) => {
                        const link = card.querySelector("a[href*='/p/']");
                        const href = link ? link.getAttribute('href') : '';
                        const slug = href
                            ? href.split('/p/')[1].split(/[?#]/)[0]
                            : '';
                        return {
                            index,
                            slug,
                            text: (card.innerText || '')
                                .replace(/\\s+/g, ' ').trim().slice(0, 160),
                        };
                    })"""
            )
        return lines or []

    async def _cart_holds(self, promotion, page: Page | None = None) -> bool:
        """Whether this promotion is one of the cart's own line items."""
        slug = self._promotion_slug(promotion)
        if not slug:
            return False
        return any(
            slug in (line.get("slug") or "")
            for line in await self._cart_lines(page)
        )

    async def _remove_cart_line(
        self, slug: str, page: Page | None = None, index: int = 0
    ) -> bool:
        page = page or self.page
        clicked = False
        with suppress(Exception):
            clicked = await page.evaluate(
                """([slug, index]) => {
                    const cards = Array.from(document.querySelectorAll(
                        "[data-testid='offer-card-layout-wrapper']"
                    ));
                    const card = slug
                        ? cards.find(node => {
                            const link = node.querySelector("a[href*='/p/']");
                            const href = link ? link.getAttribute('href') : '';
                            return href.includes('/p/' + slug);
                          })
                        : cards[index];
                    if (!card) return false;
                    const label = node => (node.innerText || '')
                        .replace(/\\s+/g, ' ').trim().toLowerCase();
                    const buttons = Array.from(card.querySelectorAll('button'));
                    // A paid line goes to the wishlist rather than the bin;
                    // a free leftover has nothing worth keeping.
                    const button = buttons.find(node => label(node) === 'move to wishlist')
                        || buttons.find(node => label(node) === 'remove');
                    if (!button) return false;
                    button.click();
                    return true;
                }""",
                [slug, index],
            )
        return bool(clicked)

    async def _clear_cart(self, page: Page | None = None) -> bool:
        """Empty the cart of everything, free lines included.

        `_empty_cart` only moves *paid* lines to the wishlist, which is how a
        free `Shrunken Head DLC` outlived every run that tried to check out:
        Epic refuses a DLC whose base game is missing, so the cart blocked
        itself permanently. One line leaves per pass, because the list is
        re-read after every removal and the positions shift with it.
        """
        page = page or self.page
        await page.goto(URL_CART, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        for _ in range(12):
            lines = await self._cart_lines(page)
            if not lines:
                return True
            line = lines[0]
            if not await self._remove_cart_line(
                line.get("slug") or "", page, line.get("index") or 0
            ):
                logger.warning(
                    "清空购物车失败: "
                    + "; ".join((item.get("text") or "")[:60] for item in lines)
                )
                return False
            logger.info(f"🛒 移除购物车残留: {(line.get('text') or '')[:60]}")
            await page.wait_for_timeout(1500)
        return not await self._cart_lines(page)

    async def _keep_only_cart(self, page: Page, promotions) -> bool:
        """Leave only the games this run means to check out.

        `_empty_cart` moves *paid* lines to the wishlist and keeps the free
        ones, which is how a free `Shrunken Head DLC` outlived every run that
        followed it: Epic refuses a DLC whose base game is missing, so the
        cart blocked itself for good and `Sker Ritual` was never added again.
        Everything that is not one of this week's promotions leaves the cart,
        free or paid.
        """
        wanted = {self._promotion_slug(promotion) for promotion in promotions}
        wanted.discard("")
        for _ in range(12):
            lines = await self._cart_lines(page)
            stale = [
                line for line in lines
                if (line.get("slug") or "") not in wanted
            ]
            if not stale:
                return True
            line = stale[0]
            if not await self._remove_cart_line(
                line.get("slug") or "", page, line.get("index") or 0
            ):
                logger.warning(
                    "移出购物车残留失败: "
                    + "; ".join((item.get("text") or "")[:60] for item in lines)
                )
                return False
            logger.info(f"🛒 移出购物车残留: {(line.get('text') or '')[:60]}")
            await page.wait_for_timeout(1500)
        return True

    async def _cart_item_count(self) -> int:
        count = 0
        with suppress(Exception):
            count = await self.page.locator(
                "//div[@data-testid='offer-card-layout-wrapper']"
            ).count()
        return count

    async def _click_checkout(self) -> None:
        """Press the cart's checkout control whatever the markup is now.

        Run 40696732890 waited thirty seconds for
        `//button//span[text()='Check Out']` and run 40698238507 found the
        cart holding the game (`items=1`) with `'Check Out'` among its
        controls, so the control was there and the selector was what failed.

        The label is looked up in the page instead of in XPath, because
        `normalize-space()` does not treat a non-breaking space as
        whitespace while the label is built from one, and because the label
        no longer sits in a span of a button. The click itself stays a real
        one, so the page receives the events it expects.

        When nothing matches, the cart's own controls are reported, so the
        next run names the right one instead of guessing again.
        """
        await self.page.wait_for_timeout(1500)
        handle = None
        with suppress(Exception):
            handle = await self.page.evaluate_handle(
                """(labels) => {
                    const norm = value => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const nodes = Array.from(
                        document.querySelectorAll('button, a, [role=button]')
                    );
                    for (const label of labels) {
                        for (const node of nodes) {
                            if (!node.offsetParent) continue;
                            const text = norm(node.innerText);
                            if (!text) continue;
                            if (text === label || text.startsWith(label)) return node;
                        }
                    }
                    return null;
                }""",
                list(self.CHECKOUT_LABELS),
            )
        element = handle.as_element() if handle is not None else None
        if element is not None:
            # Epic disables the cart's checkout when its own validation
            # refuses the cart, and a click on a disabled control only times
            # out: run 40718120106 logged `text='Check Out', ..., disabled=True`
            # and then ten seconds of "element is not enabled". So the cart's
            # verdict is read while the control settles, and the control is
            # only ever clicked once Epic has enabled it.
            warnings = ""
            for _ in range(20):
                if not await element.is_disabled():
                    break
                warnings = await self._cart_warnings()
                if warnings:
                    raise CartNotPurchasable(warnings)
                await self.page.wait_for_timeout(500)
            else:
                raise CartNotPurchasable(
                    warnings
                    or "the cart's checkout control stayed disabled "
                    f"(items={await self._cart_item_count()})"
                )
            # A click that lands but leaves the cart where it was is the
            # failure run 40716550293 hit: the cart page still had `Check Out`
            # in its markup after the click, and the run then looked for the
            # order form on the cart itself. So each click is checked by
            # whether the page left the cart, and a click that does nothing is
            # repeated with more force.
            for label, click in (
                ("a plain click", lambda: element.click(timeout=10000)),
                ("a forced click", lambda: element.click(timeout=5000, force=True)),
                ("a DOM click", lambda: element.evaluate("node => node.click()")),
            ):
                try:
                    await click()
                except Exception as err:
                    logger.warning(f"Checkout control refused {label}: {err}")
                    continue
                await self.page.wait_for_timeout(2500)
                if not self.page.url.rstrip("/").endswith("/cart"):
                    logger.debug(f"The cart handed over after {label}")
                    return
                # The checkout may also open inside the cart instead of
                # navigating, so the order form itself counts as success.
                with suppress(Exception):
                    opened = await self.page.evaluate(
                        """() => {
                            const wanted = ['place order', 'get', 'add to library',
                                            'buy now', 'confirm', 'confirm order',
                                            'complete order', 'submit order'];
                            return Array.from(document.querySelectorAll('button'))
                                .some(node => {
                                    const text = (node.innerText || '')
                                        .replace(/\\s+/g, ' ').trim().toLowerCase();
                                    return text && wanted.some(w => text === w || text.startsWith(w));
                                });
                        }"""
                    )
                    if opened:
                        logger.debug(f"The order form opened after {label}")
                        return
                logger.warning(
                    f"The cart did not hand over after {label}; trying again"
                )
        labels: list[str] = []
        with suppress(Exception):
            labels = await self.page.evaluate(
                """() => Array.from(document.querySelectorAll('button, a'))
                    .filter(el => el.offsetParent)
                    .map(el => (el.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 40))
                    .filter(Boolean)
                    .slice(0, 25)"""
            )
        cards = 0
        with suppress(Exception):
            cards = await self.page.locator(
                "//div[@data-testid='offer-card-layout-wrapper']"
            ).count()
        raise TimeoutError(
            "no checkout control on the cart page "
            f"(items={cards}, controls={labels})"
        )

    async def _purchase_free_game(self, attempt: int = 1) -> None:
        """Check the cart out, retrying a bounded number of times.

        The retry used to call itself without a limit, so a checkout that
        kept failing kept reloading: run 40700908238 logged its last line at
        13:57:45 and then nothing for twenty-four minutes, until the task
        timeout ended the run.
        """
        max_attempts = max(1, int(settings.CHECKOUT_MAX_ATTEMPTS or 1))
        await self.page.goto(URL_CART, wait_until="domcontentloaded")
        logger.debug("Move everything that is not this week's games out")
        await self._keep_only_cart(self.page, getattr(self, "_cart_targets", []))

        agent = get_hcaptcha_agent(self.page)
        await self._click_checkout()
        await self._agree_license(self.page)

        try:
            logger.debug("Move to webPurchaseContainer iframe")
            wpc, payment_btn = await self._active_purchase_container(self.page)
            logger.debug("Click payment button")
            await self._uk_confirm_order(wpc)
            await agent.wait_for_challenge()
        except Exception as err:
            logger.warning(f"验证码解决失败 [{attempt}/{max_attempts}]: {err}")
            if attempt >= max_attempts:
                raise
            await self.page.reload()
            return await self._purchase_free_game(attempt + 1)

    @retry(retry=retry_if_exception_type(TimeoutError), stop=stop_after_attempt(2), reraise=True)
    async def collect_weekly_games(self, promotions: List[PromotionGame]):
        # The games this run means to buy. `_purchase_free_game` keeps these
        # and nothing else, so a leftover from an earlier attempt cannot make
        # Epic refuse the checkout — which is exactly how a free
        # `Shrunken Head DLC` blocked every run that came after it.
        self._cart_targets = promotions
        has_cart_items = await self.add_promotion_to_cart(self.page, promotions)

        if has_cart_items:
            try:
                await self._purchase_free_game()
            except Exception as err:
                # The cart could not be checked out. A game the account's
                # region cannot buy leaves exactly that trace: the cart holds
                # it and the checkout control stays disabled. That is not a
                # failure of the run, and the claim script already accepts
                # `region_unavailable` as settled.
                region_page = await self._is_region_unavailable_page(self.page)
                reason = str(err) if isinstance(err, CartNotPurchasable) else ""
                region = region_page or any(
                    marker in reason.lower() for marker in REGION_UNAVAILABLE_MARKERS
                )
                if isinstance(err, CartNotPurchasable) or region_page:
                    status = "region_unavailable" if region else "not_purchasable"
                    for promotion in promotions:
                        logger.warning(f"{status.upper()}:{promotion.title}:{reason}")
                        emit_desktop_result(promotion.title, status)
                    await self._save_debug_page(self.page, f"cart_{status}")
                    return
                # A stale entry for a game the account already owns looks the
                # same way, so the order history is asked before the game is
                # reported as lost.
                owned = []
                for promotion in promotions:
                    if await self._owned_from_order_history(self.page, promotion):
                        owned.append(promotion.title)
                        emit_desktop_result(promotion.title, "verified_owned")
                if owned:
                    logger.success(
                        "🎉 订单历史确认已领取: " + ", ".join(owned)
                    )
                    return
                raise
            try:
                await self.page.wait_for_url(URL_CART_SUCCESS)
                logger.success("🎉 购物车游戏领取成功")
            except TimeoutError:
                logger.warning("购物车游戏领取失败")
        else:
            logger.success("🎉 任务完成（已领取或已在库中）")
