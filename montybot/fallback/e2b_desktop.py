"""The E2B Desktop fallback: a `BrowserBackend` that drives Google Chrome on an E2B Desktop sandbox (#22).

Built and tested on its own, and imported by nothing else under `montybot/` (a test checks). It is here in case a user
path turns out to need a whole desktop rather than a jailed headless browser.

How it drives Chrome:

- **Pointer and keyboard go through the `e2b-desktop` SDK** (xdotool on the VM's X display), so the page sees real
  input from a real window, the same input a human sends through the noVNC stream.
- **Everything else goes through CDP inside the VM.** Chrome runs with `--remote-debugging-port=0` on the VM's
  loopback. `cdp.py` is uploaded to the VM and run with its python3 for each step: load a URL, find an element's
  position, read the page's text, set and read cookies (HttpOnly included, through `Storage.getCookies`) and storage.
  The debugging port is never published through E2B's public hostnames.
- **Selectors become screen positions.** `cdp.py` scrolls the element into view and returns its centre and where the
  viewport sits on the screen; the click is an xdotool click there.
- **Scrolling is a CDP wheel event**, since xdotool only turns the wheel by whole notches.

Beyond `BrowserBackend`: `stream_url()` for a human (noVNC, with a password in the URL), `desktop_screenshot()` of
the whole screen, and `pause()` / `resume()`. `close()` kills the sandbox, so billing stops.

The E2B SDK is synchronous; every call runs in a worker thread. Install the SDK with the dev dependencies
(`e2b-desktop`), and set `E2B_API_KEY`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ParamSpec, Protocol, TypeVar

from montybot.browser.contract import (
    Action,
    ActionFailed,
    Click,
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
    Selector,
    Snapshot,
    TargetNotFound,
    Type,
    features_of,
)
from montybot.browser.state import BLANK_URL, BrowserState, Cookie, SameSite, origin_of
from montybot.fallback import cdp

if TYPE_CHECKING:
    from e2b_desktop import Sandbox  # pyright: ignore[reportMissingTypeStubs]

logger = logging.getLogger(__name__)

ENGINE = 'e2b-desktop'

CHROME = 'google-chrome-stable'
"""Chrome's command on E2B's `desktop` template (Ubuntu 22.04, from github.com/e2b-dev/desktop)."""

CHROME_ARGS = (
    '--no-first-run',
    '--no-default-browser-check',
    '--password-store=basic',
    '--disable-sync',
    '--disable-features=Translate,PasswordManager,Autofill',
    '--start-maximized',
)
"""Flags taken from the template's own Chrome launcher, plus a maximised window."""

# E2B's published prices, read from e2b.dev/pricing on 2026-10-06. Billed per second while running; a paused sandbox
# is not billed for compute. The Pro plan adds $150 a month.
USD_PER_VCPU_SECOND = 0.000014
USD_PER_GIB_SECOND = 0.0000045

DESKTOP_VCPUS = 8
DESKTOP_MEMORY_MIB = 8192
"""What E2B's `desktop` template is built with (template/build_prod.py in github.com/e2b-dev/desktop). A sandbox's
`get_info()` reports the real numbers."""


def cost_per_hour(*, vcpus: int, memory_mib: int) -> float:
    """US dollars for one hour of a running sandbox."""
    return 3600 * (vcpus * USD_PER_VCPU_SECOND + memory_mib / 1024 * USD_PER_GIB_SECOND)


# --- the desktop: the SDK calls this backend needs ---


class DesktopCommandFailed(Exception):
    """A command in the VM exited with an error."""


class Desktop(Protocol):
    """One desktop VM. `E2BDesktop` is the real one; tests use a local stand-in. Methods are synchronous, like the
    SDK, and the backend calls them from worker threads."""

    @property
    def sandbox_id(self) -> str: ...

    def run(self, command: str, *, timeout: float) -> str:
        """Run a shell command and return its stdout. Raises `DesktopCommandFailed` on a non-zero exit."""
        ...

    def launch(self, command: str) -> None:
        """Start a shell command in the background, on the desktop's display, and leave it running."""
        ...

    def write_file(self, path: str, data: str) -> None: ...

    def move_mouse(self, x: int, y: int) -> None:
        """Move the pointer to a screen position in device pixels."""
        ...

    def left_click(self) -> None: ...

    def mouse_press(self, button: MouseButton) -> None: ...

    def mouse_release(self, button: MouseButton) -> None: ...

    def write(self, text: str) -> None:
        """Type `text` at the keyboard focus."""
        ...

    def press(self, keys: list[str]) -> None:
        """Press keys together: the SDK's names (`enter`, `ctrl`, `page_down`) or X keysym names (`period`)."""
        ...

    def screenshot(self) -> bytes:
        """A PNG of the whole screen."""
        ...

    def stream_url(self, *, view_only: bool) -> str:
        """The noVNC page for a human, password included. Starts the stream on first use."""
        ...

    def pause(self) -> None: ...

    def resume(self) -> None: ...

    def kill(self) -> None: ...


@dataclass(kw_only=True)
class E2BDesktop:
    """`Desktop` on an `e2b_desktop.Sandbox`."""

    sandbox: Sandbox
    timeout: int
    """Seconds the sandbox may live, set again on resume. E2B kills it when this runs out."""
    type_delay_ms: int = 12
    _streaming: bool = field(default=False, init=False)

    @classmethod
    def start(cls, *, resolution: tuple[int, int] = (1280, 800), timeout: int = 900) -> E2BDesktop:
        """Create a sandbox from E2B's `desktop` template and wait for Xfce. Reads `E2B_API_KEY`."""
        from e2b_desktop import Sandbox  # pyright: ignore[reportMissingTypeStubs]

        return cls(sandbox=Sandbox.create(resolution=resolution, timeout=timeout), timeout=timeout)

    @property
    def sandbox_id(self) -> str:
        return self.sandbox.sandbox_id

    def run(self, command: str, *, timeout: float) -> str:
        from e2b import CommandExitException

        try:
            return self.sandbox.commands.run(command, timeout=timeout).stdout
        except CommandExitException as error:
            raise DesktopCommandFailed(f'exit {error.exit_code}: {error.stderr.strip()[-500:]}') from None

    def launch(self, command: str) -> None:
        self.sandbox.commands.run(command, background=True, timeout=0).disconnect()

    def write_file(self, path: str, data: str) -> None:
        self.sandbox.files.write(path, data)  # pyright: ignore[reportUnknownMemberType]

    def move_mouse(self, x: int, y: int) -> None:
        self.sandbox.move_mouse(x, y)

    def left_click(self) -> None:
        self.sandbox.left_click()

    def mouse_press(self, button: MouseButton) -> None:
        self.sandbox.mouse_press(button)

    def mouse_release(self, button: MouseButton) -> None:
        self.sandbox.mouse_release(button)

    def write(self, text: str) -> None:
        self.sandbox.write(text, delay_in_ms=self.type_delay_ms)

    def press(self, keys: list[str]) -> None:
        self.sandbox.press(keys)

    def screenshot(self) -> bytes:
        return bytes(self.sandbox.screenshot())

    def stream_url(self, *, view_only: bool) -> str:
        stream = self.sandbox.stream
        if not self._streaming:
            stream.start(require_auth=True)
            self._streaming = True
        return stream.get_url(view_only=view_only, auth_key=stream.get_auth_key())

    def pause(self) -> None:
        self.sandbox.pause()

    def resume(self) -> None:
        # The instance method keeps this object, and with it the SDK's stream state; `Sandbox.connect(id)` would not.
        self.sandbox.connect(timeout=self.timeout)

    def kill(self) -> None:
        self.sandbox.kill()


# --- turning contract types into desktop and CDP terms ---

_KEYS = {
    'Enter': 'enter',
    'Tab': 'tab',
    'Escape': 'escape',
    'Backspace': 'backspace',
    'Delete': 'delete',
    'Insert': 'insert',
    'Home': 'home',
    'End': 'end',
    'PageUp': 'page_up',
    'PageDown': 'page_down',
    'ArrowUp': 'up',
    'ArrowDown': 'down',
    'ArrowLeft': 'left',
    'ArrowRight': 'right',
    ' ': 'space',
    **{f'F{n}': f'f{n}' for n in range(1, 13)},
}
"""DOM `KeyboardEvent.key` values to the SDK's key names."""

_SYMBOLS = {
    '.': 'period',
    ',': 'comma',
    '/': 'slash',
    '\\': 'backslash',
    ';': 'semicolon',
    "'": 'apostrophe',
    '-': 'minus',
    '=': 'equal',
    '[': 'bracketleft',
    ']': 'bracketright',
    '`': 'grave',
}
"""Characters to X keysym names, for a key pressed with modifiers. Lower case, because the SDK lowercases names."""

_MODIFIERS = {'Alt': 'alt', 'Control': 'ctrl', 'Meta': 'super', 'Shift': 'shift'}


def desktop_keys(press: Press) -> list[str] | None:
    """The SDK key names for `press`, or None for a lone character that is best typed as text. Raises
    `ActionFailed` for a key the desktop has no name for."""
    modifiers = [_MODIFIERS[m] for m in ('Control', 'Alt', 'Shift', 'Meta') if m in press.modifiers]
    key = press.key
    if key in _KEYS:
        return [*modifiers, _KEYS[key]]
    if len(key) != 1:
        raise ActionFailed(f'the desktop has no key named {key!r}')
    if not modifiers:
        return None
    if key.isascii() and key.isalnum():
        if key.isupper() and 'shift' not in modifiers:
            modifiers.append('shift')
        return [*modifiers, key.lower()]
    if key in _SYMBOLS:
        return [*modifiers, _SYMBOLS[key]]
    raise ActionFailed(f'the desktop cannot press {key!r} with modifiers')


def screen_point(x: float, y: float, viewport: dict[str, Any]) -> tuple[int, int]:
    """A viewport position in CSS pixels as a screen position in device pixels, given `cdp.py`'s viewport info."""
    scale = float(viewport['scale'])
    return round((viewport['x'] + x) * scale), round((viewport['y'] + y) * scale)


def cdp_cookie(cookie: Cookie) -> dict[str, Any]:
    """A cookie as CDP's `Storage.setCookies` takes it. A domain without a leading dot stays host-only."""
    params: dict[str, Any] = {
        'name': cookie.name,
        'value': cookie.value,
        'domain': cookie.domain,
        'path': cookie.path,
        'secure': cookie.secure,
        'httpOnly': cookie.http_only,
        'sameSite': cookie.same_site,
    }
    if cookie.expires > 0:
        params['expires'] = cookie.expires
    return params


_SAME_SITE: dict[str, SameSite] = {'Strict': 'Strict', 'Lax': 'Lax', 'None': 'None'}


def from_cdp_cookie(params: dict[str, Any]) -> Cookie:
    """A cookie from CDP's `Storage.getCookies`. Chrome leaves out `sameSite` when the site did not set it, and
    treats that as Lax."""
    session = bool(params.get('session')) or float(params.get('expires', -1)) <= 0
    return Cookie(
        name=params['name'],
        value=params['value'],
        domain=params['domain'],
        path=params.get('path', '/'),
        expires=-1 if session else float(params['expires']),
        http_only=bool(params.get('httpOnly')),
        secure=bool(params.get('secure')),
        same_site=_SAME_SITE.get(params.get('sameSite') or 'Lax', 'Lax'),
    )


# --- the backend ---

_P = ParamSpec('_P')
_R = TypeVar('_R')

_HELPER_SOURCE = Path(cdp.__file__).read_text()
_INLINE_LIMIT = 16_000
"""Requests up to this size go on the command line; bigger ones, and any with credentials, go in a file."""


@dataclass(kw_only=True)
class E2BDesktopBrowser:
    """A `BrowserBackend` on an E2B Desktop: one sandbox and one Chrome window per `open()`.

    `start_desktop` makes the desktop; the default creates an E2B sandbox. While paused, everything but `resume()`
    and `close()` raises `LifecycleError`.
    """

    start_desktop: Callable[[], Desktop] = E2BDesktop.start
    chrome: str = CHROME
    chrome_args: tuple[str, ...] = CHROME_ARGS
    python: str = 'python3'
    """The VM's Python, which runs `cdp.py`."""
    work_dir: str = '/tmp/montybot'
    """Where `cdp.py`, Chrome's throwaway profile and request files live in the VM."""
    step_timeout: float = 60
    """Seconds one step in the VM may take."""

    _desktop: Desktop | None = field(default=None, init=False)
    _paused: bool = field(default=False, init=False)
    _origins: set[str] = field(default_factory=set[str], init=False)
    """Origins whose localStorage `export()` reads: those seeded, and those the tab was seen on."""

    # --- desktop extras ---

    @property
    def desktop(self) -> Desktop | None:
        """The running desktop, for measurements and tests. None when closed."""
        return self._desktop

    @property
    def paused(self) -> bool:
        return self._paused

    async def stream_url(self, *, view_only: bool = False) -> str:
        """The noVNC link a human opens to watch or drive the desktop. It carries the stream's password: treat it
        like a credential, and never log it or show it to the model."""
        return await self._in_thread(self._live().stream_url, view_only=view_only)

    async def desktop_screenshot(self) -> bytes:
        """A PNG of the whole screen, window frame and all, as a computer-use model would see it."""
        return await self._in_thread(self._live().screenshot)

    async def pause(self) -> None:
        """Pause the sandbox. E2B keeps its memory and processes, so Chrome and its tab come back on `resume()`."""
        desktop = self._live()
        await self._in_thread(desktop.pause)
        self._paused = True

    async def resume(self) -> None:
        if self._desktop is None or not self._paused:
            raise LifecycleError('the desktop is not paused')
        await self._in_thread(self._desktop.resume)
        self._paused = False

    # --- BrowserBackend ---

    async def open(self, state: BrowserState | None) -> None:
        if self._desktop is not None:
            raise LifecycleError('the browser is already open')
        try:
            self._desktop = await asyncio.to_thread(self.start_desktop)
        except Exception as error:
            raise ActionFailed(f'could not start the desktop: {type(error).__name__}') from error
        self._paused = False
        self._origins = set()
        try:
            await self._start_chrome()
        except BaseException:
            await self.close()
            raise
        if state is None:
            return
        storage: dict[str, dict[str, dict[str, str]]] = {}
        for origin, items in state.local_storage.items():
            storage.setdefault(origin, {})['localStorage'] = items
        for origin, items in state.session_storage.items():
            storage.setdefault(origin, {})['sessionStorage'] = items
        self._origins.update(state.local_storage)
        self._note(state.url)
        await self._cdp(
            'seed',
            secret=True,
            cookies=[cdp_cookie(c) for c in state.cookies],
            storage=storage,
            url=state.url,
        )

    async def export(self) -> BrowserState:
        self._live()
        page = await self._cdp('export', origins=sorted(self._origins))
        self._note(page['url'])
        current = origin_of(page['url'])
        session: dict[str, str] = page['session_storage'].get(current, {}) if current else {}
        return BrowserState(
            url=page['url'],
            cookies=[from_cdp_cookie(c) for c in page['cookies']],
            local_storage={o: items for o, items in page['local_storage'].items() if items},
            session_storage={current: session} if current and session else {},
        )

    async def release(self) -> BrowserState:
        state = await self.export()
        await self.close()
        return state

    async def snapshot(self) -> Snapshot:
        self._live()
        page = await self._cdp('snapshot')
        self._note(page['url'])
        return Snapshot(url=page['url'], title=page['title'], text=page['text'])

    async def act(self, action: Action) -> None:
        desktop = self._live()
        if 'ref' in features_of(action):
            raise NotSupported('ref', engine=ENGINE, detail='snapshots have no refs yet (#13)')
        match action:
            case Navigate(url=url):
                await self._cdp('navigate', url=url)
                self._note(url)
            case Click(target=target):
                x, y = await self._screen(target)
                await self._in_thread(desktop.move_mouse, x, y)
                await self._in_thread(desktop.left_click)
                await self._cdp('settle')
            case Type(text=text, target=target):
                if target is not None:
                    await self._locate(target, focus=True)
                if text:
                    await self._in_thread(desktop.write, text)
                elif target is not None:
                    await self._in_thread(desktop.press, ['backspace'])
            case Press():
                keys = desktop_keys(action)
                if keys is None:
                    await self._in_thread(desktop.write, action.key)
                else:
                    await self._in_thread(desktop.press, keys)
                await self._cdp('settle')
            case Scroll(delta_x=delta_x, delta_y=delta_y, at=at):
                await self._cdp(
                    'scroll',
                    x=None if at is None else at.x,
                    y=None if at is None else at.y,
                    delta_x=delta_x,
                    delta_y=delta_y,
                )
            case MouseDown(at=at, button=button):
                await self._in_thread(desktop.move_mouse, *await self._screen(at))
                await self._in_thread(desktop.mouse_press, button)
            case MouseMove(at=at):
                await self._in_thread(desktop.move_mouse, *await self._screen(at))
            case MouseUp(at=at, button=button):
                await self._in_thread(desktop.move_mouse, *await self._screen(at))
                await self._in_thread(desktop.mouse_release, button)
                await self._cdp('settle')

    async def screenshot(self) -> Screenshot:
        """The viewport, through CDP. `desktop_screenshot()` has the whole screen."""
        self._live()
        shot = await self._cdp('screenshot')
        return Screenshot(png=base64.b64decode(shot['png']), width=shot['width'], height=shot['height'])

    async def close(self) -> None:
        desktop, self._desktop = self._desktop, None
        self._paused = False
        if desktop is not None:
            try:
                await asyncio.to_thread(desktop.kill)
            except Exception:
                # Most likely already gone. If not, E2B kills it when its timeout runs out.
                logger.exception('could not kill desktop %s', desktop.sandbox_id)

    # --- internals ---

    @property
    def _profile_dir(self) -> str:
        return f'{self.work_dir}/chrome-profile'

    @property
    def _helper(self) -> str:
        return f'{self.work_dir}/cdp.py'

    async def _start_chrome(self) -> None:
        desktop = self._live()
        await self._in_thread(
            desktop.run,
            f'rm -rf {shlex.quote(self._profile_dir)} && mkdir -p {shlex.quote(self.work_dir)}',
            timeout=self.step_timeout,
        )
        await self._in_thread(desktop.write_file, self._helper, _HELPER_SOURCE)
        command = [
            self.chrome,
            f'--user-data-dir={self._profile_dir}',
            '--remote-debugging-port=0',
            *self.chrome_args,
            BLANK_URL,
        ]
        await self._in_thread(desktop.launch, shlex.join(command) + ' >/dev/null 2>&1')
        await self._cdp('ready')

    def _live(self) -> Desktop:
        if self._desktop is None:
            raise LifecycleError('the browser is not open')
        if self._paused:
            raise LifecycleError('the desktop is paused; call resume() first')
        return self._desktop

    def _note(self, url: str) -> None:
        if origin := origin_of(url):
            self._origins.add(origin)

    async def _in_thread(self, function: Callable[_P, _R], *args: _P.args, **kwargs: _P.kwargs) -> _R:
        """Run one SDK call in a worker thread. A failed command becomes `ActionFailed`."""
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        except DesktopCommandFailed as error:
            raise ActionFailed(f'a desktop command failed: {error}') from None

    async def _cdp(self, op: str, *, secret: bool = False, **args: Any) -> dict[str, Any]:
        """Run one `cdp.py` operation in the VM. `secret` requests (cookies, storage) go through a file that the
        helper deletes, never the command line."""
        desktop = self._live()
        request = json.dumps({'profile_dir': self._profile_dir, 'op': op, 'args': args})
        if secret or len(request) > _INLINE_LIMIT:
            path = f'{self.work_dir}/request-{secrets.token_hex(8)}.json'
            await self._in_thread(desktop.write_file, path, request)
            argument = path
        else:
            argument = '--inline ' + base64.b64encode(request.encode()).decode()
        output = await self._in_thread(
            desktop.run, f'{shlex.quote(self.python)} {shlex.quote(self._helper)} {argument}', timeout=self.step_timeout
        )
        lines = output.strip().splitlines()
        try:
            reply: dict[str, Any] = json.loads(lines[-1])
        except (IndexError, ValueError):
            raise ActionFailed(f'the CDP helper gave no answer to {op}') from None
        if 'ok' in reply:
            return reply['ok']
        if reply.get('error') == 'not_found':
            raise LookupError(reply.get('message', 'no element matched'))
        raise ActionFailed(f'{op} failed: {reply.get("message", "unknown error")}')

    async def _locate(self, target: Selector | Ref, *, focus: bool) -> tuple[int, int]:
        if isinstance(target, Ref):
            raise NotSupported('ref', engine=ENGINE)
        try:
            found = await self._cdp('locate', css=target.css, focus=focus)
        except LookupError as error:
            raise TargetNotFound(target, str(error)) from None
        return screen_point(found['point']['x'], found['point']['y'], found['viewport'])

    async def _screen(self, target: Selector | Ref | Point) -> tuple[int, int]:
        if isinstance(target, Point):
            return screen_point(target.x, target.y, await self._cdp('viewport'))
        return await self._locate(target, focus=False)
