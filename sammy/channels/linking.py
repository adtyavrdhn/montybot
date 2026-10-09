"""Tying a platform account to a Sammy user with a one-time code, either way round.

- An unknown sender messages the bot directly. They get a link to `/#/link/<code>`, which they open in the web app
  while signed in (`POST /api/channels/link`). That does not link yet: the web app shows a second code, which only
  that sender can send to the bot to finish. Otherwise anyone could send their own link to a signed-in user and, once
  opened, use Sammy as that user from their own chat account.
- A signed-in web user asks for a code (`POST /api/channels/<name>/code`) and sends it to the bot (`/start <code>`).

Either way, linking ends with a code shown to the signed-in user arriving from the chat account: both sides proved.

Codes work for `CODE_MINUTES`, once. Only their SHA-256 is stored. A sender who writes again while their code is live
gets the same code: it is derived from a stored random nonce with the session secret, so it is never stored itself.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

from sammy.channels import store
from sammy.db import Connection

CODE_MINUTES = 15
CODE_LENGTH = 10
"""Base32 characters: 50 bits, for a code that lives 15 minutes and is used once."""
_CODE = re.compile(r'^(?:/start\s+)?([A-Za-z2-7]{10})$')


def _encode(data: bytes) -> str:
    return base64.b32encode(data).decode()[:CODE_LENGTH]


def hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def code_in(text: str) -> str | None:
    """The code in a message that is only a code (`CODE` or `/start CODE`)."""
    match = _CODE.match(text.strip())
    return match.group(1).upper() if match else None


async def code_for_sender(
    connection: Connection, secret: str, *, channel: str, external_user_id: str, chat_id: str
) -> str:
    """The sender's live code, or a new one."""
    await store.drop_expired_codes(connection)
    nonce = await store.sender_nonce(connection, channel, external_user_id)
    if nonce is not None:
        return _derived(secret, nonce)
    nonce = secrets.token_hex(16)
    code = _derived(secret, nonce)
    await store.add_code(
        connection,
        code_hash=hash_code(code),
        channel=channel,
        minutes=CODE_MINUTES,
        external_user_id=external_user_id,
        chat_id=chat_id,
        nonce=nonce,
    )
    return code


def _derived(secret: str, nonce: str) -> str:
    return _encode(hmac.new(secret.encode(), f'sammy:link:{nonce}'.encode(), hashlib.sha256).digest())


async def code_for_user(connection: Connection, user_id: str, channel: str, *, sender: str | None = None) -> str:
    """A new code for a signed-in user to send to the bot on `channel`; only from `sender`'s account, if given."""
    await store.drop_expired_codes(connection)
    code = _encode(secrets.token_bytes(10))
    await store.add_code(
        connection,
        code_hash=hash_code(code),
        channel=channel,
        minutes=CODE_MINUTES,
        user_id=user_id,
        external_user_id=sender,
    )
    return code
