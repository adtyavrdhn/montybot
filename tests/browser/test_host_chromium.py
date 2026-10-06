"""The browser service against real headless Chromium, on the poc demo shop.

Uses `PocChromium`, a throwaway backend in `poc_chromium.py`, until #11's backend lands. The conformance suite runs
against it first, so a failure in the shop test is the service's, not the test backend's.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import signal
import threading
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.async_api import async_playwright
from poc_chromium import PocChromium

from montybot.browser.conformance import BrowserBackendConformance, Site
from montybot.browser.contract import BrowserBackend, Click, Navigate, Selector, Type
from montybot.browser.host import CRASHED, BrowserHost
from montybot.browser.jar import InMemoryJar, InMemoryJarLease
from montybot.browser.service import Restarted, Started
from montybot.browser.state import BLANK_URL

pytestmark = pytest.mark.anyio

DEMO_SITE = Path(__file__).parents[2] / 'poc' / 'montybot_poc' / 'demo_site.py'
ALICE = {'run_id': 'run-1', 'user_id': 'alice'}


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


class TestPocChromium(BrowserBackendConformance):
    not_supported = frozenset({'ref'})

    @asynccontextmanager
    async def backend(self, site: Site) -> AsyncGenerator[BrowserBackend]:
        async with async_playwright() as playwright:
            yield PocChromium(playwright)


@pytest.fixture
def shop() -> Iterator[str]:
    """The poc demo shop (#2's fixture sites replace it), served on a free port."""
    spec = importlib.util.spec_from_file_location('demo_site', DEMO_SITE)
    assert spec is not None and spec.loader is not None
    demo_site = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo_site)
    server = ThreadingHTTPServer(('127.0.0.1', 0), demo_site._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_address[1]}'
    server.shutdown()
    server.server_close()


async def page_text(host: BrowserHost) -> str:
    return (await host.snapshot(**ALICE)).snapshot.text


async def wait_until_closed(browser: PocChromium, timeout: float = 15) -> None:
    async with asyncio.timeout(timeout):
        while browser.is_open:
            await asyncio.sleep(0.05)


def is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_sign_in_pause_resume_crash_and_reap(shop: str) -> None:
    idle_timeout = 3.0
    made: list[PocChromium] = []
    pids: list[int] = []
    jar = InMemoryJar()
    async with async_playwright() as playwright:

        def new_backend() -> PocChromium:
            made.append(PocChromium(playwright))
            return made[-1]

        host = BrowserHost(
            new_backend=new_backend, jar=jar, lease=InMemoryJarLease(), idle_timeout=idle_timeout, reap_every=0.1
        )
        async with host:
            assert await host.start(**ALICE) == Started(url=BLANK_URL, reused=False)
            assert made[0].pid is not None
            pids.append(made[0].pid)
            await host.act(**ALICE, action=Navigate(url=f'{shop}/login'))
            await host.act(**ALICE, action=Type(text='alice', target=Selector(css='#username')))
            await host.act(**ALICE, action=Type(text='hunter2', target=Selector(css='#password')))
            await host.act(**ALICE, action=Click(target=Selector(css='button[type=submit]')))
            await host.act(**ALICE, action=Click(target=Selector(css='#add-eggs')))
            text = await page_text(host)
            assert 'Signed in as alice.' in text and 'In cart: eggs' in text

            # Pause: the agent's attempt saves and ends. A later attempt of the same run gets the same browser.
            await host.save_state(**ALICE)
            assert await host.start(**ALICE) == Started(url=f'{shop}/shop', reused=True)
            assert 'In cart: eggs' in await page_text(host)
            assert len(made) == 1
            saved = await jar.load(user_id='alice')
            assert saved is not None
            assert [(c.name, c.http_only) for c in saved.cookies] == [('sid', True)]
            assert saved.local_storage[shop] == {'cart': '["eggs"]'}

            # Crash: kill Chromium. The next call starts a new one from the jar and says so.
            os.kill(pids[0], signal.SIGKILL)
            result = await host.snapshot(**ALICE)
            assert result.restarted == Restarted(reason=CRASHED, url=f'{shop}/shop')
            assert 'Signed in as alice.' in result.snapshot.text and 'In cart: eggs' in result.snapshot.text
            assert len(made) == 2 and made[1].pid is not None
            pids.append(made[1].pid)

            # A long pause: the reaper saves and closes the browser, and the run's next call restarts it.
            await host.act(**ALICE, action=Click(target=Selector(css='#add-milk')))
            await wait_until_closed(made[1])
            resumed = await host.start(**ALICE)
            assert resumed == Started(
                url=f'{shop}/shop',
                reused=False,
                restarted=Restarted(reason='closed after 3 seconds idle', url=f'{shop}/shop'),
            )
            assert made[2].pid is not None
            pids.append(made[2].pid)
            text = await page_text(host)
            assert 'Signed in as alice.' in text and 'In cart: eggs, milk' in text

            assert await host.close(**ALICE) is True
            assert not made[2].is_open

    async with asyncio.timeout(10):  # every Chromium this test started is gone
        while any(is_running(pid) for pid in pids):
            await asyncio.sleep(0.1)
