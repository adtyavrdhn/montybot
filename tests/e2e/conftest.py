"""The end-to-end harness: the real app in its own process, DBOS on a real Postgres, fixture sites, a scripted model
and a scripted human.

```
pytest process                                 app process (python -m montybot serve)
  fixture sites (tests/sites) on threads  <---  browser backend (HtmlBrowser, or Chromium with --browser=chromium)
  Client: the web app's API over HTTP     --->  Starlette + DBOS workflows + the agent
  Human: drives a hand-off through the live-view API, as a person in the web app would
```

The model is `e2e.scripts:model`, a `FunctionModel` that picks its script from the user's message. Tests wait for
what the user would see (a reply, an ask, an order on the site), never for a fixed time.

Postgres comes from `tests/conftest.py`.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import httpx
import pytest
from helpers import eventually, free_port

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / 'tests'
T = TypeVar('T')

BACKENDS = {
    'fake': 'sites.html_browser:new_backend',
    'chromium': 'montybot.engines:chromium_headless',
}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        '--browser',
        default=os.environ.get('MONTYBOT_BROWSER', 'fake'),
        choices=sorted(BACKENDS),
        help='the browser engine the app drives in end-to-end tests',
    )
    parser.addoption('--live', action='store_true', help='also run the nightly tests against real sites')


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    real_browser = config.getoption('--browser') != 'fake'
    live = pytest.mark.skip(reason='real sites: run with --live, --browser=chromium and MONTYBOT_TEST_MODEL')
    scripted = pytest.mark.skip(reason='needs the scripted model')
    for item in items:
        if 'live' in item.keywords and not (config.getoption('--live') and real_browser):
            item.add_marker(live)
        if 'scripted' in item.keywords and os.environ.get('MONTYBOT_TEST_MODEL'):
            item.add_marker(scripted)


# --- the app ---


@dataclass
class App:
    """The app in a subprocess. `kill()` is a crash (SIGKILL); `start()` brings it back on the same database."""

    env: dict[str, str]
    log: Path
    port: int = field(default_factory=lambda: free_port())
    process: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self.port}'

    def start(self) -> None:
        env = {**os.environ, **self.env, 'PORT': str(self.port), 'PUBLIC_URL': self.url}
        with self.log.open('ab') as log:
            self.process = subprocess.Popen(
                [sys.executable, '-m', 'montybot', 'serve'],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=log,
                start_new_session=True,  # its own process group, so a kill takes its browsers with it
            )

        def healthy() -> bool | None:
            assert self.process is not None
            if self.process.poll() is not None:
                raise RuntimeError(f'the app exited:\n{self.log.read_text()[-4000:]}')
            try:
                return httpx.get(f'{self.url}/healthz', timeout=2).status_code == 200 or None
            except httpx.HTTPError:
                return None

        eventually(healthy, what='the app to start')

    def kill(self) -> None:
        """A crash: the app and everything it started (Playwright, Chrome) die at once."""
        assert self.process is not None
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait()

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.kill()
        if self.process is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)


@pytest.fixture
def app(database_url: str, request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[App]:
    backend = BACKENDS[str(request.config.getoption('--browser'))]
    env = {
        'DATABASE_URL': database_url,
        'SESSION_SECRET': 'test-session-secret',
        'ENCRYPTION_KEY': 'bW9udHlib3QtdGVzdC1rZXktMzItYnl0ZXMtbG9uZyE=',
        'MODEL': os.environ.get('MONTYBOT_TEST_MODEL', 'script:e2e.scripts:model'),
        'BROWSER_BACKEND': backend,
        'PYTHONPATH': os.pathsep.join([str(TESTS), os.environ.get('PYTHONPATH', '')]),
        'EXECUTOR_ID': 'local',
        'ALLOW_PRIVATE_NETWORKS': 'true',  # the fixture sites are on 127.0.0.1
    }
    app = App(env=env, log=tmp_path / 'app.log')
    app.start()
    try:
        yield app
    finally:
        app.stop()
        report = getattr(request.node, 'rep_call', None)
        if report is not None and report.failed:
            print(app.log.read_text()[-8000:])


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    report = yield
    setattr(item, f'rep_{report.when}', report)
    return report


# --- the user ---


class Client:
    """A user of the web app, through its API."""

    def __init__(self, app: App) -> None:
        self.app = app
        self.http = httpx.Client(base_url=app.url, timeout=30)

    def sign_up(self, email: str | None = None, password: str = 'correct horse') -> dict[str, Any]:
        email = email or f'user-{uuid.uuid4().hex[:8]}@example.test'
        response = self.http.post('/api/signup', json={'email': email, 'password': password})
        assert response.status_code == 201, response.text
        return response.json()

    def ask(self, text: str, thread_id: str | None = None) -> str:
        """Send a message; returns the thread id."""
        path = '/api/threads' if thread_id is None else f'/api/threads/{thread_id}/messages'
        response = self.http.post(path, json={'text': text})
        assert response.status_code == 201, response.text
        return response.json()['thread_id']

    def thread(self, thread_id: str) -> dict[str, Any]:
        response = self.http.get(f'/api/threads/{thread_id}')
        assert response.status_code == 200, response.text
        return response.json()

    def wait_for_ask(self, thread_id: str, kind: str) -> dict[str, Any]:
        def asked() -> dict[str, Any] | None:
            run = self.thread(thread_id)['run']
            if run['status'] in ('done', 'failed'):
                raise AssertionError(f'the run ended without asking: {run}')
            ask = run['ask']
            return ask if ask is not None and ask['kind'] == kind else None

        return eventually(asked, what=f'a {kind} ask')

    def wait_for_reply(self, thread_id: str, *, failed: bool = False) -> str:
        """The reply to the thread's latest message, once its run is done. A failed run is an error unless
        `failed`."""

        def replied() -> str | None:
            thread = self.thread(thread_id)
            status = thread['run']['status']
            if status not in ('done', 'failed'):
                return None
            if (status == 'failed') != failed:
                raise AssertionError(f'the run ended {status}: {thread["messages"][-1]["text"]}')
            return thread['messages'][-1]['text']

        return eventually(replied, what='the reply')

    def answer(self, ask: dict[str, Any], **answer: Any) -> None:
        response = self.http.post(f'/api/asks/{ask["id"]}', json=answer)
        assert response.status_code == 200, response.text


class Human:
    """A person driving a hand-off in the web app's live view: keys, typing, and mouse at a point. They see the
    screen (`screen()`) but, like a person, never use selectors."""

    def __init__(self, client: Client, run_id: str) -> None:
        self.client = client
        self.run_id = run_id
        self.viewport = (0, 0)

    def screen(self) -> bytes:
        response = self.client.http.get(f'/api/runs/{self.run_id}/screen')
        assert response.status_code == 200, response.text
        width, height = response.headers['X-Viewport'].split('x')
        self.viewport = (int(width), int(height))
        return response.content

    def do(self, action: dict[str, Any]) -> None:
        response = self.client.http.post(f'/api/runs/{self.run_id}/screen', json=action)
        assert response.status_code == 200, response.text

    def type(self, text: str) -> None:
        self.do({'kind': 'type', 'text': text})

    def press(self, key: str) -> None:
        self.do({'kind': 'press', 'key': key})

    def press_and_hold(self, seconds: float) -> None:
        """Hold the mouse in the middle of the screen, where a person would."""
        self.screen()
        x, y = self.viewport[0] / 2, self.viewport[1] / 2
        self.do({'kind': 'mouse_down', 'at': {'x': x, 'y': y}})
        time.sleep(seconds)
        self.do({'kind': 'mouse_up', 'at': {'x': x, 'y': y}})

    def sign_in(self, username: str, password: str) -> None:
        """On a sign-in form whose first field has the focus, as `autofocus` gives it."""
        self.screen()
        self.type(username)
        self.press('Tab')
        self.type(password)
        self.press('Enter')


@pytest.fixture
def client(app: App) -> Iterator[Client]:
    client = Client(app)
    try:
        yield client
    finally:
        client.http.close()
