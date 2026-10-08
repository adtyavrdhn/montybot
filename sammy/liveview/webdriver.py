"""`WebDriverFrameSource`: the live view over W3C WebDriver, for Servo.

WebDriver has no screencast, so frames come from polling Take Screenshot (a PNG of the viewport), and only frames that
changed are passed on. Input goes in with Perform Actions, which keeps pointer and key state between calls, so a
button pressed in one call stays down until a later call releases it: press-and-hold works.

Tested with Servo 0.7.0, which needs two workarounds, both measured on macOS:

- **One event per Perform Actions call, then Get Current URL.** A call holding a key down and up, where the key down
  navigates (Enter on a link), never returns, and the session hangs with it. Sending the key up as its own call right
  away still hangs about one time in eight. So every key and pointer event is its own call, followed by Get Current
  URL, which waits for a navigation to get going (no hangs in 72 tries).
- **Tabs by polling.** WebDriver has no event for a new window, so the source checks Get Window Handles twice a
  second, and right after any input.

The Servo backend (#12) returns one of these from `live_view()`, on its own session.

Back, forward and reload are WebDriver's own commands, which wait for the page to load, so a tab never shows as
loading and Stop is refused. WebDriver cannot tell whether there is history either way, so tabs leave that out.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import httpx

from sammy.browser.contract import (
    ActionFailed,
    Click,
    MouseButton,
    MouseDown,
    MouseMove,
    MouseUp,
    Navigate,
    NotSupported,
    Point,
    Press,
    Ref,
    Scroll,
    Type,
)
from sammy.browser.live import Frame, LiveInput, PageCommand, Tab, Tabs, Viewport, neighbour
from sammy.liveview.keys import key_for
from sammy.liveview.latest import Latest

ENGINE = 'servo'
TABS_EVERY = 0.5
"""Seconds between checks for new, closed or navigated tabs."""
_BUTTONS: dict[MouseButton, int] = {'left': 0, 'middle': 1, 'right': 2}
_PAGE_INFO = 'return [location.href, document.title, innerWidth, innerHeight]'
_COMMANDS: dict[PageCommand, str] = {'back': '/back', 'forward': '/forward', 'reload': '/refresh'}
_log = logging.getLogger(__name__)


class WebDriverError(ActionFailed):
    """A WebDriver command failed. `error` is the W3C error code, such as `no such window`."""

    def __init__(self, error: str, message: str) -> None:
        self.error = error
        super().__init__(f'webdriver: {error}: {message}')


class WebDriverSession:
    """The WebDriver commands the live view needs, on one session that someone else started and owns."""

    def __init__(self, *, http: httpx.AsyncClient, session_id: str) -> None:
        self.http = http
        """A client whose base URL is the WebDriver server, such as `http://127.0.0.1:PORT`."""
        self.session_id = session_id

    async def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        response = await self.http.request(method, f'/session/{self.session_id}{path}', json=body)
        value = response.json()['value']
        if response.status_code >= 400:
            raise WebDriverError(str(value.get('error', '')), str(value.get('message', '')))
        return value

    async def script(self, script: str, *args: Any) -> Any:
        return await self.call('POST', '/execute/sync', {'script': script, 'args': list(args)})


class WebDriverFrameSource:
    """A `FrameSource` for every window of one WebDriver session, starting on the current one, the run's tab.

    Use `await WebDriverFrameSource.start(session)`.
    """

    def __init__(self, session: WebDriverSession, *, max_fps: float = 30) -> None:
        self._session = session
        self._interval = 1 / max_fps
        self._latest = Latest()
        self._home = ''
        self._active = ''
        self._pages: dict[str, tuple[str, str]] = {}
        """Window handle to its URL and title, as last seen."""
        self._size = (0, 0)
        self._tabs_due = 0.0
        self._lock = asyncio.Lock()
        self._poller: asyncio.Task[None] | None = None
        self._closed = False

    @classmethod
    async def start(cls, session: WebDriverSession, *, max_fps: float = 30) -> WebDriverFrameSource:
        source = cls(session, max_fps=max_fps)
        source._home = source._active = await session.call('GET', '/window')
        for handle in await session.call('GET', '/window/handles'):
            source._pages[handle] = ('', '')
        source._poller = asyncio.create_task(source._poll())
        return source

    # --- FrameSource ---

    def updates(self) -> AsyncIterator[Frame | Tabs]:
        return self._latest.updates()

    async def send(self, action: LiveInput) -> None:
        async with self._lock:
            if self._closed:
                raise ActionFailed('the live view is closed')
            await self._send(action)
        self._tabs_due = 0  # input may have opened or closed a window; look now

    async def switch_tab(self, tab_id: str) -> None:
        async with self._lock:
            if tab_id not in self._pages:
                raise ActionFailed('no such tab')
            await self._switch(tab_id)
        self._tabs_due = 0

    # --- ControlsSource ---

    async def command(self, command: PageCommand) -> None:
        path = _COMMANDS.get(command)
        if path is None:
            raise ActionFailed(f'{ENGINE} cannot stop a page loading')
        async with self._lock:
            await self._session.call('POST', path, {})
        self._tabs_due = 0

    async def new_tab(self) -> None:
        async with self._lock:
            created = await self._session.call('POST', '/window/new', {'type': 'tab'})
            handle = str(created['handle'])
            self._pages[handle] = ('about:blank', '')
            await self._switch(handle)
        self._tabs_due = 0

    async def close_tab(self, tab_id: str) -> None:
        async with self._lock:
            if tab_id not in self._pages:
                raise ActionFailed('no such tab')
            if tab_id == self._home:
                raise ActionFailed("the run's own tab stays open")
            back_to = neighbour(list(self._pages), tab_id) if tab_id == self._active else self._active
            await self._switch(tab_id)
            await self._session.call('DELETE', '/window')  # closes the current window
            del self._pages[tab_id]
            await self._switch(back_to or self._home)
        self._tabs_due = 0

    async def set_viewport(self, viewport: Viewport | None) -> None:
        # WebDriver can only resize the window, which gives no phone layout and outlives the hand-off.
        raise NotSupported('viewport', engine=ENGINE)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._poller is not None:
            self._poller.cancel()
            with suppress(asyncio.CancelledError):
                await self._poller
        async with self._lock:
            with suppress(ActionFailed, httpx.HTTPError):
                await self._session.call('DELETE', '/actions')
                if self._active != self._home:
                    await self._session.call('POST', '/window', {'handle': self._home})
        self._latest.close()

    # --- frames and tabs ---

    async def _poll(self) -> None:
        last = b''
        while not self._closed:
            started = time.monotonic()
            try:
                if started >= self._tabs_due:
                    self._tabs_due = started + TABS_EVERY
                    await self._refresh_tabs()
                png = base64.b64decode(await self._session.call('GET', '/screenshot'))
            except WebDriverError as error:
                # Most likely the active window closed between two checks; look again soon.
                _log.debug('live view poll failed: %s', error.error)
                self._tabs_due = 0
                await asyncio.sleep(self._interval)
                continue
            except httpx.HTTPError:
                self._latest.close()  # the browser is gone
                return
            if png != last:
                last = png
                self._latest.put_frame(Frame(image=png, mime='image/png', width=self._size[0], height=self._size[1]))
            await asyncio.sleep(max(0.0, self._interval - (time.monotonic() - started)))

    async def _refresh_tabs(self) -> None:
        async with self._lock:
            handles: list[str] = await self._session.call('GET', '/window/handles')
            new = [h for h in handles if h not in self._pages]
            self._pages = {h: self._pages.get(h, ('', '')) for h in handles}
            if new:
                await self._switch(new[-1])  # follow a new popup or tab, as a browser window would
            elif self._active not in self._pages and handles:
                await self._switch(self._home if self._home in self._pages else handles[0])
            url, title, width, height = await self._session.script(_PAGE_INFO)
            self._pages[self._active] = (str(url), str(title))
            self._size = (int(width), int(height))
            tabs = tuple(
                Tab(tab_id=handle, url=url, title=title, active=handle == self._active, closable=handle != self._home)
                for handle, (url, title) in self._pages.items()
            )
        self._latest.put_tabs(Tabs(tabs=tabs))

    async def _switch(self, handle: str) -> None:
        """Make `handle` the active window. Call with the lock held."""
        if handle == self._active:
            return
        with suppress(WebDriverError):
            await self._session.call('DELETE', '/actions')  # let go of anything held on the old window
        await self._session.call('POST', '/window', {'handle': handle})
        self._active = handle

    # --- input ---

    async def _send(self, action: LiveInput) -> None:
        match action:
            case MouseDown(at=at, button=button):
                await self._pointer_action(at, {'type': 'pointerDown', 'button': _BUTTONS[button]})
            case MouseMove(at=at):
                await self._pointer_action(at)
            case MouseUp(at=at, button=button):
                await self._pointer_action(at, {'type': 'pointerUp', 'button': _BUTTONS[button]})
            case Click(target=Point() as at):
                await self._pointer_action(at, {'type': 'pointerDown', 'button': 0})
                await self._pointer_action(at, {'type': 'pointerUp', 'button': 0})
            case Click(target=target):
                raise NotSupported('ref' if isinstance(target, Ref) else 'selector', engine=f'{ENGINE} live view')
            case Scroll(delta_x=delta_x, delta_y=delta_y, at=at):
                at = self._clamp(at or Point(x=self._size[0] / 2, y=self._size[1] / 2))
                await self._perform(
                    {
                        'type': 'wheel',
                        'id': 'wheel',
                        'actions': [
                            {
                                'type': 'scroll',
                                'origin': 'viewport',
                                'x': round(at.x),
                                'y': round(at.y),
                                'deltaX': round(delta_x),
                                'deltaY': round(delta_y),
                                'duration': 0,
                            }
                        ],
                    }
                )
            case Press(key=name, modifiers=modifiers):
                key = key_for(name)
                if key is None:
                    raise NotSupported('press', engine=ENGINE, detail='unknown key name')
                held = [k.webdriver for m in dict.fromkeys(modifiers) if (k := key_for(m)) is not None]
                for value in [*held, key.webdriver]:
                    await self._key_action('keyDown', value)
                for value in [key.webdriver, *reversed(held)]:
                    await self._key_action('keyUp', value)
            case Type(text=text, target=None):
                for char in text:
                    key = key_for(char)
                    if key is not None:
                        await self._key_action('keyDown', key.webdriver)
                        await self._key_action('keyUp', key.webdriver)
            case Type(target=Ref()):
                raise NotSupported('ref', engine=f'{ENGINE} live view')
            case Type():
                raise NotSupported('selector', engine=f'{ENGINE} live view')
            case Navigate(url=url):
                await self._session.call('POST', '/url', {'url': url})

    def _clamp(self, at: Point) -> Point:
        """WebDriver refuses to move the pointer outside the viewport."""
        width, height = self._size
        if not width or not height:
            return at
        return Point(x=min(max(at.x, 0), width - 1), y=min(max(at.y, 0), height - 1))

    async def _pointer_action(self, at: Point, then: dict[str, Any] | None = None) -> None:
        at = self._clamp(at)
        move = {'type': 'pointerMove', 'origin': 'viewport', 'x': round(at.x), 'y': round(at.y), 'duration': 0}
        await self._perform(
            {
                'type': 'pointer',
                'id': 'mouse',
                'parameters': {'pointerType': 'mouse'},
                'actions': [move] if then is None else [move, then],
            }
        )

    async def _key_action(self, kind: str, value: str) -> None:
        await self._perform({'type': 'key', 'id': 'keyboard', 'actions': [{'type': kind, 'value': value}]})

    async def _perform(self, source: dict[str, Any]) -> None:
        await self._session.call('POST', '/actions', {'actions': [source]})
        # Servo 0.7.0: an event sent while the previous one's navigation starts can hang the session for good (4 times
        # in 30 on macOS). Get Current URL in between waits that out (0 times in 72).
        await self._session.call('GET', '/url')
