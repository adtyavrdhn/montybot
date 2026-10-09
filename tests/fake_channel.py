"""A fake chat platform, for testing the shared chat layer (`sammy.channels`) offline, as `sites/html_browser.py`
fakes the browser.

```
pytest process                                   app process
  sign(...) + POST /api/channels/fake/webhook --->  FakeChannel.verify / parse (CHANNEL_BACKENDS=fake_channel:...)
  PlatformServer (FakePlatform's routes)      <---  FakeChannel.send / edit / send_file / download
```

`PlatformServer` is the reusable part for a real platform's tests: point the adapter's API base URL at it, answer each
`(method, path)` with `reply`, and read what was called from `calls`. It runs in the test process, so it outlives an
app restart, and it can hold a call (`hold`) to stand for a crash before the platform accepted it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from starlette.responses import PlainTextResponse, Response

from sammy.channels.base import (
    Button,
    ButtonAnswer,
    Capabilities,
    Inbound,
    InboundFile,
    RawRequest,
)
from sammy.settings import Settings

SIGNATURE = 'x-fake-signature'


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --- the platform's side, in the test process ---


@dataclass(frozen=True)
class Call:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> dict[str, object]:
        loaded = json.loads(self.body)
        assert isinstance(loaded, dict)
        return loaded  # pyright: ignore[reportUnknownVariableType]


Reply = tuple[int, bytes, str]
"""Status, body and content type."""
Handler = Callable[[Call], Reply]


def reply(data: object, status: int = 200) -> Reply:
    return status, json.dumps(data).encode(), 'application/json'


@dataclass
class Hold:
    matches: Callable[[Call], bool]
    held: threading.Event = field(default_factory=threading.Event)
    released: threading.Event = field(default_factory=threading.Event)


class PlatformServer(ThreadingHTTPServer):
    """Records each call it answers, in order. A held call is never recorded or answered: once released, its
    connection is dropped, as if the platform never got it."""

    daemon_threads = True

    def __init__(self, routes: dict[tuple[str, str], Handler] | None = None) -> None:
        self.routes: dict[tuple[str, str], Handler] = dict(routes or {})
        self.calls: list[Call] = []
        self.holds: list[Hold] = []
        self.lock = threading.Lock()
        super().__init__(('127.0.0.1', 0), _Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self.server_address[1]}'

    def hold(self, matches: Callable[[Call], bool]) -> Hold:
        """Hold the next call that `matches`; wait on `held`, then set `released`."""
        hold = Hold(matches)
        with self.lock:
            self.holds.append(hold)
        return hold

    def close(self) -> None:
        for hold in self.holds:
            hold.released.set()
        self.shutdown()
        self.server_close()


class _Handler(BaseHTTPRequestHandler):
    server: PlatformServer  # pyright: ignore[reportIncompatibleVariableOverride]

    def _handle(self) -> None:
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        call = Call(self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body)
        with self.server.lock:
            hold = next((h for h in self.server.holds if not h.held.is_set() and h.matches(call)), None)
            if hold is not None:
                hold.held.set()
        if hold is not None:
            hold.released.wait()
            self.close_connection = True
            return
        handler = self.server.routes.get((self.command, self.path.split('?', 1)[0]))
        if handler is None:
            status, data, content_type = 404, b'{}', 'application/json'
        else:
            with self.server.lock:
                self.server.calls.append(call)
                status, data, content_type = handler(call)
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _handle
    do_POST = _handle

    def log_message(self, format: str, *args: object) -> None:
        pass


class FakePlatform(PlatformServer):
    """What `FakeChannel` talks to: messages, edits and files it was sent, by chat; files to download by id."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self._ids = 0
        super().__init__({('POST', '/send'): self._new, ('POST', '/edit'): self._edit, ('POST', '/file'): self._new})

    def _new(self, call: Call) -> Reply:
        self._ids += 1
        return reply({'id': f'm{self._ids}'})

    def _edit(self, call: Call) -> Reply:
        return reply({})

    def sent(self, chat: str) -> list[dict[str, object]]:
        """Each message and file sent to `chat`, in order."""
        with self.lock:
            calls = [c for c in self.calls if c.path in ('/send', '/file')]
        return [body for c in calls if (body := c.json())['chat'] == chat]

    def texts(self, chat: str) -> list[str]:
        return [str(m['text']) for m in self.sent(chat) if 'text' in m]

    def edits(self) -> list[dict[str, object]]:
        with self.lock:
            return [c.json() for c in self.calls if c.path == '/edit']

    def serve_file(self, file_id: str, data: bytes) -> None:
        self.files[file_id] = data
        self.routes[('GET', f'/files/{file_id}')] = lambda call: (200, data, 'application/octet-stream')


# --- the app's side: the adapter, which CHANNEL_BACKENDS loads ---


class FakeEvent(Inbound):
    """What the fake platform posts: a message, or a button press (`button`, the button's data)."""

    button: str | None = None


class FakeChannel:
    name = 'fake'
    capabilities = Capabilities(max_text=200, max_buttons=2, edits=True, threads=False, max_file_bytes=1_000_000)

    def __init__(self, url: str, secret: str) -> None:
        self._secret = secret
        self._http = httpx.AsyncClient(base_url=url, timeout=60)

    def verify(self, request: RawRequest) -> bool:
        given = request.headers.get(SIGNATURE, '')
        return hmac.compare_digest(given, sign(request.body, self._secret))

    def challenge(self, request: RawRequest) -> Response | None:
        if request.method == 'GET' and 'challenge' in request.query:
            return PlainTextResponse(request.query['challenge'])
        return None

    def parse(self, request: RawRequest) -> list[Inbound]:
        event = FakeEvent.model_validate_json(request.body)  # a ValidationError is a ValueError
        answer = ButtonAnswer.from_data(event.button) if event.button else None
        return [Inbound.model_validate({**event.model_dump(exclude={'button'}), 'answer': answer})]

    def ack(self) -> Response:
        return Response(status_code=200)

    def format(self, markdown: str) -> str:
        return markdown

    async def send(self, chat_id: str, text: str, buttons: Sequence[Button], reply_to: str | None) -> str:
        shown = [b.model_dump() for b in buttons]
        return await self._post('/send', {'chat': chat_id, 'text': text, 'buttons': shown})

    async def edit(self, chat_id: str, message_id: str, text: str) -> None:
        await self._post('/edit', {'chat': chat_id, 'message_id': message_id, 'text': text})

    async def send_file(self, chat_id: str, name: str, media_type: str, data: bytes) -> str:
        encoded = base64.b64encode(data).decode()
        return await self._post('/file', {'chat': chat_id, 'name': name, 'media_type': media_type, 'data': encoded})

    async def download(self, file: InboundFile) -> bytes:
        response = await self._http.get(f'/files/{file.id}')
        response.raise_for_status()
        return response.content

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, path: str, body: dict[str, object]) -> str:
        response = await self._http.post(path, json=body)
        response.raise_for_status()
        return str(response.json().get('id', ''))


def new_channel(settings: Settings) -> FakeChannel | None:
    """On only when both its settings are there, as a real platform is only with all its credentials."""
    url, secret = os.environ.get('FAKE_CHANNEL_URL'), os.environ.get('FAKE_CHANNEL_SECRET')
    return FakeChannel(url, secret) if url and secret else None
