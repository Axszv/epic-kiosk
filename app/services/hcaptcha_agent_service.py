"""Page-scoped hCaptcha agent lifecycle."""

from contextlib import suppress
from urllib.parse import urlparse
from weakref import WeakKeyDictionary

from hcaptcha_challenger.agent import AgentV
from loguru import logger
from playwright.async_api import Page

from settings import settings


async def load_hsw_script(page: Page, url: str) -> str:
    """Load hsw.js without Camoufox/Juggler response-text decoding."""
    target = urlparse(url)
    for frame in page.frames:
        current = urlparse(frame.url)
        if (current.scheme, current.netloc) != (target.scheme, target.netloc):
            continue
        with suppress(Exception):
            return await frame.evaluate(
                """async (url) => {
                    const response = await fetch(url, {
                        credentials: 'include',
                        cache: 'no-store'
                    });
                    if (!response.ok) throw new Error(`hsw fetch ${response.status}`);
                    return await response.text();
                }""",
                url,
            )

    response = await page.context.request.get(
        url,
        headers={"Accept-Encoding": "identity"},
    )
    if not response.ok:
        raise RuntimeError(f"hsw request returned HTTP {response.status}")
    return await response.text()


class EpicAgentV(AgentV):
    """AgentV with a Camoufox-safe hsw.js loader."""

    async def _task_handler(self, response):
        if not response.url.endswith("/hsw.js"):
            return await super()._task_handler(response)

        try:
            hsw_text = await load_hsw_script(self.page, response.url)
            await self.page.evaluate(hsw_text)
            injected = await self.page.evaluate("typeof hsw === 'function'")
            if not injected:
                raise RuntimeError("hsw function was not installed")
            logger.debug("Injected hsw.js through the resilient loader")
        except Exception as err:
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
