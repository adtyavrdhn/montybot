"""`CdpFrameSource`: Chromium's live view over the Chrome DevTools Protocol.

Frames come from `Page.startScreencast`: Chromium sends a JPEG each time the page repaints, so a still page costs
nothing. Input goes in with `Input.dispatchMouseEvent` and `Input.dispatchKeyEvent`, which reach the page as trusted
events, the same as a real mouse and keyboard.

The CDP session comes from Playwright (`BrowserContext.new_cdp_session`), so it travels over the pipe Playwright
already holds, and no debugging port is opened. The Chromium backend (#11) returns one of these from `live_view()`.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import suppress
from typing import Any, cast

from playwright.async_api import CDPSession, Page
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame as PlaywrightFrame

from montybot.browser.contract import (
    ActionFailed,
    Click,
    MouseButton,
    MouseDown,
    MouseMove,
    MouseUp,
    NotSupported,
    Point,
    Press,
    Ref,
    Scroll,
    Type,
)
from montybot.browser.live import Frame, LiveInput, Tab, Tabs
from montybot.liveview.keys import MODIFIER_BITS, Key, key_for
from montybot.liveview.latest import Latest

ENGINE = 'chromium'
_log = logging.getLogger(__name__)
_BUTTON_BITS: dict[MouseButton, int] = {'left': 1, 'right': 2, 'middle': 4}
_TEXT_SUPPRESSING = MODIFIER_BITS['Alt'] | MODIFIER_BITS['Control'] | MODIFIER_BITS['Meta']


async def _call(cdp: CDPSession, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """`cdp.send`, typed."""
    send = cast(Callable[[str, dict[str, Any] | None], Awaitable[dict[str, Any]]], cdp.send)  # pyright: ignore[reportUnknownMemberType]
    return await send(method, params)


class CdpFrameSource:
    """A `FrameSource` for the tabs of one Playwright `BrowserContext`, starting on `page`, the run's tab.

    Use `await CdpFrameSource.start(page)`.
    """

    def __init__(self, page: Page, *, quality: int = 80) -> None:
        self._home = page
        self._context = page.context
        self._quality = quality
        self._latest = Latest()
        self._ids = (str(n) for n in itertools.count(1))
        self._tabs: dict[str, Page] = {}
        self._titles: dict[str, str] = {}
        self._listeners: list[tuple[Page, str, Callable[..., None]]] = []
        self._active = page
        self._cdp: CDPSession | None = None
        self._buttons: list[MouseButton] = []
        self._pointer = Point(x=0, y=0)
        viewport = page.viewport_size
        self._size = (viewport['width'], viewport['height']) if viewport else (0, 0)
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Future[Any]] = set()
        self._closed = False

    @classmethod
    async def start(cls, page: Page, *, quality: int = 80) -> CdpFrameSource:
        source = cls(page, quality=quality)
        for existing in page.context.pages:
            source._track(existing)
        page.context.on('page', source._on_page)
        await source._activate(page)
        return source

    # --- FrameSource ---

    def updates(self) -> AsyncIterator[Frame | Tabs]:
        return self._latest.updates()

    async def send(self, action: LiveInput) -> None:
        async with self._lock:
            cdp = self._cdp
            if cdp is None:
                raise ActionFailed('the live view is closed')
            try:
                await self._send(cdp, action)
            except PlaywrightError as error:
                raise ActionFailed(f'{ENGINE}: {error.message}') from error

    async def switch_tab(self, tab_id: str) -> None:
        page = self._tabs.get(tab_id)
        if page is None:
            raise ActionFailed('no such tab')
        await self._activate(page)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._context.remove_listener('page', self._on_page)
        for page, event, listener in self._listeners:
            page.remove_listener(event, listener)
        async with self._lock:
            if self._cdp is not None:
                with suppress(PlaywrightError):
                    await self._release_buttons(self._cdp)
                    await _call(self._cdp, 'Page.stopScreencast')
                    await self._cdp.detach()
                self._cdp = None
            if self._active is not self._home and not self._home.is_closed():
                with suppress(PlaywrightError):
                    await self._home.bring_to_front()
        for task in self._tasks:
            task.cancel()
        self._latest.close()

    # --- tabs ---

    def _track(self, page: Page) -> None:
        tab_id = next(self._ids)
        self._tabs[tab_id] = page

        def on_close(_: Page) -> None:
            self._tabs.pop(tab_id, None)
            if page is self._active and not self._closed:
                fallback = self._home if not self._home.is_closed() else next(iter(self._tabs.values()), None)
                if fallback is None:
                    self._latest.close()
                else:
                    self._spawn(self._activate(fallback))
            else:
                self._spawn(self._refresh_tabs())

        def on_navigated(frame: PlaywrightFrame) -> None:
            if frame.parent_frame is None:
                self._spawn(self._refresh_tabs())

        def on_load(_: Page) -> None:
            self._spawn(self._refresh_tabs())

        for event, listener in (('close', on_close), ('framenavigated', on_navigated), ('load', on_load)):
            page.on(event, listener)  # pyright: ignore[reportCallIssue, reportArgumentType]
            self._listeners.append((page, event, listener))

    def _on_page(self, page: Page) -> None:
        """A popup or new tab opened: follow it, as a browser window would."""
        self._track(page)
        self._spawn(self._activate(page))

    async def _activate(self, page: Page) -> None:
        async with self._lock:
            if self._closed or page.is_closed():
                return
            old, self._cdp = self._cdp, None
            if old is not None:
                with suppress(PlaywrightError):
                    await self._release_buttons(old)
                    await _call(old, 'Page.stopScreencast')
                    await old.detach()
            self._active = page
            viewport = page.viewport_size
            if viewport:
                self._size = (viewport['width'], viewport['height'])
            await page.bring_to_front()
            cdp = await self._context.new_cdp_session(page)

            def on_frame(params: dict[str, Any]) -> None:
                self._on_frame(cdp, params)

            cdp.on('Page.screencastFrame', on_frame)
            self._cdp = cdp
            await _call(cdp, 'Page.startScreencast', {'format': 'jpeg', 'quality': self._quality})
        await self._refresh_tabs()

    async def _refresh_tabs(self) -> None:
        for tab_id, page in list(self._tabs.items()):
            with suppress(PlaywrightError):  # closing or mid-navigation; keep the title we had
                self._titles[tab_id] = await page.title()
        # No awaits from here on, so the tabs and the active one agree.
        tabs = [
            Tab(tab_id=tab_id, url=page.url, title=self._titles.get(tab_id, ''), active=page is self._active)
            for tab_id, page in self._tabs.items()
            if not page.is_closed()
        ]
        if not self._closed:
            self._latest.put_tabs(Tabs(tabs=tuple(tabs)))

    # --- frames ---

    def _on_frame(self, cdp: CDPSession, params: dict[str, Any]) -> None:
        self._spawn(_call(cdp, 'Page.screencastFrameAck', {'sessionId': params['sessionId']}))
        if cdp is not self._cdp or self._closed:
            return
        metadata = params['metadata']
        self._size = (round(metadata['deviceWidth']), round(metadata['deviceHeight']))
        image = base64.b64decode(params['data'])
        self._latest.put_frame(Frame(image=image, mime='image/jpeg', width=self._size[0], height=self._size[1]))

    # --- input ---

    async def _send(self, cdp: CDPSession, action: LiveInput) -> None:
        match action:
            case MouseDown(at=at, button=button):
                if button not in self._buttons:
                    self._buttons.append(button)
                await self._mouse(cdp, 'mousePressed', at, button)
            case MouseMove(at=at):
                await self._mouse(cdp, 'mouseMoved', at, self._buttons[0] if self._buttons else 'none')
            case MouseUp(at=at, button=button):
                if button in self._buttons:
                    self._buttons.remove(button)
                await self._mouse(cdp, 'mouseReleased', at, button)
            case Click(target=Point() as at):
                self._buttons.append('left')
                await self._mouse(cdp, 'mousePressed', at, 'left')
                self._buttons.remove('left')
                await self._mouse(cdp, 'mouseReleased', at, 'left')
            case Click(target=target):
                raise NotSupported('ref' if isinstance(target, Ref) else 'selector', engine=f'{ENGINE} live view')
            case Scroll(delta_x=delta_x, delta_y=delta_y, at=at):
                at = at or Point(x=self._size[0] / 2, y=self._size[1] / 2)
                await _call(
                    cdp,
                    'Input.dispatchMouseEvent',
                    {'type': 'mouseWheel', 'x': at.x, 'y': at.y, 'deltaX': delta_x, 'deltaY': delta_y},
                )
            case Press(key=name, modifiers=modifiers):
                key = key_for(name)
                if key is None:
                    raise NotSupported('press', engine=ENGINE, detail='unknown key name')
                await self._key(cdp, key, sum(MODIFIER_BITS[m] for m in set(modifiers)))
            case Type(text=text, target=None):
                for char in text:
                    key = key_for(char)
                    if key is not None:
                        await self._key(cdp, key, 0)
            case Type(target=Ref()):
                raise NotSupported('ref', engine=f'{ENGINE} live view')
            case Type():
                raise NotSupported('selector', engine=f'{ENGINE} live view')

    async def _mouse(self, cdp: CDPSession, kind: str, at: Point, button: MouseButton | str) -> None:
        self._pointer = at
        buttons = sum(_BUTTON_BITS[b] for b in self._buttons)
        click_count = 0 if kind == 'mouseMoved' else 1
        await _call(
            cdp,
            'Input.dispatchMouseEvent',
            {'type': kind, 'x': at.x, 'y': at.y, 'button': button, 'buttons': buttons, 'clickCount': click_count},
        )

    async def _key(self, cdp: CDPSession, key: Key, modifiers: int) -> None:
        text = '' if modifiers & _TEXT_SUPPRESSING else key.text
        common = {'key': key.key, 'code': key.code, 'windowsVirtualKeyCode': key.key_code, 'modifiers': modifiers}
        await _call(
            cdp,
            'Input.dispatchKeyEvent',
            {'type': 'keyDown' if text else 'rawKeyDown', 'text': text, 'unmodifiedText': text, **common},
        )
        await _call(cdp, 'Input.dispatchKeyEvent', {'type': 'keyUp', **common})

    async def _release_buttons(self, cdp: CDPSession) -> None:
        """Let go of any button the user still holds, so the page does not see a stuck mouse."""
        while self._buttons:
            button = self._buttons.pop()
            await self._mouse(cdp, 'mouseReleased', self._pointer, button)

    # --- tasks ---

    def _spawn(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Future[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        # A PlaywrightError means a page closed or navigated while we talked to it; the next event catches up.
        if error is not None and not isinstance(error, PlaywrightError):
            _log.error('live view task failed', exc_info=error)
