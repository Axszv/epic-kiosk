# -*- coding: utf-8 -*-
"""
@Time    : 2025/7/16 22:13
@Author  : QIN2DIM
@GitHub  : https://github.com/QIN2DIM
@Desc    :
"""
import asyncio
import json
import time
from contextlib import suppress
from enum import Enum

from hcaptcha_challenger.agent import AgentV
from loguru import logger
from playwright.async_api import expect, Page, Response

from settings import RUNTIME_DIR, settings
from services.hcaptcha_agent_service import get_hcaptcha_agent

URL_CLAIM = "https://store.epicgames.com/en-US/free-games"

CLOUDFLARE_TITLE_MARKERS = ("just a moment",)
CLOUDFLARE_BODY_MARKERS = (
    "performing security verification",
    "checking your browser before accessing",
)
EPIC_HCAPTCHA_HTML_MARKERS = (
    "h_captcha_challenge_",
    'title="hcaptcha challenge"',
    "hcaptcha.com/captcha/",
)
EPIC_EMAIL_TRANSACTION_ERROR_MARKERS = (
    "incorrect response",
    "please refresh the page",
)
EPIC_DIAGNOSTIC_SECRET_MARKERS = (
    "authorization",
    "captcha",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
)


def is_cloudflare_security_check(title: str, body_text: str, url: str = "") -> bool:
    """Return whether the current page is a blocking Cloudflare interstitial."""
    normalized_title = (title or "").casefold()
    normalized_body = (body_text or "").casefold()
    normalized_url = (url or "").casefold()
    return (
        any(marker in normalized_title for marker in CLOUDFLARE_TITLE_MARKERS)
        or any(marker in normalized_body for marker in CLOUDFLARE_BODY_MARKERS)
        or "/cdn-cgi/challenge-platform/" in normalized_url
    )


def is_epic_hcaptcha_challenge(html: str) -> bool:
    normalized_html = (html or "").casefold()
    return any(marker in normalized_html for marker in EPIC_HCAPTCHA_HTML_MARKERS)


def is_epic_email_transaction_failure(status: int, body_text: str = "") -> bool:
    """Return whether Epic rejected the email/hCaptcha transaction."""
    normalized_body = (body_text or "").casefold()
    return status in {400, 409} or any(
        marker in normalized_body for marker in EPIC_EMAIL_TRANSACTION_ERROR_MARKERS
    )


def is_recoverable_epic_captcha_error(error_code: str) -> bool:
    """Return whether Epic asks the browser to restart its captcha transaction."""
    normalized = (error_code or "").casefold()
    return "captcha_invalid" in normalized or "csrf_token_invalid" in normalized


def sanitize_epic_response_payload(value, *, max_string_length: int = 1000):
    """Redact credentials and captcha material before writing API diagnostics."""
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            normalized_key = str(key).casefold()
            if any(marker in normalized_key for marker in EPIC_DIAGNOSTIC_SECRET_MARKERS):
                sanitized[key] = "***"
            else:
                sanitized[key] = sanitize_epic_response_payload(
                    item, max_string_length=max_string_length
                )
        return sanitized
    if isinstance(value, list):
        return [
            sanitize_epic_response_payload(item, max_string_length=max_string_length)
            for item in value
        ]
    if isinstance(value, str) and len(value) > max_string_length:
        return f"{value[:max_string_length]}...<truncated>"
    return value


class ErrorType(Enum):
    """
    错误类型枚举，用于精细化区分不同错误，便于前端展示不同提示

    设计思路：
    - 每种错误类型对应不同的用户操作建议
    - 前端根据错误类型展示不同的弹窗内容
    - 便于日志分析和问题排查
    """
    # 成功，无错误
    SUCCESS = "success"

    # 账号或密码错误 - 需要用户检查密码重新提交
    INVALID_CREDENTIALS = "invalid_credentials"

    # 账号被锁定 - 需要用户联系 Epic 客服
    ACCOUNT_LOCKED = "account_locked"

    # EULA 协议处理失败 - 需要用户手动登录 Epic 接受协议
    EULA_FAILED = "eula_failed"

    # 验证码识别失败/超时 - 建议用户稍后重试
    CAPTCHA_FAILED = "captcha_failed"

    # 登录超时 - 可能是网络问题，建议稍后重试
    LOGIN_TIMEOUT = "login_timeout"

    # 网络超时 - Epic 服务不可达
    NETWORK_TIMEOUT = "network_timeout"

    # Cookie 无效 - 需要重新登录
    COOKIE_INVALID = "cookie_invalid"

    # Cloudflare interstitial blocks the Epic login form. Preserve the browser
    # profile because its cookies can help the managed challenge trust the next
    # navigation.
    CLOUDFLARE_BLOCKED = "cloudflare_blocked"

    # 未知错误 - 需要用户查看日志
    UNKNOWN = "unknown"


class LoginFailedException(Exception):
    """
    登录失败异常

    携带错误类型信息，便于上层调用者判断具体失败原因
    """
    def __init__(self, error_type: ErrorType, message: str = ""):
        self.error_type = error_type
        self.message = message
        super().__init__(message)


class EpicAuthorization:

    def __init__(self, page: Page):
        self.page = page

        self._is_login_success_signal = asyncio.Queue()
        self._is_refresh_csrf_signal = asyncio.Queue()
        self._email_transaction_failure_signal = asyncio.Queue()
        self._refresh_csrf_seen = False
        self._login_error_code = None  # 存储登录错误码

    def _has_refresh_csrf_session(self) -> bool:
        return self._refresh_csrf_seen or not self._is_refresh_csrf_signal.empty()

    async def _has_cloudflare_security_check(self) -> bool:
        title = ""
        body_text = ""
        has_cloudflare_widget = False
        with suppress(Exception):
            title = await self.page.title()
        with suppress(Exception):
            body_text = await self.page.locator("body").inner_text(timeout=3000)
        with suppress(Exception):
            has_cloudflare_widget = await self.page.locator(
                "iframe[src*='challenges.cloudflare.com'], "
                "[class*='cf-turnstile'], input[name='cf-turnstile-response']"
            ).count() > 0
        return has_cloudflare_widget or is_cloudflare_security_check(
            title, body_text, self.page.url
        )

    async def _click_cloudflare_widget(self) -> bool:
        """Best-effort click of a visible Cloudflare managed-challenge widget."""
        selectors = (
            "iframe[src*='challenges.cloudflare.com']:visible",
            "iframe[title*='Cloudflare']:visible",
            ".cf-turnstile:visible iframe",
        )
        for selector in selectors:
            widget = self.page.locator(selector).first
            with suppress(Exception):
                if await widget.count() == 0 or not await widget.is_visible():
                    continue

                # Prefer the actual checkbox when the challenge frame exposes it.
                with suppress(Exception):
                    checkbox = self.page.frame_locator(selector).locator(
                        "input[type='checkbox']"
                    ).first
                    await checkbox.click(timeout=3000)
                    logger.info("Clicked the Cloudflare verification checkbox")
                    return True

                # Some Turnstile versions hide the checkbox behind a closed
                # component tree. The left side of the visible widget is still
                # the checkbox/label hit target.
                box = await widget.bounding_box(timeout=3000)
                if box:
                    x = box["x"] + min(28, box["width"] / 4)
                    y = box["y"] + box["height"] / 2
                    await self.page.mouse.move(x, y, steps=12)
                    await self.page.wait_for_timeout(300)
                    await self.page.mouse.click(x, y)
                    logger.info("Clicked the visible Cloudflare verification widget")
                    return True
        return False

    async def _wait_for_cloudflare_auto_pass(self, timeout_seconds: int = 45) -> bool:
        if not await self._has_cloudflare_security_check():
            return True

        logger.warning("Cloudflare security check is visible, waiting for verification")
        deadline = time.monotonic() + timeout_seconds
        next_click_at = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_click_at:
                await self._click_cloudflare_widget()
                next_click_at = now + 12
            await self.page.wait_for_timeout(3000)
            if not await self._has_cloudflare_security_check():
                logger.success("Cloudflare security check passed")
                return True

        await self._save_page_debug("auth_cloudflare_check")
        logger.error("Cloudflare security check is still blocking the login page")
        return False

    async def _has_hcaptcha_challenge(self) -> bool:
        selectors = (
            "iframe[title='hCaptcha challenge']:visible",
            "iframe[src*='hcaptcha.com/captcha/']:visible",
            ".h_captcha_challenge:visible iframe",
        )
        for selector in selectors:
            with suppress(Exception):
                if await self.page.locator(selector).count() > 0:
                    return True
        return False

    async def _take_email_transaction_failure(self):
        rejection = None
        while not self._email_transaction_failure_signal.empty():
            rejection = self._email_transaction_failure_signal.get_nowait()
        if rejection:
            return rejection

        body_text = ""
        with suppress(Exception):
            body_text = await self.page.locator("body").inner_text(timeout=1000)
        if is_epic_email_transaction_failure(200, body_text):
            return {"status": 200, "body": body_text[:1000]}
        return None

    async def _solve_pre_password_hcaptcha(self, agent: AgentV) -> bool:
        # Epic can insert an email_exists_prod hCaptcha between the email and
        # password steps. Wait briefly for either state before proceeding.
        for _ in range(20):
            if await self._has_hcaptcha_challenge():
                break
            with suppress(Exception):
                if await self.page.locator("#password").is_visible(timeout=250):
                    return True
            await self.page.wait_for_timeout(500)
        else:
            return True

        for attempt in range(1, 4):
            logger.info(f"处理邮箱步骤 hCaptcha [{attempt}/3]")
            try:
                await asyncio.wait_for(
                    agent.wait_for_challenge(),
                    timeout=max(settings.EXECUTION_TIMEOUT, 120) + 30,
                )
            except Exception as err:
                logger.warning(f"邮箱步骤 hCaptcha 处理异常 [{attempt}/3]: {err}")

            await self.page.wait_for_timeout(2000)
            if not await self._has_hcaptcha_challenge():
                logger.success("✅ 邮箱步骤 hCaptcha 已通过")
                return True

        await self._save_page_debug("login_email_hcaptcha_failed")
        logger.error("❌ 邮箱步骤 hCaptcha 未通过")
        return False

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

    async def _on_response_anything(self, r: Response):
        if r.request.method != "POST" or "talon" in r.url:
            return

        if "/purchase/confirm-order" in r.url:
            body_text = ""
            response_payload = None
            with suppress(Exception):
                body_text = await r.text()
            with suppress(Exception):
                response_payload = json.loads(body_text)
            if response_payload is None:
                response_payload = {"body": body_text}
            if r.status >= 400:
                safe_payload = sanitize_epic_response_payload(response_payload)
                logger.warning(
                    "Epic confirm-order response: "
                    f"HTTP {r.status} | "
                    f"{json.dumps(safe_payload, ensure_ascii=False)[:4000]}"
                )
            else:
                order_response = response_payload.get("orderResponse", {})
                order_status = order_response.get("orderStatus") or order_response.get(
                    "status", ""
                )
                logger.debug(
                    "Epic confirm-order response: "
                    f"HTTP {r.status} | orderStatus={order_status}"
                )
            return

        if "/id/api/email/exists" in r.url:
            body_text = ""
            with suppress(Exception):
                body_text = await r.text()
            logger.debug(
                f"📡 邮箱校验 API 响应: {r.url} | 状态码: {r.status} | "
                f"响应: {body_text[:500]}"
            )
            if is_epic_email_transaction_failure(r.status, body_text):
                logger.warning(
                    f"Epic 拒绝邮箱/hCaptcha 事务，准备在当前浏览器中恢复: HTTP {r.status}"
                )
                self._email_transaction_failure_signal.put_nowait(
                    {"status": r.status, "body": body_text[:1000]}
                )
            return

        with suppress(Exception):
            result = await r.json()

            # 记录所有 POST 响应的 URL，便于调试
            logger.debug(f"📡 API 响应: {r.url} | 状态码: {r.status}")

            if "/id/api/login" in r.url:
                # 记录完整的登录 API 响应
                logger.debug(f"🔍 登录 API 完整响应: {json.dumps(result, ensure_ascii=False, indent=2)}")
                if result.get("errorCode"):
                    # 记录错误码并通知登录失败
                    self._login_error_code = result.get("errorCode")
                    error_msg = result.get("errorMessage", "未知错误")
                    if is_recoverable_epic_captcha_error(self._login_error_code):
                        logger.warning(
                            "登录 API 拒绝 captcha/CSRF 事务，准备刷新并继续本次登录: "
                            f"{self._login_error_code}"
                        )
                        self._email_transaction_failure_signal.put_nowait(
                            {
                                "status": r.status,
                                "body": json.dumps(result, ensure_ascii=False)[:1000],
                                "source": "login",
                            }
                        )
                        return
                    # 记录完整的错误信息
                    logger.error(f"❌ 登录失败: errorCode={self._login_error_code}, message={error_msg}")
                    logger.error(f"❌ 完整错误响应: {json.dumps(result, ensure_ascii=False)}")
                    # 放入失败信号，中断等待
                    self._is_login_success_signal.put_nowait({"error": True, "code": self._login_error_code, "full_response": result})
                else:
                    # 登录成功，记录 accountId
                    if result.get("accountId"):
                        logger.success(f"✅ 登录 API 返回成功: accountId={result.get('accountId')}")
                        self._is_login_success_signal.put_nowait(result)
            elif "/id/api/analytics" in r.url and result.get("accountId"):
                self._is_login_success_signal.put_nowait(result)
            elif "/account/v2/refresh-csrf" in r.url and result.get("success", False) is True:
                logger.success("✅ refresh-csrf 返回成功，账号会话已建立")
                self._refresh_csrf_seen = True
                self._is_refresh_csrf_signal.put_nowait(result)
                self._is_login_success_signal.put_nowait({"csrf": True})

    async def _handle_right_account_validation(self, agent: AgentV):
        """
        以下验证仅会在登录成功后出现
        Returns:

        """
        try:
            await self.page.goto(
                "https://www.epicgames.com/account/personal",
                wait_until="domcontentloaded",
                timeout=45000,
            )
        except Exception as err:
            if self._has_refresh_csrf_session():
                logger.warning(f"账号验证页导航超时，但 refresh-csrf 已成功，继续领取流程: {err}")
                return
            await self._save_page_debug("account_validation_nav_timeout")
            raise
        await self.page.wait_for_timeout(2000)

        btn_ids = ["#link-success", "#login-reminder-prompt-setup-tfa-skip", "#yes"]

        # == 账号长期不登录需要做的额外验证 == #

        with suppress(Exception):
            await asyncio.wait_for(
                agent.wait_for_challenge(),
                timeout=min(settings.EXECUTION_TIMEOUT, 90),
            )
        await self.page.wait_for_timeout(500)

        while not self._has_refresh_csrf_session() and btn_ids:
            await self.page.wait_for_timeout(500)
            action_chains = btn_ids.copy()
            for action in action_chains:
                with suppress(Exception):
                    reminder_btn = self.page.locator(action)
                    await expect(reminder_btn).to_be_visible(timeout=1000)
                    await reminder_btn.click(timeout=1000)
                    btn_ids.remove(action)

        if not self._has_refresh_csrf_session():
            await self._save_page_debug("account_validation_timeout")
            logger.warning("⚠️ 账号验证阶段未观察到 refresh-csrf，继续依赖已有会话状态")

    async def _login(self) -> tuple[bool, ErrorType] | None:
        """
        执行登录流程

        Returns:
            tuple[bool, ErrorType]: (是否成功, 错误类型)
            - (True, ErrorType.SUCCESS): 登录成功
            - (False, ErrorType.INVALID_CREDENTIALS): 账号或密码错误
            - (False, ErrorType.ACCOUNT_LOCKED): 账号被锁定
            - (False, ErrorType.CAPTCHA_FAILED): 验证码识别失败
            - (False, ErrorType.LOGIN_TIMEOUT): 登录超时
            - None: 异常情况
        """
        # 重置错误码
        self._login_error_code = None

        # 尽可能早地初始化机器人
        agent = get_hcaptcha_agent(self.page)

        # {{< SIGN IN PAGE >}}
        logger.debug("Login with Email")

        # 用于记录验证码处理是否成功
        captcha_success = False
        captcha_in_progress = False
        captcha_task = None
        keepalive_task = None

        try:
            point_url = "https://www.epicgames.com/id/login?lang=en-US&noHostRedirect=true"
            await self.page.goto(point_url, wait_until="domcontentloaded")
            await self.page.wait_for_timeout(1500)
            if self._has_refresh_csrf_session():
                logger.success("✅ 检测到已有 refresh-csrf 会话，跳过登录表单")
                return (True, ErrorType.SUCCESS)
            if not await self._wait_for_cloudflare_auto_pass():
                return (False, ErrorType.CLOUDFLARE_BLOCKED)

            # 1. 使用电子邮件地址登录
            email_input = self.page.locator("#email")
            try:
                await email_input.wait_for(state="visible", timeout=60000)
            except Exception:
                if await self._has_cloudflare_security_check():
                    await self._save_page_debug("login_cloudflare_check")
                    return (False, ErrorType.CLOUDFLARE_BLOCKED)
                await self._save_page_debug("login_email_wait_timeout")
                raise
            for _ in range(30):
                if self._has_refresh_csrf_session():
                    logger.success("✅ 邮箱输入框等待期间会话已建立，跳过登录表单")
                    return (True, ErrorType.SUCCESS)
                readonly = False
                with suppress(Exception):
                    readonly = bool(await email_input.get_attribute("readonly"))
                if not readonly:
                    break
                await self.page.wait_for_timeout(500)

            if readonly and self._has_refresh_csrf_session():
                logger.success("✅ 邮箱输入框仍为 readonly，但 refresh-csrf 已成功，跳过登录表单")
                return (True, ErrorType.SUCCESS)

            if readonly:
                await self._save_page_debug("login_email_readonly")
                logger.warning("⚠️ 邮箱输入框持续 readonly，且未确认 refresh-csrf 会话")
                return (False, ErrorType.CAPTCHA_FAILED)

            if self._has_refresh_csrf_session():
                logger.success("✅ 会话已建立，跳过登录表单")
                return (True, ErrorType.SUCCESS)

            for email_attempt in range(1, 5):
                await email_input.clear()
                await email_input.type(settings.EPIC_EMAIL)

                # 2. 点击继续按钮
                try:
                    await self.page.click("#continue", timeout=10000)
                except Exception:
                    if not await self._has_hcaptcha_challenge():
                        raise
                    logger.info("邮箱继续按钮触发 hCaptcha，等待解题器完成")

                if not await self._solve_pre_password_hcaptcha(agent):
                    return (False, ErrorType.CAPTCHA_FAILED)

                await self.page.wait_for_timeout(1500)
                rejection = await self._take_email_transaction_failure()
                if not rejection:
                    break
                if email_attempt == 4:
                    await self._save_page_debug("email_transaction_rejected_final")
                    logger.error("❌ Epic 连续拒绝邮箱/hCaptcha 事务")
                    return (False, ErrorType.CAPTCHA_FAILED)

                logger.warning(
                    f"邮箱/hCaptcha 事务已失效，刷新后重试 [{email_attempt}/3]"
                )
                await self._save_page_debug(
                    f"email_transaction_rejected_pre_password_{email_attempt}"
                )
                await self.page.goto(point_url, wait_until="domcontentloaded")
                await self.page.wait_for_timeout(1500)
                email_input = self.page.locator("#email")
                await email_input.wait_for(state="visible", timeout=60000)

            # 3. 输入密码
            password_input = self.page.locator("#password")
            await password_input.wait_for(state="visible", timeout=60000)
            await password_input.clear()
            await password_input.type(settings.EPIC_PASSWORD.get_secret_value())

            # 并行启动：验证码处理 + 登录结果等待
            # 关键改进：使用 wait_for 快速检测密码错误
            async def wait_for_login_result():
                """等待登录结果（成功或失败）"""
                return await self._is_login_success_signal.get()

            async def retrigger_security_check():
                """Reopen Epic/Talon security checks after a stale challenge."""
                with suppress(Exception):
                    password_box = self.page.locator("#password")
                    if await password_box.is_visible(timeout=1000):
                        await password_box.clear()
                        await password_box.type(settings.EPIC_PASSWORD.get_secret_value())

                for selector in ("#talon_error_container_login_prod button", "#sign-in"):
                    with suppress(Exception):
                        button = self.page.locator(selector).first
                        if await button.is_visible(timeout=1500):
                            logger.debug(f"重新触发安全检查: {selector}")
                            await button.click(timeout=3000)
                            await self.page.wait_for_timeout(3000)
                            return

            async def keep_login_flow_alive():
                """Re-submit the login form if Epic returns to the password step."""
                while not result_task.done():
                    await self.page.wait_for_timeout(8000)
                    if result_task.done():
                        return
                    if captcha_in_progress:
                        continue
                    with suppress(Exception):
                        password_box = self.page.locator("#password")
                        sign_in_button = self.page.locator("#sign-in").first
                        if (
                            await password_box.is_visible(timeout=1000)
                            and await sign_in_button.is_visible(timeout=1000)
                        ):
                            logger.debug("检测到仍停留在密码页，重新提交登录")
                            await password_box.clear()
                            await password_box.type(settings.EPIC_PASSWORD.get_secret_value())
                            await sign_in_button.click(timeout=3000)

            async def handle_captcha():
                """Handle every hCaptcha round until Epic accepts the login."""
                nonlocal captcha_success, captcha_in_progress
                email_transaction_retries = 0
                captcha_attempt = 0

                async def recover_email_transaction(rejection) -> bool:
                    nonlocal email_transaction_retries, captcha_success
                    email_transaction_retries += 1
                    if email_transaction_retries > 3:
                        logger.error("❌ Epic 连续拒绝邮箱/hCaptcha 事务，停止本次登录")
                        if not result_task.done():
                            self._is_login_success_signal.put_nowait(
                                {
                                    "error": True,
                                    "code": "epic_email_captcha_transaction_rejected",
                                    "full_response": rejection,
                                }
                            )
                        return False

                    captcha_success = False
                    logger.warning(
                        "刷新登录页并重试邮箱/hCaptcha 事务 "
                        f"[{email_transaction_retries}/3]"
                    )
                    await self._save_page_debug(
                        f"email_transaction_rejected_{email_transaction_retries}"
                    )
                    await self.page.goto(point_url, wait_until="domcontentloaded")
                    await self.page.wait_for_timeout(1500)
                    if self._has_refresh_csrf_session():
                        self._is_login_success_signal.put_nowait({"csrf": True})
                        return True
                    if not await self._wait_for_cloudflare_auto_pass():
                        self._is_login_success_signal.put_nowait(
                            {"error": True, "code": "cloudflare_blocked"}
                        )
                        return False

                    email_box = self.page.locator("#email")
                    await email_box.wait_for(state="visible", timeout=60000)
                    await email_box.clear()
                    await email_box.type(settings.EPIC_EMAIL)
                    await self.page.click("#continue", timeout=10000)
                    return True

                # Keep this coordinator alive for the full login wait. Epic can
                # reject a solved token after the challenge iframe disappears.
                while not result_task.done():
                    rejection = await self._take_email_transaction_failure()
                    if rejection:
                        if not await recover_email_transaction(rejection):
                            return
                        continue

                    if result_task.done():
                        return

                    challenge_seen = False
                    for _ in range(20):
                        if result_task.done():
                            return
                        rejection = await self._take_email_transaction_failure()
                        if rejection:
                            break
                        if await self._has_hcaptcha_challenge():
                            challenge_seen = True
                            break
                        await self.page.wait_for_timeout(500)
                    if rejection:
                        if not await recover_email_transaction(rejection):
                            return
                        continue
                    if not challenge_seen:
                        continue

                    try:
                        captcha_attempt += 1
                        if captcha_attempt > 12:
                            logger.error("❌ 登录 hCaptcha 处理轮次已达上限")
                            return
                        logger.info(f"处理登录 hCaptcha [{captcha_attempt}/12]")
                        captcha_in_progress = True
                        captcha_round_failed = False
                        await asyncio.wait_for(
                            agent.wait_for_challenge(),
                            timeout=max(settings.EXECUTION_TIMEOUT, 120) + 30,
                        )
                    except Exception as e:
                        captcha_round_failed = True
                        logger.warning(f"登录 hCaptcha 处理异常 [{captcha_attempt}/12]: {e}")
                    finally:
                        captcha_in_progress = False

                    await self.page.wait_for_timeout(2000)
                    rejection = await self._take_email_transaction_failure()
                    if rejection:
                        if not await recover_email_transaction(rejection):
                            return
                        continue
                    if captcha_round_failed:
                        await retrigger_security_check()
                    elif not await self._has_hcaptcha_challenge():
                        captcha_success = True
                    else:
                        logger.warning("hCaptcha 仍在页面上，继续处理下一轮")
                        await retrigger_security_check()
                    await self.page.wait_for_timeout(2000)

            # Start the result and challenge coordinators before the click. Epic
            # may create hCaptcha while the sign-in click is still settling.
            result_task = asyncio.create_task(wait_for_login_result())
            captcha_task = asyncio.create_task(handle_captcha())
            keepalive_task = asyncio.create_task(keep_login_flow_alive())
            with suppress(Exception):
                await self.page.click("#sign-in", timeout=5000)

            # 第一阶段：15秒内快速检测密码错误
            try:
                done, pending = await asyncio.wait(
                    [result_task],
                    timeout=15,
                    return_when=asyncio.FIRST_COMPLETED
                )

                if result_task in done:
                    result = result_task.result()
                    # 检查是否是登录失败信号
                    if result.get("error"):
                        captcha_task.cancel()
                        error_code = result.get("code", "")
                        if "invalid_account_credentials" in error_code:
                            logger.error("❌ 账号或密码错误")
                            return (False, ErrorType.INVALID_CREDENTIALS)
                        elif "account_locked" in error_code:
                            logger.error("❌ 账号已被锁定")
                            return (False, ErrorType.ACCOUNT_LOCKED)
                        elif "captcha" in error_code or "csrf_token_invalid" in error_code:
                            logger.error(f"❌ 登录验证码事务失败: {error_code}")
                            return (False, ErrorType.CAPTCHA_FAILED)
                        else:
                            logger.error(f"❌ 登录失败: {error_code}")
                            return (False, ErrorType.UNKNOWN)

                    # 登录成功（无验证码或已通过）
                    if result.get("accountId") or result.get("csrf"):
                        captcha_task.cancel()
                        logger.success("✅ 登录成功")
                        await asyncio.wait_for(
                            self._handle_right_account_validation(agent),
                            timeout=settings.LOGIN_RESULT_TIMEOUT_SECONDS,
                        )
                        logger.success("✅ 账号验证成功")
                        return (True, ErrorType.SUCCESS)
            except asyncio.CancelledError:
                pass

            # 第二阶段：继续等待验证码处理后的结果（最多再等 60 秒）
            try:
                result = await asyncio.wait_for(
                    result_task,
                    timeout=settings.LOGIN_RESULT_TIMEOUT_SECONDS,
                )

                if result.get("error"):
                    captcha_task.cancel()
                    error_code = result.get("code", "")
                    if "invalid_account_credentials" in error_code:
                        logger.error("❌ 账号或密码错误")
                        return (False, ErrorType.INVALID_CREDENTIALS)
                    elif "account_locked" in error_code:
                        logger.error("❌ 账号已被锁定")
                        return (False, ErrorType.ACCOUNT_LOCKED)
                    elif "captcha" in error_code or "csrf_token_invalid" in error_code:
                        logger.error(f"❌ 登录验证码事务失败: {error_code}")
                        return (False, ErrorType.CAPTCHA_FAILED)
                    else:
                        logger.error(f"❌ 登录失败: {error_code}")
                        return (False, ErrorType.UNKNOWN)

                captcha_task.cancel()
                logger.success("✅ 登录成功")
                await asyncio.wait_for(
                    self._handle_right_account_validation(agent),
                    timeout=settings.LOGIN_RESULT_TIMEOUT_SECONDS,
                )
                logger.success("✅ 账号验证成功")
                return (True, ErrorType.SUCCESS)

            except asyncio.TimeoutError:
                # 判断是验证码问题还是网络问题
                await self._save_page_debug("login_result_timeout")
                if not captcha_success:
                    logger.error("❌ 验证码识别超时")
                    return (False, ErrorType.CAPTCHA_FAILED)
                logger.error("❌ 登录超时")
                return (False, ErrorType.LOGIN_TIMEOUT)

        except asyncio.TimeoutError:
            await self._save_page_debug("login_outer_timeout")
            if await self._has_cloudflare_security_check():
                logger.error("❌ 登录被 Cloudflare 安全检查阻塞")
                return (False, ErrorType.CLOUDFLARE_BLOCKED)
            if await self._has_hcaptcha_challenge():
                logger.error("❌ 登录被 hCaptcha 安全检查阻塞")
                return (False, ErrorType.CAPTCHA_FAILED)
            logger.error("❌ 登录超时，请检查账号密码")
            return (False, ErrorType.LOGIN_TIMEOUT)
        except Exception as err:
            if await self._has_cloudflare_security_check():
                await self._save_page_debug("login_cloudflare_check")
                logger.error(f"❌ 登录被 Cloudflare 安全检查阻塞: {err}")
                return (False, ErrorType.CLOUDFLARE_BLOCKED)
            if await self._has_hcaptcha_challenge():
                await self._save_page_debug("login_hcaptcha_check")
                logger.error(f"❌ 登录被 hCaptcha 安全检查阻塞: {err}")
                return (False, ErrorType.CAPTCHA_FAILED)
            logger.warning(f"登录异常: {err}")
            return (False, ErrorType.UNKNOWN)
        finally:
            # 确保清理任务
            try:
                if captcha_task:
                    captcha_task.cancel()
            except:
                pass
            try:
                if keepalive_task:
                    keepalive_task.cancel()
            except:
                pass

    async def _handle_eula_correction(self) -> tuple[bool, ErrorType]:
        """
        处理 EULA 修正页面

        Epic Games 在某些情况下会将用户重定向到 EULA 修正页面：
        - 新注册账号首次登录
        - Epic 更新服务条款
        - 账号长期未登录
        - 账号在新设备/地区登录

        页面特征（基于实际 HTML）：
        - URL 包含 "correction/eula" 或 "corrective="
        - 接受按钮: <button id="accept" type="submit" aria-label="接受">接受</button>
        - 拒绝按钮: <button id="decline" type="button" aria-label="拒绝">拒绝</button>
        - 使用 Material UI 组件 (MuiButton-containedPrimary)

        Returns:
            tuple[bool, ErrorType]: (是否成功, 错误类型)
            - (True, SUCCESS): 成功接受 EULA
            - (False, EULA_FAILED): 处理失败，需要用户手动操作
            - (False, SUCCESS): 无需处理（不在 EULA 页面）
        """
        current_url = self.page.url

        # 检测是否在 EULA 修正页面
        if "correction/eula" not in current_url and "corrective=" not in current_url:
            return (False, ErrorType.SUCCESS)  # 无需处理

        logger.warning("⚠️ 检测到 EULA 修正页面，尝试自动接受协议...")
        logger.info(f"📋 当前 URL: {current_url}")

        try:
            # ============================================================
            # SPA 页面需要等待网络完全空闲
            # Material UI 对话框需要额外时间渲染和动画完成
            # ============================================================
            logger.debug("⏳ 等待 EULA 页面加载完成...")
            await self.page.wait_for_load_state("networkidle")

            # 等待 React/Material UI 渲染完成（对话框动画约 225ms）
            await self.page.wait_for_timeout(2000)

            # 等待对话框元素出现（确认页面已渲染）
            try:
                await self.page.wait_for_selector("#accept", timeout=10000)
                logger.debug("✅ EULA 接受按钮已渲染")
            except Exception as e:
                logger.warning(f"⚠️ 等待按钮超时: {e}")

            # ============================================================
            # EULA 接受按钮选择器（按优先级排序）
            # 基于实际 HTML 结构: <button id="accept" type="submit" aria-label="接受">
            # ============================================================
            accept_selectors = [
                # === 最精确：通过 ID 选择（最稳定）===
                "#accept",
                "button#accept",

                # === 通过 aria-label 属性（多语言支持）===
                "//button[@aria-label='接受']",
                "//button[@aria-label='Accept']",

                # === 通过 type=submit（次优）===
                "//button[@type='submit']",

                # === 通过文本匹配（多语言）===
                "//button[normalize-space(text())='接受']",
                "//button[normalize-space(text())='Accept']",

                # === 通过 Material UI class（备用）===
                "//button[contains(@class, 'MuiButton-containedPrimary')]",
            ]

            # 尝试点击接受按钮
            for i, selector in enumerate(accept_selectors, 1):
                try:
                    logger.debug(f"🔍 尝试 EULA 选择器 [{i}/{len(accept_selectors)}]: {selector}")

                    btn = self.page.locator(selector).first

                    # 检查按钮是否存在且可见
                    if not await btn.is_visible(timeout=3000):
                        logger.debug(f"按钮不可见: {selector}")
                        continue

                    btn_text = await btn.text_content()
                    logger.info(f"📋 找到 EULA 接受按钮: '{btn_text}' | 选择器: {selector}")

                    # ============================================================
                    # 🔥 关键修复：使用多种点击方式确保成功
                    # 某些情况下 Playwright 的普通点击会被拦截
                    # ============================================================

                    # 方式1：滚动到按钮位置，确保可见
                    await btn.scroll_into_view_if_needed()
                    await self.page.wait_for_timeout(500)

                    # 方式2：使用 force=True 绕过可操作性检查
                    try:
                        await btn.click(force=True, timeout=5000)
                        logger.info("👆 已点击接受按钮 (force=True)")
                    except Exception as click_err:
                        logger.warning(f"普通点击失败，尝试 JS 点击: {click_err}")
                        # 方式3：使用 JavaScript 直接点击
                        await btn.evaluate("el => el.click()")
                        logger.info("👆 已点击接受按钮 (JS evaluate)")

                    # 等待页面跳转（增加超时时间到 30 秒）
                    logger.info("⏳ 等待页面跳转...")
                    await self.page.wait_for_load_state("networkidle", timeout=30000)

                    # 额外等待，确保重定向完成
                    await self.page.wait_for_timeout(2000)

                    # 验证是否成功跳转
                    new_url = self.page.url
                    logger.debug(f"📋 点击后 URL: {new_url}")

                    if "correction/eula" not in new_url and "corrective=" not in new_url:
                        logger.success("✅ EULA 协议已接受，页面已跳转")
                        return (True, ErrorType.SUCCESS)
                    else:
                        logger.warning("⚠️ 点击后仍在 EULA 页面，尝试下一个选择器")

                except Exception as e:
                    logger.debug(f"EULA 选择器 '{selector}' 失败: {e}")
                    continue

            # ============================================================
            # 所有选择器都失败，记录详细的页面信息便于调试
            # ============================================================
            logger.error("❌ 未能找到 EULA 接受按钮")
            try:
                # 截图保存，便于分析
                screenshot_path = f"/tmp/eula_error_{int(time.time())}.png"
                await self.page.screenshot(path=screenshot_path)
                logger.info(f"📸 EULA 页面截图已保存: {screenshot_path}")

                # 打印页面 HTML，便于调试
                page_content = await self.page.content()
                logger.debug(f"📄 EULA 页面 HTML (前 2000 字符):\n{page_content[:2000]}")
            except Exception as e:
                logger.warning(f"保存调试信息失败: {e}")

            return (False, ErrorType.EULA_FAILED)

        except Exception as e:
            logger.error(f"❌ 处理 EULA 页面异常: {e}")
            return (False, ErrorType.EULA_FAILED)

    async def invoke(self) -> ErrorType:
        """
        执行 Epic 登录认证流程

        流程：
        1. 访问 Epic 免费游戏页面
        2. 检测并处理 EULA 修正页面
        3. 检查登录状态
        4. 如果未登录，执行登录流程
        5. 处理登录后的验证

        Returns:
            ErrorType: 错误类型
            - SUCCESS: 登录成功或已登录
            - 其他错误类型: 对应的失败原因
        """
        self.page.on("response", self._on_response_anything)

        for attempt in range(3):
            logger.info(f"🔄 登录尝试 [{attempt + 1}/3]")

            try:
                await self.page.goto(URL_CLAIM, wait_until="domcontentloaded")
            except Exception as e:
                logger.warning(f"页面加载失败: {e}")
                if "timeout" in str(e).lower():
                    return ErrorType.NETWORK_TIMEOUT
                continue

            # ============================================================
            # 🔥 关键修复：等待页面稳定
            # Epic Games 页面是 SPA，JS 需要时间执行
            # domcontentloaded 触发时重定向可能还没完成
            # ============================================================
            await self.page.wait_for_timeout(3000)  # 等待 3 秒让 JS 执行完成

            # ============================================================
            # 🔥 EULA 修正页面检测与处理
            # 登录后可能被重定向到 EULA 页面，需要自动接受协议
            # ============================================================
            for eula_attempt in range(3):  # 最多处理 3 次 EULA（通常只需要 1 次）
                current_url = self.page.url
                logger.debug(f"📍 当前页面 URL: {current_url}")
                if "correction/eula" in current_url or "corrective=" in current_url:
                    logger.warning(f"⚠️ 检测到修正页面 (EULA 尝试 {eula_attempt + 1}/3): {current_url}")

                    success, error_type = await self._handle_eula_correction()

                    if success:
                        # EULA 处理成功后，重新导航到目标页面
                        await self.page.goto(URL_CLAIM, wait_until="domcontentloaded")
                        await self.page.wait_for_timeout(2000)  # 再次等待稳定
                    else:
                        logger.error(f"❌ EULA 处理失败: {error_type.value}")
                        return error_type  # 返回具体错误类型
                else:
                    break

            # 检查登录状态（增加超时处理）
            try:
                if not await self._wait_for_cloudflare_auto_pass():
                    if attempt < 2:
                        delay_seconds = 10 * (attempt + 1)
                        logger.warning(
                            "Cloudflare check is still active; retrying navigation "
                            f"in {delay_seconds}s without clearing the browser profile"
                        )
                        await self.page.wait_for_timeout(delay_seconds * 1000)
                        continue
                    return ErrorType.CLOUDFLARE_BLOCKED
                status = await self.page.locator("//egs-navigation").get_attribute("isloggedin", timeout=45000)
            except Exception as e:
                # 超时时检查是否在修正页面
                current_url = self.page.url
                logger.debug(f"📍 获取登录状态超时，当前 URL: {current_url}")
                await self._save_page_debug("auth_nav_timeout")
                if "correction" in current_url or "eula" in current_url:
                    logger.error("❌ 仍在修正页面，无法继续")
                    return ErrorType.EULA_FAILED
                logger.warning(f"⚠️ 未找到 egs-navigation，按未登录继续尝试登录: {e}")
                status = "false"

            if status == "true":
                logger.success("✅ Epic Games 已登录")
                return ErrorType.SUCCESS

            # 执行登录
            login_result = await self._login()
            if login_result:
                success, error_type = login_result
                if success:
                    return ErrorType.SUCCESS
                if error_type == ErrorType.CLOUDFLARE_BLOCKED and attempt < 2:
                    delay_seconds = 10 * (attempt + 1)
                    logger.warning(
                        "Cloudflare blocked the login form; retrying navigation "
                        f"in {delay_seconds}s without clearing the browser profile"
                    )
                    await self.page.wait_for_timeout(delay_seconds * 1000)
                    continue
                # 登录失败，返回具体错误类型
                return error_type

            # login_result 为 None 时继续下一次尝试
            logger.warning("⚠️ 登录结果为空，尝试下一次...")
            continue

        # 所有尝试都失败
        logger.error("❌ 所有登录尝试都失败")
        return ErrorType.UNKNOWN
