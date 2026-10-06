"""Who is signed in: what the live view needs from the web app (#8), and a stand-in until it exists.

The hand-off link names only the hand-off. Whoever opens it must be signed in as the run's requester; the link
itself grants nothing, so a forwarded or leaked link is useless to anyone else.
"""

from __future__ import annotations

import secrets
from typing import Protocol

from starlette.requests import HTTPConnection

from montybot.browser.service import UserId

SESSION_COOKIE = 'montybot_session'


class Authenticator(Protocol):
    """Reads the signed-in user from a request or a WebSocket upgrade. The web app (#8) implements it."""

    async def signed_in_user(self, connection: HTTPConnection) -> UserId | None:
        """The user this connection is signed in as, or None."""
        ...


class StubAuthenticator:
    """Stands in for #8's sign-in: a random session cookie per signed-in user, kept in memory."""

    def __init__(self) -> None:
        self._sessions: dict[str, UserId] = {}

    def sign_in(self, user_id: UserId) -> str:
        """Start a session for `user_id` and return the value for the `montybot_session` cookie."""
        token = secrets.token_urlsafe(24)
        self._sessions[token] = user_id
        return token

    def sign_out(self, token: str) -> None:
        self._sessions.pop(token, None)

    async def signed_in_user(self, connection: HTTPConnection) -> UserId | None:
        token = connection.cookies.get(SESSION_COOKIE)
        return self._sessions.get(token) if token else None
