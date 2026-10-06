"""A remote browser backed by Servo, driven over W3C WebDriver.

Tested with Servo 0.7.0. What its WebDriver supports, and what that means for hand-off:

- Navigation, finding elements, typing, clicking and Execute Script all work.
- Add Cookie works, including `httpOnly: true`: the site sees the cookie and page scripts do not.
- Get All Cookies leaves out HttpOnly cookies. So Servo can take in a login made elsewhere, but cannot
  give back a login it made itself, or a session cookie the site refreshed while Servo held it. On
  export, this backend returns the HttpOnly cookies it was seeded with, which may be out of date.
- One WebDriver session per Servo process, so each run gets its own process and its own throwaway
  config folder.
- WebDriver has no storage API, so storage is read and written with Execute Script, one origin at a time.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from montybot_poc.state import BrowserState, Cookie

DEFAULT_BINARY = Path.home() / '.cache/montybot/servo/Servo.app/Contents/MacOS/servoshell'
_ELEMENT = 'element-6066-11e4-a52e-4f735466cecf'
_READ_STORAGE = 'return Object.fromEntries(Object.entries(%s))'
_WRITE_STORAGE = (
    'const [items, clear] = arguments; if (clear) %(s)s.clear();'
    ' for (const [k, v] of Object.entries(items)) %(s)s.setItem(k, v);'
)


class WebDriverError(Exception):
    pass


def _origin(url: str) -> str | None:
    parts = urlsplit(url)
    return f'{parts.scheme}://{parts.netloc}' if parts.scheme in ('http', 'https') else None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _cookie_key(cookie: Cookie) -> tuple[str, str, str]:
    return cookie.name, cookie.domain.lstrip('.'), cookie.path


class ServoBrowser:
    """One Servo process per opened state; `release` exports the state and stops the process."""

    def __init__(self, binary: Path = DEFAULT_BINARY) -> None:
        self._binary = binary
        self._process: asyncio.subprocess.Process | None = None
        self._config_dir: str | None = None
        self._http: httpx.AsyncClient | None = None
        self._session: str | None = None
        self._seeded = BrowserState(url='about:blank')

    # --- WebDriver plumbing ---

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        assert self._http is not None and self._session is not None, 'the browser is handed off to the user'
        response = await self._http.request(method, f'/session/{self._session}{path}', json=body)
        value = response.json()['value']
        if response.status_code >= 400:
            raise WebDriverError(f'{value["error"]}: {value["message"]}')
        return value

    async def _script(self, script: str, *args: Any) -> Any:
        return await self._call('POST', '/execute/sync', {'script': script, 'args': list(args)})

    async def _goto(self, url: str) -> None:
        await self._call('POST', '/url', {'url': url})

    async def _settle(self) -> None:
        """Let a click's navigation start, then wait for the document to finish loading."""
        await asyncio.sleep(0.3)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if await self._script('return document.readyState') == 'complete':
                return
            await asyncio.sleep(0.1)

    # --- lifecycle ---

    async def open(self, state: BrowserState | None) -> None:
        started = time.monotonic()
        port = _free_port()
        self._config_dir = tempfile.mkdtemp(prefix='montybot-servo-')
        self._process = await asyncio.create_subprocess_exec(
            str(self._binary),
            '--headless',
            f'--webdriver={port}',
            f'--config-dir={self._config_dir}',
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._http = httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=30)
        deadline = time.monotonic() + 20
        while True:
            try:
                response = await self._http.post('/session', json={'capabilities': {}})
                self._session = response.json()['value']['sessionId']
                break
            except httpx.TransportError:
                if time.monotonic() > deadline:
                    raise
                await asyncio.sleep(0.1)
        print(f'[servo] started in {time.monotonic() - started:.2f}s')
        self._seeded = state or BrowserState(url='about:blank')
        if state is not None:
            await self._apply(state)

    async def _apply(self, state: BrowserState) -> None:
        """Seed cookies and storage. WebDriver only sets them for the current page's origin, so visit each."""
        target = _origin(state.url)
        origins: dict[str, list[Cookie]] = {origin: [] for origin in state.local_storage}
        for cookie in state.cookies:
            host = cookie.domain.lstrip('.')
            origin = target if target and urlsplit(target).hostname == host else None
            origin = origin or f'{"https" if cookie.secure else "http"}://{host}'
            origins.setdefault(origin, []).append(cookie)
        for origin, cookies in origins.items():
            await self._goto(origin + '/')
            for cookie in cookies:
                body: dict[str, Any] = {
                    'name': cookie.name,
                    'value': cookie.value,
                    'path': cookie.path,
                    'secure': cookie.secure,
                    'httpOnly': cookie.http_only,
                    'sameSite': cookie.same_site,
                }
                if cookie.domain.startswith('.'):
                    body['domain'] = cookie.domain
                if cookie.expires > 0:
                    body['expiry'] = int(cookie.expires)
                await self._call('POST', '/cookie', {'cookie': body})
            if items := state.local_storage.get(origin):
                await self._script(_WRITE_STORAGE % {'s': 'localStorage'}, items, False)
        if target is None:
            return
        await self._goto(state.url)
        if items := state.session_storage.get(target):
            await self._script(_WRITE_STORAGE % {'s': 'sessionStorage'}, items, True)
            await self._call('POST', '/refresh', {})

    async def release(self) -> BrowserState:
        """Export what WebDriver can read, keep the seeded HttpOnly cookies it cannot, and stop Servo."""
        url = await self._call('GET', '/url')
        readable = [
            Cookie(
                name=c['name'],
                value=c['value'],
                domain=c.get('domain', ''),
                path=c.get('path', '/'),
                expires=c.get('expiry', -1),
                http_only=c.get('httpOnly', False),
                secure=c.get('secure', False),
                same_site=c.get('sameSite', 'Lax'),
            )
            for c in await self._call('GET', '/cookie')
        ]
        seen = {_cookie_key(c) for c in readable}
        hidden = [c for c in self._seeded.cookies if c.http_only and _cookie_key(c) not in seen]
        local = dict(self._seeded.local_storage)
        session: dict[str, dict[str, str]] = {}
        if origin := _origin(url):
            local[origin] = await self._script(_READ_STORAGE % 'localStorage')
            session[origin] = await self._script(_READ_STORAGE % 'sessionStorage')
        await self.close()
        return BrowserState(url=url, cookies=readable + hidden, local_storage=local, session_storage=session)

    async def close(self) -> None:
        if self._http is not None:
            try:
                await self._call('DELETE', '')
            except (httpx.HTTPError, WebDriverError):
                pass
            await self._http.aclose()
        if self._process is not None and self._process.returncode is None:
            # Servo 0.7.0 ignores SIGTERM, and the profile is thrown away, so there is nothing to shut down cleanly.
            self._process.kill()
            await self._process.wait()
        if self._config_dir is not None:
            shutil.rmtree(self._config_dir, ignore_errors=True)
        self._process = self._http = self._session = self._config_dir = None

    # --- the agent's actions ---

    async def goto(self, url: str) -> None:
        await self._goto(url)

    async def click(self, selector: str) -> None:
        deadline = time.monotonic() + 5
        while True:
            try:
                element = await self._call('POST', '/element', {'using': 'css selector', 'value': selector})
                break
            except WebDriverError:
                if time.monotonic() > deadline:
                    raise
                await asyncio.sleep(0.1)
        await self._call('POST', f'/element/{element[_ELEMENT]}/click', {})
        await self._settle()

    async def describe(self) -> str:
        url = await self._call('GET', '/url')
        text = await self._script('return document.body ? document.body.innerText : ""')
        return f'URL: {url}\n\n{text[:4000]}'
