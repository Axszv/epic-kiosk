"""The calls to the model gateway stay inside its per-minute limit."""

import asyncio
import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("GEMINI_API_KEY", "not_used")
os.environ.setdefault("EPIC_EMAIL", "not@example.test")
os.environ.setdefault("EPIC_PASSWORD", "not-used")
if str(ROOT / "app") not in sys.path:
    sys.path.insert(0, str(ROOT / "app"))

import settings  # noqa: E402


class ModelCallRateLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        settings._model_call_at = 0.0

    async def test_two_calls_are_spaced_out(self):
        original = settings.MODEL_CALL_INTERVAL_SECONDS
        settings.MODEL_CALL_INTERVAL_SECONDS = 0.2
        try:
            await settings.wait_for_model_slot()
            started = asyncio.get_event_loop().time()
            await settings.wait_for_model_slot()
            waited = asyncio.get_event_loop().time() - started
        finally:
            settings.MODEL_CALL_INTERVAL_SECONDS = original

        self.assertGreaterEqual(waited, 0.15)

    async def test_the_limit_can_be_turned_off(self):
        original = settings.MODEL_CALL_INTERVAL_SECONDS
        settings.MODEL_CALL_INTERVAL_SECONDS = 0
        try:
            await settings.wait_for_model_slot()
            started = asyncio.get_event_loop().time()
            await settings.wait_for_model_slot()
            waited = asyncio.get_event_loop().time() - started
        finally:
            settings.MODEL_CALL_INTERVAL_SECONDS = original

        self.assertLess(waited, 0.1)

    def test_the_interval_is_inside_the_free_tier(self):
        # The free tier of the gateway takes about ten requests a minute.
        self.assertGreaterEqual(settings.MODEL_CALL_INTERVAL_SECONDS, 6.0)


if __name__ == "__main__":
    unittest.main()
