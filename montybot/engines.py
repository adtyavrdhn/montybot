"""Browser backends the app can be started with, by name (`BROWSER_BACKEND=montybot.engines:chromium`).

`ChromiumBackend` (#11) needs a running Playwright, which needs the event loop, while the browser service takes a plain
factory. `LazyChromium` starts one Playwright per process on the first `open()` and makes a `ChromiumBackend` from it.

| Name | Chrome |
|---|---|
| `montybot.engines:chromium` | a window on this machine (Mac or Linux desktop), or the server setup on Linux without one |
| `montybot.engines:chromium_headless` | Playwright's headless shell, for CI and tests |
| `montybot.engines:chromium_server` | the Linux server: Xvfb per browser, inside bwrap |
| `montybot.engines:servo_server` | evaluation only: headless Servo in the same jail and egress proxy (`servo.md`) |
| `montybot.engines:chromium_cdp_server` | evaluation: the same Chrome and jail, over our own CDP pipe, no Playwright (`cdp.md`) |
| `montybot.engines:chromium_cdp_headless` | the same in Chrome's headless mode, unjailed, for tests |
| `montybot.engines:lightpanda_server` | evaluation only: Lightpanda in the same jail and egress proxy (`lightpanda.md`) |

Servo needs servoshell at `$MONTYBOT_SERVO_BINARY`, which the app image does not ship. Its sessions are not saved
between runs on a stock build (`NotSupported('export')`), so keep `chromium_server` for real users.

Lightpanda needs its binary at `$MONTYBOT_LIGHTPANDA_BINARY`, which the app image does not ship either. It draws no
pixels, so it has no screenshots, no live view and no hand-off: also not for real users.

The CDP backend uses the app image's Playwright Chromium, or `$MONTYBOT_CHROME_BINARY`.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path

from playwright.async_api import Playwright, async_playwright

from montybot.browser.cdp import CDPOptions, ChromiumCDPBackend
from montybot.browser.chromium import ChromiumBackend, ChromiumOptions
from montybot.browser.contract import Action, Download, Screenshot, Snapshot
from montybot.browser.lightpanda import LightpandaBackend, LightpandaOptions
from montybot.browser.live import FrameSource
from montybot.browser.servo import ServoBackend, ServoOptions
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


def _egress_socket() -> Path | None:
    socket = os.environ.get('BROWSER_EGRESS_SOCKET')
    return Path(socket) if socket else None


def chromium_server() -> LazyChromium:
    return LazyChromium(lambda: ChromiumOptions.server(egress_socket=_egress_socket()))


def chromium_cdp_server() -> ChromiumCDPBackend:
    """Always jailed, headed on its own Xvfb screen, like `chromium_server`."""
    return ChromiumCDPBackend(CDPOptions.server(egress_socket=_egress_socket()))


def chromium_cdp_headless() -> ChromiumCDPBackend:
    return ChromiumCDPBackend(CDPOptions(headless=True))


def servo_server() -> ServoBackend:
    """Always jailed: Servo's WebDriver listens on every interface, so it never runs on the server's own network."""
    return ServoBackend(ServoOptions(bwrap=True, egress_socket=_egress_socket()))


def lightpanda_server() -> LightpandaBackend:
    """Always jailed: the server's network is reached only through the egress proxy."""
    return LightpandaBackend(LightpandaOptions(bwrap=True, egress_socket=_egress_socket()))
