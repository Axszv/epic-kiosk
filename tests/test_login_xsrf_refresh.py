"""Keep Epic's login transaction on its live XSRF cookie."""

import ast
import types
import unittest
from contextlib import suppress
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


SOURCE = Path(__file__).resolve().parents[1] / "app/services/epic_authorization_service.py"
WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/epic-claim.yml"

tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "EpicAuthorization")
cls.body = [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)
            and n.name == "_install_live_xsrf_route"]
namespace = {
    "suppress": suppress,
    "logger": MagicMock(),
}
exec(compile(ast.Module(body=[cls], type_ignores=[]), str(SOURCE), "exec"), namespace)


class LiveXsrfRouteTests(unittest.IsolatedAsyncioTestCase):
    def build_page(self, cookies, body='{"captcha": "first"}'):
        route = types.SimpleNamespace(
            continue_=AsyncMock(),
            abort=AsyncMock(),
            request=types.SimpleNamespace(
                url="https://www.epicgames.com/id/api/email/exists",
                headers={"x-xsrf-token": "stale-value"},
                post_data=body,
            ),
        )
        context = types.SimpleNamespace(cookies=AsyncMock(return_value=cookies))
        return types.SimpleNamespace(
            context=context,
            route=AsyncMock(),
            url="https://www.epicgames.com/id/login",
        ), route

    async def test_stale_header_is_replaced_with_the_live_cookie(self):
        auth = namespace["EpicAuthorization"]()
        page, route = self.build_page([{"name": "XSRF-TOKEN", "value": "live-value"}])
        auth.page = page
        auth._refresh_xsrf_token = AsyncMock(return_value="live-value")

        await auth._install_live_xsrf_route()

        page.route.assert_awaited_once()
        handler = page.route.await_args.args[1]
        await handler(route)
        headers = route.continue_.await_args.kwargs["headers"]
        self.assertEqual(headers["x-xsrf-token"], "live-value")

    async def test_matching_header_is_left_alone(self):
        auth = namespace["EpicAuthorization"]()
        page, route = self.build_page([{"name": "XSRF-TOKEN", "value": "stale-value"}])
        auth.page = page
        auth._refresh_xsrf_token = AsyncMock(return_value="stale-value")

        await auth._install_live_xsrf_route()

        handler = page.route.await_args.args[1]
        await handler(route)
        route.continue_.assert_awaited_once_with()

    async def test_repeated_transaction_is_dropped(self):
        auth = namespace["EpicAuthorization"]()
        page, route = self.build_page([{"name": "XSRF-TOKEN", "value": "live-value"}])
        auth.page = page
        auth._refresh_xsrf_token = AsyncMock(return_value="live-value")

        await auth._install_live_xsrf_route()

        handler = page.route.await_args.args[1]
        await handler(route)
        # The portal repeats the same transaction with the same captcha token.
        await handler(route)
        route.abort.assert_awaited_once()
        self.assertEqual(route.continue_.await_count, 1)

    async def test_a_new_transaction_is_allowed(self):
        auth = namespace["EpicAuthorization"]()
        page, route = self.build_page([{"name": "XSRF-TOKEN", "value": "live-value"}])
        auth.page = page
        auth._refresh_xsrf_token = AsyncMock(return_value="live-value")

        await auth._install_live_xsrf_route()

        handler = page.route.await_args.args[1]
        await handler(route)
        route.request.post_data = '{"captcha": "second"}'
        await handler(route)
        route.abort.assert_not_awaited()
        self.assertEqual(route.continue_.await_count, 2)


class ScheduledProxyTests(unittest.TestCase):
    def test_the_schedule_keeps_the_runners_own_connection(self):
        # The cached browser profile and the account session belong to the
        # address that created them. A scheduled run behind another address
        # would throw the session away and need a captcha every week.
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertNotIn("EPIC_BROWSER_PROXY_ENABLED", workflow)
        proxy_start = workflow.index("      - name: Prepare browser proxy")
        proxy_end = workflow.index("      - name: Run Epic claim", proxy_start)
        proxy_step = workflow[proxy_start:proxy_end]
        self.assertIn(
            "if: github.event.inputs.use_browser_proxy == 'true'", proxy_step
        )
        # A proxy outage must not skip the weekly claim.
        self.assertIn("continue-on-error: true", proxy_step)


if __name__ == "__main__":
    unittest.main()
