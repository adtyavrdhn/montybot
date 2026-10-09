"""`LiveViewClient`: talks to a hand-off's WebSocket exactly as the page does, for scripted users and tests."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed
from websockets.typing import Origin

from sammy.browser.live import Frame, LiveInput, Outline, Tab, Tabs
from sammy.liveview.auth import SESSION_COOKIE
from sammy.liveview.wire import (
    ClientMessage,
    Ended,
    ErrorMessage,
    GiveBackRequest,
    Hello,
    OutlineRequest,
    SwitchTab,
    decode_frame,
    decode_server,
    encode_client,
)

MAX_FRAME = 16 * 1024 * 1024


class LiveViewClosed(Exception):
    """The server closed the connection before what we waited for happened."""

    def __init__(self, code: int | None) -> None:
        self.code = code
        super().__init__(f'the live view closed the connection (code {code})')


@dataclass(frozen=True, kw_only=True)
class ReceivedFrame:
    seq: int
    frame: Frame
    received_at: float
    """`time.monotonic()` when it arrived."""


class LiveViewClient:
    """Use `async with LiveViewClient.connect(url, session=...) as client`.

    It keeps the latest frame, tab list and errors, and `wait_until` waits for any condition on them.
    """

    def __init__(self, connection: ClientConnection) -> None:
        self._connection = connection
        self._changed = asyncio.Condition()
        self.hello: Hello | None = None
        self.tabs: Tabs | None = None
        self.frame: ReceivedFrame | None = None
        self.frames = 0
        """Frames received so far."""
        self.errors: list[str] = []
        self.outline: Outline | None = None
        self._outlines = 0
        self.ended: Ended | None = None
        self.closed = False

    @classmethod
    @asynccontextmanager
    async def connect(
        cls, url: str, *, session: str | None = None, origin: str | None = None
    ) -> AsyncGenerator[LiveViewClient]:
        """Connect to `ws://.../handoff/ID/ws`, signed in with the `sammy_session` cookie `session`."""
        headers = {'Cookie': f'{SESSION_COOKIE}={session}'} if session else None
        async with connect(
            url,
            additional_headers=headers,
            origin=Origin(origin) if origin else None,
            max_size=MAX_FRAME,
            compression=None,
            proxy=None,
        ) as connection:
            client = cls(connection)
            reader = asyncio.create_task(client._read())
            try:
                yield client
            finally:
                reader.cancel()
                with suppress(asyncio.CancelledError):
                    await reader

    @property
    def close_code(self) -> int | None:
        return self._connection.close_code

    @property
    def active_tab(self) -> Tab | None:
        return next((tab for tab in self.tabs.tabs if tab.active), None) if self.tabs else None

    async def send(self, message: LiveInput | ClientMessage) -> None:
        await self._connection.send(encode_client(message))

    async def switch_tab(self, tab_id: str) -> None:
        await self.send(SwitchTab(tab_id=tab_id))

    async def give_back(self, *, timeout: float = 30) -> Ended:
        await self.send(GiveBackRequest())
        await self.wait_until(lambda: self.ended is not None, timeout=timeout)
        assert self.ended is not None
        return self.ended

    async def read_outline(self, *, timeout: float = 10) -> Outline:
        """Ask what is on the page, as a screen reader would, and wait for the answer."""
        seen = self._outlines
        await self.send(OutlineRequest())
        await self.wait_until(lambda: self._outlines > seen, timeout=timeout)
        assert self.outline is not None
        return self.outline

    async def wait_until(self, condition: Callable[[], bool], *, timeout: float = 10) -> None:
        """Wait until `condition()` holds. Raises `LiveViewClosed` if the connection closes first."""
        async with self._changed:
            await asyncio.wait_for(self._changed.wait_for(lambda: condition() or self.closed), timeout)
        if not condition():
            raise LiveViewClosed(self.close_code)

    async def wait_for_url(self, condition: Callable[[str], bool], *, timeout: float = 10) -> str:
        """Wait until the active tab's URL meets `condition`, and return it."""
        await self.wait_until(lambda: (tab := self.active_tab) is not None and condition(tab.url), timeout=timeout)
        assert self.active_tab is not None
        return self.active_tab.url

    async def next_frame(self, *, after: int | None = None, timeout: float = 10) -> ReceivedFrame:
        """The first frame numbered above `after` (by default, the latest one so far)."""
        after = (self.frame.seq if self.frame else 0) if after is None else after
        await self.wait_until(lambda: self.frame is not None and self.frame.seq > after, timeout=timeout)
        assert self.frame is not None
        return self.frame

    async def _read(self) -> None:
        try:
            async for message in self._connection:
                received_at = time.monotonic()
                if isinstance(message, bytes):
                    numbered = decode_frame(message)
                    self.frame = ReceivedFrame(seq=numbered.seq, frame=numbered.frame, received_at=received_at)
                    self.frames += 1
                else:
                    match decode_server(message):
                        case Hello() as hello:
                            self.hello = hello
                        case Tabs() as tabs:
                            self.tabs = tabs
                        case ErrorMessage(message=text):
                            self.errors.append(text)
                        case Ended() as ended:
                            self.ended = ended
                        case Outline() as outline:
                            self.outline = outline
                            self._outlines += 1
                async with self._changed:
                    self._changed.notify_all()
        except ConnectionClosed:
            pass
        finally:
            self.closed = True
            async with self._changed:
                self._changed.notify_all()
