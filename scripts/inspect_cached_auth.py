"""Print credential-free login diagnostics from a selected Actions cache."""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit


root = Path("app/volumes")
for path in root.rglob("runtime.log"):
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "2026-10-16 12:" not in line:
            continue
        if "邮箱校验 API 响应" in line and "响应: " in line:
            raw = line.split("响应: ", 1)[1]
            try:
                payload = json.loads(raw)
            except ValueError:
                print("EMAIL_RESPONSE:non_json")
                continue
            if isinstance(payload, dict):
                safe = {key: payload[key] for key in ("errorCode", "errorMessage", "numericErrorCode") if key in payload}
                print("EMAIL_RESPONSE:" + re.sub(r"[\w.+-]+@[\w.-]+", "[email]", json.dumps(safe)))
        elif any(marker in line for marker in (
            "处理登录 hCaptcha", "重新提交登录", "重新触发安全检查", "密码", "login_result_timeout",
        )):
            # Report only known stage names, never the raw log line or inputs.
            stage = next((marker for marker in (
                "处理登录 hCaptcha", "重新提交登录", "重新触发安全检查", "login_result_timeout",
            ) if marker in line), "password_stage")
            print("AUTH_STAGE:" + stage)

for path in sorted(root.rglob("login*timeout.json")):
    data = json.loads(path.read_text(encoding="utf-8"))
    url = urlsplit(data.get("url", ""))
    print("AUTH_DEBUG:" + json.dumps({"file": path.name, "host": url.hostname, "path": url.path}))
    html = path.with_suffix(".html")
    if html.exists():
        source = html.read_text(encoding="utf-8", errors="replace")
        print("AUTH_FORM:" + json.dumps({
            "email": 'id="email"' in source,
            "password": 'id="password"' in source,
            "sign_in": 'id="sign-in"' in source,
            "captcha": "h_captcha_challenge_" in source,
            "incorrect_response": "Incorrect response" in source,
        }))
