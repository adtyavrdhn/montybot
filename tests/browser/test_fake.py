from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest

from sammy.browser.conformance import BrowserBackendConformance, Site, fake_site
from sammy.browser.contract import (
    Action,
    BrowserBackend,
    Click,
    MouseDown,
    MouseMove,
    MouseUp,
    Navigate,
    NotSupported,
    Point,
    Press,
    Ref,
    Scroll,
    Selector,
    TargetNotFound,
    Type,
    features_of,
)
from sammy.browser.fake import FakeBrowser, FakeElement, FakePage
from sammy.browser.state import BrowserState, Cookie

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


class TestFakeBrowser(BrowserBackendConformance):
    @asynccontextmanager
    async def backend(self, site: Site) -> AsyncGenerator[BrowserBackend]:
        yield FakeBrowser(pages=fake_site(site))


class TestLimitedFakeBrowser(BrowserBackendConformance):
    """A fake standing in for a weaker engine, which proves the suite's `NotSupported` checks."""

    not_supported = frozenset({'export', 'point', 'scroll', 'mouse', 'screenshot'})

    @asynccontextmanager
    async def backend(self, site: Site) -> AsyncGenerator[BrowserBackend]:
        yield FakeBrowser(pages=fake_site(site), not_supported=self.not_supported)


# --- what only the fake does ---

SHOP = 'http://shop.test'


def shop() -> FakeBrowser:
    def sign_in(browser: FakeBrowser, action: Action) -> None:
        if isinstance(action, Click) and action.target in (Selector(css='#submit'), Ref(ref='3')):
            browser.cookies.append(Cookie(name='sid', value='s3cret', domain='shop.test', http_only=True))
            browser.load('/account')

    return FakeBrowser(
        pages={
            f'{SHOP}/login': FakePage(
                title='Sign in',
                text='Sign in to continue',
                elements=[
                    FakeElement(selector='#user', role='textbox', name='Username'),
                    FakeElement(selector='#password', role='textbox', name='Password', secret=True),
                    FakeElement(selector='#submit', role='button', name='Sign in'),
                    FakeElement(selector='#help', role='link', name='Help', href='/help'),
                ],
                on_action=sign_in,
            ),
            f'{SHOP}/account': FakePage(title='Your account', text='Signed in'),
            f'{SHOP}/help': FakePage(title='Help'),
        }
    )


async def test_refs_come_from_the_latest_snapshot() -> None:
    browser = shop()
    await browser.open(BrowserState(url=f'{SHOP}/login'))
    with pytest.raises(TargetNotFound, match='no snapshot'):
        await browser.act(Click(target=Ref(ref='1')))
    snapshot = await browser.snapshot()
    assert snapshot.text == (
        'Sign in to continue\n[1] textbox "Username"\n[2] textbox "Password"\n[3] button "Sign in"\n[4] link "Help"'
    )
    await browser.act(Type(text='mike', target=Ref(ref='1')))
    await browser.act(Type(text='hunter2', target=Ref(ref='2')))
    assert (await browser.snapshot()).text.splitlines()[1:3] == [
        '[1] textbox "Username" value="mike"',
        '[2] textbox "Password" value="***"',
    ]
    with pytest.raises(TargetNotFound, match='not a ref'):
        await browser.act(Click(target=Ref(ref='9')))
    await browser.act(Click(target=Ref(ref='3')))
    assert (browser.url, browser.page.title) == (f'{SHOP}/account', 'Your account')
    with pytest.raises(TargetNotFound, match='no snapshot'):
        await browser.act(Click(target=Ref(ref='3')))


async def test_hooks_set_http_only_cookies_and_links_navigate() -> None:
    browser = shop()
    await browser.open(BrowserState(url=f'{SHOP}/login'))
    await browser.act(Click(target=Selector(css='#help')))
    assert browser.url == f'{SHOP}/help'
    await browser.act(Navigate(url=f'{SHOP}/login'))
    await browser.act(Click(target=Selector(css='#submit')))
    state = await browser.release()
    assert state.url == f'{SHOP}/account'
    assert state.cookies == [Cookie(name='sid', value='s3cret', domain='shop.test', http_only=True)]
    assert len(browser.actions) == 3


async def test_unknown_urls_load_a_not_found_page() -> None:
    browser = shop()
    await browser.open(None)
    await browser.act(Navigate(url=f'{SHOP}/nope'))
    snapshot = await browser.snapshot()
    assert (snapshot.url, snapshot.title, snapshot.text) == (f'{SHOP}/nope', 'Not found', 'Not found')


async def test_cookies_for_matches_like_a_server() -> None:
    browser = FakeBrowser()
    await browser.open(
        BrowserState(
            cookies=[
                Cookie(name='host', value='1', domain='a.test'),
                Cookie(name='wide', value='1', domain='.a.test'),
                Cookie(name='sub', value='1', domain='b.a.test'),
                Cookie(name='path', value='1', domain='a.test', path='/cart'),
                Cookie(name='secure', value='1', domain='a.test', secure=True),
            ]
        )
    )
    assert [c.name for c in browser.cookies_for('http://a.test/')] == ['host', 'wide']
    assert [c.name for c in browser.cookies_for('https://b.a.test/x')] == ['wide', 'sub']
    assert [c.name for c in browser.cookies_for('https://a.test/cart/1')] == ['host', 'wide', 'path', 'secure']
    assert [c.name for c in browser.cookies_for('http://a.test/cartoon')] == ['host', 'wide']


async def test_not_supported_names_the_feature() -> None:
    browser = FakeBrowser(not_supported={'ref'}, engine='weak')
    await browser.open(None)
    with pytest.raises(NotSupported, match='weak does not support ref') as raised:
        await browser.act(Click(target=Ref(ref='1')))
    assert raised.value.feature == 'ref'
    assert browser.actions == []


@pytest.mark.parametrize(
    ('action', 'features'),
    [
        (Navigate(url='http://a.test/'), ('navigate',)),
        (Click(target=Selector(css='#a')), ('click', 'selector')),
        (Click(target=Point(x=1, y=2)), ('click', 'point')),
        (Type(text='x'), ('type',)),
        (Type(text='x', target=Ref(ref='1')), ('type', 'ref')),
        (Press(key='Enter'), ('press',)),
        (Scroll(delta_y=1), ('scroll',)),
        (MouseDown(at=Point(x=1, y=2)), ('mouse',)),
        (MouseMove(at=Point(x=1, y=2)), ('mouse',)),
        (MouseUp(at=Point(x=1, y=2)), ('mouse',)),
    ],
)
def test_features_of(action: Action, features: tuple[str, ...]) -> None:
    assert features_of(action) == features


def test_state_json_round_trip() -> None:
    state = BrowserState(
        url='https://a.test/',
        cookies=[Cookie(name='sid', value='1', domain='a.test', http_only=True, same_site='Strict', expires=2e9)],
        local_storage={'https://a.test': {'cart': 'eggs'}},
        session_storage={'https://a.test': {'views': '2'}},
    )
    assert BrowserState.from_json(state.to_json()) == state
