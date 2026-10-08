"""The snapshot walker in real engines: Chromium through Playwright, and Servo through WebDriver.

Both engines load the fixture pages in `tests/fixtures/`, and every test runs in both. The engines are driven directly
here, with just enough of each (load a page, run a script, click at a point, type keys) to stand in for the Chromium
(#11) and Servo (#12) backends. A test is skipped when its engine is not installed.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import AsyncGenerator, Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, ViewportSize, async_playwright

from sammy.browser.contract import Action, ActionFailed, Click, Point, Ref, TargetNotFound, Type
from sammy.browser.snapshot import JSON, SnapshotWalker, webdriver_script

FIXTURES = Path(__file__).parent.parent / 'fixtures'
SERVO = Path(os.environ.get('SAMMY_SERVO', '~/.cache/sammy/servo/Servo.app/Contents/MacOS/servoshell'))
# Servo's headless default, so both engines lay out the same width.
VIEWPORT: ViewportSize = {'width': 1024, 'height': 740}

pytestmark = pytest.mark.anyio


# --- the fixture site ---


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(scope='module')
def site() -> Iterator[str]:
    """`tests/fixtures/` on 127.0.0.1 at a free port. The same server as localhost is a second origin."""
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(_Quiet, directory=str(FIXTURES)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_address[1]}'
    server.shutdown()
    server.server_close()


# --- the engines ---


class Engine(Protocol):
    name: str

    async def goto(self, url: str) -> None: ...

    async def run_script(self, function: str, arg: JSON, /) -> object: ...

    async def native(self, action: Action) -> None:
        """A click at a `Point`, or `Type` at the caret: what `SnapshotWalker.resolve` hands the engine."""
        ...


class Chromium:
    name = 'chromium'

    def __init__(self, page: Page) -> None:
        self.page = page

    async def goto(self, url: str) -> None:
        await self.page.goto(url)

    async def run_script(self, function: str, arg: JSON, /) -> object:
        return await self.page.evaluate(function, arg)  # pyright: ignore[reportUnknownMemberType]

    async def native(self, action: Action) -> None:
        match action:
            case Click(target=Point(x=x, y=y)):
                await self.page.mouse.click(x, y)
            case Type(text=text, target=None):
                await self.page.keyboard.type(text)
            case _:
                raise AssertionError(f'not a native action: {action}')


class Servo:
    """One servoshell process with one WebDriver session, as in `poc/sammy_poc/servo.py`."""

    name = 'servo'

    def __init__(self, port: int, session: str) -> None:
        self.port = port
        self.session = session

    @staticmethod
    def request(port: int, method: str, path: str, body: object = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f'http://127.0.0.1:{port}{path}', data=data, method=method, headers={'Content-Type': 'application/json'}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)['value']
        except urllib.error.HTTPError as error:
            value = json.load(error)['value']
            raise RuntimeError(f'{value["error"]}: {value["message"]}') from None

    async def call(self, method: str, path: str, body: object = None) -> Any:
        return await asyncio.to_thread(self.request, self.port, method, f'/session/{self.session}{path}', body)

    async def goto(self, url: str) -> None:
        await self.call('POST', '/url', {'url': url})

    async def run_script(self, function: str, arg: JSON, /) -> object:
        return await self.call('POST', '/execute/sync', {'script': webdriver_script(function), 'args': [arg]})

    async def native(self, action: Action) -> None:
        match action:
            case Click(target=Point(x=x, y=y)):
                steps: list[dict[str, Any]] = [
                    {'type': 'pointerMove', 'x': round(x), 'y': round(y), 'origin': 'viewport', 'duration': 0},
                    {'type': 'pointerDown', 'button': 0},
                    {'type': 'pointerUp', 'button': 0},
                ]
                source = {'type': 'pointer', 'id': 'mouse', 'parameters': {'pointerType': 'mouse'}, 'actions': steps}
            case Type(text=text, target=None):
                keys = [{'type': kind, 'value': char} for char in text for kind in ('keyDown', 'keyUp')]
                source = {'type': 'key', 'id': 'keyboard', 'actions': keys}
            case _:
                raise AssertionError(f'not a native action: {action}')
        await self.call('POST', '/actions', {'actions': [source]})
        await self.call('DELETE', '/actions')


@pytest.fixture(scope='module')
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture(scope='module')
async def chromium() -> AsyncGenerator[Chromium]:
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch()
        except PlaywrightError as error:  # no browser installed: `uv run playwright install chromium`
            pytest.skip(f'Chromium did not start: {error}')
        page = await browser.new_page(viewport=VIEWPORT)
        yield Chromium(page)
        await browser.close()


@pytest.fixture(scope='module')
def servo() -> Iterator[Servo]:
    binary = SERVO.expanduser()
    if not binary.exists():
        pytest.skip(f'servoshell not found at {binary}; set SAMMY_SERVO')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    config_dir = tempfile.mkdtemp(prefix='sammy-servo-')
    process = subprocess.Popen(
        [str(binary), '--headless', f'--webdriver={port}', f'--config-dir={config_dir}'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                session = Servo.request(port, 'POST', '/session', {'capabilities': {}})['sessionId']
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        yield Servo(port, session)
    finally:
        process.kill()  # Servo 0.7.0 ignores SIGTERM
        process.wait()
        shutil.rmtree(config_dir, ignore_errors=True)


@pytest.fixture(params=['chromium', 'servo'])
def engine(request: pytest.FixtureRequest) -> Engine:
    return request.getfixturevalue(request.param)


# --- helpers ---


async def load(engine: Engine, site: str, page: str) -> SnapshotWalker:
    await engine.goto(f'{site}/{page}')
    return SnapshotWalker(run_script=engine.run_script)


async def act(engine: Engine, walker: SnapshotWalker, action: Action) -> None:
    """What a backend's `act` does with a ref action: resolve it with the walker, then perform it natively."""
    lowered = await walker.resolve(action)
    if lowered is not None:
        await engine.native(lowered)


def ref_of(text: str, line: str) -> Ref:
    """The ref of the one snapshot line that ends with `line`, such as `button "Sign in"`."""
    found = [row.strip() for row in text.splitlines() if row.strip().startswith('[') and row.endswith(line)]
    assert len(found) == 1, f'{len(found)} lines end with {line!r} in:\n{text}'
    return Ref(ref=found[0][1 : found[0].index(']')])


async def wait_for(walker: SnapshotWalker, expected: str, timeout: float = 5) -> str:
    deadline = time.monotonic() + timeout
    while True:
        text = (await walker.snapshot()).text
        if expected in text:
            return text
        if time.monotonic() > deadline:
            raise AssertionError(f'snapshot never showed {expected!r}; last text:\n{text}')
        await asyncio.sleep(0.05)


def lines(*rows: str) -> str:
    return '\n'.join(rows)


# --- the same text in both engines ---

EXPECTED = {
    'login.html': lines(
        '# Sign in',
        'Use your shop account.',
        '[1] textbox "Username"',
        '[2] textbox "Password" type=password',
        '[3] checkbox "Remember me" checked',
        '[4] textbox "Email for receipts" type=email',
        '[5] button "Sign in"',
        '[6] link "Forgot password?"',
    ),
    'shop.html': lines(
        '# Groceries',
        '[1] searchbox "Search"',
        '[2] combobox "Sort by" value="Price" options=["Name", "Price", "Rating"]',
        '### Eggs',
        '$3.50',
        '[3] combobox "Quantity" value="1" options=["1", "2", "3"]',
        '[4] button "Add to cart"',
        '### Milk',
        '$1.25',
        '[5] combobox "Quantity" value="1" options=["1", "2", "3"]',
        '[6] button "Add to cart"',
        '### Bread',
        '$2.75',
        '[7] combobox "Quantity" value="1" options=["1", "2", "3"]',
        '[8] button "Add to cart"',
        '## Cart',
        'Total: $0.00',
        '[9] link "Checkout"',
        '[10] button "Delivery options" collapsed',
    ),
    'iframe.html': lines(
        '# News',
        'Before the frames.',
        'iframe "Newsletter"',
        '  Get the weekly deals.',
        '  [1] textbox "Email" type=email',
        '  [2] button "Subscribe"',
        'Between the frames.',
        'iframe "Ads" (other origin, not shown)',
        'After the frames.',
    ),
    'shadow.html': lines(
        '# Components',
        'Light text before.',
        '### Coffee beans',
        'Fresh from the roaster.',
        'Shadow text bold.',
        '[1] textbox "Gift note"',
        'Nested shadow',
        '[2] button "More"',
        '[3] button "Buy now"',
        'Light text after.',
    ),
    'rerender.html': lines(
        '# Tasks',
        'Clicks: 0',
        '[1] button "Count"',
        'Renders: 1, likes: 0',
        '[2] button "Re-render"',
        'Buy milk',
        '[3] button "Delete Buy milk"',
        'Call mum',
        '[4] button "Delete Call mum"',
        '[5] button "Like"',
        '[6] button "Like"',
    ),
}


@pytest.mark.parametrize('page', list(EXPECTED))
async def test_same_snapshot_in_chromium_and_servo(chromium: Chromium, servo: Servo, site: str, page: str) -> None:
    in_chromium = await (await load(chromium, site, page)).snapshot()
    in_servo = await (await load(servo, site, page)).snapshot()
    assert in_servo == in_chromium
    assert in_chromium.text == EXPECTED[page]
    assert in_chromium.url == f'{site}/{page}'


async def test_same_snapshot_after_the_same_actions(chromium: Chromium, servo: Servo, site: str) -> None:
    async def shop_with_milk(engine: Engine) -> str:
        walker = await load(engine, site, 'shop.html')
        await walker.snapshot()
        await act(engine, walker, Type(text='2', target=Ref(ref='5')))
        await act(engine, walker, Click(target=Ref(ref='6')))
        await act(engine, walker, Click(target=Ref(ref='10')))
        await act(engine, walker, Type(text='oat milk', target=Ref(ref='1')))
        return await wait_for(walker, 'Same-day delivery')

    assert await shop_with_milk(servo) == await shop_with_milk(chromium)


# --- acting on refs, in each engine ---


async def test_login_form(engine: Engine, site: str) -> None:
    walker = await load(engine, site, 'login.html')
    text = (await walker.snapshot()).text
    await act(engine, walker, Type(text='mike', target=ref_of(text, 'textbox "Username"')))
    await act(engine, walker, Type(text='hunter2', target=ref_of(text, 'textbox "Password" type=password')))
    await act(engine, walker, Click(target=ref_of(text, 'checkbox "Remember me" checked')))
    await act(engine, walker, Type(text='m@shop.test', target=Ref(ref='4')))
    after = (await walker.snapshot()).text
    assert after.splitlines()[2:6] == [
        '[1] textbox "Username" value="mike"',
        '[2] textbox "Password" type=password value="***"',
        '[3] checkbox "Remember me"',
        '[4] textbox "Email for receipts" type=email value="m@shop.test"',
    ]
    assert 'hunter2' not in after
    await act(engine, walker, Type(text='mike2', target=Ref(ref='1')))  # replaces the value
    await act(engine, walker, Click(target=ref_of(text, 'button "Sign in"')))
    assert (await wait_for(walker, 'Signed in as')).endswith('Signed in as mike2')


async def test_shop_cart(engine: Engine, site: str) -> None:
    walker = await load(engine, site, 'shop.html')
    await walker.snapshot()
    await act(engine, walker, Type(text='3', target=Ref(ref='3')))  # Eggs' quantity, by label
    await act(engine, walker, Type(text='Rating', target=Ref(ref='2')))
    await act(engine, walker, Click(target=Ref(ref='4')))
    text = await wait_for(walker, 'Total: $10.50')
    assert '[2] combobox "Sort by" value="Rating"' in text
    assert '3 x Eggs\n[11] button "Remove Eggs"\nTotal: $10.50' in text
    with pytest.raises(ActionFailed, match='ref 2 has no option'):
        await act(engine, walker, Type(text='Colour', target=Ref(ref='2')))
    await act(engine, walker, Click(target=Ref(ref='11')))
    await wait_for(walker, 'Total: $0.00')
    with pytest.raises(TargetNotFound, match='the element is gone'):
        await act(engine, walker, Click(target=Ref(ref='11')))


async def test_same_origin_iframe(engine: Engine, site: str) -> None:
    walker = await load(engine, site, 'iframe.html')
    await walker.snapshot()
    await act(engine, walker, Type(text='a@b.test', target=Ref(ref='1')))
    await act(engine, walker, Click(target=Ref(ref='2')))
    text = await wait_for(walker, 'Subscribed')
    assert '  [2] button "Subscribe"\n  Subscribed a@b.test\nBetween the frames.' in text


async def test_shadow_dom(engine: Engine, site: str) -> None:
    walker = await load(engine, site, 'shadow.html')
    await walker.snapshot()
    await act(engine, walker, Type(text='Happy birthday', target=Ref(ref='1')))
    await act(engine, walker, Click(target=Ref(ref='3')))
    assert (await wait_for(walker, 'Bought')).endswith('Light text after.\nBought with note: Happy birthday')
    await act(engine, walker, Click(target=Ref(ref='2')))  # inside a shadow root inside a shadow root
    await wait_for(walker, 'More clicked')


async def test_refs_survive_a_rerender(engine: Engine, site: str) -> None:
    walker = await load(engine, site, 'rerender.html')
    await walker.snapshot()
    # #count survives as the same element.
    await act(engine, walker, Click(target=Ref(ref='1')))
    await act(engine, walker, Click(target=Ref(ref='1')))
    await wait_for(walker, 'Clicks: 2')
    # Re-render replaces every element in #app. Refs carry over to the replacements without a new snapshot...
    await act(engine, walker, Click(target=Ref(ref='2')))
    await act(engine, walker, Click(target=Ref(ref='2')))
    text = await wait_for(walker, 'Renders: 3')
    # ...and in the next one, except where two replacements look the same: the Like buttons get new refs.
    assert text.splitlines()[4:] == [
        '[2] button "Re-render"',
        'Buy milk',
        '[3] button "Delete Buy milk"',
        'Call mum',
        '[4] button "Delete Call mum"',
        '[7] button "Like"',
        '[8] button "Like"',
    ]
    with pytest.raises(TargetNotFound, match='ref=.5.*the element is gone'):
        await act(engine, walker, Click(target=Ref(ref='5')))
    await act(engine, walker, Click(target=Ref(ref='3')))  # deletes "Buy milk"
    await wait_for(walker, 'Renders: 4')
    with pytest.raises(TargetNotFound, match='the element is gone'):
        await act(engine, walker, Click(target=Ref(ref='3')))
    await act(engine, walker, Click(target=Ref(ref='4')))  # the "Delete Call mum" button, re-rendered at index 0
    text = await wait_for(walker, 'Renders: 5')
    assert 'Call mum' not in text
    # Each re-render gives the two look-alike Like buttons new refs, so an old one never clicks the wrong one.
    assert text.splitlines()[4:5] == ['[2] button "Re-render"']
    assert '[7] button "Like"' not in text


async def test_refs_fail_clearly(engine: Engine, site: str) -> None:
    walker = await load(engine, site, 'login.html')
    with pytest.raises(TargetNotFound, match='no snapshot of this page yet'):
        await act(engine, walker, Click(target=Ref(ref='1')))
    await walker.snapshot()
    with pytest.raises(TargetNotFound, match='not a ref in the latest snapshot'):
        await act(engine, walker, Click(target=Ref(ref='99')))
    with pytest.raises(TargetNotFound, match='not a ref'):
        await act(engine, walker, Click(target=Ref(ref='button')))
    with pytest.raises(ActionFailed, match='ref 5 cannot take typing'):
        await act(engine, walker, Type(text='x', target=Ref(ref='5')))
    await engine.goto(f'{site}/shop.html')
    with pytest.raises(TargetNotFound, match='a new page has loaded'):
        await act(engine, walker, Click(target=Ref(ref='1')))


async def test_size_budget(engine: Engine, site: str) -> None:
    await engine.goto(f'{site}/shop.html')
    small = await SnapshotWalker(run_script=engine.run_script, budget=200).snapshot()
    assert len(small.text) <= 200
    assert small.text == lines(
        '# Groceries',
        '[1] searchbox "Search"',
        '[2] combobox "Sort by" value="Price" options=["Name", "Price", "Rating"]',
        '### Eggs',
        '[cut at 200 characters: 15 more lines, 8 more refs]',
    )
    # Refs do not depend on the budget: elements past the cut are numbered all the same.
    assert (await SnapshotWalker(run_script=engine.run_script).snapshot()).text == EXPECTED['shop.html']


@pytest.mark.parametrize('length', [11_900, 12_100, 13_000, 19_000, 20_000])
async def test_default_budget_preserves_oversized_prose(engine: Engine, length: int) -> None:
    from sammy.browsing import SNAPSHOT_LIMIT

    await engine.goto('about:blank')
    await engine.run_script(
        "(n) => { document.body.innerHTML = '<p>' + 'x'.repeat(n) + '</p><button>More</button>'; }", length
    )
    walker = SnapshotWalker(run_script=engine.run_script)
    small = await walker.snapshot()
    assert walker.budget == SNAPSHOT_LIMIT == 12_000
    assert len(small.text) <= SNAPSHOT_LIMIT
    if length == 11_900:
        assert small.text == 'x' * length + '\n[1] button "More"'
    else:
        assert small.text == 'x' * 11_920 + (
            '\n[cut at 12000 characters: first line truncated; 1 more lines, 1 more refs]'
        )
    # Check assignment before a larger snapshot can mutate shared ref state. Internal preservation is not
    # agent-visible completeness: code must not guess a ref omitted from its returned page.
    assert await engine.run_script("() => document.querySelector('button').getAttribute('data-sammy-ref')", None) == '1'
    await act(engine, walker, Click(target=Ref(ref='1')))
    # A larger explicit budget can reveal the same internally retained ref.
    full = await SnapshotWalker(run_script=engine.run_script, budget=30_000).snapshot()
    assert full.text == 'x' * length + '\n[1] button "More"'
    await act(engine, walker, Click(target=Ref(ref='1')))


async def test_budget_never_exposes_a_partial_control(engine: Engine) -> None:
    await engine.goto('about:blank')
    await engine.run_script("() => { document.body.innerHTML = '<button>' + 'x'.repeat(200) + '</button>'; }", None)
    small = await SnapshotWalker(run_script=engine.run_script, budget=90).snapshot()
    assert small.text == '[cut at 90 characters: 1 more lines, 1 more refs]'


async def test_partial_prose_reserves_the_complete_notice(engine: Engine) -> None:
    await engine.goto('about:blank')
    await engine.run_script(
        "() => { document.body.innerHTML = '<p>' + 'x'.repeat(13000) + '</p>' + '<button>More</button>'.repeat(1000); }",
        None,
    )
    small = await SnapshotWalker(run_script=engine.run_script).snapshot()
    notice = '[cut at 12000 characters: first line truncated; 1000 more lines, 1000 more refs]'
    assert small.text == 'x' * (12_000 - len(notice) - 1) + '\n' + notice
    assert len(small.text) == 12_000
