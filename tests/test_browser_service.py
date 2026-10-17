import ast
import os
import types
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import unquote, urlsplit


SOURCE = Path(__file__).resolve().parents[1] / "app/services/browser_service.py"


class BrowserServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.browser = types.SimpleNamespace(close=AsyncMock())
        self.launch = AsyncMock(return_value=self.browser)
        self.camoufox_options = {}

        @asynccontextmanager
        async def playwright_context():
            yield types.SimpleNamespace(chromium=types.SimpleNamespace(
                launch_persistent_context=self.launch,
            ))

        @asynccontextmanager
        async def camoufox_context(**kwargs):
            self.camoufox_options.update(kwargs)
            yield self.browser

        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        nodes = [n for n in tree.body if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))]
        namespace = {
            "os": os, "Path": Path, "asynccontextmanager": asynccontextmanager,
            "logger": MagicMock(), "async_playwright": playwright_context,
            "AsyncCamoufox": camoufox_context, "Screen": MagicMock(),
            "DefaultAddons": types.SimpleNamespace(UBO="ubo"),
            "unquote": unquote, "urlsplit": urlsplit,
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
        self.claim_browser = namespace["claim_browser"]
        self.proxy_config = namespace["browser_proxy_config"]

    def test_browser_proxy_precedence_and_escaped_credentials(self):
        with patch.dict(os.environ, {
            "EPIC_BROWSER_PROXY": "https://name%40host:secret%3Avalue@proxy.example",
            "HTTPS_PROXY": "http://old.example:3128",
        }, clear=True):
            self.assertEqual(self.proxy_config(), {
                "server": "https://proxy.example:443", "username": "name@host",
                "password": "secret:value",
            })
            self.assertEqual(os.environ["HTTPS_PROXY"], "http://old.example:3128")

    def test_browser_proxy_defaults_and_ipv6(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "socks5://[::1]"}, clear=True):
            self.assertEqual(self.proxy_config(), {"server": "socks5://[::1]:1080"})
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(self.proxy_config())

    def test_invalid_proxy_error_does_not_echo_credentials(self):
        for value in ("private-secret", "http://name:private-secret@host:bad", "file:///private-secret"):
            with patch.dict(os.environ, {"EPIC_BROWSER_PROXY": value}, clear=True):
                with self.assertRaises(ValueError) as error:
                    self.proxy_config()
                self.assertNotIn("private-secret", str(error.exception))

    async def test_default_keeps_the_existing_camoufox_profile_and_options(self):
        with patch.dict(os.environ, {}, clear=True):
            async with self.claim_browser("profiles/account", headless=False) as browser:
                self.assertIs(browser, self.browser)
        self.assertEqual(self.camoufox_options["user_data_dir"], "profiles/account")
        self.assertFalse(self.camoufox_options["headless"])
        self.assertNotIn("extra_http_headers", self.camoufox_options)
        self.assertEqual(self.camoufox_options["exclude_addons"], ["ubo"])
        self.launch.assert_not_awaited()

    async def test_chromium_profile_is_separate_and_closed_after_error(self):
        with patch.dict(os.environ, {"EPIC_BROWSER_ENGINE": "chromium"}):
            with self.assertRaisesRegex(RuntimeError, "claim failed"):
                async with self.claim_browser("profiles/account", headless=False):
                    raise RuntimeError("claim failed")
        self.assertEqual(
            Path(self.launch.call_args.kwargs["user_data_dir"]),
            Path("profiles/account-chromium"),
        )
        self.browser.close.assert_awaited_once()
        self.assertEqual(self.camoufox_options, {})

    async def test_unknown_engine_fails_before_opening_any_profile(self):
        with patch.dict(os.environ, {"EPIC_BROWSER_ENGINE": "unsupported"}):
            with self.assertRaises(ValueError):
                async with self.claim_browser("profiles/account"):
                    self.fail("An unsupported engine must not launch")
        self.launch.assert_not_awaited()
        self.assertEqual(self.camoufox_options, {})

    async def test_display_mode_override_reaches_browser_launch(self):
        with patch.dict(os.environ, {
            "EPIC_BROWSER_ENGINE": "camoufox", "EPIC_BROWSER_HEADLESS": "false",
        }):
            async with self.claim_browser("profiles/account", headless=True):
                pass
        self.assertFalse(self.camoufox_options["headless"])


if __name__ == "__main__":
    unittest.main()
