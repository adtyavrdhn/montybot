# Grown from viktor c1896df (viktor/store.py). Every function a request reaches takes the user's id and reads only
# that user's rows, so a caller cannot tell another user's thread, run or ask from one that does not exist.
# Functions without a user id (`load_run`, `load_history`, `append_history`, `set_run_status`, `finish_run`,
# `add_activity`) are for the run's own workflow, which starts from a run id the app created.
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from montybot.db import Connection
from montybot.models import Ask, AskKind, Run, RunStatus, Thread, Trigger, User


class ActiveRun(Exception):
    """The thread already has a run that has not finished."""


RUN_COLUMNS = 'id, user_id, thread_id, trigger, prompt, status, output, error'
ASK_COLUMNS = 'id, run_id, user_id, occurrence, kind, prompt, details, answer'

# --- users ---


async def create_user(connection: Connection, email: str, password_hash: str, name: str = '') -> User | None:
    """None when the email is taken."""
    cursor = await connection.execute(
        'INSERT INTO montybot.users (email, password_hash, name) VALUES (%s, %s, %s) '
        'ON CONFLICT (email) DO NOTHING RETURNING id, email, name',
        (email, password_hash, name),
    )
    row = await cursor.fetchone()
    return None if row is None else user_from(row)


async def find_login(connection: Connection, email: str) -> tuple[User, str] | None:
    cursor = await connection.execute(
        'SELECT id, email, name, password_hash FROM montybot.users WHERE email = %s', (email,)
    )
    row = await cursor.fetchone()
    return None if row is None else (user_from(row), row['password_hash'])


async def get_user(connection: Connection, user_id: str) -> User | None:
    cursor = await connection.execute('SELECT id, email, name FROM montybot.users WHERE id = %s', (user_id,))
    row = await cursor.fetchone()
    return None if row is None else user_from(row)


# --- threads ---


async def create_thread(connection: Connection, user_id: str, title: str) -> Thread:
    cursor = await connection.execute(
        'INSERT INTO montybot.threads (user_id, title) VALUES (%s, %s) RETURNING id, user_id, title',
        (user_id, title[:120]),
    )
    row = await cursor.fetchone()
    assert row is not None
    return thread_from(row)


async def get_thread(connection: Connection, user_id: str, thread_id: str) -> Thread | None:
    cursor = await connection.execute(
        'SELECT id, user_id, title FROM montybot.threads WHERE id = %s AND user_id = %s', (thread_id, user_id)
    )
    row = await cursor.fetchone()
    return None if row is None else thread_from(row)


async def list_threads(connection: Connection, user_id: str) -> list[Thread]:
    cursor = await connection.execute(
        'SELECT id, user_id, title FROM montybot.threads WHERE user_id = %s ORDER BY created_at DESC', (user_id,)
    )
    return [thread_from(row) for row in await cursor.fetchall()]


# --- runs ---


async def create_run(
    connection: Connection, *, run_id: str, user_id: str, thread_id: str, prompt: str, trigger: Trigger
) -> Run:
    """Raises `ActiveRun` when the thread has an unfinished run. Call inside a transaction."""
    try:
        async with connection.transaction():
            cursor = await connection.execute(
                f'INSERT INTO montybot.runs (id, user_id, thread_id, trigger, prompt) VALUES (%s, %s, %s, %s, %s) '
                f'RETURNING {RUN_COLUMNS}',
                (run_id, user_id, thread_id, trigger, prompt),
            )
    except UniqueViolation as error:
        raise ActiveRun('this thread is still working on the last message') from error
    row = await cursor.fetchone()
    assert row is not None
    return run_from(row)


async def get_run(connection: Connection, user_id: str, run_id: str) -> Run | None:
    cursor = await connection.execute(
        f'SELECT {RUN_COLUMNS} FROM montybot.runs WHERE id = %s AND user_id = %s', (run_id, user_id)
    )
    row = await cursor.fetchone()
    return None if row is None else run_from(row)


async def latest_run(connection: Connection, user_id: str, thread_id: str) -> Run | None:
    cursor = await connection.execute(
        f'SELECT {RUN_COLUMNS} FROM montybot.runs WHERE thread_id = %s AND user_id = %s '
        'ORDER BY created_at DESC LIMIT 1',
        (thread_id, user_id),
    )
    row = await cursor.fetchone()
    return None if row is None else run_from(row)


async def load_run(connection: Connection, run_id: str) -> Run:
    cursor = await connection.execute(f'SELECT {RUN_COLUMNS} FROM montybot.runs WHERE id = %s', (run_id,))
    row = await cursor.fetchone()
    assert row is not None, 'a workflow starts only for a run the app recorded'
    return run_from(row)


async def set_run_status(connection: Connection, run_id: str, status: RunStatus) -> None:
    await connection.execute(
        "UPDATE montybot.runs SET status = %s WHERE id = %s AND status NOT IN ('done', 'failed')", (status, run_id)
    )


async def finish_run(
    connection: Connection, run_id: str, status: RunStatus, *, output: str | None = None, error: str | None = None
) -> None:
    await connection.execute(
        'UPDATE montybot.runs SET status = %s, output = %s, error = %s, completed_at = now() WHERE id = %s',
        (status, output, error, run_id),
    )


# --- history ---


async def load_history(connection: Connection, thread_id: str) -> list[ModelMessage]:
    cursor = await connection.execute(
        'SELECT payload FROM montybot.messages WHERE thread_id = %s ORDER BY position', (thread_id,)
    )
    payloads = [row['payload'] for row in await cursor.fetchall()]
    return ModelMessagesTypeAdapter.validate_python(payloads)


async def append_history(connection: Connection, thread_id: str, messages: Sequence[ModelMessage]) -> None:
    cursor = await connection.execute(
        'SELECT coalesce(max(position), -1) AS last FROM montybot.messages WHERE thread_id = %s', (thread_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    payloads: list[Any] = json.loads(ModelMessagesTypeAdapter.dump_json(list(messages)))
    for offset, payload in enumerate(payloads, start=row['last'] + 1):
        await connection.execute(
            'INSERT INTO montybot.messages (thread_id, position, payload) VALUES (%s, %s, %s)',
            (thread_id, offset, Jsonb(payload)),
        )


async def list_history(connection: Connection, user_id: str, thread_id: str) -> list[ModelMessage] | None:
    """The thread's messages, or None if the thread is not the user's."""
    if await get_thread(connection, user_id, thread_id) is None:
        return None
    return await load_history(connection, thread_id)


# --- asks ---


async def create_ask(
    connection: Connection,
    *,
    ask_id: str,
    run_id: str,
    user_id: str,
    occurrence: int,
    kind: AskKind,
    prompt: str,
    details: dict[str, Any],
) -> None:
    """Idempotent: a retried step finds the ask it made the first time."""
    await connection.execute(
        'INSERT INTO montybot.asks (id, run_id, user_id, occurrence, kind, prompt, details) '
        'VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING',
        (ask_id, run_id, user_id, occurrence, kind, prompt, Jsonb(details)),
    )


async def open_ask(connection: Connection, user_id: str, run_id: str) -> Ask | None:
    cursor = await connection.execute(
        f'SELECT {ASK_COLUMNS} FROM montybot.asks WHERE run_id = %s AND user_id = %s AND answer IS NULL '
        'ORDER BY occurrence DESC LIMIT 1',
        (run_id, user_id),
    )
    row = await cursor.fetchone()
    return None if row is None else ask_from(row)


async def get_ask(connection: Connection, user_id: str, ask_id: str) -> Ask | None:
    cursor = await connection.execute(
        f'SELECT {ASK_COLUMNS} FROM montybot.asks WHERE id = %s AND user_id = %s', (ask_id, user_id)
    )
    row = await cursor.fetchone()
    return None if row is None else ask_from(row)


async def answer_ask(connection: Connection, user_id: str, ask_id: str, answer: dict[str, Any]) -> Ask | None:
    """Record the answer once. None if the ask is not the user's or was answered already."""
    cursor = await connection.execute(
        f'UPDATE montybot.asks SET answer = %s, answered_at = now() '
        f'WHERE id = %s AND user_id = %s AND answer IS NULL RETURNING {ASK_COLUMNS}',
        (Jsonb(answer), ask_id, user_id),
    )
    row = await cursor.fetchone()
    return None if row is None else ask_from(row)


async def expire_ask(connection: Connection, ask_id: str) -> None:
    await connection.execute(
        'UPDATE montybot.asks SET answer = \'{"expired": true}\', answered_at = now() WHERE id = %s AND answer IS NULL',
        (ask_id,),
    )


# --- activity ---


async def add_activity(connection: Connection, run_id: str, text: str) -> None:
    await connection.execute('INSERT INTO montybot.activity (run_id, text) VALUES (%s, %s)', (run_id, text))


async def list_activity(connection: Connection, user_id: str, run_id: str) -> list[str]:
    cursor = await connection.execute(
        'SELECT a.text FROM montybot.activity a JOIN montybot.runs r ON r.id = a.run_id '
        'WHERE a.run_id = %s AND r.user_id = %s ORDER BY a.id',
        (run_id, user_id),
    )
    return [row['text'] for row in await cursor.fetchall()]


# --- rows ---


def user_from(row: dict[str, Any]) -> User:
    return User(id=str(row['id']), email=row['email'], name=row['name'])


def thread_from(row: dict[str, Any]) -> Thread:
    return Thread(id=str(row['id']), user_id=str(row['user_id']), title=row['title'])


def run_from(row: dict[str, Any]) -> Run:
    return Run(
        id=str(row['id']),
        user_id=str(row['user_id']),
        thread_id=str(row['thread_id']),
        trigger=row['trigger'],
        prompt=row['prompt'],
        status=row['status'],
        output=row['output'],
        error=row['error'],
    )


def ask_from(row: dict[str, Any]) -> Ask:
    return Ask(
        id=str(row['id']),
        run_id=str(row['run_id']),
        user_id=str(row['user_id']),
        occurrence=row['occurrence'],
        kind=row['kind'],
        prompt=row['prompt'],
        details=row['details'],
        answer=row['answer'],
    )
