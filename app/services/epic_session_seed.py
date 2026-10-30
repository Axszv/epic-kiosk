"""Seed the browser profile with an Epic session exported from a login.

The weekly claim only needs a captcha when the profile has no session, and
the captcha is the one step of the run that a small model cannot answer
reliably. A profile that already holds a session claims the games in
seconds and keeps that session in the Actions cache, so the session is the
thing worth carrying between runs.

`scripts/export_epic_session.py` writes the sessions of a manual login into
one JSON document, and the `EPIC_SESSIONS_JSON` secret carries it into the
run. The cookies are only written into a profile that has none of its own,
because a profile that is already signed in refreshes its own cookies on
every response and an older copy would replace them.
"""

import json
import time

EPIC_COOKIE_DOMAIN = "epicgames.com"
COOKIE_FIELDS = (
    "name",
    "value",
    "domain",
    "path",
    "expires",
    "httpOnly",
    "secure",
    "sameSite",
)


def _is_epic_cookie(cookie: dict) -> bool:
    domain = str(cookie.get("domain") or "").lstrip(".").lower()
    return domain == EPIC_COOKIE_DOMAIN or domain.endswith("." + EPIC_COOKIE_DOMAIN)


def normalise_cookie(cookie: dict) -> dict | None:
    """Keep the fields `add_cookies` accepts, and drop what has expired."""
    if not isinstance(cookie, dict):
        return None
    name = str(cookie.get("name") or "")
    if not name:
        return None
    domain = str(cookie.get("domain") or "")
    if not _is_epic_cookie({"domain": domain}):
        return None
    expires = cookie.get("expires")
    if isinstance(expires, (int, float)) and expires > 0 and expires < time.time():
        return None
    normalised = {
        key: cookie[key] for key in COOKIE_FIELDS if cookie.get(key) is not None
    }
    normalised["name"] = name
    normalised["value"] = str(cookie.get("value") or "")
    normalised["domain"] = domain
    normalised.setdefault("path", "/")
    same_site = str(normalised.get("sameSite") or "")
    if same_site not in {"Strict", "Lax", "None"}:
        normalised.pop("sameSite", None)
    return normalised


def load_sessions(raw: str) -> dict[str, list[dict]]:
    """Read the exported document into one cookie list per account."""
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        accounts = data.get("accounts")
        if accounts is None and "cookies" in data:
            accounts = [data]
    else:
        accounts = data
    if not isinstance(accounts, list):
        return {}
    sessions: dict[str, list[dict]] = {}
    for entry in accounts:
        if not isinstance(entry, dict):
            continue
        email = str(entry.get("email") or "").strip().lower()
        cookies = [
            cookie
            for cookie in (
                normalise_cookie(item) for item in entry.get("cookies") or []
            )
            if cookie
        ]
        if email and cookies:
            sessions[email] = cookies
    return sessions


def cookies_for(raw: str, email: str) -> list[dict]:
    return load_sessions(raw).get(str(email or "").strip().lower(), [])


def has_epic_session(cookies: list[dict]) -> bool:
    return any(_is_epic_cookie(cookie) for cookie in cookies or [])


async def seed_epic_session(context, raw: str, email: str, mode: str = "auto") -> int:
    """Put the exported cookies of one account into the browser context.

    Returns the number of cookies that were written. `mode` is `auto` (only
    a profile without Epic cookies is seeded), `always` (a bootstrap run
    that has to replace the cookies of a dead session) or `off`.
    """
    mode = str(mode or "auto").strip().lower()
    if mode in {"off", "false", "no", "0", ""}:
        return 0
    cookies = cookies_for(raw, email)
    if not cookies:
        return 0
    if mode != "always" and has_epic_session(await context.cookies()):
        print("SESSION_SEED:skipped:the_profile_has_its_own_cookies", flush=True)
        return 0
    written = 0
    for cookie in cookies:
        try:
            await context.add_cookies([cookie])
            written += 1
        except Exception as err:  # noqa: BLE001
            print(f"SESSION_SEED:cookie_rejected:{cookie.get('name')}:{err}", flush=True)
    print(f"SESSION_SEED:seeded:{written}/{len(cookies)}:mode={mode}", flush=True)
    return written
