"""The tests every `BrowserBackend` must pass. Needs pytest and anyio; import it from tests only.

Subclass it in a test module, under a name pytest collects, and say how to make the backend:

    class TestChromium(BrowserBackendConformance):
        @asynccontextmanager
        async def backend(self, site: Site) -> AsyncGenerator[BrowserBackend]:
            async with async_playwright() as playwright:
                yield ChromiumBackend(await playwright.chromium.launch())

`backend` yields a closed backend; the tests open it. For each feature in `not_supported`, the tests that use it check
that it raises `NotSupported` instead of doing it.

The tests drive a small fixture site served on 127.0.0.1 (`Site.origin`), also reachable as localhost
(`Site.other_origin`) for a second origin and a second cookie host. Its pages write what they see (cookies, storage,
clicks, keys, scrolling, mouse) into their visible text, and the tests read it back with `snapshot()`. So every test
of an action needs a working `snapshot()` that includes the page's visible text. `fake_site` is the same site as
`FakePage`s, for `FakeBrowser`.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from functools import cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from montybot.browser.contract import (
    Action,
    BrowserBackend,
    Click,
    Feature,
    LifecycleError,
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
from montybot.browser.fake import FakeBrowser, FakeElement, FakePage
from montybot.browser.state import BLANK_URL, BrowserState, Cookie, origin_of

# --- the fixture site ---


@dataclass(frozen=True, kw_only=True)
class Site:
    origin: str
    """`http://127.0.0.1:PORT`."""
    other_origin: str
    """`http://localhost:PORT`: the same server as another origin and another cookie host."""

    @property
    def home(self) -> str:
        return f'{self.origin}/'

    @property
    def probe(self) -> str:
        """Shows the cookies the server got and the cookies and storage its script saw at load."""
        return f'{self.origin}/probe'

    @property
    def actions(self) -> str:
        """A button, a text input, a box to hold, and a tall body, at fixed positions; shows what happened to them."""
        return f'{self.origin}/actions'


# Positions in CSS pixels, from the top-left of the page. The page has no margin, so before scrolling these are also
# viewport positions.
BUTTON_CENTRE = Point(x=150, y=120)
HOLD_START = Point(x=150, y=320)
HOLD_END = Point(x=170, y=330)

_HOME_HTML = '<!doctype html><title>Home</title><p>Conformance home</p>'

_PROBE_HTML = """<!doctype html><title>Probe</title>
<p>server saw: [%s]</p><p id="script"></p><p id="local"></p><p id="session"></p>
<script>
const names = (s) => s.split(';').map((c) => c.trim().split('=')[0]).filter(Boolean).sort().join(' ');
const items = (st) => Object.keys(st).sort().map((k) => k + '=' + st.getItem(k)).join(' ');
document.getElementById('script').textContent = 'script saw: [' + names(document.cookie) + ']';
document.getElementById('local').textContent = 'local at load: [' + items(localStorage) + ']';
document.getElementById('session').textContent = 'session at load: [' + items(sessionStorage) + ']';
</script>"""

_ACTIONS_HTML = """<!doctype html><title>Actions</title>
<style>
  body { margin: 0; height: 3000px; font: 16px sans-serif; }
  #button { position: absolute; left: 100px; top: 100px; width: 100px; height: 40px; }
  #input { position: absolute; left: 100px; top: 200px; width: 200px; height: 30px; }
  #hold { position: absolute; left: 100px; top: 300px; width: 200px; height: 60px; background: #ddd; }
  #log { position: fixed; left: 400px; top: 0; }
</style>
<button id="button">Press me</button>
<input id="input" value="old" aria-label="Input">
<div id="hold">Hold me</div>
<div id="log">
  <p id="click"></p><p id="typed"></p><p id="key"></p><p id="scroll"></p><p id="down"></p><p id="move"></p>
  <p id="up"></p>
</div>
<script>
const show = (id, text) => { document.getElementById(id).textContent = text; };
const at = (e) => Math.round(e.clientX) + ',' + Math.round(e.clientY);
const modifiers = ['Alt', 'Control', 'Meta', 'Shift'];
const input = document.getElementById('input');
document.getElementById('button').addEventListener('click', () => show('click', 'clicked: button'));
input.addEventListener('input', () => show('typed', 'typed: ' + input.value));
document.addEventListener('keydown', (e) => {
  if (modifiers.includes(e.key)) return;
  show('key', 'key: ' + [...modifiers.filter((m) => e.getModifierState(m)), e.key].join('+'));
});
addEventListener('scroll', () => show('scroll', 'scrolled: ' + Math.round(scrollX) + ',' + Math.round(scrollY)));
document.addEventListener('mousedown', (e) => show('down', 'down: ' + at(e)));
document.addEventListener('mousemove', (e) => { if (e.buttons & 1) show('move', 'held move: ' + at(e)); });
document.addEventListener('mouseup', (e) => show('up', 'up: ' + at(e)));
</script>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.partition('?')[0]
        if path == '/':
            body = _HOME_HTML
        elif path == '/probe':
            body = _PROBE_HTML % ' '.join(_cookie_names(self.headers.get('Cookie', '')))
        elif path == '/actions':
            body = _ACTIONS_HTML
        else:
            self.send_error(404)
            return
        data = body.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass


def _cookie_names(header: str) -> list[str]:
    return sorted(part.split('=')[0].strip() for part in header.split(';') if part.strip())


@cache
def serve_site() -> Site:
    """Start the fixture site once per process, on a free port, in a daemon thread."""
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    return Site(origin=f'http://127.0.0.1:{port}', other_origin=f'http://localhost:{port}')


def fake_site(site: Site) -> dict[str, FakePage]:
    """The fixture site as `FakePage`s, behaving as the served pages do."""
    shown: dict[str, str] = {}
    held = False
    scrolled = [0.0, 0.0]

    def probe_loaded(browser: FakeBrowser) -> None:
        sent = browser.cookies_for(browser.url)
        origin = origin_of(browser.url) or ''

        def items(storage: dict[str, dict[str, str]]) -> str:
            return ' '.join(f'{k}={v}' for k, v in sorted(storage.get(origin, {}).items()))

        browser.page.text = '\n'.join(
            [
                f'server saw: [{" ".join(sorted(c.name for c in sent))}]',
                f'script saw: [{" ".join(sorted(c.name for c in sent if not c.http_only))}]',
                f'local at load: [{items(browser.local_storage)}]',
                f'session at load: [{items(browser.session_storage)}]',
            ]
        )

    def actions_loaded(browser: FakeBrowser) -> None:
        nonlocal held
        shown.clear()
        held = False
        scrolled[:] = [0.0, 0.0]
        browser.page.text = 'Hold me'

    def on_action(browser: FakeBrowser, action: Action) -> None:
        nonlocal held
        match action:
            case Click(target=Point(x=x, y=y)):
                shown['down'] = f'down: {round(x)},{round(y)}'
                shown['up'] = f'up: {round(x)},{round(y)}'
                if 100 <= x <= 200 and 100 <= y <= 140:
                    shown['click'] = 'clicked: button'
            case Click(target=Selector() | Ref() as target):
                if browser.resolve(target).selector == '#button':
                    shown['click'] = 'clicked: button'
            case Click():
                pass
            case Type():
                field = browser.find('#input')
                if field is not None and browser.focused is field:
                    shown['typed'] = f'typed: {field.value}'
            case Press(key=key, modifiers=modifiers):
                held_keys = [m for m in ('Alt', 'Control', 'Meta', 'Shift') if m in modifiers]
                shown['key'] = 'key: ' + '+'.join([*held_keys, key])
            case Scroll(delta_x=dx, delta_y=dy):
                scrolled[:] = [max(0.0, scrolled[0] + dx), max(0.0, scrolled[1] + dy)]
                shown['scroll'] = f'scrolled: {round(scrolled[0])},{round(scrolled[1])}'
            case MouseDown(at=at):
                held = True
                shown['down'] = f'down: {round(at.x)},{round(at.y)}'
            case MouseMove(at=at):
                if held:
                    shown['move'] = f'held move: {round(at.x)},{round(at.y)}'
            case MouseUp(at=at):
                held = False
                shown['up'] = f'up: {round(at.x)},{round(at.y)}'
            case Navigate():
                pass
        browser.page.text = '\n'.join(['Hold me', *shown.values()])

    return {
        site.home: FakePage(title='Home', text='Conformance home'),
        f'{site.other_origin}/': FakePage(title='Home', text='Conformance home'),
        site.probe: FakePage(title='Probe', on_load=probe_loaded),
        site.actions: FakePage(
            title='Actions',
            elements=[
                FakeElement(selector='#button', role='button', name='Press me'),
                FakeElement(selector='#input', role='textbox', name='Input', value='old'),
            ],
            on_load=actions_loaded,
            on_action=on_action,
        ),
    }


def sample_state(site: Site, url: str) -> BrowserState:
    """A signed-in state: an HttpOnly session cookie, cookies on two hosts, and storage on two origins.

    Every cookie's host is also the host of an origin in the state, so a backend that can only add cookies for the
    current page (WebDriver) knows which origin to open for each.
    """
    in_thirty_days = float(int(time.time()) + 30 * 24 * 3600)
    return BrowserState(
        url=url,
        cookies=[
            Cookie(name='sid', value='session-1', domain='127.0.0.1', http_only=True),
            Cookie(name='theme', value='dark', domain='127.0.0.1', expires=in_thirty_days, same_site='Strict'),
            Cookie(name='other', value='b', domain='localhost'),
        ],
        local_storage={site.origin: {'cart': 'eggs'}, site.other_origin: {'lang': 'en'}},
        session_storage={site.origin: {'views': '2'}},
    )


def assert_same_state(actual: BrowserState, expected: BrowserState) -> None:
    """Equal, ignoring cookie order and fractions of a second in expiry times."""

    def cookies(state: BrowserState) -> list[Cookie]:
        normal = [replace(c, expires=float(int(c.expires))) for c in state.cookies]
        return sorted(normal, key=lambda c: (c.domain, c.path, c.name))

    assert actual.url == expected.url
    assert cookies(actual) == cookies(expected)
    assert actual.local_storage == expected.local_storage
    assert actual.session_storage == expected.session_storage


def ref_in(text: str, role: str, name: str) -> Ref:
    """The ref on the snapshot line `[ref] role "name"`, which every snapshot format prints this way (#13)."""
    found = re.findall(rf'^\s*\[([^\]]+)\] {re.escape(role)} "{re.escape(name)}"', text, re.MULTILINE)
    assert len(found) == 1, f'{len(found)} refs for {role} "{name}" in:\n{text}'
    return Ref(ref=found[0])


async def wait_for_text(browser: BrowserBackend, *expected: str, timeout: float = 5) -> str:
    """Poll `snapshot()` until its text contains every `expected` string, and return that text."""
    deadline = time.monotonic() + timeout
    while True:
        text = (await browser.snapshot()).text
        if all(e in text for e in expected):
            return text
        if time.monotonic() > deadline:
            missing = [e for e in expected if e not in text]
            raise AssertionError(f'snapshot never showed {missing}; last text:\n{text}')
        await asyncio.sleep(0.1)


# --- the tests ---


class BrowserBackendConformance(ABC):
    """The contract's behaviour, run against one backend. See the module docstring."""

    not_supported: ClassVar[frozenset[Feature]] = frozenset()
    """Features this backend raises `NotSupported` for. Its tests check that it does."""

    pytestmark = pytest.mark.anyio

    @pytest.fixture
    def anyio_backend(self) -> str:
        return 'asyncio'

    @pytest.fixture
    def site(self) -> Site:
        return serve_site()

    @abstractmethod
    def backend(self, site: Site) -> AbstractAsyncContextManager[BrowserBackend]:
        """A closed backend that can reach `site`. Clean up the engine on exit."""

    @asynccontextmanager
    async def opened(self, site: Site, state: BrowserState | None) -> AsyncGenerator[BrowserBackend]:
        async with self.backend(site) as browser:
            await browser.open(state)
            try:
                yield browser
            finally:
                await browser.close()

    def supports(self, *features: Feature) -> bool:
        return not set(features) & self.not_supported

    async def act_or_refuse(self, browser: BrowserBackend, action: Action) -> bool:
        """Perform `action` and return True, or check that it raises `NotSupported` as declared and return False."""
        if self.supports(*features_of(action)):
            await browser.act(action)
            return True
        with pytest.raises(NotSupported) as raised:
            await browser.act(action)
        assert raised.value.feature in self.not_supported
        return False

    # --- lifecycle ---

    async def test_open_empty(self, site: Site) -> None:
        async with self.opened(site, None) as browser:
            assert (await browser.snapshot()).url == BLANK_URL
            if self.supports('export'):
                assert_same_state(await browser.release(), BrowserState())

    async def test_lifecycle_errors(self, site: Site) -> None:
        async with self.backend(site) as browser:
            await browser.close()  # closing a closed backend is fine
            with pytest.raises(LifecycleError):
                await browser.snapshot()
            with pytest.raises(LifecycleError):
                await browser.act(Navigate(url=site.home))
            await browser.open(None)
            with pytest.raises(LifecycleError):
                await browser.open(None)
            await browser.close()
            await browser.close()
            with pytest.raises(LifecycleError):
                await browser.snapshot()

    # --- state in and out ---

    async def test_round_trip(self, site: Site) -> None:
        state = sample_state(site, site.home)
        async with self.opened(site, state) as browser:
            if not self.supports('export'):
                with pytest.raises(NotSupported):
                    await browser.release()
                return
            assert_same_state(await browser.release(), state)

    async def test_export_keeps_the_browser_open(self, site: Site) -> None:
        state = sample_state(site, site.home)
        async with self.opened(site, state) as browser:
            if not self.supports('export'):
                with pytest.raises(NotSupported):
                    await browser.export()
                return
            assert_same_state(await browser.export(), state)
            assert (await browser.snapshot()).url == site.home
            assert_same_state(await browser.export(), state)

    async def test_release_closes_and_the_state_reopens(self, site: Site) -> None:
        state = sample_state(site, site.home)
        async with self.opened(site, state) as browser:
            if not self.supports('export'):
                with pytest.raises(NotSupported):
                    await browser.release()
                assert (await browser.snapshot()).url == site.home  # a failed release leaves it open
                return
            released = await browser.release()
            with pytest.raises(LifecycleError):
                await browser.snapshot()
            await browser.open(released)
            assert (await browser.snapshot()).url == site.home
            assert_same_state(await browser.release(), state)

    async def test_open_seeds_state_before_page_scripts(self, site: Site) -> None:
        """The server gets the HttpOnly cookie, page scripts do not, and they see both kinds of storage at load."""
        async with self.opened(site, sample_state(site, site.probe)) as browser:
            await wait_for_text(
                browser,
                'server saw: [sid theme]',
                'script saw: [theme]',
                'local at load: [cart=eggs]',
                'session at load: [views=2]',
            )

    async def test_export_follows_navigation(self, site: Site) -> None:
        state = sample_state(site, site.home)
        async with self.opened(site, state) as browser:
            if not await self.act_or_refuse(browser, Navigate(url=site.actions)) or not self.supports('export'):
                return
            assert (await browser.release()).url == site.actions

    # --- actions ---

    async def test_navigate(self, site: Site) -> None:
        async with self.opened(site, None) as browser:
            if await self.act_or_refuse(browser, Navigate(url=site.actions)):
                snapshot = await browser.snapshot()
                assert (snapshot.url, snapshot.title) == (site.actions, 'Actions')

    async def test_click_selector(self, site: Site) -> None:
        async with self.opened(site, BrowserState(url=site.actions)) as browser:
            if await self.act_or_refuse(browser, Click(target=Selector(css='#button'))):
                await wait_for_text(browser, 'clicked: button')

    async def test_click_point(self, site: Site) -> None:
        async with self.opened(site, BrowserState(url=site.actions)) as browser:
            if await self.act_or_refuse(browser, Click(target=BUTTON_CENTRE)):
                await wait_for_text(browser, 'clicked: button')

    async def test_click_missing_element(self, site: Site) -> None:
        async with self.opened(site, BrowserState(url=site.actions)) as browser:
            action = Click(target=Selector(css='#missing'))
            if self.supports(*features_of(action)):
                with pytest.raises(TargetNotFound):
                    await browser.act(action)
            else:
                assert not await self.act_or_refuse(browser, action)

    async def test_type(self, site: Site) -> None:
        """With a target, typing replaces the value; without, it goes on at the focused element."""
        async with self.opened(site, BrowserState(url=site.actions)) as browser:
            if await self.act_or_refuse(browser, Type(text='hello', target=Selector(css='#input'))):
                await wait_for_text(browser, 'typed: hello')
                await browser.act(Type(text=' world'))
                await wait_for_text(browser, 'typed: hello world')

    async def test_click_ref(self, site: Site) -> None:
        async with self.opened(site, BrowserState(url=site.actions)) as browser:
            ref = ref_in((await browser.snapshot()).text, 'button', 'Press me')
            if await self.act_or_refuse(browser, Click(target=ref)):
                await wait_for_text(browser, 'clicked: button')

    async def test_type_ref(self, site: Site) -> None:
        async with self.opened(site, BrowserState(url=site.actions)) as browser:
            ref = ref_in((await browser.snapshot()).text, 'textbox', 'Input')
            if await self.act_or_refuse(browser, Type(text='hello', target=ref)):
                text = await wait_for_text(browser, 'typed: hello')
                assert 'value="hello"' in text

    async def test_ref_from_an_earlier_page(self, site: Site) -> None:
        """A ref never reaches an element on a page loaded after its snapshot."""
        async with self.opened(site, BrowserState(url=site.actions)) as browser:
            ref = ref_in((await browser.snapshot()).text, 'button', 'Press me')
            if not await self.act_or_refuse(browser, Navigate(url=site.actions)):
                return
            action = Click(target=ref)
            if not self.supports(*features_of(action)):
                assert not await self.act_or_refuse(browser, action)
                return
            with pytest.raises(TargetNotFound):
                await browser.act(action)

    async def test_press(self, site: Site) -> None:
        async with self.opened(site, BrowserState(url=site.actions)) as browser:
            if await self.act_or_refuse(browser, Press(key='Enter')):
                await wait_for_text(browser, 'key: Enter')
                await browser.act(Press(key='b', modifiers=('Control',)))
                await wait_for_text(browser, 'key: Control+b')

    async def test_scroll(self, site: Site) -> None:
        async with self.opened(site, BrowserState(url=site.actions)) as browser:
            if await self.act_or_refuse(browser, Scroll(delta_y=400)):
                await wait_for_text(browser, 'scrolled: 0,400')

    async def test_mouse_press_and_hold(self, site: Site) -> None:
        async with self.opened(site, BrowserState(url=site.actions)) as browser:
            if await self.act_or_refuse(browser, MouseDown(at=HOLD_START)):
                await browser.act(MouseMove(at=HOLD_END))
                await browser.act(MouseUp(at=HOLD_END))
                await wait_for_text(browser, 'down: 150,320', 'held move: 170,330', 'up: 170,330')

    async def test_screenshot(self, site: Site) -> None:
        async with self.opened(site, BrowserState(url=site.actions)) as browser:
            if not self.supports('screenshot'):
                with pytest.raises(NotSupported):
                    await browser.screenshot()
                return
            shot = await browser.screenshot()
            assert shot.png.startswith(b'\x89PNG\r\n\x1a\n')
            assert shot.width > 0 and shot.height > 0
