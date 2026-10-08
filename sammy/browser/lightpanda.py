"""`LightpandaBackend`: Lightpanda behind the browser contract, driven over its CDP server. Engine evaluation (#18).

Tested with Lightpanda 1.0.0. See `lightpanda.md` next to this file for what works, what does not, and the numbers.

- **One Lightpanda process per `open`**, started as `lightpanda serve` on a free loopback port, with one browser
  context and one page. A Lightpanda browser context holds one page only (`TargetAlreadyLoaded` for a second), so
  there is no side tab.
- **A small CDP client** on the `websockets` library: commands, responses, and the few events the backend needs (page
  loads, navigation failures, execution contexts, paused requests). No raw CDP leaves this module.
- **State in:** cookies (HttpOnly too) through `Network.setCookies` before any page loads. Storage needs a document of
  its origin, so `open` loads `ORIGIN/__sammy_storage__` for each origin with `Fetch` interception answering it
  with an empty page: no request reaches the site.
- **State out:** `Network.getAllCookies` returns every cookie, HttpOnly included, with its domain and SameSite.
  localStorage of the current origin is read from the page; of every other seeded or visited origin, from a hidden
  iframe on the same intercepted empty page, removed straight after.
- **No pixels.** Lightpanda has no real layout: `getBoundingClientRect` is a simple block flow that ignores CSS
  positioning, nothing scrolls, and `Page.captureScreenshot` draws the text only, at places that match neither the
  site's design nor Lightpanda's own boxes. So `screenshot`, points, the mouse and scrolling raise `NotSupported`.
  Clicks on refs and selectors go to the element's centre in Lightpanda's own layout, which its hit test agrees with.
- **On the Linux server** (`LightpandaOptions(bwrap=True)`), Lightpanda runs in the same jail as Chromium
  (`chromium_linux`): its own network namespace, whose only way out is the `EgressProxy`, reached as a SOCKS5 proxy
  with remote DNS (`--http-proxy socks5h://...`). Its CDP port is published only as a Unix socket in its profile.
- **No telemetry.** Lightpanda reports usage to `telemetry.lightpanda.io` unless `LIGHTPANDA_DISABLE_TELEMETRY` is
  set, so it always is.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import tempfile
import time
from base64 import b64encode
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from websockets.asyncio.client import ClientConnection, connect, unix_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from sammy.browser.chromium_linux import bwrap_command
from sammy.browser.contract import (
    Action,
    ActionFailed,
    Click,
    ElementTarget,
    LifecycleError,
    Modifier,
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
    Snapshot,
    TargetNotFound,
    Type,
)
from sammy.browser.egress import PROXY_PORT, EgressProxy
from sammy.browser.snapshot import JSON, SnapshotWalker
from sammy.browser.state import BLANK_URL, BrowserState, Cookie, SameSite, origin_of

ENGINE = 'lightpanda'

TELEMETRY_OFF = {'LIGHTPANDA_DISABLE_TELEMETRY': 'true'}
"""Without it, Lightpanda sends usage reports to telemetry.lightpanda.io."""

STORAGE_PATH = '/__sammy_storage__'
"""Loaded on an origin to reach its storage. `Fetch` interception answers it with an empty page, so no request
reaches the site."""
_STORAGE_PAGE = b64encode(b'<!doctype html><title>storage</title>').decode()

_NO_PIXELS = 'Lightpanda has no layout engine, so it cannot draw the page or place points, the mouse or a scroll on it'
_MODIFIER_BITS: dict[Modifier, int] = {'Alt': 1, 'Control': 2, 'Meta': 4, 'Shift': 8}

# The named `KeyboardEvent.key` values: (`code`, Windows virtual key code, the text the key types). One-character keys
# are sent as themselves.
CDP_KEYS: dict[str, tuple[str, int, str]] = {
    'Backspace': ('Backspace', 8, ''),
    'Tab': ('Tab', 9, ''),
    'Enter': ('Enter', 13, '\r'),
    'Shift': ('ShiftLeft', 16, ''),
    'Control': ('ControlLeft', 17, ''),
    'Alt': ('AltLeft', 18, ''),
    'Pause': ('Pause', 19, ''),
    'Escape': ('Escape', 27, ''),
    'PageUp': ('PageUp', 33, ''),
    'PageDown': ('PageDown', 34, ''),
    'End': ('End', 35, ''),
    'Home': ('Home', 36, ''),
    'ArrowLeft': ('ArrowLeft', 37, ''),
    'ArrowUp': ('ArrowUp', 38, ''),
    'ArrowRight': ('ArrowRight', 39, ''),
    'ArrowDown': ('ArrowDown', 40, ''),
    'Insert': ('Insert', 45, ''),
    'Delete': ('Delete', 46, ''),
    'Meta': ('MetaLeft', 91, ''),
    **{f'F{n}': (f'F{n}', 111 + n, '') for n in range(1, 13)},
}

_READ_STORAGE = '(() => Object.fromEntries(Object.entries(%s)))()'
_WRITE_STORAGE = '((items) => { for (const [k, v] of Object.entries(items)) %s.setItem(k, v); })(%s)'
_FIND = """(css) => {
  const element = document.querySelector(css);
  if (!element) return null;
  element.scrollIntoView({block: 'center', inline: 'center'});
  const box = element.getBoundingClientRect();
  return {x: box.left + box.width / 2, y: box.top + box.height / 2};
}"""
_FOCUS = """(css) => {
  const element = document.querySelector(css);
  if (!element) return false;
  element.focus();
  if (typeof element.select === 'function') element.select();
  return true;
}"""
_STORAGE_FRAME = """(src) => {
  const frame = document.createElement('iframe');
  frame.setAttribute('data-sammy-storage', '');
  frame.style.display = 'none';
  frame.src = src;
  (document.body || document.documentElement).appendChild(frame);
}"""
_REMOVE_STORAGE_FRAMES = "document.querySelectorAll('iframe[data-sammy-storage]').forEach((f) => f.remove())"


def default_binary() -> Path:
    """`$SAMMY_LIGHTPANDA_BINARY`, else `~/.cache/sammy/lightpanda/lightpanda` (the release binary, renamed)."""
    if env := os.environ.get('SAMMY_LIGHTPANDA_BINARY'):
        return Path(env)
    return Path.home() / '.cache/sammy/lightpanda/lightpanda'


def cdp_socket(profile: Path) -> Path:
    """Where a jailed Lightpanda's CDP server is reached from the host."""
    return profile / 'cdp.sock'


@dataclass(frozen=True, kw_only=True)
class LightpandaOptions:
    binary: Path = field(default_factory=default_binary)
    window_size: tuple[int, int] = (1280, 800)
    """The viewport in CSS pixels that pages see (`innerWidth`, media queries). Lightpanda's own is 1920x1080."""
    load_resources: tuple[str, ...] = ('stylesheet', 'iframe')
    """Lightpanda's `--load-resources`: what it fetches besides documents and scripts. Stylesheets, so the snapshot
    leaves out what CSS hides; iframes, so it shows same-origin frames (and export reaches other origins' storage)."""
    bwrap: bool = False
    """Run Lightpanda inside bubblewrap with its own profile folder and network namespace (Linux). Its connections go
    out through an `EgressProxy`, which refuses private addresses, and its CDP server is reached through a Unix
    socket."""
    allow_private_networks: bool = False
    """With `bwrap`: let pages reach loopback and private addresses. Only for local fixture sites in tests."""
    egress_socket: Path | None = None
    """A shared egress proxy in a separate container. Unset: start a private proxy for this browser (local tests)."""
    bwrap_path: str = 'bwrap'
    start_timeout: float = 20
    page_load_timeout: float = 30
    find_timeout: float = 3
    """How long `act` waits for a selector to match before `TargetNotFound`."""
    settle_delay: float = 0.1
    """How long a click or key press gets to start a navigation before `act` waits for the page to load."""

    def command(self, *, port: int, profile: Path, proxy: Path | None = None) -> list[str]:
        """Lightpanda's command line; with `bwrap`, wrapped in the jail, with `proxy` the egress proxy's socket."""
        argv = [
            str(self.binary),
            'serve',
            *('--host', '127.0.0.1'),
            *('--port', str(port)),
            '--disable-metrics',
            *('--log-level', 'error'),
            *(arg for resource in self.load_resources for arg in ('--load-resources', resource)),
        ]
        if not self.bwrap:
            return argv
        if proxy is None:
            raise ValueError('a jailed Lightpanda needs its egress proxy')
        # Every request through the proxy, loopback too. socks5h: the proxy resolves names, so it checks the address.
        argv += ['--http-proxy', f'socks5h://127.0.0.1:{PROXY_PORT}']
        jail = bwrap_command(
            chrome=self.binary,
            profile=profile,
            display=None,
            proxy=proxy,
            proxy_directory=proxy.parent if self.egress_socket is not None else None,
            expose=(port, cdp_socket(profile)),
            env=TELEMETRY_OFF,
            bwrap=self.bwrap_path,
        )
        return [*jail, *argv[1:]]


class LightpandaBackend:
    """A `BrowserBackend` on Lightpanda. One process per `open`; see the module docstring."""

    def __init__(self, options: LightpandaOptions | None = None) -> None:
        self.options = options or LightpandaOptions()
        self._process: asyncio.subprocess.Process | None = None
        self._proxy: EgressProxy | None = None
        """This browser's own egress proxy, when jailed without a shared one."""
        self._profile: Path | None = None
        self._cdp: _Cdp | None = None
        self._session = ''
        self._frame = ''
        """The page's main frame id, which is also its target id."""
        self._loader = ''
        """The main frame's latest navigation."""
        self._loaded: set[str] = set()
        """Navigations of the main frame whose `load` has fired since they started."""
        self._failed: dict[str, str] = {}
        """Navigations of the main frame whose document request failed, with Lightpanda's reason."""
        self._committed: set[str] = set()
        """Navigations of the main frame whose document is shown."""
        self._navigations = 0
        self._page_events = asyncio.Event()
        self._contexts: list[tuple[int, str, str]] = []
        """Execution contexts as they are created: id, frame id, origin."""
        self._local_origins: set[str] = set()
        """Origins to read localStorage for on export: seeded or visited."""
        self._tasks: set[asyncio.Task[None]] = set()
        self._walker = SnapshotWalker(run_script=self._run_walker)

    @property
    def pid(self) -> int | None:
        """The Lightpanda process (or bwrap, which runs it) while open."""
        return self._process.pid if self._process is not None else None

    # --- BrowserBackend ---

    async def open(self, state: BrowserState | None) -> None:
        if self._cdp is not None:
            raise LifecycleError('the browser is already open')
        await self._start()
        self._walker = SnapshotWalker(run_script=self._run_walker)
        if state is None:
            return
        await self._seed_cookies(state.cookies)
        origin = origin_of(state.url)
        session = state.session_storage.get(origin, {}) if origin is not None else {}
        to_seed = [o for o in state.local_storage if o != origin]
        if origin is not None and (origin in state.local_storage or session):
            to_seed.append(origin)  # last, so its sessionStorage is the tab's when the page loads
        if to_seed:
            async with self._intercepting_storage_pages():
                for other in to_seed:
                    await self._navigate(f'{other}{STORAGE_PATH}')
                    if items := state.local_storage.get(other):
                        await self._evaluate(_WRITE_STORAGE % ('localStorage', json.dumps(items)))
                    if other == origin and session:
                        await self._evaluate(_WRITE_STORAGE % ('sessionStorage', json.dumps(session)))
                    self._local_origins.add(other)
        await self._navigate(state.url)

    async def export(self) -> BrowserState:
        self._check_open()
        url = await self._current_url()
        origin = origin_of(url)
        local: dict[str, dict[str, str]] = {}
        session: dict[str, dict[str, str]] = {}
        if origin is not None:
            local[origin] = await self._evaluate(_READ_STORAGE % 'localStorage')
            session[origin] = await self._evaluate(_READ_STORAGE % 'sessionStorage')
        others = sorted(self._local_origins - {origin})
        if others:
            async with self._intercepting_storage_pages():
                for other in others:
                    local[other] = await self._read_local_storage_in_frame(other)
        raw = await self._call('Network.getAllCookies')
        return BrowserState(
            url=url,
            cookies=[_cookie_from_cdp(c) for c in cast(list[dict[str, Any]], raw['cookies'])],
            local_storage={o: items for o, items in local.items() if items},
            session_storage={o: items for o, items in session.items() if items},
        )

    async def release(self) -> BrowserState:
        state = await self.export()
        await self.close()
        return state

    async def snapshot(self) -> Snapshot:
        self._check_open()
        return await self._walker.snapshot()

    async def act(self, action: Action) -> None:
        self._check_open()
        match action:
            case Click(target=Point()):
                raise NotSupported('point', engine=ENGINE, detail=_NO_PIXELS)
            case Scroll():
                raise NotSupported('scroll', engine=ENGINE, detail=_NO_PIXELS)
            case MouseDown() | MouseMove() | MouseUp():
                raise NotSupported('mouse', engine=ENGINE, detail=_NO_PIXELS)
            case _:
                pass
        match await self._walker.resolve(
            action
        ):  # a ref becomes a point in Lightpanda's layout, or typing at the caret
            case None:
                return  # the walker already did it, such as choosing a select's option
            case Navigate(url=url):
                await self._navigate(url)
                return
            case Click(target=target):
                at = target if isinstance(target, Point) else await self._find(target)
                await self._click(at)
            case Type(text=text, target=target):
                if target is not None:
                    await self._focus(target)
                for char in text:
                    await self._key(char, modifiers=())
                return
            case Press(key=key, modifiers=modifiers):
                for modifier in modifiers:
                    await self._key_event('keyDown', modifier, modifiers=modifiers)
                await self._key(key, modifiers=modifiers)
                for modifier in reversed(modifiers):
                    await self._key_event('keyUp', modifier, modifiers=())
            case _:
                pass  # the mouse and scrolling were refused above
        await self._settle()

    async def screenshot(self) -> Screenshot:
        self._check_open()
        raise NotSupported(
            'screenshot', engine=ENGINE, detail='Page.captureScreenshot draws the text only, not the page as laid out'
        )

    async def close(self) -> None:
        cdp, process, profile, proxy = self._cdp, self._process, self._profile, self._proxy
        self._cdp = self._process = self._profile = self._proxy = None
        self._local_origins = set()
        self._loaded, self._committed, self._failed, self._contexts = set(), set(), {}, []
        if cdp is not None:
            await cdp.close()
        for task in self._tasks:
            task.cancel()
        if process is not None and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        if proxy is not None:
            await proxy.stop()
        if profile is not None:
            shutil.rmtree(profile, ignore_errors=True)

    # --- starting ---

    async def _start(self) -> None:
        options = self.options
        self._profile = Path(tempfile.mkdtemp(prefix='sammy-lightpanda-'))
        port = _free_port()
        try:
            proxy: Path | None = None
            if options.bwrap:
                if shutil.which(options.bwrap_path) is None:
                    raise ActionFailed(f'could not start Lightpanda: {options.bwrap_path} not found')
                if options.egress_socket is not None:
                    if options.allow_private_networks:
                        raise ActionFailed('a shared browser proxy cannot allow private networks')
                    proxy = options.egress_socket
                else:
                    self._proxy = EgressProxy(
                        self._profile / 'egress.sock', allow_private=options.allow_private_networks
                    )
                    await self._proxy.start()
                    proxy = self._proxy.path
            argv = options.command(port=port, profile=self._profile, proxy=proxy)
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env={**os.environ, **TELEMETRY_OFF},
                start_new_session=True,  # its own process group, so close() kills everything it started
            )
            self._cdp = await self._connect(port, unix_socket=cdp_socket(self._profile) if options.bwrap else None)
            self._cdp.on_event(self._on_event)
            context = await self._call('Target.createBrowserContext')
            target = await self._call('Target.createTarget', {'url': BLANK_URL, **context})
            self._frame = str(target['targetId'])
            attached = await self._call('Target.attachToTarget', {'targetId': self._frame, 'flatten': True})
            self._session = str(attached['sessionId'])
            for method in ('Page.enable', 'Runtime.enable', 'Network.enable'):
                await self._call(method)
            await self._call('Page.setLifecycleEventsEnabled', {'enabled': True})
            width, height = options.window_size
            metrics = {'width': width, 'height': height, 'deviceScaleFactor': 1, 'mobile': False}
            await self._call('Emulation.setDeviceMetricsOverride', metrics)
            if not str(await self._evaluate('navigator.userAgent')).startswith('Lightpanda/'):
                raise ActionFailed('something other than Lightpanda answered on its CDP port')
        except BaseException:
            await self.close()
            raise

    async def _connect(self, port: int, *, unix_socket: Path | None) -> _Cdp:
        deadline = time.monotonic() + self.options.start_timeout
        uri = f'ws://127.0.0.1:{port}/'
        while True:
            assert self._process is not None
            if self._process.returncode is not None:
                raise ActionFailed(f'Lightpanda exited at start with code {self._process.returncode}')
            try:
                if unix_socket is None:
                    connection = await connect(uri, max_size=None, ping_interval=None, open_timeout=5)
                else:
                    connection = await unix_connect(
                        str(unix_socket), uri, max_size=None, ping_interval=None, open_timeout=5
                    )
            except (OSError, WebSocketException, TimeoutError):
                if time.monotonic() > deadline:
                    raise ActionFailed('Lightpanda did not start its CDP server in time') from None
                await asyncio.sleep(0.02)
                continue
            return _Cdp(connection, timeout=self.options.page_load_timeout + 30)

    # --- page events ---

    def _on_event(self, method: str, params: dict[str, Any]) -> None:
        match method:
            case 'Page.frameStartedNavigating' if params.get('frameId') == self._frame:
                self._loader = str(params.get('loaderId', ''))
                self._loaded.discard(self._loader)
                self._committed.discard(self._loader)
                self._failed.pop(self._loader, None)
                self._navigations += 1
            case 'Page.lifecycleEvent' if params.get('frameId') == self._frame and params.get('name') == 'load':
                self._loaded.add(str(params.get('loaderId', '')))
            case 'Network.loadingFailed' if params.get('requestId') == self._loader:
                self._failed[self._loader] = str(params.get('errorText') or 'failed')
            case 'Page.frameNavigated':
                frame = cast(dict[str, Any], params.get('frame') or {})
                if frame.get('id') != self._frame:
                    return
                self._committed.add(str(frame.get('loaderId', '')))
                if origin := origin_of(str(frame.get('url', ''))):
                    self._local_origins.add(origin)
            case 'Runtime.executionContextCreated':
                context = cast(dict[str, Any], params.get('context') or {})
                frame = str(cast(dict[str, Any], context.get('auxData') or {}).get('frameId', ''))
                self._contexts.append((int(context.get('id', 0)), frame, str(context.get('origin', ''))))
            case 'Fetch.requestPaused':
                task = asyncio.ensure_future(self._answer_paused(params))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            case _:
                return
        self._page_events.set()

    async def _answer_paused(self, params: dict[str, Any]) -> None:
        """Answer an intercepted storage page with an empty document; let anything else through."""
        request_id = params['requestId']
        url = str(cast(dict[str, Any], params.get('request') or {}).get('url', ''))
        try:
            if urlsplit(url).path == STORAGE_PATH:
                headers = [{'name': 'Content-Type', 'value': 'text/html; charset=utf-8'}]
                answer = {
                    'requestId': request_id,
                    'responseCode': 200,
                    'responseHeaders': headers,
                    'body': _STORAGE_PAGE,
                }
                await self._call('Fetch.fulfillRequest', answer)
            else:
                await self._call('Fetch.continueRequest', {'requestId': request_id})
        except (ActionFailed, LifecycleError):
            pass  # the page or the browser went away meanwhile

    async def _wait_for(self, done: Callable[[], bool], *, timeout: float, what: str) -> None:
        deadline = time.monotonic() + timeout
        while not done():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ActionFailed(f'timed out waiting for {what}')
            self._page_events.clear()
            try:
                await asyncio.wait_for(self._page_events.wait(), min(remaining, 0.5))
            except TimeoutError:
                pass

    # --- seeding and reading state ---

    async def _seed_cookies(self, cookies: Sequence[Cookie]) -> None:
        now = time.time()
        params: list[dict[str, Any]] = []
        for cookie in cookies:
            if 0 <= cookie.expires < now:
                continue  # already expired: a browser would drop it
            # A `domain` makes a domain cookie (`localhost` becomes `.localhost`), so a host-only one is set by URL.
            where = (
                {'domain': cookie.domain}
                if cookie.domain.startswith('.')
                else {'url': f'{"https" if cookie.secure else "http"}://{cookie.domain}{cookie.path}'}
            )
            raw: dict[str, Any] = {
                'name': cookie.name,
                'value': cookie.value,
                **where,
                'path': cookie.path,
                'secure': cookie.secure,
                'httpOnly': cookie.http_only,
                'sameSite': cookie.same_site,
            }
            if cookie.expires >= 0:
                raw['expires'] = cookie.expires
            params.append(raw)
        if params:
            await self._call('Network.setCookies', {'cookies': params})

    async def _read_local_storage_in_frame(self, origin: str) -> dict[str, str]:
        """`origin`'s localStorage, from a hidden iframe on its storage page in the current document."""
        seen = len(self._contexts)

        def context() -> int | None:
            for context_id, frame, context_origin in self._contexts[seen:]:
                if frame != self._frame and context_origin == origin:
                    return context_id
            return None

        await self._evaluate(f'({_STORAGE_FRAME})({json.dumps(origin + STORAGE_PATH)})')
        try:
            await self._wait_for(lambda: context() is not None, timeout=10, what=f'the storage of {origin}')
            context_id = context()
            assert context_id is not None
            return await self._evaluate(_READ_STORAGE % 'localStorage', context=context_id)
        finally:
            await self._evaluate(_REMOVE_STORAGE_FRAMES)

    @asynccontextmanager
    async def _intercepting_storage_pages(self) -> AsyncGenerator[None]:
        """Meanwhile, the storage pages are answered by `_answer_paused`, never by the site."""
        await self._call('Fetch.enable', {'patterns': [{'urlPattern': f'*{STORAGE_PATH}*'}]})
        try:
            yield
        finally:
            if self._cdp is not None:
                await self._call('Fetch.disable')

    # --- actions ---

    async def _navigate(self, url: str) -> None:
        result = await self._call('Page.navigate', {'url': url})
        loader = str(result.get('loaderId') or '')
        error = result.get('errorText')
        if error and loader == self._loader and loader not in self._committed:  # see `_wait_for_load`
            raise ActionFailed(f'could not load {url}: {error}')
        if loader:
            await self._wait_for_load(loader, what=url)

    async def _wait_for_load(self, loader: str, *, what: str) -> None:
        """Wait for `loader` to load, or for whichever navigation replaced it, such as a script's redirect.

        A document whose request fails after it was shown (Lightpanda says `Shutdown` when a script on it starts
        another navigation) did load: only a request that fails before its document is shown fails the load.
        """
        deadline = time.monotonic() + self.options.page_load_timeout
        while True:
            await self._wait_for(
                partial(self._load_ended, loader),
                timeout=max(0.0, deadline - time.monotonic()),
                what=f'{what} to load',
            )
            if loader != self._loader and loader not in self._loaded:
                loader = self._loader  # replaced before it loaded: follow the new one
                continue
            if loader in self._failed and loader in self._committed:
                await asyncio.sleep(self.options.settle_delay)  # time for the script's navigation to start
                if loader != self._loader:
                    loader = self._loader
                    continue
            break
        if loader not in self._loaded and loader not in self._committed and (error := self._failed.get(loader)):
            raise ActionFailed(f'could not load {what}: {error}')

    def _load_ended(self, loader: str) -> bool:
        return loader in self._loaded or loader in self._failed or loader != self._loader

    async def _find(self, target: ElementTarget) -> Point:
        if isinstance(target, Ref):
            raise TypeError('refs are resolved by the snapshot walker before this')
        deadline = time.monotonic() + self.options.find_timeout
        while True:
            found = await self._evaluate(f'({_FIND})({json.dumps(target.css)})')
            if isinstance(found, dict):
                at = cast(dict[str, float], found)
                return Point(x=float(at['x']), y=float(at['y']))
            if time.monotonic() > deadline:
                raise TargetNotFound(target)
            await asyncio.sleep(0.1)

    async def _focus(self, target: ElementTarget) -> None:
        if isinstance(target, Ref):
            raise TypeError('refs are resolved by the snapshot walker before this')
        deadline = time.monotonic() + self.options.find_timeout
        while not await self._evaluate(f'({_FOCUS})({json.dumps(target.css)})'):
            if time.monotonic() > deadline:
                raise TargetNotFound(target)
            await asyncio.sleep(0.1)

    async def _click(self, at: Point) -> None:
        for kind in ('mousePressed', 'mouseReleased'):
            event = {'type': kind, 'x': at.x, 'y': at.y, 'button': 'left', 'clickCount': 1}
            await self._call('Input.dispatchMouseEvent', event)

    async def _key(self, key: str, *, modifiers: Sequence[Modifier]) -> None:
        await self._key_event('keyDown', key, modifiers=modifiers)
        await self._key_event('keyUp', key, modifiers=modifiers)

    async def _key_event(self, kind: str, key: str, *, modifiers: Sequence[Modifier]) -> None:
        if key in CDP_KEYS:
            code, key_code, text = CDP_KEYS[key]
        elif len(key) == 1:
            code, key_code, text = _code_of(key), ord(key.upper()) if key.isalnum() else 0, key
        elif key == '\n':
            code, key_code, text = CDP_KEYS['Enter']
        else:
            raise ActionFailed(f'unknown key {key!r}: use a KeyboardEvent.key value such as Enter, ArrowDown or a')
        bits = sum(_MODIFIER_BITS[m] for m in modifiers)
        event: dict[str, Any] = {'type': kind, 'key': key, 'code': code, 'modifiers': bits}
        if key_code:
            event['windowsVirtualKeyCode'] = key_code
        if kind == 'keyDown' and text and not bits & (_MODIFIER_BITS['Control'] | _MODIFIER_BITS['Meta']):
            event['text'] = text
        await self._call('Input.dispatchKeyEvent', event)

    async def _settle(self) -> None:
        """Give a click or key press time to start a navigation, then wait until that navigation has loaded."""
        before = self._navigations
        await asyncio.sleep(self.options.settle_delay)
        if self._navigations != before:
            loader = self._loader
            try:
                await self._wait_for_load(loader, what='the page')
            except ActionFailed:
                pass  # a click whose page fails to load is not the click's failure; the snapshot shows the error

    # --- CDP plumbing ---

    def _check_open(self) -> None:
        if self._cdp is None:
            raise LifecycleError('the browser is not open')

    async def _current_url(self) -> str:
        return str(await self._evaluate('location.href'))

    async def _run_walker(self, function: str, arg: JSON, /) -> object:
        return await self._evaluate(f'({function})({json.dumps(arg)})')

    async def _evaluate(self, expression: str, *, context: int | None = None) -> Any:
        params: dict[str, Any] = {'expression': expression, 'returnByValue': True, 'awaitPromise': True}
        if context is not None:
            params['contextId'] = context
        result = await self._call('Runtime.evaluate', params)
        if 'exceptionDetails' in result:
            details = cast(dict[str, Any], result['exceptionDetails'])
            kind = cast(dict[str, Any], details.get('exception') or {}).get('className') or 'an exception'
            raise ActionFailed(f'a script in the page failed with {kind}')  # the message may hold page data
        return cast(dict[str, Any], result.get('result') or {}).get('value')

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """A command to the page's session, or to the browser for `Target.` commands. CDP errors are
        `ActionFailed`."""
        cdp = self._cdp
        if cdp is None:
            raise LifecycleError('the browser is not open')
        session = None if method.startswith('Target.') else self._session
        try:
            return await cdp.call(method, params or {}, session=session)
        except _CdpError as e:
            raise ActionFailed(f'{method} failed: {e}') from None
        except _Unreachable:
            raise ActionFailed('Lightpanda stopped answering; it may have crashed') from None


# --- the CDP client ---


class _Unreachable(Exception):
    pass


class _CdpError(Exception):
    pass


class _Cdp:
    """One CDP connection: numbered commands, their responses, and events passed to one handler."""

    def __init__(self, connection: ClientConnection, *, timeout: float) -> None:
        self._connection = connection
        self._timeout = timeout
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._handler: Callable[[str, dict[str, Any]], None] = lambda method, params: None
        self._reader = asyncio.ensure_future(self._read())

    def on_event(self, handler: Callable[[str, dict[str, Any]], None]) -> None:
        self._handler = handler

    async def call(self, method: str, params: dict[str, Any], *, session: str | None) -> dict[str, Any]:
        if self._reader.done():
            raise _Unreachable
        self._next_id += 1
        message: dict[str, Any] = {'id': self._next_id, 'method': method, 'params': params}
        if session is not None:
            message['sessionId'] = session
        future = asyncio.get_running_loop().create_future()
        self._pending[self._next_id] = future
        try:
            await self._connection.send(json.dumps(message))
            return await asyncio.wait_for(future, self._timeout)
        except (ConnectionClosed, TimeoutError):
            raise _Unreachable from None
        finally:
            self._pending.pop(message['id'], None)

    async def close(self) -> None:
        self._reader.cancel()
        await self._connection.close()
        self._fail_pending()

    async def _read(self) -> None:
        try:
            async for raw in self._connection:
                message = cast(dict[str, Any], json.loads(raw))
                if 'id' in message:
                    future = self._pending.get(int(message['id']))
                    if future is None or future.done():
                        continue
                    if 'error' in message:
                        error = cast(dict[str, Any], message['error'])
                        future.set_exception(_CdpError(str(error.get('message', error))))
                    else:
                        future.set_result(cast(dict[str, Any], message.get('result') or {}))
                elif 'method' in message:
                    self._handler(str(message['method']), cast(dict[str, Any], message.get('params') or {}))
        except ConnectionClosed:
            pass
        finally:
            self._fail_pending()

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(_Unreachable())


# --- helpers ---


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _code_of(char: str) -> str:
    """The `KeyboardEvent.code` of a one-character key on a US keyboard, as far as it matters to pages."""
    if char.isalpha() and char.isascii():
        return f'Key{char.upper()}'
    if char.isdigit():
        return f'Digit{char}'
    return {' ': 'Space', '-': 'Minus', '=': 'Equal', ',': 'Comma', '.': 'Period', '/': 'Slash'}.get(char, '')


def _cookie_from_cdp(raw: dict[str, Any]) -> Cookie:
    same_site = cast(SameSite, raw.get('sameSite') or 'Lax')
    return Cookie(
        name=str(raw['name']),
        value=str(raw['value']),
        domain=str(raw['domain']),
        path=str(raw.get('path') or '/'),
        expires=float(raw.get('expires', -1)) if not raw.get('session') else -1,
        http_only=bool(raw.get('httpOnly', False)),
        secure=bool(raw.get('secure', False)),
        same_site=same_site,
    )
