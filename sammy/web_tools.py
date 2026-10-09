"""`web_search` and `web_fetch`: looking things up and reading pages without opening the browser.

They are Pydantic AI's `WebSearch` and `WebFetch` capabilities. A model with web tools of its own (Anthropic, OpenAI,
Google and others) searches and fetches on its provider's side, unless `NATIVE_WEB_TOOLS` is off. Otherwise the local
tools here do it, through `sammy.web`: each is one DBOS step (a recovered run gets back what it read, not a new
read), with a `web.search` or `web.fetch` span and the user's activity log saying what it does. Only the host of a
page goes into the trace (`web.site`), as for the browser. `web_search` needs `TAVILY_API_KEY`; without it, a model
with no search of its own has no `web_search` and searches in its browser.
"""

from __future__ import annotations

from dbos import DBOS
from opentelemetry import trace
from pydantic_ai import RunContext, Tool, ToolDefinition, ToolFailed
from pydantic_ai.capabilities import AbstractCapability, WebFetch, WebSearch
from pydantic_ai.native_tools import WebFetchTool, WebSearchTool

from sammy import store, web
from sammy.deps import RunDeps
from sammy.observability import timed
from sammy.resources import current

INSTRUCTIONS = """\
Look things up with `web_search` (when you have it) and read pages with `web_fetch`: they are quick and cheap, and do
not open your browser. Use them for questions such as opening hours, prices, news or docs. Use the browser
(`run_code`) when a page needs the user's sign-in, clicks, a form or its scripts to show what you need (a `web_fetch`
that comes back empty or broken means that), and for doing anything on a site."""

DEFAULT_CHARS = 20_000


async def activity(run_id: str, text: str) -> None:
    async with current().pool.connection() as connection:
        await store.add_activity(connection, run_id, text)


@timed('web.search')
async def web_search(ctx: RunContext[RunDeps], query: str, max_results: int = 5) -> str:
    """Search the web. Returns titles, addresses and short extracts; read a result in full with `web_fetch`.

    Args:
        query: What to search for, as you would type it into a search engine.
        max_results: How many results, 1 to 10.
    """
    run_id = ctx.deps.run_id

    async def step() -> tuple[bool, str]:
        settings = current().settings
        key = settings.tavily_api_key
        if key is None:  # only offered with a key (`searchable`)
            return False, 'Web search is not set up. Search in your browser instead.'
        await activity(run_id, f'Searching the web for {query!r}')
        try:
            found = await web.search(
                query, max_results=max_results, api_key=key.get_secret_value(), url=settings.tavily_url
            )
        except web.WebError as error:
            return False, str(error)
        return True, web.shown_results(query, found)

    done, text = await DBOS.run_step_async({'name': 'web.search'}, step)
    if not done:
        raise ToolFailed(text)
    return text


async def searchable(ctx: RunContext[RunDeps], tool: ToolDefinition) -> ToolDefinition | None:
    """`web_search` only with a search API key."""
    return tool if ctx.deps.resources.settings.tavily_api_key is not None else None


@timed('web.fetch')
async def web_fetch(ctx: RunContext[RunDeps], url: str, max_chars: int = DEFAULT_CHARS) -> str:
    """Read a web page as text, with its links, without opening the browser.

    Args:
        url: The page's address, http or https.
        max_chars: The most characters of text to return, up to 100000.
    """
    run_id = ctx.deps.run_id
    trace.get_current_span().set_attribute('web.site', web.host_of(str(url)))  # the host only, never the URL

    async def step() -> tuple[bool, str]:
        await activity(run_id, f'Reading {web.host_of(str(url))}')
        try:
            page = await web.fetch(str(url), allow_private=current().settings.allow_private_networks)
        except web.WebError as error:
            return False, str(error)
        return True, page.shown(max_chars)

    done, text = await DBOS.run_step_async({'name': 'web.fetch'}, step)
    if not done:
        raise ToolFailed(text)
    return text


def native_search(ctx: RunContext[RunDeps]) -> WebSearchTool | None:
    # Optional: a model without it, and without a search key for the local tool, simply has no web search.
    return WebSearchTool(optional=True) if ctx.deps.resources.settings.native_web_tools else None


def native_fetch(ctx: RunContext[RunDeps]) -> WebFetchTool | None:
    return WebFetchTool() if ctx.deps.resources.settings.native_web_tools else None


def web_capabilities() -> list[AbstractCapability[RunDeps]]:
    return [
        WebSearch[RunDeps](native=native_search, local=Tool(web_search, prepare=searchable)),
        WebFetch[RunDeps](native=native_fetch, local=Tool(web_fetch)),
    ]
