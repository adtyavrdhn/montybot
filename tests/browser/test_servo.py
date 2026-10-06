"""The Servo backend (#12): the conformance suite and Servo's own behaviour, against a real servoshell.

Needs servoshell at `montybot.browser.servo.default_binary()` (set `MONTYBOT_SERVO_BINARY` to move it); the tests that
start Servo are skipped without it. Each test starts its own Servo on a free port and kills it at the end.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from operator import attrgetter
from pathlib import Path

import pytest

from montybot.browser.conformance import BrowserBackendConformance, Site, serve_site, wait_for_text
from montybot.browser.contract import ActionFailed, BrowserBackend, Navigate, NotSupported
from montybot.browser.servo import CHROME_USER_AGENT, ServoBackend, ServoOptions, default_binary
from montybot.browser.servo_bwrap import BwrapLauncher
from montybot.browser.state import BrowserState, Cookie

pytestmark = pytest.mark.anyio

needs_servo = pytest.mark.skipif(not default_binary().exists(), reason=f'no servoshell at {default_binary()}')


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


def test_bwrap_launch_command() -> None:
    """Not run on Linux here: this only checks the command line that `BwrapLauncher` builds."""
    options = ServoOptions(binary=Path('/opt/servo/servoshell'), prefs=(), user_agent=None, launcher=BwrapLauncher())
    argv = options.command(port=4444, config_dir=Path('/tmp/profile'))
    servo_at = argv.index('/opt/servo/servoshell')
    network, sandbox, servo = argv[: argv.index('bwrap')], argv[argv.index('bwrap') : servo_at], argv[servo_at:]
    assert network[0] == 'pasta' and network[-1] == '--'
    assert network[network.index('-t') + 1] == '127.0.0.1/4444'
    assert '--no-map-gw' in network
    assert sandbox[-1] == '--'
    for flag in ('--unshare-all', '--share-net', '--die-with-parent', '--new-session'):
        assert flag in sandbox
    joined = ' '.join(sandbox)
    assert '--bind /tmp/profile /tmp/profile' in joined
    assert '--ro-bind /opt/servo /opt/servo' in joined
    assert '--ro-bind-try /usr /usr' in joined
    assert servo[1:4] == ['--headless', '--webdriver=4444', '--config-dir=/tmp/profile']

    without_pasta = ServoOptions(binary=Path('/opt/servo/servoshell'), launcher=BwrapLauncher(pasta=None))
    assert without_pasta.command(port=4444, config_dir=Path('/tmp/profile'))[0] == 'bwrap'
