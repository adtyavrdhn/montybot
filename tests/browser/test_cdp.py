"""`ChromiumCDPBackend`: the conformance suite and its own behaviour, against a real Chrome over our CDP pipe.

Needs Chrome at `montybot.browser.cdp.default_executable()` (Playwright's Chromium, or `MONTYBOT_CHROME_BINARY`); the
tests that start Chrome are skipped without it. The jailed tests also need Linux with bwrap, socat and Xvfb.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import threading
import time
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from operator import attrgetter
from pathlib import Path
from typing import ClassVar

import pytest

from montybot.browser.cdp import CDPOptions, ChromiumCDPBackend, default_executable
from montybot.browser.conformance import (
    BUTTON_CENTRE,
    HOLD_START,
    BrowserBackendConformance,
    Site,
    serve_site,
    wait_for_text,
)
from montybot.browser.contract import (
    ActionFailed,
    BrowserBackend,
    Click,
    MouseDown,
    Navigate,
    Selector,
    TargetNotFound,
    Type,
)
from montybot.browser.live import Frame, OutlineSource, Tabs, Viewport
from montybot.browser.state import BrowserState, Cookie

pytestmark = pytest.mark.anyio

needs_chrome = pytest.mark.skipif(not default_executable().exists(), reason=f'no Chrome at {default_executable()}')
needs_jail = pytest.mark.skipif(
    sys.platform != 'linux' or not all(shutil.which(tool) for tool in ('bwrap', 'socat', 'Xvfb')),
    reason='the jail needs Linux with bwrap, socat and Xvfb (CI, or the server)',
)
HEADLESS = CDPOptions(headless=True)


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@needs_chrome
class TestChromiumCDP(BrowserBackendConformance):
    """Everything, export included: no `not_supported`."""

    @asynccontextmanager
    async def backend(self, site: Site) -> AsyncGenerator[BrowserBackend]:
        browser = ChromiumCDPBackend(HEADLESS)
        try:
            yield browser
        finally:
            await browser.close()


@needs_chrome
@needs_jail
class TestJailedChromiumCDP(BrowserBackendConformance):
    """The server setup: headed on its own Xvfb screen, inside bwrap, every request through a private `EgressProxy`.
    The fixture site is on loopback, so this proxy allows private addresses; the test below checks the default."""

    @asynccontextmanager
    async def backend(self, site: Site) -> AsyncGenerator[BrowserBackend]:
        browser = ChromiumCDPBackend(CDPOptions(bwrap=True, virtual_screen=True, allow_private_networks=True))
        try:
            yield browser
        finally:
            await browser.close()


def listening_ports() -> set[int]:
    """The TCP ports something listens on in this network namespace (Linux)."""
    ports: set[int] = set()
    for table in ('/proc/net/tcp', '/proc/net/tcp6'):
        for line in Path(table).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == '0A':
                ports.add(int(fields[1].rsplit(':', 1)[1], 16))
    return ports


@needs_chrome
@needs_jail
async def test_jailed_chrome_reaches_no_private_address_and_no_host_port(site: Site) -> None:
    before = listening_ports()
    async with chrome(CDPOptions.server()) as browser:
        await browser.open(None)
        assert browser.pid is not None
        argv = Path(f'/proc/{browser.pid}/cmdline').read_bytes().split(b'\0')
        assert b'--remote-debugging-pipe' in argv and not any(b'--remote-debugging-port' in arg for arg in argv)
        assert listening_ports() <= before  # socat listens inside the jail's own network namespace
        with pytest.raises(ActionFailed, match='could not load'):
            await browser.act(Navigate(url=f'{site.origin}/probe'))
        page = await browser.snapshot()  # Chrome's error page keeps the URL it was asked for
        assert page.url == f'{site.origin}/probe' and 'server saw' not in page.text
        for address in ('http://10.128.0.2/', 'http://169.254.169.254/'):
            with pytest.raises(ActionFailed, match='could not load'):
                await browser.act(Navigate(url=address))


# --- its own behaviour ---


@pytest.fixture
def site() -> Site:
    return serve_site()


@asynccontextmanager
async def chrome(options: CDPOptions = HEADLESS) -> AsyncGenerator[ChromiumCDPBackend]:
    browser = ChromiumCDPBackend(options)
    try:
        yield browser
    finally:
        await browser.close()


_TELLS = """<!doctype html><title>Tells</title><p id=out></p><script>
let runtime = false;
const probe = new Error();
Object.defineProperty(probe, 'stack', {get() { runtime = true; return ''; }});
console.debug(probe);
setTimeout(() => {
  const globals = Object.keys(window).filter((k) => /^(cdc_|__playwright|__pw|__montybot)/.test(k));
  out.textContent = ['webdriver=' + navigator.webdriver, 'runtime=' + runtime, 'globals=' + globals.length,
    'refs=' + (typeof window.__montybotRefs)].join(' ');
}, 200);
</script>"""


@needs_chrome
async def test_the_page_sees_no_automation() -> None:
    """`navigator.webdriver` is false, the Runtime domain is off (a console message's stack is never read), and the
    walker's globals live in our isolated world, not the page's."""
    async with chrome() as browser:
        await browser.open(BrowserState(url='data:text/html,' + _TELLS.replace('#', '%23')))
        await browser.snapshot()
        await browser.act(Navigate(url='data:text/html,' + _TELLS.replace('#', '%23')))
        await browser.snapshot()
        await wait_for_text(browser, 'webdriver=false runtime=false globals=0 refs=undefined')


@needs_chrome
async def test_domain_cookies_and_the_bare_host(site: Site) -> None:
    """A `.shop.test` cookie reaches `www.shop.test`; the host-only HttpOnly one only `shop.test`, never scripts; and
    export gives back both as they were."""
    port = site.origin.rsplit(':', 1)[1]
    rules = '--host-resolver-rules=MAP shop.test 127.0.0.1, MAP *.shop.test 127.0.0.1'
    state = BrowserState(
        url=f'http://www.shop.test:{port}/probe',
        cookies=[
            Cookie(name='wide', value='1', domain='.shop.test'),
            Cookie(name='host', value='2', domain='shop.test', http_only=True),
        ],
    )
    async with chrome(CDPOptions(headless=True, extra_args=(rules,))) as browser:
        await browser.open(state)
        await wait_for_text(browser, 'server saw: [wide]', 'script saw: [wide]')
        await browser.act(Navigate(url=f'http://shop.test:{port}/probe'))
        await wait_for_text(browser, 'server saw: [host wide]', 'script saw: [wide]')
        key = attrgetter('domain', 'path', 'name')
        assert sorted((await browser.export()).cookies, key=key) == sorted(state.cookies, key=key)


@needs_chrome
async def test_a_failed_load_raises_and_leaves_the_browser_open(site: Site) -> None:
    unreachable = 'http://127.0.0.1:1/'  # nothing listens there
    async with chrome() as browser:
        with pytest.raises(ActionFailed, match='could not load'):
            await browser.open(BrowserState(url=unreachable))
        assert (await browser.snapshot()).url == unreachable
        await browser.act(Navigate(url=site.home))
        with pytest.raises(ActionFailed, match='could not load'):
            await browser.act(Navigate(url=unreachable))
        await browser.act(Navigate(url=site.home))
        assert (await browser.snapshot()).title == 'Home'


@needs_chrome
async def test_selectors_that_match_nothing_or_cannot_type(site: Site) -> None:
    async with chrome() as browser:
        await browser.open(BrowserState(url=site.actions))
        with pytest.raises(TargetNotFound, match='not a valid CSS selector'):
            await browser.act(Click(target=Selector(css='#[')))
        with pytest.raises(ActionFailed, match='cannot take typing'):
            await browser.act(Type(text='x', target=Selector(css='#button')))


async def process_list() -> str:
    """Every process's command line."""
    ps = await asyncio.create_subprocess_exec('ps', '-Aww', '-o', 'command=', stdout=asyncio.subprocess.PIPE)
    out, _ = await ps.communicate()
    return out.decode(errors='replace')


@needs_chrome
async def test_close_stops_every_process_and_removes_the_profile() -> None:
    browser = ChromiumCDPBackend(HEADLESS)
    await browser.open(None)
    workdir = browser.workdir
    assert workdir is not None and workdir.exists()
    await browser.close()
    assert browser.workdir is None and not workdir.exists()
    deadline = time.monotonic() + 5
    while str(workdir) in await process_list():
        assert time.monotonic() < deadline, 'a Chrome process outlived close()'
        await asyncio.sleep(0.1)
    await browser.close()


class _Shop(BaseHTTPRequestHandler):
    """Counts its requests, and serves `/invoice.csv` as a download."""

    requests: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        self.requests.append(self.path)
        if self.path == '/invoice.csv':
            body, kind = b'item,total\neggs,3\n', 'text/csv'
            extra = [('Content-Disposition', 'attachment; filename="invoice.csv"')]
        else:
            body, kind, extra = b'<!doctype html><title>Shop</title><a href="/invoice.csv">Invoice</a>', 'text/html', []
        self.send_response(200)
        for name, value in [('Content-Type', kind), ('Content-Length', str(len(body))), *extra]:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def shop() -> Iterator[str]:
    _Shop.requests = []
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Shop)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f'http://127.0.0.1:{server.server_address[1]}'
    finally:
        server.shutdown()
        server.server_close()


@needs_chrome
async def test_storage_of_other_origins_is_reached_without_a_request() -> None:
    """localStorage for an origin the tab is not on goes in and out through a hidden tab whose requests we answer."""
    with shop() as origin:
        state = BrowserState(local_storage={origin: {'cart': 'eggs'}})
        async with chrome() as browser:
            await browser.open(state)
            exported = await browser.export()
        assert exported.local_storage == state.local_storage
        assert _Shop.requests == []


@needs_chrome
async def test_downloads_from_a_link_and_from_navigate() -> None:
    with shop() as origin:
        async with chrome() as browser:
            await browser.open(BrowserState(url=f'{origin}/'))
            await browser.act(Click(target=Selector(css='a')))
            await browser.act(Navigate(url=f'{origin}/invoice.csv'))
            downloads = await browser.take_downloads()
            assert [(d.name, d.data) for d in downloads] == [('invoice.csv', b'item,total\neggs,3\n')] * 2
            assert await browser.take_downloads() == []
            assert (await browser.snapshot()).url == f'{origin}/'


async def first_frame(updates: AsyncIterator[Frame | Tabs], width: int | None = None) -> Frame:
    async def find() -> Frame:
        async for update in updates:
            if isinstance(update, Frame) and (width is None or update.width == width):
                return update
        raise AssertionError('the live view ended')

    return await asyncio.wait_for(find(), 10)


@needs_chrome
async def test_live_view(site: Site) -> None:
    """Frames come, input reaches the page, a phone size applies until close, and a held button is let go."""
    async with chrome() as browser:
        await browser.open(BrowserState(url=site.actions))
        source = await browser.live_view()
        try:
            updates = source.updates()
            frame = await first_frame(updates)
            assert frame.mime == 'image/jpeg' and frame.width > 0
            await source.send(Click(target=BUTTON_CENTRE))
            await wait_for_text(browser, 'clicked: button')
            assert isinstance(source, OutlineSource)
            assert any(item.name == 'Press me' for item in (await source.outline()).items)
            await source.send(MouseDown(at=HOLD_START))
            await wait_for_text(browser, 'down: 150,320')
        finally:
            await source.close()
        await wait_for_text(browser, 'up: 150,320')
        phone = await browser.live_view()
        try:
            await phone.set_viewport(Viewport(width=390, height=700))
            await first_frame(phone.updates(), 390)
        finally:
            await phone.close()
        shot = await browser.screenshot()
        assert shot.width == frame.width  # its own size again


# --- without Chrome ---


def test_command_line() -> None:
    options = CDPOptions(executable=Path('/opt/chrome/chrome'), extra_args=('--lang=en-US',))
    argv = options.command(profile=Path('/tmp/profile'))
    assert argv[:3] == ['/opt/chrome/chrome', '--user-data-dir=/tmp/profile', '--remote-debugging-pipe']
    assert '--disable-blink-features=AutomationControlled' in argv
    assert '--window-size=1280,800' in argv
    assert argv[-2:] == ['--lang=en-US', 'about:blank']
    for tell in ('--enable-automation', '--headless', '--remote-debugging-port', '--no-sandbox'):
        assert not any(arg.startswith(tell) for arg in argv)
    assert '--headless' in CDPOptions(executable=Path('/c'), headless=True).command(profile=Path('/p'))


def test_jailed_command() -> None:
    """The Linux server's jail: no network but the egress proxy, and the CDP pipe passed through on fds 3 and 4."""
    profile = Path('/tmp/profile')
    options = CDPOptions(executable=Path('/opt/chrome/chrome'), bwrap=True, virtual_screen=True)
    argv = options.command(profile=profile, proxy=profile.parent / 'egress.sock')
    chrome_at = argv.index('/opt/chrome/chrome')
    sandbox, browser = argv[:chrome_at], argv[chrome_at:]
    assert sandbox[0] == 'bwrap'
    for flag in ('--unshare-user', '--unshare-pid', '--unshare-net', '--die-with-parent', '--new-session'):
        assert flag in sandbox
    joined = ' '.join(sandbox)
    assert '--bind /tmp/profile /tmp/profile' in joined and '--ro-bind /opt/chrome /opt/chrome' in joined
    script = sandbox[-1]
    assert 'UNIX-CONNECT:/tmp/egress.sock' in script and 'TCP-LISTEN:1080,bind=127.0.0.1' in script
    assert '3>&- 4>&-' in script  # socat does not hold the CDP pipe
    assert browser[1:3] == ['--user-data-dir=/tmp/profile', '--remote-debugging-pipe']
    assert '--proxy-server=socks5://127.0.0.1:1080' in browser and '--proxy-bypass-list=<-loopback>' in browser
    with pytest.raises(ValueError, match='egress proxy'):
        options.command(profile=profile)


def test_server_options() -> None:
    options = CDPOptions.server(egress_socket=Path('/run/egress/proxy.sock'))
    assert (options.bwrap, options.virtual_screen, options.headless) == (True, True, False)
    assert options.egress_socket == Path('/run/egress/proxy.sock')


async def test_a_shared_proxy_cannot_allow_private_networks(tmp_path: Path) -> None:
    options = CDPOptions(
        executable=tmp_path / 'chrome',
        bwrap=True,
        bwrap_path='sh',
        egress_socket=tmp_path / 's',
        allow_private_networks=True,
    )
    with pytest.raises(ActionFailed, match='cannot allow private networks'):
        await ChromiumCDPBackend(options).open(None)
