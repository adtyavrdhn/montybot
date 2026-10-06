"""`ChromiumBackend` against real Chrome: the contract's conformance tests, headless and headed, and a sign-in."""

from __future__ import annotations

import secrets
import shutil
import socket
import sys
import threading
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from playwright.async_api import async_playwright

from montybot.browser.chromium import ChromiumBackend, ChromiumOptions
from montybot.browser.conformance import BrowserBackendConformance, Site
from montybot.browser.contract import (
    ActionFailed,
    BrowserBackend,
    Click,
    Navigate,
    Press,
    Ref,
    Selector,
    TargetNotFound,
    Type,
)
from montybot.browser.state import BrowserState

pytestmark = pytest.mark.anyio

HEADLESS = ChromiumOptions(headless=True)
HEADED = ChromiumOptions.for_this_machine()
"""A window on a Mac or a Linux desktop; on Linux without a desktop, Xvfb and bwrap."""


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


def can_run(options: ChromiumOptions) -> str | None:
    """Why `options` cannot run on this machine, or None."""
    if options.bwrap and sys.platform != 'linux':
        return 'bwrap needs Linux'
    for needed, tool in ((options.bwrap, options.bwrap_path), (options.virtual_screen, options.xvfb_path)):
        if needed and shutil.which(tool) is None:
            return f'{tool} is not installed'
    return None


@asynccontextmanager
async def chromium(options: ChromiumOptions) -> AsyncGenerator[ChromiumBackend]:
    if reason := can_run(options):
        pytest.skip(reason)
    async with async_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).exists():
            pytest.skip('Playwright Chromium is not installed: uv run playwright install chromium')
        backend = ChromiumBackend(playwright=playwright, options=options)
        try:
            yield backend
        finally:
            await backend.close()


class TestChromiumHeadless(BrowserBackendConformance):
    @asynccontextmanager
    async def backend(self, site: Site) -> AsyncGenerator[BrowserBackend]:
        async with chromium(HEADLESS) as backend:
            yield backend


class TestChromiumHeaded(BrowserBackendConformance):
    @asynccontextmanager
    async def backend(self, site: Site) -> AsyncGenerator[BrowserBackend]:
        async with chromium(HEADED) as backend:
            yield backend


# --- a site with a real sign-in: the server sets the HttpOnly cookie, not the test ---

_sessions: dict[str, str] = {}

_LOGIN = """<!doctype html><title>Sign in</title>
<form method="post" action="/login">
  <input id="username" name="username" aria-label="Username">
  <input id="password" name="password" type="password" aria-label="Password">
  <button id="submit">Sign in</button>
</form>"""

_ACCOUNT = """<!doctype html><title>Account</title><p>Signed in as %s</p><p id="views"></p>
<script>
const views = Number(sessionStorage.getItem('views') || 0) + 1;
sessionStorage.setItem('views', String(views));
document.getElementById('views').textContent = 'views: ' + views;
</script>"""

_WHOAMI = """<!doctype html><title>Who am I</title><p id="out"></p>
<script>document.getElementById('out').textContent =
  'webdriver: ' + navigator.webdriver + ' agent: ' + navigator.userAgent;</script>"""


class _ShopHandler(BaseHTTPRequestHandler):
    def _send(self, body: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        sid = SimpleCookie(self.headers.get('Cookie', '')).get('sid')
        user = _sessions.get(sid.value) if sid else None
        if self.path == '/login':
            self._send(_LOGIN)
        elif self.path == '/account':
            self._send(_ACCOUNT % user if user else '<!doctype html><title>Account</title><p>Signed out</p>')
        elif self.path == '/whoami':
            self._send(_WHOAMI)
        else:
            self._send('not found', 404)

    def do_POST(self) -> None:
        form = parse_qs(self.rfile.read(int(self.headers.get('Content-Length', 0))).decode())
        if form.get('password') == ['hunter2']:
            sid = secrets.token_urlsafe(16)
            _sessions[sid] = form.get('username', ['?'])[0]
            cookie = f'sid={sid}; HttpOnly; Path=/; SameSite=Lax'
            self._send('', 303, {'Location': '/account', 'Set-Cookie': cookie})
        else:
            self._send('wrong password', 401)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(scope='module')
def shop() -> Iterator[str]:
    server = ThreadingHTTPServer(('127.0.0.1', 0), _ShopHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_address[1]}'
    server.shutdown()
    server.server_close()


async def test_sign_in_exports_the_servers_http_only_cookie(shop: str) -> None:
    async with chromium(HEADLESS) as browser:
        await browser.open(BrowserState(url=f'{shop}/login'))
        await browser.act(Type(text='mike', target=Selector(css='#username')))
        await browser.act(Type(text='hunter2', target=Selector(css='#password')))
        assert 'hunter2' not in (await browser.snapshot()).text
        await browser.act(Press(key='Enter'))  # submits the form; act waits for the next page
        snapshot = await browser.snapshot()
        assert (snapshot.url, snapshot.title) == (f'{shop}/account', 'Account')
        assert 'Signed in as mike' in snapshot.text and 'views: 1' in snapshot.text

        state = await browser.release()
        assert [(c.name, c.http_only) for c in state.cookies] == [('sid', True)]
        assert state.session_storage == {shop: {'views': '1'}}

        await browser.open(state)  # a new Chrome, with a new profile
        assert 'Signed in as mike' in (text := (await browser.snapshot()).text) and 'views: 2' in text
        await browser.close()

        await browser.open(None)  # nothing survives close() but the exported state
        await browser.act(Navigate(url=f'{shop}/account'))
        assert 'Signed out' in (await browser.snapshot()).text


async def test_click_waits_for_the_page_it_opens(shop: str) -> None:
    async with chromium(HEADLESS) as browser:
        await browser.open(BrowserState(url=f'{shop}/login'))
        await browser.act(Type(text='ada', target=Selector(css='#username')))
        await browser.act(Type(text='hunter2', target=Selector(css='#password')))
        await browser.act(Click(target=Selector(css='#submit')))
        assert 'Signed in as ada' in (await browser.snapshot()).text


async def test_close_deletes_the_profile(shop: str) -> None:
    async with chromium(HEADLESS) as browser:
        await browser.open(None)
        workdir = browser.workdir
        assert workdir is not None and (workdir / 'profile').is_dir()
        await browser.close()
        assert browser.workdir is None and not workdir.exists()


async def test_unreachable_pages_raise_action_failed_and_stay_open(shop: str) -> None:
    with socket.socket() as sock:  # a port nothing listens on
        sock.bind(('127.0.0.1', 0))
        dead = f'http://127.0.0.1:{sock.getsockname()[1]}/'
    async with chromium(HEADLESS) as browser:
        with pytest.raises(ActionFailed, match='ERR_CONNECTION_REFUSED'):
            await browser.open(BrowserState(url=dead))
        assert (await browser.export()).url == dead  # the address bar's URL, not Chrome's error page
        with pytest.raises(ActionFailed, match='ERR_CONNECTION_REFUSED'):
            await browser.act(Navigate(url=dead))
        await browser.act(Navigate(url=f'{shop}/login'))
        assert (await browser.snapshot()).title == 'Sign in'


async def test_refs_and_bad_selectors_are_not_found(shop: str) -> None:
    async with chromium(HEADLESS) as browser:
        await browser.open(BrowserState(url=f'{shop}/login'))
        with pytest.raises(TargetNotFound, match='no snapshot of this page yet'):
            await browser.act(Click(target=Ref(ref='1')))
        with pytest.raises(TargetNotFound, match='not a valid CSS selector'):
            await browser.act(Click(target=Selector(css='##bad')))


async def test_headed_chrome_does_not_say_it_is_automated(shop: str) -> None:
    async with chromium(HEADED) as browser:
        await browser.open(BrowserState(url=f'{shop}/whoami'))
        text = (await browser.snapshot()).text
        assert 'webdriver: false' in text
        assert 'Headless' not in text
