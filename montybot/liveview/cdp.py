"""`CDPFrameSource`: the live view of `ChromiumCDPBackend` (`montybot.browser.cdp`), over our own CDP pipe.

The same design as `CdpFrameSource` for Playwright (`chromium.py` here): frames from `Page.startScreencast`, input with
`Input.dispatchMouseEvent` and `dispatchKeyEvent`, a phone size with `Emulation.setDeviceMetricsOverride`. Each
activated tab gets its own CDP session, detached when the user moves on, so the run's own session is never touched.

Tabs come from target discovery (`Target.targetCreated`, `targetInfoChanged`, `targetDestroyed`), which also carries
each tab's URL and title. Only the run's tab and tabs a page opened (they have an `openerId`) are shown: the backend's
own hidden tabs, for reading storage, never are.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import logging
from collections.abc import AsyncIterator, Coroutine
from contextlib import suppress
from typing import Any, cast

from montybot.browser.cdp import ENGINE, CDPInput, evaluate
from montybot.browser.cdp_client import CDPConnection, CDPError, CDPParams
from montybot.browser.contract import (
    ActionFailed,
    Click,
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
from montybot.browser.live import OUTLINE_JS, Frame, LiveInput, Outline, Tab, Tabs, Viewport
from montybot.liveview.latest import Latest

_log = logging.getLogger(__name__)


class CDPFrameSource:
    """A `FrameSource` for `home`, the run's tab, and the tabs and popups opened from it, starting on `home`. Other
    runs' tabs of the same Chrome are never shown. Use `await CDPFrameSource.start(connection, home=target_id)`."""

    def __init__(self, connection: CDPConnection, *, home: str, quality: int = 80) -> None:
        self._connection = connection
        self._home = home
        self._quality = quality
        self._latest = Latest()
        self._ids = (str(n) for n in itertools.count(1))
        self._tabs: dict[str, CDPParams] = {}
        """Target id to its latest `TargetInfo`, in the order they opened."""
        self._tab_ids: dict[str, str] = {}
        """Target id to the tab id the user sees."""
        self._active = home
        self._session: str | None = None
        self._input: CDPInput | None = None
        self._size = (0, 0)
        self._viewport: Viewport | None = None
        self._emulating = False
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Future[Any]] = set()
        self._removers = [
            connection.on('Target.targetCreated', self._on_target),
            connection.on('Target.targetInfoChanged', self._on_target),
            connection.on('Target.targetDestroyed', self._on_destroyed),
        ]
        self._closed = False

    @classmethod
    async def start(cls, connection: CDPConnection, *, home: str, quality: int = 80) -> CDPFrameSource:
        source = cls(connection, home=home, quality=quality)
        targets = cast(list[CDPParams], (await connection.send('Target.getTargets'))['targetInfos'])
        while True:  # a popup is ours only once its opener is, whatever order Chrome lists them in
            known = len(source._tabs)
            for info in targets:
                source._track(info, follow=False)
            if len(source._tabs) == known:
                break
        await source._activate(home)
        return source

    # --- FrameSource, and OutlineSource ---

    async def outline(self) -> Outline:
        """What is on the active tab for a screen reader. A page mid-navigation reads as empty, not as an error."""
        session = self._session
        if session is None:
            return Outline()
        try:
            return Outline.from_walker(await evaluate(self._connection, session, self._active, OUTLINE_JS))
        except (CDPError, ActionFailed):
            return Outline()

    def updates(self) -> AsyncIterator[Frame | Tabs]:
        return self._latest.updates()

    async def send(self, action: LiveInput) -> None:
        async with self._lock:
            mouse = self._input
            if mouse is None:
                raise ActionFailed('the live view is closed')
            try:
                await self._send(mouse, action)
            except CDPError as error:
                raise ActionFailed(f'{ENGINE}: {error.message}') from error

    async def switch_tab(self, tab_id: str) -> None:
        target = next((t for t, shown in self._tab_ids.items() if shown == tab_id and t in self._tabs), None)
        if target is None:
            raise ActionFailed('no such tab')
        await self._activate(target)

    async def set_viewport(self, viewport: Viewport | None) -> None:
        async with self._lock:
            if self._closed or viewport == self._viewport:
                return
            if (session := self._session) is not None:
                try:
                    await self._emulate(session, viewport)
                    # The screencast sends a frame only when something repaints, and a new size does not always
                    # repaint. Starting it again sends the page at its new size at once.
                    await self._connection.send('Page.stopScreencast', session=session)
                    start = {'format': 'jpeg', 'quality': self._quality}
                    await self._connection.send('Page.startScreencast', start, session=session)
                except CDPError as error:
                    raise ActionFailed(f'{ENGINE}: {error.message}') from error
            self._viewport = viewport

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for remove in self._removers:
            remove()
        async with self._lock:
            if self._session is not None:
                await self._let_go()
            if self._active != self._home and self._home in self._tabs:
                with suppress(CDPError):
                    await self._connection.send('Target.activateTarget', {'targetId': self._home})
        for task in self._tasks:
            task.cancel()
        self._latest.close()

    # --- tabs ---

    def _track(self, info: CDPParams, *, follow: bool) -> None:
        """Note a target's info. A page's new tab or popup is shown, and followed if `follow`."""
        target = str(info['targetId'])
        if info.get('type') != 'page' or (target != self._home and info.get('openerId') not in self._tabs):
            return  # not ours: another run's tab, or a hidden tab of the backend's
        new = target not in self._tabs
        self._tabs[target] = info
        if new:
            self._tab_ids[target] = next(self._ids)
        if new and follow and not self._closed:
            self._spawn(self._activate(target))
        else:
            self._put_tabs()

    def _on_target(self, params: CDPParams) -> None:
        self._track(cast(CDPParams, params['targetInfo']), follow=True)

    def _on_destroyed(self, params: CDPParams) -> None:
        target = str(params['targetId'])
        if self._tabs.pop(target, None) is None:
            return
        if target == self._active and not self._closed:
            fallback = self._home if self._home in self._tabs else next(iter(self._tabs), None)
            if fallback is None:
                self._latest.close()
            else:
                self._spawn(self._activate(fallback))
        else:
            self._put_tabs()

    def _put_tabs(self) -> None:
        if self._closed:
            return
        tabs = [
            Tab(
                tab_id=self._tab_ids[target],
                url=str(info.get('url', '')),
                title=str(info.get('title', '')),
                active=target == self._active,
            )
            for target, info in self._tabs.items()
        ]
        self._latest.put_tabs(Tabs(tabs=tuple(tabs)))

    async def _activate(self, target: str) -> None:
        async with self._lock:
            if self._closed or target not in self._tabs:
                return
            if self._session is not None:
                await self._let_go()
            self._active = target
            connection = self._connection
            await connection.send('Target.activateTarget', {'targetId': target})
            attached = await connection.send('Target.attachToTarget', {'targetId': target, 'flatten': True})
            session = str(attached['sessionId'])
            connection.on('Page.screencastFrame', lambda params: self._on_frame(session, params), session=session)
            self._session = session
            self._input = CDPInput(connection, session)
            if self._viewport is not None:
                await self._emulate(session, self._viewport)
            metrics = await connection.send('Page.getLayoutMetrics', session=session)
            viewport = cast(CDPParams, metrics['cssLayoutViewport'])
            self._size = (int(viewport['clientWidth']), int(viewport['clientHeight']))
            await connection.send('Page.startScreencast', {'format': 'jpeg', 'quality': self._quality}, session=session)
        self._put_tabs()

    async def _let_go(self) -> None:
        """Leave the tab as the user found it: buttons up (where the user left them), no screencast, its own size, and
        our session detached. Each step on its own, so a failure cannot skip the next."""
        session, mouse = self._session, self._input
        self._session = self._input = None
        if session is None:
            return
        if mouse is not None:
            with suppress(CDPError):
                await mouse.release_buttons()
        with suppress(CDPError):
            await self._connection.send('Page.stopScreencast', session=session)
        with suppress(CDPError):
            await self._emulate(session, None)
        with suppress(CDPError):
            await self._connection.send('Target.detachFromTarget', {'sessionId': session})
        self._connection.drop_session(session)

    async def _emulate(self, session: str, viewport: Viewport | None) -> None:
        """Give the tab a phone's size and layout, or (None) its own size back."""
        if viewport is None:
            if self._emulating:
                await self._connection.send('Emulation.clearDeviceMetricsOverride', session=session)
                self._emulating = False
            return
        metrics = {
            'width': viewport.width,
            'height': viewport.height,
            # 1: the screencast sends frames in CSS pixels, so more would cost rendering and show nothing sharper.
            'deviceScaleFactor': 1,
            'mobile': True,  # the page's meta viewport applies, so sites show their phone layout
        }
        await self._connection.send('Emulation.setDeviceMetricsOverride', metrics, session=session)
        self._emulating = True

    # --- frames ---

    def _on_frame(self, session: str, params: CDPParams) -> None:
        ack = self._connection.send('Page.screencastFrameAck', {'sessionId': params['sessionId']}, session=session)
        self._spawn(ack)
        if session != self._session or self._closed:
            return
        metadata = cast(CDPParams, params['metadata'])
        self._size = (round(metadata['deviceWidth']), round(metadata['deviceHeight']))
        image = base64.b64decode(params['data'])
        self._latest.put_frame(Frame(image=image, mime='image/jpeg', width=self._size[0], height=self._size[1]))

    # --- input ---

    async def _send(self, mouse: CDPInput, action: LiveInput) -> None:
        match action:
            case MouseDown(at=at, button=button):
                await mouse.mouse('mousePressed', at, button)
            case MouseMove(at=at):
                await mouse.mouse('mouseMoved', at)
            case MouseUp(at=at, button=button):
                await mouse.mouse('mouseReleased', at, button)
            case Click(target=Point() as at):
                await mouse.click(at)
            case Click(target=target):
                raise NotSupported('ref' if isinstance(target, Ref) else 'selector', engine=f'{ENGINE} live view')
            case Scroll(delta_x=delta_x, delta_y=delta_y, at=at):
                await mouse.wheel(at or Point(x=self._size[0] / 2, y=self._size[1] / 2), delta_x, delta_y)
            case Press(key=name, modifiers=modifiers):
                try:
                    await mouse.press(name, modifiers)
                except ActionFailed:
                    raise NotSupported('press', engine=ENGINE, detail='unknown key name') from None
            case Type(text=text, target=None):
                await mouse.type(text)
            case Type(target=Ref()):
                raise NotSupported('ref', engine=f'{ENGINE} live view')
            case Type():
                raise NotSupported('selector', engine=f'{ENGINE} live view')

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
        # A CDPError means a tab closed or navigated while we talked to it; the next event catches up.
        if error is not None and not isinstance(error, CDPError):
            _log.error('live view task failed', exc_info=error)
