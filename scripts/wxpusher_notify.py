import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


WXPUSHER_ENDPOINT = "https://wxpusher.zjiecode.com/api/send/message"
WXPUSHER_STATUS_ENDPOINT = "https://wxpusher.zjiecode.com/api/send/query/status"


def load_summary(path: Path) -> dict:
    if not path.exists():
        return {"accounts": [], "missing_summary": True}
    return json.loads(path.read_text(encoding="utf-8"))


def account_lines(summary: dict) -> tuple[int, int, list[str]]:
    accounts = summary.get("accounts") or []
    completed = 0
    lines: list[str] = []

    for account in accounts:
        status = account.get("status", "unknown")
        if status == "completed":
            completed += 1

        attempts = account.get("attempts") or []
        line = f"#{account.get('index')} {status}, attempts {len(attempts)}"
        lines.append(line)

        evidence = account.get("final_evidence") or {}
        if evidence:
            for title, item in evidence.items():
                lines.append(f"  - {title}: {item.get('status', 'unknown')}")
        else:
            lines.append("  - no ownership evidence")

        failed_attempts = [
            attempt for attempt in attempts if attempt.get("failed")
        ]
        for attempt in failed_attempts:
            reasons = attempt.get("failure_reasons") or ["unknown_failure"]
            lines.append(
                f"  - attempt {attempt.get('attempt')} failed: {', '.join(reasons)}"
            )

    return completed, len(accounts), lines


def build_message(summary: dict) -> tuple[str, str]:
    run_url = os.getenv("GITHUB_RUN_URL", "")
    run_conclusion = os.getenv("RUN_CONCLUSION", "").strip() or "unknown"

    completed, total, lines = account_lines(summary)
    has_failed_accounts = completed != total
    title_status = "FAILED" if has_failed_accounts or run_conclusion == "failure" else "SUCCESS"
    title = f"Epic Kiosk {title_status}: {completed}/{total} accounts"

    body = [
        title,
        "",
        f"Run conclusion: {run_conclusion}",
        f"Completed accounts: {completed}/{total}",
    ]

    if summary.get("missing_summary"):
        body.extend(["", "github_actions_summary.json was not found."])

    if lines:
        body.extend(["", *lines])

    if run_url:
        body.extend(["", f"Run: {run_url}"])

    return title[:99], "\n".join(body)


def send_wxpusher(summary: str, content: str) -> None:
    app_token = os.getenv("WXPUSHER_APP_TOKEN", "").strip()
    uid = os.getenv("WXPUSHER_UID", "").strip()
    if not app_token or not uid:
        print("WxPusher secrets are not configured; skipping notification.")
        return

    payload = {
        "appToken": app_token,
        "content": content,
        "summary": summary,
        "contentType": 1,
        "uids": [uid],
        "verifyPay": False,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        WXPUSHER_ENDPOINT,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        print(f"WxPusher request failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

    print("WxPusher response received.")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        print("WxPusher returned a non-JSON response.", file=sys.stderr)
        raise SystemExit(1)

    code = result.get("code")
    success = bool(result.get("success"))
    if code not in (1000, 200) and not success:
        print(f"WxPusher returned failure: code={code}", file=sys.stderr)
        raise SystemExit(1)

    record_ids = [
        str(item.get("sendRecordId"))
        for item in (result.get("data") or [])
        if item.get("sendRecordId")
    ]
    for record_id in record_ids:
        wait_for_delivery_status(record_id)


def query_delivery_status(send_record_id: str) -> dict:
    params = urllib.parse.urlencode({"sendRecordId": send_record_id})
    request = urllib.request.Request(f"{WXPUSHER_STATUS_ENDPOINT}?{params}")
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def wait_for_delivery_status(send_record_id: str) -> None:
    status_names = {
        0: "unknown",
        1: "waiting",
        2: "sent",
        3: "failed",
    }
    delays = [5, 15, 30]
    for attempt, delay in enumerate(delays, start=1):
        time.sleep(delay)
        result = query_delivery_status(send_record_id)
        status_code = result.get("data")
        status_name = status_names.get(status_code, str(status_code))
        print(
            f"WxPusher delivery status for record {send_record_id}: "
            f"{status_name} ({status_code})"
        )
        if status_code == 2:
            return
        if status_code == 3:
            raise SystemExit(1)
        if attempt == len(delays):
            print(
                "WxPusher accepted the message but it is still waiting to be sent.",
                file=sys.stderr,
            )
            raise SystemExit(1)


def main() -> int:
    summary_path = Path(
        os.getenv(
            "EPIC_SUMMARY_PATH",
            "app/volumes/runtime/github_actions_summary.json",
        )
    )
    summary = load_summary(summary_path)
    title, content = build_message(summary)
    send_wxpusher(title, content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
