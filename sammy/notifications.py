"""Telling the user the bot needs them: a web push to each browser or phone they turned it on in, an email, and a
message in each chat app they linked and left pings on (`sammy.channels.outbound.notify`).

Sent when a run asks something (`sammy.approvals.open_ask`), when a scheduled task finishes, and when a watch finds
what the user waits for (`sammy.schedules`). The message says only which of these it is and links to the chat,
which needs signing in: no prompt, no page, no hand-off link. Either channel is off until it is
configured (`VAPID_*` for push, `SMTP_URL` for email). A failure to notify is logged by type and never fails the run.
"""

from __future__ import annotations

import asyncio
import base64
import json
import smtplib
from email.message import EmailMessage
from typing import Any
from urllib.parse import unquote, urlsplit

import logfire
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from psycopg.types.json import Jsonb
from pywebpush import WebPushException, webpush  # pyright: ignore[reportMissingTypeStubs, reportUnknownVariableType]

from sammy.channels import outbound
from sammy.db import Connection
from sammy.models import NoticeKind
from sammy.resources import Resources
from sammy.settings import Settings

WHAT = {
    'question': 'Sammy has a question for you.',
    'approval': 'Sammy needs your approval before it goes on.',
    'handoff': 'Sammy needs you to take over its browser for a moment.',
    'connect': 'Sammy needs you to connect an app to carry on.',
    'finished': 'Sammy finished a scheduled task.',
    'failed': 'Sammy could not finish a scheduled task.',
    'found': 'Sammy found what you asked it to watch for.',
}
SUBJECT = {
    'finished': 'Sammy finished a task',
    'failed': 'Sammy could not finish a task',
    'found': 'Sammy found something',
}


def new_vapid_keys() -> tuple[str, str]:
    """A private key and the public key the browser subscribes with, both base64url (DER, and the uncompressed point),
    as `VAPID_PRIVATE_KEY` and `VAPID_PUBLIC_KEY`."""
    key = ec.generate_private_key(ec.SECP256R1())
    der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    point = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64(der), _b64(point)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


MAX_SUBSCRIPTIONS = 10
"""Push subscriptions kept per user; adding one more drops the oldest."""


class TakenEndpoint(Exception):
    """The push endpoint is registered to another user."""


async def add_subscription(connection: Connection, user_id: str, endpoint: str, keys: dict[str, str]) -> None:
    """Save the subscription, or update its keys if the user has it already. Keeps the user's newest
    `MAX_SUBSCRIPTIONS`. Raises `TakenEndpoint` if another user registered this endpoint."""
    async with connection.transaction():
        cursor = await connection.execute(
            'INSERT INTO sammy.push_subscriptions (endpoint, user_id, keys) VALUES (%s, %s, %s) '
            'ON CONFLICT (endpoint) DO UPDATE SET keys = EXCLUDED.keys, created_at = now() '
            'WHERE sammy.push_subscriptions.user_id = EXCLUDED.user_id RETURNING endpoint',
            (endpoint, user_id, Jsonb(keys)),
        )
        if await cursor.fetchone() is None:
            raise TakenEndpoint(endpoint)
        await connection.execute(
            'DELETE FROM sammy.push_subscriptions WHERE user_id = %s AND endpoint NOT IN ('
            'SELECT endpoint FROM sammy.push_subscriptions WHERE user_id = %s ORDER BY created_at DESC LIMIT %s)',
            (user_id, user_id, MAX_SUBSCRIPTIONS),
        )


async def remove_subscription(connection: Connection, user_id: str, endpoint: str) -> bool:
    cursor = await connection.execute(
        'DELETE FROM sammy.push_subscriptions WHERE user_id = %s AND endpoint = %s', (user_id, endpoint)
    )
    return cursor.rowcount > 0


async def notify(resources: Resources, *, user_id: str, thread_id: str, kind: NoticeKind, tag: str) -> None:
    """Push, email and chat apps: the bot needs the user. Never raises: a failure is logged by type only."""
    try:
        # First: it only adds rows to the chat outbox, in the step that calls this, so a retry adds them once.
        await outbound.notify(resources, user_id=user_id, thread_id=thread_id, kind=kind, tag=tag, what=WHAT[kind])
    except Exception as error:  # noqa: BLE001  a notification must never fail the run
        logfire.warn('A chat notification was not queued: {error_type}', error_type=type(error).__name__)
    try:
        await _notify(resources, user_id=user_id, thread_id=thread_id, kind=kind, tag=tag)
    except Exception as error:  # noqa: BLE001  a notification must never fail the run
        logfire.warn('A notification was not sent: {error_type}', error_type=type(error).__name__)


async def _notify(resources: Resources, *, user_id: str, thread_id: str, kind: NoticeKind, tag: str) -> None:
    settings = resources.settings
    async with resources.pool.connection() as connection:
        cursor = await connection.execute('SELECT email FROM sammy.users WHERE id = %s', (user_id,))
        row = await cursor.fetchone()
        cursor = await connection.execute(
            'SELECT endpoint, keys FROM sammy.push_subscriptions WHERE user_id = %s ORDER BY created_at DESC LIMIT %s',
            (user_id, MAX_SUBSCRIPTIONS),
        )
        subscriptions = await cursor.fetchall()
    url = f'{settings.public_url}/#/t/{thread_id}'
    body = WHAT[kind]
    gone: list[str] = []
    if settings.vapid_private_key is not None and subscriptions:
        payload = json.dumps({'title': 'Sammy', 'body': body, 'url': url, 'tag': tag})
        delivered = await asyncio.gather(*(asyncio.to_thread(_push, settings, s, payload) for s in subscriptions))
        gone = [s['endpoint'] for s, ok in zip(subscriptions, delivered, strict=True) if not ok]
    if gone:
        async with resources.pool.connection() as connection:
            await connection.execute('DELETE FROM sammy.push_subscriptions WHERE endpoint = ANY(%s)', (gone,))
    if settings.smtp_url and row is not None:
        await asyncio.to_thread(_email, settings, row['email'], SUBJECT.get(kind, 'Sammy needs you'), body, url)


def _push(settings: Settings, subscription: dict[str, Any], payload: str) -> bool:
    """Send one push. False if the subscription is gone (the user turned it off), so it can be dropped."""
    assert settings.vapid_private_key is not None
    try:
        webpush(
            subscription_info={'endpoint': subscription['endpoint'], 'keys': subscription['keys']},
            data=payload,
            vapid_private_key=settings.vapid_private_key.get_secret_value(),
            vapid_claims={'sub': settings.vapid_subject},
            timeout=10,
        )
    except WebPushException as error:
        response: Any = getattr(error, 'response', None)  # pywebpush is untyped
        status: int | None = getattr(response, 'status_code', None)
        if status in (404, 410):
            return False
        logfire.warn('A push was not delivered: {status}', status=status)
    except Exception as error:  # noqa: BLE001  a notification must never fail the run
        logfire.warn('A push was not delivered: {error_type}', error_type=type(error).__name__)
    return True


def send_email(settings: Settings, to: str, subject: str, body: str) -> None:
    """One plain message, if email is set up; a failure is logged, never raised."""
    if settings.smtp_url:
        _email(settings, to, subject, body, url=None)


def _email(settings: Settings, to: str, subject: str, body: str, url: str | None) -> None:
    assert settings.smtp_url is not None
    parts = urlsplit(settings.smtp_url)
    message = EmailMessage()
    message['From'] = settings.mail_from
    message['To'] = to
    message['Subject'] = subject
    message.set_content(f'{body}\n\nOpen the chat: {url}\n' if url else f'{body}\n')
    try:
        smtp_class = smtplib.SMTP_SSL if parts.scheme == 'smtps' else smtplib.SMTP
        with smtp_class(parts.hostname or 'localhost', parts.port or 25, timeout=10) as smtp:
            if parts.scheme == 'smtp+starttls':
                smtp.starttls()
            if parts.username:
                smtp.login(unquote(parts.username), unquote(parts.password or ''))
            smtp.send_message(message)
    except Exception as error:  # noqa: BLE001  a notification must never fail the run
        logfire.warn('An email was not delivered: {error_type}', error_type=type(error).__name__)
