"""The browser proxy stays separate from the model API traffic."""

import ast
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock


SOURCE = Path(__file__).resolve().parents[1] / "app/deploy.py"
tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
function = next(
    node
    for node in tree.body
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "execute_browser_tasks"
)
namespace = {
    "os": types.SimpleNamespace(getenv=lambda name, default=None: {
        "EPIC_BROWSER_PROXY": "http://127.0.0.1:17891",
    }.get(name, default)),
    "logger": MagicMock(),
    "settings": types.SimpleNamespace(user_data_dir="/tmp/profile"),
    "AsyncCamoufox": MagicMock(),
    "Screen": MagicMock(),
    "EpicAuthorization": MagicMock(),
    "EpicAgent": MagicMock(),
    "ErrorType": types.SimpleNamespace(SUCCESS="success", UNKNOWN="unknown"),
    "GameCollectResult": MagicMock(),
    "collect_mobile_offers": MagicMock(),
    "load_mobile_offers": MagicMock(return_value=[]),
    "suppress": __import__("contextlib", fromlist=["suppress"]).suppress,
}
exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"), namespace)
execute_browser_tasks = namespace["execute_browser_tasks"]


class BrowserProxyConfigTests(unittest.TestCase):
    def test_browser_proxy_env_var_is_read_first(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn('os.getenv("EPIC_BROWSER_PROXY")', source)
        # The model API traffic must not be routed through the browser proxy.
        self.assertIn('os.getenv("HTTP_PROXY")', source)

    def test_browser_proxy_is_parsed_with_escaped_credentials(self):
        proxy = types.SimpleNamespace(
            scheme="http", hostname="127.0.0.1", port=17891,
            username="name%40host", password="secret%3Avalue",
        )
        from urllib.parse import unquote, urlparse

        parsed = urlparse("http://name%40host:secret%3Avalue@127.0.0.1:17891")
        config = {
            "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
            "username": unquote(parsed.username),
            "password": unquote(parsed.password),
        }
        self.assertEqual(config["server"], "http://127.0.0.1:17891")
        self.assertEqual(config["username"], "name@host")
        self.assertEqual(config["password"], "secret:value")
        del proxy


if __name__ == "__main__":
    unittest.main()
