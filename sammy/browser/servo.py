"""`ServoBackend`: Servo behind the browser contract, driven over servoshell's built-in W3C WebDriver. Issue #12.

Grown from `poc/sammy_poc/servo.py`. Tested with Servo 0.7.0. See `servo.md` next to this file for the decisions on
Servo's gaps and the numbers measured.

- **One Servo process per `open`.** servoshell allows one WebDriver session per process, and each run should get a
  clean engine anyway. Each process gets a free port chosen at runtime and a throwaway config folder.
- **`close` sends SIGKILL** to the process group. Headless servoshell ignores SIGTERM and SIGINT, and the profile is
  thrown away, so there is nothing to shut down cleanly.
- **Cookies are seeded from a page on the cookie's bare domain.** WebDriver's Add Cookie only accepts cookies for the
  current document's host, so `open` visits `https://HOST:1/` in a side tab for each cookie host. Port 1 is on the
  fetch spec's bad-port list, so Servo shows its error page at once, with no request to the site and no redirect to
  another host, and Add Cookie works on that page. localStorage needs a real document of its origin, so it is seeded
  from `ORIGIN/robots.txt`, a page without scripts.
- **`export` raises `NotSupported('export')` by default**, because Servo 0.7.0's Get All Cookies leaves out HttpOnly
  cookies. A servoshell built with `patches/servo-webdriver-httponly.patch` can export: set
  `ServoOptions(http_only_export=True)`. It then reads each known host's cookies from `https://HOST:1/` pages in a
  side tab, so the user's tab and its history stay as they were.
- **Site compatibility:** `ServoOptions` turns on the prefs real sites need (IntersectionObserver, adoptedStyleSheets
  and more) and sends a Chrome user agent, which got through walmart.com where Servo's Firefox one was challenged.
- **Snapshots and refs** come from `SnapshotWalker` (#13), run with Execute Script, so the text and refs match
  Chromium's for the same page. A click on a ref is a pointer action at the element's centre.
- **On the Linux server** (`ServoOptions(bwrap=True)`), Servo runs in the same jail as Chromium (`chromium_linux`):
  its own network namespace, whose only way out is the `EgressProxy`, reached as Servo's HTTP proxy. Its WebDriver
  server listens on every interface, so it is reached through a Unix socket in its profile folder, never a host port.
"""

from __future__ import annotations

import asyncio
import base64
import http.client
import ipaddress
import json
import os
import shutil
import signal
import socket
import struct
import sys
import tempfile
import time
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sammy.browser.chromium_linux import bwrap_command
from sammy.browser.contract import (
    Action,
    ActionFailed,
    Click,
    ElementTarget,
    LifecycleError,
    MouseButton,
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
from sammy.browser.snapshot import JSON, SnapshotWalker, webdriver_script
from sammy.browser.state import BLANK_URL, BrowserState, Cookie, SameSite, origin_of

ENGINE = 'servo'

SITE_PREFS: tuple[str, ...] = (
    'dom_intersection_observer_enabled',
    'dom_resize_observer_enabled',
    'dom_adoptedstylesheet_enabled',
    'dom_crypto_subtle_enabled',
    'dom_fontface_enabled',
    'dom_indexeddb_enabled',
    'layout_columns_enabled',
    'layout_container_queries_enabled',
    'layout_variable_fonts_enabled',
)
"""Servo prefs that real sites need and that are off, or may be off, in a stock 0.7.0. The names are from
`components/config/prefs.rs` at v0.7.0; `servoshell --pref=NAME` sets each to true."""

_CHROME_VERSION = '141.0.0.0'
CHROME_USER_AGENT = (
    f'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) '
    f'Chrome/{_CHROME_VERSION} Safari/537.36'
    if sys.platform == 'darwin'
    else f'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{_CHROME_VERSION} Safari/537.36'
)
"""A desktop Chrome user agent for this platform. Servo's own says Firefox, and bot checks challenged it every time."""

BLOCKED_PORT = 1
"""A port on the fetch spec's bad-port list: Servo refuses it before any DNS lookup or connection and shows its error
page, whose URL still has the host we asked for."""

_ELEMENT = 'element-6066-11e4-a52e-4f735466cecf'
_ERROR_PAGE_SCRIPT = (
    "const t = document.body ? document.body.innerText : '';"
    " return document.title === 'Error loading page' && t.startsWith('Could not load the requested page: ')"
    ' ? t.slice(34) : null'
)
"""Servo shows `resources/neterror.html` instead of failing the WebDriver navigation, so this spots that page."""
_READ_STORAGE = 'return Object.fromEntries(Object.entries(%s))'
_WRITE_STORAGE = 'const [items] = arguments; for (const [k, v] of Object.entries(items)) %s.setItem(k, v);'

# WebDriver's code points for the named `KeyboardEvent.key` values. One-character keys are sent as themselves.
WEBDRIVER_KEYS: dict[str, str] = {
    'Cancel': '\ue001',
    'Help': '\ue002',
    'Backspace': '\ue003',
    'Tab': '\ue004',
    'Clear': '\ue005',
    'Enter': '\ue007',
    'Shift': '\ue008',
    'Control': '\ue009',
    'Alt': '\ue00a',
    'Pause': '\ue00b',
    'Escape': '\ue00c',
    'PageUp': '\ue00e',
    'PageDown': '\ue00f',
    'End': '\ue010',
    'Home': '\ue011',
    'ArrowLeft': '\ue012',
    'ArrowUp': '\ue013',
    'ArrowRight': '\ue014',
    'ArrowDown': '\ue015',
    'Insert': '\ue016',
    'Delete': '\ue017',
    'Meta': '\ue03d',
    **{f'F{n}': chr(0xE031 + n - 1) for n in range(1, 13)},
}
_BUTTONS: dict[MouseButton, int] = {'left': 0, 'middle': 1, 'right': 2}


def default_binary() -> Path:
    """`$SAMMY_SERVO_BINARY`, else where the release build is unpacked under `~/.cache/sammy/servo`."""
    if env := os.environ.get('SAMMY_SERVO_BINARY'):
        return Path(env)
    if sys.platform == 'darwin':
        return Path.home() / '.cache/sammy/servo/Servo.app/Contents/MacOS/servoshell'
    return Path.home() / '.cache/sammy/servo/servo/servoshell'


def webdriver_socket(config_dir: Path) -> Path:
    """Where a jailed Servo's WebDriver is reached from the host."""
    return config_dir / 'webdriver.sock'


@dataclass(frozen=True, kw_only=True)
class ServoOptions:
    binary: Path = field(default_factory=default_binary)
    user_agent: str | None = CHROME_USER_AGENT
    """None keeps Servo's own, which says Firefox."""
    prefs: tuple[str, ...] = SITE_PREFS
    """Each is passed as `--pref=ENTRY`: a name to set it to true, or `name=value`."""
    window_size: tuple[int, int] = (1024, 740)
    """The viewport in CSS pixels. Servo's default."""
    host_file: Path | None = None
    """A hosts file, for tests that need host names such as `www.shop.test` to reach a local server."""
    http_only_export: bool = False
    """Only for a servoshell built with `patches/servo-webdriver-httponly.patch`. If an HttpOnly cookie seeded at
    `open` cannot be read back, export stays off."""
    bwrap: bool = False
    """Run Servo inside bubblewrap with its own profile folder and network namespace (Linux). Its connections go out
    through an `EgressProxy`, which refuses private addresses, and its WebDriver is reached through a Unix socket."""
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

    def command(self, *, port: int, config_dir: Path, proxy: Path | None = None) -> list[str]:
        """servoshell's command line; with `bwrap`, wrapped in the jail, with `proxy` the egress proxy's socket."""
        argv = [
            str(self.binary),
            '--headless',
            f'--webdriver={port}',
            f'--config-dir={config_dir}',
            f'--window-size={self.window_size[0]}x{self.window_size[1]}',
            *(f'--pref={pref}' for pref in self.prefs),
        ]
        if self.user_agent is not None:
            argv.append(f'--user-agent={self.user_agent}')
        if self.host_file is not None:
            argv.append(f'--host-file={self.host_file}')
        if not self.bwrap:
            return [*argv, BLANK_URL]
        if proxy is None:
            raise ValueError('a jailed Servo needs its egress proxy')
        # Every request through the proxy, loopback too: Servo tunnels http:// with CONNECT as well as https://.
        uri = f'http://127.0.0.1:{PROXY_PORT}'
        argv += [f'--pref=network_http_proxy_uri={uri}', f'--pref=network_https_proxy_uri={uri}', BLANK_URL]
        jail = bwrap_command(
            chrome=self.binary,
            profile=config_dir,
            display=None,
            proxy=proxy,
            proxy_directory=proxy.parent if self.egress_socket is not None else None,
            expose=(port, webdriver_socket(config_dir)),
            bwrap=self.bwrap_path,
        )
        return [*jail, *argv[1:]]


class ServoBackend:
    """A `BrowserBackend` on Servo. One process per `open`; see the module docstring."""

    def __init__(self, options: ServoOptions | None = None) -> None:
        self.options = options or ServoOptions()
        self._process: asyncio.subprocess.Process | None = None
        self._proxy: EgressProxy | None = None
        """This browser's own egress proxy, when jailed without a shared one."""
        self._config_dir: Path | None = None
        self._driver: _WebDriver | None = None
        self._device_pixel_ratio = 1.0
        self._http_only_readable = False
        self._cookie_paths: dict[str, set[str]] = {}
        """Host to the paths to read its cookies at on export: the seeded cookies' and the visited pages'."""
        self._local_origins: set[str] = set()
        """Origins to read localStorage for on export: seeded or visited."""
        self._walker = SnapshotWalker(run_script=self._run_walker)

    @property
    def pid(self) -> int | None:
        """The Servo process (or bwrap, which runs it) while open."""
        return self._process.pid if self._process is not None else None

    # --- BrowserBackend ---

    async def open(self, state: BrowserState | None) -> None:
        if self._driver is not None:
            raise LifecycleError('the browser is already open')
        await self._start()
        self._walker = SnapshotWalker(run_script=self._run_walker)
        self._http_only_readable = self.options.http_only_export
        if state is None:
            return
        async with self._side_tab():
            await self._seed_cookies(state.cookies)
            for origin, items in state.local_storage.items():
                await self._open_origin(origin)
                await self._script(_WRITE_STORAGE % 'localStorage', items)
                self._visited(f'{origin}/')
        await self._navigate(state.url)
        origin = origin_of(state.url)
        if origin is not None and (items := state.session_storage.get(origin)):
            await self._script(_WRITE_STORAGE % 'sessionStorage', items)
            await self._call('POST', '/refresh', {})
            await self._check_loaded(state.url)

    async def export(self) -> BrowserState:
        self._check_open()
        if not self._http_only_readable:
            raise NotSupported(
                'export',
                engine=ENGINE,
                detail='Get All Cookies leaves out HttpOnly cookies; see patches/servo-webdriver-httponly.patch',
            )
        url = await self._current_url()
        origin = origin_of(url)
        local: dict[str, dict[str, str]] = {}
        session: dict[str, dict[str, str]] = {}
        if origin is not None:
            local[origin] = await self._script(_READ_STORAGE % 'localStorage')
            session[origin] = await self._script(_READ_STORAGE % 'sessionStorage')
        async with self._side_tab():
            cookies = await self._read_cookies()
            for other in sorted(self._local_origins - {origin}):
                await self._open_origin(other)
                local[other] = await self._script(_READ_STORAGE % 'localStorage')
        return BrowserState(
            url=url,
            cookies=cookies,
            local_storage={o: items for o, items in local.items() if items},
            session_storage={o: items for o, items in session.items() if items},
        )

    async def release(self) -> BrowserState:
        state = await self.export()
        await self.close()
        return state

    async def snapshot(self) -> Snapshot:
        self._check_open()
        snapshot = await self._walker.snapshot()
        self._visited(snapshot.url)
        return snapshot

    async def act(self, action: Action) -> None:
        self._check_open()
        match await self._walker.resolve(action):  # a ref becomes a point to click, or typing at the caret
            case None:
                return  # the walker already did it, such as choosing a select's option
            case Navigate(url=url):
                await self._navigate(url)
                return
            case Click(target=target):
                if isinstance(target, Point):
                    await self._pointer({'type': 'pointerMove', **_xy(target)}, _down('left'), _up('left'))
                else:
                    element = await self._find(target)
                    await self._call('POST', f'/element/{element}/click', {})
            case Type(text=text, target=target):
                if target is None:
                    await self._keys([step for char in text for step in _press(char)])
                else:
                    element = await self._find(target)
                    await self._call('POST', f'/element/{element}/clear', {})
                    await self._call('POST', f'/element/{element}/value', {'text': text})
                return
            case Press(key=key, modifiers=modifiers):
                held = [_key(m) for m in modifiers]
                await self._keys(
                    [
                        *({'type': 'keyDown', 'value': m} for m in held),
                        *_press(_key(key)),
                        *({'type': 'keyUp', 'value': m} for m in reversed(held)),
                    ]
                )
            case Scroll(delta_x=dx, delta_y=dy, at=at):
                width, height = self.options.window_size
                where = at or Point(x=width / 2, y=height / 2)
                scroll = {'type': 'scroll', **_xy(where), 'deltaX': round(dx), 'deltaY': round(dy)}
                wheel = {'type': 'wheel', 'id': 'wheel', 'actions': [scroll]}
                await self._call('POST', '/actions', {'actions': [wheel]})
                return
            case MouseDown(at=at, button=button):
                await self._pointer({'type': 'pointerMove', **_xy(at)}, _down(button))
                return
            case MouseMove(at=at):
                await self._pointer({'type': 'pointerMove', **_xy(at)})
                return
            case MouseUp(at=at, button=button):
                await self._pointer({'type': 'pointerMove', **_xy(at)}, _up(button))
                return
        await self._settle()

    async def screenshot(self) -> Screenshot:
        self._check_open()
        png = base64.b64decode(await self._call('GET', '/screenshot'))
        width, height = struct.unpack('>II', png[16:24])  # the PNG's IHDR chunk
        ratio = self._device_pixel_ratio
        return Screenshot(png=png, width=round(width / ratio), height=round(height / ratio))

    async def close(self) -> None:
        driver, process, config_dir, proxy = self._driver, self._process, self._config_dir, self._proxy
        self._driver = self._process = self._config_dir = self._proxy = None
        self._http_only_readable = False
        self._cookie_paths = {}
        self._local_origins = set()
        if driver is not None:
            driver.close()
        if process is not None and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        if proxy is not None:
            await proxy.stop()
        if config_dir is not None:
            shutil.rmtree(config_dir, ignore_errors=True)

    # --- starting ---

    async def _start(self) -> None:
        options = self.options
        self._config_dir = Path(tempfile.mkdtemp(prefix='sammy-servo-'))
        port = _free_port()
        try:
            proxy: Path | None = None
            if options.bwrap:
                if shutil.which(options.bwrap_path) is None:
                    raise ActionFailed(f'could not start Servo: {options.bwrap_path} not found')
                if options.egress_socket is not None:
                    if options.allow_private_networks:
                        raise ActionFailed('a shared browser proxy cannot allow private networks')
                    proxy = options.egress_socket
                else:
                    self._proxy = EgressProxy(
                        self._config_dir / 'egress.sock', allow_private=options.allow_private_networks
                    )
                    await self._proxy.start()
                    proxy = self._proxy.path
            argv = options.command(port=port, config_dir=self._config_dir, proxy=proxy)
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,  # its own process group, so close() kills everything it started
            )
            self._driver = await self._new_session(
                port, unix_socket=webdriver_socket(self._config_dir) if options.bwrap else None
            )
            timeouts = {'pageLoad': int(self.options.page_load_timeout * 1000), 'script': 10_000}
            await self._call('POST', '/timeouts', timeouts)
            self._device_pixel_ratio = float(await self._script('return devicePixelRatio'))
        except BaseException:
            await self.close()
            raise

    async def _new_session(self, port: int, *, unix_socket: Path | None) -> _WebDriver:
        deadline = time.monotonic() + self.options.start_timeout
        timeout = self.options.page_load_timeout + 30
        driver = _WebDriver(port=port, unix_socket=unix_socket, timeout=timeout)
        while True:
            assert self._process is not None
            if self._process.returncode is not None:
                raise ActionFailed(f'Servo exited at start with code {self._process.returncode}')
            try:
                value = await driver.request('POST', '/session', {'capabilities': {}})
            except _Unreachable:
                if time.monotonic() > deadline:
                    raise ActionFailed('Servo did not start its WebDriver server in time') from None
                await asyncio.sleep(0.05)
                continue
            if value['capabilities'].get('browserName') != 'servo':  # another program took the port first
                raise ActionFailed('something other than Servo answered on its WebDriver port')
            driver.session = value['sessionId']
            return driver

    # --- seeding and reading state ---

    async def _seed_cookies(self, cookies: Sequence[Cookie]) -> None:
        now = time.time()
        groups: dict[tuple[str, str], list[Cookie]] = {}
        for cookie in cookies:
            if 0 <= cookie.expires < now:
                continue  # already expired: a browser would drop it
            groups.setdefault((cookie.domain.lstrip('.'), cookie.path), []).append(cookie)
        for (host, path), group in groups.items():
            self._cookie_paths.setdefault(host, set()).add(path)
            await self._open_blocked(host, path)
            for cookie in group:
                body: dict[str, Any] = {
                    'name': cookie.name,
                    'value': cookie.value,
                    'path': cookie.path,
                    'secure': cookie.secure,
                    'httpOnly': cookie.http_only,
                    'sameSite': cookie.same_site,
                }
                if cookie.domain.startswith('.'):
                    body['domain'] = host
                if cookie.expires >= 0:
                    body['expiry'] = int(cookie.expires)
                await self._call('POST', '/cookie', {'cookie': body})
            names = {c['name'] for c in await self._call('GET', '/cookie')}
            for cookie in group:
                if cookie.name in names:
                    continue
                if not cookie.http_only:
                    raise ActionFailed(f'Servo did not accept the cookie {cookie.name!r} for {cookie.domain}')
                self._http_only_readable = False  # a stock servoshell: export stays off

    async def _read_cookies(self) -> list[Cookie]:
        """Every cookie for the hosts this browser was seeded with or visited, read from `https://HOST:1/PATH`.

        Servo reports a cookie's domain without the leading dot, so a cookie whose domain is the host it was read
        on is checked again from a made-up subdomain: only a domain cookie shows there.
        """
        found: dict[tuple[str, str, str], Cookie] = {}
        for host, paths in sorted(self._cookie_paths.items()):
            for path in sorted(paths):
                wide: set[tuple[str, str, str]] = set()
                if not _is_ip(host):
                    await self._open_blocked(f'sammy-probe.{host}', path)
                    wide = {_cookie_key(c) for c in await self._call('GET', '/cookie')}
                await self._open_blocked(host, path)
                for raw in await self._call('GET', '/cookie'):
                    cookie = _cookie_from_webdriver(raw, host=host, wide=wide)
                    found[(cookie.domain, cookie.path, cookie.name)] = cookie
        return list(found.values())

    def _visited(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            return
        self._cookie_paths.setdefault(parts.hostname, set()).update({'/', parts.path or '/'})
        if origin := origin_of(url):
            self._local_origins.add(origin)

    @asynccontextmanager
    async def _side_tab(self) -> AsyncGenerator[None]:
        """Do the work in a new tab, then close it and switch back, so the user's tab and its history stay as
        they were."""
        main = await self._call('GET', '/window')
        side = await self._call('POST', '/window/new', {'type': 'tab'})
        await self._call('POST', '/window', {'handle': side['handle']})
        try:
            yield
        finally:
            await self._call('DELETE', '/window')
            await self._call('POST', '/window', {'handle': main})

    async def _open_blocked(self, host: str, path: str) -> None:
        """A page whose URL has `host`, without a request to it: Servo's error page for a blocked port."""
        await self._call('POST', '/url', {'url': f'https://{host}:{BLOCKED_PORT}{path}'})

    async def _open_origin(self, origin: str) -> None:
        """A document of `origin` without scripts, to read or write its storage."""
        await self._call('POST', '/url', {'url': f'{origin}/robots.txt'})
        actual = await self._script('return location.origin')
        if actual != origin:
            raise ActionFailed(f'could not open {origin} to reach its storage: it went to {actual}')

    # --- actions ---

    async def _navigate(self, url: str) -> None:
        try:
            await self._command('POST', '/url', {'url': url})
        except _WebDriverError as e:
            raise ActionFailed(f'could not load {url}: {e}') from None
        await self._check_loaded(url)

    async def _check_loaded(self, url: str) -> None:
        reason = await self._script(_ERROR_PAGE_SCRIPT)
        if reason is not None:
            raise ActionFailed(f'could not load {url}: {reason}')
        self._visited(await self._current_url())

    async def _find(self, target: ElementTarget) -> str:
        if isinstance(target, Ref):
            raise TypeError('refs are resolved by the snapshot walker before this')
        deadline = time.monotonic() + self.options.find_timeout
        while True:
            try:
                element = await self._command('POST', '/element', {'using': 'css selector', 'value': target.css})
                return element[_ELEMENT]
            except _WebDriverError as e:
                if e.error != 'no such element':
                    raise ActionFailed(str(e)) from None
                if time.monotonic() > deadline:
                    raise TargetNotFound(target) from None
                await asyncio.sleep(0.1)

    async def _pointer(self, *steps: dict[str, Any]) -> None:
        source = {'type': 'pointer', 'id': 'mouse', 'parameters': {'pointerType': 'mouse'}, 'actions': list(steps)}
        await self._call('POST', '/actions', {'actions': [source]})

    async def _keys(self, steps: list[dict[str, str]]) -> None:
        await self._call('POST', '/actions', {'actions': [{'type': 'key', 'id': 'keyboard', 'actions': steps}]})

    async def _settle(self) -> None:
        """Give a click or key press time to start a navigation, then wait until the document has loaded."""
        await asyncio.sleep(self.options.settle_delay)
        deadline = time.monotonic() + self.options.page_load_timeout
        while time.monotonic() < deadline:
            try:
                script: dict[str, Any] = {'script': 'return document.readyState', 'args': []}
                if await self._command('POST', '/execute/sync', script) == 'complete':
                    break
            except _WebDriverError:
                pass  # the old document went away mid-call
            await asyncio.sleep(0.05)
        self._visited(await self._current_url())

    # --- WebDriver plumbing ---

    def _check_open(self) -> None:
        if self._driver is None:
            raise LifecycleError('the browser is not open')

    async def _current_url(self) -> str:
        return await self._call('GET', '/url')

    async def _script(self, script: str, *args: Any) -> Any:
        return await self._call('POST', '/execute/sync', {'script': script, 'args': list(args)})

    async def _run_walker(self, function: str, arg: JSON, /) -> object:
        return await self._script(webdriver_script(function), arg)

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """A command in this session, with WebDriver errors as `ActionFailed`. Servo's messages carry no page data."""
        try:
            return await self._command(method, path, body)
        except _WebDriverError as e:
            raise ActionFailed(str(e)) from None

    async def _command(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """A command in this session. Raises `_WebDriverError`."""
        driver = self._driver
        if driver is None:
            raise LifecycleError('the browser is not open')
        try:
            return await driver.request(method, f'/session/{driver.session}{path}', body)
        except _Unreachable:
            raise ActionFailed('Servo stopped answering; it may have crashed') from None


# --- helpers ---


class _Unreachable(Exception):
    pass


class _WebDriverError(Exception):
    def __init__(self, error: str, message: str) -> None:
        self.error = error
        super().__init__(f'{error}: {message}' if message else error)


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP over a Unix socket: a jailed Servo's WebDriver (`webdriver_socket`). The Host header still names the
    port inside the jail, `127.0.0.1:PORT`, because Servo's WebDriver server refuses any other."""

    def __init__(self, path: Path, *, port: int, timeout: float) -> None:
        super().__init__('127.0.0.1', port, timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(self._path))
        except OSError:
            sock.close()
            raise
        self.sock = sock


class _WebDriver:
    """A blocking keep-alive HTTP connection, used from a worker thread. One request at a time."""

    def __init__(self, *, port: int, unix_socket: Path | None, timeout: float) -> None:
        self._connection = (
            http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
            if unix_socket is None
            else _UnixHTTPConnection(unix_socket, port=port, timeout=timeout)
        )
        self._lock = asyncio.Lock()
        self.session = ''

    async def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        async with self._lock:
            return await asyncio.to_thread(self._request, method, path, body)

    def _request(self, method: str, path: str, body: dict[str, Any] | None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        headers = {} if data is None else {'Content-Type': 'application/json'}
        try:
            self._connection.request(method, path, body=data, headers=headers)
            response = self._connection.getresponse()
            raw = response.read()
        except (OSError, http.client.HTTPException):
            self._connection.close()
            raise _Unreachable from None
        value = json.loads(raw)['value'] if raw else None
        if response.status >= 400:
            details: dict[str, Any] = value or {}
            raise _WebDriverError(str(details.get('error', response.status)), str(details.get('message', '')))
        return value

    def close(self) -> None:
        self._connection.close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _cookie_key(raw: dict[str, Any]) -> tuple[str, str, str]:
    return raw['name'], raw.get('domain') or '', raw.get('path') or '/'


def _cookie_from_webdriver(raw: dict[str, Any], *, host: str, wide: set[tuple[str, str, str]]) -> Cookie:
    """A cookie from Get All Cookies, read on `host`. `wide` holds the keys of the cookies that also showed on a
    subdomain of `host`, so are domain cookies even when Servo reports their domain as `host`."""
    domain: str = raw.get('domain') or host
    path: str = raw.get('path') or '/'
    is_domain_cookie = domain != host or _cookie_key(raw) in wide
    same_site: SameSite = raw.get('sameSite') or 'Lax'
    return Cookie(
        name=raw['name'],
        value=raw['value'],
        domain=f'.{domain}' if is_domain_cookie else domain,
        path=path,
        expires=float(raw.get('expiry', -1)),
        http_only=bool(raw.get('httpOnly', False)),
        secure=bool(raw.get('secure', False)),
        same_site=same_site,
    )


def _key(key: str) -> str:
    """A `KeyboardEvent.key` value as WebDriver's key action value."""
    if key in WEBDRIVER_KEYS:
        return WEBDRIVER_KEYS[key]
    if len(key) == 1:
        return key
    raise ActionFailed(f'unknown key {key!r}: use a KeyboardEvent.key value such as Enter, ArrowDown or a')


def _press(value: str) -> list[dict[str, str]]:
    return [{'type': 'keyDown', 'value': value}, {'type': 'keyUp', 'value': value}]


def _xy(at: Point) -> dict[str, Any]:
    return {'x': round(at.x), 'y': round(at.y), 'origin': 'viewport', 'duration': 0}


def _down(button: MouseButton) -> dict[str, Any]:
    return {'type': 'pointerDown', 'button': _BUTTONS[button]}


def _up(button: MouseButton) -> dict[str, Any]:
    return {'type': 'pointerUp', 'button': _BUTTONS[button]}
