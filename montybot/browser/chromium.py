"""`ChromiumBackend`: real Chrome driven by Playwright, behind `BrowserBackend`.

Grown from `ChromiumBrowser` in `poc/montybot_poc/remote.py`. Each `open()` starts a new Chrome with a new, empty
profile folder, and `close()` stops it and deletes the folder, so nothing outlives a session except the
`BrowserState` that `export()` returns. Playwright talks to Chrome over a pipe (`--remote-debugging-pipe`), never a
port.

Chrome is visible by default, because sites spot headless Chrome. Where it draws depends on `ChromiumOptions`:

| Where | Options | Chrome |
|---|---|---|
| A Mac or a Linux desktop | `ChromiumOptions()` | a normal window on your screen |
| The Linux server | `ChromiumOptions.server()` | its own Xvfb screen, inside bwrap (see `chromium_linux`) |
| CI | `ChromiumOptions(headless=True)` | Playwright's headless shell, no window |

`snapshot()` and `Ref` targets come from `SnapshotWalker` (#13): the text format and refs every engine shares, with
typed passwords masked. A click on a ref is a mouse click at the element's centre; typing on a ref replaces its value.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import AsyncGenerator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar, cast

from playwright.async_api import (
    BrowserContext,
    Frame,
    Locator,
    Page,
    Playwright,
    Request,
    StorageState,
    StorageStateCookie,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from montybot.browser.chromium_linux import VirtualScreen, start_virtual_screen, write_bwrap_script
from montybot.browser.contract import (
    Action,
    ActionFailed,
    Click,
    ElementTarget,
    LifecycleError,
    MouseDown,
    MouseMove,
    MouseUp,
    Navigate,
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
from montybot.browser.egress import PROXY_PORT, EgressProxy
from montybot.browser.live import FrameSource
from montybot.browser.snapshot import JSON, SnapshotWalker
from montybot.browser.state import BLANK_URL, BrowserState, Cookie, origin_of

T = TypeVar('T')


@dataclass(frozen=True, kw_only=True)
class ChromiumOptions:
    """How to start Chrome. The defaults suit a Mac or a Linux desktop."""

    headless: bool = False
    """No window: Playwright's headless shell, for CI. Sites can tell, so production runs headed."""
    virtual_screen: bool = False
    """Give each browser its own Xvfb screen (Linux, headed only)."""
    bwrap: bool = False
    """Run Chrome inside bubblewrap with its own profile folder and network namespace (Linux). Its connections go out
    through an `EgressProxy`, which refuses private addresses."""
    allow_private_networks: bool = False
    """With `bwrap`: let pages reach loopback and private addresses. Only for local fixture sites in tests."""
    window_width: int = 1280
    window_height: int = 800
    """The window's outer size in pixels, and the virtual screen's size. The page gets a little less."""
    executable_path: str | None = None
    """The Chrome to run. Default: Playwright's Chromium."""
    xvfb_path: str = 'Xvfb'
    bwrap_path: str = 'bwrap'
    target_timeout: float = 3
    """How long to wait for a selector to match before `TargetNotFound`, in seconds."""
    action_timeout: float = 10
    """How long a click or a fill may wait for its element to be ready, in seconds."""
    navigation_timeout: float = 30
    """How long a page may take to load, in seconds."""
    extra_args: tuple[str, ...] = ()

    @classmethod
    def server(cls, *, headless: bool = False, **overrides: Any) -> ChromiumOptions:
        """The Linux server: bwrap, and an Xvfb screen per browser unless headless."""
        return cls(headless=headless, virtual_screen=not headless, bwrap=True, **overrides)

    @classmethod
    def for_this_machine(cls, *, headless: bool = False, **overrides: Any) -> ChromiumOptions:
        """`server()` on Linux without a desktop, the defaults elsewhere."""
        if sys.platform == 'linux' and not os.environ.get('DISPLAY'):
            return cls.server(headless=headless, **overrides)
        return cls(headless=headless, **overrides)


# Launch flags on top of Playwright's. Dropping `--enable-automation` removes the "controlled by automated test
# software" bar; `AutomationControlled` off makes `navigator.webdriver` false, as in a normal Chrome.
_IGNORED_DEFAULT_ARGS = ['--enable-automation']
_ARGS = ['--disable-blink-features=AutomationControlled', '--window-position=0,0']

# Runs before any page script of a matching origin, so the page sees its sessionStorage at load.
_SEED_SESSION_STORAGE = """((seed) => {
  const items = seed[location.origin];
  if (items) for (const [key, value] of Object.entries(items)) sessionStorage.setItem(key, value);
})(%s);"""
_READ_SESSION_STORAGE = """() => {
  const items = {};
  for (let i = 0; i < sessionStorage.length; i++) {
    const key = sessionStorage.key(i);
    items[key] = sessionStorage.getItem(key);
  }
  return items;
}"""
_VIEWPORT = '() => [innerWidth, innerHeight]'

_NAVIGATION_GRACE = 0.05
"""After an action, how long to wait for it to start a navigation. Chrome starts one while the click is dispatched,
so this only covers the event reaching us."""


@dataclass(kw_only=True)
class _Chrome:
    """One running Chrome and everything started for it."""

    workdir: Path
    context: BrowserContext
    page: Page
    screen: VirtualScreen | None = None
    proxy: EgressProxy | None = None
    failed_url: str = BLANK_URL
    """The last page that failed to load. Chrome shows an error page for it, at `chrome-error://chromewebdata/`."""

    def __post_init__(self) -> None:
        def on_request_failed(request: Request) -> None:
            if request.is_navigation_request() and request.frame == self.page.main_frame:
                self.failed_url = request.url

        self.page.on('requestfailed', on_request_failed)

    @property
    def url(self) -> str:
        """The tab's URL as its address bar shows it: an error page shows the URL that failed."""
        url = self.page.url
        return self.failed_url if url.startswith('chrome-error:') else url

    async def stop(self) -> None:
        with contextlib.suppress(PlaywrightError, TimeoutError):
            await asyncio.wait_for(self.context.close(), 15)
        if self.screen is not None:
            await self.screen.stop()
        if self.proxy is not None:
            await self.proxy.stop()
        shutil.rmtree(self.workdir, ignore_errors=True)


async def _launch(playwright: Playwright, options: ChromiumOptions) -> _Chrome:
    workdir = Path(tempfile.mkdtemp(prefix='montybot-chrome-'))
    profile = workdir / 'profile'
    profile.mkdir()
    screen: VirtualScreen | None = None
    proxy: EgressProxy | None = None
    args = [*_ARGS, f'--window-size={options.window_width},{options.window_height}', *options.extra_args]
    try:
        executable = options.executable_path
        env: dict[str, str | float | bool] | None = None
        if options.virtual_screen and not options.headless:
            try:
                screen = await start_virtual_screen(
                    workdir=workdir, width=options.window_width, height=options.window_height, xvfb=options.xvfb_path
                )
            except (OSError, RuntimeError) as error:
                raise ActionFailed(f'could not start a virtual screen: {error}') from error
            env = {**os.environ, 'DISPLAY': screen.display.name, 'XAUTHORITY': str(screen.display.xauthority)}
        if options.bwrap:
            if shutil.which(options.bwrap_path) is None:
                raise ActionFailed(f'could not start Chrome: {options.bwrap_path} not found')
            proxy = EgressProxy(workdir / 'egress.sock', allow_private=options.allow_private_networks)
            await proxy.start()
            # Every connection through the proxy, loopback too, which Chrome would otherwise connect to directly.
            args += [f'--proxy-server=socks5://127.0.0.1:{PROXY_PORT}', '--proxy-bypass-list=<-loopback>']
            executable = str(
                write_bwrap_script(
                    path=workdir / 'chrome-in-bwrap',
                    chrome=Path(executable or playwright.chromium.executable_path),
                    profile=profile,
                    display=screen.display if screen else None,
                    proxy=proxy.path,
                    bwrap=options.bwrap_path,
                )
            )
        context = await playwright.chromium.launch_persistent_context(
            profile,
            executable_path=executable,
            headless=options.headless,
            chromium_sandbox=True,
            ignore_default_args=_IGNORED_DEFAULT_ARGS,
            args=args,
            no_viewport=True,
            accept_downloads=False,
            env=env,
            timeout=options.navigation_timeout * 1000,
        )
    except BaseException:
        if screen is not None:
            await screen.stop()
        if proxy is not None:
            await proxy.stop()
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    context.set_default_timeout(options.action_timeout * 1000)
    context.set_default_navigation_timeout(options.navigation_timeout * 1000)
    page = context.pages[0] if context.pages else await context.new_page()
    return _Chrome(workdir=workdir, context=context, page=page, screen=screen, proxy=proxy)


class ChromiumBackend:
    """A `BrowserBackend` running Chrome through Playwright. One Chrome, one tab, per `open()`.

    `playwright` is the caller's running Playwright (`async with async_playwright() as playwright`), shared by every
    backend in the process; the backend never stops it.
    """

    def __init__(self, *, playwright: Playwright, options: ChromiumOptions | None = None) -> None:
        self.playwright = playwright
        self.options = options or ChromiumOptions()
        self._chrome: _Chrome | None = None
        self._walker = SnapshotWalker(run_script=self._evaluate)

    @property
    def workdir(self) -> Path | None:
        """The folder holding this browser's profile and launch files, while open. Every process started for the
        browser has it on its command line, which is how the measurements find them."""
        return self._chrome.workdir if self._chrome else None

    # --- LiveViewBackend (#14) ---

    async def live_view(self) -> FrameSource:
        """A CDP screencast of the tab, with input dispatched to it."""
        from montybot.liveview.chromium import CdpFrameSource

        return await CdpFrameSource.start(self._require_open().page)

    # --- BrowserBackend ---

    async def open(self, state: BrowserState | None) -> None:
        if self._chrome is not None:
            raise LifecycleError('the browser is already open')
        try:
            chrome = await _launch(self.playwright, self.options)
        except PlaywrightError as error:
            raise ActionFailed(f'could not start Chrome: {_first_line(error)}') from error
        self._chrome = chrome
        self._walker = SnapshotWalker(run_script=self._evaluate)
        if state is None:
            return
        async with contextlib.AsyncExitStack() as stack:
            try:
                await chrome.context.set_storage_state(_to_playwright(state))
                if state.session_storage:
                    # Only while the first page loads: later loads keep what the pages left in sessionStorage.
                    script = _SEED_SESSION_STORAGE % json.dumps(state.session_storage)
                    await stack.enter_async_context(await chrome.context.add_init_script(script=script))
            except PlaywrightError:
                await self.close()
                # Playwright's message may quote a cookie, so it is left out.
                raise ActionFailed('could not seed the browser state: Chrome rejected it') from None
            except BaseException:
                await self.close()
                raise
            if state.url != BLANK_URL:
                await self._goto(state.url)

    async def export(self) -> BrowserState:
        chrome = self._require_open()
        try:
            storage = await chrome.context.storage_state()
            url = chrome.url
            session: dict[str, str] = {}
            origin = origin_of(url)
            if origin is not None and origin == origin_of(chrome.page.url):  # not on an error page
                session = cast(dict[str, str], await self._evaluate(_READ_SESSION_STORAGE))
        except PlaywrightError as error:
            raise ActionFailed(f'could not read the browser state: {_first_line(error)}') from error
        return BrowserState(
            url=url,
            cookies=[_from_playwright(c) for c in storage.get('cookies', [])],
            local_storage={
                o['origin']: {item['name']: item['value'] for item in o['localStorage']}
                for o in storage.get('origins', [])
                if o['localStorage']
            },
            session_storage={origin: session} if origin and session else {},
        )

    async def release(self) -> BrowserState:
        state = await self.export()
        await self.close()
        return state

    async def snapshot(self) -> Snapshot:
        chrome = self._require_open()
        try:
            snapshot = await self._walker.snapshot()
        except PlaywrightError as error:
            raise ActionFailed(f'could not read the page: {_first_line(error)}') from error
        return Snapshot(url=chrome.url, title=snapshot.title, text=snapshot.text)

    async def act(self, action: Action) -> None:
        chrome = self._require_open()
        page = chrome.page
        try:
            resolved = await self._walker.resolve(action)  # a ref becomes a point to click, or typing at the caret
            match resolved:
                case None:
                    return  # the walker already did it, such as choosing a select's option
                case Navigate(url=url):
                    await self._goto(url)
                case Click(target=target):
                    if isinstance(target, Point):
                        async with self._settled(page):
                            await page.mouse.click(target.x, target.y)
                    else:
                        locator = await self._locate(target)
                        async with self._settled(page):
                            await locator.click()
                case Type(text=text, target=target):
                    locator = None if target is None else await self._locate(target)
                    async with self._settled(page):
                        if locator is not None:
                            await locator.fill('')  # focus it and clear it
                        await page.keyboard.type(text)
                case Press(key=key, modifiers=modifiers):
                    async with self._settled(page):
                        await self._press(page, key, modifiers)
                case Scroll(delta_x=dx, delta_y=dy, at=at):
                    point = at or await self._centre()
                    await page.mouse.move(point.x, point.y)
                    await page.mouse.wheel(dx, dy)
                case MouseDown(at=at, button=button):
                    await page.mouse.move(at.x, at.y)
                    await page.mouse.down(button=button)
                case MouseMove(at=at):
                    await page.mouse.move(at.x, at.y)
                case MouseUp(at=at, button=button):
                    async with self._settled(page):
                        await page.mouse.move(at.x, at.y)
                        await page.mouse.up(button=button)
        except PlaywrightTimeoutError as error:
            raise ActionFailed(f'{action.kind} timed out: {_first_line(error)}') from error
        except PlaywrightError as error:
            raise ActionFailed(f'{action.kind} failed: {_first_line(error)}') from error

    async def screenshot(self) -> Screenshot:
        chrome = self._require_open()
        try:
            png = await chrome.page.screenshot(type='png')
            width, height = cast(tuple[int, int], await self._evaluate(_VIEWPORT))
        except PlaywrightError as error:
            raise ActionFailed(f'could not take a screenshot: {_first_line(error)}') from error
        return Screenshot(png=png, width=width, height=height)

    async def close(self) -> None:
        chrome, self._chrome = self._chrome, None
        if chrome is not None:
            await chrome.stop()

    # --- internals ---

    def _require_open(self) -> _Chrome:
        if self._chrome is None:
            raise LifecycleError('the browser is not open')
        return self._chrome

    async def _goto(self, url: str) -> None:
        page = self._require_open().page
        error_page = asyncio.Event()

        def on_frame(frame: Frame) -> None:
            if frame == page.main_frame and frame.url.startswith('chrome-error:'):
                error_page.set()

        page.on('framenavigated', on_frame)
        try:
            await page.goto(url, wait_until='load')
        except PlaywrightTimeoutError as error:
            raise ActionFailed(f'{url} did not finish loading in {self.options.navigation_timeout:g} s') from error
        except PlaywrightError as error:
            reason = _net_error(error)
            shows_error_page = reason.startswith('net::') and reason != 'net::ERR_ABORTED'
            # Let Chrome's error page load, or it interrupts the next navigation.
            if shows_error_page and await _wait(error_page.wait(), 2):
                with contextlib.suppress(PlaywrightError):
                    await page.wait_for_load_state('load', timeout=2000)
            raise ActionFailed(f'could not load {url}: {reason}') from error
        finally:
            page.remove_listener('framenavigated', on_frame)

    async def _evaluate(self, script: str, arg: JSON = None) -> Any:
        """`page.evaluate`, retried while a navigation replaces the document under it."""
        page = self._require_open().page
        for attempt in range(3):
            try:
                return await page.evaluate(script, arg)
            except PlaywrightError as error:
                if attempt == 2 or not _is_navigation_race(error):
                    raise
                with contextlib.suppress(PlaywrightError):
                    await page.wait_for_load_state('domcontentloaded')
        raise AssertionError('unreachable')

    async def _locate(self, target: ElementTarget) -> Locator:
        match target:
            case Ref():
                raise AssertionError('refs are resolved by the snapshot walker before this')
            case Selector(css=css):
                locator = self._require_open().page.locator(f'css={css}').first
                try:
                    await locator.wait_for(state='attached', timeout=self.options.target_timeout * 1000)
                except PlaywrightTimeoutError as error:
                    raise TargetNotFound(target) from error
                except PlaywrightError as error:
                    raise TargetNotFound(target, 'not a valid CSS selector') from error
                return locator

    async def _centre(self) -> Point:
        width, height = cast(tuple[int, int], await self._evaluate(_VIEWPORT))
        return Point(x=width / 2, y=height / 2)

    @staticmethod
    async def _press(page: Page, key: str, modifiers: tuple[str, ...]) -> None:
        held: list[str] = []
        try:
            for modifier in modifiers:
                await page.keyboard.down(modifier)
                held.append(modifier)
            await page.keyboard.press(key)
        finally:
            for modifier in reversed(held):
                await page.keyboard.up(modifier)

    @asynccontextmanager
    async def _settled(self, page: Page) -> AsyncGenerator[None]:
        """Run the body, then, if it started a main-frame navigation, wait until the new page has loaded."""
        started = asyncio.Event()
        committed = asyncio.Event()
        ended = asyncio.Event()
        navigations: list[Request] = []

        def on_request(request: Request) -> None:
            if request.is_navigation_request() and request.frame == page.main_frame:
                navigations.append(request)
                started.set()

        def on_request_end(request: Request) -> None:
            if navigations and request is navigations[-1] and request.redirected_to is None:
                ended.set()  # failed, or finished without a new document (a 204 or a download)

        def on_frame(frame: Frame) -> None:
            if frame == page.main_frame:
                committed.set()

        page.on('request', on_request)
        page.on('requestfailed', on_request_end)
        page.on('requestfinished', on_request_end)
        page.on('framenavigated', on_frame)
        try:
            yield
            if not await _wait(started.wait(), _NAVIGATION_GRACE):
                return
            timeout = self.options.navigation_timeout
            await _wait(_first(committed.wait(), _after(ended.wait(), 1)), timeout)
            if committed.is_set():
                await page.wait_for_load_state('load', timeout=timeout * 1000)
        finally:
            page.remove_listener('request', on_request)
            page.remove_listener('requestfailed', on_request_end)
            page.remove_listener('requestfinished', on_request_end)
            page.remove_listener('framenavigated', on_frame)


async def _wait(awaitable: Awaitable[T], timeout: float) -> bool:
    """Wait up to `timeout` seconds; True if it finished."""
    try:
        await asyncio.wait_for(awaitable, timeout)
    except TimeoutError:
        return False
    return True


async def _after(awaitable: Awaitable[object], delay: float) -> None:
    await awaitable
    await asyncio.sleep(delay)


async def _first(*awaitables: Awaitable[object]) -> None:
    tasks = [asyncio.ensure_future(a) for a in awaitables]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()


def _to_playwright(state: BrowserState) -> StorageState:
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


def _from_playwright(cookie: StorageStateCookie) -> Cookie:
    return Cookie(
        name=cookie.get('name', ''),
        value=cookie.get('value', ''),
        domain=cookie.get('domain', ''),
        path=cookie.get('path', '/'),
        expires=cookie.get('expires', -1),
        http_only=cookie.get('httpOnly', False),
        secure=cookie.get('secure', False),
        same_site=cookie.get('sameSite', 'Lax'),
    )


def _first_line(error: Exception) -> str:
    """Playwright's message without its call log, which can name selectors but never values."""
    return str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__


def _net_error(error: Exception) -> str:
    message = _first_line(error)
    return next((word for word in message.split() if word.startswith('net::')), message)


def _is_navigation_race(error: Exception) -> bool:
    message = str(error)
    return 'Execution context was destroyed' in message or 'Cannot find context' in message
