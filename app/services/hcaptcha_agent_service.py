"""Page-scoped hCaptcha agent lifecycle."""

from weakref import WeakKeyDictionary

from hcaptcha_challenger.agent import AgentV
from playwright.async_api import Page

from settings import settings


_AGENTS: WeakKeyDictionary[Page, AgentV] = WeakKeyDictionary()


def get_hcaptcha_agent(page: Page) -> AgentV:
    """Return the one hCaptcha agent attached to a browser page.

    AgentV registers a response listener when constructed. Recreating it for
    every checkout leaves old listeners active and lets multiple agents race
    over the same hCaptcha payload, so its lifetime must match the page.
    """
    agent = _AGENTS.get(page)
    if agent is None:
        agent = AgentV(page=page, agent_config=settings)
        _AGENTS[page] = agent
    return agent
