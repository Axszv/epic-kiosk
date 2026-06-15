import json
import os
import re
import selectors
import shutil
import subprocess
import sys
import time
from pathlib import Path


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


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
PROMOTION_RE = re.compile(r'\{"title": "([^"]+)", "url": "([^"]+)"\}')
OWNERSHIP_CTA_RE = re.compile(r"Ownership CTA for (.*?): '([^']+)'")


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


def run_account(account: dict[str, str], display_index: int, attempt: int) -> tuple[bool, dict]:
    email = account["email"]
    masked = mask_email(email)
    print(f"::group::Epic account {display_index}: {masked} (attempt {attempt})")

    env = os.environ.copy()
    env["EPIC_EMAIL"] = email
    env["EPIC_PASSWORD"] = account["password"]
    env["ENABLE_APSCHEDULER"] = "false"
    env.setdefault("GEMINI_API_KEY", "not_used")

    timeout_seconds = int(os.getenv("TASK_TIMEOUT_SECONDS", "1200"))
    proc = subprocess.Popen(
        ["xvfb-run", "-a", sys.executable, "app/deploy.py"],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
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
            proc.kill()
            proc.wait()
            break

    remainder = proc.stdout.read()
    if remainder:
        chunks.append(remainder)
        print(remainder, end="", flush=True)
    selector.close()

    output = "".join(chunks)
    print("::endgroup::")

    evidence = parse_claim_evidence(output)
    missing_evidence = [
        title
        for title, item in evidence.items()
        if item["status"] not in ("already_owned", "verified_owned")
    ]
    if missing_evidence:
        print(
            "Missing strict ownership evidence for: "
            + ", ".join(missing_evidence),
            flush=True,
        )

    failed = timed_out or proc.returncode != 0 or bool(missing_evidence) or any(
        marker in output
        for marker in (
            "FINAL_ERROR:",
            "ERROR_TYPE:",
            "GAME_ERROR:",
            "Traceback",
            "ModuleNotFoundError",
        )
    )
    result = {
        "attempt": attempt,
        "timed_out": timed_out,
        "returncode": proc.returncode,
        "failed": failed,
        "missing_evidence": missing_evidence,
        "evidence": evidence,
    }
    return not failed, result


def main() -> int:
    raw_accounts = load_accounts()
    account_index = os.getenv("EPIC_ACCOUNT_INDEX", "").strip()
    account_limit = os.getenv("EPIC_ACCOUNT_LIMIT", "").strip()
    indexed_accounts = list(enumerate(raw_accounts, start=1))
    if account_index:
        index = int(account_index)
        if index <= 0 or index > len(raw_accounts):
            raise SystemExit(
                f"EPIC_ACCOUNT_INDEX must be between 1 and {len(raw_accounts)}"
            )
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
    failures = 0
    max_attempts = max(1, int(os.getenv("EPIC_ACCOUNT_MAX_ATTEMPTS", "2")))
    summary = {"accounts": []}

    for original_index, account in indexed_accounts:
        email = account["email"]
        masked = mask_email(email)
        account_result = {
            "index": original_index,
            "email": masked,
            "status": "failed",
            "attempts": [],
            "final_evidence": {},
        }

        for attempt in range(1, max_attempts + 1):
            ok, attempt_result = run_account(account, original_index, attempt)
            account_result["attempts"].append(attempt_result)
            account_result["final_evidence"] = attempt_result["evidence"]
            if ok:
                account_result["status"] = "completed"
                break
            if attempt < max_attempts:
                print(
                    f"Account {original_index} failed attempt {attempt}; "
                    f"retrying with a fresh browser process."
                )

        summary["accounts"].append(account_result)

        if account_result["status"] != "completed":
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
            for title, item in account_result["final_evidence"].items():
                append_summary(f"  - {title}: {item['status']}\n")

        Path("app/volumes/runtime/github_actions_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
