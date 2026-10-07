"""Browser backends the app can be started with, by name (`BROWSER_BACKEND=montybot.engines:chromium`).

`ChromiumBackend` (#11) needs a running Playwright, which needs the event loop, while the browser service takes a plain
factory. `LazyChromium` starts one Playwright per process on the first `open()` and makes a `ChromiumBackend` from it.

| Name | Chrome |
|---|---|
| `montybot.engines:chromium` | a window on this machine (Mac or Linux desktop), or the server setup on Linux without one |
| `montybot.engines:chromium_headless` | Playwright's headless shell, for CI and tests |
| `montybot.engines:chromium_server` | the Linux server: Xvfb per browser, inside bwrap |
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path

from playwright.async_api import Playwright, async_playwright

from montybot.browser.chromium import ChromiumBackend, ChromiumOptions
from montybot.browser.contract import Action, Download, Screenshot, Snapshot
from montybot.browser.live import FrameSource
from montybot.browser.state import BrowserState

_playwright: Playwright | None = None
_lock: asyncio.Lock | None = None


async def shared_playwright() -> Playwright:
    """This process's Playwright, started on first use. It lives as long as the process, on the app's one event loop;
    the browser service runs there, so every backend does too."""
    global _playwright, _lock
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if _playwright is None:
            _playwright = await async_playwright().start()
    return _playwright


class LazyChromium:
    """A `BrowserBackend` that makes its `ChromiumBackend` on the first `open()`."""

    def __init__(self, options: Callable[[], ChromiumOptions]) -> None:
        self._options = options
        self._inner: ChromiumBackend | None = None

    async def _backend(self) -> ChromiumBackend:
        if self._inner is None:
            playwright = await shared_playwright()
            if self._inner is None:  # a concurrent first call may have made it while this one waited
                self._inner = ChromiumBackend(playwright=playwright, options=self._options())
        return self._inner

    async def open(self, state: BrowserState | None) -> None:
        await (await self._backend()).open(state)

    async def export(self) -> BrowserState:
        return await (await self._backend()).export()

    async def release(self) -> BrowserState:
        return await (await self._backend()).release()

    async def snapshot(self) -> Snapshot:
        return await (await self._backend()).snapshot()

    async def act(self, action: Action) -> None:
        await (await self._backend()).act(action)

    async def screenshot(self) -> Screenshot:
        return await (await self._backend()).screenshot()

    async def live_view(self) -> FrameSource:
        return await (await self._backend()).live_view()

    async def take_downloads(self) -> list[Download]:
        return await (await self._backend()).take_downloads()

    async def close(self) -> None:
        if self._inner is not None:
            await self._inner.close()


def chromium() -> LazyChromium:
    return LazyChromium(ChromiumOptions.for_this_machine)


def chromium_headless() -> LazyChromium:
    return LazyChromium(lambda: ChromiumOptions(headless=True))


def chromium_server() -> LazyChromium:
    def options() -> ChromiumOptions:
        socket = os.environ.get('BROWSER_EGRESS_SOCKET')
        return ChromiumOptions.server(egress_socket=Path(socket) if socket else None)

    return LazyChromium(options)
