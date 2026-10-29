import json
import os
import re
import selectors
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mobile_offer_discovery import discover_mobile_offers


def mask_email(email: str) -> str:
    if "@" not in email:
        return "***"
    name, domain = email.split("@", 1)
    if len(name) <= 2:
        return f"***@{domain}"
    return f"{name[:2]}***@{domain}"


def load_accounts() -> list[dict[str, str]]:
    raw = os.getenv("EPIC_ACCOUNTS_JSON", "").strip()
    if not raw:
        raise SystemExit("EPIC_ACCOUNTS_JSON secret is empty")

    data = json.loads(raw)
    if isinstance(data, dict):
        data = data.get("accounts", [])
    if not isinstance(data, list):
        raise SystemExit("EPIC_ACCOUNTS_JSON must be a list or an object with an accounts list")

    accounts: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        email = str(item.get("email", "")).strip()
        password = str(item.get("password", "")).strip()
        if email and password:
            accounts.append({"email": email, "password": password})

    if not accounts:
        raise SystemExit("No valid Epic accounts found in EPIC_ACCOUNTS_JSON")
    return accounts


def append_summary(text: str) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    Path(summary_path).write_text(
        Path(summary_path).read_text(encoding="utf-8") + text,
        encoding="utf-8",
    ) if Path(summary_path).exists() else Path(summary_path).write_text(text, encoding="utf-8")


def clean_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for item in path.iterdir():
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()


def write_run_summary(path: Path, summary: dict) -> None:
    path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def display_evidence_status(status: str) -> str:
    return {
        "already_owned": "already_owned",
        "verified_owned": "verified_owned",
        "region_unavailable": "region_unavailable_skipped",
    }.get(status, status)


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
PROMOTION_RE = re.compile(r'\{"title": "([^"]+)", "url": "([^"]+)"\}')
OWNERSHIP_CTA_RE = re.compile(r"Ownership CTA for (.*?): '([^']+)'")
DESKTOP_RESULT_RE = re.compile(r"DESKTOP_RESULT:(.*):([a-z_]+)\s*$")
ERROR_MARKER_RE = re.compile(r"(FINAL_ERROR|ERROR_TYPE|GAME_ERROR):([A-Za-z0-9_\-]+)")
REGION_UNAVAILABLE_RE = re.compile(r"REGION_UNAVAILABLE:(.+)")
MOBILE_RESULT_RE = re.compile(r"MOBILE_RESULT:([^:\r\n]+):(.*):([a-z_]+)\s*$")
# `region_unavailable` and `not_purchasable` are the store refusing the
# promotion for this account, not the run failing to claim it: a game the
# account's country cannot buy, or a DLC whose base game it does not own.
# An account is attempted more than once, and every attempt reports the same
# games again. A later attempt that merely fails to find a purchase control
# must not erase the ownership an earlier one confirmed: run 40744596200
# claimed both games, then failed with "Missing strict mobile ownership
# evidence" because its last attempt had ended on `no_purchase_cta` — the
# state an owned game shows once the store stops offering it.
EVIDENCE_RANK = {
    "verified_owned": 4,
    "already_owned": 4,
    "region_unavailable": 3,
    "not_purchasable": 3,
    "no_purchase_cta": 1,
    "cta_disabled": 1,
    "login_required": 0,
    "unknown": -1,
}


def _outranks(status: str, current: str | None) -> bool:
    """Whether `status` says more about ownership than `current` does."""
    return EVIDENCE_RANK.get(status, 2) > EVIDENCE_RANK.get(current or "unknown", -1)


SUCCESS_EVIDENCE_STATUSES = {
    "already_owned", "verified_owned", "region_unavailable", "not_purchasable"
}
RETRY_BACKOFF_SECONDS = {
    "cloudflare_blocked": 45,
    "captcha_failed": 20,
    "login_timeout": 15,
    "network_timeout": 15,
}


def account_retry_delay(attempt_result: dict) -> int:
    """Return a short backoff for transient login failures."""
    values = {
        marker.get("value")
        for marker in attempt_result.get("error_markers", [])
        if marker.get("kind") == "ERROR_TYPE"
    }
    return max((RETRY_BACKOFF_SECONDS.get(value, 0) for value in values), default=0)


def merge_desktop_evidence(current: dict, incoming: dict) -> dict:
    """Keep strict ownership evidence across retries without promoting unknowns."""
    merged = dict(current)
    for title, item in incoming.items():
        existing = merged.get(title)
        if existing and existing.get("status") in SUCCESS_EVIDENCE_STATUSES:
            continue
        merged[title] = item
    return merged


def merge_mobile_evidence(current: dict, incoming: dict) -> dict:
    merged = {platform: dict(items) for platform, items in current.items()}
    for platform, items in incoming.items():
        platform_evidence = merged.setdefault(platform, {})
        for title, item in items.items():
            existing = platform_evidence.get(title)
            if existing and existing.get("status") in SUCCESS_EVIDENCE_STATUSES:
                continue
            platform_evidence[title] = item
    return merged


def has_complete_desktop_evidence(desktop_evidence: dict) -> bool:
    """Whether the desktop store proved every promotion is in the library."""
    return bool(desktop_evidence) and not missing_desktop_evidence(desktop_evidence)


def missing_desktop_evidence(desktop_evidence: dict) -> list[str]:
    return [
        title
        for title, item in desktop_evidence.items()
        if item.get("status") not in SUCCESS_EVIDENCE_STATUSES
    ]


def attempt_outcome(
    desktop_evidence: dict,
    mobile_evidence: dict,
    mobile_offers: list[dict],
) -> dict:
    """What each channel of one attempt still has to claim.

    The two channels hand out different games: the desktop promotions of the
    week are not the Android and iOS offers. A proof from one channel says
    nothing about the games of the other, so each channel is judged on its
    own evidence and both are required for a completed account.

    Both channels claim on the same `store.epicgames.com/p/<slug>` page with
    the same cookies; `mobile_offer_discovery` builds the URL from the same
    Store origin. A mobile result of `login_required` is therefore this code
    reading the product page as signed out, not a second login.
    """
    desktop_missing = missing_desktop_evidence(desktop_evidence)
    mobile_missing = missing_mobile_evidence(mobile_offers, mobile_evidence)
    return {
        "desktop_complete": bool(desktop_evidence) and not desktop_missing,
        "mobile_complete": bool(mobile_evidence) and not mobile_missing,
        "desktop_missing": desktop_missing,
        "mobile_missing": mobile_missing,
    }


def is_settled(account_result: dict) -> bool:
    """Whether the account needs no further attempt in this run."""
    return account_result.get("status") == "completed"


def parse_claim_evidence(output: str) -> dict[str, dict[str, str]]:
    """Extract strict ownership evidence from the browser log."""
    promotions: list[str] = []
    evidence: dict[str, dict[str, str]] = {}

    def ensure(title: str) -> dict[str, str]:
        return evidence.setdefault(title, {"status": "unknown", "evidence": ""})

    for raw_line in output.splitlines():
        line = ANSI_RE.sub("", raw_line)

        promotion_match = PROMOTION_RE.search(line)
        if promotion_match:
            title = promotion_match.group(1)
            if title not in promotions:
                promotions.append(title)
                ensure(title)
            continue

        region_unavailable_match = REGION_UNAVAILABLE_RE.search(line)
        if region_unavailable_match:
            title = region_unavailable_match.group(1).strip()
            if title:
                if title not in promotions:
                    promotions.append(title)
                ensure(title).update(
                    {
                        "status": "region_unavailable",
                        "evidence": line.strip(),
                    }
                )
            continue

        desktop_result_match = DESKTOP_RESULT_RE.search(line)
        if desktop_result_match:
            title, status = (part.strip() for part in desktop_result_match.groups())
            if title:
                if title not in promotions:
                    promotions.append(title)
                if _outranks(status, ensure(title).get("status")):
                    ensure(title).update(
                        {
                            "status": status,
                            "evidence": f"DESKTOP_RESULT:{title}:{status}",
                        }
                    )
            continue

        cta_match = OWNERSHIP_CTA_RE.search(line)
        if cta_match:
            title, button_text = cta_match.groups()
            if "IN LIBRARY" in button_text.upper() or "OWNED" in button_text.upper():
                ensure(title).update(
                    {
                        "status": "verified_owned",
                        "evidence": f"Ownership CTA: {button_text}",
                    }
                )
            continue

        if "'In Library'" in line or "'Owned'" in line:
            for title in promotions:
                item = ensure(title)
                if item["status"] == "unknown":
                    item.update({"status": "already_owned", "evidence": line.strip()})
                    break

    return evidence


def parse_mobile_evidence(output: str) -> dict[str, dict[str, dict[str, str]]]:
    evidence: dict[str, dict[str, dict[str, str]]] = {}
    for raw_line in output.splitlines():
        line = ANSI_RE.sub("", raw_line)
        match = MOBILE_RESULT_RE.search(line)
        if not match:
            continue
        platform, title, status = (part.strip() for part in match.groups())
        if not platform or not title:
            continue
        current = evidence.setdefault(platform, {}).get(title)
        if current and not _outranks(status, current.get("status")):
            continue
        evidence[platform][title] = {
            "status": status,
            "evidence": f"MOBILE_RESULT:{platform}:{title}:{status}",
        }
    return evidence


def missing_mobile_evidence(
    offers: list[dict],
    evidence: dict[str, dict[str, dict[str, str]]],
) -> list[str]:
    missing = []
    for offer in offers:
        platform = str(offer.get("platform") or "Mobile")
        title = str(offer.get("title") or "Unknown mobile offer")
        item = evidence.get(platform, {}).get(title)
        if not item or item.get("status") not in SUCCESS_EVIDENCE_STATUSES:
            missing.append(f"{platform}:{title}")
    return missing


def parse_error_markers(output: str) -> list[dict[str, str]]:
    markers: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_line in output.splitlines():
        line = ANSI_RE.sub("", raw_line)
        for kind, value in ERROR_MARKER_RE.findall(line):
            key = (kind, value)
            if key in seen:
                continue
            seen.add(key)
            markers.append({"kind": kind, "value": value})
    return markers


def run_account(
    account: dict[str, str],
    display_index: int,
    attempt: int,
    mobile_offers: list[dict],
) -> tuple[bool, dict]:
    email = account["email"]
    masked = mask_email(email)
    print(f"::group::Epic account {display_index}: {masked} (attempt {attempt})")

    env = os.environ.copy()
    env["EPIC_EMAIL"] = email
    env["EPIC_PASSWORD"] = account["password"]
    env["MOBILE_OFFERS_JSON"] = json.dumps(mobile_offers, ensure_ascii=False)
    env["ENABLE_APSCHEDULER"] = "false"
    env.setdefault("GEMINI_API_KEY", "not_used")

    fresh_profile = os.getenv("EPIC_FRESH_PROFILE", "false").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if fresh_profile:
        run_id = os.getenv("GITHUB_RUN_ID", str(os.getpid()))
        profile = Path(tempfile.gettempdir()).joinpath(
            f"epic-kiosk-fresh-{run_id}-{display_index}-{attempt}"
        )
        if profile.exists():
            shutil.rmtree(profile)
        profile.mkdir(parents=True)
        env["EPIC_USER_DATA_DIR"] = str(profile)
        print(
            f"BROWSER_PROFILE_MODE:fresh_temporary:account={display_index}",
            flush=True,
        )

    timeout_seconds = int(os.getenv("TASK_TIMEOUT_SECONDS", "1200"))
    popen_kwargs = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        ["xvfb-run", "-a", sys.executable, "app/deploy.py"],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        **popen_kwargs,
    )

    chunks: list[str] = []
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    assert proc.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    while proc.poll() is None:
        for key, _ in selector.select(timeout=0.2):
            line = key.fileobj.readline()
            if line:
                chunks.append(line)
                print(line, end="", flush=True)

        if time.monotonic() > deadline:
            timed_out = True
            timeout_msg = f"FINAL_ERROR:timeout after {timeout_seconds}s\n"
            chunks.append(timeout_msg)
            print(timeout_msg, end="", flush=True)
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            break

    if not timed_out:
        remainder = proc.stdout.read()
        if remainder:
            chunks.append(remainder)
            print(remainder, end="", flush=True)
    selector.close()

    output = "".join(chunks)
    print("::endgroup::")

    desktop_evidence = parse_claim_evidence(output)
    mobile_evidence = parse_mobile_evidence(output)
    error_markers = parse_error_markers(output)
    outcome = attempt_outcome(desktop_evidence, mobile_evidence, mobile_offers)
    if outcome["desktop_missing"]:
        print(
            "Missing strict desktop ownership evidence for: "
            + ", ".join(outcome["desktop_missing"]),
            flush=True,
        )
    if outcome["mobile_missing"]:
        # The mobile offers are games of their own, and they are claimed on
        # the same product page as the desktop promotions, so a missing one
        # is a game that was not claimed.
        print(
            "Missing strict mobile ownership evidence for: "
            + ", ".join(outcome["mobile_missing"]),
            flush=True,
        )

    failure_reasons = []
    if timed_out:
        failure_reasons.append(f"process_timeout:{timeout_seconds}s")
    if proc.returncode != 0:
        failure_reasons.append(f"returncode:{proc.returncode}")
    if outcome["desktop_missing"]:
        failure_reasons.append("missing_desktop_ownership_evidence")
    if outcome["mobile_missing"]:
        failure_reasons.append("missing_mobile_ownership_evidence")
    failure_reasons.extend(
        f"{marker['kind']}:{marker['value']}" for marker in error_markers
    )
    if "Traceback" in output:
        failure_reasons.append("python_traceback")
    if "ModuleNotFoundError" in output:
        failure_reasons.append("module_not_found")

    failed = bool(failure_reasons)
    if fresh_profile and not failed:
        # A fresh profile holds the session that the login just established.
        # The cached profile is the next week's login, so the session is
        # copied back into it before the workflow saves the state.
        cached = Path(__file__).resolve().parents[1].joinpath(
            "app", "volumes", "user_data", email
        )
        try:
            if cached.exists():
                shutil.rmtree(cached)
            cached.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                profile,
                cached,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("lock", ".parentlock", "parent.lock"),
            )
            print(f"BROWSER_PROFILE_MODE:session_cached:{email}", flush=True)
        except Exception as err:  # noqa: BLE001
            print(f"BROWSER_PROFILE_MODE:session_cache_failed:{err}", flush=True)
    result = {
        "attempt": attempt,
        "timed_out": timed_out,
        "returncode": proc.returncode,
        "failed": failed,
        "failure_reasons": failure_reasons,
        "error_markers": error_markers,
        "missing_desktop_evidence": outcome["desktop_missing"],
        "missing_mobile_evidence": outcome["mobile_missing"],
        "desktop_complete": outcome["desktop_complete"],
        "mobile_complete": outcome["mobile_complete"],
        "desktop_evidence": desktop_evidence,
        "mobile_evidence": mobile_evidence,
    }
    return not failed, result


def main() -> int:
    raw_accounts = load_accounts()
    account_index = os.getenv("EPIC_ACCOUNT_INDEX", "").strip()
    account_limit = os.getenv("EPIC_ACCOUNT_LIMIT", "").strip()
    indexed_accounts = list(enumerate(raw_accounts, start=1))
    if account_index:
        index = int(account_index)
        if index <= 0:
            raise SystemExit(
                f"EPIC_ACCOUNT_INDEX must be between 1 and {len(raw_accounts)}"
            )
        if index > len(raw_accounts):
            # Every account runs in its own job, and a matrix can offer more
            # accounts than there are. There is simply nothing to do here.
            print(
                f"EPIC_ACCOUNT_INDEX {index} is beyond the {len(raw_accounts)} "
                "configured accounts; nothing to do",
                flush=True,
            )
            return 0
        indexed_accounts = [indexed_accounts[index - 1]]
    elif account_limit:
        limit = int(account_limit)
        if limit <= 0:
            raise SystemExit("EPIC_ACCOUNT_LIMIT must be a positive integer")
        indexed_accounts = indexed_accounts[:limit]

    Path("app/volumes/user_data").mkdir(parents=True, exist_ok=True)
    clean_directory(Path("app/volumes/logs"))
    clean_directory(Path("app/volumes/runtime"))

    append_summary("## Epic Kiosk run\n\n")
    # The mobile offers are claimed on the same product page as the desktop
    # promotions, and that page sends the browser to Epic's login page when
    # it does not accept the session for the claim. Those navigations are
    # the one step of a run that touches the session the desktop claim just
    # used, so a run has to be able to leave the mobile channel out.
    skip_mobile = os.getenv("EPIC_SKIP_MOBILE", "false").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if skip_mobile:
        mobile_discovery = {"status": "skipped", "offers": []}
        mobile_offers = []
        print("Mobile collection is skipped for this run", flush=True)
        append_summary("- Mobile collection: skipped for this run\n\n")
    else:
        try:
            mobile_discovery = discover_mobile_offers()
            mobile_offers = mobile_discovery["offers"]
            if not mobile_offers:
                raise RuntimeError("official Epic mobile discovery returned no weekly free offers")
        except Exception as err:
            message = f"Mobile offer discovery failed: {type(err).__name__}: {err}"
            print(message, flush=True)
            summary = {
                "accounts": [],
                "mobile_discovery": {"status": "failed", "error": message},
            }
            write_run_summary(
                Path("app/volumes/runtime/github_actions_summary.json"),
                summary,
            )
            append_summary(f"- {message}\n")
            return 1

    Path("app/volumes/runtime/mobile_discovery_summary.json").write_text(
        json.dumps(mobile_discovery, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if mobile_offers:
        print(
            "Discovered mobile weekly free offers: "
            + ", ".join(
                f"{offer['platform']}:{offer['title']}" for offer in mobile_offers
            ),
            flush=True,
        )
        append_summary(
            "- Mobile discovery: "
            + ", ".join(
                f"{offer['platform']} {offer['title']}" for offer in mobile_offers
            )
            + "\n\n"
        )

    failures = 0
    max_attempts = max(1, int(os.getenv("EPIC_ACCOUNT_MAX_ATTEMPTS", "3")))
    summary_path = Path("app/volumes/runtime/github_actions_summary.json")
    account_results: dict[int, dict] = {}

    for original_index, account in indexed_accounts:
        email = account["email"]
        masked = mask_email(email)
        account_results[original_index] = {
            "index": original_index,
            "email": masked,
            "status": "pending",
            "attempts": [],
            "desktop_evidence": {},
            "mobile_evidence": {},
        }

    summary = {
        "accounts": list(account_results.values()),
        "mobile_discovery": {
            "status": "completed",
            "offers": mobile_offers,
        },
    }
    write_run_summary(summary_path, summary)

    # One account that needs a login costs about ten minutes, and eight of
    # them across three rounds is more than the job may run. The guard stops
    # the run before GitHub kills it, because a killed job does not save the
    # browser state that the next run starts from.
    deadline_minutes = float(os.getenv("EPIC_RUN_DEADLINE_MINUTES", "0") or 0)
    started_at = time.monotonic()

    def past_deadline() -> bool:
        if deadline_minutes <= 0:
            return False
        elapsed = (time.monotonic() - started_at) / 60
        if elapsed < deadline_minutes:
            return False
        print(
            f"Run deadline of {deadline_minutes:g} minutes reached after "
            f"{elapsed:.0f} minutes; the remaining accounts are left for the next run.",
            flush=True,
        )
        return True

    for attempt in range(1, max_attempts + 1):
        pending_indexes = [
            original_index
            for original_index, _ in indexed_accounts
            if not is_settled(account_results[original_index])
        ]
        if not pending_indexes:
            break
        if past_deadline():
            break

        print(
            f"Starting attempt round {attempt}/{max_attempts} for account(s): "
            + ", ".join(str(index) for index in pending_indexes),
            flush=True,
        )

        for original_index, account in indexed_accounts:
            account_result = account_results[original_index]
            if is_settled(account_result):
                continue
            if past_deadline():
                break

            ok, attempt_result = run_account(
                account,
                original_index,
                attempt,
                mobile_offers,
            )
            account_result["attempts"].append(attempt_result)
            account_result["desktop_evidence"] = merge_desktop_evidence(
                account_result["desktop_evidence"],
                attempt_result["desktop_evidence"],
            )
            account_result["mobile_evidence"] = merge_mobile_evidence(
                account_result["mobile_evidence"],
                attempt_result["mobile_evidence"],
            )
            # Evidence is kept across attempts, so a game claimed by an
            # earlier attempt of this run still counts. An account is only
            # completed when both channels proved every one of their games.
            desktop_complete = has_complete_desktop_evidence(
                account_result["desktop_evidence"]
            )
            mobile_missing = missing_mobile_evidence(
                mobile_offers, account_result["mobile_evidence"]
            )
            account_result["desktop_missing"] = missing_desktop_evidence(
                account_result["desktop_evidence"]
            )
            account_result["mobile_missing"] = mobile_missing
            if desktop_complete and not mobile_missing:
                account_result["status"] = "completed"
                if not ok:
                    attempt_result["completed_from_cumulative_evidence"] = True
                    print(
                        f"Account {original_index} has ownership evidence for every "
                        "desktop and mobile offer across its attempts.",
                        flush=True,
                    )
            else:
                if desktop_complete:
                    print(
                        f"Account {original_index} claimed every desktop promotion; "
                        "the mobile offers are still missing: "
                        + ", ".join(mobile_missing),
                        flush=True,
                    )
                # The profile is never thrown away here. It holds the session
                # the account logs in with, and a login that fails is a lost
                # captcha round rather than a broken profile: the retry needs
                # the very cookies this branch would delete.
                if attempt < max_attempts:
                    retry_delay = account_retry_delay(attempt_result)
                    print(
                        f"Account {original_index} failed attempt {attempt}; "
                        "retrying in the next round with a fresh browser process.",
                        flush=True,
                    )
                    if retry_delay:
                        print(
                            f"Waiting {retry_delay}s before continuing after a transient "
                            "login failure.",
                            flush=True,
                        )
                        time.sleep(retry_delay)

            write_run_summary(summary_path, summary)

    for original_index, _ in indexed_accounts:
        account_result = account_results[original_index]
        masked = account_result["email"]
        if not is_settled(account_result):
            account_result["status"] = "failed"
            failures += 1
            append_summary(
                f"- account {original_index} {masked}: failed after "
                f"{len(account_result['attempts'])} attempt(s)\n"
            )
        else:
            attempt_count = len(account_result["attempts"])
            append_summary(
                f"- account {original_index} {masked}: completed "
                f"after {attempt_count} attempt(s)\n"
            )
            for title, item in account_result["desktop_evidence"].items():
                append_summary(
                    f"  - Desktop / {title}: {display_evidence_status(item['status'])}\n"
                )
            for platform, platform_evidence in account_result["mobile_evidence"].items():
                for title, item in platform_evidence.items():
                    append_summary(
                        f"  - {platform} / {title}: "
                        f"{display_evidence_status(item['status'])}\n"
                    )

        for attempt_result in account_result["attempts"]:
            if not attempt_result.get("failed"):
                continue
            reasons = attempt_result.get("failure_reasons") or ["unknown_failure"]
            append_summary(
                f"  - attempt {attempt_result['attempt']} failed: "
                f"{', '.join(reasons)}\n"
            )

        write_run_summary(summary_path, summary)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
