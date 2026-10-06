"""Stand-ins and fixture sites for the live view's tests.

- `StubBrowserService` stands in for the browser service (#10), with the in-memory jar standing in for #4.
- `ChromiumStandIn` and `ServoStandIn` drive the engines directly, with only what these tests need, standing in for
  the Chromium (#11) and Servo (#12) backends. Each returns its engine's real `FrameSource` from `live_view()`.
- `serve_demo_shop` serves `poc/montybot_poc/demo_site.py`; `serve_fixtures` serves the hold check (U6) and the pages
  the measurements use.
- `serve_app` runs the live view app under uvicorn.

Every server binds to a port the OS picks, and every browser and server is stopped on exit.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from starlette.types import ASGIApp

from montybot.browser.contract import (
    Action,
    BrowserBackend,
    Download,
    LifecycleError,
    Navigate,
    NotSupported,
    Screenshot,
    Snapshot,
)
from montybot.browser.live import FrameSource, LiveViewBackend
from montybot.browser.service import (
    ActionResult,
    Handoff,
    HandoffActive,
    HandoffEnded,
    HandoffId,
    HandoffNotActive,
    RunId,
    ScreenshotResult,
    SnapshotResult,
    Started,
    UnknownRun,
    UserId,
)
from montybot.browser.state import BLANK_URL, BrowserState, Cookie
from montybot.liveview.chromium import CdpFrameSource
from montybot.liveview.polling import PollingFrameSource
from montybot.liveview.webdriver import WebDriverFrameSource, WebDriverSession

REPO = Path(__file__).resolve().parents[2]
SERVO = Path(os.environ.get('MONTYBOT_SERVO', '~/.cache/montybot/servo/Servo.app/Contents/MacOS/servoshell'))
SERVO = SERVO.expanduser()
VIEWPORT = {'width': 1280, 'height': 720}

# --- the browser service stand-in ---


@dataclass(kw_only=True)
class _Run:
    user_id: UserId
    backend: BrowserBackend
    handoff: Handoff | None = None
    sources: list[FrameSource] = field(default_factory=list[FrameSource])


class StubBrowserService:
    """Stands in for #10: one backend per run and the contract's hand-off lease. No restarts and no reaper."""

    def __init__(self, new_backend: Callable[[], BrowserBackend]) -> None:
        self._new_backend = new_backend
        self._runs: dict[RunId, _Run] = {}
        self.jar: dict[UserId, BrowserState] = {}
        """The saved state per user, standing in for #4's sign-in jar."""

    async def start(self, *, run_id: RunId, user_id: UserId) -> Started:
        if run_id in self._runs:
            run = self._run(run_id, user_id)
            return Started(url=(await run.backend.snapshot()).url, reused=True)
        backend = self._new_backend()
        await backend.open(self.jar.get(user_id))
        self._runs[run_id] = _Run(user_id=user_id, backend=backend)
        return Started(url=(await backend.snapshot()).url, reused=False)

    async def act(
        self, *, run_id: RunId, user_id: UserId, action: Action, handoff_id: HandoffId | None = None
    ) -> ActionResult:
        run = self._leased(run_id, user_id, handoff_id)
        await run.backend.act(action)
        return ActionResult()

    async def snapshot(self, *, run_id: RunId, user_id: UserId) -> SnapshotResult:
        run = self._leased(run_id, user_id, None)
        return SnapshotResult(snapshot=await run.backend.snapshot())

    async def screenshot(
        self, *, run_id: RunId, user_id: UserId, handoff_id: HandoffId | None = None
    ) -> ScreenshotResult:
        run = self._leased(run_id, user_id, handoff_id)
        return ScreenshotResult(screenshot=await run.backend.screenshot())

    async def start_handoff(self, *, run_id: RunId, user_id: UserId, reason: str) -> Handoff:
        run = self._run(run_id, user_id)
        if run.handoff is None:
            run.handoff = Handoff(handoff_id=uuid.uuid4().hex, run_id=run_id, user_id=user_id, reason=reason)
        return run.handoff

    async def live_view(self, *, run_id: RunId, user_id: UserId, handoff_id: HandoffId) -> FrameSource:
        run = self._run(run_id, user_id)
        if run.handoff is None or run.handoff.handoff_id != handoff_id:
            raise HandoffNotActive(handoff_id)
        if isinstance(run.backend, LiveViewBackend):
            source = await run.backend.live_view()
        else:
            source = await PollingFrameSource.start(run.backend)
        run.sources.append(source)
        return source

    async def end_handoff(self, *, run_id: RunId, user_id: UserId, handoff_id: HandoffId) -> HandoffEnded:
        run = self._leased(run_id, user_id, handoff_id)
        await self._stop_sources(run)
        run.handoff = None
        saved = await self._save(run)
        return HandoffEnded(handoff_id=handoff_id, url=(await run.backend.snapshot()).url, saved=saved)

    async def save_state(self, *, run_id: RunId, user_id: UserId) -> None:
        run = self._run(run_id, user_id)
        self.jar[user_id] = await run.backend.export()

    async def take_downloads(self, *, run_id: RunId, user_id: UserId) -> list[Download]:
        self._run(run_id, user_id)
        return []  # the live view does not use downloads

    async def close(self, *, run_id: RunId, user_id: UserId) -> bool:
        run = self._run(run_id, user_id)
        await self._stop_sources(run)
        saved = await self._save(run)
        await run.backend.close()
        del self._runs[run_id]
        return saved

    async def close_all(self) -> None:
        for run_id, run in list(self._runs.items()):
            await self._stop_sources(run)
            await run.backend.close()
            del self._runs[run_id]

    def _run(self, run_id: RunId, user_id: UserId) -> _Run:
        run = self._runs.get(run_id)
        if run is None or run.user_id != user_id:
            raise UnknownRun(run_id)
        return run

    def _leased(self, run_id: RunId, user_id: UserId, handoff_id: HandoffId | None) -> _Run:
        run = self._run(run_id, user_id)
        if run.handoff is not None and handoff_id is None:
            raise HandoffActive('the user is driving the browser')
        if handoff_id is not None and (run.handoff is None or run.handoff.handoff_id != handoff_id):
            raise HandoffNotActive(handoff_id)
        return run

    async def _save(self, run: _Run) -> bool:
        try:
            self.jar[run.user_id] = await run.backend.export()
        except NotSupported:
            return False
        return True

    async def _stop_sources(self, run: _Run) -> None:
        sources, run.sources = run.sources, []
        for source in sources:
            await source.close()


# --- engine stand-ins ---


class ChromiumStandIn:
    """Stands in for #11: a Playwright context and page, enough for these tests, and `CdpFrameSource`."""

    def __init__(self, browser: Browser) -> None:
        self._browser = browser
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise LifecycleError('the browser is not open')
        return self._page

    async def open(self, state: BrowserState | None) -> None:
        if self._context is not None:
            raise LifecycleError('the browser is already open')
        storage: Any = None
        if state is not None:
            storage = {
                'cookies': [
                    {
                        'name': c.name,
                        'value': c.value,
                        'domain': c.domain,
                        'path': c.path,
                        'expires': c.expires,
                        'httpOnly': c.http_only,
                        'secure': c.secure,
                        'sameSite': c.same_site,
                    }
                    for c in state.cookies
                ],
                'origins': [
                    {'origin': o, 'localStorage': [{'name': k, 'value': v} for k, v in items.items()]}
                    for o, items in state.local_storage.items()
                ],
            }
        self._context = await self._browser.new_context(storage_state=storage, viewport=VIEWPORT)  # pyright: ignore[reportArgumentType]
        self._page = await self._context.new_page()
        if state is not None and state.url != BLANK_URL:
            await self._page.goto(state.url)

    async def export(self) -> BrowserState:
        if self._context is None:
            raise LifecycleError('the browser is not open')
        storage: dict[str, Any] = dict(await self._context.storage_state())
        return BrowserState(
            url=self.page.url,
            cookies=[
                Cookie(
                    name=c['name'],
                    value=c['value'],
                    domain=c['domain'],
                    path=c['path'],
                    expires=c['expires'],
                    http_only=c['httpOnly'],
                    secure=c['secure'],
                    same_site=c['sameSite'],
                )
                for c in storage['cookies']
            ],
            local_storage={
                o['origin']: {i['name']: i['value'] for i in o['localStorage']}
                for o in storage['origins']
                if o['localStorage']
            },
        )

    async def release(self) -> BrowserState:
        state = await self.export()
        await self.close()
        return state

    async def snapshot(self) -> Snapshot:
        page = self.page
        text = await page.inner_text('body') if page.url != BLANK_URL else ''
        return Snapshot(url=page.url, title=await page.title(), text=text)

    async def act(self, action: Action) -> None:
        if not isinstance(action, Navigate):
            raise NotSupported('click', engine='chromium stand-in', detail='only navigate')
        await self.page.goto(action.url)

    async def screenshot(self) -> Screenshot:
        return Screenshot(png=await self.page.screenshot(), width=VIEWPORT['width'], height=VIEWPORT['height'])

    async def live_view(self) -> FrameSource:
        return await CdpFrameSource.start(self.page)

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
        self._context = self._page = None


class ServoStandIn:
    """Stands in for #12: one headless servoshell per open, driven over WebDriver, and `WebDriverFrameSource`.

    It cannot export (Servo leaves HttpOnly cookies out of Get All Cookies), and opens only plain URLs.
    """

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._config_dir: str | None = None
        self.session: WebDriverSession | None = None

    async def open(self, state: BrowserState | None) -> None:
        if self.session is not None:
            raise LifecycleError('the browser is already open')
        if state is not None and (state.cookies or state.local_storage or state.session_storage):
            raise NotSupported('export', engine='servo stand-in', detail='opens plain URLs only')
        port = free_port()
        self._config_dir = tempfile.mkdtemp(prefix='montybot-liveview-servo-')
        self._process = await asyncio.create_subprocess_exec(
            str(SERVO),
            '--headless',
            f'--webdriver={port}',
            f'--config-dir={self._config_dir}',
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        http = httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=30)
        deadline = time.monotonic() + 20
        while True:
            try:
                response = await http.post('/session', json={'capabilities': {}})
                break
            except httpx.TransportError:
                if time.monotonic() > deadline:
                    await http.aclose()
                    await self.close()
                    raise
                await asyncio.sleep(0.1)
        self.session = WebDriverSession(http=http, session_id=response.json()['value']['sessionId'])
        await self.session.call('POST', '/window/rect', VIEWPORT)
        if state is not None and state.url != BLANK_URL:
            await self.session.call('POST', '/url', {'url': state.url})

    @property
    def _session(self) -> WebDriverSession:
        if self.session is None:
            raise LifecycleError('the browser is not open')
        return self.session

    async def export(self) -> BrowserState:
        raise NotSupported('export', engine='servo', detail='Get All Cookies leaves out HttpOnly cookies')

    async def release(self) -> BrowserState:
        return await self.export()

    async def snapshot(self) -> Snapshot:
        url, title, text = await self._session.script(
            'return [location.href, document.title, document.body ? document.body.innerText : ""]'
        )
        return Snapshot(url=url, title=title, text=text)

    async def act(self, action: Action) -> None:
        if not isinstance(action, Navigate):
            raise NotSupported('click', engine='servo stand-in', detail='only navigate')
        await self._session.call('POST', '/url', {'url': action.url})

    async def screenshot(self) -> Screenshot:
        png = base64.b64decode(await self._session.call('GET', '/screenshot'))
        width, height = struct.unpack('>II', png[16:24])
        return Screenshot(png=png, width=width, height=height)

    async def live_view(self) -> FrameSource:
        return await WebDriverFrameSource.start(self._session)

    async def close(self) -> None:
        if self.session is not None:
            with suppress(Exception):
                await self.session.call('DELETE', '')
            await self.session.http.aclose()
        if self._process is not None and self._process.returncode is None:
            self._process.kill()  # servoshell ignores SIGTERM
            await self._process.wait()
        if self._config_dir is not None:
            shutil.rmtree(self._config_dir, ignore_errors=True)
        self._process = self._config_dir = self.session = None


@asynccontextmanager
async def backends(engine: str) -> AsyncIterator[Callable[[], BrowserBackend]]:
    """A factory of fresh, closed stand-in backends for `engine`, `chromium` or `servo`. Skips the test when
    servoshell is not installed."""
    if engine == 'chromium':
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                yield lambda: ChromiumStandIn(browser)
            finally:
                await browser.close()
    else:
        if not SERVO.exists():
            pytest.skip(f'servoshell not found at {SERVO}; set MONTYBOT_SERVO')
        yield ServoStandIn


# --- servers ---


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@contextmanager
def serve_http(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    """Serve `handler` on a free port in a thread; yields the origin."""
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_address[1]}'
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def serve_demo_shop() -> Iterator[str]:
    """`poc/montybot_poc/demo_site.py`, loaded from the repo, served on a free port."""
    spec = importlib.util.spec_from_file_location('montybot_poc_demo_site', REPO / 'poc/montybot_poc/demo_site.py')
    assert spec is not None and spec.loader is not None
    demo_site = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo_site)
    with serve_http(demo_site._Handler) as origin:
        yield origin


HOLD_BOX = {'left': 100, 'top': 100, 'width': 240, 'height': 80}
HOLD_CENTRE = (220, 140)
HOLD_SECONDS = 1.5

_HOLD_HTML = """<!doctype html><title>Are you human?</title>
<style>
  body { margin: 0; font: 16px sans-serif; }
  #hold { position: absolute; left: %(left)dpx; top: %(top)dpx; width: %(width)dpx; height: %(height)dpx;
          background: #ddd; user-select: none; display: flex; align-items: center; justify-content: center; }
  #result { position: absolute; left: 100px; top: 220px; }
</style>
<div id="hold">Press and hold</div><p id="result"></p>
<script>
let downAt = 0, moves = 0;
const box = document.getElementById('hold');
box.addEventListener('mousedown', () => { downAt = performance.now(); moves = 0; box.style.background = '#9ca3af'; });
document.addEventListener('mousemove', (e) => { if (downAt && (e.buttons & 1)) moves++; });
document.addEventListener('mouseup', () => {
  if (!downAt) return;
  const held = performance.now() - downAt;
  downAt = 0;
  box.style.background = '#ddd';
  if (held >= %(ms)d && moves >= 2) location.href = '/passed';
  else document.getElementById('result').textContent = 'Hold longer (' + Math.round(held) + ' ms, ' + moves + ' moves)';
});
</script>"""

_BENCH_HTML = """<!doctype html><title>Bench</title>
<style>html, body { margin: 0; height: 100%; background: #fff; }</style>
<script>
const paint = (colour) => { document.body.style.background = colour; };
addEventListener('mousedown', () => paint('#000'));
addEventListener('mouseup', () => paint('#fff'));
addEventListener('keydown', () => paint('#000'));
addEventListener('keyup', () => paint('#fff'));
</script>"""

_ANIMATE_HTML = """<!doctype html><title>Animate</title>
<style>html, body { margin: 0; height: 100%; } #n { font: 120px monospace; }</style>
<div id="n"></div>
<script>
let n = 0;
const tick = () => { n++; document.getElementById('n').textContent = n;
  document.body.style.background = n % 2 ? '#eee' : '#fff'; requestAnimationFrame(tick); };
requestAnimationFrame(tick);
</script>"""

_POPUP_HTML = """<!doctype html><title>Opener</title>
<style>body { margin: 0; } a { position: absolute; left: 100px; top: 100px; font: 24px sans-serif; }</style>
<a id="open" href="/popup-target" target="_blank">Open a tab</a>"""

_POPUP_TARGET_HTML = '<!doctype html><title>Popup</title><p>In the popup</p>'


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.partition('?')[0]
        human = 'human=yes' in self.headers.get('Cookie', '')
        if path == '/protected':
            if not human:
                self._send(302, '', {'Location': '/hold'})
            else:
                self._send(200, '<!doctype html><title>Protected</title><p>Protected content</p>')
        elif path == '/hold':
            self._send(200, _HOLD_HTML % {**HOLD_BOX, 'ms': int(HOLD_SECONDS * 1000)})
        elif path == '/passed':
            headers = {'Location': '/protected', 'Set-Cookie': 'human=yes; HttpOnly; Path=/; SameSite=Lax'}
            self._send(303, '', headers)
        elif path in _PAGES:
            self._send(200, _PAGES[path])
        else:
            self._send(404, 'not found')

    def _send(self, status: int, body: str, headers: dict[str, str] | None = None) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass


_PAGES = {
    '/bench': _BENCH_HTML,
    '/animate': _ANIMATE_HTML,
    '/popup': _POPUP_HTML,
    '/popup-target': _POPUP_TARGET_HTML,
}


@contextmanager
def serve_fixtures() -> Iterator[str]:
    """The hold check (`/protected`, `/hold`), `/bench`, `/animate` and `/popup`, on a free port."""
    with serve_http(_FixtureHandler) as origin:
        yield origin


@asynccontextmanager
async def serve_app(app: ASGIApp) -> AsyncIterator[str]:
    """Run `app` under uvicorn on a free port; yields `http://127.0.0.1:PORT`."""
    config = uvicorn.Config(
        app, host='127.0.0.1', port=0, log_level='warning', ws_max_size=1 << 20, timeout_graceful_shutdown=2
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
