"""Page-scoped hCaptcha agent lifecycle."""

import asyncio
from weakref import WeakKeyDictionary

from hcaptcha_challenger.agent import AgentV
from loguru import logger
from playwright.async_api import Page

from settings import settings


async def load_hsw_script(page: Page, url: str) -> str:
    """Load hsw.js without Camoufox/Juggler response-text decoding."""
    response = await page.context.request.get(
        url,
        headers={
            "Accept-Encoding": "identity",
            "Referer": page.url,
        },
        timeout=10000,
    )
    if not response.ok:
        raise RuntimeError(f"hsw request returned HTTP {response.status}")
    return await response.text()


class EpicAgentV(AgentV):
    """AgentV with a Camoufox-safe hsw.js loader."""

    def __init__(self, *args, **kwargs):
        self._hsw_lock = asyncio.Lock()
        self._hsw_document_key = ""
        super().__init__(*args, **kwargs)

    async def _task_handler(self, response):
        if not response.url.endswith("/hsw.js"):
            return await super()._task_handler(response)

        document_key = self.page.url
        async with self._hsw_lock:
            if document_key == self._hsw_document_key:
                return
            try:
                hsw_text = await load_hsw_script(self.page, response.url)
                await self.page.evaluate(hsw_text)
                injected = await self.page.evaluate("typeof hsw === 'function'")
                if not injected:
                    raise RuntimeError("hsw function was not installed")
                self._hsw_document_key = document_key
                logger.debug("Injected hsw.js through the resilient loader")
            except Exception as err:
                # Mark this document as attempted so a burst of identical hsw
                # responses cannot create a new task for every response.
                self._hsw_document_key = document_key
                logger.warning(f"Resilient hsw.js injection failed: {err}")


_AGENTS: WeakKeyDictionary[Page, AgentV] = WeakKeyDictionary()


def get_hcaptcha_agent(page: Page) -> AgentV:
    """Return the one hCaptcha agent attached to a browser page.

    AgentV registers a response listener when constructed. Recreating it for
    every checkout leaves old listeners active and lets multiple agents race
    over the same hCaptcha payload, so its lifetime must match the page.
    """
    agent = _AGENTS.get(page)
    if agent is None:
        agent = EpicAgentV(page=page, agent_config=settings)
        _AGENTS[page] = agent
    return agent
