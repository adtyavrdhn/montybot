"""A fake Discord, gateway and HTTP API, and recorded events (`tests/fixtures/discord/`), for testing
`sammy.channels.discord` offline with no real token.

```
pytest process                                        app process (or the test itself, in test_discord.py)
  FakeGateway: a websocket server on its own thread  <--- Gateway: Identify or Resume, heartbeats
    dispatch(...) sends an event (or keeps it for a resume, while no one is connected)
  DiscordAPI (a PlatformServer)                       <--- messages, edits, threads, click answers, attachments
```

The gateway keeps one session as Discord does: a Resume with its id gets every event after the sequence the client
says it has (`replay_last` sends the one before it again too, as after a drop in the middle of handling it); a Resume
of another session gets Invalid Session.
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import json
import os
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import TypeVar

from fake_channel import Call, PlatformServer, Reply, reply
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from sammy.channels.discord import DiscordChannel
from sammy.settings import Settings

TOKEN = 'MTEwMDAwMDAwMDAwMDAwMDAwMQ.FAKE.token-for-tests-only'
BOT_ID = '1100000000000000001'
PAT_ID = '1200000000000000002'
DM_ID = '1300000000000000003'
GUILD_ID = '1400000000000000004'
CHANNEL_ID = '1500000000000000005'
PAYLOADS = Path(__file__).parent / 'fixtures' / 'discord'
_ids = itertools.count(1460000000000001000)
T = TypeVar('T')


def payload(name: str, **changes: object) -> dict[str, object]:
    """A recorded event's data (`fixtures/discord/<name>.json`), with top-level fields changed."""
    loaded = json.loads((PAYLOADS / f'{name}.json').read_text())
    assert isinstance(loaded, dict)
    made: dict[str, object] = copy.deepcopy(loaded)  # pyright: ignore[reportUnknownArgumentType]
    made.update(changes)
    return made


def new_id() -> str:
    return str(next(_ids))


def message(text: str, *, name: str = 'direct_message', **changes: object) -> dict[str, object]:
    """A message from Pat (a direct one unless `name` says otherwise), with a fresh id."""
    return payload(name, id=new_id(), content=text, **changes)


def click(ask_data: str, message_id: str, *, channel_id: str = DM_ID) -> dict[str, object]:
    """Pat clicking the button whose data is `ask_data` on message `message_id`."""
    made = payload('button_click', id=new_id(), channel_id=channel_id)
    made['data'] = {'id': 2, 'custom_id': ask_data, 'component_type': 2}
    clicked = made['message']
    assert isinstance(clicked, dict)
    clicked['id'] = message_id
    return made


class FakeGateway:
    """Discord's gateway, as far as Sammy uses it. Its methods are for the test's thread."""

    def __init__(self, heartbeat_ms: int = 41250) -> None:
        self.heartbeat_ms = heartbeat_ms
        self.identifies: list[dict[str, object]] = []
        self.resumes: list[dict[str, object]] = []
        self.heartbeats = 0
        self.acks = True
        """Answer heartbeats; False makes the connection a zombie."""
        self.replay_last = False
        self.session_id = ''
        self.events: list[tuple[int, str, object]] = []
        """The session's events, by sequence, for a resume."""
        self.lock = threading.Lock()
        self._sessions = itertools.count(1)
        self._ws: ServerConnection | None = None
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        self._server = self._run(self._start())

    @property
    def url(self) -> str:
        return f'ws://127.0.0.1:{self._server.sockets[0].getsockname()[1]}'

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def _run(self, coroutine: Coroutine[object, object, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(timeout=30)

    async def _start(self) -> Server:
        return await serve(self._serve, '127.0.0.1', 0)

    # --- for the test ---

    def dispatch(self, name: str, data: object) -> int:
        """Send an event now, or keep it for the resume if no one is connected. Its sequence."""
        return self._run(self._dispatch(name, data))

    def drop(self) -> None:
        """The network drops the connection: no close frame, the session stays resumable."""
        self._run(self._drop())

    def send(self, op: int, data: object = None) -> None:
        """A raw opcode, such as Reconnect (7) or Invalid Session (9)."""
        self._run(self._send_op(op, data))

    def close_with(self, code: int) -> None:
        """Close the connection as Discord does when it refuses the bot (4004 bad token, 4014 disallowed intents)."""
        self._run(self._close_with(code))

    def close(self) -> None:
        self._run(self._close())
        self._loop.call_soon_threadsafe(self._loop.stop)

    # --- the gateway's side ---

    async def _serve(self, ws: ServerConnection) -> None:
        await ws.send(json.dumps({'op': 10, 'd': {'heartbeat_interval': self.heartbeat_ms}, 's': None, 't': None}))
        try:
            async for raw in ws:
                sent = json.loads(raw)
                op, data = sent['op'], sent.get('d')
                if op == 1:
                    with self.lock:
                        self.heartbeats += 1
                    if self.acks:
                        await ws.send(json.dumps({'op': 11}))
                elif op == 2:
                    await self._identify(ws, data)
                elif op == 6:
                    await self._resume(ws, data)
        except ConnectionClosed:
            pass
        finally:
            if self._ws is ws:
                self._ws = None

    async def _identify(self, ws: ServerConnection, data: dict[str, object]) -> None:
        with self.lock:
            self.identifies.append(data)
            self.session_id = f'session-{next(self._sessions)}'
            self.events.clear()
        self._ws = ws
        ready = payload('ready', session_id=self.session_id, resume_gateway_url=self.url)
        await self._dispatch('READY', ready)
        await self._dispatch('GUILD_CREATE', payload('guild_create'))

    async def _resume(self, ws: ServerConnection, data: dict[str, object]) -> None:
        with self.lock:
            self.resumes.append(data)
        if data.get('session_id') != self.session_id:
            await ws.send(json.dumps({'op': 9, 'd': False}))
            return
        seq = data.get('seq')
        last = seq if isinstance(seq, int) else 0
        self._ws = ws
        for number, name, event in list(self.events):
            if number > last or (self.replay_last and number == last):
                await self._send(ws, number, name, event)
        await self._dispatch('RESUMED', None)

    async def _dispatch(self, name: str, data: object) -> int:
        with self.lock:
            number = len(self.events) + 1
            self.events.append((number, name, data))
        if self._ws is not None:
            try:
                await self._send(self._ws, number, name, data)
            except ConnectionClosed:
                self._ws = None
        return number

    @staticmethod
    async def _send(ws: ServerConnection, number: int, name: str, data: object) -> None:
        await ws.send(json.dumps({'op': 0, 's': number, 't': name, 'd': data}))

    async def _send_op(self, op: int, data: object) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps({'op': op, 'd': data}))

    async def _close_with(self, code: int) -> None:
        if self._ws is not None:
            await self._ws.close(code)

    async def _drop(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            ws.transport.abort()

    async def _close(self) -> None:
        self._server.close()
        await self._server.wait_closed()


class DiscordAPI(PlatformServer):
    """Discord's HTTP API as far as Sammy calls it, recording each call; `gateway` is where `/gateway/bot` points."""

    def __init__(self, gateway: FakeGateway) -> None:
        self.gateway = gateway
        self._message_ids = itertools.count(1600000000000000001)
        self._made_threads: set[str] = set()
        super().__init__(
            {
                ('GET', '/gateway/bot'): self._gateway,
                ('POST', '/channels/*/messages'): self._message,
                ('PATCH', '/channels/*/messages/*'): self._edit,
                ('POST', '/channels/*/messages/*/threads'): self._thread,
                ('POST', '/interactions/*/*/callback'): lambda call: (204, b'', 'application/json'),
            }
        )

    def _gateway(self, call: Call) -> Reply:
        limit = {'total': 1000, 'remaining': 999, 'reset_after': 14400000, 'max_concurrency': 1}
        return reply({'url': self.gateway.url, 'shards': 1, 'session_start_limit': limit})

    def _message(self, call: Call) -> Reply:
        channel_id = call.path.split('/')[2]
        return reply({'id': str(next(self._message_ids)), 'channel_id': channel_id, 'type': 0})

    def _edit(self, call: Call) -> Reply:
        _, _, channel_id, _, message_id = call.path.split('/')
        return reply({'id': message_id, 'channel_id': channel_id, 'type': 0})

    def _thread(self, call: Call) -> Reply:
        _, _, channel_id, _, message_id, _ = call.path.split('/')
        if message_id in self._made_threads:
            return reply({'message': 'A thread has already been created for this message', 'code': 160004}, 400)
        self._made_threads.add(message_id)
        name = call.json()['name']
        return reply({'id': message_id, 'type': 11, 'parent_id': channel_id, 'owner_id': BOT_ID, 'name': name})

    def serve_file(self, path: str, data: bytes) -> None:
        self.routes[('GET', path)] = lambda call: (200, data, 'application/octet-stream')

    def called(self, method: str, prefix: str) -> list[Call]:
        with self.lock:
            return [c for c in self.calls if c.method == method and c.path.startswith(prefix)]

    def messages(self, chat_id: str) -> list[Call]:
        """Each message sent to `chat_id`, text or file, in order."""
        return [c for c in self.called('POST', f'/channels/{chat_id}/messages') if c.path.endswith('/messages')]

    def texts(self, chat_id: str) -> list[str]:
        return [
            str(c.json()['content']) for c in self.messages(chat_id) if c.headers['content-type'] == 'application/json'
        ]

    def message_id(self, call: Call) -> str:
        """The id the API gave the message `call` sent: messages are numbered in the order they were answered."""
        with self.lock:
            sent = [c for c in self.calls if c.method == 'POST' and c.path.endswith('/messages')]
        return str(1600000000000000001 + sent.index(call))


def new_channel(settings: Settings) -> DiscordChannel | None:
    """The real adapter, pointed at `DiscordAPI` (FAKE_DISCORD_URL) with the fake token; files come from there too."""
    url = os.environ.get('FAKE_DISCORD_URL')
    return DiscordChannel(TOKEN, api_url=url, files_url=url, backoff=0.2) if url else None
