"""Moving `BrowserState` in and out of a Playwright browser context.

Both sides use this: the remote (headless) browser and the user's local window.
"""

from __future__ import annotations

import json
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page

from montybot_poc.state import BrowserState

# Runs before any page script, so the site sees its sessionStorage as if the tab had never moved.
_SEED_SESSION_STORAGE = """
((seed) => {
  const items = seed[location.origin];
  if (items && sessionStorage.length === 0) {
    for (const [key, value] of Object.entries(items)) sessionStorage.setItem(key, value);
  }
})(%s);
"""

_READ_SESSION_STORAGE = '() => ({[location.origin]: Object.fromEntries(Object.entries(sessionStorage))})'


async def seeded_context(browser: Browser, state: BrowserState | None, **context_options: Any) -> BrowserContext:
    """A fresh context with `state`'s cookies, localStorage and sessionStorage, and no page yet."""
    context = await browser.new_context(
        storage_state=state.to_playwright() if state else None,  # pyright: ignore[reportArgumentType]
        **context_options,
    )
    if state and state.session_storage:
        await context.add_init_script(script=_SEED_SESSION_STORAGE % json.dumps(state.session_storage))
    return context


async def open_page(context: BrowserContext, state: BrowserState | None) -> Page:
    """A new page in `context`, on `state.url` when there is one."""
    page = await context.new_page()
    if state and state.url.startswith('http'):
        await page.goto(state.url)
    return page


async def export_state(context: BrowserContext, page: Page | None) -> BrowserState:
    """Cookies (HttpOnly included) and localStorage for the context, sessionStorage for `page`."""
    storage = await context.storage_state()
    session: dict[str, dict[str, str]] = {}
    url = 'about:blank'
    if page is not None and not page.is_closed():
        url = page.url
        if url.startswith('http'):
            session = await page.evaluate(_READ_SESSION_STORAGE)
    return BrowserState.from_playwright(dict(storage), url=url, session_storage=session)
