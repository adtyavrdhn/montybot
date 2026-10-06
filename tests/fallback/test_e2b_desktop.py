"""The E2B Desktop backend without E2B.

`LocalDesktop` stands in for the VM: commands run on this machine, Chrome for Testing runs headless, and the pointer
and keyboard are CDP input events instead of xdotool. So the conformance suite runs every CDP path of the backend
(seeding, export with HttpOnly cookies, selectors, screenshots) against a real Chromium, with no network and no key.
What it cannot cover (xdotool, Xfce, noVNC, pause and resume) is in `e2b_desktop_trial.py`.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from montybot.browser.conformance import BrowserBackendConformance, Site
from montybot.browser.contract import (
    ActionFailed,
    BrowserBackend,
    Click,
    LifecycleError,
    MouseButton,
    Navigate,
    NotSupported,
    Press,
    Ref,
    Type,
)
from montybot.browser.state import Cookie
from montybot.fallback import cdp
from montybot.fallback.e2b_desktop import (
    CHROME_ARGS,
    DESKTOP_MEMORY_MIB,
    DESKTOP_VCPUS,
    DesktopCommandFailed,
    E2BDesktopBrowser,
    cdp_cookie,
    cost_per_hour,
    desktop_keys,
    from_cdp_cookie,
    screen_point,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


PLAYWRIGHT_CACHE = Path.home() / 'Library/Caches/ms-playwright'


def find_chrome() -> str | None:
    """Playwright's Chrome for Testing on macOS, newest first, or `$MONTYBOT_TEST_CHROME`."""
    if chrome := os.environ.get('MONTYBOT_TEST_CHROME'):
        return chrome
    pattern = 'chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'
    found = sorted(PLAYWRIGHT_CACHE.glob(pattern), key=lambda p: int(p.parts[-6].split('-')[1]), reverse=True)
    return str(found[0]) if found else None


CHROME = find_chrome()
HELPER_PYTHON = os.environ.get('MONTYBOT_TEST_HELPER_PYTHON', sys.executable)
"""The Python that runs `cdp.py`. Point it at a Python 3.10 to check the helper on the VM's version."""
needs_chrome = pytest.mark.skipif(CHROME is None, reason="Playwright's Chromium is not installed")

# --- the stand-in desktop ---

_KEYS: dict[str, tuple[str, str, int]] = {
    'enter': ('Enter', 'Enter', 13),
    'tab': ('Tab', 'Tab', 9),
    'escape': ('Escape', 'Escape', 27),
    'backspace': ('Backspace', 'Backspace', 8),
    'space': (' ', 'Space', 32),
}
_MODIFIER_BITS = {'alt': 1, 'ctrl': 2, 'super': 4, 'shift': 8}
_BUTTON_BITS = {'left': 1, 'right': 2, 'middle': 4}


@dataclass(kw_only=True)
class LocalDesktop:
    """A `Desktop` on this machine, for tests. Screen positions are turned back into viewport positions with the same
    viewport info the backend uses, as the X server would by hit-testing the window."""

    profile_dir: str
    processes: list[subprocess.Popen[bytes]] = field(default_factory=list[subprocess.Popen[bytes]])
    calls: list[str] = field(default_factory=list[str])
    _pointer: tuple[float, float] = (0, 0)
    _held: set[str] = field(default_factory=set[str])

    @property
    def sandbox_id(self) -> str:
        return 'local'

    def run(self, command: str, *, timeout: float) -> str:
        done = subprocess.run(['sh', '-c', command], capture_output=True, text=True, timeout=timeout, check=False)
        if done.returncode:
            raise DesktopCommandFailed(f'exit {done.returncode}: {done.stderr.strip()[-500:]}')
        return done.stdout

    def launch(self, command: str) -> None:
        self.processes.append(
            subprocess.Popen(['sh', '-c', f'exec {command}'], start_new_session=True, stdin=subprocess.DEVNULL)
        )

    def write_file(self, path: str, data: str) -> None:
        Path(path).write_text(data)

    def move_mouse(self, x: int, y: int) -> None:
        self._pointer = self._to_viewport(x, y)
        held = next(iter(self._held), 'none')
        self._mouse('mouseMoved', button=held)

    def left_click(self) -> None:
        self._mouse('mousePressed', button='left', clickCount=1)
        self._mouse('mouseReleased', button='left', clickCount=1)

    def mouse_press(self, button: MouseButton) -> None:
        self._held.add(button)
        self._mouse('mousePressed', button=button, clickCount=1)

    def mouse_release(self, button: MouseButton) -> None:
        self._held.discard(button)
        self._mouse('mouseReleased', button=button, clickCount=1)

    def write(self, text: str) -> None:
        self._tab_call('Input.insertText', {'text': text})

    def press(self, keys: list[str]) -> None:
        *modifiers, key = keys
        bits = sum(_MODIFIER_BITS[m] for m in modifiers)
        name, code, vk = _KEYS[key] if key in _KEYS else (key, f'Key{key.upper()}', ord(key.upper()))
        down: dict[str, Any] = {'key': name, 'code': code, 'windowsVirtualKeyCode': vk, 'modifiers': bits}
        if len(name) == 1 and not bits & 7:
            down['text'] = name
        self._tab_call('Input.dispatchKeyEvent', {'type': 'keyDown', **down})
        self._tab_call('Input.dispatchKeyEvent', {'type': 'keyUp', **down})

    def screenshot(self) -> bytes:
        self.calls.append('screenshot')
        return b'\x89PNG\r\n\x1a\n'

    def stream_url(self, *, view_only: bool) -> str:
        self.calls.append('stream_url')
        return f'https://6080-local.invalid/vnc.html?view_only={str(view_only).lower()}&password=pw'

    def pause(self) -> None:
        self.calls.append('pause')

    def resume(self) -> None:
        self.calls.append('resume')

    def kill(self) -> None:
        self.calls.append('kill')
        for process in self.processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        self.processes.clear()

    def _tab_call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        connection = cdp.Cdp.connect(self.profile_dir)
        try:
            return cdp.Tab.first(connection).call(method, params)
        finally:
            connection.close()

    def _to_viewport(self, x: int, y: int) -> tuple[float, float]:
        connection = cdp.Cdp.connect(self.profile_dir)
        try:
            viewport = cdp.Tab.first(connection).viewport()
        finally:
            connection.close()
        return x / viewport['scale'] - viewport['x'], y / viewport['scale'] - viewport['y']

    def _mouse(self, kind: str, **params: Any) -> None:
        x, y = self._pointer
        buttons = sum(_BUTTON_BITS[b] for b in self._held)
        self._tab_call('Input.dispatchMouseEvent', {'type': kind, 'x': x, 'y': y, 'buttons': buttons, **params})


@asynccontextmanager
async def local_backend() -> AsyncGenerator[tuple[E2BDesktopBrowser, list[LocalDesktop]]]:
    """A backend whose desktops are `LocalDesktop`s in a throwaway folder. Kills every Chrome it started."""
    assert CHROME is not None
    work_dir = tempfile.mkdtemp(prefix='montybot-e2b-test-')
    desktops: list[LocalDesktop] = []

    def start() -> LocalDesktop:
        desktop = LocalDesktop(profile_dir=f'{work_dir}/chrome-profile')
        desktops.append(desktop)
        return desktop

    browser = E2BDesktopBrowser(
        start_desktop=start,
        chrome=CHROME,
        chrome_args=(*CHROME_ARGS, '--headless=new', '--use-mock-keychain'),
        python=HELPER_PYTHON,
        work_dir=work_dir,
    )
    try:
        yield browser, desktops
    finally:
        await browser.close()
        for desktop in desktops:
            desktop.kill()
        shutil.rmtree(work_dir, ignore_errors=True)


@needs_chrome
class TestE2BDesktopOnLocalChrome(BrowserBackendConformance):
    not_supported = frozenset({'ref'})

    @asynccontextmanager
    async def backend(self, site: Site) -> AsyncGenerator[BrowserBackend]:
        async with local_backend() as (browser, _):
            yield browser


# --- desktop extras, on the stand-in ---


@needs_chrome
async def test_pause_blocks_everything_but_resume_and_close() -> None:
    async with local_backend() as (browser, desktops):
        with pytest.raises(LifecycleError):
            await browser.pause()
        await browser.open(None)
        await browser.pause()
        assert browser.paused
        for call in (browser.snapshot(), browser.export(), browser.stream_url(), browser.pause()):
            with pytest.raises(LifecycleError, match='paused'):
                await call
        await browser.resume()
        with pytest.raises(LifecycleError):
            await browser.resume()
        assert (await browser.snapshot()).url == 'about:blank'
        await browser.pause()
        await browser.close()
        assert desktops[0].calls == ['pause', 'resume', 'pause', 'kill']
        assert browser.desktop is None


@needs_chrome
async def test_stream_url_and_desktop_screenshot_come_from_the_desktop() -> None:
    async with local_backend() as (browser, desktops):
        await browser.open(None)
        assert 'view_only=true' in await browser.stream_url(view_only=True)
        assert (await browser.desktop_screenshot()).startswith(b'\x89PNG')
        assert desktops[0].calls == ['stream_url', 'screenshot']


@needs_chrome
async def test_close_kills_the_desktop_and_open_starts_a_new_one() -> None:
    async with local_backend() as (browser, desktops):
        await browser.open(None)
        await browser.close()
        assert desktops[0].calls == ['kill'] and not desktops[0].processes
        await browser.open(None)
        assert len(desktops) == 2


@needs_chrome
async def test_refs_and_unreachable_pages() -> None:
    async with local_backend() as (browser, _):
        await browser.open(None)
        with pytest.raises(NotSupported, match='e2b-desktop does not support ref'):
            await browser.act(Click(target=Ref(ref='1')))
        with pytest.raises(NotSupported):
            await browser.act(Type(text='x', target=Ref(ref='1')))
        with pytest.raises(ActionFailed, match='ERR_'):
            await browser.act(Navigate(url='http://127.0.0.1:1/'))


async def test_a_desktop_that_will_not_start_leaves_the_backend_closed() -> None:
    def broken() -> LocalDesktop:
        raise RuntimeError('no key')

    browser = E2BDesktopBrowser(start_desktop=broken)
    with pytest.raises(ActionFailed, match='could not start the desktop: RuntimeError'):
        await browser.open(None)
    assert browser.desktop is None
    with pytest.raises(LifecycleError):
        await browser.snapshot()


# --- pure helpers ---


@pytest.mark.parametrize(
    ('press', 'keys'),
    [
        (Press(key='Enter'), ['enter']),
        (Press(key='ArrowDown', modifiers=('Shift',)), ['shift', 'down']),
        (Press(key='PageUp'), ['page_up']),
        (Press(key='F5'), ['f5']),
        (Press(key=' '), ['space']),
        (Press(key='a'), None),
        (Press(key='é'), None),
        (Press(key='b', modifiers=('Control',)), ['ctrl', 'b']),
        (Press(key='B', modifiers=('Control',)), ['ctrl', 'shift', 'b']),
        (Press(key='a', modifiers=('Meta', 'Alt')), ['alt', 'super', 'a']),
        (Press(key='.', modifiers=('Control',)), ['ctrl', 'period']),
    ],
)
def test_desktop_keys(press: Press, keys: list[str] | None) -> None:
    assert desktop_keys(press) == keys


@pytest.mark.parametrize('press', [Press(key='MediaPlay'), Press(key='é', modifiers=('Control',))])
def test_desktop_keys_refuses_what_it_cannot_name(press: Press) -> None:
    with pytest.raises(ActionFailed):
        desktop_keys(press)


def test_screen_point_adds_the_window_offset_and_scales() -> None:
    assert screen_point(10, 20, {'x': 0, 'y': 85, 'scale': 1}) == (10, 105)
    assert screen_point(10.4, 20, {'x': 5, 'y': 85, 'scale': 2}) == (31, 210)


def test_cookies_round_trip_through_cdp_form() -> None:
    cookies = [
        Cookie(name='sid', value='s', domain='shop.test', http_only=True),
        Cookie(name='wide', value='w', domain='.shop.test', expires=2e9, secure=True, same_site='Strict'),
    ]
    assert cdp_cookie(cookies[0]) == {
        'name': 'sid',
        'value': 's',
        'domain': 'shop.test',
        'path': '/',
        'secure': False,
        'httpOnly': True,
        'sameSite': 'Lax',
    }
    as_chrome_returns = [{**cdp_cookie(c), 'expires': c.expires, 'session': c.expires < 0} for c in cookies]
    assert [from_cdp_cookie(c) for c in as_chrome_returns] == cookies
    no_same_site = {'name': 'a', 'value': 'b', 'domain': 'x.test', 'path': '/', 'expires': -1, 'session': True}
    assert from_cdp_cookie(no_same_site).same_site == 'Lax'


def test_cost_per_hour_uses_e2b_published_prices() -> None:
    assert cost_per_hour(vcpus=2, memory_mib=4096) == pytest.approx(0.1656)
    assert cost_per_hour(vcpus=DESKTOP_VCPUS, memory_mib=DESKTOP_MEMORY_MIB) == pytest.approx(0.5328)


# --- the trial script ---

TRIAL = Path(__file__).with_name('e2b_desktop_trial.py')


def test_trial_script_skips_cleanly_without_a_key() -> None:
    env = {k: v for k, v in os.environ.items() if k != 'E2B_API_KEY'}
    done = subprocess.run(
        [sys.executable, str(TRIAL)], capture_output=True, text=True, env=env, timeout=60, check=False
    )
    assert done.returncode == 0, done.stderr
    assert 'E2B_API_KEY is not set' in done.stdout
    assert '"run": false' in done.stdout
