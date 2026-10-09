"""The SQL of the chat tables (`migrations/0013_channels.sql`). Kept apart from `sammy/store.py`, which is long
enough already. Functions a web request reaches take the user's id and touch only that user's rows."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from psycopg.types.json import Jsonb

from sammy.channels.base import Outgoing
from sammy.db import Connection


@dataclass(frozen=True, kw_only=True)
class Identity:
    channel: str
    external_user_id: str
    user_id: str
    notify_chat_id: str | None
    notify: bool
    linked_at: datetime


@dataclass(frozen=True, kw_only=True)
class Chat:
    channel: str
    chat_id: str


@dataclass(frozen=True, kw_only=True)
class OutboxRow:
    id: str
    channel: str
    chat_id: str
    user_id: str | None
    outgoing: Outgoing
    sent: bool


IDENTITY_COLUMNS = 'channel, external_user_id, user_id, notify_chat_id, notify, linked_at'


def _identity(row: dict[str, object]) -> Identity:
    notify_chat_id = row['notify_chat_id']
    linked_at = row['linked_at']
    assert isinstance(linked_at, datetime)
    return Identity(
        channel=str(row['channel']),
        external_user_id=str(row['external_user_id']),
        user_id=str(row['user_id']),
        notify_chat_id=None if notify_chat_id is None else str(notify_chat_id),
        notify=bool(row['notify']),
        linked_at=linked_at,
    )


# --- identities ---


async def identity(connection: Connection, channel: str, external_user_id: str) -> Identity | None:
    cursor = await connection.execute(
        f'SELECT {IDENTITY_COLUMNS} FROM sammy.channel_identities WHERE channel = %s AND external_user_id = %s',
        (channel, external_user_id),
    )
    row = await cursor.fetchone()
    return None if row is None else _identity(row)


async def link(
    connection: Connection, *, channel: str, external_user_id: str, user_id: str, notify_chat_id: str | None
) -> None:
    """Tie the platform account to the user, replacing whichever user it was tied to."""
    await connection.execute(
        'INSERT INTO sammy.channel_identities (channel, external_user_id, user_id, notify_chat_id) '
        'VALUES (%s, %s, %s, %s) ON CONFLICT (channel, external_user_id) DO UPDATE SET user_id = EXCLUDED.user_id, '
        'notify_chat_id = coalesce(EXCLUDED.notify_chat_id, sammy.channel_identities.notify_chat_id), '
        'notify = true, linked_at = now()',
        (channel, external_user_id, user_id, notify_chat_id),
    )


async def identities_of(connection: Connection, user_id: str) -> list[Identity]:
    cursor = await connection.execute(
        f'SELECT {IDENTITY_COLUMNS} FROM sammy.channel_identities WHERE user_id = %s ORDER BY channel, linked_at',
        (user_id,),
    )
    return [_identity(row) for row in await cursor.fetchall()]


async def set_notify(connection: Connection, user_id: str, channel: str, notify: bool) -> bool:
    cursor = await connection.execute(
        'UPDATE sammy.channel_identities SET notify = %s WHERE user_id = %s AND channel = %s',
        (notify, user_id, channel),
    )
    return cursor.rowcount > 0


async def unlink(connection: Connection, user_id: str, channel: str) -> bool:
    """With the chats it mapped, so a new link starts new threads; the threads themselves stay on the web."""
    cursor = await connection.execute(
        'DELETE FROM sammy.channel_identities WHERE user_id = %s AND channel = %s', (user_id, channel)
    )
    await connection.execute('DELETE FROM sammy.channel_chats WHERE user_id = %s AND channel = %s', (user_id, channel))
    return cursor.rowcount > 0


# --- link codes (sammy.channels.linking) ---


async def drop_expired_codes(connection: Connection) -> None:
    await connection.execute('DELETE FROM sammy.channel_link_codes WHERE expires_at <= now()')


async def sender_nonce(connection: Connection, channel: str, external_user_id: str) -> str | None:
    cursor = await connection.execute(
        'SELECT nonce FROM sammy.channel_link_codes WHERE channel = %s AND external_user_id = %s '
        'AND expires_at > now() ORDER BY expires_at DESC LIMIT 1',
        (channel, external_user_id),
    )
    row = await cursor.fetchone()
    return None if row is None else str(row['nonce'])


async def add_code(
    connection: Connection,
    *,
    code_hash: str,
    channel: str,
    minutes: int,
    external_user_id: str | None = None,
    chat_id: str | None = None,
    nonce: str | None = None,
    user_id: str | None = None,
) -> None:
    await connection.execute(
        'INSERT INTO sammy.channel_link_codes (code_hash, channel, external_user_id, chat_id, nonce, user_id, '
        "expires_at) VALUES (%s, %s, %s, %s, %s, %s, now() + %s * interval '1 minute')",
        (code_hash, channel, external_user_id, chat_id, nonce, user_id, minutes),
    )


async def take_sender_code(connection: Connection, code_hash: str) -> tuple[str, str, str | None] | None:
    """Use up a live code issued to a sender: its channel, sender and chat."""
    cursor = await connection.execute(
        'DELETE FROM sammy.channel_link_codes WHERE code_hash = %s AND external_user_id IS NOT NULL '
        'AND expires_at > now() RETURNING channel, external_user_id, chat_id',
        (code_hash,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return str(row['channel']), str(row['external_user_id']), row['chat_id']


async def take_user_code(connection: Connection, channel: str, code_hash: str) -> str | None:
    """Use up a live code issued to a web user for this channel: the user's id."""
    cursor = await connection.execute(
        'DELETE FROM sammy.channel_link_codes WHERE code_hash = %s AND channel = %s AND user_id IS NOT NULL '
        'AND expires_at > now() RETURNING user_id',
        (code_hash, channel),
    )
    row = await cursor.fetchone()
    return None if row is None else str(row['user_id'])


# --- chats and runs ---


async def thread_of_chat(connection: Connection, channel: str, chat_id: str, user_id: str) -> str | None:
    cursor = await connection.execute(
        'SELECT thread_id FROM sammy.channel_chats WHERE channel = %s AND chat_id = %s AND user_id = %s',
        (channel, chat_id, user_id),
    )
    row = await cursor.fetchone()
    return None if row is None else str(row['thread_id'])


async def add_chat(connection: Connection, *, channel: str, chat_id: str, user_id: str, thread_id: str) -> None:
    await connection.execute(
        'INSERT INTO sammy.channel_chats (channel, chat_id, user_id, thread_id) VALUES (%s, %s, %s, %s) '
        'ON CONFLICT (channel, chat_id, user_id) DO UPDATE SET thread_id = EXCLUDED.thread_id',
        (channel, chat_id, user_id, thread_id),
    )


async def add_run(connection: Connection, run_id: str, channel: str, chat_id: str) -> None:
    await connection.execute(
        'INSERT INTO sammy.channel_runs (run_id, channel, chat_id) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING',
        (run_id, channel, chat_id),
    )


async def chat_of_run(connection: Connection, run_id: str) -> Chat | None:
    cursor = await connection.execute('SELECT channel, chat_id FROM sammy.channel_runs WHERE run_id = %s', (run_id,))
    row = await cursor.fetchone()
    return None if row is None else Chat(channel=str(row['channel']), chat_id=str(row['chat_id']))


# --- the outbox (sammy.channels.outbound) ---


def outbox_id(key: str) -> str:
    """The row for `key` (`reply:<run id>`, `ask:<ask id>`, ...): the same key is the same row."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'sammy:channel:{key}'))


async def enqueue(
    connection: Connection,
    key: str,
    chat: Chat,
    outgoing: Outgoing,
    *,
    user_id: str | None = None,
    ask_id: str | None = None,
) -> None:
    await connection.execute(
        'INSERT INTO sammy.channel_outbox (id, channel, chat_id, user_id, ask_id, payload) '
        'VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING',
        (outbox_id(key), chat.channel, chat.chat_id, user_id, ask_id, Jsonb(outgoing.model_dump(mode='json'))),
    )


async def next_rows(connection: Connection, channels: Sequence[str]) -> list[str]:
    """The oldest unsent row of each chat, so each chat's messages go out one at a time, in order."""
    cursor = await connection.execute(
        'SELECT DISTINCT ON (channel, chat_id) id FROM sammy.channel_outbox '
        'WHERE sent_at IS NULL AND failed_at IS NULL AND channel = ANY(%s) ORDER BY channel, chat_id, seq',
        (list(channels),),
    )
    return [str(row['id']) for row in await cursor.fetchall()]


async def load_row(connection: Connection, row_id: str) -> OutboxRow | None:
    cursor = await connection.execute(
        'SELECT id, channel, chat_id, user_id, payload, sent_at IS NOT NULL OR failed_at IS NOT NULL AS done '
        'FROM sammy.channel_outbox WHERE id = %s',
        (row_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    user_id = row['user_id']
    return OutboxRow(
        id=str(row['id']),
        channel=str(row['channel']),
        chat_id=str(row['chat_id']),
        user_id=None if user_id is None else str(user_id),
        outgoing=Outgoing.model_validate(row['payload']),
        sent=bool(row['done']),
    )


async def mark_sent(connection: Connection, row_id: str, message_ids: Sequence[str], forget_text: bool) -> None:
    """`forget_text`: the text held a link code, which is not kept once sent."""
    await connection.execute(
        'UPDATE sammy.channel_outbox SET sent_at = now(), message_ids = %s, '
        "payload = CASE WHEN %s THEN jsonb_set(payload, '{text}', '\"\"') ELSE payload END WHERE id = %s",
        (Jsonb(list(message_ids)), forget_text, row_id),
    )


async def mark_failed(connection: Connection, row_id: str, error_type: str) -> None:
    await connection.execute(
        'UPDATE sammy.channel_outbox SET failed_at = now(), error = %s WHERE id = %s', (error_type, row_id)
    )


async def ask_message(connection: Connection, ask_id: str) -> tuple[Chat, str] | None:
    """Where the ask was sent, and the id of its last part, which carries the buttons."""
    cursor = await connection.execute(
        'SELECT channel, chat_id, message_ids FROM sammy.channel_outbox WHERE ask_id = %s AND sent_at IS NOT NULL '
        'ORDER BY seq LIMIT 1',
        (ask_id,),
    )
    row = await cursor.fetchone()
    ids = row['message_ids'] if row is not None else None
    if row is None or not isinstance(ids, list) or not ids:
        return None
    return Chat(channel=str(row['channel']), chat_id=str(row['chat_id'])), str(ids[-1])  # pyright: ignore[reportUnknownArgumentType]
