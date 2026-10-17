import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


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


if __name__ == "__main__":
    unittest.main()
