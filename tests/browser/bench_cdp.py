"""Compare `ChromiumCDPBackend` with the Playwright `ChromiumBackend`, call by call. Not a test; run it by hand:

    uv run python tests/browser/bench_cdp.py [--runs 10] [--headed] [--server]

For each backend, on the conformance fixture site, it times: `open(None)` (start Chrome), `open` with the conformance
state (cookies, storage on two origins, then the page), `Navigate`, `snapshot()`, a `Click` on a point, a `Type` into a
selector, `screenshot()`, `export()` and `close()`. It prints medians of `--runs`, and the resident memory of every
process started for the browser (from `bench_chromium.usage`) with the page loaded.

Both run the same Chrome for Testing build: Playwright's Chromium. `--headed` runs both with a window (on Linux without
a desktop, use `--server`, which also jails both with bwrap and Xvfb as on the server).
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Protocol

from bench_chromium import usage
from playwright.async_api import async_playwright

from montybot.browser.cdp import CDPOptions, ChromiumCDPBackend, playwright_chromium
from montybot.browser.chromium import ChromiumBackend, ChromiumOptions
from montybot.browser.conformance import BUTTON_CENTRE, sample_state, serve_site
from montybot.browser.contract import BrowserBackend, Click, Navigate, Selector, Type

STEPS = ('start', 'open with state', 'navigate', 'snapshot', 'click', 'type', 'screenshot', 'export', 'close')


class Measured(BrowserBackend, Protocol):
    @property
    def workdir(self) -> Path | None: ...


async def timed(times: dict[str, list[float]], step: str, call: Callable[[], Awaitable[object]]) -> None:
    started = time.perf_counter()
    await call()
    times.setdefault(step, []).append(time.perf_counter() - started)


async def one_run(new: Callable[[], Measured], times: dict[str, list[float]], memory: list[float]) -> None:
    site = serve_site()
    browser = new()
    await timed(times, 'start', lambda: browser.open(None))
    await browser.close()
    browser = new()
    await timed(times, 'open with state', lambda: browser.open(sample_state(site, site.home)))
    try:
        await timed(times, 'navigate', lambda: browser.act(Navigate(url=site.actions)))
        await timed(times, 'snapshot', browser.snapshot)
        await timed(times, 'click', lambda: browser.act(Click(target=BUTTON_CENTRE)))
        await timed(times, 'type', lambda: browser.act(Type(text='eggs', target=Selector(css='#input'))))
        await timed(times, 'screenshot', browser.screenshot)
        await timed(times, 'export', browser.export)
        await asyncio.sleep(2)
        assert browser.workdir is not None
        memory.append(usage(browser.workdir).rss_mib)
    finally:
        await timed(times, 'close', browser.close)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--runs', type=int, default=10)
    parser.add_argument('--headed', action='store_true', help='a window for each browser')
    parser.add_argument('--server', action='store_true', help='bwrap and Xvfb, as on the server (Linux)')
    args = parser.parse_args()

    if args.server:
        cdp = CDPOptions(bwrap=True, virtual_screen=True, allow_private_networks=True)
    else:
        cdp = CDPOptions(headless=not args.headed)

    @asynccontextmanager
    async def playwright_backend() -> AsyncIterator[Callable[[], Measured]]:
        async with async_playwright() as playwright:
            if args.server:
                options = ChromiumOptions.server(allow_private_networks=True)
            else:
                # The same Chrome as the CDP backend, not Playwright's headless shell.
                options = ChromiumOptions(headless=not args.headed, executable_path=str(playwright_chromium()))
            yield lambda: ChromiumBackend(playwright=playwright, options=options)

    @asynccontextmanager
    async def cdp_backend() -> AsyncIterator[Callable[[], Measured]]:
        yield lambda: ChromiumCDPBackend(cdp)

    backends: dict[str, Callable[[], AbstractAsyncContextManager[Callable[[], Measured]]]] = {
        'Playwright': playwright_backend,
        'CDP': cdp_backend,
    }
    results: dict[str, tuple[dict[str, list[float]], list[float]]] = {}
    for name, backend in backends.items():
        times: dict[str, list[float]] = {}
        memory: list[float] = []
        async with backend() as new:
            for _ in range(args.runs):
                await one_run(new, times, memory)
        results[name] = (times, memory)

    print(f'{args.runs} runs each, median seconds (min to max)')
    print('| Step | ' + ' | '.join(results) + ' |')
    print('|---|' + '---|' * len(results))
    for step in STEPS:
        cells = [
            f'{statistics.median(t[step]):.3f} ({min(t[step]):.3f} to {max(t[step]):.3f})' for t, _ in results.values()
        ]
        print(f'| {step} | ' + ' | '.join(cells) + ' |')
    print('| RSS MiB, page loaded | ' + ' | '.join(f'{statistics.median(m):.0f}' for _, m in results.values()) + ' |')


if __name__ == '__main__':
    asyncio.run(main())
