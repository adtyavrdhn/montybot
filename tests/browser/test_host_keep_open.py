"""`BrowserHost(keep_open=True)`: a user's browser stays open between their runs, until idle for long or needed."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from test_host import ALICE, SHOP, Setup, page_text, shop_pages, sign_in_and_add_eggs

from montybot.browser.fake import FakeBrowser
from montybot.browser.host import BrowserHost, Detour, _duration
from montybot.browser.service import Started, UnknownRun

pytestmark = pytest.mark.anyio

NEXT = {'run_id': 'run-2', 'user_id': 'alice'}
"""Alice's next run: her next message in the chat, or another task."""


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


def keeping(setup: Setup, **options: object) -> BrowserHost:
    return BrowserHost(
        new_backend=setup.new_backend,
        jar=setup.jar,
        lease=setup.lease,
        idle_timeout=setup.idle_timeout,
        reap_every=setup.idle_timeout / 5,
        keep_open=True,
        **options,  # pyright: ignore[reportArgumentType]
    )


async def test_the_next_run_carries_on_in_the_kept_browser() -> None:
    setup = Setup()
    host = keeping(setup)
    await host.start(**ALICE)
    await sign_in_and_add_eggs(host)
    setup.made[0].session_storage[SHOP] = {'step': 'payment'}  # not in the saved state: only an open browser keeps it
    assert await host.close(**ALICE) is True
    assert setup.made[0].is_open and setup.jar.saves == 1  # saved, but still open

    assert await host.start(**NEXT) == Started(url=f'{SHOP}/shop', reused=False)
    assert await page_text(host, NEXT) == 'Signed in. In cart: eggs'
    assert setup.made[0].session_storage[SHOP] == {'step': 'payment'}
    assert len(setup.made) == 1  # no new browser


async def test_the_kept_browser_can_still_be_watched() -> None:
    """The Mac app's browser panel stays open after the task: it shows the browser where Monty left it."""
    setup = Setup()
    host = keeping(setup)
    await host.start(**ALICE)
    await host.close(**ALICE)
    assert (await host.peek_screenshot(**ALICE)).png.startswith(b'\x89PNG')
    await host.start(**NEXT)  # carries on in it: now it is that run's to show
    with pytest.raises(UnknownRun):
        await host.peek_screenshot(**ALICE)
    with pytest.raises(UnknownRun):
        await host.peek_screenshot(run_id='run-1', user_id='bob')  # nobody else's


async def test_without_keep_open_there_is_nothing_to_watch_after_the_run() -> None:
    setup = Setup()
    host = setup.host()
    await host.start(**ALICE)
    await host.close(**ALICE)
    with pytest.raises(UnknownRun):
        await host.peek_screenshot(**ALICE)


async def test_a_kept_browser_is_closed_once_idle() -> None:
    setup = Setup(idle_timeout=0.05)
    async with keeping(setup) as host:
        await host.start(**ALICE)
        await sign_in_and_add_eggs(host)
        await host.close(**ALICE)
        await asyncio.sleep(0.2)
        assert not setup.made[0].is_open
        await host.start(**NEXT)
        assert await page_text(host, NEXT) == 'Signed in. In cart: eggs'  # a new browser, from the saved state
        assert len(setup.made) == 2


async def test_kept_browsers_make_room_first() -> None:
    setup = Setup()
    host = keeping(setup, max_open_browsers=1)
    await host.start(**ALICE)
    await sign_in_and_add_eggs(host)
    await host.close(**ALICE)
    await host.start(run_id='run-3', user_id='bob')  # not "all browsers are in use": nobody is using alice's
    assert not setup.made[0].is_open and setup.made[1].is_open


async def test_a_kept_browser_is_discarded_when_the_saved_state_changes() -> None:
    """A forgotten sign-in: the next run starts from the jar, not from the kept browser's cookies."""
    setup = Setup()
    host = keeping(setup)
    await host.start(**ALICE)
    await sign_in_and_add_eggs(host)
    await host.close(**ALICE)
    await host.discard_parked('alice')
    assert not setup.made[0].is_open and setup.jar.saves == 1  # closed, not saved again over the edited jar
    await host.discard_parked('alice')  # nothing kept: nothing to do
    await host.start(**NEXT)
    assert len(setup.made) == 2  # a new browser, from the saved state


async def test_a_kept_browser_that_stopped_is_replaced() -> None:
    setup = Setup()
    host = keeping(setup)
    await host.start(**ALICE)
    await sign_in_and_add_eggs(host)
    await host.close(**ALICE)
    await setup.made[0].close()  # crashed while parked
    await host.start(**NEXT)
    assert await page_text(host, NEXT) == 'Signed in. In cart: eggs'
    assert len(setup.made) == 2


async def test_a_browser_that_cannot_export_is_not_kept() -> None:
    """It would carry on with sign-ins the jar does not have; closing keeps the jar the one truth."""
    setup = Setup(not_supported={'export'})
    host = keeping(setup)
    await host.start(**ALICE)
    assert await host.close(**ALICE) is False
    assert not setup.made[0].is_open


async def test_a_kept_browser_only_carries_on_the_same_way_out() -> None:
    """A browser going out from the user's Mac is not reused by a run going out from the server, or the other way."""
    setup = Setup()
    tunnel = Path('/tmp/tunnels/alice/proxy.sock')

    async def route(run_id: str, user_id: str) -> Path | None:
        return tunnel if run_id == NEXT['run_id'] else None

    host = keeping(setup, detour=Detour(route=route, new_backend=lambda socket: setup.new_backend()))
    await host.start(**ALICE)
    await sign_in_and_add_eggs(host)  # a scheduled run: the server's way out
    await host.close(**ALICE)
    await host.start(**NEXT)
    assert await page_text(host, NEXT) == 'Signed in. In cart: eggs'  # from the saved state, through the Mac
    assert not setup.made[0].is_open and len(setup.made) == 2


class TabbedBrowser(FakeBrowser):
    """A `FakeBrowser` with tabs (`TabsBackend`): each tab is its own fake, enough to see which one closes."""

    def new_tab(self) -> TabbedBrowser:
        return TabbedBrowser(pages=shop_pages())


async def test_only_the_tab_closes_while_another_run_has_the_browser() -> None:
    made: list[FakeBrowser] = []

    def new_backend() -> TabbedBrowser:
        browser = TabbedBrowser(pages=shop_pages())
        made.append(browser)
        return browser

    setup = Setup()
    host = BrowserHost(new_backend=new_backend, jar=setup.jar, lease=setup.lease, share_browser=True, keep_open=True)
    await host.start(**ALICE)
    await host.start(**NEXT)  # a tab of alice's browser
    first, second = made[0], host._runs['run-2'].backend  # pyright: ignore[reportPrivateUsage]
    assert isinstance(second, TabbedBrowser) and second is not first

    await host.close(**ALICE)
    assert not first.is_open and second.is_open
    await host.close(**NEXT)  # the last run of hers: its tab is kept
    assert second.is_open
    await host.start(run_id='run-3', user_id='alice')
    assert host._runs['run-3'].backend is second  # pyright: ignore[reportPrivateUsage]


def test_a_day_reads_in_hours() -> None:
    assert _duration(24 * 60 * 60) == '24 hours'
