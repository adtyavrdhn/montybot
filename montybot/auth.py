"""Accounts: email and password, and a signed session cookie that names the user.

Passwords are hashed with scrypt from the standard library. The session cookie is Starlette's `SessionMiddleware`
(signed with `SESSION_SECRET`, HttpOnly, SameSite=Lax). Writes to the API need a JSON body, which a page on another
site cannot send with the user's cookie without a CORS preflight we never answer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from montybot import store
from montybot.models import User
from montybot.resources import Resources

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return f'scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}'


def check_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = encoded.split('$')
    except ValueError:
        return False
    if scheme != 'scrypt':
        return False
    actual = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p))
    return hmac.compare_digest(actual, _unb64(digest))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def sign_in(request: Request, user: User) -> None:
    request.session.clear()
    request.session['user_id'] = user.id


def sign_out(request: Request) -> None:
    request.session.clear()


async def signed_in_user(request: Request) -> User | None:
    user_id = request.session.get('user_id')
    if not isinstance(user_id, str):
        return None
    resources: Resources = request.state.resources
    async with resources.pool.connection() as connection:
        return await store.get_user(connection, user_id)


Handler = Callable[[Request, User], Awaitable[Response]]


def signed_in(handler: Handler) -> Callable[[Request], Awaitable[Response]]:
    """Answer 401 unless a user is signed in; refuse writes that are not JSON."""

    async def endpoint(request: Request) -> Response:
        user = await signed_in_user(request)
        if user is None:
            return JSONResponse({'detail': 'sign in first'}, status_code=401)
        if request.method not in ('GET', 'HEAD') and not _is_json(request):
            return JSONResponse({'detail': 'send JSON'}, status_code=415)
        return await handler(request, user)

    return endpoint


def _is_json(request: Request) -> bool:
    return request.headers.get('content-type', '').split(';')[0].strip() == 'application/json'
