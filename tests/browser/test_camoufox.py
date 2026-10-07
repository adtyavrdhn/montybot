"""The Camoufox backend (#18): the conformance suite and Camoufox's own behaviour, against a real Camoufox.

Needs Camoufox at `montybot.browser.camoufox.default_binary()` (set `MONTYBOT_CAMOUFOX_BINARY` to move it); the tests
that start it are skipped without it. Each test starts its own Camoufox on a free port and kills it at the end.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import threading
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from montybot.browser.camoufox import (
    BASE_PREFS,
    PROXY_PREFS,
    CamoufoxBackend,
    CamoufoxOptions,
    default_binary,
    fingerprint,
)
from montybot.browser.conformance import BrowserBackendConformance, Site, serve_site
from montybot.browser.contract import ActionFailed, BrowserBackend, Click, Navigate, Press, Selector, Type
from montybot.browser.state import BrowserState, Cookie

pytestmark = pytest.mark.anyio

needs_camoufox = pytest.mark.skipif(not default_binary().exists(), reason=f'no Camoufox at {default_binary()}')
needs_jail = pytest.mark.skipif(
    sys.platform != 'linux' or shutil.which('bwrap') is None or shutil.which('socat') is None,
    reason='the jail needs Linux with bwrap and socat (tests/linux/run.sh)',
)
needs_screen = pytest.mark.skipif(
    sys.platform != 'linux' or shutil.which('Xvfb') is None, reason='a virtual screen needs Linux with Xvfb'
)


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@needs_camoufox
class TestCamoufox(BrowserBackendConformance):
    """Headless, on this machine's network."""

    @asynccontextmanager
    async def backend(self, site: Site) -> AsyncGenerator[BrowserBackend]:
        browser = CamoufoxBackend(CamoufoxOptions(headless=True))
        try:
            yield browser
        finally:
            await browser.close()


@needs_camoufox
@needs_jail
@needs_screen
class TestJailedCamoufox(BrowserBackendConformance):
    """The server setup: headed in Xvfb, inside bwrap, every request through a private `EgressProxy`. The fixture site
    is on loopback, so this proxy allows private addresses; the test below checks the default."""

    @asynccontextmanager
    async def backend(self, site: Site) -> AsyncGenerator[BrowserBackend]:
        browser = CamoufoxBackend(CamoufoxOptions.server(allow_private_networks=True))
        try:
            yield browser
        finally:
            await browser.close()


@needs_camoufox
@needs_jail
@needs_screen
async def test_jailed_camoufox_reaches_no_private_address_and_no_host_port(site: Site) -> None:
    async with camoufox(CamoufoxOptions.server()) as browser:
        await browser.open(None)
        assert browser.pid is not None
        argv = Path(f'/proc/{browser.pid}/cmdline').read_bytes().split(b'\0')
        (port,) = [int(arg.split(b'=')[1]) for arg in argv if arg.startswith(b'--remote-debugging-port=')]
        with socket.socket() as probe:
            assert probe.connect_ex(('127.0.0.1', port)) != 0  # BiDi is only on the socket in the profile
        with pytest.raises(ActionFailed, match='could not load'):
            await browser.act(Navigate(url=f'{site.origin}/probe'))
        page = await browser.snapshot()  # Firefox's error page keeps the URL it was asked for
        assert page.url == f'{site.origin}/probe' and 'server saw' not in page.text


# --- Camoufox's own behaviour ---


@pytest.fixture
def site() -> Site:
    return serve_site()


REPORT = b'a,b\n1,2\n'
_PAGES = {
    '/files': '<title>Files</title><a id=get href=/report>Get the report</a>',
    '/search': '<title>Search</title><form action=/found><input name=q aria-label=Search autofocus></form>',
    '/found': '<title>Found</title><p>found</p>',
}


class _FilesHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.partition('?')[0]
        if path == '/report':
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv')
            self.send_header('Content-Disposition', 'attachment; filename="report.csv"')
            body = REPORT
        elif path in _PAGES:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            body = _PAGES[path].encode()
        else:
            self.send_error(404)
            return
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(scope='module')
def files_site() -> Iterator[str]:
    """A download and a search form, on 127.0.0.1."""
    server = ThreadingHTTPServer(('127.0.0.1', 0), _FilesHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_address[1]}'
    server.shutdown()


@asynccontextmanager
async def camoufox(options: CamoufoxOptions | None = None) -> AsyncGenerator[CamoufoxBackend]:
    browser = CamoufoxBackend(options or CamoufoxOptions(headless=True))
    try:
        yield browser
    finally:
        await browser.close()


@needs_camoufox
async def test_the_fingerprint_is_worn_and_the_browser_is_not_flagged() -> None:
    """The pinned identity's user agent and platform, en-US whatever the host's locale, and no automation flag."""
    page = (
        'data:text/html,<title>Identity</title><p id=f></p><script>f.textContent = JSON.stringify([navigator.userAgent,'
        ' navigator.platform, navigator.languages, navigator.webdriver, typeof RTCPeerConnection])</script>'
    )
    expected = fingerprint(CamoufoxOptions().identity)
    async with camoufox() as browser:
        await browser.open(BrowserState(url=page))
        text = (await browser.snapshot()).text
    agent, platform, languages, webdriver, rtc = json.loads(text)
    assert (agent, platform) == (expected['navigator.userAgent'], expected['navigator.platform'])
    assert 'Camoufox' not in agent
    assert languages == ['en-US', 'en'] and webdriver is False and rtc == 'function'


@needs_camoufox
async def test_domain_cookies_round_trip(site: Site) -> None:
    """A `.shop.test` cookie and a host-only one go in without any page and come back as they were."""
    state = BrowserState(
        url=site.home,
        cookies=[
            Cookie(name='wide', value='1', domain='.shop.test'),
            Cookie(name='host', value='2', domain='shop.test', http_only=True, secure=True, same_site='None'),
        ],
    )
    async with camoufox() as browser:
        await browser.open(state)
        exported = await browser.export()
    assert sorted(exported.cookies, key=lambda c: c.name) == sorted(state.cookies, key=lambda c: c.name)


@needs_camoufox
async def test_a_failed_load_raises_and_leaves_the_browser_open(site: Site) -> None:
    unreachable = 'http://127.0.0.1:9/'  # the discard port: refused before any request, like a dead site
    async with camoufox() as browser:
        with pytest.raises(ActionFailed, match='could not load'):
            await browser.open(BrowserState(url=unreachable))
        await browser.act(Navigate(url=site.home))
        with pytest.raises(ActionFailed, match='could not load'):
            await browser.act(Navigate(url=unreachable))
        assert (await browser.snapshot()).url == unreachable
        await browser.act(Navigate(url=site.home))
        assert (await browser.snapshot()).title == 'Home'


@needs_camoufox
async def test_a_download_is_kept(files_site: str) -> None:
    async with camoufox() as browser:
        await browser.open(BrowserState(url=f'{files_site}/files'))
        await browser.act(Click(target=Selector(css='#get')))
        downloads = await browser.take_downloads()
        assert [(d.name, d.data) for d in downloads] == [('report.csv', REPORT)]
        assert await browser.take_downloads() == []


@needs_camoufox
async def test_close_kills_the_process_and_removes_the_profile() -> None:
    browser = CamoufoxBackend(CamoufoxOptions(headless=True))
    await browser.open(None)
    pid, workdir = browser.pid, browser.workdir
    assert pid is not None and workdir is not None and workdir.exists()
    await browser.close()
    assert browser.pid is None
    assert not workdir.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    await browser.close()


@needs_camoufox
async def test_autofocus_then_enter_submits_the_form(files_site: str) -> None:
    """What Servo could not do: type into the autofocused field, then Enter submits the form (implicit submission)."""
    async with camoufox() as browser:
        await browser.open(BrowserState(url=f'{files_site}/search'))
        await browser.act(Type(text='eggs'))
        await browser.act(Press(key='Enter'))
        page = await browser.snapshot()
        assert (page.url, page.title) == (f'{files_site}/found?q=eggs', 'Found')


# --- without Camoufox ---


def test_command_line() -> None:
    options = CamoufoxOptions(binary=Path('/opt/camoufox/camoufox'), headless=True)
    argv = options.command(port=4444, profile=Path('/tmp/profile'))
    assert argv == [
        '/opt/camoufox/camoufox',
        '--no-remote',
        '--profile',
        '/tmp/profile',
        '--remote-debugging-port=4444',
        '--headless',
        '--window-size=1280,800',
        'about:blank',
    ]


def test_jailed_command() -> None:
    """The Linux server's jail: no network but the egress proxy, and BiDi only on a socket in the profile."""
    profile = Path('/tmp/profile')
    options = CamoufoxOptions.server(
        binary=Path('/opt/camoufox/camoufox'), identity='windows', font_cache=Path('/var/cache/fonts')
    )
    argv = options.command(port=4444, profile=profile, proxy=profile / 'egress.sock')
    camoufox_at = argv.index('/opt/camoufox/camoufox')
    sandbox, browser = argv[:camoufox_at], argv[camoufox_at:]
    assert sandbox[0] == 'bwrap'
    for flag in (
        '--unshare-user',
        '--unshare-pid',
        '--unshare-net',
        '--die-with-parent',
        '--new-session',
        '--clearenv',
    ):
        assert flag in sandbox
    joined = ' '.join(sandbox)
    assert '--bind /tmp/profile /tmp/profile' in joined
    assert '--ro-bind /opt/camoufox /opt/camoufox' in joined
    script = sandbox[-1]
    assert 'UNIX-LISTEN:/tmp/profile/bidi.sock,mode=600' in script and 'TCP:127.0.0.1:4444' in script
    assert 'UNIX-CONNECT:/tmp/profile/egress.sock' in script and 'TCP-LISTEN:1080,bind=127.0.0.1' in script
    config = json.loads(
        ''.join(
            sandbox[i + 2]
            for i, a in enumerate(sandbox)
            if a == '--setenv' and sandbox[i + 1].startswith('CAMOU_CONFIG_')
        )
    )
    assert config['navigator.platform'] == 'Win32' and (config['window.outerWidth'], config['window.outerHeight']) == (
        1280,
        800,
    )
    if sys.platform != 'darwin':
        assert '--setenv FONTCONFIG_FILE /tmp/profile/fonts.conf' in joined
        assert '--ro-bind /var/cache/fonts /var/cache/fonts' in joined
    assert browser[1:5] == ['--no-remote', '--profile', '/tmp/profile', '--remote-debugging-port=4444']
    assert '--headless' not in browser  # headed, in its own Xvfb screen
    with pytest.raises(ValueError, match='egress proxy'):
        options.command(port=4444, profile=profile)


def test_prefs_send_everything_through_the_proxy_only_in_the_jail() -> None:
    assert CamoufoxOptions().user_prefs() == BASE_PREFS
    jailed = CamoufoxOptions(bwrap=True).user_prefs()
    assert jailed['network.proxy.type'] == 1 and jailed['network.proxy.socks_port'] == 1080
    assert jailed['network.proxy.no_proxies_on'] == '' and jailed['network.proxy.allow_hijacking_localhost'] is True
    assert jailed['network.proxy.socks_remote_dns'] is True and jailed['network.proxy.failover_direct'] is False
    assert PROXY_PREFS.items() <= jailed.items()
    assert jailed['media.peerconnection.ice.proxy_only'] is True


def test_the_config_is_split_for_the_environment() -> None:
    options = CamoufoxOptions(identity='macos', config={'timezone': 'America/Chicago'})
    env = options.environment(profile=Path('/tmp/profile'))
    pieces = sorted((k for k in env if k.startswith('CAMOU_CONFIG_')), key=lambda k: int(k.rsplit('_', 1)[1]))
    assert len(pieces) == 2  # the Mac identity's font and voice lists are over one piece
    config = json.loads(''.join(env[k] for k in pieces))
    assert config['timezone'] == 'America/Chicago' and config['navigator.platform'] == 'MacIntel'
