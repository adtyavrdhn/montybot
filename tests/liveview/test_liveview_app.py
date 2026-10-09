"""The live view app over a real WebSocket, with `FakeBrowser` behind the stand-in browser service."""

from __future__ import annotations

import asyncio
import dataclasses
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

import httpx
import pytest
from liveview_harness import StubBrowserService, backends, serve_app, serve_fixtures
from playwright.async_api import async_playwright, expect
from websockets.exceptions import InvalidStatus

from sammy.browser.contract import (
    Action,
    Click,
    MouseDown,
    MouseMove,
    MouseUp,
    Navigate,
    Point,
    Press,
    Scroll,
    Type,
)
from sammy.browser.fake import FakeBrowser, FakePage
from sammy.browser.live import LiveInput
from sammy.browser.service import Handoff, HandoffActive
from sammy.liveview.app import CLOSE_ENDED, CLOSE_NOT_FOUND, CLOSE_REPLACED, CLOSE_SIGNED_OUT, live_view_app
from sammy.liveview.auth import SESSION_COOKIE, StubAuthenticator
from sammy.liveview.client import LiveViewClient, LiveViewClosed
from sammy.liveview.handoffs import InMemoryHandoffs
from sammy.liveview.wire import CloseTab, Command, NewTab, ViewportSize

pytestmark = pytest.mark.anyio

RUN = 'run-1'
LOGIN = 'http://shop.test/login'


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@dataclass(kw_only=True)
class Live:
    base: str
    service: StubBrowserService
    handoffs: InMemoryHandoffs
    handoff: Handoff
    browser: FakeBrowser
    alice: str
    """Alice's session cookie; she asked for the run."""
    bob: str

    def ws(self, handoff_id: str | None = None) -> str:
        return f'{self.base.replace("http", "ws", 1)}/handoff/{handoff_id or self.handoff.handoff_id}/ws'

    def connect(
        self, session: str | None = None, *, origin: str | None = None
    ) -> AbstractAsyncContextManager[LiveViewClient]:
        return LiveViewClient.connect(self.ws(), session=session or self.alice, origin=origin)


@asynccontextmanager
async def live(*, allowed_origins: frozenset[str] = frozenset(), **fake: object) -> AsyncIterator[Live]:
    browser = FakeBrowser(pages={LOGIN: FakePage(title='Sign in', text='Sign in to continue')}, **fake)  # type: ignore[arg-type]
    service = StubBrowserService(lambda: browser)
    await service.start(run_id=RUN, user_id='alice')
    await service.act(run_id=RUN, user_id='alice', action=Navigate(url=LOGIN))
    handoff = await service.start_handoff(run_id=RUN, user_id='alice', reason='Please sign in to the shop')
    handoffs = InMemoryHandoffs()
    handoffs.add(handoff)
    auth = StubAuthenticator()
    app = live_view_app(service=service, handoffs=handoffs, auth=auth, allowed_origins=allowed_origins)
    try:
        async with serve_app(app) as base:
            yield Live(
                base=base,
                service=service,
                handoffs=handoffs,
                handoff=handoff,
                browser=browser,
                alice=auth.sign_in('alice'),
                bob=auth.sign_in('bob'),
            )
    finally:
        await service.close_all()


async def close_code(url: str, session: str | None) -> int | None:
    async with LiveViewClient.connect(url, session=session) as client:
        with pytest.raises(LiveViewClosed):
            await client.wait_until(lambda: False)
        return client.close_code


async def eventually(condition: Callable[[], bool], timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, 'timed out'
        await asyncio.sleep(0.02)


def performed(browser: FakeBrowser) -> list[Action]:
    return [a for a in browser.actions if not isinstance(a, Navigate)]


async def test_the_page_is_only_for_the_requester() -> None:
    async def get(url: str, session: str | None = None) -> httpx.Response:
        async with httpx.AsyncClient(cookies={SESSION_COOKIE: session} if session else None) as http:
            return await http.get(url)

    async with live() as setup:
        path = f'{setup.base}/handoff/{setup.handoff.handoff_id}'
        assert (await get(path)).status_code == 401
        assert (await get(path, setup.bob)).status_code == 404
        assert (await get(f'{setup.base}/handoff/nope', setup.alice)).status_code == 404
        # Still the page, so inside the web app its "Back to chat" button gets the user out.
        assert 'id="back"' in (await get(f'{setup.base}/handoff/nope', setup.alice)).text
        page = await get(path, setup.alice)
        assert page.status_code == 200
        assert 'Give back to Sammy' in page.text
        assert "script-src 'self'" in page.headers['content-security-policy']
        assert page.headers['cache-control'] == 'no-store'
        script = await get(f'{setup.base}/live.js')
        assert script.status_code == 200 and 'give_back' in script.text


async def test_the_socket_is_only_for_the_requester() -> None:
    async with live() as setup:
        assert await close_code(setup.ws(), None) == CLOSE_SIGNED_OUT
        assert await close_code(setup.ws(), setup.bob) == CLOSE_NOT_FOUND
        assert await close_code(setup.ws('nope'), setup.alice) == CLOSE_NOT_FOUND
        assert await close_code(setup.ws(), 'forged-session') == CLOSE_SIGNED_OUT


async def test_other_sites_cannot_open_the_socket() -> None:
    async with live(allowed_origins=frozenset({'https://app.example'})) as setup:
        with pytest.raises(InvalidStatus) as refused:
            async with setup.connect(origin='https://evil.example'):
                pass
        assert refused.value.response.status_code == 403
        for origin in (setup.base, 'https://app.example'):
            async with setup.connect(origin=origin) as client:
                await client.wait_until(lambda: client.hello is not None)


async def test_the_user_sees_and_drives_while_the_agent_is_refused() -> None:
    async with live() as setup, setup.connect() as client:
        await client.wait_until(lambda: client.hello is not None and client.frame is not None)
        assert client.hello is not None and client.hello.reason == 'Please sign in to the shop'
        assert client.frame is not None and client.frame.frame.image.startswith(b'\x89PNG')
        assert await client.wait_for_url(lambda url: url == LOGIN) == LOGIN

        at, to = Point(x=150, y=320), Point(x=170, y=330)
        actions: list[LiveInput] = [
            MouseDown(at=at),
            MouseMove(at=to),
            MouseUp(at=to),
            Click(target=at),
            Type(text='mike'),
            Press(key='Enter', modifiers=('Shift',)),
            Scroll(delta_y=300, at=at),
        ]
        for action in actions:
            await client.send(action)
        await eventually(lambda: len(performed(setup.browser)) == len(actions))
        assert performed(setup.browser) == actions

        # No screenshot or snapshot for the agent, so nothing of the page reaches the model or its history.
        with pytest.raises(HandoffActive):
            await setup.service.screenshot(run_id=RUN, user_id='alice')
        with pytest.raises(HandoffActive):
            await setup.service.snapshot(run_id=RUN, user_id='alice')
        with pytest.raises(HandoffActive):
            await setup.service.act(run_id=RUN, user_id='alice', action=Click(target=at))
        assert len(performed(setup.browser)) == len(actions)


async def test_the_user_can_go_to_an_address_but_only_a_web_one() -> None:
    async with live() as setup, setup.connect() as client:
        await client.wait_until(lambda: client.hello is not None)
        for refused in ('file:///etc/passwd', 'chrome://settings', 'javascript:alert(1)', 'https://'):
            await client.send(Navigate(url=refused))
        await client.wait_until(lambda: len(client.errors) == 4)
        assert set(client.errors) == {'Only web addresses can be opened.'}
        await client.send(Navigate(url='http://shop.test/cart'))
        await eventually(lambda: setup.browser.actions[-1:] == [Navigate(url='http://shop.test/cart')])
        assert await client.wait_for_url(lambda url: url == 'http://shop.test/cart') == 'http://shop.test/cart'


async def test_a_polled_browser_has_no_buttons_and_says_so() -> None:
    """The polled source cannot go back, reload or open tabs: the hello says so, so the app disables them, and each
    is refused in words with the connection kept."""
    async with live() as setup, setup.connect() as client:
        await client.wait_until(lambda: client.hello is not None)
        assert client.hello is not None and not client.hello.controls
        await client.wait_until(lambda: client.active_tab is not None)
        tab = client.active_tab
        assert tab is not None and not tab.closable
        for message in (Command(kind='back'), Command(kind='reload'), NewTab(), CloseTab(tab_id=tab.tab_id)):
            await client.send(message)
        await client.wait_until(lambda: len(client.errors) == 4)
        assert set(client.errors) == {'This browser has no back, reload or tab buttons.'}
        assert performed(setup.browser) == []


async def test_an_input_the_engine_cannot_do_is_reported_and_the_connection_stays() -> None:
    async with live(not_supported={'scroll'}) as setup, setup.connect() as client:
        await client.wait_until(lambda: client.hello is not None)
        await client.send(Scroll(delta_y=10))
        await client.wait_until(lambda: bool(client.errors))
        assert client.errors == ['fake does not support scroll']
        await client.send(Click(target=Point(x=1, y=1)))
        await eventually(lambda: performed(setup.browser) == [Click(target=Point(x=1, y=1))])


async def test_a_new_connection_takes_over_from_the_old_one() -> None:
    async with live() as setup, setup.connect() as first:
        await first.wait_until(lambda: first.hello is not None)
        async with setup.connect() as second:
            with pytest.raises(LiveViewClosed):
                await first.wait_until(lambda: False)
            assert first.close_code == CLOSE_REPLACED
            await second.wait_until(lambda: second.frame is not None)
            await second.send(Press(key='Tab'))
            await eventually(lambda: performed(setup.browser) == [Press(key='Tab')])


async def test_reconnecting_and_giving_back() -> None:
    async with live() as setup:
        async with setup.connect() as client:
            await client.wait_until(lambda: client.hello is not None)
            await client.send(Click(target=Point(x=1, y=1)))
            await eventually(lambda: len(performed(setup.browser)) == 1)
        async with setup.connect() as client:  # the page reloads, or the phone's network changes
            await client.wait_until(lambda: client.frame is not None)
            await client.send(Type(text='hunter2'))
            await eventually(lambda: len(performed(setup.browser)) == 2)
            ended = await client.give_back()
            assert ended.given_back
            await client.wait_until(lambda: client.closed)
            assert client.close_code == 1000

        given = await asyncio.wait_for(setup.handoffs.wait_given_back(setup.handoff.handoff_id), 5)
        assert given.ended.url == LOGIN
        assert 'clicked once' in given.summary and 'typed 7 characters' in given.summary
        assert 'hunter2' not in given.summary
        assert {f.name for f in dataclasses.fields(given)} == {'handoff', 'ended', 'summary'}  # words, no images

        # The agent has the browser back, and the link is spent.
        await setup.service.screenshot(run_id=RUN, user_id='alice')
        assert await close_code(setup.ws(), setup.alice) == CLOSE_ENDED


async def test_the_page_in_a_real_browser() -> None:
    """The page's own script, in headless Chromium as the user's browser: frames drawn, input sent, give back."""
    async with live() as setup, async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={'width': 1400, 'height': 900})
            await context.add_cookies([{'name': SESSION_COOKIE, 'value': setup.alice, 'url': setup.base}])
            page = await context.new_page()
            errors: list[str] = []
            page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(f'{setup.base}/handoff/{setup.handoff.handoff_id}')
            # Locators, not wait_for_function: the page's CSP rightly refuses Playwright's string evaluation.
            await page.locator('#reason', has_text='sign in to the shop').wait_for()
            await page.locator('#url', has_text=LOGIN).wait_for()

            box = await page.locator('#view').bounding_box()
            assert box is not None
            await page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
            await page.mouse.down()
            await page.mouse.move(box['x'] + box['width'] / 2 + 20, box['y'] + box['height'] / 2)
            await page.mouse.up()
            await page.keyboard.type('hi')
            await page.keyboard.press('Enter')
            await eventually(lambda: len(performed(setup.browser)) > 0 and performed(setup.browser)[-1].kind == 'press')
            actions = performed(setup.browser)
            kinds = [a.kind for a in actions]
            assert [k for k in kinds if k != 'mouse_move'] == ['mouse_down', 'mouse_up', 'type', 'type', 'press']
            assert 'mouse_move' in kinds[kinds.index('mouse_down') : kinds.index('mouse_up')]  # dragged while held
            down = actions[kinds.index('mouse_down')]
            assert isinstance(down, MouseDown)
            assert down.at.x == pytest.approx(640, abs=2) and down.at.y == pytest.approx(360, abs=2)
            assert actions[-3:] == [Type(text='h'), Type(text='i'), Press(key='Enter')]

            await page.click('#give-back')
            await page.locator('#status', has_text='Thanks').wait_for()
            given = await asyncio.wait_for(setup.handoffs.wait_given_back(setup.handoff.handoff_id), 5)
            assert 'pressed 1 key' in given.summary
            assert errors == []  # nothing refused by the CSP, no script errors
        finally:
            await browser.close()


async def test_the_live_view_ends_when_the_handoff_ends_elsewhere() -> None:
    async with live() as setup, setup.connect() as client:
        await client.wait_until(lambda: client.frame is not None)
        await setup.service.close(run_id=RUN, user_id='alice')  # the run was cancelled, or the reaper closed it
        await client.wait_until(lambda: client.closed)
        assert client.ended is not None and not client.ended.given_back
        assert client.close_code == CLOSE_ENDED


async def test_a_size_the_engine_cannot_use_is_ignored() -> None:
    """The polled `FakeBrowser` cannot resize: the page's size is dropped quietly, and the user keeps driving."""
    async with live() as setup, setup.connect() as client:
        await client.wait_until(lambda: client.hello is not None)
        await client.send(ViewportSize(width=390, height=700))
        await client.send(Press(key='Tab'))
        await eventually(lambda: performed(setup.browser) == [Press(key='Tab')])
        assert client.errors == []


@pytest.mark.parametrize(('width', 'height', 'phone'), [(1280, 800, False), (390, 844, True)])
async def test_the_whole_picture_fits_the_screen(width: int, height: int, phone: bool) -> None:
    """The page in a real browser, on a laptop and on a phone, with real Chromium as the bot's browser: the picture
    fits the window without scrolling. On the phone the bot's page is laid out at the phone's size while the user
    drives, and at its own size again after the give-back."""
    with serve_fixtures() as origin:
        async with backends('chromium') as new, async_playwright() as playwright:
            service = StubBrowserService(new)
            handoffs = InMemoryHandoffs()
            auth = StubAuthenticator()
            await service.start(run_id=RUN, user_id='alice')
            await service.act(run_id=RUN, user_id='alice', action=Navigate(url=f'{origin}/size'))
            handoff = await service.start_handoff(run_id=RUN, user_id='alice', reason='Please sign in')
            handoffs.add(handoff)
            browser = await playwright.chromium.launch(headless=True)
            try:
                async with serve_app(live_view_app(service=service, handoffs=handoffs, auth=auth)) as base:
                    context = await browser.new_context(
                        viewport={'width': width, 'height': height},
                        is_mobile=phone,
                        device_scale_factor=3 if phone else 1,
                    )
                    await context.add_cookies([{'name': SESSION_COOKIE, 'value': auth.sign_in('alice'), 'url': base}])
                    page = await context.new_page()
                    await page.goto(f'{base}/handoff/{handoff.handoff_id}')
                    await page.locator('#reason', has_text='Sammy needs you: Please sign in').wait_for()
                    view = page.locator('#view')
                    # Frames are in CSS pixels: on the phone, as wide as the room the page has once it is phone-sized.
                    await expect(view).to_have_attribute('width', '1280' if not phone else re.compile(r'^3\d\d$'))
                    box = await view.bounding_box()
                    assert box is not None
                    assert box['x'] >= 0 and box['y'] >= 0
                    assert box['x'] + box['width'] <= width and box['y'] + box['height'] <= height
                    if phone:
                        assert box['width'] >= 370  # the phone's whole width, less a margin
                    else:
                        assert box['width'] / box['height'] == pytest.approx(1280 / 720, abs=0.01)
                    await page.click('#give-back')
                    await page.locator('#status', has_text='Thanks').wait_for()
                after = (await service.snapshot(run_id=RUN, user_id='alice')).snapshot.text
                assert 'size: 1280x720' in after
            finally:
                await browser.close()
                await service.close_all()


async def test_the_picture_keeps_coming_after_the_page_reconnects() -> None:
    """A dropped connection reconnects by itself. The new one numbers its frames from 1 again, and they are drawn,
    not taken for frames older than those of the long first connection."""
    with serve_fixtures() as origin:
        async with backends('chromium') as new, async_playwright() as playwright:
            service = StubBrowserService(new)
            handoffs = InMemoryHandoffs()
            auth = StubAuthenticator()
            await service.start(run_id=RUN, user_id='alice')
            await service.act(run_id=RUN, user_id='alice', action=Navigate(url=f'{origin}/size'))
            handoff = await service.start_handoff(run_id=RUN, user_id='alice', reason='Please sign in')
            handoffs.add(handoff)
            browser = await playwright.chromium.launch(headless=True)
            try:
                async with serve_app(live_view_app(service=service, handoffs=handoffs, auth=auth)) as base:
                    # bypass_csp: the page's policy forbids the evaluated test code, not anything the page does.
                    context = await browser.new_context(viewport={'width': 1280, 'height': 800}, bypass_csp=True)
                    await context.add_cookies([{'name': SESSION_COOKIE, 'value': auth.sign_in('alice'), 'url': base}])
                    page = await context.new_page()
                    await page.goto(f'{base}/handoff/{handoff.handoff_id}')
                    await page.wait_for_function('shown >= 1')
                    await page.evaluate("""() => {
                        shown = 1000;  // as after a long first connection
                        window.draws = 0;
                        const draw = context.drawImage.bind(context);
                        context.drawImage = (...args) => { window.draws++; draw(...args); };
                        socket.close();  // a dropped connection
                    }""")
                    await page.wait_for_function('window.draws > 0')
            finally:
                await browser.close()
                await service.close_all()
