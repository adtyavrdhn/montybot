"""`Latest`: the newest frame and tab list, kept for one reader, so a slow reader skips frames instead of falling
behind."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from sammy.browser.live import Frame, Tabs


class Latest:
    def __init__(self) -> None:
        self._frame: Frame | None = None
        self._tabs: Tabs | None = None
        self._last_tabs: Tabs | None = None
        self._changed = asyncio.Event()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def put_frame(self, frame: Frame) -> None:
        self._frame = frame
        self._changed.set()

    def put_tabs(self, tabs: Tabs) -> None:
        """Queue `tabs` unless it equals the last list put."""
        if tabs != self._last_tabs:
            self._tabs = self._last_tabs = tabs
            self._changed.set()

    def close(self) -> None:
        self._closed = True
        self._changed.set()

    async def updates(self) -> AsyncIterator[Frame | Tabs]:
        """The tab list when it changed, then the newest frame, until `close()`."""
        while True:
            await self._changed.wait()
            self._changed.clear()
            if self._closed:
                return
            if self._tabs is not None:
                tabs, self._tabs = self._tabs, None
                yield tabs
            if self._frame is not None:
                frame, self._frame = self._frame, None
                yield frame
