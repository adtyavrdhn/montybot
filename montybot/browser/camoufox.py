"""`CamoufoxBackend`: Camoufox, an anti-detect Firefox, behind the browser contract, over WebDriver BiDi. Issue #18.

Camoufox (github.com/daijro/camoufox, MPL-2.0) is Firefox with its fingerprint (user agent, screen, WebGL, fonts, speech
voices and more) replaced in C++ from a config it reads from the environment at start. Tested with Camoufox
156.0.1-beta.36. See `camoufox.md` next to this file for the decisions and the evaluation.

- **WebDriver BiDi, not Playwright.** Firefox's own remote agent (`--remote-debugging-port`) is in the Camoufox build
  and keeps `navigator.webdriver` false there. It needs no Node driver and does not tie Camoufox to Playwright's Juggler
  protocol, whose version Camoufox's own package pins below the Playwright this repo uses.
- **One Camoufox process per `open`**, with a throwaway profile and a free port. `close` sends SIGKILL to the process
  group: the profile is thrown away, so there is nothing to shut down cleanly.
- **A fixed fingerprint per platform** (`camoufox_fingerprints/`): a Windows desktop on Linux, a Mac on macOS, en-US.
  Each was generated once with Camoufox's Python package. A Mac identity on a Mac keeps start fast: any other identity
  makes Camoufox register its bundled fonts with macOS, about 25 seconds per start.
- **State without pages.** BiDi's storage module reads and writes every cookie, HttpOnly too, for any host, so `open`
  and `export` need no page for cookies. localStorage still needs a document of its origin: it is read and written on
  `ORIGIN/robots.txt` in a background tab. sessionStorage goes in with a preload script, before the first page's
  scripts run.
- **Scripts run in a sandbox** (a BiDi sandbox realm), so pages cannot see the snapshot walker's globals.
- **On the Linux server** (`CamoufoxOptions.server()`), Camoufox runs headed in its own Xvfb screen, in the same jail
  as Chromium (`chromium_linux`): its own network namespace, whose only way out is the `EgressProxy`, set as Firefox's
  SOCKS5 proxy for every request, loopback too. BiDi's port is reached only through a Unix socket in the profile.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import shutil
import signal
import socket
import struct
import sys
import tempfile
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

from websockets.asyncio.client import ClientConnection, connect, unix_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from montybot.browser.chromium_linux import VirtualScreen, bwrap_command, start_virtual_screen
from montybot.browser.contract import (
    MAX_DOWNLOAD_BYTES,
    Action,
    ActionFailed,
    Click,
    Download,
    ElementTarget,
    LifecycleError,
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
from montybot.browser.egress import PROXY_PORT, EgressProxy
from montybot.browser.servo import WEBDRIVER_KEYS
from montybot.browser.snapshot import JSON, SnapshotWalker
from montybot.browser.state import BLANK_URL, BrowserState, Cookie, SameSite, origin_of

ENGINE = 'camoufox'
VERSION = '156.0.1-beta.36'
"""The Camoufox release the fingerprints and tests are for."""

Identity = Literal['windows', 'macos']
Pref = bool | int | str
T = TypeVar('T')

SANDBOX = 'montybot'
"""The BiDi sandbox our scripts run in: the page's DOM, but not its globals, and the page cannot see ours."""

BASE_PREFS: dict[str, Pref] = {
    # The remote agent's "recommended" prefs are for test suites (popups allowed, permissions in testing mode, ...);
    # keep Camoufox's own defaults instead.
    'remote.prefs.recommended': False,
    'intl.accept_languages': 'en-US, en',
    'intl.locale.requested': 'en-US',
    'privacy.donottrackheader.enabled': False,
    'privacy.globalprivacycontrol.enabled': False,
    'webgl.enable-webgl2': True,
    'webgl.force-enabled': True,
    'ui.useOverlayScrollbars': 1,
    # WebRTC stays on, as in any Firefox, but its ICE never uses UDP or shows local addresses.
    'media.peerconnection.ice.proxy_only': True,
    'media.peerconnection.ice.default_address_only': True,
    'media.peerconnection.ice.no_host': True,
    'browser.shell.checkDefaultBrowser': False,
    'browser.startup.homepage_override.mstone': 'ignore',
    'browser.sessionstore.resume_from_crash': False,
}
"""Firefox prefs for every Camoufox, written to the profile's `user.js`. The values Camoufox's Python package sets
for these identities, plus quiet start-up."""

PROXY_PREFS: dict[str, Pref] = {
    'network.proxy.type': 1,
    'network.proxy.socks': '127.0.0.1',
    'network.proxy.socks_port': PROXY_PORT,
    'network.proxy.socks_version': 5,
    'network.proxy.socks_remote_dns': True,
    'network.proxy.socks5_remote_dns': True,
    'network.proxy.allow_hijacking_localhost': True,
    'network.proxy.no_proxies_on': '',
    'network.proxy.failover_direct': False,
    'network.trr.mode': 5,
}
"""In the jail: every request through the egress proxy's SOCKS5 port, loopback too, with DNS resolved by the proxy
(so it checks the address it connects to) and no DNS over HTTPS of Firefox's own."""

_CONFIG_CHUNK = 32767
"""Camoufox reads its config from `CAMOU_CONFIG_1`, `CAMOU_CONFIG_2`, ... in pieces of at most this many characters."""

_ORIGIN = "() => document.documentURI.startsWith('about:') ? null : location.origin"
"""The page's origin, or null on Firefox's error page, whose `location` still shows the URL that failed."""
_READ_STORAGE = '(name) => Object.fromEntries(Object.entries(window[name]))'
_WRITE_STORAGE = '(name, items) => { for (const [k, v] of Object.entries(items)) window[name].setItem(k, v); }'
_SEED_SESSION_STORAGE = """() => {
  const items = %s[location.origin];
  if (items) for (const [key, value] of Object.entries(items)) sessionStorage.setItem(key, value);
}"""
_FIND = """(css) => {
  let element;
  try { element = document.querySelector(css); } catch { return {error: 'invalid'}; }
  if (!element) return null;
  element.scrollIntoView({block: 'center', inline: 'center'});
  const box = element.getBoundingClientRect();
  if (!box.width && !box.height) return {error: 'hidden'};
  return {x: box.left + box.width / 2, y: box.top + box.height / 2};
}"""
_FOCUS = """(css) => {
  const element = document.querySelector(css);
  if (!element) return false;
  element.focus();
  if (typeof element.select === 'function') element.select();
  else if (element.isContentEditable) getSelection().selectAllChildren(element);
  return true;
}"""
_NAVIGATION_EVENTS = (
    'browsingContext.navigationStarted',
    'browsingContext.load',
    'browsingContext.navigationFailed',
    'browsingContext.fragmentNavigated',
    'browsingContext.downloadWillBegin',
    'browsingContext.downloadEnd',
)
_BUTTONS: dict[MouseButton, int] = {'left': 0, 'middle': 1, 'right': 2}
_SAME_SITE_IN: dict[str, SameSite] = {'strict': 'Strict', 'lax': 'Lax', 'none': 'None', 'default': 'Lax'}


def default_binary() -> Path:
    """`$MONTYBOT_CAMOUFOX_BINARY`, else where the release is unpacked under `~/.cache/montybot/camoufox`."""
    if env := os.environ.get('MONTYBOT_CAMOUFOX_BINARY'):
        return Path(env)
    root = Path.home() / '.cache/montybot/camoufox'
    if sys.platform == 'darwin':
        return root / 'Camoufox.app/Contents/MacOS/camoufox'
    return root / 'camoufox'


def default_identity() -> Identity:
    """The host's own OS on a Mac, which keeps start fast; a Windows desktop elsewhere, the most common one."""
    return 'macos' if sys.platform == 'darwin' else 'windows'


def default_font_cache() -> Path:
    return Path.home() / '.cache/montybot/camoufox-fontconfig'


def fingerprint(identity: Identity) -> dict[str, JSON]:
    """The pinned Camoufox config for `identity`, from `camoufox_fingerprints/`."""
    text = files('montybot.browser').joinpath('camoufox_fingerprints', f'{identity}.json').read_text(encoding='utf-8')
    return cast(dict[str, JSON], json.loads(text))


def bidi_socket(profile: Path) -> Path:
    """Where a jailed Camoufox's BiDi port is reached from the host."""
    return profile / 'bidi.sock'


@dataclass(frozen=True, kw_only=True)
class CamoufoxOptions:
    """How to start Camoufox. The defaults suit a Mac or a Linux desktop."""

    binary: Path = field(default_factory=default_binary)
    identity: Identity = field(default_factory=default_identity)
    """Which pinned fingerprint to wear. On a Mac, keep `macos`: another OS's costs about 25 s per start."""
    config: Mapping[str, JSON] = field(default_factory=dict[str, JSON])
    """Camoufox config keys over the fingerprint, such as `{'timezone': 'America/Chicago'}`."""
    prefs: Mapping[str, Pref] = field(default_factory=dict[str, Pref])
    """Firefox prefs over `BASE_PREFS`."""
    headless: bool = False
    """No window. Camoufox recommends a real or virtual screen, so the server runs headed in Xvfb."""
    virtual_screen: bool = False
    """Give each browser its own Xvfb screen (Linux, headed only)."""
    window_size: tuple[int, int] = (1280, 800)
    """The window's outer size in pixels, and the virtual screen's size."""
    bwrap: bool = False
    """Run Camoufox inside bubblewrap with its own profile folder and network namespace (Linux). Its connections go
    out through an `EgressProxy`, which refuses private addresses, and BiDi is reached through a Unix socket."""
    allow_private_networks: bool = False
    """With `bwrap`: let pages reach loopback and private addresses. Only for local fixture sites in tests."""
    egress_socket: Path | None = None
    """A shared egress proxy in a separate container. Unset: start a private proxy for this browser (local tests)."""
    font_cache: Path | None = field(default_factory=default_font_cache)
    """Linux: a fontconfig cache for the bundled fonts, built once with `fc-cache` and mounted read-only in the jail.
    Without it, every start scans about a gigabyte of fonts."""
    bwrap_path: str = 'bwrap'
    xvfb_path: str = 'Xvfb'
    fc_cache_path: str = 'fc-cache'
    start_timeout: float = 30
    page_load_timeout: float = 30
    find_timeout: float = 3
    """How long `act` waits for a selector to match before `TargetNotFound`."""
    settle_delay: float = 0.1
    """How long a click or key press gets to start a navigation before `act` returns."""

    @classmethod
    def server(cls, **overrides: Any) -> CamoufoxOptions:
        """The Linux server: bwrap, and an Xvfb screen per browser."""
        return cls(virtual_screen=True, bwrap=True, **overrides)

    @property
    def resources(self) -> Path:
        """The folder with Camoufox's `fonts` and `fontconfig`: next to the binary, or the app bundle's Resources."""
        if self.binary.parent.name == 'MacOS':
            return self.binary.parent.parent / 'Resources'
        return self.binary.parent

    def camoufox_config(self) -> dict[str, JSON]:
        width, height = self.window_size
        return {**fingerprint(self.identity), 'window.outerWidth': width, 'window.outerHeight': height, **self.config}

    def user_prefs(self) -> dict[str, Pref]:
        return {**BASE_PREFS, **(PROXY_PREFS if self.bwrap else {}), **self.prefs}

    def uses_fontconfig(self) -> bool:
        """Linux: Camoufox's fonts come from a fontconfig file listing the bundled fonts the identity's OS has."""
        return sys.platform != 'darwin'

    def fontconfig(self) -> str:
        """The fontconfig file for this identity: Camoufox's own, with absolute font folders and our cache folder."""
        resources = self.resources
        os_dir = {'windows': 'windows', 'macos': 'macos'}[self.identity]
        os_key = {'windows': 'win', 'macos': 'mac'}[self.identity]
        groups = json.loads((resources / 'fonts/groups.json').read_text())['readBy'][os_key]
        text = (resources / 'fontconfig' / os_dir / 'fonts.conf').read_text()
        folders = ''.join(f'<dir>{resources / "fonts" / group}</dir>' for group in groups)
        text = text.replace('<dir prefix="cwd">fonts</dir>', folders)
        if self.font_cache is not None:
            text = text.replace(
                '<cachedir prefix="xdg">fontconfig</cachedir>', f'<cachedir>{self.font_cache}</cachedir>'
            )
        return text

    def environment(self, *, profile: Path) -> dict[str, str]:
        """The variables Camoufox reads: its config in pieces, and on Linux the fontconfig file in the profile."""
        config = json.dumps(self.camoufox_config(), separators=(',', ':'), ensure_ascii=True)
        env = {
            f'CAMOU_CONFIG_{n + 1}': config[i : i + _CONFIG_CHUNK]
            for n, i in enumerate(range(0, len(config), _CONFIG_CHUNK))
        }
        if self.uses_fontconfig():
            env['FONTCONFIG_FILE'] = str(profile / 'fonts.conf')
        return env

    def command(
        self, *, port: int, profile: Path, proxy: Path | None = None, display: VirtualScreen | None = None
    ) -> list[str]:
        """Camoufox's command line; with `bwrap`, wrapped in the jail, with `proxy` the egress proxy's socket."""
        argv = [str(self.binary), '--no-remote', '--profile', str(profile), f'--remote-debugging-port={port}']
        if self.headless:
            width, height = self.window_size
            argv += ['--headless', f'--window-size={width},{height}']
        argv.append(BLANK_URL)
        if not self.bwrap:
            return argv
        if proxy is None:
            raise ValueError('a jailed Camoufox needs its egress proxy')
        jail = bwrap_command(
            chrome=self.binary,
            profile=profile,
            display=display.display if display is not None else None,
            proxy=proxy,
            proxy_directory=proxy.parent if self.egress_socket is not None else None,
            expose=(port, bidi_socket(profile)),
            env=self.environment(profile=profile),
            read_only=[self.font_cache] if self.font_cache is not None and self.uses_fontconfig() else [],
            bwrap=self.bwrap_path,
        )
        return [*jail, *argv[1:]]


@dataclass(kw_only=True)
class _Watch:
    """Whether an action started a navigation of the tab, and whether it has ended."""

    started: asyncio.Event = field(default_factory=asyncio.Event)
    ended: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(kw_only=True)
class _Downloading:
    name: str
    started: float
    finished: asyncio.Future[Path | None]


@dataclass(kw_only=True)
class _Session:
    """One running Camoufox and everything started for it."""

    workdir: Path
    profile: Path
    process: asyncio.subprocess.Process | None = None
    bidi: _BiDi | None = None
    context: str = ''
    """The tab's browsing context: the one the agent sees."""
    screen: VirtualScreen | None = None
    proxy: EgressProxy | None = None
    device_pixel_ratio: float = 1
    origins: set[str] = field(default_factory=set[str])
    """Origins to read localStorage for on export: seeded, or loaded in the tab."""
    watch: _Watch | None = None
    downloads: list[_Downloading] = field(default_factory=list[_Downloading])
    """Downloads started and not yet taken."""
    running: dict[str, _Downloading] = field(default_factory=dict[str, _Downloading])
    """Downloads not finished yet, by BiDi's download id."""


class CamoufoxBackend:
    """A `BrowserBackend` on Camoufox. One process per `open`; see the module docstring."""

    def __init__(self, options: CamoufoxOptions | None = None) -> None:
        self.options = options or CamoufoxOptions()
        self._session: _Session | None = None
        self._walker = SnapshotWalker(run_script=self._run_walker)

    @property
    def pid(self) -> int | None:
        """The Camoufox process (or bwrap, which runs it) while open."""
        session = self._session
        return session.process.pid if session is not None and session.process is not None else None

    @property
    def workdir(self) -> Path | None:
        """The folder holding this browser's profile and launch files, while open."""
        return self._session.workdir if self._session is not None else None

    # --- BrowserBackend ---

    async def open(self, state: BrowserState | None) -> None:
        if self._session is not None:
            raise LifecycleError('the browser is already open')
        await self._start()
        self._walker = SnapshotWalker(run_script=self._run_walker)
        if state is None:
            return
        try:
            preload = await self._seed(state)
        except BaseException:
            await self.close()
            raise
        try:
            await self._navigate(state.url)
        finally:
            if preload is not None:
                await self._call('script.removePreloadScript', {'script': preload})

    async def export(self) -> BrowserState:
        session = self._require()
        url = await self._current_url()
        origin = origin_of(url)
        local: dict[str, dict[str, str]] = {}
        session_storage: dict[str, dict[str, str]] = {}
        if origin is not None and await self._script(_ORIGIN) == origin:  # not an error page
            local[origin] = cast(dict[str, str], await self._script(_READ_STORAGE, 'localStorage'))
            session_storage[origin] = cast(dict[str, str], await self._script(_READ_STORAGE, 'sessionStorage'))
        raw = await self._call('storage.getCookies', {})
        cookies = [_cookie_from_bidi(c) for c in raw['cookies']]
        others = sorted(session.origins - set(local))
        if others:
            async with self._side_tab() as tab:
                for other in others:
                    if await self._open_origin(tab, other, required=False):
                        local[other] = cast(dict[str, str], await self._script(_READ_STORAGE, 'localStorage', at=tab))
        return BrowserState(
            url=url,
            cookies=cookies,
            local_storage={o: items for o, items in local.items() if items},
            session_storage={o: items for o, items in session_storage.items() if items},
        )

    async def release(self) -> BrowserState:
        state = await self.export()
        await self.close()
        return state

    async def snapshot(self) -> Snapshot:
        self._require()
        return await self._walker.snapshot()

    async def act(self, action: Action) -> None:
        self._require()
        match await self._walker.resolve(action):  # a ref becomes a point to click, or typing at the caret
            case None:
                return  # the walker already did it, such as choosing a select's option
            case Navigate(url=url):
                await self._navigate(url)
            case Click(target=target):
                at = target if isinstance(target, Point) else await self._find(target)
                async with self._settled():
                    await self._pointer({'type': 'pointerMove', **_xy(at)}, _down('left'), _up('left'))
            case Type(text=text, target=target):
                keys = [WEBDRIVER_KEYS['Enter'] if char == '\n' else char for char in text]
                if isinstance(target, Selector):
                    await self._find(target)
                    await self._script(_FOCUS, target.css)
                    keys = keys or [WEBDRIVER_KEYS['Delete']]  # clear the selected value
                if not keys:
                    return
                async with self._settled():
                    await self._keys([step for key in keys for step in _press(key)])
            case Press(key=key, modifiers=modifiers):
                held = [_key(m) for m in modifiers]
                async with self._settled():
                    await self._keys(
                        [
                            *({'type': 'keyDown', 'value': m} for m in held),
                            *_press(_key(key)),
                            *({'type': 'keyUp', 'value': m} for m in reversed(held)),
                        ]
                    )
            case Scroll(delta_x=dx, delta_y=dy, at=at):
                where = at or await self._centre()
                scroll = {'type': 'scroll', **_xy(where), 'deltaX': round(dx), 'deltaY': round(dy)}
                await self._perform([{'type': 'wheel', 'id': 'wheel', 'actions': [scroll]}])
            case MouseDown(at=at, button=button):
                await self._pointer({'type': 'pointerMove', **_xy(at)}, _down(button))
            case MouseMove(at=at):
                await self._pointer({'type': 'pointerMove', **_xy(at)})
            case MouseUp(at=at, button=button):
                async with self._settled():
                    await self._pointer({'type': 'pointerMove', **_xy(at)}, _up(button))

    async def screenshot(self) -> Screenshot:
        session = self._require()
        result = await self._call(
            'browsingContext.captureScreenshot', {'context': session.context, 'origin': 'viewport'}
        )
        png = base64.b64decode(result['data'])
        width, height = struct.unpack('>II', png[16:24])  # the PNG's IHDR chunk
        ratio = session.device_pixel_ratio
        return Screenshot(png=png, width=round(width / ratio), height=round(height / ratio))

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is None:
            return
        for downloading in session.downloads:
            downloading.finished.cancel()
        if session.bidi is not None:
            await session.bidi.close()
        process = session.process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        if session.screen is not None:
            await session.screen.stop()
        if session.proxy is not None:
            await session.proxy.stop()
        shutil.rmtree(session.workdir, ignore_errors=True)

    # --- DownloadsBackend (#21) ---

    async def take_downloads(self) -> list[Download]:
        """The downloads finished since the last call. One still running gets up to `page_load_timeout` from its
        start to finish, else it is dropped. A failed download is dropped."""
        session = self._require()
        pending, session.downloads = session.downloads, []
        if not pending:
            return []
        deadline = max(d.started for d in pending) + self.options.page_load_timeout
        await asyncio.wait([d.finished for d in pending], timeout=max(0, deadline - time.monotonic()))
        taken: list[Download] = []
        for downloading in pending:
            future = downloading.finished
            if not future.done() or future.cancelled() or (path := future.result()) is None:
                future.cancel()
                continue
            try:
                too_large = (await asyncio.to_thread(path.stat)).st_size > MAX_DOWNLOAD_BYTES
                data = b'' if too_large else await asyncio.to_thread(path.read_bytes)
                path.unlink()
            except OSError:
                continue
            taken.append(Download(name=downloading.name, data=data, too_large=too_large))
        return taken

    # --- starting ---

    async def _start(self) -> None:
        options = self.options
        workdir = Path(tempfile.mkdtemp(prefix='montybot-camoufox-'))
        profile = workdir / 'profile'
        profile.mkdir()
        (profile / 'downloads').mkdir()
        session = self._session = _Session(workdir=workdir, profile=profile)
        port = _free_port()
        try:
            _write_prefs(profile / 'user.js', options.user_prefs())
            env = {**os.environ, **options.environment(profile=profile)}
            if options.uses_fontconfig():
                (profile / 'fonts.conf').write_text(options.fontconfig())
                await self._warm_font_cache(env)
            proxy: Path | None = None
            if options.virtual_screen and not options.headless:
                try:
                    session.screen = await start_virtual_screen(
                        workdir=workdir,
                        width=options.window_size[0],
                        height=options.window_size[1],
                        xvfb=options.xvfb_path,
                    )
                except (OSError, RuntimeError) as error:
                    raise ActionFailed(f'could not start a virtual screen: {error}') from error
                env |= {'DISPLAY': session.screen.display.name, 'XAUTHORITY': str(session.screen.display.xauthority)}
            if options.bwrap:
                if shutil.which(options.bwrap_path) is None:
                    raise ActionFailed(f'could not start Camoufox: {options.bwrap_path} not found')
                if options.egress_socket is not None:
                    if options.allow_private_networks:
                        raise ActionFailed('a shared browser proxy cannot allow private networks')
                    proxy = options.egress_socket
                else:
                    session.proxy = EgressProxy(workdir / 'egress.sock', allow_private=options.allow_private_networks)
                    await session.proxy.start()
                    proxy = session.proxy.path
            argv = options.command(port=port, profile=profile, proxy=proxy, display=session.screen)
            with (workdir / 'camoufox.log').open('wb') as log:
                session.process = await asyncio.create_subprocess_exec(
                    *argv,
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,  # its own process group, so close() kills everything it started
                )
            session.bidi = await self._connect(session, port)
            await self._new_session(session)
        except BaseException:
            await self.close()
            raise

    async def _warm_font_cache(self, env: Mapping[str, str]) -> None:
        """Build the fontconfig cache for the bundled fonts once, outside the jail, with `fc-cache`."""
        cache = self.options.font_cache
        if cache is None:
            return
        cache.mkdir(parents=True, exist_ok=True)
        config = Path(env['FONTCONFIG_FILE'])
        marker = cache / f'montybot-{hashlib.sha256(config.read_bytes()).hexdigest()[:16]}'
        if marker.exists() or shutil.which(self.options.fc_cache_path) is None:
            return
        async with _font_cache_lock():
            if marker.exists():
                return
            process = await asyncio.create_subprocess_exec(
                self.options.fc_cache_path,
                env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'FONTCONFIG_FILE': env['FONTCONFIG_FILE']},
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if await process.wait() == 0:
                marker.touch()

    async def _connect(self, session: _Session, port: int) -> _BiDi:
        deadline = time.monotonic() + self.options.start_timeout
        uri = f'ws://127.0.0.1:{port}/session'  # the Host header Firefox's remote agent accepts, jailed too
        while True:
            assert session.process is not None
            if session.process.returncode is not None:
                raise ActionFailed(f'Camoufox exited at start with code {session.process.returncode}{self._log_tail()}')
            try:
                if self.options.bwrap:
                    connection = await unix_connect(str(bidi_socket(session.profile)), uri, max_size=None)
                else:
                    connection = await connect(uri, max_size=None)
            except (OSError, EOFError, WebSocketException, TimeoutError):
                if time.monotonic() > deadline:
                    raise ActionFailed(f'Camoufox did not start its BiDi server in time{self._log_tail()}') from None
                await asyncio.sleep(0.05)
                continue
            return _BiDi(connection, on_event=self._on_event)

    async def _new_session(self, session: _Session) -> None:
        assert session.bidi is not None
        capabilities = {'alwaysMatch': {'unhandledPromptBehavior': {'default': 'dismiss', 'beforeUnload': 'accept'}}}
        result = await asyncio.wait_for(
            self._call('session.new', {'capabilities': capabilities}), self.options.start_timeout
        )
        if result['capabilities'].get('browserName') != ENGINE:  # another program took the port first
            raise ActionFailed('something other than Camoufox answered on its BiDi port')
        await self._call('session.subscribe', {'events': list(_NAVIGATION_EVENTS)})
        tree = await self._call('browsingContext.getTree', {'maxDepth': 0})
        session.context = tree['contexts'][0]['context']
        downloads = {'type': 'allowed', 'destinationFolder': str(session.profile / 'downloads')}
        await self._call('browser.setDownloadBehavior', {'downloadBehavior': downloads})
        session.device_pixel_ratio = float(cast(float, await self._script('() => devicePixelRatio')))

    def _log_tail(self) -> str:
        """The end of Camoufox's own output, for a start that failed. Nothing has loaded a page yet."""
        session = self._session
        if session is None:
            return ''
        with contextlib.suppress(OSError):
            tail = (session.workdir / 'camoufox.log').read_text(errors='replace')[-500:].strip()
            if tail:
                return f': {tail}'
        return ''

    # --- events ---

    def _on_event(self, method: str, params: dict[str, Any]) -> None:
        session = self._session
        if session is None:
            return
        if method == 'browsingContext.downloadWillBegin':
            future = asyncio.get_running_loop().create_future()
            name = str(params.get('suggestedFilename') or 'download')
            downloading = _Downloading(name=name, started=time.monotonic(), finished=future)
            session.downloads.append(downloading)
            session.running[params['download']] = downloading
        elif method == 'browsingContext.downloadEnd':
            downloading = session.running.pop(params['download'], None)
            if downloading is not None and not downloading.finished.done():
                path = params.get('filepath')
                ok = params.get('status') == 'complete' and isinstance(path, str)
                downloading.finished.set_result(Path(cast(str, path)) if ok else None)
        if params.get('context') != session.context:
            return
        watch = session.watch
        if method == 'browsingContext.navigationStarted':
            if watch is not None:
                watch.started.set()
        elif watch is not None and watch.started.is_set():
            watch.ended.set()  # loaded, failed, aborted, a fragment, or a download instead of a page
        if method == 'browsingContext.load' and (origin := origin_of(str(params.get('url', '')))):
            session.origins.add(origin)

    @asynccontextmanager
    async def _settled(self) -> AsyncGenerator[None]:
        """Run the body, then, if it started a navigation of the tab, wait until the new page has loaded."""
        session = self._require()
        watch = session.watch = _Watch()
        try:
            yield
            if await _wait(watch.started.wait(), self.options.settle_delay):
                await _wait(watch.ended.wait(), self.options.page_load_timeout)
        finally:
            session.watch = None

    # --- seeding and reading state ---

    async def _seed(self, state: BrowserState) -> str | None:
        """Put the cookies and localStorage in place, and add a preload script for the first page's sessionStorage.
        Returns that script's id, to remove once the page has loaded."""
        session = self._require()
        now = time.time()
        for cookie in state.cookies:
            if 0 <= cookie.expires < now:
                continue  # already expired: a browser would drop it
            try:
                await self._command('storage.setCookie', {'cookie': _cookie_to_bidi(cookie)})
            except _BiDiError:
                # Firefox's message quotes the cookie, so it is left out.
                raise ActionFailed(f'Camoufox did not accept the cookie {cookie.name!r} for {cookie.domain}') from None
        if state.local_storage:
            async with self._side_tab() as tab:
                for origin, items in state.local_storage.items():
                    await self._open_origin(tab, origin, required=True)
                    await self._script(_WRITE_STORAGE, 'localStorage', cast(JSON, items), at=tab)
        session.origins |= set(state.local_storage)
        origin = origin_of(state.url)
        if origin is None or not (items := state.session_storage.get(origin)):
            return None
        seed = _SEED_SESSION_STORAGE % json.dumps({origin: items})
        params = {'functionDeclaration': seed, 'contexts': [session.context], 'sandbox': SANDBOX}
        return str((await self._call('script.addPreloadScript', params))['script'])

    @asynccontextmanager
    async def _side_tab(self) -> AsyncGenerator[str]:
        """A background tab for work on other origins, so the user's tab and its history stay as they were."""
        tab = (await self._call('browsingContext.create', {'type': 'tab', 'background': True}))['context']
        try:
            yield tab
        finally:
            with contextlib.suppress(ActionFailed):
                await self._call('browsingContext.close', {'context': tab})

    async def _open_origin(self, tab: str, origin: str, *, required: bool) -> bool:
        """Load a document of `origin` without scripts, to reach its storage. False if it went elsewhere."""
        try:
            await self._command(
                'browsingContext.navigate', {'context': tab, 'url': f'{origin}/robots.txt', 'wait': 'complete'}
            )
        except _BiDiError as error:
            if required:
                raise ActionFailed(f'could not open {origin} to reach its storage: {error.message}') from None
            return False
        actual = await self._script('() => location.origin', at=tab)
        if actual != origin and required:
            raise ActionFailed(f'could not open {origin} to reach its storage: it went to {actual}')
        return actual == origin

    # --- actions ---

    async def _navigate(self, url: str) -> None:
        session = self._require()
        params = {'context': session.context, 'url': url, 'wait': 'complete'}
        try:
            await asyncio.wait_for(self._command('browsingContext.navigate', params), self.options.page_load_timeout)
        except TimeoutError:
            raise ActionFailed(f'{url} did not finish loading in {self.options.page_load_timeout:g} s') from None
        except _BiDiError as error:
            raise ActionFailed(f'could not load {url}: {error.message.removeprefix("Error: ")}') from None

    async def _find(self, target: ElementTarget) -> Point:
        """Wait for the selector to match, scroll its element into view, and return its centre."""
        if isinstance(target, Ref):
            raise TypeError('refs are resolved by the snapshot walker before this')
        deadline = time.monotonic() + self.options.find_timeout
        while True:
            found = await self._script(_FIND, target.css)
            if isinstance(found, dict) and 'x' in found:
                return Point(x=float(cast(float, found['x'])), y=float(cast(float, found['y'])))
            if isinstance(found, dict) and found.get('error') == 'invalid':
                raise TargetNotFound(target, 'not a valid CSS selector')
            if time.monotonic() > deadline:
                if found is None:
                    raise TargetNotFound(target)
                raise ActionFailed(f'{target.css} matches an element that is not shown')
            await asyncio.sleep(0.1)

    async def _centre(self) -> Point:
        width, height = cast(list[float], await self._script('() => [innerWidth, innerHeight]'))
        return Point(x=width / 2, y=height / 2)

    async def _pointer(self, *steps: dict[str, Any]) -> None:
        source = {'type': 'pointer', 'id': 'mouse', 'parameters': {'pointerType': 'mouse'}, 'actions': list(steps)}
        await self._perform([source])

    async def _keys(self, steps: list[dict[str, str]]) -> None:
        await self._perform([{'type': 'key', 'id': 'keyboard', 'actions': steps}])

    async def _perform(self, actions: list[dict[str, Any]]) -> None:
        await self._call('input.performActions', {'context': self._require().context, 'actions': actions})

    # --- BiDi plumbing ---

    def _require(self) -> _Session:
        if self._session is None or self._session.bidi is None:
            raise LifecycleError('the browser is not open')
        return self._session

    async def _current_url(self) -> str:
        return str(await self._script('() => location.href'))

    async def _run_walker(self, function: str, arg: JSON, /) -> object:
        return await self._script(function, arg)

    async def _script(self, function: str, *args: JSON, at: str | None = None) -> JSON:
        """Call `function` with JSON arguments in our sandbox of the tab (or `at`), and return its JSON result.
        Retried while a navigation replaces the document under it."""
        declaration = f'async (...args) => JSON.stringify(await ({function})(...args.map((a) => JSON.parse(a))))'
        params = {
            'functionDeclaration': declaration,
            'arguments': [{'type': 'string', 'value': json.dumps(arg)} for arg in args],
            'target': {'context': at or self._require().context, 'sandbox': SANDBOX},
            'awaitPromise': True,
        }
        for attempt in range(3):
            try:
                result = await self._command('script.callFunction', params)
            except _BiDiError as error:
                if attempt == 2 or not error.is_navigation_race:
                    raise ActionFailed(str(error)) from None
                await asyncio.sleep(0.1)
                continue
            if result['type'] == 'exception':
                raise ActionFailed(f'script failed: {result["exceptionDetails"]["text"]}')
            value = result['result']
            return cast(JSON, json.loads(value['value'])) if value.get('type') == 'string' else None
        raise AssertionError('unreachable')

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """A BiDi command, with its errors as `ActionFailed`."""
        try:
            return await self._command(method, params)
        except _BiDiError as error:
            raise ActionFailed(str(error)) from None

    async def _command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """A BiDi command. Raises `_BiDiError`."""
        session = self._session
        if session is None or session.bidi is None:
            raise LifecycleError('the browser is not open')
        try:
            return await session.bidi.call(method, params)
        except _Unreachable:
            raise ActionFailed('Camoufox stopped answering; it may have crashed') from None


# --- helpers ---


class _Unreachable(Exception):
    pass


class _BiDiError(Exception):
    def __init__(self, error: str, message: str) -> None:
        self.error = error
        self.message = message
        super().__init__(f'{error}: {message}' if message else error)

    @property
    def is_navigation_race(self) -> bool:
        """The document went away under a script call, so a retry on the new one may work."""
        return self.error in ('no such frame', 'unknown error') and any(
            word in self.message for word in ('destroyed', 'discarded', 'no longer', 'Actor')
        )


class _BiDi:
    """A WebDriver BiDi connection: commands matched to their replies by id, and events passed to `on_event`."""

    def __init__(self, connection: ClientConnection, *, on_event: Callable[[str, dict[str, Any]], None]) -> None:
        self._connection = connection
        self._on_event = on_event
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader = asyncio.create_task(self._read())

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        command_id = self._next_id
        reply = asyncio.get_running_loop().create_future()
        self._pending[command_id] = reply
        try:
            await self._connection.send(json.dumps({'id': command_id, 'method': method, 'params': params}))
            message = await reply
        except ConnectionClosed:
            raise _Unreachable from None
        finally:
            self._pending.pop(command_id, None)
        if message.get('type') == 'error':
            raise _BiDiError(str(message.get('error')), str(message.get('message') or ''))
        return message['result']

    async def _read(self) -> None:
        try:
            async for raw in self._connection:
                message = json.loads(raw)
                if message.get('type') == 'event':
                    self._on_event(message['method'], message['params'])
                elif (reply := self._pending.get(message.get('id'))) is not None and not reply.done():
                    reply.set_result(message)
        except ConnectionClosed:
            pass
        finally:
            for reply in self._pending.values():
                if not reply.done():
                    reply.set_exception(_Unreachable())

    async def close(self) -> None:
        self._reader.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(self._connection.close(), 1)


_font_cache_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}


def _font_cache_lock() -> asyncio.Lock:
    """One `fc-cache` at a time per event loop, for browsers opening together."""
    return _font_cache_locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())


def _write_prefs(path: Path, prefs: Mapping[str, Pref]) -> None:
    path.write_text(''.join(f'user_pref({json.dumps(name)}, {json.dumps(value)});\n' for name, value in prefs.items()))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


async def _wait(awaitable: Awaitable[T], timeout: float) -> bool:
    """Wait up to `timeout` seconds; True if it finished."""
    try:
        await asyncio.wait_for(awaitable, timeout)
    except TimeoutError:
        return False
    return True


def _cookie_to_bidi(cookie: Cookie) -> dict[str, Any]:
    # Firefox refuses SameSite=None without Secure; "default" is how it stores a cookie that set no SameSite.
    same_site = cookie.same_site.lower() if cookie.same_site != 'None' or cookie.secure else 'default'
    raw: dict[str, Any] = {
        'name': cookie.name,
        'value': {'type': 'string', 'value': cookie.value},
        'domain': cookie.domain,
        'path': cookie.path,
        'httpOnly': cookie.http_only,
        'secure': cookie.secure,
        'sameSite': same_site,
    }
    if cookie.expires >= 0:
        raw['expiry'] = int(cookie.expires)
    return raw


def _cookie_from_bidi(raw: dict[str, Any]) -> Cookie:
    value = raw['value']
    text = base64.b64decode(value['value']).decode(errors='replace') if value['type'] == 'base64' else value['value']
    return Cookie(
        name=raw['name'],
        value=text,
        domain=raw['domain'],
        path=raw.get('path') or '/',
        expires=float(raw.get('expiry', -1)),
        http_only=bool(raw.get('httpOnly', False)),
        secure=bool(raw.get('secure', False)),
        same_site=_SAME_SITE_IN.get(str(raw.get('sameSite')), 'Lax'),
    )


def _key(key: str) -> str:
    """A `KeyboardEvent.key` value as BiDi's key action value (WebDriver's code points)."""
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
