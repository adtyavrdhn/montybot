"""`PollingFrameSource`: a live view for any `BrowserBackend`, from `screenshot()` and `act()`.

The browser service uses it for a backend that is not a `LiveViewBackend`, such as `FakeBrowser`. It is slower than
a backend's own source and shows one tab only.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from montybot.browser.contract import ActionFailed, BrowserBackend, BrowserError, NotSupported
from montybot.browser.live import Frame, LiveInput, Tab, Tabs, Viewport
from montybot.liveview.latest import Latest

TAB_ID = 'tab'


class PollingFrameSource:
    """Polls `backend.screenshot()` up to `max_fps` times a second and passes input to `backend.act()`.

    Use `await PollingFrameSource.start(backend)`. The URL and title in its one `Tab` come from `snapshot()`, read
    when it starts and after each input; the snapshot's text is never kept.
    """

    def __init__(self, backend: BrowserBackend, *, max_fps: float = 10) -> None:
        self._backend = backend
        self._interval = 1 / max_fps
        self._latest = Latest()
        self._lock = asyncio.Lock()
        self._poller: asyncio.Task[None] | None = None

    @classmethod
    async def start(cls, backend: BrowserBackend, *, max_fps: float = 10) -> PollingFrameSource:
        source = cls(backend, max_fps=max_fps)
        await source._refresh_tab()
        source._poller = asyncio.create_task(source._poll())
        return source

    def updates(self) -> AsyncIterator[Frame | Tabs]:
        return self._latest.updates()

    async def send(self, action: LiveInput) -> None:
        if self._latest.closed:
            raise ActionFailed('the live view is closed')
        async with self._lock:
            await self._backend.act(action)
        await self._refresh_tab()

    async def switch_tab(self, tab_id: str) -> None:
        if tab_id != TAB_ID:
            raise ActionFailed('no such tab')

    async def set_viewport(self, viewport: Viewport | None) -> None:
        raise NotSupported('viewport', engine='a polled live view', detail='the backend has no way to resize')

    async def close(self) -> None:
        if self._poller is not None:
            self._poller.cancel()
        self._latest.close()

    async def _poll(self) -> None:
        last = b''
        while not self._latest.closed:
            started = time.monotonic()
            try:
                async with self._lock:
                    shot = await self._backend.screenshot()
            except BrowserError:
                self._latest.close()
                return
            if shot.png != last:
                last = shot.png
                self._latest.put_frame(Frame(image=shot.png, mime='image/png', width=shot.width, height=shot.height))
            await asyncio.sleep(max(0.0, self._interval - (time.monotonic() - started)))

    async def _refresh_tab(self) -> None:
        try:
            async with self._lock:
                snapshot = await self._backend.snapshot()
        except NotSupported:
            return
        self._latest.put_tabs(Tabs(tabs=(Tab(tab_id=TAB_ID, url=snapshot.url, title=snapshot.title, active=True),)))
