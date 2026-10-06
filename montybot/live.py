"""The live view (#14, `montybot.liveview`) wired into the app: who is signed in, and what a give-back does.

```
web app: POST /api/runs/<run>/live     the run's active hand-off (renewed if the browser service lost it) -> its link
browser: /live/handoff/<id>            Mike's page and WebSocket, mounted in this app, behind the session cookie
  LiveAuth.signed_in_user              the session's user; only the run's user gets the page or the socket
  DbHandoffs.find(id)                  the hand-off ask that carries this id
  "Give back to the bot"               the browser service ends the hand-off and saves the sign-ins, then
  DbHandoffs.given_back(...)           answers the ask: the run wakes from DBOS.recv with a summary in words
```
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import HTTPConnection

from montybot import approvals, store
from montybot.browser.service import Handoff, HandoffId, UserId
from montybot.liveview.app import live_view_app
from montybot.liveview.handoffs import GiveBack
from montybot.resources import Resources


class LiveAuth:
    """The signed-in user, from the app's session cookie (Starlette's `SessionMiddleware` covers WebSockets too)."""

    async def signed_in_user(self, connection: HTTPConnection) -> UserId | None:
        if 'session' not in connection.scope:
            return None
        user_id = connection.session.get('user_id')
        return user_id if isinstance(user_id, str) else None


class DbHandoffs:
    """Hand-offs are asks of kind `handoff`; the id is in the ask's details."""

    def __init__(self, resources: Resources) -> None:
        self._resources = resources

    async def find(self, handoff_id: HandoffId) -> Handoff | None:
        async with self._resources.pool.connection() as connection:
            ask = await store.find_handoff(connection, handoff_id)
        if ask is None:
            return None
        return Handoff(handoff_id=handoff_id, run_id=ask.run_id, user_id=ask.user_id, reason=ask.prompt)

    async def given_back(self, given: GiveBack) -> None:
        async with self._resources.pool.connection() as connection:
            ask = await store.find_handoff(connection, given.handoff.handoff_id)
        if ask is not None:
            await approvals.answer(self._resources, ask.user_id, ask.id, {'done': True, 'note': given.summary})


def live_app(resources: Resources) -> Starlette:
    """The app's public origin may open the WebSocket too, for when a proxy in front changes the Host header."""
    public = urlsplit(resources.settings.public_url)
    return live_view_app(
        service=resources.browser,
        handoffs=DbHandoffs(resources),
        auth=LiveAuth(),
        allowed_origins=frozenset({f'{public.scheme}://{public.netloc}'}),
    )
