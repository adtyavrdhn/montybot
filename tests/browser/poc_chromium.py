"""A throwaway Chromium `BrowserBackend`, ported from `ChromiumBrowser` in `poc/sammy_poc/remote.py`, so the browser
service can be tested against a real engine before #11 lands. Replace it with #11's backend then.

Each `open` launches its own headless Chromium, so one run's browser is one process that a test can kill (`pid`).
Refs are not supported: they belong to #13's snapshot.
"""

from __future__ import annotations

import json
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from sammy.browser.contract import (
    Action,
    ActionFailed,
    Click,
    LifecycleError,
    MouseDown,
    MouseMove,
    MouseUp,
    Navigate,
    NotSupported,
    Point,
    Press,
    Ref,
    Screenshot,
    Scroll,
    Selector,
    Snapshot,
    TargetNotFound,
    Type,
)
from sammy.browser.state import BLANK_URL, BrowserState, Cookie

ENGINE = 'chromium (poc)'
WAIT_MS = 2000

# Runs before any page script, so the page sees its sessionStorage as if the tab had never moved.
_SEED_SESSION_STORAGE = """
((seed) => {
  const items = seed[location.origin];
  if (items && sessionStorage.length === 0) {
    for (const [key, value] of Object.entries(items)) sessionStorage.setItem(key, value);
  }
})(%s);
"""
_READ_SESSION_STORAGE = '() => [location.origin, Object.fromEntries(Object.entries(sessionStorage))]'


class PocChromium:
    def __init__(self, playwright: Playwright) -> None:
        self._playwright = playwright
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self.pid: int | None = None
        """The Chromium browser process, while open."""

    @property
    def is_open(self) -> bool:
        return self._page is not None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise LifecycleError('the browser is not open')
        return self._page

    async def open(self, state: BrowserState | None) -> None:
        if self._page is not None:
            raise LifecycleError('the browser is already open')
        browser = await self._playwright.chromium.launch()
        try:
            cdp = await browser.new_browser_cdp_session()
            info = await cdp.send('SystemInfo.getProcessInfo')
            self.pid = next(int(p['id']) for p in info['processInfo'] if p['type'] == 'browser')
            context = await browser.new_context(
                storage_state=_to_playwright(state) if state else None  # pyright: ignore[reportArgumentType]
            )
            if state and state.session_storage:
                await context.add_init_script(script=_SEED_SESSION_STORAGE % json.dumps(state.session_storage))
            page = await context.new_page()
        except BaseException:
            await browser.close()
            raise
        self._browser, self._context, self._page = browser, context, page
        if state is not None and state.url != BLANK_URL:
            await self._guard(page.goto(state.url))

    async def export(self) -> BrowserState:
        page = self.page
        assert self._context is not None
        storage: Any = await self._guard(self._context.storage_state())
        url = page.url
        session: dict[str, dict[str, str]] = {}
        if url.startswith('http'):
            origin, items = await self._guard(page.evaluate(_READ_SESSION_STORAGE))
            if items:
                session = {origin: items}
        return BrowserState(
            url=url,
            cookies=[
                Cookie(
                    name=c['name'],
                    value=c['value'],
                    domain=c['domain'],
                    path=c['path'],
                    expires=c['expires'],
                    http_only=c['httpOnly'],
                    secure=c['secure'],
                    same_site=c['sameSite'],
                )
                for c in storage['cookies']
            ],
            local_storage={
                o['origin']: {i['name']: i['value'] for i in o['localStorage']}
                for o in storage['origins']
                if o['localStorage']
            },
            session_storage=session,
        )

    async def release(self) -> BrowserState:
        state = await self.export()
        await self.close()
        return state

    async def snapshot(self) -> Snapshot:
        page = self.page
        text = await self._guard(page.locator('body').inner_text(timeout=WAIT_MS))
        return Snapshot(url=page.url, title=await self._guard(page.title()), text=text)

    async def act(self, action: Action) -> None:
        page = self.page
        mouse = page.mouse
        match action:
            case Navigate(url=url):
                await self._guard(page.goto(url))
            case Click(target=Point(x=x, y=y)):
                await self._guard(mouse.click(x, y))
            case Click(target=Selector(css=css)):
                await self._guard(page.locator(css).first.click(timeout=WAIT_MS), Selector(css=css))
            case Type(text=text, target=Selector(css=css)):
                field = page.locator(css).first
                await self._guard(field.fill('', timeout=WAIT_MS), Selector(css=css))
                await self._guard(field.press_sequentially(text))
            case Type(text=text, target=None):
                await self._guard(page.keyboard.type(text))
            case Press(key=key, modifiers=modifiers):
                await self._guard(page.keyboard.press('+'.join([*modifiers, key])))
            case Scroll(delta_x=dx, delta_y=dy, at=at):
                size = page.viewport_size or {'width': 1280, 'height': 720}
                point = at or Point(x=size['width'] / 2, y=size['height'] / 2)
                await self._guard(mouse.move(point.x, point.y))
                await self._guard(mouse.wheel(dx, dy))
            case MouseDown(at=at, button=button):
                await self._guard(mouse.move(at.x, at.y))
                await self._guard(mouse.down(button=button))
            case MouseMove(at=at):
                await self._guard(mouse.move(at.x, at.y))
            case MouseUp(at=at, button=button):
                await self._guard(mouse.move(at.x, at.y))
                await self._guard(mouse.up(button=button))
            case Click(target=Ref()) | Type(target=Ref()):
                raise NotSupported('ref', engine=ENGINE)
        await self._guard(page.wait_for_load_state())

    async def screenshot(self) -> Screenshot:
        page = self.page
        png = await self._guard(page.screenshot())
        size = page.viewport_size or {'width': 0, 'height': 0}
        return Screenshot(png=png, width=size['width'], height=size['height'])

    async def close(self) -> None:
        browser = self._browser
        self._browser = self._context = self._page = None
        self.pid = None
        if browser is not None:
            try:
                await browser.close()
            except PlaywrightError:
                pass  # already gone

    async def _guard(self, call: Any, target: Selector | None = None) -> Any:
        """Await a Playwright call, turning its errors into the contract's."""
        try:
            return await call
        except PlaywrightTimeout as error:
            if target is not None:
                raise TargetNotFound(target) from error
            raise ActionFailed(f'{ENGINE} timed out') from error
        except PlaywrightError as error:
            raise ActionFailed(f'{ENGINE} failed: {type(error).__name__}') from error


def _to_playwright(state: BrowserState) -> dict[str, Any]:
    return {
        'cookies': [
            {
                'name': c.name,
                'value': c.value,
                'domain': c.domain,
                'path': c.path,
                'expires': c.expires,
                'httpOnly': c.http_only,
                'secure': c.secure,
                'sameSite': c.same_site,
            }
            for c in state.cookies
        ],
        'origins': [
            {'origin': origin, 'localStorage': [{'name': k, 'value': v} for k, v in items.items()]}
            for origin, items in state.local_storage.items()
        ],
    }
