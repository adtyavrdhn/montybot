"""Discord's gateway: the websocket that brings direct messages, server messages and button clicks. Only the
protocol is here; what the events mean is `sammy.channels.discord`.

```
GET /gateway/bot -> url;  connect <url>/?v=10&encoding=json
  <- Hello (op 10, heartbeat_interval)
  -> Identify (op 2: token, intents)       or, after a drop, Resume (op 6: session id, last sequence)
  <- Ready (session_id, resume_gateway_url) or Resumed, and the events missed meanwhile
  <- Dispatch (op 0, s, t, d) ...          each handed to `dispatch(t, d)`; its sequence counts once that returns
  -> Heartbeat (op 1, last sequence) every interval; <- Heartbeat ACK (op 11). No ACK since the last one: a zombie
     connection, closed and resumed.
  <- Reconnect (op 7): resume on a new connection.  <- Invalid Session (op 9, d false): Identify afresh.
```

A sequence only counts once its event was handed on: if that fails (the database is briefly away), the connection is
closed and resumed from the last event handled, so Discord sends it again. Every event that is a delivery is handled
once by its id (`inbound.receive`), so a replay is harmless.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

DISPATCH, HEARTBEAT, IDENTIFY, RESUME, RECONNECT, INVALID_SESSION, HELLO, HEARTBEAT_ACK = 0, 1, 2, 6, 7, 9, 10, 11
GUILDS, GUILD_MESSAGES, DIRECT_MESSAGES, MESSAGE_CONTENT = 1 << 0, 1 << 9, 1 << 12, 1 << 15
INTENTS = GUILDS | GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT
"""Message Content is privileged: it must be turned on for the bot in the Developer Portal (README)."""
FATAL = {
    4004: 'the bot token was refused',
    4010: 'invalid shard',
    4011: 'sharding is required',
    4012: 'invalid API version',
    4013: 'invalid intents',
    4014: 'disallowed intents: turn on the Message Content intent in the Developer Portal',
}
"""Close codes that no reconnect fixes."""
FRESH = {4007, 4009}
"""Close codes after which the session cannot be resumed (invalid sequence, session timed out)."""
OWN_CLOSE = 4000
"""How Sammy closes a connection it wants to resume: 1000 and 1001 would end the session."""
VERSION = 10
MAX_FRAME = 32 * 1024 * 1024
"""A large server's GUILD_CREATE is several megabytes."""
HELLO_SECONDS = 30.0
FATAL_WAIT = 600.0
MAX_BACKOFF = 60.0

logger = logging.getLogger(__name__)
_wire_logger = logging.getLogger(f'{__name__}.wire')
_wire_logger.setLevel(logging.WARNING)  # the websocket library logs frames at DEBUG, and Identify holds the token

Dispatch = Callable[[str, object], Awaitable[None]]


class Payload(BaseModel):
    model_config = ConfigDict(extra='ignore')

    op: int
    d: object = None
    s: int | None = None
    t: str | None = None


class Hello(BaseModel):
    heartbeat_interval: int
    """Milliseconds."""


class Ready(BaseModel):
    session_id: str
    resume_gateway_url: str


class _Reconnect(Exception):
    """Close this connection and resume on a new one."""


class Gateway:
    """One bot session, kept up until cancelled. `dispatch(t, d)` gets each event, Ready and Resumed included."""

    def __init__(
        self,
        token: str,
        gateway_url: Callable[[], Awaitable[str]],
        dispatch: Dispatch,
        *,
        intents: int = INTENTS,
        backoff: float = 1.0,
    ) -> None:
        self._token = token
        self._gateway_url = gateway_url
        self._dispatch = dispatch
        self._intents = intents
        self._backoff = backoff
        self.session_id: str | None = None
        self.resume_url: str | None = None
        self.seq: int | None = None
        self._acked = True
        self._failures = 0

    async def run(self) -> None:
        while True:
            try:
                await self._connection()
                continue  # Discord asked for a new connection: no wait
            except ConnectionClosed as error:
                code = error.rcvd.code if error.rcvd is not None else None
                if code in FATAL:
                    logger.error('Discord refused the bot (%s: %s); trying again later', code, FATAL[code])
                    await asyncio.sleep(FATAL_WAIT)
                    continue
                if code in FRESH:
                    self._forget_session()
                logger.warning('The Discord gateway closed the connection (%s)', code)
            except (_Reconnect, WebSocketException, OSError, TimeoutError, ValueError) as error:
                logger.warning('The Discord gateway connection dropped: %s', type(error).__qualname__)
            except Exception as error:  # noqa: BLE001  the gateway URL lookup failed (the HTTP API is down), say
                logger.warning('Could not reach the Discord gateway: %s', type(error).__qualname__)
            self._failures += 1
            delay = min(MAX_BACKOFF, self._backoff * 2 ** min(self._failures - 1, 6))
            await asyncio.sleep(delay * random.uniform(1, 1.5))

    def _forget_session(self) -> None:
        self.session_id = self.resume_url = self.seq = None

    async def _connection(self) -> None:
        resuming = self.session_id is not None and self.resume_url is not None
        url = self.resume_url if resuming and self.resume_url else await self._gateway_url()
        address = f'{url.rstrip("/")}/?v={VERSION}&encoding=json'
        async with connect(address, max_size=MAX_FRAME, ping_interval=None, logger=_wire_logger) as ws:
            hello = Payload.model_validate_json(await asyncio.wait_for(ws.recv(), HELLO_SECONDS))
            if hello.op != HELLO:
                raise ValueError('the gateway did not say hello')
            interval = Hello.model_validate(hello.d).heartbeat_interval / 1000
            if resuming:
                await self._send(ws, RESUME, {'token': self._token, 'session_id': self.session_id, 'seq': self.seq})
            else:
                await self._send(ws, IDENTIFY, self._identify())
            self._acked = True
            beating = asyncio.create_task(self._heartbeat(ws, interval))
            try:
                async for raw in ws:
                    if not await self._handle(ws, Payload.model_validate_json(raw)):
                        return
            finally:
                beating.cancel()
                await asyncio.gather(beating, return_exceptions=True)
            raise _Reconnect('the gateway closed the connection')

    def _identify(self) -> dict[str, object]:
        properties = {'os': 'linux', 'browser': 'sammy', 'device': 'sammy'}
        return {'token': self._token, 'intents': self._intents, 'properties': properties}

    async def _handle(self, ws: ClientConnection, payload: Payload) -> bool:
        """False once this connection should be left for a new one."""
        if payload.op == DISPATCH and payload.t is not None:
            await self._event(ws, payload.t, payload.d)
            if payload.s is not None:
                self.seq = payload.s
        elif payload.op == HEARTBEAT:  # Discord wants one now
            await self._send(ws, HEARTBEAT, self.seq)
        elif payload.op == HEARTBEAT_ACK:
            self._acked = True
        elif payload.op in (RECONNECT, INVALID_SESSION):
            if payload.op == INVALID_SESSION:
                if payload.d is not True:  # d says whether the session can still be resumed
                    self._forget_session()
                await asyncio.sleep(random.uniform(1, 5))  # as Discord asks, before identifying again
            await ws.close(OWN_CLOSE)
            return False
        return True

    async def _event(self, ws: ClientConnection, name: str, data: object) -> None:
        if name == 'READY':
            ready = Ready.model_validate(data)
            self.session_id, self.resume_url = ready.session_id, ready.resume_gateway_url
        if name in ('READY', 'RESUMED'):
            self._failures = 0
            logger.info('Discord gateway %s', 'ready' if name == 'READY' else 'resumed')
        try:
            await self._dispatch(name, data)
        except ValidationError as error:  # an event we cannot read is skipped, not replayed for ever
            logger.warning('Skipped a Discord %s event that could not be read: %s', name, type(error).__qualname__)
        except Exception as error:
            logger.warning('Handling a Discord %s event failed: %s', name, type(error).__qualname__)
            await ws.close(OWN_CLOSE)
            raise _Reconnect('resume, so the event comes again') from error

    async def _heartbeat(self, ws: ClientConnection, interval: float) -> None:
        await asyncio.sleep(interval * random.random())  # as Discord asks: the first one jittered
        while True:
            if not self._acked:
                logger.warning('No heartbeat ACK from the Discord gateway: reconnecting')
                await ws.close(OWN_CLOSE)
                return
            self._acked = False
            await self._send(ws, HEARTBEAT, self.seq)
            await asyncio.sleep(interval)

    @staticmethod
    async def _send(ws: ClientConnection, op: int, data: object) -> None:
        await ws.send(json.dumps({'op': op, 'd': data}))
