"""The Lightpanda backend (#18): the conformance suite and Lightpanda's own behaviour, against a real Lightpanda.

Needs the Lightpanda binary at `montybot.browser.lightpanda.default_binary()` (set `MONTYBOT_LIGHTPANDA_BINARY` to move
it); the tests that start Lightpanda are skipped without it. Each test starts its own Lightpanda and kills it at the
end.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from montybot.browser.conformance import BrowserBackendConformance, Site, serve_site, wait_for_text
from montybot.browser.contract import ActionFailed, BrowserBackend, Click, Feature, Navigate, NotSupported, Point
from montybot.browser.lightpanda import LightpandaBackend, LightpandaOptions, default_binary
from montybot.browser.state import BrowserState, Cookie

pytestmark = pytest.mark.anyio

needs_lightpanda = pytest.mark.skipif(not default_binary().exists(), reason=f'no Lightpanda at {default_binary()}')
needs_jail = pytest.mark.skipif(
    sys.platform != 'linux' or shutil.which('bwrap') is None or shutil.which('socat') is None,
    reason='the jail needs Linux with bwrap and socat (tests/linux/run.sh)',
)

NOT_SUPPORTED: frozenset[Feature] = frozenset({'screenshot', 'point', 'mouse', 'scroll'})
"""Lightpanda has no layout engine: nothing to draw, and no real place for a point, the mouse or a scroll."""


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@needs_lightpanda
class TestLightpanda(BrowserBackendConformance):
    not_supported = NOT_SUPPORTED

    @asynccontextmanager
    async def backend(self, site: Site) -> AsyncGenerator[BrowserBackend]:
        browser = LightpandaBackend()
        try:
            yield browser
        finally:
            await browser.close()


@needs_lightpanda
@needs_jail
class TestJailedLightpanda(BrowserBackendConformance):
    """The server setup: the same contract from inside bwrap, every request through a private `EgressProxy`. The
    fixture site is on loopback, so this proxy allows private addresses; the test below checks the default."""

    not_supported = NOT_SUPPORTED

    @asynccontextmanager
    async def backend(self, site: Site) -> AsyncGenerator[BrowserBackend]:
        browser = LightpandaBackend(LightpandaOptions(bwrap=True, allow_private_networks=True))
        try:
            yield browser
        finally:
            await browser.close()


@needs_lightpanda
@needs_jail
async def test_jailed_lightpanda_reaches_no_private_address_and_no_host_port(site: Site) -> None:
    async with lightpanda(LightpandaOptions(bwrap=True)) as browser:
        await browser.open(None)
        assert browser.pid is not None
        argv = Path(f'/proc/{browser.pid}/cmdline').read_bytes().split(b'\0')
        port = int(argv[argv.index(b'--port') + 1])
        with socket.socket() as probe:
            assert probe.connect_ex(('127.0.0.1', port)) != 0  # CDP is only on the socket in the profile
        for url in (f'{site.origin}/probe', 'http://10.0.0.1/', 'http://169.254.169.254/'):
            with pytest.raises(ActionFailed, match='could not load'):
                await browser.act(Navigate(url=url))
        assert 'server saw' not in (await browser.snapshot()).text


# --- Lightpanda's own behaviour ---


@pytest.fixture
def site() -> Site:
    return serve_site()


@asynccontextmanager
async def lightpanda(options: LightpandaOptions | None = None) -> AsyncGenerator[LightpandaBackend]:
    browser = LightpandaBackend(options)
    try:
        yield browser
    finally:
        await browser.close()


@needs_lightpanda
async def test_export_reads_every_origin_without_touching_the_page(site: Site) -> None:
    """Other origins' localStorage comes from a hidden iframe on an intercepted page: the site gets no request, and
    the iframe is gone afterwards."""
    state = BrowserState(
        url=f'{site.origin}/probe',
        cookies=[Cookie(name='sid', value='s', domain='127.0.0.1', http_only=True)],
        local_storage={site.origin: {'cart': 'eggs'}, site.other_origin: {'lang': 'en'}},
    )
    async with lightpanda() as browser:
        await browser.open(state)
        await wait_for_text(browser, 'server saw: [sid]', 'local at load: [cart=eggs]')
        exported = await browser.export()
        assert exported.local_storage == state.local_storage
        assert [c.name for c in exported.cookies if c.http_only] == ['sid']
        assert await browser._evaluate("document.querySelectorAll('iframe').length") == 0
        assert (await browser.snapshot()).url == state.url


@needs_lightpanda
async def test_a_failed_load_raises_and_leaves_the_browser_open(site: Site) -> None:
    unreachable = 'http://127.0.0.1:9/'  # the discard port: refused before any request, like a dead site
    async with lightpanda() as browser:
        with pytest.raises(ActionFailed, match='could not load'):
            await browser.open(BrowserState(url=unreachable))
        await browser.act(Navigate(url=site.home))
        with pytest.raises(ActionFailed, match='could not load'):
            await browser.act(Navigate(url=unreachable))
        await browser.act(Navigate(url=site.home))
        assert (await browser.snapshot()).title == 'Home'


@needs_lightpanda
async def test_no_pixels() -> None:
    async with lightpanda() as browser:
        await browser.open(None)
        with pytest.raises(NotSupported, match='draws the text only'):
            await browser.screenshot()
        with pytest.raises(NotSupported, match='no layout engine'):
            await browser.act(Click(target=Point(x=1, y=1)))


@needs_lightpanda
async def test_viewport_and_user_agent() -> None:
    async with lightpanda(LightpandaOptions(window_size=(1024, 740))) as browser:
        await browser.open(None)
        assert await browser._evaluate('[innerWidth, innerHeight]') == [1024, 740]
        assert str(await browser._evaluate('navigator.userAgent')).startswith('Lightpanda/')


@needs_lightpanda
async def test_close_kills_the_process_and_removes_the_profile() -> None:
    browser = LightpandaBackend()
    await browser.open(None)
    pid = browser.pid
    profile = browser._profile
    assert pid is not None and profile is not None and profile.exists()
    await browser.close()
    assert browser.pid is None
    assert not profile.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    await browser.close()


# --- without Lightpanda ---


def test_command_line() -> None:
    options = LightpandaOptions(binary=Path('/opt/lightpanda/lightpanda'))
    argv = options.command(port=9222, profile=Path('/tmp/profile'))
    assert argv == [
        '/opt/lightpanda/lightpanda',
        'serve',
        *('--host', '127.0.0.1'),
        *('--port', '9222'),
        '--disable-metrics',
        *('--log-level', 'error'),
        *('--load-resources', 'stylesheet'),
        *('--load-resources', 'iframe'),
    ]


def test_jailed_command() -> None:
    """The Linux server's jail: no network but the egress proxy (names resolved there), CDP only on a socket in the
    profile, and no telemetry."""
    profile = Path('/tmp/profile')
    options = LightpandaOptions(binary=Path('/opt/lightpanda/lightpanda'), bwrap=True, load_resources=())
    argv = options.command(port=9222, profile=profile, proxy=profile / 'egress.sock')
    at = argv.index('/opt/lightpanda/lightpanda')
    sandbox, browser = argv[:at], argv[at:]
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
    assert '--ro-bind /opt/lightpanda /opt/lightpanda' in joined
    assert '--setenv LIGHTPANDA_DISABLE_TELEMETRY true' in joined
    script = sandbox[-1]
    assert 'UNIX-LISTEN:/tmp/profile/cdp.sock,mode=600' in script and 'TCP:127.0.0.1:9222' in script
    assert 'UNIX-CONNECT:/tmp/profile/egress.sock' in script and 'TCP-LISTEN:1080,bind=127.0.0.1' in script
    assert browser[1:6] == ['serve', '--host', '127.0.0.1', '--port', '9222']
    assert browser[-2:] == ['--http-proxy', 'socks5h://127.0.0.1:1080']
    with pytest.raises(ValueError, match='egress proxy'):
        options.command(port=9222, profile=profile)


def test_a_shared_proxy_is_bound_by_its_folder() -> None:
    """A shared egress proxy may restart, so its folder is mounted rather than the socket itself."""
    shared = Path('/run/egress/proxy.sock')
    options = LightpandaOptions(binary=Path('/opt/lightpanda/lightpanda'), bwrap=True, egress_socket=shared)
    joined = ' '.join(options.command(port=9222, profile=Path('/tmp/profile'), proxy=shared))
    assert '--ro-bind /run/egress /run/egress' in joined
