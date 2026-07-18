import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))


class NullLogger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def passthrough_decorator(*_args, **_kwargs):
    return lambda function: function


sys.modules.setdefault("httpx", types.SimpleNamespace())
sys.modules.setdefault(
    "hcaptcha_challenger",
    types.SimpleNamespace(),
)
sys.modules.setdefault(
    "hcaptcha_challenger.agent",
    types.SimpleNamespace(AgentV=object),
)
sys.modules.setdefault("loguru", types.SimpleNamespace(logger=NullLogger()))
sys.modules.setdefault("playwright", types.SimpleNamespace())
sys.modules.setdefault(
    "playwright.async_api",
    types.SimpleNamespace(
        Page=object,
        FrameLocator=object,
        TimeoutError=TimeoutError,
        expect=lambda *_args, **_kwargs: None,
    ),
)
sys.modules.setdefault(
    "tenacity",
    types.SimpleNamespace(
        retry=passthrough_decorator,
        retry_if_exception_type=lambda *_args, **_kwargs: None,
        stop_after_attempt=lambda *_args, **_kwargs: None,
    ),
)
sys.modules.setdefault(
    "models",
    types.SimpleNamespace(OrderItem=object, Order=object, PromotionGame=object),
)
sys.modules.setdefault(
    "settings",
    types.SimpleNamespace(settings=types.SimpleNamespace(), RUNTIME_DIR=ROOT),
)

epic_games_service = importlib.import_module("services.epic_games_service")


class FakeApiResponse:
    status = 401
    url = epic_games_service.URL_ORDER_HISTORY


class FakeRequest:
    def __init__(self):
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeApiResponse()


class FakeAccountPage:
    def __init__(self):
        self.url = "about:blank"
        self.closed = False
        self.goto_calls = []

    async def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        self.url = url

    async def wait_for_timeout(self, _timeout):
        return None

    async def evaluate(self, _script, endpoint):
        return {
            "status": 200,
            "url": endpoint,
            "text": '{"orders": [{"orderId": "test-order"}]}',
        }

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self):
        self.request = FakeRequest()
        self.account_page = FakeAccountPage()

    async def new_page(self):
        return self.account_page


class FakeStorePage:
    def __init__(self):
        self.context = FakeContext()


class OrderHistorySessionTests(unittest.TestCase):
    def test_bootstraps_account_origin_after_unauthorized_api_request(self):
        page = FakeStorePage()

        result = asyncio.run(
            epic_games_service.EpicGames.fetch_order_history(page)
        )

        self.assertEqual(result["orders"][0]["orderId"], "test-order")
        self.assertEqual(
            page.context.account_page.goto_calls[0][0],
            epic_games_service.URL_ACCOUNT_TRANSACTIONS,
        )
        self.assertTrue(page.context.account_page.closed)


if __name__ == "__main__":
    unittest.main()
