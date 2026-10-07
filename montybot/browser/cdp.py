"""`ChromiumCDPBackend`: Chrome behind the browser contract, driven by our own CDP client (`cdp_client.py`), with no
Playwright at runtime. See `cdp.md` next to this file for the evaluation.

- **One Chrome per `open`**, with a throwaway profile folder. We start it ourselves and talk to it over
  `--remote-debugging-pipe` (fds 3 and 4): no port, and no driver process in between. `close` kills the process group
  and deletes the folder.
- **Few automation tells.** No `--enable-automation`, `AutomationControlled` off (so `navigator.webdriver` is false),
  and the page's Runtime domain is never enabled. Every script we run (the snapshot walker, storage reads and writes)
  runs in an isolated world (`Page.createIsolatedWorld`), so the page's own scripts cannot see it or its globals.
- **Input is trusted:** `Input.dispatchMouseEvent`, `dispatchKeyEvent` and `insertText`, the same events a real mouse
  and keyboard make.
- **State in and out:** `Storage.getCookies` and `setCookies` (HttpOnly included); localStorage of origins other than
  the current one through a hidden tab whose requests are answered by us (`Fetch`), so no request reaches the site;
  sessionStorage through a script that runs before the page's own, in the isolated world.
- **Snapshots and refs** come from `SnapshotWalker` (#13), the same text and refs as the other engines.
- **Live view** (`live_view()`): `Page.startScreencast`, in `montybot.liveview.cdp`. **Downloads**:
  `Browser.setDownloadBehavior` into the profile folder.
- **On the Linux server** (`CDPOptions.server()`), Chrome is headed on its own Xvfb screen, inside the same bwrap jail
  as the Playwright backend (`chromium_linux`), whose only way out is the `EgressProxy`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import platform
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar, cast

from montybot.browser.cdp_client import CDPClosed, CDPConnection, CDPError, CDPParams
from montybot.browser.chromium_linux import Display, VirtualScreen, bwrap_command, start_virtual_screen
from montybot.browser.contract import (
    MAX_DOWNLOAD_BYTES,
    Action,
    ActionFailed,
    Click,
    Download,
    LifecycleError,
    Modifier,
    MouseButton,
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
from montybot.browser.egress import PROXY_PORT, EgressProxy, proxy_answers
from montybot.browser.live import FrameSource
from montybot.browser.snapshot import JSON, SnapshotWalker
from montybot.browser.state import BLANK_URL, BrowserState, Cookie, SameSite, origin_of
from montybot.liveview.keys import MODIFIER_BITS, Key, key_for

ENGINE = 'chromium-cdp'
WORLD = 'montybot'
"""The isolated world our scripts run in. Page scripts cannot reach it."""

# Launch flags. Kept short on purpose: each one a page could notice is a reason to be blocked.
_ARGS = (
    '--remote-debugging-pipe',
    '--no-first-run',
    '--no-default-browser-check',
    # `navigator.webdriver` false, as in a normal Chrome. The pipe alone would turn it on.
    '--disable-blink-features=AutomationControlled',
    '--password-store=basic',
    '--use-mock-keychain',
    # A tab under a popup, or a window nobody looks at (Xvfb), keeps running its timers and painting.
    '--disable-background-timer-throttling',
    '--disable-backgrounding-occluded-windows',
    '--disable-renderer-backgrounding',
    # Take input before a new page's first frame is on screen. Without it, a window on Xvfb dropped the first key
    # presses after a load. Playwright passes it too.
    '--allow-pre-commit-input',
    # Nothing Chrome would fetch on its own (component updates, sync, phishing lists) goes out through the proxy, and
    # no background extension or service runs in its own process. No page can see these.
    '--disable-background-networking',
    '--disable-component-update',
    '--disable-component-extensions-with-background-pages',
    '--disable-default-apps',
    '--disable-sync',
    '--disable-client-side-phishing-detection',
    '--disable-breakpad',
    '--metrics-recording-only',
    '--no-service-autorun',
    '--disable-search-engine-choice-screen',
    # Chrome for Testing's built-in experiments off, so it behaves like a default Chrome. Playwright does the same.
    '--disable-field-trial-config',
    '--disable-features=Translate,MediaRouter,DialMediaRouteProvider',
    # WebGL in software where there is no GPU (Xvfb), as a normal Chrome would have it.
    '--enable-unsafe-swiftshader',
    '--window-position=0,0',
)

_VIEWPORT = '() => [innerWidth, innerHeight]'
_READ_STORAGE = (
    '(kind) => { const s = window[kind]; return Object.fromEntries(Object.keys(s).map((k) => [k, s.getItem(k)])); }'
)
_WRITE_STORAGE = '(items) => { for (const [k, v] of Object.entries(items)) localStorage.setItem(k, v); }'
_SEED_SESSION_STORAGE = """((seed) => {
  const items = seed[location.origin];
  if (items) for (const [key, value] of Object.entries(items)) sessionStorage.setItem(key, value);
})(%s);"""
_FIND = """(request) => {
  let element;
  try { element = document.querySelector(request.css); } catch (e) { return {error: 'invalid'}; }
  if (!element) return {error: 'missing'};
  if (element.scrollIntoViewIfNeeded) element.scrollIntoViewIfNeeded(true); else element.scrollIntoView({block: 'center'});
  const box = element.getBoundingClientRect();
  if (!box.width || !box.height || getComputedStyle(element).visibility === 'hidden') return {error: 'hidden'};
  if (request.type) {
    const editable = element.isContentEditable || element.matches('input, textarea');
    if (!editable || element.disabled || element.readOnly) return {error: 'not-editable'};
    element.focus();
    if (element.select) element.select(); else getSelection().selectAllChildren(element);
    document.execCommand('delete');
  }
  return {x: box.left + box.width / 2, y: box.top + box.height / 2};
}"""
"""Find `css`, scroll it into view, and return its centre. For typing, also focus it and clear it, as the user would
by selecting everything and pressing Delete."""

_NAVIGATION_GRACE = 0.05
"""After an action, how long to wait for it to start a navigation."""
_BUTTON_BITS: dict[MouseButton, int] = {'left': 1, 'right': 2, 'middle': 4}
_TEXT_SUPPRESSING = MODIFIER_BITS['Alt'] | MODIFIER_BITS['Control'] | MODIFIER_BITS['Meta']
_SIDE_PAGE = base64.b64encode(b'<!doctype html><title></title>').decode()


def playwright_chromium() -> Path:
    """Where Playwright keeps its Chromium (a Chrome for Testing build), found without starting Playwright."""
    import playwright

    browsers = json.loads(
        (Path(playwright.__file__).parent / 'driver/package/browsers.json').read_text(encoding='utf-8')
    )
    revision = next(b['revision'] for b in browsers['browsers'] if b['name'] == 'chromium')
    root = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
    if root in (None, '', '0'):
        base = Path.home() / ('Library/Caches' if sys.platform == 'darwin' else '.cache') / 'ms-playwright'
    else:
        base = Path(root)
    folder = base / f'chromium-{revision}'
    if sys.platform == 'darwin':
        arch = 'arm64' if platform.machine() == 'arm64' else 'x64'
        app = 'Google Chrome for Testing'
        return folder / f'chrome-mac-{arch}/{app}.app/Contents/MacOS/{app}'
    for name in ('chrome-linux64', 'chrome-linux'):
        if (folder / name / 'chrome').exists():
            return folder / name / 'chrome'
    return folder / 'chrome-linux64/chrome'


def default_executable() -> Path:
    """`$MONTYBOT_CHROME_BINARY` (such as a pinned Chrome for Testing), else Playwright's Chromium."""
    if env := os.environ.get('MONTYBOT_CHROME_BINARY'):
        return Path(env)
    return playwright_chromium()


@dataclass(frozen=True, kw_only=True)
class CDPOptions:
    """How to start Chrome. The defaults suit a Mac or a Linux desktop: a normal window."""

    executable: Path = field(default_factory=default_executable)
    headless: bool = False
    """Chrome's own headless mode, for CI. Sites can tell (the user agent says HeadlessChrome), so the server runs
    headed."""
    virtual_screen: bool = False
    """Give each browser its own Xvfb screen (Linux, headed only)."""
    bwrap: bool = False
    """Run Chrome inside bubblewrap with its own profile folder and network namespace (Linux). Its connections go out
    through an `EgressProxy`, which refuses private addresses."""
    allow_private_networks: bool = False
    """With `bwrap`: let pages reach loopback and private addresses. Only for local fixture sites in tests."""
    egress_socket: Path | None = None
    """A shared egress proxy in a separate container. Unset: start a private proxy for this browser (local tests)."""
    window_width: int = 1280
    window_height: int = 800
    """The window's outer size in pixels, and the virtual screen's size. The page gets a little less."""
    xvfb_path: str = 'Xvfb'
    bwrap_path: str = 'bwrap'
    start_timeout: float = 20
    target_timeout: float = 3
    """How long to wait for a selector to match before `TargetNotFound`, in seconds."""
    navigation_timeout: float = 30
    """How long a page may take to load, in seconds."""
    extra_args: tuple[str, ...] = ()
    software_webgl: bool = True
    """Pass `--enable-unsafe-swiftshader`, for WebGL with no GPU. A browser that reports a GPU of its own, such as
    CloakBrowser (`cloak.py`), goes without it."""

    @classmethod
    def server(cls, *, egress_socket: Path | None = None, headless: bool = False) -> CDPOptions:
        """The Linux server: bwrap, and an Xvfb screen per browser unless headless."""
        return cls(headless=headless, virtual_screen=not headless, bwrap=True, egress_socket=egress_socket)

    def command(self, *, profile: Path, display: Display | None = None, proxy: Path | None = None) -> list[str]:
        """Chrome's command line; with `bwrap`, wrapped in the jail, with `proxy` the egress proxy's socket."""
        argv = [
            str(self.executable),
            f'--user-data-dir={profile}',
            *(arg for arg in _ARGS if self.software_webgl or arg != '--enable-unsafe-swiftshader'),
            f'--window-size={self.window_width},{self.window_height}',
        ]
        if self.headless:
            argv.append('--headless')
        if not self.bwrap:
            return [*argv, *self.extra_args, BLANK_URL]
        if proxy is None:
            raise ValueError('a jailed Chrome needs its egress proxy')
        # Every connection through the proxy, loopback too, which Chrome would otherwise connect to directly.
        argv += [f'--proxy-server=socks5://127.0.0.1:{PROXY_PORT}', '--proxy-bypass-list=<-loopback>']
        argv += [*self.extra_args, BLANK_URL]
        jail = bwrap_command(
            chrome=self.executable,
            profile=profile,
            display=display,
            proxy=proxy,
            proxy_directory=proxy.parent if self.egress_socket is not None else None,
            bwrap=self.bwrap_path,
        )
        return [*jail, *argv[1:]]


# --- input, shared with the live view ---


class CDPInput:
    """Trusted mouse and keyboard input into one attached tab. Remembers held buttons and where the mouse is."""

    def __init__(self, connection: CDPConnection, session: str) -> None:
        self._connection = connection
        self._session = session
        self.buttons: list[MouseButton] = []
        self.pointer = Point(x=0, y=0)

    async def mouse(self, kind: str, at: Point, button: MouseButton | None = None) -> None:
        """`mousePressed`, `mouseMoved` or `mouseReleased` at `at`. A press or release changes `buttons`."""
        if kind == 'mousePressed' and button is not None and button not in self.buttons:
            self.buttons.append(button)
        if kind == 'mouseReleased' and button in self.buttons:
            self.buttons.remove(button)
        self.pointer = at
        held = sum(_BUTTON_BITS[b] for b in self.buttons)
        which = button or (self.buttons[0] if self.buttons else 'none')
        params = {
            'type': kind,
            'x': at.x,
            'y': at.y,
            'button': which,
            'buttons': held,
            'clickCount': 0 if kind == 'mouseMoved' else 1,
        }
        await self._send('Input.dispatchMouseEvent', params)

    async def click(self, at: Point) -> None:
        await self.mouse('mouseMoved', at)
        await self.mouse('mousePressed', at, 'left')
        await self.mouse('mouseReleased', at, 'left')

    async def wheel(self, at: Point, delta_x: float, delta_y: float) -> None:
        await self.mouse('mouseMoved', at)
        params = {'type': 'mouseWheel', 'x': at.x, 'y': at.y, 'deltaX': delta_x, 'deltaY': delta_y}
        await self._send('Input.dispatchMouseEvent', params)

    async def press(self, name: str, modifiers: Sequence[Modifier] = ()) -> None:
        """One key, with `modifiers` held down around it. An unknown key name raises `ActionFailed`."""
        key = key_for(name)
        if key is None:
            raise ActionFailed(f'unknown key {name!r}: use a KeyboardEvent.key value such as Enter, ArrowDown or a')
        held: list[Key] = []
        bits = 0
        try:
            for modifier in dict.fromkeys(modifiers):
                modifier_key = key_for(modifier)
                assert modifier_key is not None
                bits |= MODIFIER_BITS[modifier]
                await self._key('rawKeyDown', modifier_key, bits)
                held.append(modifier_key)
            await self.key(key, bits)
        finally:
            for modifier_key in reversed(held):
                bits &= ~MODIFIER_BITS[cast(Modifier, modifier_key.key)]
                await self._key('keyUp', modifier_key, bits)

    async def key(self, key: Key, modifiers: int = 0) -> None:
        """Press and release `key`. It types its text unless Alt, Control or Meta is held."""
        text = '' if modifiers & _TEXT_SUPPRESSING else key.text
        await self._key('keyDown' if text else 'rawKeyDown', key, modifiers, text)
        await self._key('keyUp', key, modifiers)

    async def type(self, text: str) -> None:
        """Each character as a key press, so the page sees key events; one with no key as inserted text."""
        for char in text:
            key = key_for(char)
            if key is None or not key.text:
                await self._send('Input.insertText', {'text': char})
            else:
                await self.key(key)

    async def release_buttons(self) -> None:
        """Let go of every held button where the mouse is, so the page does not see a stuck mouse."""
        while self.buttons:
            await self.mouse('mouseReleased', self.pointer, self.buttons[-1])

    async def _key(self, kind: str, key: Key, modifiers: int, text: str = '') -> None:
        params: CDPParams = {
            'type': kind,
            'key': key.key,
            'code': key.code,
            'windowsVirtualKeyCode': key.key_code,
            'modifiers': modifiers,
        }
        if text:
            params |= {'text': text, 'unmodifiedText': text}
        await self._send('Input.dispatchKeyEvent', params)

    async def _send(self, method: str, params: CDPParams) -> None:
        await self._connection.send(method, params, session=self._session)


async def evaluate(connection: CDPConnection, session: str, frame_id: str, function: str, arg: JSON = None) -> object:
    """Call `function` with `arg` in our isolated world of the frame, and return its JSON result. Raises `CDPError`,
    with Chrome's messages, if the document goes away mid-call, and `ActionFailed` if the script throws."""
    world = await connection.send(
        'Page.createIsolatedWorld', {'frameId': frame_id, 'worldName': WORLD}, session=session
    )
    result = await connection.send(
        'Runtime.callFunctionOn',
        {
            'functionDeclaration': function,
            'executionContextId': world['executionContextId'],
            'arguments': [{'value': arg}],
            'returnByValue': True,
            'awaitPromise': True,
        },
        session=session,
    )
    if details := result.get('exceptionDetails'):
        exception = cast(CDPParams, details.get('exception') or {})
        message = str(exception.get('description') or details.get('text') or 'error').splitlines()[0]
        raise ActionFailed(f'a script failed in the page: {message}')
    return cast(CDPParams, result.get('result') or {}).get('value')


def is_navigation_race(error: CDPError) -> bool:
    """The document went away under a call: a navigation replaced it."""
    return any(
        part in error.message
        for part in ('Cannot find context', 'Execution context was destroyed', 'navigated or closed', 'No frame')
    )


# --- the browser ---


@dataclass(kw_only=True)
class _Tab:
    """The run's tab: its CDP session and what its main frame did, as events arrive."""

    target_id: str
    session: str
    events: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    """The main frame's navigation events, oldest first: (kind, loader id). Kinds: `started`, `committed`,
    `same-document`, `stopped`, `load`, and `download` (with the download's guid), from any frame."""
    document_url: str = BLANK_URL
    """The main frame's document URL. An error page is `chrome-error://chromewebdata/`."""
    origins: set[str] = field(default_factory=set[str])
    """Origins of the documents this tab showed, for export."""
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def note(self, kind: str, loader: str = '') -> None:
        self.events.append((kind, loader))
        self.changed.set()

    def loaded(self, mark: int, loader: str) -> bool:
        """Whether the main frame's page has loaded: the document `loader` brought, or the latest one committed
        since `mark`. A page that moves on to another before its load event never fires it (Google's results did)."""
        committed = self.since(mark, 'committed')
        return ('load', committed[-1] if committed else loader) in self.events[mark:]

    def since(self, mark: int, *kinds: str) -> list[str]:
        """The loader ids of events of `kinds` from index `mark` on."""
        return [loader for kind, loader in self.events[mark:] if kind in kinds]

    async def until(self, predicate: Callable[[], bool], timeout: float) -> bool:
        """Wait up to `timeout` seconds for `predicate`, checked after each event. True if it held."""
        deadline = time.monotonic() + timeout
        while not predicate():
            self.changed.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), remaining)
        return True


@dataclass(kw_only=True)
class _Downloading:
    """A download Chrome started (#21)."""

    guid: str
    name: str
    started: float
    state: asyncio.Future[str]
    """`completed` or `canceled`."""


@dataclass(kw_only=True)
class _Chrome:
    """One running Chrome and everything started for it."""

    workdir: Path
    process: asyncio.subprocess.Process
    connection: CDPConnection
    tab: _Tab
    screen: VirtualScreen | None = None
    proxy: EgressProxy | None = None
    downloads: list[_Downloading] = field(default_factory=list[_Downloading])
    """Downloads started and not yet taken."""
    running: dict[str, _Downloading] = field(default_factory=dict[str, _Downloading])
    """Downloads not finished yet, by guid, taken or not."""

    @property
    def download_dir(self) -> Path:
        return self.workdir / 'profile' / 'downloads'

    async def stop(self) -> None:
        self.connection.close()
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            await self.process.wait()
        if self.screen is not None:
            await self.screen.stop()
        if self.proxy is not None:
            await self.proxy.stop()
        shutil.rmtree(self.workdir, ignore_errors=True)


class ChromiumCDPBackend:
    """A `BrowserBackend` running Chrome over our own CDP pipe. One Chrome, one tab, per `open()`. Also a
    `LiveViewBackend` and a `DownloadsBackend`."""

    def __init__(self, options: CDPOptions | None = None) -> None:
        self.options = options or CDPOptions()
        self._chrome: _Chrome | None = None
        self._walker = SnapshotWalker(run_script=self._evaluate)
        self._input: CDPInput | None = None
        self._seeded_origins: set[str] = set()

    @property
    def workdir(self) -> Path | None:
        """The folder holding this browser's profile and launch files, while open. Every process started for the
        browser has it on its command line, which is how the measurements find them."""
        return self._chrome.workdir if self._chrome else None

    @property
    def pid(self) -> int | None:
        """The process we started (Chrome, or bwrap running it), while open."""
        return self._chrome.process.pid if self._chrome else None

    # --- LiveViewBackend (#14) ---

    async def live_view(self) -> FrameSource:
        """A CDP screencast of the tab, with input dispatched to it, on its own session over the same pipe."""
        from montybot.liveview.cdp import CDPFrameSource

        chrome = self._require_open()
        return await CDPFrameSource.start(chrome.connection, home=chrome.tab.target_id)

    # --- BrowserBackend ---

    async def open(self, state: BrowserState | None) -> None:
        if self._chrome is not None:
            raise LifecycleError('the browser is already open')
        self._chrome = await self._launch()
        self._walker = SnapshotWalker(run_script=self._evaluate)
        self._input = CDPInput(self._chrome.connection, self._chrome.tab.session)
        self._seeded_origins = set()
        if state is None:
            return
        chrome = self._chrome
        script = ''
        try:
            cookies = [_to_cdp(c) for c in state.cookies if not 0 <= c.expires < time.time()]
            if cookies:
                await chrome.connection.send('Storage.setCookies', {'cookies': cookies})
            if state.local_storage:
                async with self._side_tab() as side:
                    for origin, items in state.local_storage.items():
                        await side(origin, _WRITE_STORAGE, cast(JSON, items))
                        self._seeded_origins.add(origin)
            if state.session_storage:
                # Only while the first page loads, in our world, before the page's scripts: later loads keep what
                # the pages left in sessionStorage.
                seed = _SEED_SESSION_STORAGE % json.dumps(state.session_storage)
                added = await self._send('Page.addScriptToEvaluateOnNewDocument', {'source': seed, 'worldName': WORLD})
                script = str(added['identifier'])
        except CDPError:
            await self.close()
            # Chrome's message may quote a cookie, so it is left out.
            raise ActionFailed('could not seed the browser state: Chrome rejected it') from None
        except BaseException:
            await self.close()
            raise
        try:
            if state.url != BLANK_URL:
                await self._goto(state.url)
        finally:
            if script:
                with contextlib.suppress(CDPError):
                    await self._send('Page.removeScriptToEvaluateOnNewDocument', {'identifier': script})

    async def export(self) -> BrowserState:
        chrome = self._require_open()
        try:
            url = await self._url()
            origin = origin_of(url)
            local: dict[str, dict[str, str]] = {}
            session: dict[str, str] = {}
            if origin is not None and origin == origin_of(chrome.tab.document_url):  # not on an error page
                local[origin] = cast(dict[str, str], await self._evaluate(_READ_STORAGE, 'localStorage'))
                session = cast(dict[str, str], await self._evaluate(_READ_STORAGE, 'sessionStorage'))
            others = sorted((self._seeded_origins | chrome.tab.origins) - {origin})
            if others:
                async with self._side_tab() as side:
                    for other in others:
                        local[other] = cast(dict[str, str], await side(other, _READ_STORAGE, 'localStorage'))
            raw = cast(list[CDPParams], (await chrome.connection.send('Storage.getCookies'))['cookies'])
        except CDPError as error:
            raise ActionFailed(f'could not read the browser state: {error.message}') from error
        return BrowserState(
            url=url,
            cookies=[_from_cdp(c) for c in raw],
            local_storage={o: items for o, items in local.items() if items},
            session_storage={origin: session} if origin and session else {},
        )

    async def release(self) -> BrowserState:
        state = await self.export()
        await self.close()
        return state

    async def snapshot(self) -> Snapshot:
        self._require_open()
        snapshot = await self._walker.snapshot()
        return Snapshot(url=await self._url(), title=snapshot.title, text=snapshot.text)

    async def act(self, action: Action) -> None:
        chrome = self._require_open()
        mouse = self._mouse()
        try:
            resolved = await self._walker.resolve(action)  # a ref becomes a point to click, or typing at the caret
            match resolved:
                case None:
                    return  # the walker already did it, such as choosing a select's option
                case Navigate(url=url):
                    await self._goto(url)
                case Click(target=target):
                    at = target if isinstance(target, Point) else await self._find(target, typing=False)
                    async with self._settled(chrome.tab):
                        await mouse.click(at)
                case Type(text=text, target=target):
                    if target is not None:
                        await self._find(target, typing=True)
                    async with self._settled(chrome.tab):
                        await mouse.type(text)
                case Press(key=key, modifiers=modifiers):
                    async with self._settled(chrome.tab):
                        await mouse.press(key, modifiers)
                case Scroll(delta_x=dx, delta_y=dy, at=at):
                    await mouse.wheel(at or await self._centre(), dx, dy)
                case MouseDown(at=at, button=button):
                    await mouse.mouse('mouseMoved', at)
                    await mouse.mouse('mousePressed', at, button)
                case MouseMove(at=at):
                    await mouse.mouse('mouseMoved', at)
                case MouseUp(at=at, button=button):
                    async with self._settled(chrome.tab):
                        await mouse.mouse('mouseMoved', at)
                        await mouse.mouse('mouseReleased', at, button)
        except CDPError as error:
            raise ActionFailed(f'{action.kind} failed: {error.message}') from error

    async def screenshot(self) -> Screenshot:
        self._require_open()
        try:
            try:
                shot = await self._send('Page.captureScreenshot', {'format': 'png'})
            except CDPError:
                # A just-opened visible window may not have drawn its first frame yet.
                await asyncio.sleep(0.5)
                shot = await self._send('Page.captureScreenshot', {'format': 'png'})
            width, height = cast(tuple[int, int], await self._evaluate(_VIEWPORT))
        except CDPError as error:
            raise ActionFailed(f'could not take a screenshot: {error.message}') from error
        return Screenshot(png=base64.b64decode(shot['data']), width=width, height=height)

    async def close(self) -> None:
        chrome, self._chrome, self._input = self._chrome, None, None
        if chrome is not None:
            for downloading in chrome.running.values():
                downloading.state.cancel()
            await chrome.stop()

    # --- DownloadsBackend (#21) ---

    async def take_downloads(self) -> list[Download]:
        """The downloads finished since the last call. One still running gets up to `navigation_timeout` from its
        start to finish, else it is cancelled. A failed download is dropped."""
        chrome = self._require_open()
        pending, chrome.downloads = chrome.downloads, []
        if not pending:
            return []
        deadline = max(d.started for d in pending) + self.options.navigation_timeout
        await asyncio.wait([d.state for d in pending], timeout=max(0, deadline - time.monotonic()))
        taken: list[Download] = []
        for downloading in pending:
            path = chrome.download_dir / downloading.guid
            state = downloading.state
            if not state.done():
                state.cancel()
                chrome.running.pop(downloading.guid, None)
                with contextlib.suppress(CDPError):
                    await chrome.connection.send('Browser.cancelDownload', {'guid': downloading.guid})
            elif not state.cancelled() and state.result() == 'completed':
                with contextlib.suppress(OSError):
                    too_large = (await asyncio.to_thread(path.stat)).st_size > MAX_DOWNLOAD_BYTES
                    data = b'' if too_large else await asyncio.to_thread(path.read_bytes)
                    taken.append(Download(name=downloading.name, data=data, too_large=too_large))
            path.unlink(missing_ok=True)
        return taken

    # --- starting ---

    async def _launch(self) -> _Chrome:
        options = self.options
        workdir = Path(tempfile.mkdtemp(prefix='montybot-cdp-'))
        profile = workdir / 'profile'
        (profile / 'downloads').mkdir(parents=True)
        screen: VirtualScreen | None = None
        proxy: EgressProxy | None = None
        process: asyncio.subprocess.Process | None = None
        connection: CDPConnection | None = None
        ours: list[int] = []
        try:
            env: dict[str, str] | None = None
            if options.virtual_screen and not options.headless:
                try:
                    screen = await start_virtual_screen(
                        workdir=workdir,
                        width=options.window_width,
                        height=options.window_height,
                        xvfb=options.xvfb_path,
                    )
                except (OSError, RuntimeError) as error:
                    raise ActionFailed(f'could not start a virtual screen: {error}') from error
                env = {**os.environ, 'DISPLAY': screen.display.name, 'XAUTHORITY': str(screen.display.xauthority)}
            socket_path: Path | None = None
            if options.bwrap:
                if shutil.which(options.bwrap_path) is None:
                    raise ActionFailed(f'could not start Chrome: {options.bwrap_path} not found')
                if options.egress_socket is not None:
                    if options.allow_private_networks:
                        raise ActionFailed('a shared browser proxy cannot allow private networks')
                    socket_path = options.egress_socket
                    if not await proxy_answers(socket_path):
                        raise ActionFailed('browser network proxy unavailable')
                else:
                    proxy = EgressProxy(workdir / 'egress.sock', allow_private=options.allow_private_networks)
                    await proxy.start()
                    socket_path = proxy.path
            argv = options.command(profile=profile, display=screen.display if screen else None, proxy=socket_path)
            # Chrome reads commands on its fd 3 and writes on its fd 4. bash puts our pipe ends there, then becomes
            # Chrome (or bwrap, which passes them on). Not sh: dash cannot redirect from a descriptor above 9.
            commands_read, commands_write = os.pipe()
            replies_read, replies_write = os.pipe()
            ours = [commands_write, replies_read]
            log = os.open(workdir / 'chrome.log', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                process = await asyncio.create_subprocess_exec(
                    '/bin/bash',
                    '-c',
                    f'exec "$@" 3<&{commands_read} 4>&{replies_write} {commands_read}<&- {replies_write}>&-',
                    'sh',
                    *argv,
                    pass_fds=(commands_read, replies_write),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    env=env,
                    start_new_session=True,  # its own process group, so close() kills everything it started
                )
            finally:
                for fd in (commands_read, replies_write, log):
                    os.close(fd)
            connection = await CDPConnection.open(read_fd=replies_read, write_fd=commands_write)
            ours = []
            try:
                tab = await asyncio.wait_for(self._first_tab(connection, process), options.start_timeout)
            except (TimeoutError, CDPClosed) as error:
                log = (workdir / 'chrome.log').read_text(errors='replace').splitlines()
                # Chrome's fatal error, such as "No usable sandbox!", not the stack trace after it.
                reason = next((line.split('] ', 1)[-1] for line in log if ':FATAL:' in line), '\n'.join(log[-5:]))
                raise ActionFailed(f'could not start Chrome: {reason[:300] or type(error).__name__}') from None
            chrome = _Chrome(
                workdir=workdir, process=process, connection=connection, tab=tab, screen=screen, proxy=proxy
            )
            self._listen(chrome)
            _refuse_passkeys_in_popups(connection)
            await connection.send(
                'Browser.setDownloadBehavior',
                {'behavior': 'allowAndName', 'downloadPath': str(chrome.download_dir), 'eventsEnabled': True},
            )
        except BaseException:
            for fd in ours:
                os.close(fd)
            if connection is not None:
                connection.close()
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            if screen is not None:
                await screen.stop()
            if proxy is not None:
                await proxy.stop()
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        return chrome

    async def _first_tab(self, connection: CDPConnection, process: asyncio.subprocess.Process) -> _Tab:
        """Wait for the tab Chrome opens at start, attach to it, and turn on the page events `act` waits for."""
        while True:
            if process.returncode is not None:
                raise CDPClosed('Target.getTargets')
            targets = cast(list[CDPParams], (await connection.send('Target.getTargets'))['targetInfos'])
            pages = [t for t in targets if t['type'] == 'page']
            if pages:
                break
            await asyncio.sleep(0.02)
        target_id = str(pages[0]['targetId'])
        _, attached = await asyncio.gather(
            connection.send('Target.setDiscoverTargets', {'discover': True}),  # for the live view's tabs
            connection.send('Target.attachToTarget', {'targetId': target_id, 'flatten': True}),
        )
        tab = _Tab(target_id=target_id, session=str(attached['sessionId']))
        await asyncio.gather(
            connection.send('Page.enable', session=tab.session),
            connection.send('Page.setLifecycleEventsEnabled', {'enabled': True}, session=tab.session),
            # A window on a screen with no window manager (Xvfb) may not get the focus. The page gets it, as the
            # front window of a desktop would, as Playwright does for its pages.
            connection.send('Emulation.setFocusEmulationEnabled', {'enabled': True}, session=tab.session),
            _refuse_passkeys(connection, tab.session),
        )
        return tab

    def _listen(self, chrome: _Chrome) -> None:
        connection, tab = chrome.connection, chrome.tab
        main = tab.target_id

        def started(params: CDPParams) -> None:
            if params.get('frameId') == main:
                tab.note('started', str(params.get('loaderId', '')))

        def navigated(params: CDPParams) -> None:
            frame = cast(CDPParams, params['frame'])
            url = str(frame.get('url', ''))
            if origin := origin_of(url):
                tab.origins.add(origin)
            if frame['id'] == main:
                tab.document_url = url
                tab.note('committed', str(frame['loaderId']))

        def within_document(params: CDPParams) -> None:
            if params.get('frameId') == main:
                tab.note('same-document')

        def stopped(params: CDPParams) -> None:
            if params.get('frameId') == main:
                tab.note('stopped')

        def lifecycle(params: CDPParams) -> None:
            if params.get('frameId') == main and params.get('name') == 'load':
                tab.note('load', str(params['loaderId']))

        def dialog(params: CDPParams) -> None:
            # Nobody is there to answer: dismiss it, as Playwright does, but leave a page that asks before unloading.
            accept = params.get('type') == 'beforeunload'
            send = connection.send('Page.handleJavaScriptDialog', {'accept': accept}, session=tab.session)
            asyncio.ensure_future(send).add_done_callback(_ignore_result)

        def download_begins(params: CDPParams) -> None:
            guid = str(params['guid'])
            future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            name = str(params.get('suggestedFilename') or 'download')
            downloading = _Downloading(guid=guid, name=name, started=time.monotonic(), state=future)
            chrome.downloads.append(downloading)
            chrome.running[guid] = downloading
            tab.note('download', guid)

        def download_progress(params: CDPParams) -> None:
            state = str(params.get('state'))
            if state not in ('completed', 'canceled'):
                return
            downloading = chrome.running.pop(str(params['guid']), None)
            if downloading is not None and not downloading.state.done():
                downloading.state.set_result(state)

        session = tab.session
        for method in ('Page.frameRequestedNavigation', 'Page.frameStartedNavigating', 'Page.frameStartedLoading'):
            connection.on(method, started, session=session)
        connection.on('Page.frameNavigated', navigated, session=session)
        connection.on('Page.navigatedWithinDocument', within_document, session=session)
        connection.on('Page.frameStoppedLoading', stopped, session=session)
        connection.on('Page.lifecycleEvent', lifecycle, session=session)
        connection.on('Page.javascriptDialogOpening', dialog, session=session)
        connection.on('Browser.downloadWillBegin', download_begins)
        connection.on('Browser.downloadProgress', download_progress)

    # --- internals ---

    def _require_open(self) -> _Chrome:
        if self._chrome is None:
            raise LifecycleError('the browser is not open')
        return self._chrome

    def _mouse(self) -> CDPInput:
        self._require_open()
        assert self._input is not None
        return self._input

    async def _send(self, method: str, params: CDPParams | None = None) -> CDPParams:
        chrome = self._require_open()
        return await chrome.connection.send(method, params, session=chrome.tab.session)

    async def _url(self) -> str:
        """The tab's URL as its address bar shows it: an error page shows the URL that failed."""
        history = await self._send('Page.getNavigationHistory')
        entries = cast(list[CDPParams], history['entries'])
        return str(entries[int(history['currentIndex'])]['url'])

    async def _evaluate(self, function: str, arg: JSON = None) -> object:
        """`evaluate` in the run's tab, retried while a navigation replaces the document under it."""
        chrome = self._require_open()
        tab = chrome.tab
        for attempt in range(3):
            mark = len(tab.events)
            try:
                return await evaluate(chrome.connection, tab.session, tab.target_id, function, arg)
            except CDPError as error:
                if attempt == 2 or not is_navigation_race(error):
                    raise ActionFailed(f'could not run a script in the page: {error.message}') from error
                await tab.until(lambda since=mark: bool(tab.since(since, 'committed')), 1)
        raise AssertionError('unreachable')

    async def _goto(self, url: str) -> None:
        chrome = self._require_open()
        tab = chrome.tab
        timeout = self.options.navigation_timeout
        mark = len(tab.events)
        try:
            result = await asyncio.wait_for(self._send('Page.navigate', {'url': url}), timeout)
        except TimeoutError:
            raise ActionFailed(f'{url} did not finish loading in {timeout:g} s') from None
        except CDPError as error:
            raise ActionFailed(f'could not load {url}: {error.message}') from error
        loader = str(result.get('loaderId', ''))
        if result.get('isDownload'):
            # #21: the URL is a file. Chrome downloads it and stays on the page, as for a click; the download's
            # event may come just after this answer, so `take_downloads` sees it.
            await tab.until(lambda: bool(tab.since(mark, 'download')), 5)
            return
        if error_text := result.get('errorText'):
            # Let Chrome's error page load, or it interrupts the next navigation.
            await tab.until(lambda: ('load', loader) in tab.events, 2)
            raise ActionFailed(f'could not load {url}: {error_text}')
        if not loader:
            return  # a jump within the document
        if not await tab.until(lambda: tab.loaded(mark, loader), timeout):
            raise ActionFailed(f'{url} did not finish loading in {timeout:g} s')

    async def _find(self, target: Selector | Ref, *, typing: bool) -> Point:
        """The centre of the selector's first match, scrolled into view; with `typing`, focused and cleared."""
        if isinstance(target, Ref):
            raise TypeError('refs are resolved by the snapshot walker before this')
        deadline = time.monotonic() + self.options.target_timeout
        while True:
            found = cast(CDPParams, await self._evaluate(_FIND, {'css': target.css, 'type': typing}))
            error = found.get('error')
            if error is None:
                return Point(x=float(found['x']), y=float(found['y']))
            if error == 'invalid':
                raise TargetNotFound(target, 'not a valid CSS selector')
            if error == 'not-editable':
                raise ActionFailed(f'{target.css} cannot take typing')
            if time.monotonic() > deadline:
                raise TargetNotFound(target, 'it is hidden' if error == 'hidden' else '')
            await asyncio.sleep(0.05)

    async def _centre(self) -> Point:
        width, height = cast(tuple[int, int], await self._evaluate(_VIEWPORT))
        return Point(x=width / 2, y=height / 2)

    @asynccontextmanager
    async def _settled(self, tab: _Tab) -> AsyncGenerator[None]:
        """Run the body, then, if it started a main-frame navigation, wait until the new page has loaded."""
        mark = len(tab.events)
        yield
        began = ('started', 'committed', 'same-document')
        if not await tab.until(lambda: bool(tab.since(mark, *began)), _NAVIGATION_GRACE):
            return
        timeout = self.options.navigation_timeout
        # Committed, or ended without a new document (a 204, a download, a jump within the page).
        await tab.until(lambda: bool(tab.since(mark, 'committed', 'stopped', 'same-document')), timeout)
        if not tab.since(mark, 'committed', 'same-document'):
            # A download's events may come just after the navigation ends, so `take_downloads` sees it.
            await tab.until(lambda: bool(tab.since(mark, 'committed', 'download')), 1)
        committed = tab.since(mark, 'committed')
        if committed and not await tab.until(lambda: tab.loaded(mark, committed[0]), timeout):
            raise ActionFailed(f'the page did not finish loading in {timeout:g} s')

    @asynccontextmanager
    async def _side_tab(self) -> AsyncGenerator[Callable[[str, str, JSON], Awaitable[object]]]:
        """A hidden tab to read or write another origin's storage. Each request it makes is answered here with an
        empty page, so the site never sees it. Yields `run(origin, function, arg)`."""
        chrome = self._require_open()
        connection = chrome.connection
        try:
            created = await connection.send('Target.createTarget', {'url': BLANK_URL, 'hidden': True})
        except CDPError:  # an older Chrome without hidden tabs
            created = await connection.send('Target.createTarget', {'url': BLANK_URL, 'background': True})
        target_id = str(created['targetId'])
        session = ''
        try:
            attached = await connection.send('Target.attachToTarget', {'targetId': target_id, 'flatten': True})
            session = str(attached['sessionId'])
            side = _Tab(target_id=target_id, session=session)

            def paused(params: CDPParams) -> None:
                fulfil = {
                    'requestId': params['requestId'],
                    'responseCode': 200,
                    'responseHeaders': [{'name': 'Content-Type', 'value': 'text/html'}],
                    'body': _SIDE_PAGE,
                }
                send = connection.send('Fetch.fulfillRequest', fulfil, session=session)
                asyncio.ensure_future(send).add_done_callback(_ignore_result)

            def lifecycle(params: CDPParams) -> None:
                if params.get('frameId') == target_id and params.get('name') == 'load':
                    side.note('load', str(params['loaderId']))

            connection.on('Fetch.requestPaused', paused, session=session)
            connection.on('Page.lifecycleEvent', lifecycle, session=session)
            await connection.send('Page.enable', session=session)
            await connection.send('Page.setLifecycleEventsEnabled', {'enabled': True}, session=session)
            await connection.send('Fetch.enable', {'patterns': [{'urlPattern': '*'}]}, session=session)

            async def run(origin: str, function: str, arg: JSON) -> object:
                result = await connection.send('Page.navigate', {'url': f'{origin}/'}, session=session)
                loader = str(result.get('loaderId', ''))
                if result.get('errorText') or not await side.until(lambda: ('load', loader) in side.events, 10):
                    raise ActionFailed(f'could not open {origin} to reach its storage')
                return await evaluate(connection, session, target_id, function, arg)

            yield run
        finally:
            with contextlib.suppress(CDPError):
                await connection.send('Target.closeTarget', {'targetId': target_id})
            if session:
                connection.drop_session(session)


_T = TypeVar('_T')

_NO_PASSKEYS: CDPParams = {
    'protocol': 'ctap2',
    'transport': 'usb',  # a security key, not a built-in one: a page still sees no platform authenticator
    'hasResidentKey': True,
    'hasUserVerification': True,
    'isUserVerified': False,
}


async def _refuse_passkeys(connection: CDPConnection, session: str) -> None:
    """Answer the page's passkey (WebAuthn) requests with an empty security key instead of Chrome's own dialog. The
    dialog is Chrome's window, not the page, so the live view cannot show it, and it blocks clicks on the page. With
    no passkey to give, the request fails at once (`NotAllowedError`), as on a computer without one, and the site
    offers its other ways to sign in. A user's passkeys are on their own devices, so none could be used here anyway."""
    await connection.send('WebAuthn.enable', {'enableUI': False}, session=session)
    await connection.send('WebAuthn.addVirtualAuthenticator', {'options': _NO_PASSKEYS}, session=session)


def _refuse_passkeys_in_popups(connection: CDPConnection) -> None:
    """Do the same in every popup or tab a page opens, as Chrome reports it (`Target.setDiscoverTargets` is on). A
    sign-in popup loads its page from the site first, so this is in place before the page can ask. The session stays
    attached, since the empty key lives with it."""

    def created(params: CDPParams) -> None:
        info = cast(CDPParams, params['targetInfo'])
        if info.get('type') != 'page' or not info.get('openerId'):
            return

        async def set_up() -> None:
            attached = await connection.send('Target.attachToTarget', {'targetId': info['targetId'], 'flatten': True})
            await _refuse_passkeys(connection, str(attached['sessionId']))

        asyncio.ensure_future(set_up()).add_done_callback(_ignore_result)

    connection.on('Target.targetCreated', created)


def _ignore_result(future: asyncio.Future[_T]) -> None:
    """For commands sent from an event handler: the page may be gone by the time Chrome answers."""
    if not future.cancelled():
        future.exception()


def _to_cdp(cookie: Cookie) -> CDPParams:
    """A cookie for `Storage.setCookies`. A domain without a leading dot makes a host-only cookie there."""
    param: CDPParams = {
        'name': cookie.name,
        'value': cookie.value,
        'domain': cookie.domain,
        'path': cookie.path,
        'httpOnly': cookie.http_only,
        'secure': cookie.secure,
        'sameSite': cookie.same_site,
    }
    if cookie.expires >= 0:
        param['expires'] = cookie.expires
    return param


def _from_cdp(cookie: CDPParams) -> Cookie:
    same_site = cast(SameSite, cookie.get('sameSite') or 'Lax')
    return Cookie(
        name=str(cookie['name']),
        value=str(cookie['value']),
        domain=str(cookie['domain']),
        path=str(cookie.get('path') or '/'),
        expires=-1 if cookie.get('session') else float(cookie.get('expires', -1)),
        http_only=bool(cookie.get('httpOnly', False)),
        secure=bool(cookie.get('secure', False)),
        same_site=same_site,
    )
