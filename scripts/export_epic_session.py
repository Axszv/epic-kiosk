"""Export the Epic session of a manual login for the weekly claim.

The claim only needs a captcha when the browser profile has no session, and
that captcha is the one step the model cannot answer reliably. Logging in
once per account in a real browser and carrying those cookies into the run
takes the captcha out of the weekly path.

Usage (from the repository root):

    python scripts/export_epic_session.py --email one@example.com --email two@example.com
    python scripts/export_epic_session.py --accounts-file accounts.json

Each account opens a browser window. Log in there, and the script writes
`app/volumes/runtime/sessions.json`. Then store it as the repository secret:

    gh secret set EPIC_SESSIONS_JSON --repo Axszv/epic-kiosk \
        --body-file app/volumes/runtime/sessions.json

The next run seeds the cookies into the profile of each account and claims
without a captcha. A run only seeds a profile that has no Epic cookies of
its own, so a profile that already signed in keeps its fresher cookies;
`seed_sessions=always` is for the first recovery run.
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE_ROOT = ROOT / "app" / "volumes" / "login"
DEFAULT_OUTPUT = ROOT / "app" / "volumes" / "runtime" / "sessions.json"

STORE_HOME = "https://store.epicgames.com/en-US/free-games"
PORTAL_URL = "https://accounts.epicgames.com/account/transactions?lang=en-US"
CHANNELS = ("msedge", "chrome", "chromium")
SWITCH_MARKERS = ("/id/login/switch-account", "/id/login/switch")
SWITCH_SELECTORS = (
    "button:has-text('Continue')",
    "button:has-text('继续')",
    "[data-testid='switch-account-continue']",
    "#continue",
    "main button",
)


async def read_login_state(page) -> str:
    try:
        state = await page.locator("egs-navigation").get_attribute(
            "isloggedin", timeout=5000
        )
    except Exception:  # noqa: BLE001
        return ""
    return str(state or "").strip().lower()


async def wait_for_login(page, email: str, timeout: int) -> bool:
    print(f"\n=== {email} ===")
    print("请在打开的浏览器里登录这个账号，脚本会自动检测登录状态。")
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        state = await read_login_state(page)
        if state != last:
            print(f"  登录状态: {state or 'unknown'}")
            last = state
        if state == "true":
            return True
        await page.wait_for_timeout(2000)
    return False


async def confirm_account_portal(context) -> str:
    """Open the account portal and report where it lands."""
    page = await context.new_page()
    try:
        await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(3000)
        if any(marker in page.url for marker in SWITCH_MARKERS):
            print("  账号中心要求确认（switch-account），正在点击继续")
            for selector in SWITCH_SELECTORS:
                try:
                    candidate = page.locator(selector).first
                    if await candidate.count() == 0:
                        continue
                    if not await candidate.is_visible(timeout=1500):
                        continue
                    await candidate.click(timeout=5000)
                    await page.wait_for_timeout(3000)
                    break
                except Exception:  # noqa: BLE001
                    continue
        return page.url
    finally:
        await page.close()


def epic_cookies(cookies: list[dict]) -> list[dict]:
    result = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if domain == "epicgames.com" or domain.endswith(".epicgames.com"):
            result.append(
                {
                    key: cookie[key]
                    for key in (
                        "name",
                        "value",
                        "domain",
                        "path",
                        "expires",
                        "httpOnly",
                        "secure",
                        "sameSite",
                    )
                    if cookie.get(key) is not None
                }
            )
    return result


async def export_account(pw, email: str, profile_root: Path, channel: str, timeout: int):
    profile = profile_root / email
    profile.mkdir(parents=True, exist_ok=True)
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(profile),
        channel=channel,
        headless=False,
        viewport={"width": 1280, "height": 920},
    )
    try:
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(STORE_HOME, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        if not await wait_for_login(page, email, timeout):
            print("  超时：没有检测到登录")
            return None

        portal_url = await confirm_account_portal(context)
        print(f"  账号中心: {portal_url}")
        if not portal_url.startswith("https://accounts.epicgames.com/"):
            print("  警告：账号中心会话未建立，领取会报 cookie_invalid")

        cookies = epic_cookies(await context.cookies())
        print(f"  导出 {len(cookies)} 个 cookie")
        return {
            "email": email,
            "cookies": cookies,
            "portal_url": portal_url,
            "exported_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        await context.close()


def read_emails(args) -> list[str]:
    emails: list[str] = []
    for email in args.email or []:
        emails.append(email.strip())
    if args.accounts_file:
        data = json.loads(Path(args.accounts_file).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("accounts", [])
        for item in data:
            if isinstance(item, dict) and item.get("email"):
                emails.append(str(item["email"]).strip())
            elif isinstance(item, str):
                emails.append(item.strip())
    seen = []
    for email in emails:
        if email and email.lower() not in {item.lower() for item in seen}:
            seen.append(email)
    return seen


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", action="append", help="account email, repeatable")
    parser.add_argument("--accounts-file", help="JSON list of accounts, only emails are used")
    parser.add_argument("--profile-root", default=str(DEFAULT_PROFILE_ROOT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--channel", default="", help="msedge, chrome or chromium")
    parser.add_argument("--timeout", type=int, default=600, help="seconds to wait per account")
    args = parser.parse_args()

    emails = read_emails(args)
    if not emails:
        parser.error("no account email: use --email or --accounts-file")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if output.exists():
        try:
            for entry in json.loads(output.read_text(encoding="utf-8")).get("accounts", []):
                existing[str(entry.get("email") or "").lower()] = entry
        except json.JSONDecodeError:
            existing = {}

    profile_root = Path(args.profile_root)
    results = []
    async with async_playwright() as pw:
        channel = args.channel
        for candidate in [channel] if channel else list(CHANNELS):
            try:
                browser = await pw.chromium.launch(channel=candidate, headless=True)
                await browser.close()
                channel = candidate
                break
            except Exception:  # noqa: BLE001
                continue
        if not channel:
            print("找不到可用的浏览器（msedge / chrome / chromium）。", file=sys.stderr)
            return 2
        print(f"使用浏览器: {channel}")

        for email in emails:
            try:
                result = await export_account(pw, email, profile_root, channel, args.timeout)
            except Exception as err:  # noqa: BLE001
                print(f"  {email} 导出失败: {type(err).__name__}: {err}")
                result = None
            if result:
                existing[email.lower()] = result
                results.append(result)

    if not results and not existing:
        print("没有导出任何会话。", file=sys.stderr)
        return 1

    output.write_text(
        json.dumps(
            {
                "version": 1,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "accounts": list(existing.values()),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n已写入 {output}（{len(existing)} 个账号）")
    print("下一步：")
    print(
        "  gh secret set EPIC_SESSIONS_JSON --repo Axszv/epic-kiosk "
        f'--body-file "{output}"'
    )
    print("然后手动跑一次工作流，seed_sessions 选 always。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
