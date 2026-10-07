"""The browser service (`BrowserHost`) against `FakeBrowser`, with the in-memory jar and lease."""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from dataclasses import dataclass, field

import pytest

from montybot.browser.contract import Action, ActionFailed, Click, Feature, Navigate, NotSupported, Selector
from montybot.browser.contract import TargetNotFound as TargetNotFoundError
from montybot.browser.fake import FakeBrowser, FakeElement, FakePage
from montybot.browser.host import CRASHED, SERVICE_RESTARTED, BrowserHost, _duration
from montybot.browser.jar import InMemoryJar, InMemoryJarLease
from montybot.browser.service import (
    ActionResult,
    HandoffActive,
    HandoffNotActive,
    Restarted,
    Started,
    UnknownRun,
    UserBusy,
)
from montybot.browser.state import BLANK_URL, BrowserState, Cookie

pytestmark = pytest.mark.anyio

SHOP = 'http://shop.test'
ALICE = {'run_id': 'run-1', 'user_id': 'alice'}


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


def shop_pages() -> dict[str, FakePage]:
    """A shop with a sign-in that sets an HttpOnly cookie, and a cart in localStorage."""

    def sign_in(browser: FakeBrowser, action: Action) -> None:
        if action == Click(target=Selector(css='#submit')):
            browser.cookies.append(Cookie(name='sid', value='s3cret', domain='shop.test', http_only=True))
            browser.load('/shop')

    def show_cart(browser: FakeBrowser) -> None:
        signed_in = 'Signed in' if browser.cookies_for(browser.url) else 'Signed out'
        browser.page.text = f'{signed_in}. In cart: {browser.local_storage.get(SHOP, {}).get("cart", "empty")}'

    def add(browser: FakeBrowser, action: Action) -> None:
        if action == Click(target=Selector(css='#add-eggs')):
            items = browser.local_storage.setdefault(SHOP, {})
            items['cart'] = ' '.join(filter(None, [items.get('cart'), 'eggs']))
            show_cart(browser)

    return {
        f'{SHOP}/login': FakePage(
            title='Sign in',
            elements=[FakeElement(selector='#submit', role='button', name='Sign in')],
            on_action=sign_in,
        ),
        f'{SHOP}/shop': FakePage(
            title='Shop',
            elements=[FakeElement(selector='#add-eggs', role='button', name='Add eggs')],
            on_load=show_cart,
            on_action=add,
        ),
    }


@dataclass(kw_only=True)
class Setup:
    """A host and everything behind it. `made` lists every backend the host asked for, oldest first."""

    not_supported: Collection[Feature] = ()
    idle_timeout: float = 600
    jar: InMemoryJar = field(default_factory=InMemoryJar)
    lease: InMemoryJarLease = field(default_factory=InMemoryJarLease)
    made: list[FakeBrowser] = field(default_factory=list[FakeBrowser])

    def new_backend(self) -> FakeBrowser:
        browser = FakeBrowser(pages=shop_pages(), not_supported=self.not_supported)
        self.made.append(browser)
        return browser

    def host(self) -> BrowserHost:
        return BrowserHost(
            new_backend=self.new_backend,
            jar=self.jar,
            lease=self.lease,
            idle_timeout=self.idle_timeout,
            reap_every=self.idle_timeout / 5,
        )


async def sign_in_and_add_eggs(host: BrowserHost, run: dict[str, str] = ALICE) -> None:
    await host.act(**run, action=Navigate(url=f'{SHOP}/login'))
    await host.act(**run, action=Click(target=Selector(css='#submit')))
    await host.act(**run, action=Click(target=Selector(css='#add-eggs')))


async def page_text(host: BrowserHost, run: dict[str, str] = ALICE) -> str:
    return (await host.snapshot(**run)).snapshot.text.splitlines()[0]


# --- one browser per run ---


async def test_a_retry_of_the_run_gets_the_same_browser() -> None:
    setup = Setup()
    host = setup.host()
    assert await host.start(**ALICE) == Started(url=BLANK_URL, reused=False)
    await sign_in_and_add_eggs(host)
    assert await host.start(**ALICE) == Started(url=f'{SHOP}/shop', reused=True)
    assert await page_text(host) == 'Signed in. In cart: eggs'
    assert len(setup.made) == 1


async def test_browser_cap_saves_and_eviction_restores_state() -> None:
    setup = Setup()
    host = BrowserHost(new_backend=setup.new_backend, jar=setup.jar, lease=setup.lease, max_open_browsers=1)
    await host.start(**ALICE)
    await sign_in_and_add_eggs(host)
    bob = {'run_id': 'run-2', 'user_id': 'bob'}
    await host.start(**bob)
    assert not setup.made[0].is_open
    saved = await setup.jar.load(user_id='alice')
    assert saved is not None and saved.url == f'{SHOP}/shop'
    restarted = await host.start(**ALICE)
    assert restarted.restarted is not None
    assert await page_text(host) == 'Signed in. In cart: eggs'
    assert not setup.made[1].is_open


async def test_browser_under_construction_uses_a_slot() -> None:
    started, proceed = asyncio.Event(), asyncio.Event()

    class SlowBrowser(FakeBrowser):
        async def open(self, state: BrowserState | None) -> None:
            started.set()
            await proceed.wait()
            await super().open(state)

    setup = Setup()
    host = BrowserHost(
        new_backend=lambda: SlowBrowser(pages=shop_pages()),
        jar=setup.jar,
        lease=setup.lease,
        max_open_browsers=1,
    )
    first = asyncio.create_task(host.start(**ALICE))
    try:
        await asyncio.wait_for(started.wait(), 1)
        with pytest.raises(ActionFailed, match='all browsers are in use'):
            await asyncio.wait_for(host.start(run_id='run-2', user_id='bob'), 1)
    finally:
        proceed.set()
        await first
    assert host._launching == 0


async def test_browser_cap_does_not_evict_a_handoff() -> None:
    setup = Setup()
    host = BrowserHost(new_backend=setup.new_backend, jar=setup.jar, lease=setup.lease, max_open_browsers=1)
    await host.start(**ALICE)
    await host.start_handoff(**ALICE, reason='sign in')
    with pytest.raises(ActionFailed, match='all browsers are in use'):
        await host.start(run_id='run-2', user_id='bob')
    assert setup.made[0].is_open
    assert len(setup.made) == 1


async def test_start_opens_the_saved_state() -> None:
    setup = Setup()
    saved = BrowserState(
        url=f'{SHOP}/shop',
        cookies=[Cookie(name='sid', value='1', domain='shop.test', http_only=True)],
        local_storage={SHOP: {'cart': 'milk'}},
    )
    await setup.jar.save(user_id='alice', state=saved)
    host = setup.host()
    assert await host.start(**ALICE) == Started(url=f'{SHOP}/shop', reused=False)
    assert await page_text(host) == 'Signed in. In cart: milk'


async def test_a_run_that_ends_is_saved_and_closed() -> None:
    setup = Setup()
    host = setup.host()
    await host.start(**ALICE)
    await sign_in_and_add_eggs(host)
    assert await host.close(**ALICE) is True
    assert not setup.made[0].is_open
    saved = await setup.jar.load(user_id='alice')
    assert saved is not None
    assert saved.url == f'{SHOP}/shop'
    assert [c.name for c in saved.cookies] == ['sid']
    assert saved.local_storage == {SHOP: {'cart': 'eggs'}}
    assert not await setup.lease.holds(**ALICE)


async def test_a_run_closed_before_its_first_call_never_opens_a_browser() -> None:
    setup = Setup()
    host = setup.host()
    with pytest.raises(UnknownRun):
        await host.close(**ALICE)  # the user stopped the run before it used the browser
    with pytest.raises(UnknownRun):
        await host.start(**ALICE)  # a call that was already on its way
    assert setup.made == []
    assert not await setup.lease.holds(**ALICE)


async def test_unknown_closed_and_other_users_runs_look_the_same() -> None:
    host = Setup().host()
    await host.start(**ALICE)
    await host.start(run_id='run-2', user_id='bob')
    await host.close(run_id='run-2', user_id='bob')
    for run_id, user_id in [('run-1', 'bob'), ('nope', 'alice'), ('run-2', 'bob')]:
        run = {'run_id': run_id, 'user_id': user_id}
        with pytest.raises(UnknownRun, match='^no browser for this run$'):
            await host.act(**run, action=Navigate(url=f'{SHOP}/shop'))
        for call in (host.snapshot, host.screenshot, host.save_state, host.close):
            with pytest.raises(UnknownRun, match='^no browser for this run$'):
                await call(**run)
        with pytest.raises(UnknownRun):
            await host.start_handoff(**run, reason='x')
    for run_id, user_id in [('run-1', 'bob'), ('run-2', 'bob')]:
        with pytest.raises(UnknownRun):
            await host.start(run_id=run_id, user_id=user_id)


async def test_watching_never_opens_or_keeps_a_browser() -> None:
    setup = Setup(idle_timeout=0.05)
    host = setup.host()
    with pytest.raises(UnknownRun):
        await host.peek_screenshot(**ALICE)  # no run yet
    await host.start(**ALICE)
    assert (await host.peek_screenshot(**ALICE)).png.startswith(b'\x89PNG')
    with pytest.raises(UnknownRun):
        await host.peek_screenshot(run_id='run-1', user_id='bob')
    async with host._runs['run-1'].lock:  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(UnknownRun):
            await host.peek_screenshot(**ALICE)  # busy with a call
    await asyncio.sleep(0.06)
    await host.peek_screenshot(**ALICE)  # does not count as use
    assert await host.reap_idle() == 1
    with pytest.raises(UnknownRun):
        await host.peek_screenshot(**ALICE)  # reaped, and not reopened
    assert len(setup.made) == 1
    await host.start(**ALICE)
    await host.start_handoff(**ALICE, reason='Please sign in')
    with pytest.raises(HandoffActive):
        await host.peek_screenshot(**ALICE)


class SlowJar(InMemoryJar):
    async def save(self, *, user_id: str, state: BrowserState) -> None:
        await asyncio.sleep(0.05)
        await super().save(user_id=user_id, state=state)


async def test_a_call_waiting_while_the_run_closes_does_not_reopen_it() -> None:
    setup = Setup(jar=SlowJar())
    host = setup.host()
    await host.start(**ALICE)
    closing = asyncio.ensure_future(host.close(**ALICE))
    await asyncio.sleep(0.01)  # close holds the run while it saves
    with pytest.raises(UnknownRun):
        await host.act(**ALICE, action=Navigate(url=f'{SHOP}/shop'))
    assert await closing is True
    assert len(setup.made) == 1 and not setup.made[0].is_open


async def test_backend_errors_pass_through_without_a_restart() -> None:
    setup = Setup(not_supported={'screenshot'})
    host = setup.host()
    await host.start(**ALICE)
    with pytest.raises(TargetNotFoundError):
        await host.act(**ALICE, action=Click(target=Selector(css='#missing')))
    with pytest.raises(NotSupported):
        await host.screenshot(**ALICE)
    assert await host.act(**ALICE, action=Navigate(url=f'{SHOP}/shop')) == ActionResult()
    assert len(setup.made) == 1


# --- one writer per user's jar ---


async def test_one_run_per_user_at_a_time() -> None:
    setup = Setup()
    host = setup.host()
    await host.start(**ALICE)
    second = {'run_id': 'run-2', 'user_id': 'alice'}
    with pytest.raises(UserBusy):
        await host.start(**second)
    await host.start(run_id='run-3', user_id='bob')  # other users are not affected
    await sign_in_and_add_eggs(host)
    await host.close(**ALICE)
    assert await host.start(**second) == Started(url=f'{SHOP}/shop', reused=False)
    assert await page_text(host, second) == 'Signed in. In cart: eggs'
    assert await setup.lease.holds(**second)


async def test_an_engine_that_cannot_export_leaves_the_jar_alone() -> None:
    setup = Setup(not_supported={'export'})
    before = BrowserState(url=f'{SHOP}/shop', local_storage={SHOP: {'cart': 'milk'}})
    await setup.jar.save(user_id='alice', state=before)
    host = setup.host()
    await host.start(**ALICE)
    await sign_in_and_add_eggs(host)
    with pytest.raises(NotSupported):
        await host.save_state(**ALICE)
    assert await host.close(**ALICE) is False
    assert await setup.jar.load(user_id='alice') == before
    assert setup.jar.saves == 1


# --- the idle reaper ---


async def test_the_reaper_saves_then_closes_idle_browsers() -> None:
    setup = Setup(idle_timeout=0.05)
    async with setup.host() as host:
        await host.start(**ALICE)
        await sign_in_and_add_eggs(host)
        await asyncio.sleep(0.2)
        assert not setup.made[0].is_open
        saved = await setup.jar.load(user_id='alice')
        assert saved is not None and saved.local_storage == {SHOP: {'cart': 'eggs'}}

        result = await host.act(**ALICE, action=Click(target=Selector(css='#add-eggs')))
        assert result == ActionResult(restarted=Restarted(reason='closed after 0.05 seconds idle', url=f'{SHOP}/shop'))
        assert (await host.snapshot(**ALICE)).restarted is None  # reported once
        assert await page_text(host) == 'Signed in. In cart: eggs eggs'
        assert len(setup.made) == 2


async def test_the_reaper_leaves_browsers_in_use() -> None:
    setup = Setup(idle_timeout=0.2)
    async with setup.host() as host:
        await host.start(**ALICE)
        for _ in range(10):
            await host.act(**ALICE, action=Navigate(url=f'{SHOP}/shop'))
            await asyncio.sleep(0.05)
        assert setup.made[0].is_open
        assert await host.reap_idle() == 0


async def test_a_reaped_browser_that_could_not_be_saved_says_so() -> None:
    setup = Setup(not_supported={'export'}, idle_timeout=0.05)
    async with setup.host() as host:
        await host.start(**ALICE)
        await asyncio.sleep(0.2)
        assert not setup.made[0].is_open
        restarted = (await host.snapshot(**ALICE)).restarted
        assert restarted is not None
        assert restarted.reason == (
            'closed after 0.05 seconds idle; changes since the last save are lost, because this browser cannot '
            'export its state'
        )


async def test_a_reaped_browser_during_a_handoff_comes_back_for_the_user() -> None:
    setup = Setup(idle_timeout=0.05)
    async with setup.host() as host:
        await host.start(**ALICE)
        await host.act(**ALICE, action=Navigate(url=f'{SHOP}/login'))
        handoff = await host.start_handoff(**ALICE, reason='Please sign in')
        await asyncio.sleep(0.2)  # the user does not open the link in time
        assert not setup.made[0].is_open
        shot = await host.screenshot(**ALICE, handoff_id=handoff.handoff_id)
        assert shot.restarted == Restarted(reason='closed after 0.05 seconds idle', url=f'{SHOP}/login')


def test_durations_read_well() -> None:
    assert _duration(600) == '10 minutes'
    assert _duration(60) == '1 minute'
    assert _duration(90) == '90 seconds'


# --- crashes and restarts ---


async def test_a_crash_is_reported_and_reads_are_retried() -> None:
    setup = Setup()
    host = setup.host()
    await host.start(**ALICE)
    await sign_in_and_add_eggs(host)
    await host.save_state(**ALICE)
    await setup.made[0].close()  # the engine dies under the service

    result = await host.snapshot(**ALICE)
    assert result.restarted == Restarted(reason=CRASHED, url=f'{SHOP}/shop')
    assert result.snapshot.text.startswith('Signed in. In cart: eggs\n')
    assert len(setup.made) == 2


async def test_an_action_is_not_replayed_on_a_new_browser() -> None:
    setup = Setup()
    host = setup.host()
    await host.start(**ALICE)
    await sign_in_and_add_eggs(host)
    await host.save_state(**ALICE)
    await setup.made[0].close()

    eggs = Click(target=Selector(css='#add-eggs'))
    with pytest.raises(ActionFailed, match='did not finish'):
        await host.act(**ALICE, action=eggs)
    assert len(setup.made) == 1  # the new browser starts on the next call
    result = await host.act(**ALICE, action=eggs)
    assert result.restarted == Restarted(reason=CRASHED, url=f'{SHOP}/shop')
    assert setup.made[1].actions == [eggs]
    assert await page_text(host) == 'Signed in. In cart: eggs eggs'


async def test_a_restart_of_the_service_is_reported() -> None:
    setup = Setup()
    first = setup.host()
    await first.start(**ALICE)
    await sign_in_and_add_eggs(first)
    await first.save_state(**ALICE)

    second = setup.host()  # a new process; the lease and the jar are in Postgres
    assert await second.start(**ALICE) == Started(
        url=f'{SHOP}/shop', reused=False, restarted=Restarted(reason=SERVICE_RESTARTED, url=f'{SHOP}/shop')
    )
    assert await page_text(second) == 'Signed in. In cart: eggs'
    with pytest.raises(UnknownRun):
        await second.snapshot(run_id='run-1', user_id='bob')
    await first.aclose()


async def test_shutdown_saves_and_the_next_call_restarts() -> None:
    setup = Setup()
    host = setup.host()
    await host.start(**ALICE)
    await sign_in_and_add_eggs(host)
    await host.aclose()
    assert not setup.made[0].is_open
    result = await host.snapshot(**ALICE)
    assert result.restarted == Restarted(reason=SERVICE_RESTARTED, url=f'{SHOP}/shop')
    assert result.snapshot.text.startswith('Signed in. In cart: eggs\n')


# --- hand-off ---


async def test_a_handoff_holds_the_browser_for_the_user() -> None:
    setup = Setup()
    host = setup.host()
    await host.start(**ALICE)
    await host.act(**ALICE, action=Navigate(url=f'{SHOP}/login'))
    handoff = await host.start_handoff(**ALICE, reason='Please sign in')
    assert (handoff.run_id, handoff.user_id, handoff.reason) == ('run-1', 'alice', 'Please sign in')
    assert await host.start_handoff(**ALICE, reason='again') == handoff

    for refused in (
        host.act(**ALICE, action=Navigate(url=f'{SHOP}/shop')),
        host.snapshot(**ALICE),
        host.screenshot(**ALICE),
    ):
        with pytest.raises(HandoffActive):
            await refused
    with pytest.raises(HandoffNotActive):
        await host.act(**ALICE, action=Navigate(url=f'{SHOP}/shop'), handoff_id='wrong')

    signed = await host.act(**ALICE, action=Click(target=Selector(css='#submit')), handoff_id=handoff.handoff_id)
    assert signed == ActionResult()
    assert (await host.screenshot(**ALICE, handoff_id=handoff.handoff_id)).screenshot.png.startswith(b'\x89PNG')
    await host.save_state(**ALICE)  # allowed during a hand-off

    ended = await host.end_handoff(**ALICE, handoff_id=handoff.handoff_id)
    assert (ended.handoff_id, ended.url, ended.saved) == (handoff.handoff_id, f'{SHOP}/shop', True)
    with pytest.raises(HandoffNotActive):
        await host.end_handoff(**ALICE, handoff_id=handoff.handoff_id)
    with pytest.raises(HandoffNotActive):
        await host.act(**ALICE, action=Navigate(url=f'{SHOP}/shop'), handoff_id=handoff.handoff_id)
    assert await page_text(host) == 'Signed in. In cart: empty'
    saved = await setup.jar.load(user_id='alice')
    assert saved is not None and [c.name for c in saved.cookies] == ['sid']


async def test_closing_ends_the_handoff() -> None:
    host = Setup().host()
    await host.start(**ALICE)
    handoff = await host.start_handoff(**ALICE, reason='Please sign in')
    assert await host.close(**ALICE) is True
    with pytest.raises(UnknownRun):
        await host.end_handoff(**ALICE, handoff_id=handoff.handoff_id)
