import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


SERVERCHAN_ENDPOINT = "https://sctapi.ftqq.com/{sendkey}.send"


def load_summary(path: Path) -> dict:
    if not path.exists():
        return {"accounts": [], "missing_summary": True}
    return json.loads(path.read_text(encoding="utf-8"))


def account_sections(summary: dict) -> tuple[int, int, list[str]]:
    accounts = summary.get("accounts") or []
    completed = 0
    sections: list[str] = []

    for account in accounts:
        status = account.get("status", "unknown")
        if status == "completed":
            completed += 1

        attempts = account.get("attempts") or []
        email = account.get("email", "unknown")
        status_text = "已完成" if status == "completed" else "失败"
        block = [
            f"### 账号 {account.get('index')}：{email}",
            "",
            f"状态：{status_text} ｜ 尝试：{len(attempts)} 次",
            "",
        ]

        evidence = account.get("final_evidence") or {}
        if evidence:
            for title, item in evidence.items():
                evidence_status = item.get("status", "unknown")
                if evidence_status == "already_owned":
                    evidence_status = "已在库"
                elif evidence_status == "verified_owned":
                    evidence_status = "已入库"
                elif evidence_status == "region_unavailable":
                    evidence_status = "锁区跳过（未领取）"
                block.append(f"- {title}：{evidence_status}")
        else:
            block.append("- 没有领取证据")

        failed_attempts = [attempt for attempt in attempts if attempt.get("failed")]
        for attempt in failed_attempts:
            reasons = attempt.get("failure_reasons") or ["unknown_failure"]
            block.append(
                f"- 第 {attempt.get('attempt')} 次尝试失败：{', '.join(reasons)}"
            )

        sections.append("\n".join(block))

    return completed, len(accounts), sections


def build_message(summary: dict) -> tuple[str, str]:
    run_url = os.getenv("GITHUB_RUN_URL", "")
    run_conclusion = os.getenv("RUN_CONCLUSION", "").strip() or "unknown"

    completed, total, sections = account_sections(summary)
    has_failed_accounts = completed != total
    title_status = "失败" if has_failed_accounts or run_conclusion == "failure" else "成功"
    title = f"Epic Kiosk {title_status}：{completed}/{total} 个账号"

    body = [
        f"# {title}",
        "",
        f"- 运行结论：{run_conclusion}",
        f"- 已完成账号：{completed}/{total}",
    ]

    if summary.get("missing_summary"):
        body.extend(["", "未找到 github_actions_summary.json。"])

    if sections:
        body.extend(["", "## 账号明细", ""])
        body.append("\n\n".join(sections))

    if run_url:
        body.extend(["", f"[打开 GitHub Actions 运行记录]({run_url})"])

    return title[:100], "\n".join(body)


def send_serverchan(title: str, content: str) -> None:
    sendkey = os.getenv("SERVERCHAN_SENDKEY", "").strip()
    if not sendkey:
        print("ServerChan SendKey is not configured; skipping notification.")
        return

    data = urllib.parse.urlencode({"title": title, "desp": content}).encode("utf-8")
    request = urllib.request.Request(
        SERVERCHAN_ENDPOINT.format(sendkey=urllib.parse.quote(sendkey, safe="")),
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        print(f"ServerChan request failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

    print("ServerChan response received.")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        print("ServerChan returned a non-JSON response.", file=sys.stderr)
        raise SystemExit(1)

    if result.get("code") not in (0, 200) and result.get("errno") not in (0, None):
        print(
            "ServerChan returned failure: "
            f"code={result.get('code')} errno={result.get('errno')}",
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
    send_serverchan(title, content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
