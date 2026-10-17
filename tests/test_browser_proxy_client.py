import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit


SOURCE = Path(__file__).resolve().parents[1] / "scripts/prepare_browser_proxy.py"
spec = importlib.util.spec_from_file_location("prepare_browser_proxy", SOURCE)
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)
UUID = "d75a3240-386d-4edc-a1d9-614c7b3a70f0"
NODE = (f"vless://{UUID}@edge.example:443?encryption=none&security=tls"
        "&sni=tls.example&fp=chrome&type=ws&host=ws.example&path=%2Frelay%3Fed%3D2048")


class VlessConfigTests(unittest.TestCase):
    def test_preserves_distinct_dial_tls_and_websocket_hosts(self):
        config = client.vless_config(NODE)
        self.assertEqual(config["inbounds"][0]["listen"], "127.0.0.1")
        self.assertEqual(len(config["outbounds"]), 1)  # No direct fallback.
        outbound = config["outbounds"][0]
        self.assertEqual(outbound["settings"]["vnext"][0]["address"], "edge.example")
        stream = outbound["streamSettings"]
        self.assertEqual(stream["tlsSettings"]["serverName"], "tls.example")
        self.assertFalse(stream["tlsSettings"]["allowInsecure"])
        self.assertEqual(stream["wsSettings"], {
            "path": "/relay?ed=2048", "headers": {"Host": "ws.example"},
        })

    def test_unsupported_or_insecure_nodes_fail_without_echoing_credentials(self):
        for node in (NODE + "&allowInsecure=1", NODE + "&security=none",
                     NODE.replace("type=ws", "type=grpc"), NODE + "&ech=secret",
                     NODE.replace(UUID, "private-credential")):
            with self.subTest(node_type=node.split("?", 1)[-1].split("&")[0]):
                with self.assertRaises(ValueError) as error:
                    client.vless_config(node)
                self.assertNotIn(UUID, str(error.exception))
                self.assertNotIn("private-credential", str(error.exception))

    def test_worker_pin_preserves_transport_and_existing_path_options(self):
        original = client.vless_config(NODE)
        pinned = client.vless_config(NODE, "8.8.8.8")
        stream = pinned["outbounds"][0]["streamSettings"]
        path = urlsplit(stream["wsSettings"]["path"])
        self.assertEqual(path.path, "/relay")
        self.assertEqual(parse_qs(path.query), {"ed": ["2048"], "proxyip": ["8.8.8.8"]})
        stream["wsSettings"]["path"] = "/relay?ed=2048"
        self.assertEqual(pinned, original)

    def test_worker_pin_replaces_duplicate_query_values_and_rejects_private_targets(self):
        node = NODE.replace("ed%3D2048", "proxyip%3D1.1.1.1%26proxyip%3D9.9.9.9")
        path = client.vless_config(node, "8.8.8.8")["outbounds"][0]["streamSettings"]["wsSettings"]["path"]
        self.assertEqual(parse_qs(urlsplit(path).query), {"proxyip": ["8.8.8.8"]})
        for address in ("127.0.0.1", "10.0.0.1", "::1", "proxy.example", "8.8.8.8&secret=value"):
            with self.assertRaises(ValueError):
                client.vless_config(NODE, address)

    def test_checksum_mismatch_cannot_install_a_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(client, "urlopen", return_value=io.BytesIO(b"not trusted")):
                with self.assertRaisesRegex(RuntimeError, "checksum"):
                    client.install_xray(Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])


class ProxyPreparationTests(unittest.TestCase):
    def run_preparation(self, preflight_error=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            envfile = root / "github-env"
            process = Mock(pid=12345)
            process.poll.return_value = None
            output = io.StringIO()
            observed = []

            def start(binary, config_path):
                observed.append(json.loads(config_path.read_text()))
                if os.name == "posix":
                    self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
                return process

            with patch.dict(os.environ, {
                "EPIC_BROWSER_PROXY": NODE, "GITHUB_ENV": str(envfile), "RUNNER_TEMP": directory,
            }, clear=True), patch.object(client.sys, "platform", "linux"), \
                    patch.object(client, "install_xray", return_value=root / "xray"), \
                    patch.object(client, "start_xray", side_effect=start), \
                    patch.object(client, "preflight", return_value=200, side_effect=preflight_error), \
                    contextlib.redirect_stdout(output):
                result = client.main()
            exported = envfile.read_text() if envfile.exists() else ""
            self.assertNotIn(UUID, exported + output.getvalue())
            self.assertNotIn("edge.example", exported + output.getvalue())
            self.assertEqual(list(root.rglob("config.json")), [])
            self.assertEqual(len(observed), 1)
            return result, exported, process

    def test_success_exports_only_loopback_endpoint_and_pid(self):
        result, exported, process = self.run_preparation()
        self.assertEqual(result, 0)
        self.assertIn(f"EPIC_BROWSER_PROXY={client.LOCAL_PROXY}\n", exported)
        self.assertIn("EPIC_BROWSER_PROXY_PID=12345\n", exported)
        process.terminate.assert_not_called()

    def test_failed_upstream_probe_cleans_up_and_never_exports_proxy(self):
        result, exported, process = self.run_preparation(RuntimeError(NODE))
        self.assertEqual(result, 1)
        self.assertEqual(exported, "")
        process.terminate.assert_called_once()
        process.wait.assert_called_once()


class EgressDiagnosticTests(unittest.TestCase):
    def check(self, bodies):
        opener = Mock()
        opener.open.side_effect = [io.BytesIO(body) if isinstance(body, bytes) else body for body in bodies]
        with patch.object(client, "build_opener", return_value=opener):
            return client.egress_diagnostic(client.LOCAL_PROXY)

    def test_detects_different_origins_with_stable_individual_egress(self):
        result = self.check([b"ip=203.0.113.1\n", b"198.51.100.1", b"ip=203.0.113.1", b"ip=203.0.113.1"] * 2)
        self.assertEqual(result["completed_samples"], 8)
        self.assertEqual(result["distinct_egress_count"], 2)
        self.assertTrue(all(result["stable_per_origin"].values()))
        self.assertFalse(result["same_egress_across_origins"])
        self.assertTrue(result["same_egress_epic_hcaptcha"])
        self.assertNotIn("203.0.113.1", json.dumps(result))
        self.assertNotIn("198.51.100.1", json.dumps(result))

    def test_detects_changing_egress_on_the_same_origin(self):
        result = self.check([b"ip=203.0.113.1", b"203.0.113.1", b"ip=203.0.113.1", b"ip=203.0.113.1",
                             b"ip=203.0.113.2", b"203.0.113.1", b"ip=203.0.113.1", b"ip=203.0.113.1"])
        self.assertFalse(result["stable_per_origin"]["cloudflare"])
        self.assertTrue(result["stable_per_origin"]["amazon"])

    def test_incomplete_samples_do_not_claim_consistent_egress(self):
        result = self.check([b"invalid", b"203.0.113.1", b"HTML error", OSError("private detail")] * 2)
        self.assertEqual(result["completed_samples"], 2)
        self.assertIsNone(result["same_egress_across_origins"])
        self.assertIsNone(result["same_egress_epic_hcaptcha"])
        self.assertEqual(result["failures"]["cloudflare"], 2)

    def test_epic_and_hcaptcha_mismatch_is_detected_even_when_each_is_stable(self):
        result = self.check([b"ip=203.0.113.1", b"203.0.113.1", b"ip=203.0.113.1", b"ip=203.0.113.2"] * 2)
        self.assertTrue(result["stable_per_origin"]["epic"])
        self.assertTrue(result["stable_per_origin"]["hcaptcha"])
        self.assertFalse(result["same_egress_epic_hcaptcha"])


if __name__ == "__main__":
    unittest.main()
