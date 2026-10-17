"""Adapt the existing dae VLESS/TLS/WebSocket node for an Actions browser.

Credentials/configuration stay in RUNNER_TEMP. Only the loopback proxy URL and
PID are exported to later steps; no client log or configuration is uploaded.
"""

import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen
from uuid import UUID
from zipfile import ZipFile


XRAY_VERSION = "26.3.27"
XRAY_SHA256 = "23cd9af937744d97776ee35ecad4972cf4b2109d1e0fe6be9930467608f7c8ae"
XRAY_URL = f"https://github.com/XTLS/Xray-core/releases/download/v{XRAY_VERSION}/Xray-linux-64.zip"
PROXY_PORT = 17891
LOCAL_PROXY = f"http://127.0.0.1:{PROXY_PORT}"
EPIC_PREFLIGHT = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
EGRESS_TARGETS = {
    "cloudflare": "https://www.cloudflare.com/cdn-cgi/trace",
    "amazon": "https://checkip.amazonaws.com/",
}


def vless_config(value: str) -> dict:
    """Support the transport actually used by the saved dae epic_cf node."""
    try:
        parsed = urlsplit(value.strip())
        query = parse_qs(parsed.query, keep_blank_values=True)
        if any(len(values) != 1 for values in query.values()):
            raise ValueError
        query = {key: values[0] for key, values in query.items()}
        if (
            parsed.scheme != "vless" or not parsed.hostname or parsed.password
            or query.get("type") != "ws" or query.get("security") != "tls"
            or query.get("encryption", "none") != "none"
            or query.get("flow")
            or any(query.get(key, "false").lower() not in {"", "0", "false"}
                   for key in ("insecure", "allowInsecure"))
            or set(query) - {"type", "security", "encryption", "flow", "sni", "fp",
                             "host", "path", "alpn", "insecure", "allowInsecure"}
        ):
            raise ValueError
        user_id = str(UUID(unquote(parsed.username or "")))
        port = parsed.port or 443
        tls = {"serverName": query.get("sni") or parsed.hostname,
               "allowInsecure": False, "fingerprint": query.get("fp") or "chrome"}
        if query.get("alpn"):
            tls["alpn"] = query["alpn"].split(",")
        ws = {"path": query.get("path") or "/"}
        if query.get("host"):
            ws["headers"] = {"Host": query["host"]}
        return {
            "log": {"loglevel": "none"},
            "inbounds": [{"listen": "127.0.0.1", "port": PROXY_PORT,
                          "protocol": "http", "settings": {}}],
            "outbounds": [{
                "protocol": "vless", "tag": "dae-node",
                "settings": {"vnext": [{"address": parsed.hostname, "port": port,
                                        "users": [{"id": user_id, "encryption": "none"}]}]},
                "streamSettings": {"network": "ws", "security": "tls",
                                   "tlsSettings": tls, "wsSettings": ws},
            }],
        }
    except (ValueError, TypeError):
        raise ValueError("Expected a valid VLESS/TLS/WebSocket node with certificate verification") from None


def install_xray(directory: Path) -> Path:
    request = Request(XRAY_URL, headers={"User-Agent": "epic-kiosk-actions"})
    with urlopen(request, timeout=90) as response:
        archive = response.read(100 * 1024 * 1024 + 1)
    if hashlib.sha256(archive).hexdigest() != XRAY_SHA256:
        raise RuntimeError("Xray archive checksum mismatch")
    with ZipFile(io.BytesIO(archive)) as bundle:
        binary_data = bundle.read("xray")
    binary = directory / "xray"
    binary.write_bytes(binary_data)
    binary.chmod(0o700)
    return binary


def start_xray(binary: Path, config: Path):
    process = subprocess.Popen(
        [str(binary), "run", "-config", str(config)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Browser proxy client exited during startup")
        try:
            with socket.create_connection(("127.0.0.1", PROXY_PORT), timeout=0.5):
                return process
        except OSError:
            time.sleep(0.25)
    process.terminate()
    process.wait(timeout=5)
    raise RuntimeError("Browser proxy client did not become ready")


def preflight(proxy: str) -> int:
    # No account credentials are sent. HTTP errors still prove that TLS to
    # Epic completed; the browser handles any site verification separately.
    opener = build_opener(ProxyHandler({"https": proxy, "http": proxy}))
    try:
        with opener.open(EPIC_PREFLIGHT, timeout=30) as response:
            return response.status
    except HTTPError as error:
        if error.code == 407:
            raise RuntimeError("Proxy authentication rejected") from None
        return error.code


def egress_diagnostic(proxy: str) -> dict:
    """Compare two public origins without logging or exporting IP addresses."""
    opener = build_opener(ProxyHandler({"https": proxy, "http": proxy}))
    samples = {name: [] for name in EGRESS_TARGETS}
    failures = {name: 0 for name in EGRESS_TARGETS}
    for _ in range(2):
        for name, url in EGRESS_TARGETS.items():
            try:
                with opener.open(url, timeout=15) as response:
                    body = response.read(8192).decode("utf-8").strip()
                if name == "cloudflare":
                    body = dict(line.split("=", 1) for line in body.splitlines() if "=" in line)["ip"]
                samples[name].append(str(ipaddress.ip_address(body)))
            except Exception:
                failures[name] += 1
    all_samples = [sample for values in samples.values() for sample in values]
    complete = len(all_samples) == 4
    return {
        "completed_samples": len(all_samples),
        "failures": failures,
        "distinct_egress_count": len(set(all_samples)),
        "stable_per_origin": {name: len(values) == 2 and len(set(values)) == 1
                              for name, values in samples.items()},
        "same_egress_across_origins": len(set(all_samples)) == 1 if complete else None,
    }


def main() -> int:
    value = os.environ.get("EPIC_BROWSER_PROXY", "").strip()
    if not value or not os.environ.get("GITHUB_ENV"):
        print("::error::EPIC_BROWSER_PROXY secret and GITHUB_ENV are required", flush=True)
        return 1
    process = None
    config_path = None
    stage = "configuration"
    try:
        scheme = urlsplit(value).scheme.lower()
        if scheme == "vless":
            config = vless_config(value)
            if not sys.platform.startswith("linux"):
                raise RuntimeError("The pinned proxy client requires a Linux runner")
            directory = Path(tempfile.mkdtemp(prefix="epic-browser-proxy-", dir=os.environ.get("RUNNER_TEMP")))
            directory.chmod(0o700)
            config_path = directory / "config.json"
            descriptor = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(config, stream)
            stage = "install_client"
            print(f"BROWSER_PROXY_CLIENT:xray:{XRAY_VERSION}:installing", flush=True)
            binary = install_xray(directory)
            stage = "start_client"
            process = start_xray(binary, config_path)
            # Xray loads configuration at startup; avoid retaining credentials
            # in even the temporary workspace after readiness.
            config_path.unlink()
            value = LOCAL_PROXY
            print(f"BROWSER_PROXY_CLIENT:xray:{XRAY_VERSION}:ready", flush=True)
        elif scheme not in {"http", "https", "socks5"}:
            raise ValueError("Unsupported proxy scheme")
        if "\n" in value or "\r" in value:
            raise ValueError("Invalid proxy value")
        if scheme != "socks5":
            stage = "epic_preflight"
            print("BROWSER_PROXY_PREFLIGHT:started", flush=True)
            status = preflight(value)
            if status in {502, 503, 504}:
                raise RuntimeError("Proxy upstream unavailable")
            print(f"BROWSER_PROXY_PREFLIGHT:epic_http_status={status}", flush=True)
        if os.environ.get("EPIC_PROXY_EGRESS_CHECK", "false").lower() == "true":
            stage = "egress_check"
            diagnostic = egress_diagnostic(value)
            print(f"BROWSER_PROXY_EGRESS:{json.dumps(diagnostic, sort_keys=True)}", flush=True)
        stage = "export"
        with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as env:
            env.write(f"EPIC_BROWSER_PROXY={value}\n")
            if process:
                env.write(f"EPIC_BROWSER_PROXY_PID={process.pid}\n")
        return 0
    except Exception as error:
        # Exceptions from network clients can include the credential-bearing
        # URL. Report only the class, never their message or traceback.
        print(f"::error::Browser proxy preparation failed at {stage} ({type(error).__name__})", flush=True)
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        return 1
    finally:
        if config_path:
            config_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
