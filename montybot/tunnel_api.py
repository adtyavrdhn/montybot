"""`/api/tunnel`: the Mac app's tunnel, which carries the browser's connections for the runs its user starts.

How the tunnel works, and its wire, are in `montybot/browser/tunnel.py`. This is only the door. The user is the
session's, as everywhere else. Only clients that are not browsers may open it: a WebSocket with an `Origin` header is
refused, so no web page (this app's own included) can open one with the user's cookie and stand in for their Mac.

A second Mac of the same user takes over from the first, whose WebSocket is closed with `REPLACED`, so the apps know
not to take it straight back.

The Mac says where it is in two headers, `X-Monty-Timezone` (an IANA zone) and `X-Monty-Locale` (such as `en-CA`).
A browser going out through it takes them, so its clock and language agree with its address (`tunnel.Place`).
"""

from __future__ import annotations

import contextlib
import re

import logfire
from starlette.websockets import WebSocket, WebSocketDisconnect

from montybot.browser.tunnel import MacTunnel, Place
from montybot.resources import Resources
from montybot.schedules import is_timezone

REPLACED = 4000
"""The close code when another Mac of the same user opened its tunnel."""


LOCALE = re.compile(r'[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}')


def place_of(websocket: WebSocket) -> Place:
    """Where the Mac says it is; what is not a real zone, or not a language tag, is left out."""
    timezone = websocket.headers.get('x-monty-timezone')
    locale = websocket.headers.get('x-monty-locale')
    return Place(
        timezone=timezone if timezone and is_timezone(timezone) else None,
        locale=locale if locale and LOCALE.fullmatch(locale) else None,
    )


async def mac_tunnel(websocket: WebSocket) -> None:
    resources: Resources = websocket.state.resources
    tunnels = resources.tunnels
    user_id = websocket.session.get('user_id') if 'session' in websocket.scope else None
    if tunnels is None or not isinstance(user_id, str) or 'origin' in websocket.headers:
        await websocket.close(code=1008)  # policy violation: off, signed out, or a web page
        return
    await websocket.accept()

    async def hang_up() -> None:
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):  # it may be closing already
            await websocket.close(code=REPLACED)

    tunnel = MacTunnel(websocket.send_bytes, hang_up=hang_up, place=place_of(websocket))
    tunnels.attach(user_id, tunnel)
    logfire.info('Mac tunnel opened', user_id=user_id)
    try:
        while True:
            received = await websocket.receive()
            if received['type'] == 'websocket.disconnect':
                break
            if data := received.get('bytes'):
                await tunnel.received(data)
    finally:
        tunnels.detach(user_id, tunnel)
        logfire.info('Mac tunnel closed', user_id=user_id)
