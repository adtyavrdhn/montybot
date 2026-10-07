"""A small Chrome DevTools Protocol client over `--remote-debugging-pipe`, for `ChromiumCDPBackend` (`cdp.py`).

Chrome reads commands on its fd 3 and writes replies and events on its fd 4, one JSON message each, ended by a NUL
byte. There is no port: only the process that started Chrome holds the pipe. Sessions are flat
(`Target.attachToTarget` with `flatten`), so one pipe carries every tab's messages, told apart by `sessionId`.

Nothing here knows about pages or the browser contract. It sends a command and waits for its reply, and calls the
handlers registered for each event.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
from collections.abc import Callable
from typing import Any, cast

_log = logging.getLogger(__name__)

CDPParams = dict[str, Any]
"""A command's parameters or result, or an event's parameters: CDP's JSON objects, typed per method by Chrome."""
EventHandler = Callable[[CDPParams], None]


class CDPError(Exception):
    """Chrome answered a command with an error. The message names the problem, never page data."""

    def __init__(self, method: str, message: str) -> None:
        self.method = method
        self.message = message
        super().__init__(f'{method}: {message}')


class CDPClosed(CDPError):
    """The pipe closed: Chrome exited or crashed."""

    def __init__(self, method: str) -> None:
        super().__init__(method, 'the browser closed')


class _PipeReader(asyncio.Protocol):
    def __init__(self, connection: CDPConnection) -> None:
        self._connection = connection
        self._buffer = bytearray()

    def data_received(self, data: bytes) -> None:
        self._buffer += data
        while (end := self._buffer.find(b'\0')) >= 0:
            message = bytes(self._buffer[:end])
            del self._buffer[: end + 1]
            self._connection.received(message)

    def connection_lost(self, exc: Exception | None) -> None:
        self._connection.lost()


class CDPConnection:
    """One browser's pipe. Use `await CDPConnection.open(read_fd=..., write_fd=...)`, with our ends of the pipes.

    `send` without a session talks to the browser; with one, to the target attached as that session.
    """

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self._pending: dict[int, tuple[str, asyncio.Future[CDPParams]]] = {}
        self._handlers: dict[tuple[str, str], list[EventHandler]] = {}
        self._writer: asyncio.WriteTransport | None = None
        self._reader: asyncio.ReadTransport | None = None
        self.closed = asyncio.Event()

    @classmethod
    async def open(cls, *, read_fd: int, write_fd: int) -> CDPConnection:
        """Take over `read_fd` (Chrome's fd 4) and `write_fd` (Chrome's fd 3); both are closed with the connection."""
        connection = cls()
        loop = asyncio.get_running_loop()
        reader, _ = await loop.connect_read_pipe(lambda: _PipeReader(connection), os.fdopen(read_fd, 'rb', 0))
        writer, _ = await loop.connect_write_pipe(asyncio.Protocol, os.fdopen(write_fd, 'wb', 0))
        connection._reader = reader
        connection._writer = writer
        return connection

    async def send(self, method: str, params: CDPParams | None = None, *, session: str | None = None) -> CDPParams:
        """Send a command and return its result. Raises `CDPError`, or `CDPClosed` if Chrome is gone."""
        if self._writer is None or self.closed.is_set():
            raise CDPClosed(method)
        message_id = next(self._ids)
        message: CDPParams = {'id': message_id, 'method': method, 'params': params or {}}
        if session is not None:
            message['sessionId'] = session
        future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = (method, future)
        try:
            self._writer.write(json.dumps(message).encode() + b'\0')
            return await future
        finally:
            self._pending.pop(message_id, None)

    def on(self, method: str, handler: EventHandler, *, session: str = '') -> Callable[[], None]:
        """Call `handler` with the parameters of each `method` event from `session` ('' for the browser's own).
        Returns a function that removes it. Handlers run on the event loop and must not block."""
        handlers = self._handlers.setdefault((session, method), [])
        handlers.append(handler)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                handlers.remove(handler)

        return remove

    def drop_session(self, session: str) -> None:
        """Forget every handler of a session that is gone."""
        for key in [key for key in self._handlers if key[0] == session]:
            del self._handlers[key]

    def close(self) -> None:
        for transport in (self._writer, self._reader):
            if transport is not None:
                transport.close()
        self.lost()

    # --- called by the reader ---

    def received(self, raw: bytes) -> None:
        try:
            message = cast(CDPParams, json.loads(raw))
        except ValueError:
            _log.warning('Chrome sent a message that is not JSON')
            return
        if 'id' in message:
            entry = self._pending.get(int(message['id']))
            if entry is None:
                return
            method, future = entry
            if future.done():
                return
            if 'error' in message:
                error = cast(CDPParams, message['error'])
                future.set_exception(CDPError(method, str(error.get('message', 'error'))))
            else:
                future.set_result(cast(CDPParams, message.get('result') or {}))
            return
        key = (str(message.get('sessionId', '')), str(message.get('method', '')))
        params = cast(CDPParams, message.get('params') or {})
        for handler in list(self._handlers.get(key, ())):
            try:
                handler(params)
            except Exception:  # noqa: BLE001  one bad handler must not stop the pipe for the others
                _log.exception('CDP event handler for %s failed', key[1])

    def lost(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        for method, future in list(self._pending.values()):
            if not future.done():
                future.set_exception(CDPClosed(method))
