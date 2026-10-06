"""Telling the user the bot needs them: a web push to each browser or phone they turned it on in, and an email.

Sent when a run asks something (`montybot.approvals.open_ask`), when a scheduled task finishes, and when a watch finds
what the user waits for (`montybot.schedules`). The message says only which of these it is and links to the chat,
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

from montybot.db import Connection
from montybot.models import NoticeKind
from montybot.resources import Resources
from montybot.settings import Settings

WHAT = {
    'question': 'monty-bot has a question for you.',
    'approval': 'monty-bot needs your approval before it goes on.',
    'handoff': 'monty-bot needs you to take over its browser for a moment.',
    'finished': 'monty-bot finished a scheduled task.',
    'failed': 'monty-bot could not finish a scheduled task.',
    'found': 'monty-bot found what you asked it to watch for.',
}
SUBJECT = {
    'finished': 'monty-bot finished a task',
    'failed': 'monty-bot could not finish a task',
    'found': 'monty-bot found something',
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


async def add_subscription(connection: Connection, user_id: str, endpoint: str, keys: dict[str, str]) -> None:
    await connection.execute(
        'INSERT INTO montybot.push_subscriptions (endpoint, user_id, keys) VALUES (%s, %s, %s) '
        'ON CONFLICT (endpoint) DO UPDATE SET user_id = EXCLUDED.user_id, keys = EXCLUDED.keys',
        (endpoint, user_id, Jsonb(keys)),
    )


async def notify(resources: Resources, *, user_id: str, thread_id: str, kind: NoticeKind, tag: str) -> None:
    settings = resources.settings
    async with resources.pool.connection() as connection:
        cursor = await connection.execute('SELECT email FROM montybot.users WHERE id = %s', (user_id,))
        row = await cursor.fetchone()
        cursor = await connection.execute(
            'SELECT endpoint, keys FROM montybot.push_subscriptions WHERE user_id = %s', (user_id,)
        )
        subscriptions = await cursor.fetchall()
    url = f'{settings.public_url}/#/t/{thread_id}'
    body = WHAT[kind]
    gone: list[str] = []
    if settings.vapid_private_key is not None:
        payload = json.dumps({'title': 'monty-bot', 'body': body, 'url': url, 'tag': tag})
        for subscription in subscriptions:
            if not await asyncio.to_thread(_push, settings, subscription, payload):
                gone.append(subscription['endpoint'])
    if gone:
        async with resources.pool.connection() as connection:
            await connection.execute('DELETE FROM montybot.push_subscriptions WHERE endpoint = ANY(%s)', (gone,))
    if settings.smtp_url and row is not None:
        await asyncio.to_thread(_email, settings, row['email'], SUBJECT.get(kind, 'monty-bot needs you'), body, url)


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


def _email(settings: Settings, to: str, subject: str, body: str, url: str) -> None:
    assert settings.smtp_url is not None
    parts = urlsplit(settings.smtp_url)
    message = EmailMessage()
    message['From'] = settings.mail_from
    message['To'] = to
    message['Subject'] = subject
    message.set_content(f'{body}\n\nOpen the chat: {url}\n')
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
