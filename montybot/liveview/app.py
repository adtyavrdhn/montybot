"""The live view's ASGI app: one page and one WebSocket per hand-off.

    GET /handoff/{handoff_id}      the page: frames, input, tabs and "Give back to the bot"
    WS  /handoff/{handoff_id}/ws   the WebSocket the page (or the web app's own client) talks to; see `wire.py`
    GET /live.js                   the page's script

Mount it in the web app (#8). Who may open a hand-off comes only from being signed in as the run's requester; the
link names the hand-off and grants nothing. Each hand-off has one live connection: a new one (a reload, the same
link on a phone) takes over, and the old one is closed with `CLOSE_REPLACED`. Reconnecting is always allowed while
the hand-off is active.

The app reaches the browser only through `BrowserService.live_view` and `end_handoff`, with the hand-off's id, so the
service's lease decides what is allowed. Frames go to the socket and nowhere else: they are not logged, stored or
given to the run. The run gets a `GiveBack` with a summary in words.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from importlib.resources import files
from typing import Protocol
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from montybot.browser.contract import BrowserError, NotSupported
from montybot.browser.live import Frame, FrameSource, Viewport
from montybot.browser.service import Handoff, HandoffEnded, HandoffId, HandoffNotActive, RunId, UnknownRun, UserId
from montybot.liveview.activity import Activity
from montybot.liveview.auth import Authenticator
from montybot.liveview.handoffs import GiveBack, Handoffs
from montybot.liveview.wire import (
    Ended,
    ErrorMessage,
    GiveBackRequest,
    Hello,
    ServerMessage,
    SwitchTab,
    ViewportSize,
    WireError,
    decode_client,
    encode_frame,
    encode_server,
)

CLOSE_SIGNED_OUT = 4401
"""Nobody is signed in: sign in, then open the link again."""
CLOSE_NOT_FOUND = 4404
"""No such hand-off, or it belongs to someone else. The same answer for both."""
CLOSE_REPLACED = 4409
"""A newer connection for the same hand-off took over."""
CLOSE_ENDED = 4410
"""The hand-off is over: given back, timed out, or the run closed."""

PHONE_WIDTH = 900
"""A page narrower than this, in CSS pixels, gets the bot's browser laid out at its own size, as a phone's browser
would: a desktop page shrunk to a phone's width is too small to read or tap. A wider page keeps the browser's own
desktop size, scaled down a little at most, so the sites look as they do for the agent."""

_PAGE = (files('montybot.liveview') / 'page.html').read_text()
_SCRIPT = (files('montybot.liveview') / 'live.js').read_text()


class LiveViewService(Protocol):
    """The part of `BrowserService` the live view calls."""

    async def live_view(self, *, run_id: RunId, user_id: UserId, handoff_id: HandoffId) -> FrameSource: ...

    async def end_handoff(self, *, run_id: RunId, user_id: UserId, handoff_id: HandoffId) -> HandoffEnded: ...


def live_view_app(
    *,
    service: LiveViewService,
    handoffs: Handoffs,
    auth: Authenticator,
    allowed_origins: frozenset[str] = frozenset(),
    frame_ancestors: str = "'self'",
) -> Starlette:
    """The app. `allowed_origins` lists page origins, such as `https://app.example`, that may open the WebSocket
    besides the app's own host; `frame_ancestors` is the CSP list of sites that may embed the page."""
    live = _LiveView(service=service, handoffs=handoffs, auth=auth, allowed_origins=allowed_origins)
    headers = {
        'Content-Security-Policy': (
            "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
            f"connect-src 'self'; frame-ancestors {frame_ancestors}"
        ),
        'Cache-Control': 'no-store',
        'Referrer-Policy': 'no-referrer',
        'X-Content-Type-Options': 'nosniff',
    }

    async def page(request: Request) -> Response:
        handoff, error = await live.authorise(request, request.path_params['handoff_id'])
        if handoff is None:
            text, status = ('Sign in, then open this link again.', 401) if error == CLOSE_SIGNED_OUT else ('', 404)
            return PlainTextResponse(text or 'Not found.', status_code=status, headers=headers)
        return HTMLResponse(_PAGE, headers=headers)

    async def script(request: Request) -> Response:
        return Response(_SCRIPT, media_type='text/javascript', headers=headers)

    return Starlette(
        routes=[
            Route('/handoff/{handoff_id}', page),
            WebSocketRoute('/handoff/{handoff_id}/ws', live.socket),
            Route('/live.js', script),
        ]
    )


class _Slot:
    """One connection's claim on a hand-off."""

    def __init__(self) -> None:
        self.replaced = asyncio.Event()
        self.done = asyncio.Event()


class _LiveView:
    def __init__(
        self, *, service: LiveViewService, handoffs: Handoffs, auth: Authenticator, allowed_origins: frozenset[str]
    ) -> None:
        self._service = service
        self._handoffs = handoffs
        self._auth = auth
        self._allowed_origins = allowed_origins
        self._slots: dict[HandoffId, _Slot] = {}
        self._activity: dict[HandoffId, Activity] = {}

    async def authorise(self, connection: Request | WebSocket, handoff_id: str) -> tuple[Handoff | None, int]:
        user_id = await self._auth.signed_in_user(connection)
        if user_id is None:
            return None, CLOSE_SIGNED_OUT
        handoff = await self._handoffs.find(handoff_id)
        if handoff is None or handoff.user_id != user_id:
            return None, CLOSE_NOT_FOUND
        return handoff, 0

    async def socket(self, websocket: WebSocket) -> None:
        if not self._origin_allowed(websocket):
            await websocket.close(code=1008)  # before accepting: the upgrade is refused with 403
            return
        await websocket.accept()
        handoff, error = await self.authorise(websocket, websocket.path_params['handoff_id'])
        if handoff is None:
            await websocket.close(code=error)
            return
        slot = await self._claim(handoff.handoff_id)
        try:
            if slot.replaced.is_set():
                await websocket.close(code=CLOSE_REPLACED)
                return
            try:
                source = await self._service.live_view(
                    run_id=handoff.run_id, user_id=handoff.user_id, handoff_id=handoff.handoff_id
                )
            except (HandoffNotActive, UnknownRun):
                await websocket.close(code=CLOSE_ENDED)
                return
            activity = self._activity.setdefault(handoff.handoff_id, Activity())
            connection = _Connection(
                websocket=websocket,
                source=source,
                handoff=handoff,
                activity=activity,
                slot=slot,
                service=self._service,
                handoffs=self._handoffs,
            )
            try:
                await connection.run()
            finally:
                await source.close()
            if connection.over:
                self._activity.pop(handoff.handoff_id, None)
        finally:
            slot.done.set()
            if self._slots.get(handoff.handoff_id) is slot:
                del self._slots[handoff.handoff_id]

    async def _claim(self, handoff_id: HandoffId) -> _Slot:
        """Take the hand-off's one connection, closing the previous holder first."""
        slot = _Slot()
        previous = self._slots.get(handoff_id)
        self._slots[handoff_id] = slot
        if previous is not None:
            previous.replaced.set()
            with suppress(TimeoutError):
                await asyncio.wait_for(previous.done.wait(), timeout=10)
        return slot

    def _origin_allowed(self, websocket: WebSocket) -> bool:
        """Browsers always send `Origin` on a WebSocket upgrade, so a page on another site cannot use the user's
        cookies here. Clients that are not browsers send none."""
        origin = websocket.headers.get('origin')
        if origin is None or origin in self._allowed_origins:
            return True
        return urlsplit(origin).netloc == websocket.headers.get('host')


class _Connection:
    """One WebSocket driving one hand-off's browser, until it is given back, replaced, closed, or the hand-off ends."""

    def __init__(
        self,
        *,
        websocket: WebSocket,
        source: FrameSource,
        handoff: Handoff,
        activity: Activity,
        slot: _Slot,
        service: LiveViewService,
        handoffs: Handoffs,
    ) -> None:
        self._websocket = websocket
        self._source = source
        self._handoff = handoff
        self._activity = activity
        self._slot = slot
        self._service = service
        self._handoffs = handoffs
        self._send_lock = asyncio.Lock()
        self._giving_back = False
        self._closed = False
        self.over = False
        """The hand-off is over: given back here, or ended elsewhere."""

    async def run(self) -> None:
        await self._send(Hello(handoff_id=self._handoff.handoff_id, reason=self._handoff.reason))
        pump = asyncio.create_task(self._pump())
        read = asyncio.create_task(self._read())
        replaced = asyncio.create_task(self._slot.replaced.wait())
        tasks = (pump, read, replaced)
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if self._giving_back and not read.done():
                await read  # the give-back ends the source too; let it finish
            was_replaced, source_ended, disconnected = replaced.done(), pump.done(), read.done()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in (pump, read):
            if not task.cancelled() and (error := task.exception()) is not None:
                raise error
        if self._closed:
            return
        if self.over or (source_ended and not disconnected and not was_replaced):
            self.over = True
            await self._send(Ended(given_back=False))
            await self._close(CLOSE_ENDED)
        elif was_replaced:
            await self._close(CLOSE_REPLACED)

    async def _pump(self) -> None:
        """Frames and tabs to the page. Ends when the source does, which is when the hand-off ends."""
        seq = 0
        async for update in self._source.updates():
            if isinstance(update, Frame):
                seq += 1
                if not await self._send_bytes(encode_frame(update, seq)):
                    return
            else:
                self._activity.saw(update)
                if not await self._send(update):
                    return

    async def _read(self) -> None:
        """The page's input to the browser, until it disconnects or gives the browser back."""
        while True:
            message = await self._websocket.receive()
            if message['type'] == 'websocket.disconnect':
                return
            text = message.get('text')
            if not isinstance(text, str):
                continue
            try:
                decoded = decode_client(text)
            except WireError as error:
                await self._send(ErrorMessage(message=str(error)))
                continue
            try:
                match decoded:
                    case GiveBackRequest():
                        await self._give_back()
                        return
                    case SwitchTab(tab_id=tab_id):
                        await self._source.switch_tab(tab_id)
                    case ViewportSize():
                        await self._fit(decoded)
                    case _:
                        self._activity.record(decoded)
                        await self._source.send(decoded)
            except HandoffNotActive:
                self.over = True
                return
            except BrowserError as error:
                await self._send(ErrorMessage(message=str(error)))

    async def _fit(self, size: ViewportSize) -> None:
        """Lay the browser out for a phone, or give it its own size back. The source restores the size when it
        closes, so the agent never sees the phone layout."""
        small = size.width < PHONE_WIDTH
        viewport = Viewport(width=size.width, height=size.height, scale=size.scale) if small else None
        with suppress(NotSupported):  # Servo and polled engines: the picture is scaled to fit instead
            await self._source.set_viewport(viewport)

    async def _give_back(self) -> None:
        self._giving_back = True
        handoff = self._handoff
        try:
            ended = await self._service.end_handoff(
                run_id=handoff.run_id, user_id=handoff.user_id, handoff_id=handoff.handoff_id
            )
        except HandoffNotActive:
            self.over = True
            return
        summary = self._activity.summary(ended.url)
        await self._handoffs.given_back(GiveBack(handoff=handoff, ended=ended, summary=summary))
        self.over = True
        await self._send(Ended(given_back=True))
        await self._close(1000)

    async def _send(self, message: ServerMessage) -> bool:
        return await self._transmit(encode_server(message))

    async def _send_bytes(self, data: bytes) -> bool:
        return await self._transmit(data)

    async def _transmit(self, data: str | bytes) -> bool:
        """Send one message; False if the socket is gone."""
        async with self._send_lock:
            try:
                if isinstance(data, str):
                    await self._websocket.send_text(data)
                else:
                    await self._websocket.send_bytes(data)
            except (WebSocketDisconnect, RuntimeError, OSError):
                return False
        return True

    async def _close(self, code: int) -> None:
        self._closed = True
        async with self._send_lock:
            with suppress(WebSocketDisconnect, RuntimeError, OSError):
                await self._websocket.close(code=code)
