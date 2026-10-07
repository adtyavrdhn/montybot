"""The live view on real engines: Chromium (Playwright, headless), Chromium over our own CDP pipe (`cdp`, headless)
and Servo (servoshell, headless WebDriver).

Chromium and Servo are driven directly by stand-in backends from `liveview_harness`, not by #11's or #12's backends;
`cdp` is the real `ChromiumCDPBackend`. Servo tests skip when servoshell is not installed (set `MONTYBOT_SERVO` to its
path).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import pytest
from liveview_harness import HOLD_CENTRE, StubBrowserService, backends, serve_app, serve_demo_shop, serve_fixtures

from montybot.browser.conformance import BUTTON_CENTRE, HOLD_END, HOLD_START, serve_site, wait_for_text
from montybot.browser.contract import (
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
from montybot.browser.live import Frame, FrameSource, LiveViewBackend, Tabs
from montybot.browser.service import HandoffActive
from montybot.browser.state import BrowserState
from montybot.liveview.app import live_view_app
from montybot.liveview.auth import StubAuthenticator
from montybot.liveview.client import LiveViewClient
from montybot.liveview.handoffs import InMemoryHandoffs
from montybot.liveview.scripted_user import press_and_hold, sign_in_to_demo_shop
from montybot.liveview.wire import ViewportSize

pytestmark = [pytest.mark.anyio, pytest.mark.parametrize('engine', ['chromium', 'cdp', 'servo'])]

RUN = 'run-1'
USER = 'alice'


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@asynccontextmanager
async def opened_source(engine: str, url: str) -> AsyncIterator[tuple[LiveViewBackend, FrameSource]]:
    async with backends(engine) as new:
        backend = new()
        assert isinstance(backend, LiveViewBackend)
        await backend.open(BrowserState(url=url))
        try:
            source = await backend.live_view()
            try:
                yield backend, source
            finally:
                await source.close()
        finally:
            await backend.close()


async def next_update(updates: AsyncIterator[Frame | Tabs], kind: type[Frame | Tabs]) -> Frame | Tabs:
    async def find() -> Frame | Tabs:
        async for update in updates:
            if isinstance(update, kind):
                return update
        raise AssertionError('the source ended')

    return await asyncio.wait_for(find(), 10)


async def test_input_reaches_the_page(engine: str) -> None:
    site = serve_site()
    async with opened_source(engine, site.actions) as (backend, source):
        updates = source.updates()
        frame = await next_update(updates, Frame)
        assert isinstance(frame, Frame) and frame.width > 0 and frame.height > 0
        for action in (MouseDown(at=HOLD_START), MouseMove(at=HOLD_END), MouseUp(at=HOLD_END)):
            await source.send(action)
        await wait_for_text(backend, 'down: 150,320', 'held move: 170,330', 'up: 170,330')
        for action in (
            Click(target=BUTTON_CENTRE),
            Click(target=Point(x=200, y=215)),
            Press(key='End'),
            Type(text='hello'),
            Press(key='b', modifiers=('Control',)),
            Scroll(delta_y=400),
        ):
            await source.send(action)
        await wait_for_text(backend, 'clicked: button', 'typed: oldhello', 'key: Control+b', 'scrolled: 0,400')
        await updates.aclose()  # pyright: ignore[reportAttributeAccessIssue]


async def test_closing_lets_go_of_a_held_button(engine: str) -> None:
    """A dropped connection must not leave the page with the mouse held down."""
    site = serve_site()
    async with opened_source(engine, site.actions) as (backend, source):
        await source.send(MouseDown(at=HOLD_START))
        await wait_for_text(backend, 'down: 150,320')
        await source.close()
        await wait_for_text(backend, 'up: 150,320')


async def test_popups_are_followed_and_the_run_keeps_its_tab(engine: str) -> None:
    with serve_fixtures() as origin:
        async with opened_source(engine, f'{origin}/popup') as (backend, source):
            updates = source.updates()
            tabs = await next_update(updates, Tabs)
            assert isinstance(tabs, Tabs) and len(tabs.tabs) == 1
            await source.send(Click(target=Point(x=120, y=112)))
            while not (len(tabs.tabs) == 2 and any(t.active and t.url.endswith('/popup-target') for t in tabs.tabs)):
                tabs = await next_update(updates, Tabs)
                assert isinstance(tabs, Tabs)
            opener = next(t for t in tabs.tabs if not t.active)
            await source.switch_tab(opener.tab_id)
            while not (len(tabs.tabs) == 2 and any(t.active and t.tab_id == opener.tab_id for t in tabs.tabs)):
                tabs = await next_update(updates, Tabs)
                assert isinstance(tabs, Tabs)
            popup = next(t for t in tabs.tabs if not t.active)
            await source.switch_tab(popup.tab_id)
            await source.close()
            assert (await backend.snapshot()).url == f'{origin}/popup'  # the agent is back on its own tab


async def test_a_phone_gets_the_page_at_its_size_until_the_give_back(engine: str) -> None:
    """On a phone the user drives the page laid out for the phone; after the give-back the agent has it at its own size
    again. Servo cannot resize, and keeps its own size."""
    with serve_fixtures() as origin:
        async with backends(engine) as new:
            service = StubBrowserService(new)
            handoffs = InMemoryHandoffs()
            auth = StubAuthenticator()
            app = live_view_app(service=service, handoffs=handoffs, auth=auth)
            try:
                await service.start(run_id=RUN, user_id=USER)
                await service.act(run_id=RUN, user_id=USER, action=Navigate(url=f'{origin}/size'))
                before = (await service.snapshot(run_id=RUN, user_id=USER)).snapshot.text
                handoff = await service.start_handoff(run_id=RUN, user_id=USER, reason='Please sign in')
                handoffs.add(handoff)
                async with serve_app(app) as base:
                    ws = f'{base.replace("http", "ws", 1)}/handoff/{handoff.handoff_id}/ws'
                    async with LiveViewClient.connect(ws, session=auth.sign_in(USER)) as user:
                        await user.wait_until(lambda: user.frame is not None)
                        assert user.frame is not None
                        desktop = (user.frame.frame.width, user.frame.frame.height)
                        await user.send(ViewportSize(width=390, height=700))
                        if engine != 'servo':
                            await user.wait_until(lambda: user.frame is not None and user.frame.frame.width == 390)
                            assert user.frame.frame.height == 700
                            await user.send(ViewportSize(width=1280, height=700))  # turned wide: its own size again
                            await user.wait_until(lambda: user.frame is not None and user.frame.frame.width == 1280)
                            await user.send(ViewportSize(width=390, height=700))
                            await user.wait_until(lambda: user.frame is not None and user.frame.frame.width == 390)
                        else:
                            await user.send(Press(key='Tab'))  # the size was ignored, and the connection works
                            await asyncio.sleep(0.5)
                            assert (user.frame.frame.width, user.frame.frame.height) == desktop
                        assert user.errors == []
                    # The phone's connection dropped without a give-back; the user comes back on a laptop.
                    async with LiveViewClient.connect(ws, session=auth.sign_in(USER)) as laptop:
                        await laptop.wait_until(lambda: laptop.frame is not None)
                        assert laptop.frame is not None
                        assert (laptop.frame.frame.width, laptop.frame.frame.height) == desktop
                        await laptop.give_back()
                after = (await service.snapshot(run_id=RUN, user_id=USER)).snapshot.text
                assert after == before
                if engine == 'chromium':  # Playwright's default viewport
                    assert 'size: 1280x720' in after
            finally:
                await service.close_all()


async def test_u2_sign_in_through_the_live_view(engine: str) -> None:
    """U2: the agent hits a sign-in wall, the scripted user signs in over the WebSocket and gives the browser back,
    and the agent carries on signed in. On Chromium the sign-in is saved and the next run starts signed in."""
    with serve_demo_shop() as shop:
        async with backends(engine) as new:
            service = StubBrowserService(new)
            handoffs = InMemoryHandoffs()
            auth = StubAuthenticator()
            app = live_view_app(service=service, handoffs=handoffs, auth=auth)
            try:
                await service.start(run_id=RUN, user_id=USER)
                await service.act(run_id=RUN, user_id=USER, action=Navigate(url=f'{shop}/shop'))
                page = (await service.snapshot(run_id=RUN, user_id=USER)).snapshot
                assert 'Sign in to continue' in page.text
                handoff = await service.start_handoff(run_id=RUN, user_id=USER, reason='Please sign in to the shop')
                handoffs.add(handoff)

                async with serve_app(app) as base:
                    ws = f'{base.replace("http", "ws", 1)}/handoff/{handoff.handoff_id}/ws'
                    async with LiveViewClient.connect(ws, session=auth.sign_in(USER)) as user:
                        await sign_in_to_demo_shop(user, username='mike', add='coffee')
                        with pytest.raises(HandoffActive):
                            await service.screenshot(run_id=RUN, user_id=USER)
                        ended = await user.give_back()
                        assert ended.given_back
                    given = await asyncio.wait_for(handoffs.wait_given_back(handoff.handoff_id), 10)
                assert urlsplit(given.ended.url).path == '/shop'
                assert 'mike' not in given.summary and 'hunter2' not in given.summary

                page = (await service.snapshot(run_id=RUN, user_id=USER)).snapshot
                assert 'Signed in as mike' in page.text
                await service.act(run_id=RUN, user_id=USER, action=Navigate(url=f'{shop}/cart'))
                page = (await service.snapshot(run_id=RUN, user_id=USER)).snapshot
                assert 'Cart for mike' in page.text
                await wait_for_cart(service, 'coffee')

                saved = await service.close(run_id=RUN, user_id=USER)
                if engine == 'servo':
                    assert not saved  # Servo cannot export HttpOnly cookies (#12), so the jar is left as it was
                    return
                assert saved
                await service.start(run_id='run-2', user_id=USER)  # the next day
                await service.act(run_id='run-2', user_id=USER, action=Navigate(url=f'{shop}/shop'))
                page = (await service.snapshot(run_id='run-2', user_id=USER)).snapshot
                assert 'Signed in as mike' in page.text
            finally:
                await service.close_all()


async def test_signing_in_by_the_outline_alone(engine: str) -> None:
    """A user who drives with VoiceOver: they read the page from its outline (what a screen reader says, with where
    each thing is) and act on it by pressing items, which clicks them. The outline never reveals a password. An engine
    that cannot read the page says so."""
    with serve_demo_shop() as shop:
        async with backends(engine) as new:
            service = StubBrowserService(new)
            handoffs = InMemoryHandoffs()
            auth = StubAuthenticator()
            app = live_view_app(service=service, handoffs=handoffs, auth=auth)
            try:
                await service.start(run_id=RUN, user_id=USER)
                await service.act(run_id=RUN, user_id=USER, action=Navigate(url=f'{shop}/login'))
                handoff = await service.start_handoff(run_id=RUN, user_id=USER, reason='Please sign in to the shop')
                handoffs.add(handoff)
                async with serve_app(app) as base:
                    ws = f'{base.replace("http", "ws", 1)}/handoff/{handoff.handoff_id}/ws'
                    async with LiveViewClient.connect(ws, session=auth.sign_in(USER)) as user:
                        await user.next_frame()
                        outline = await user.read_outline()
                        if engine == 'servo':
                            assert not outline.available and outline.items == ()
                            return
                        assert outline.available and outline.title == 'Sign in'
                        roles = [(item.role, item.name) for item in outline.items]
                        assert roles[:4] == [
                            ('heading', 'Sign in'),
                            ('textbox', 'username'),
                            ('textbox', 'password (hunter2)'),
                            ('button', 'Sign in'),
                        ]

                        async def press(name: str, role: str = 'textbox') -> None:
                            item = next(
                                i for i in (await user.read_outline()).items if (i.role, i.name) == (role, name)
                            )
                            centre = Point(x=item.x + item.width / 2, y=item.y + item.height / 2)
                            await user.send(MouseDown(at=centre))
                            await user.send(MouseUp(at=centre))

                        await press('username')
                        await user.send(Type(text='mike'))
                        await press('password (hunter2)')
                        await user.send(Type(text='wrong'))
                        typed = {i.name: i for i in (await user.read_outline()).items}
                        assert typed['username'].value == 'mike'
                        assert (
                            typed['password (hunter2)'].value == '5 characters' and typed['password (hunter2)'].secure
                        )
                        assert typed['password (hunter2)'].focused
                        await press('Sign in', 'button')

                        async def reads(line: tuple[str, str]) -> None:
                            for _ in range(50):
                                if line in [(i.role, i.name) for i in (await user.read_outline()).items]:
                                    return
                                await asyncio.sleep(0.1)
                            raise AssertionError(f'the page never read {line}: {user.outline}')

                        await reads(('text', 'Wrong password.'))

                        await press('username')  # the page came back empty
                        await user.send(Type(text='mike'))
                        await press('password (hunter2)')
                        await user.send(Type(text='hunter2'))
                        await press('Sign in', 'button')
                        await user.wait_for_url(lambda url: urlsplit(url).path == '/shop')
                        await reads(('text', 'Signed in as mike.'))
                        await reads(('button', 'Add coffee'))
                        await reads(('text', 'In cart: empty'))  # one line, as a screen reader reads a paragraph
                        assert 'hunter2' not in str(user.outline)
            finally:
                await service.close_all()


async def wait_for_cart(service: StubBrowserService, item: str) -> None:
    text = ''
    for _ in range(50):
        text = (await service.snapshot(run_id=RUN, user_id=USER)).snapshot.text
        if f'In cart: {item}' in text:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f'the cart never showed {item}: {text}')


async def test_u6_press_and_hold_through_the_live_view(engine: str) -> None:
    """U6: the agent meets a press-and-hold check, the scripted user holds it over the WebSocket, and the agent gets
    through."""
    with serve_fixtures() as origin:
        async with backends(engine) as new:
            service = StubBrowserService(new)
            handoffs = InMemoryHandoffs()
            auth = StubAuthenticator()
            app = live_view_app(service=service, handoffs=handoffs, auth=auth)
            try:
                await service.start(run_id=RUN, user_id=USER)
                await service.act(run_id=RUN, user_id=USER, action=Navigate(url=f'{origin}/protected'))
                page = (await service.snapshot(run_id=RUN, user_id=USER)).snapshot
                assert 'Press and hold' in page.text
                handoff = await service.start_handoff(run_id=RUN, user_id=USER, reason='Please prove you are human')
                handoffs.add(handoff)
                async with serve_app(app) as base:
                    ws = f'{base.replace("http", "ws", 1)}/handoff/{handoff.handoff_id}/ws'
                    async with LiveViewClient.connect(ws, session=auth.sign_in(USER)) as user:
                        await user.wait_for_url(lambda url: url.endswith('/hold'))
                        await press_and_hold(
                            user,
                            at=Point(x=HOLD_CENTRE[0], y=HOLD_CENTRE[1]),
                            seconds=2,
                            done=lambda url: url.endswith('/protected'),
                        )
                        await user.give_back()
                page = (await service.snapshot(run_id=RUN, user_id=USER)).snapshot
                assert 'Protected content' in page.text
            finally:
                await service.close_all()
