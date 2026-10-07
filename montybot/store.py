# Grown from viktor c1896df (viktor/store.py). Every function a request reaches takes the user's id and reads only
# that user's rows, so a caller cannot tell another user's thread, run or ask from one that does not exist.
# Functions without a user id (`load_run`, `load_history`, `append_history`, `set_run_status`, `finish_run`,
# `add_activity`, `load_schedule`, `schedule_of_thread`) are for the run's own workflow, which starts from a run id
# the app created, or for a schedule's occurrence, which starts from the schedule's id.
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from montybot.db import Connection
from montybot.models import FINISHED, Ask, AskKind, Run, RunStatus, Schedule, Thread, Trigger, User


class ActiveRun(Exception):
    """The thread already has a run that has not finished."""


USER_COLUMNS = 'id, email, name, timezone'
RUN_COLUMNS = 'id, user_id, thread_id, trigger, prompt, status, output, error'
ASK_COLUMNS = 'id, run_id, user_id, occurrence, kind, prompt, details, answer'
SCHEDULE_COLUMNS = 'id, user_id, thread_id, name, cron, timezone, when_text, prompt, watch'

# --- users ---


async def create_user(connection: Connection, email: str, password_hash: str, name: str = '') -> User | None:
    """None when the email is taken."""
    cursor = await connection.execute(
        'INSERT INTO montybot.users (email, password_hash, name) VALUES (%s, %s, %s) '
        f'ON CONFLICT (email) DO NOTHING RETURNING {USER_COLUMNS}',
        (email, password_hash, name),
    )
    row = await cursor.fetchone()
    return None if row is None else user_from(row)


async def find_login(connection: Connection, email: str) -> tuple[User, str] | None:
    cursor = await connection.execute(
        f'SELECT {USER_COLUMNS}, password_hash FROM montybot.users WHERE email = %s', (email,)
    )
    row = await cursor.fetchone()
    return None if row is None else (user_from(row), row['password_hash'])


async def start_password_reset(connection: Connection, user_id: str, code_hash: str, minutes: int) -> None:
    """A new code replaces any earlier one."""
    await connection.execute(
        'INSERT INTO montybot.password_resets (user_id, code_hash, expires_at) '
        "VALUES (%s, %s, now() + %s * interval '1 minute') ON CONFLICT (user_id) DO UPDATE "
        'SET code_hash = EXCLUDED.code_hash, expires_at = EXCLUDED.expires_at, attempts = 0',
        (user_id, code_hash, minutes),
    )


async def password_reset(connection: Connection, user_id: str, max_attempts: int) -> str | None:
    """The live code's hash, counting this attempt; None once it has expired or been tried too often."""
    cursor = await connection.execute(
        'UPDATE montybot.password_resets SET attempts = attempts + 1 '
        'WHERE user_id = %s AND expires_at > now() AND attempts < %s RETURNING code_hash',
        (user_id, max_attempts),
    )
    row = await cursor.fetchone()
    return None if row is None else row['code_hash']


async def finish_password_reset(connection: Connection, user_id: str, password_hash: str) -> None:
    await connection.execute('UPDATE montybot.users SET password_hash = %s WHERE id = %s', (password_hash, user_id))
    await connection.execute('DELETE FROM montybot.password_resets WHERE user_id = %s', (user_id,))


async def get_user(connection: Connection, user_id: str) -> User | None:
    cursor = await connection.execute(f'SELECT {USER_COLUMNS} FROM montybot.users WHERE id = %s', (user_id,))
    row = await cursor.fetchone()
    return None if row is None else user_from(row)


async def set_timezone(connection: Connection, user_id: str, timezone: str) -> None:
    await connection.execute('UPDATE montybot.users SET timezone = %s WHERE id = %s', (timezone, user_id))


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
    """The user's threads, the one with the latest message first."""
    cursor = await connection.execute(
        'SELECT t.id, t.user_id, t.title FROM montybot.threads t WHERE t.user_id = %s '
        'ORDER BY (SELECT max(r.created_at) FROM montybot.runs r WHERE r.thread_id = t.id) DESC NULLS LAST, '
        't.created_at DESC',
        (user_id,),
    )
    return [thread_from(row) for row in await cursor.fetchall()]


async def active_runs(connection: Connection, user_id: str) -> dict[str, RunStatus]:
    """The status of each of the user's unfinished runs, by thread id."""
    cursor = await connection.execute(
        # The same condition as the index runs_active_by_user.
        "SELECT thread_id, status FROM montybot.runs WHERE user_id = %s AND status IN ('queued', 'running', 'waiting')",
        (user_id,),
    )
    return {str(row['thread_id']): row['status'] for row in await cursor.fetchall()}


async def latest_outcomes(connection: Connection, user_id: str) -> dict[str, RunStatus]:
    """How each of the user's threads' latest run ended (`done`, `failed` or `stopped`), by thread id; threads whose
    latest run is unfinished or that have none are left out."""
    cursor = await connection.execute(
        'SELECT DISTINCT ON (thread_id) thread_id, status FROM montybot.runs WHERE user_id = %s '
        'ORDER BY thread_id, created_at DESC',
        (user_id,),
    )
    return {str(row['thread_id']): row['status'] for row in await cursor.fetchall() if row['status'] in FINISHED}


async def rename_thread(connection: Connection, user_id: str, thread_id: str, title: str) -> bool:
    cursor = await connection.execute(
        'UPDATE montybot.threads SET title = %s WHERE id = %s AND user_id = %s', (title[:120], thread_id, user_id)
    )
    return cursor.rowcount == 1


async def delete_thread(connection: Connection, user_id: str, thread_id: str) -> bool:
    """With its messages, runs and their asks and activity, and its schedule (cascades)."""
    cursor = await connection.execute(
        'DELETE FROM montybot.threads WHERE id = %s AND user_id = %s', (thread_id, user_id)
    )
    return cursor.rowcount == 1


async def thread_schedule(connection: Connection, user_id: str, thread_id: str) -> Schedule | None:
    cursor = await connection.execute(
        f'SELECT {SCHEDULE_COLUMNS} FROM montybot.schedules WHERE thread_id = %s AND user_id = %s',
        (thread_id, user_id),
    )
    row = await cursor.fetchone()
    return None if row is None else schedule_from(row)


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


async def list_runs(connection: Connection, user_id: str, thread_id: str) -> list[Run]:
    """The thread's runs, oldest first."""
    cursor = await connection.execute(
        f'SELECT {RUN_COLUMNS} FROM montybot.runs WHERE thread_id = %s AND user_id = %s ORDER BY created_at',
        (thread_id, user_id),
    )
    return [run_from(row) for row in await cursor.fetchall()]


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
        'UPDATE montybot.runs SET status = %s WHERE id = %s AND NOT status = ANY(%s)', (status, run_id, list(FINISHED))
    )


async def lock_finished(connection: Connection, run_id: str) -> bool:
    """Lock the run's row for this transaction; True if it has finished already (done, failed or stopped)."""
    cursor = await connection.execute('SELECT status FROM montybot.runs WHERE id = %s FOR UPDATE', (run_id,))
    row = await cursor.fetchone()
    return row is not None and row['status'] in FINISHED


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


async def list_answered_asks(connection: Connection, user_id: str, thread_id: str) -> list[Ask]:
    """The thread's asks that have an answer (or expired), in the order they were asked."""
    cursor = await connection.execute(
        f'SELECT {", ".join("a." + column for column in ASK_COLUMNS.split(", "))} FROM montybot.asks a '
        'JOIN montybot.runs r ON r.id = a.run_id '
        'WHERE r.thread_id = %s AND a.user_id = %s AND a.answer IS NOT NULL ORDER BY r.created_at, a.occurrence',
        (thread_id, user_id),
    )
    return [ask_from(row) for row in await cursor.fetchall()]


async def close_open_asks(connection: Connection, run_id: str) -> None:
    """Close what a finished run still asks, so a late answer (from an old notification) is refused."""
    await connection.execute(
        'UPDATE montybot.asks SET answer = \'{"closed": true}\', answered_at = now() '
        'WHERE run_id = %s AND answer IS NULL',
        (run_id,),
    )


async def get_ask(connection: Connection, user_id: str, ask_id: str) -> Ask | None:
    cursor = await connection.execute(
        f'SELECT {ASK_COLUMNS} FROM montybot.asks WHERE id = %s AND user_id = %s', (ask_id, user_id)
    )
    row = await cursor.fetchone()
    return None if row is None else ask_from(row)


async def handoff_of(connection: Connection, ask_id: str) -> str | None:
    cursor = await connection.execute("SELECT details->>'handoff_id' AS id FROM montybot.asks WHERE id = %s", (ask_id,))
    row = await cursor.fetchone()
    return None if row is None else row['id']


async def find_handoff(connection: Connection, handoff_id: str) -> Ask | None:
    """The hand-off ask that carries `handoff_id`, answered or not."""
    cursor = await connection.execute(
        f"SELECT {ASK_COLUMNS} FROM montybot.asks WHERE kind = 'handoff' AND details->>'handoff_id' = %s",
        (handoff_id,),
    )
    row = await cursor.fetchone()
    return None if row is None else ask_from(row)


async def lock_open_ask(connection: Connection, ask_id: str) -> bool:
    """Lock the ask's row for this transaction; True if it is still unanswered."""
    cursor = await connection.execute(
        'SELECT 1 FROM montybot.asks WHERE id = %s AND answer IS NULL FOR UPDATE', (ask_id,)
    )
    return await cursor.fetchone() is not None


async def set_handoff(connection: Connection, ask_id: str, handoff_id: str) -> None:
    await connection.execute(
        "UPDATE montybot.asks SET details = jsonb_set(details, '{handoff_id}', to_jsonb(%s::text)) WHERE id = %s",
        (handoff_id, ask_id),
    )


async def answer_ask(connection: Connection, user_id: str, ask_id: str, answer: dict[str, Any]) -> Ask | None:
    """Record the answer once. None if the ask is not the user's or was answered already."""
    cursor = await connection.execute(
        f'UPDATE montybot.asks SET answer = %s, answered_at = now() '
        f'WHERE id = %s AND user_id = %s AND answer IS NULL RETURNING {ASK_COLUMNS}',
        (Jsonb(answer), ask_id, user_id),
    )
    row = await cursor.fetchone()
    return None if row is None else ask_from(row)


async def expire_ask(connection: Connection, ask_id: str) -> dict[str, Any] | None:
    """Mark the ask expired, unless the user answered it meanwhile: then return that answer."""
    cursor = await connection.execute(
        'UPDATE montybot.asks SET answer = \'{"expired": true}\', answered_at = now() '
        'WHERE id = %s AND answer IS NULL RETURNING id',
        (ask_id,),
    )
    if await cursor.fetchone() is not None:
        return None
    cursor = await connection.execute('SELECT answer FROM montybot.asks WHERE id = %s', (ask_id,))
    row = await cursor.fetchone()
    return None if row is None else row['answer']


# --- schedules ---


async def create_schedule(
    connection: Connection,
    *,
    schedule_id: str,
    user_id: str,
    name: str,
    cron: str,
    timezone: str,
    when: str,
    prompt: str,
    watch: bool,
) -> Schedule:
    """The schedule and its thread. Idempotent: a retried step finds the schedule it made the first time."""
    if (found := await load_schedule(connection, schedule_id)) is not None:
        return found
    thread = await create_thread(connection, user_id, name)
    cursor = await connection.execute(
        f'INSERT INTO montybot.schedules ({SCHEDULE_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) '
        f'RETURNING {SCHEDULE_COLUMNS}',
        (schedule_id, user_id, thread.id, name, cron, timezone, when, prompt, watch),
    )
    row = await cursor.fetchone()
    assert row is not None
    return schedule_from(row)


async def get_schedule(connection: Connection, user_id: str, schedule_id: str) -> Schedule | None:
    cursor = await connection.execute(
        f'SELECT {SCHEDULE_COLUMNS} FROM montybot.schedules WHERE id = %s AND user_id = %s', (schedule_id, user_id)
    )
    row = await cursor.fetchone()
    return None if row is None else schedule_from(row)


async def list_schedules(connection: Connection, user_id: str) -> list[Schedule]:
    cursor = await connection.execute(
        f'SELECT {SCHEDULE_COLUMNS} FROM montybot.schedules WHERE user_id = %s ORDER BY created_at', (user_id,)
    )
    return [schedule_from(row) for row in await cursor.fetchall()]


async def delete_schedule(connection: Connection, user_id: str, schedule_id: str) -> bool:
    cursor = await connection.execute(
        'DELETE FROM montybot.schedules WHERE id = %s AND user_id = %s', (schedule_id, user_id)
    )
    return cursor.rowcount == 1


async def load_schedule(connection: Connection, schedule_id: str) -> Schedule | None:
    """For the schedule's own occurrences, which start from the schedule's id. None once it is deleted."""
    cursor = await connection.execute(
        f'SELECT {SCHEDULE_COLUMNS} FROM montybot.schedules WHERE id = %s', (schedule_id,)
    )
    row = await cursor.fetchone()
    return None if row is None else schedule_from(row)


async def schedule_of_thread(connection: Connection, thread_id: str) -> Schedule | None:
    cursor = await connection.execute(
        f'SELECT {SCHEDULE_COLUMNS} FROM montybot.schedules WHERE thread_id = %s', (thread_id,)
    )
    row = await cursor.fetchone()
    return None if row is None else schedule_from(row)


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
    return User(id=str(row['id']), email=row['email'], name=row['name'], timezone=row['timezone'])


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


def schedule_from(row: dict[str, Any]) -> Schedule:
    return Schedule(
        id=str(row['id']),
        user_id=str(row['user_id']),
        thread_id=str(row['thread_id']),
        name=row['name'],
        cron=row['cron'],
        timezone=row['timezone'],
        when=row['when_text'],
        prompt=row['prompt'],
        watch=row['watch'],
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
