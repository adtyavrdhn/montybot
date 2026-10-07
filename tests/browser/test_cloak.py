"""CloakBrowser under `ChromiumCDPBackend` (`montybot/browser/cloak.py`): the conformance suite, the jail, and the device
it presents.

Needs the binary at `$MONTYBOT_CLOAK_BINARY` (`tests/linux/fetch_cloak.sh` downloads it); the tests that start it are
skipped without it. The jailed tests also need Linux with bwrap, socat and Xvfb.
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from test_cdp import listening_ports

from montybot.browser.cdp import CDPOptions, ChromiumCDPBackend
from montybot.browser.cloak import Fingerprint, cloak_executable, fingerprint_seed, with_cloak
from montybot.browser.conformance import BrowserBackendConformance, Site, serve_site, wait_for_text
from montybot.browser.contract import ActionFailed, BrowserBackend, Navigate

pytestmark = pytest.mark.anyio

BINARY = cloak_executable()
needs_cloak = pytest.mark.skipif(
    BINARY is None or not BINARY.exists(), reason='no CloakBrowser: set MONTYBOT_CLOAK_BINARY'
)
needs_jail = pytest.mark.skipif(
    sys.platform != 'linux' or not all(shutil.which(tool) for tool in ('bwrap', 'socat', 'Xvfb')),
    reason='the jail needs Linux with bwrap, socat and Xvfb (CI, or the server)',
)
DEVICE = Fingerprint(seed=41_235, platform='macos' if sys.platform == 'darwin' else 'windows')


def cloak(options: CDPOptions) -> ChromiumCDPBackend:
    return ChromiumCDPBackend(with_cloak(options, fingerprint=DEVICE))


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
def site() -> Site:
    return serve_site()


@needs_cloak
class TestCloak(BrowserBackendConformance):
    """Headless and unjailed, as the end-to-end tests run it. Everything, export included."""

    @asynccontextmanager
    async def backend(self, site: Site) -> AsyncGenerator[BrowserBackend]:
        browser = cloak(CDPOptions(headless=True))
        try:
            yield browser
        finally:
            await browser.close()


@needs_cloak
@needs_jail
class TestJailedCloak(BrowserBackendConformance):
    """The server setup: headed on its own Xvfb screen, inside bwrap, every request through a private `EgressProxy`
    that allows the loopback fixture site."""

    @asynccontextmanager
    async def backend(self, site: Site) -> AsyncGenerator[BrowserBackend]:
        browser = cloak(CDPOptions(bwrap=True, virtual_screen=True, allow_private_networks=True))
        try:
            yield browser
        finally:
            await browser.close()


@needs_cloak
@needs_jail
async def test_jailed_cloak_reaches_no_private_address_and_no_host_port(site: Site) -> None:
    before = listening_ports()
    browser = cloak(CDPOptions.server())
    try:
        await browser.open(None)
        assert browser.pid is not None
        argv = Path(f'/proc/{browser.pid}/cmdline').read_bytes().split(b'\0')
        assert b'--remote-debugging-pipe' in argv and not any(b'--remote-debugging-port' in arg for arg in argv)
        assert f'--fingerprint={DEVICE.seed}'.encode() in argv
        assert listening_ports() <= before
        for address in (f'{site.origin}/probe', 'http://10.128.0.2/', 'http://169.254.169.254/'):
            with pytest.raises(ActionFailed, match='could not load'):
                await browser.act(Navigate(url=address))
        assert 'server saw' not in (await browser.snapshot()).text
    finally:
        await browser.close()


_DEVICE_PAGE = """<!doctype html><title>Device</title><p id=out></p><script>
const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
out.textContent = JSON.stringify({webdriver: navigator.webdriver, languages: navigator.languages, zone,
  headless: /Headless/.test(navigator.userAgent), platform: navigator.platform});
</script>"""


async def device(options: CDPOptions) -> dict[str, object]:
    """What a page reads about the browser: `navigator`, the time zone."""
    browser = cloak(options)
    try:
        await browser.open(None)
        await browser.act(Navigate(url='data:text/html,' + _DEVICE_PAGE))
        text = await wait_for_text(browser, '"zone"')
        return json.loads(text[text.index('{') : text.rindex('}') + 1])
    finally:
        await browser.close()


@needs_cloak
@pytest.mark.parametrize(
    'options',
    [
        pytest.param(CDPOptions(headless=True), id='headless'),
        pytest.param(
            CDPOptions(bwrap=True, virtual_screen=True, allow_private_networks=True), id='jailed', marks=needs_jail
        ),
    ],
)
async def test_the_page_sees_the_device_we_chose(options: CDPOptions) -> None:
    """No automation, en-US (not the jail's `C` locale), our time zone, and the platform of the fingerprint."""
    seen = await device(options)
    assert seen['webdriver'] is False and seen['headless'] is False
    assert seen['languages'] == ['en-US', 'en'] and seen['zone'] == 'America/Chicago'
    assert seen['platform'] == ('MacIntel' if DEVICE.platform == 'macos' else 'Win32')


# --- without the binary ---


@contextmanager
def binary_env(value: str | None) -> Iterator[None]:
    with pytest.MonkeyPatch.context() as patch:
        if value is None:
            patch.delenv('MONTYBOT_CLOAK_BINARY', raising=False)
        else:
            patch.setenv('MONTYBOT_CLOAK_BINARY', value)
        yield


def test_command_line() -> None:
    """CloakBrowser's flags after the backend's own, SwiftShader off, and still no automation flags."""
    base = CDPOptions(executable=Path('/opt/chrome/chrome'), extra_args=('--x=1',))
    with binary_env('/opt/cloak/chrome'):
        options = with_cloak(base, fingerprint=Fingerprint(seed=12_345, platform='windows'))
    argv = options.command(profile=Path('/tmp/profile'))
    assert argv[:3] == ['/opt/cloak/chrome', '--user-data-dir=/tmp/profile', '--remote-debugging-pipe']
    cloak_args = [
        '--fingerprint=12345',
        '--fingerprint-platform=windows',
        '--fingerprint-timezone=America/Chicago',
        '--lang=en-US',
        '--accept-lang=en-US,en',
        '--ignore-gpu-blocklist',
    ]
    assert argv[-len(cloak_args) - 2 :] == [*cloak_args, '--x=1', 'about:blank']
    assert '--disable-blink-features=AutomationControlled' in argv
    for tell in ('--enable-unsafe-swiftshader', '--enable-automation', '--headless', '--no-sandbox', '--user-agent'):
        assert not any(arg.startswith(tell) for arg in argv)
    flags = [arg.split('=', 1)[0] for arg in argv[1:]]
    assert len(flags) == len(set(flags)), 'a flag given twice'
    assert '--enable-unsafe-swiftshader' in base.command(profile=Path('/p'))  # plain Chrome keeps it


def test_headless_and_jailed_command_lines() -> None:
    fingerprint = Fingerprint(seed=12_345, platform='windows')
    headless = with_cloak(CDPOptions(headless=True), executable=Path('/c/chrome'), fingerprint=fingerprint)
    argv = headless.command(profile=Path('/p'))
    assert '--headless' in argv and '--ignore-gpu-blocklist' not in argv
    server = with_cloak(CDPOptions.server(), executable=Path('/opt/cloak/chrome'), fingerprint=fingerprint)
    assert (server.bwrap, server.virtual_screen, server.headless) == (True, True, False)
    argv = server.command(profile=Path('/tmp/profile'), proxy=Path('/tmp/egress.sock'))
    assert argv[0] == 'bwrap' and '--unshare-net' in argv
    assert ' '.join(argv).count('--ro-bind /opt/cloak /opt/cloak') == 1
    assert '--proxy-server=socks5://127.0.0.1:1080' in argv and '--fingerprint=12345' in argv


def test_the_binary_comes_from_the_environment() -> None:
    with binary_env(None), pytest.raises(RuntimeError, match='MONTYBOT_CLOAK_BINARY'):
        with_cloak(CDPOptions(executable=Path('/c')))
    with binary_env('/opt/cloak/chrome'):
        assert with_cloak(CDPOptions(executable=Path('/c'))).executable == Path('/opt/cloak/chrome')


def test_the_seed_is_stable() -> None:
    """One identity is one device on every launch and every machine; the seed stays in their wrapper's range."""
    assert fingerprint_seed('montybot') == fingerprint_seed('montybot') != fingerprint_seed('other')
    assert all(10_000 <= fingerprint_seed(str(n)) <= 99_999 for n in range(200))
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('MONTYBOT_CLOAK_SEED', '54321')
        assert Fingerprint().seed == 54_321
        patch.setenv('MONTYBOT_CLOAK_SEED', 'staging')
        assert Fingerprint().seed == fingerprint_seed('staging')
        patch.delenv('MONTYBOT_CLOAK_SEED')
        assert Fingerprint().seed == fingerprint_seed('montybot')
    assert replace(DEVICE, seed=1).args(headless=True)[0] == '--fingerprint=1'
