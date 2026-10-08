"""The Servo backend (#12): the conformance suite and Servo's own behaviour, against a real servoshell.

Needs servoshell at `sammy.browser.servo.default_binary()` (set `SAMMY_SERVO_BINARY` to move it); the tests that
start Servo are skipped without it. Each test starts its own Servo on a free port and kills it at the end.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from operator import attrgetter
from pathlib import Path

import pytest

from sammy.browser.chromium_linux import bwrap_command
from sammy.browser.conformance import BrowserBackendConformance, Site, serve_site, wait_for_text
from sammy.browser.contract import ActionFailed, BrowserBackend, Navigate, NotSupported
from sammy.browser.servo import CHROME_USER_AGENT, ServoBackend, ServoOptions, default_binary
from sammy.browser.state import BrowserState, Cookie

pytestmark = pytest.mark.anyio

needs_servo = pytest.mark.skipif(not default_binary().exists(), reason=f'no servoshell at {default_binary()}')
needs_jail = pytest.mark.skipif(
    sys.platform != 'linux' or shutil.which('bwrap') is None or shutil.which('socat') is None,
    reason='the jail needs Linux with bwrap and socat (tests/linux/run.sh)',
)


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@needs_servo
class TestServo(BrowserBackendConformance):
    """Stock Servo 0.7.0 cannot read HttpOnly cookies back, so it refuses `export`."""

    not_supported = frozenset({'export'})

    @asynccontextmanager
    async def backend(self, site: Site) -> AsyncGenerator[BrowserBackend]:
        browser = ServoBackend()
        try:
            yield browser
        finally:
            await browser.close()


@needs_servo
@needs_jail
class TestJailedServo(BrowserBackendConformance):
    """The server setup: the same contract from inside bwrap, every request through a private `EgressProxy`. The
    fixture site is on loopback, so this proxy allows private addresses; the test below checks the default."""

    not_supported = frozenset({'export'})

    @asynccontextmanager
    async def backend(self, site: Site) -> AsyncGenerator[BrowserBackend]:
        browser = ServoBackend(ServoOptions(bwrap=True, allow_private_networks=True))
        try:
            yield browser
        finally:
            await browser.close()


@needs_servo
@needs_jail
async def test_jailed_servo_reaches_no_private_address_and_no_host_port(site: Site) -> None:
    async with servo(ServoOptions(bwrap=True)) as browser:
        await browser.open(None)
        assert browser.pid is not None
        argv = Path(f'/proc/{browser.pid}/cmdline').read_bytes().split(b'\0')
        (port,) = [int(arg.split(b'=')[1]) for arg in argv if arg.startswith(b'--webdriver=')]
        with socket.socket() as probe:
            assert probe.connect_ex(('127.0.0.1', port)) != 0  # WebDriver is only on the socket in the profile
        with pytest.raises(ActionFailed):
            await browser.act(Navigate(url=f'{site.origin}/probe'))
        page = await browser.snapshot()  # Servo's error page keeps the URL it was asked for
        assert page.title == 'Error loading page' and 'server saw' not in page.text


# --- Servo's own behaviour ---


@pytest.fixture
def site() -> Site:
    return serve_site()


@pytest.fixture
def host_file(tmp_path: Path) -> Path:
    """`shop.test` and `www.shop.test` on this machine, for domain cookies (an IP address cannot have them)."""
    path = tmp_path / 'hosts'
    path.write_text('127.0.0.1 shop.test www.shop.test\n')
    return path


@asynccontextmanager
async def servo(options: ServoOptions | None = None) -> AsyncGenerator[ServoBackend]:
    browser = ServoBackend(options)
    try:
        yield browser
    finally:
        await browser.close()


def port_of(site: Site) -> str:
    return site.origin.rsplit(':', 1)[1]


@needs_servo
async def test_cookies_are_seeded_from_the_bare_domain(site: Site, host_file: Path) -> None:
    """Add Cookie only takes cookies for the current host, so a `.shop.test` cookie is added on `shop.test`, and it
    still reaches `www.shop.test`. The host-only HttpOnly one reaches only `shop.test`, and never page scripts."""
    port = port_of(site)
    state = BrowserState(
        url=f'http://www.shop.test:{port}/probe',
        cookies=[
            Cookie(name='wide', value='1', domain='.shop.test'),
            Cookie(name='host', value='2', domain='shop.test', http_only=True),
        ],
    )
    async with servo(ServoOptions(host_file=host_file)) as browser:
        await browser.open(state)
        await wait_for_text(browser, 'server saw: [wide]', 'script saw: [wide]')
        await browser.act(Navigate(url=f'http://shop.test:{port}/probe'))
        await wait_for_text(browser, 'server saw: [host wide]', 'script saw: [wide]')


@needs_servo
async def test_export_with_the_patched_flag_reads_every_host(site: Site, host_file: Path) -> None:
    """With `http_only_export`, export reads every host's cookies and every origin's storage from a side tab.

    Stock Servo hides HttpOnly cookies, so this state has none; it checks the rest of the export path.
    """
    port = port_of(site)
    origin, other = site.origin, site.other_origin
    in_a_day = float(int(time.time()) + 24 * 3600)
    state = BrowserState(
        url=f'{origin}/probe',
        cookies=[
            Cookie(name='theme', value='dark', domain='127.0.0.1', expires=in_a_day, same_site='Strict'),
            Cookie(name='other', value='b', domain='localhost'),
            Cookie(name='wide', value='w', domain='.shop.test'),
            Cookie(name='host', value='h', domain='shop.test', path='/cart', secure=True),
        ],
        local_storage={origin: {'cart': 'eggs'}, other: {'lang': 'en'}, f'http://shop.test:{port}': {'k': 'v'}},
        session_storage={origin: {'views': '2'}},
    )
    async with servo(ServoOptions(host_file=host_file, http_only_export=True)) as browser:
        await browser.open(state)
        await wait_for_text(browser, 'server saw: [theme]', 'session at load: [views=2]')
        exported = await browser.export()
        assert exported.url == state.url
        key = attrgetter('domain', 'path', 'name')
        assert sorted(exported.cookies, key=key) == sorted(state.cookies, key=key)
        assert exported.local_storage == state.local_storage
        assert exported.session_storage == state.session_storage
        # the user's tab is untouched: same page, same sessionStorage, no extra tab left behind
        assert (await browser.snapshot()).url == state.url
        assert await browser._call('GET', '/window/handles') == [await browser._call('GET', '/window')]
        assert await browser._script('return sessionStorage.getItem("views")') == '2'


@needs_servo
async def test_the_patched_flag_on_a_stock_servo_still_refuses_export(site: Site) -> None:
    """A seeded HttpOnly cookie that cannot be read back means this servoshell is not patched."""
    state = BrowserState(url=site.home, cookies=[Cookie(name='sid', value='s', domain='127.0.0.1', http_only=True)])
    async with servo(ServoOptions(http_only_export=True)) as browser:
        await browser.open(state)
        with pytest.raises(NotSupported, match='HttpOnly'):
            await browser.export()


@needs_servo
async def test_site_prefs_and_chrome_user_agent() -> None:
    page = (
        'data:text/html,<title>Features</title><p id=f></p><script>f.textContent = [typeof IntersectionObserver,'
        ' typeof ResizeObserver, "adoptedStyleSheets" in document, typeof crypto.subtle, typeof FontFace,'
        ' CSS.supports("container-type", "inline-size"), navigator.userAgent].join(" | ")</script>'
    )
    async with servo() as browser:
        await browser.open(BrowserState(url=page))
        text = (await browser.snapshot()).text
    assert text == f'function | function | true | object | function | true | {CHROME_USER_AGENT}'


@needs_servo
async def test_a_failed_load_raises_and_leaves_the_browser_open(site: Site) -> None:
    unreachable = 'http://127.0.0.1:9/'  # the discard port: refused before any request, like a dead site
    async with servo() as browser:
        with pytest.raises(ActionFailed, match='could not load'):
            await browser.open(BrowserState(url=unreachable))
        await browser.act(Navigate(url=site.home))
        with pytest.raises(ActionFailed, match='could not load'):
            await browser.act(Navigate(url=unreachable))
        await browser.act(Navigate(url=site.home))
        assert (await browser.snapshot()).title == 'Home'


@needs_servo
async def test_close_kills_the_process_and_removes_the_profile() -> None:
    browser = ServoBackend()
    await browser.open(None)
    pid = browser.pid
    config_dir = browser._config_dir
    assert pid is not None and config_dir is not None and config_dir.exists()
    await browser.close()
    assert browser.pid is None
    assert not config_dir.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    await browser.close()


# --- without Servo ---


def test_command_line() -> None:
    options = ServoOptions(binary=Path('/opt/servo/servoshell'), prefs=('a', 'b=false'), user_agent='UA')
    argv = options.command(port=4444, config_dir=Path('/tmp/profile'))
    assert argv == [
        '/opt/servo/servoshell',
        '--headless',
        '--webdriver=4444',
        '--config-dir=/tmp/profile',
        '--window-size=1024x740',
        '--pref=a',
        '--pref=b=false',
        '--user-agent=UA',
        'about:blank',
    ]


def test_jailed_command() -> None:
    """The Linux server's jail: no network but the egress proxy, and WebDriver only on a socket in the profile."""
    profile = Path('/tmp/profile')
    options = ServoOptions(binary=Path('/opt/servo/servoshell'), prefs=(), user_agent=None, bwrap=True)
    argv = options.command(port=4444, config_dir=profile, proxy=profile / 'egress.sock')
    servo_at = argv.index('/opt/servo/servoshell')
    sandbox, servo = argv[:servo_at], argv[servo_at:]
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
    assert '--ro-bind /opt/servo /opt/servo' in joined
    script = sandbox[-1]
    assert 'UNIX-LISTEN:/tmp/profile/webdriver.sock,mode=600' in script and 'TCP:127.0.0.1:4444' in script
    assert 'UNIX-CONNECT:/tmp/profile/egress.sock' in script and 'TCP-LISTEN:1080,bind=127.0.0.1' in script
    assert servo[1:4] == ['--headless', '--webdriver=4444', '--config-dir=/tmp/profile']
    assert '--pref=network_http_proxy_uri=http://127.0.0.1:1080' in servo
    assert '--pref=network_https_proxy_uri=http://127.0.0.1:1080' in servo
    assert servo[-1] == 'about:blank'
    with pytest.raises(ValueError, match='egress proxy'):
        options.command(port=4444, config_dir=profile)


def test_expose_needs_a_network_namespace() -> None:
    """A port inside a jail that shares the host's network is already on the host: refuse rather than mislead."""
    with pytest.raises(ValueError, match='needs a proxy'):
        bwrap_command(
            chrome=Path('/opt/servo/servoshell'),
            profile=Path('/tmp/profile'),
            display=None,
            expose=(4444, Path('/tmp/profile/webdriver.sock')),
        )
