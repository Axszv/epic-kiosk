import json
import os
import subprocess
import sys
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


def main() -> int:
    accounts = load_accounts()
    Path("app/volumes/user_data").mkdir(parents=True, exist_ok=True)
    Path("app/volumes/logs").mkdir(parents=True, exist_ok=True)
    Path("app/volumes/runtime").mkdir(parents=True, exist_ok=True)

    append_summary("## Epic Kiosk run\n\n")
    failures = 0

    for idx, account in enumerate(accounts, start=1):
        email = account["email"]
        masked = mask_email(email)
        print(f"::group::Epic account {idx}: {masked}")

        env = os.environ.copy()
        env["EPIC_EMAIL"] = email
        env["EPIC_PASSWORD"] = account["password"]
        env["ENABLE_APSCHEDULER"] = "false"
        env.setdefault("GEMINI_API_KEY", "not_used")

        proc = subprocess.run(
            ["xvfb-run", "-a", sys.executable, "app/deploy.py"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=int(os.getenv("TASK_TIMEOUT_SECONDS", "1200")),
        )

        output = proc.stdout or ""
        print(output)
        print("::endgroup::")

        failed = proc.returncode != 0 or any(
            marker in output
            for marker in (
                "FINAL_ERROR:",
                "ERROR_TYPE:",
                "GAME_ERROR:",
                "Traceback",
                "ModuleNotFoundError",
            )
        )
        if failed:
            failures += 1
            append_summary(f"- {masked}: failed\n")
        else:
            append_summary(f"- {masked}: completed\n")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
