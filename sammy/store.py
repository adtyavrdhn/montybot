# Grown from viktor c1896df (viktor/store.py). Every function a request reaches takes the user's id and reads only
# that user's rows, so a caller cannot tell another user's thread, run or ask from one that does not exist.
# Functions without a user id (`load_run`, `load_history`, `append_history`, `set_run_status`, `finish_run`,
# `add_activity`, `load_schedule`, `schedule_of_thread`) are for the run's own workflow, which starts from a run id
# the app created, or for a schedule's occurrence, which starts from the schedule's id.
from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from psycopg.errors import ForeignKeyViolation, UniqueViolation
from psycopg.types.json import Jsonb
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from sammy.db import Connection
from sammy.models import FINISHED, Ask, AskKind, Run, RunStatus, Schedule, Thread, Trigger, User


class ActiveRun(Exception):
    """The thread already has a run that has not finished."""


class ThreadGone(Exception):
    """The thread was deleted (with its schedule) while a run for it was being created."""


USER_COLUMNS = 'id, email, name, timezone, squirrel_name'
RUN_COLUMNS = 'id, user_id, thread_id, trigger, prompt, status, output, error, created_at, completed_at'
ASK_COLUMNS = 'id, run_id, user_id, occurrence, kind, prompt, details, answer'
THREAD_RUNS = f'SELECT {RUN_COLUMNS} FROM sammy.runs WHERE thread_id = %s AND user_id = %s AND parent_run_id IS NULL'
SCHEDULE_COLUMNS = 'id, user_id, thread_id, name, cron, timezone, when_text, prompt, watch'

# --- users ---


async def create_user(connection: Connection, email: str, password_hash: str, name: str = '') -> User | None:
    """None when the email is taken."""
    cursor = await connection.execute(
        'INSERT INTO sammy.users (email, password_hash, name) VALUES (%s, %s, %s) '
        f'ON CONFLICT (email) DO NOTHING RETURNING {USER_COLUMNS}',
        (email, password_hash, name),
    )
    row = await cursor.fetchone()
    return None if row is None else user_from(row)


async def find_login(connection: Connection, email: str) -> tuple[User, str] | None:
    cursor = await connection.execute(
        f'SELECT {USER_COLUMNS}, password_hash FROM sammy.users WHERE email = %s', (email,)
    )
    row = await cursor.fetchone()
    return None if row is None else (user_from(row), row['password_hash'])


async def start_password_reset(connection: Connection, user_id: str, code_hash: str, minutes: int) -> None:
    """A new code replaces any earlier one."""
    await connection.execute(
        'INSERT INTO sammy.password_resets (user_id, code_hash, expires_at) '
        "VALUES (%s, %s, now() + %s * interval '1 minute') ON CONFLICT (user_id) DO UPDATE "
        'SET code_hash = EXCLUDED.code_hash, expires_at = EXCLUDED.expires_at, attempts = 0',
        (user_id, code_hash, minutes),
    )


async def password_reset(connection: Connection, user_id: str, max_attempts: int) -> str | None:
    """The live code's hash, counting this attempt; None once it has expired or been tried too often."""
    cursor = await connection.execute(
        'UPDATE sammy.password_resets SET attempts = attempts + 1 '
        'WHERE user_id = %s AND expires_at > now() AND attempts < %s RETURNING code_hash',
        (user_id, max_attempts),
    )
    row = await cursor.fetchone()
    return None if row is None else row['code_hash']


async def finish_password_reset(connection: Connection, user_id: str, password_hash: str) -> None:
    await connection.execute('UPDATE sammy.users SET password_hash = %s WHERE id = %s', (password_hash, user_id))
    await connection.execute('DELETE FROM sammy.password_resets WHERE user_id = %s', (user_id,))


async def get_user(connection: Connection, user_id: str) -> User | None:
    cursor = await connection.execute(f'SELECT {USER_COLUMNS} FROM sammy.users WHERE id = %s', (user_id,))
    row = await cursor.fetchone()
    return None if row is None else user_from(row)


async def set_timezone(connection: Connection, user_id: str, timezone: str) -> None:
    await connection.execute('UPDATE sammy.users SET timezone = %s WHERE id = %s', (timezone, user_id))


async def set_squirrel_name(connection: Connection, user_id: str, name: str) -> None:
    await connection.execute('UPDATE sammy.users SET squirrel_name = %s WHERE id = %s', (name, user_id))


# --- threads ---


async def create_thread(connection: Connection, user_id: str, title: str) -> Thread:
    cursor = await connection.execute(
        'INSERT INTO sammy.threads (user_id, title) VALUES (%s, %s) RETURNING id, user_id, title',
        (user_id, title[:120]),
    )
    row = await cursor.fetchone()
    assert row is not None
    return thread_from(row)


async def get_thread(connection: Connection, user_id: str, thread_id: str) -> Thread | None:
    cursor = await connection.execute(
        'SELECT id, user_id, title FROM sammy.threads WHERE id = %s AND user_id = %s', (thread_id, user_id)
    )
    row = await cursor.fetchone()
    return None if row is None else thread_from(row)


async def list_threads(connection: Connection, user_id: str) -> list[Thread]:
    """The user's threads, the one last active first (its latest run started, or it was created)."""
    cursor = await connection.execute(
        'SELECT t.id, t.user_id, t.title FROM sammy.threads t WHERE t.user_id = %s '
        # When it was last active, as `last_active` says: a schedule's chat with no run yet counts from its creation.
        'ORDER BY coalesce((SELECT max(r.created_at) FROM sammy.runs r WHERE r.thread_id = t.id), t.created_at) DESC',
        (user_id,),
    )
    return [thread_from(row) for row in await cursor.fetchall()]


async def active_runs(connection: Connection, user_id: str) -> dict[str, RunStatus]:
    """Each unfinished run's status by thread id: waiting while it or one of its subagents asks (see `api.run_view`)."""
    cursor = await connection.execute(
        'SELECT r.thread_id, CASE WHEN EXISTS (SELECT 1 FROM sammy.asks a JOIN sammy.runs c ON c.id = a.run_id '
        "WHERE r.id IN (c.id, c.parent_run_id) AND a.answer IS NULL) THEN 'waiting' "
        "WHEN r.status = 'waiting' THEN 'running' ELSE r.status END AS status FROM sammy.runs r "
        # The same condition as the index runs_active_by_user.
        "WHERE r.user_id = %s AND r.status IN ('queued', 'running', 'waiting') AND r.parent_run_id IS NULL",
        (user_id,),
    )
    return {str(row['thread_id']): row['status'] for row in await cursor.fetchall()}


async def latest_outcomes(connection: Connection, user_id: str) -> dict[str, RunStatus]:
    """How each of the user's threads' latest run ended (`done`, `failed` or `stopped`), by thread id; threads whose
    latest run is unfinished or that have none are left out."""
    cursor = await connection.execute(
        'SELECT DISTINCT ON (thread_id) thread_id, status FROM sammy.runs WHERE user_id = %s '
        'AND parent_run_id IS NULL ORDER BY thread_id, created_at DESC',
        (user_id,),
    )
    return {str(row['thread_id']): row['status'] for row in await cursor.fetchall() if row['status'] in FINISHED}


async def waiting_for(connection: Connection, user_id: str) -> dict[str, str]:
    """What each of the user's waiting threads waits for (`question`, `approval` or `handoff`): the latest open ask
    of its waiting run, by thread id, so the chat list can say "Approval" rather than just "needs you"."""
    cursor = await connection.execute(
        'SELECT DISTINCT ON (r.thread_id) r.thread_id, a.kind FROM sammy.runs r '
        'JOIN sammy.asks a ON a.run_id = r.id AND a.answer IS NULL '
        "WHERE r.user_id = %s AND r.status = 'waiting' ORDER BY r.thread_id, a.occurrence DESC",
        (user_id,),
    )
    return {str(row['thread_id']): row['kind'] for row in await cursor.fetchall()}


async def last_active(connection: Connection, user_id: str) -> dict[str, datetime]:
    """When each of the user's threads last had something happen: its latest run started, or it was created. The
    order of `list_threads`, as times an app can group by ("Today", "Yesterday")."""
    cursor = await connection.execute(
        'SELECT t.id, coalesce((SELECT max(r.created_at) FROM sammy.runs r WHERE r.thread_id = t.id), t.created_at) '
        'AS at FROM sammy.threads t WHERE t.user_id = %s',
        (user_id,),
    )
    return {str(row['id']): row['at'] for row in await cursor.fetchall()}


async def rename_thread(connection: Connection, user_id: str, thread_id: str, title: str) -> bool:
    cursor = await connection.execute(
        'UPDATE sammy.threads SET title = %s WHERE id = %s AND user_id = %s', (title[:120], thread_id, user_id)
    )
    return cursor.rowcount == 1


async def delete_thread(connection: Connection, user_id: str, thread_id: str) -> bool:
    """With its messages, runs and their asks and activity, and its schedule (cascades)."""
    cursor = await connection.execute('DELETE FROM sammy.threads WHERE id = %s AND user_id = %s', (thread_id, user_id))
    return cursor.rowcount == 1


async def thread_schedule(connection: Connection, user_id: str, thread_id: str) -> Schedule | None:
    cursor = await connection.execute(
        f'SELECT {SCHEDULE_COLUMNS} FROM sammy.schedules WHERE thread_id = %s AND user_id = %s',
        (thread_id, user_id),
    )
    row = await cursor.fetchone()
    return None if row is None else schedule_from(row)


# --- runs ---


async def create_run(
    connection: Connection, *, run_id: str, user_id: str, thread_id: str, prompt: str, trigger: Trigger
) -> Run:
    """Raises `ActiveRun` when the thread has an unfinished run, `ThreadGone` when it was deleted. Call inside a
    transaction."""
    try:
        async with connection.transaction():
            cursor = await connection.execute(
                f'INSERT INTO sammy.runs (id, user_id, thread_id, trigger, prompt) VALUES (%s, %s, %s, %s, %s) '
                f'RETURNING {RUN_COLUMNS}',
                (run_id, user_id, thread_id, trigger, prompt),
            )
    except UniqueViolation as error:
        raise ActiveRun('Sammy is still working on the last message in this chat.') from error
    except ForeignKeyViolation as error:
        raise ThreadGone('this chat was deleted') from error
    row = await cursor.fetchone()
    assert row is not None
    return run_from(row)


async def list_runs(connection: Connection, user_id: str, thread_id: str) -> list[Run]:
    """The thread's own runs (not their subagents', `sammy.subagents`), oldest first."""
    cursor = await connection.execute(
        f'{THREAD_RUNS} ORDER BY created_at',
        (thread_id, user_id),
    )
    return [run_from(row) for row in await cursor.fetchall()]


async def get_run(connection: Connection, user_id: str, run_id: str) -> Run | None:
    cursor = await connection.execute(
        f'SELECT {RUN_COLUMNS} FROM sammy.runs WHERE id = %s AND user_id = %s', (run_id, user_id)
    )
    row = await cursor.fetchone()
    return None if row is None else run_from(row)


async def latest_run(connection: Connection, user_id: str, thread_id: str) -> Run | None:
    cursor = await connection.execute(
        f'{THREAD_RUNS} ORDER BY created_at DESC LIMIT 1',
        (thread_id, user_id),
    )
    row = await cursor.fetchone()
    return None if row is None else run_from(row)


async def load_run(connection: Connection, run_id: str) -> Run:
    cursor = await connection.execute(f'SELECT {RUN_COLUMNS} FROM sammy.runs WHERE id = %s', (run_id,))
    row = await cursor.fetchone()
    assert row is not None, 'a workflow starts only for a run the app recorded'
    return run_from(row)


async def set_run_status(connection: Connection, run_id: str, status: RunStatus) -> None:
    await connection.execute(
        'UPDATE sammy.runs SET status = %s WHERE id = %s AND NOT status = ANY(%s)', (status, run_id, list(FINISHED))
    )


async def lock_finished(connection: Connection, run_id: str) -> bool:
    """Lock the run's row for this transaction; True if it has finished already (done, failed or stopped)."""
    cursor = await connection.execute('SELECT status FROM sammy.runs WHERE id = %s FOR UPDATE', (run_id,))
    row = await cursor.fetchone()
    return row is not None and row['status'] in FINISHED


async def finish_run(
    connection: Connection, run_id: str, status: RunStatus, *, output: str | None = None, error: str | None = None
) -> None:
    await connection.execute(
        'UPDATE sammy.runs SET status = %s, output = %s, error = %s, completed_at = now() WHERE id = %s',
        (status, output, error, run_id),
    )


# --- history ---


async def load_history(connection: Connection, thread_id: str) -> list[ModelMessage]:
    cursor = await connection.execute(
        'SELECT payload FROM sammy.messages WHERE thread_id = %s ORDER BY position', (thread_id,)
    )
    payloads = [row['payload'] for row in await cursor.fetchall()]
    return ModelMessagesTypeAdapter.validate_python(payloads)


async def append_history(connection: Connection, thread_id: str, messages: Sequence[ModelMessage]) -> None:
    cursor = await connection.execute(
        'SELECT coalesce(max(position), -1) AS last FROM sammy.messages WHERE thread_id = %s', (thread_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    payloads: list[Any] = json.loads(ModelMessagesTypeAdapter.dump_json(list(messages)))
    for offset, payload in enumerate(payloads, start=row['last'] + 1):
        await connection.execute(
            'INSERT INTO sammy.messages (thread_id, position, payload) VALUES (%s, %s, %s)',
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
        'INSERT INTO sammy.asks (id, run_id, user_id, occurrence, kind, prompt, details) '
        'VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING',
        (ask_id, run_id, user_id, occurrence, kind, prompt, Jsonb(details)),
    )


async def open_ask(connection: Connection, user_id: str, run_id: str) -> Ask | None:
    cursor = await connection.execute(
        f'SELECT {ASK_COLUMNS} FROM sammy.asks WHERE user_id = %s AND answer IS NULL AND run_id IN '
        '(SELECT id FROM sammy.runs WHERE %s IN (id, parent_run_id)) ORDER BY created_at DESC LIMIT 1',
        (user_id, run_id),
    )
    row = await cursor.fetchone()
    return None if row is None else ask_from(row)


async def list_answered_asks(connection: Connection, user_id: str, thread_id: str) -> list[Ask]:
    """The thread's asks that have an answer (or expired), in the order asked; a subagent's under its parent."""
    cursor = await connection.execute(
        'SELECT a.id, coalesce(r.parent_run_id, a.run_id) AS run_id, a.user_id, a.occurrence, a.kind, a.prompt, '
        'a.details, a.answer FROM sammy.asks a JOIN sammy.runs r ON r.id = a.run_id WHERE r.thread_id = %s '
        'AND a.user_id = %s AND a.answer IS NOT NULL ORDER BY a.created_at, a.occurrence',
        (thread_id, user_id),
    )
    return [ask_from(row) for row in await cursor.fetchall()]


async def open_connect_asks(connection: Connection, user_id: str) -> list[Ask]:
    """The user's unanswered asks to connect a service, from any of their runs."""
    cursor = await connection.execute(
        f"SELECT {ASK_COLUMNS} FROM sammy.asks WHERE user_id = %s AND kind = 'connect' AND answer IS NULL",
        (user_id,),
    )
    return [ask_from(row) for row in await cursor.fetchall()]


async def close_open_asks(connection: Connection, run_id: str) -> None:
    """Close what a finished run still asks, so a late answer (from an old notification) is refused."""
    await connection.execute(
        'UPDATE sammy.asks SET answer = \'{"closed": true}\', answered_at = now() WHERE run_id = %s AND answer IS NULL',
        (run_id,),
    )


async def get_ask(connection: Connection, user_id: str, ask_id: str) -> Ask | None:
    cursor = await connection.execute(
        f'SELECT {ASK_COLUMNS} FROM sammy.asks WHERE id = %s AND user_id = %s', (ask_id, user_id)
    )
    row = await cursor.fetchone()
    return None if row is None else ask_from(row)


async def handoff_of(connection: Connection, ask_id: str) -> str | None:
    cursor = await connection.execute("SELECT details->>'handoff_id' AS id FROM sammy.asks WHERE id = %s", (ask_id,))
    row = await cursor.fetchone()
    return None if row is None else row['id']


async def find_handoff(connection: Connection, handoff_id: str) -> Ask | None:
    """The hand-off ask that carries `handoff_id`, answered or not."""
    cursor = await connection.execute(
        f"SELECT {ASK_COLUMNS} FROM sammy.asks WHERE kind = 'handoff' AND details->>'handoff_id' = %s",
        (handoff_id,),
    )
    row = await cursor.fetchone()
    return None if row is None else ask_from(row)


async def lock_open_ask(connection: Connection, ask_id: str) -> bool:
    """Lock the ask's row for this transaction; True if it is still unanswered."""
    cursor = await connection.execute('SELECT 1 FROM sammy.asks WHERE id = %s AND answer IS NULL FOR UPDATE', (ask_id,))
    return await cursor.fetchone() is not None


async def set_handoff(connection: Connection, ask_id: str, handoff_id: str) -> None:
    await connection.execute(
        "UPDATE sammy.asks SET details = jsonb_set(details, '{handoff_id}', to_jsonb(%s::text)) WHERE id = %s",
        (handoff_id, ask_id),
    )


async def answer_ask(connection: Connection, user_id: str, ask_id: str, answer: dict[str, Any]) -> Ask | None:
    """Record the answer once. None if the ask is not the user's or was answered already."""
    cursor = await connection.execute(
        f'UPDATE sammy.asks SET answer = %s, answered_at = now() '
        f'WHERE id = %s AND user_id = %s AND answer IS NULL RETURNING {ASK_COLUMNS}',
        (Jsonb(answer), ask_id, user_id),
    )
    row = await cursor.fetchone()
    return None if row is None else ask_from(row)


async def expire_ask(connection: Connection, ask_id: str) -> dict[str, Any] | None:
    """Mark the ask expired, unless the user answered it meanwhile: then return that answer."""
    cursor = await connection.execute(
        'UPDATE sammy.asks SET answer = \'{"expired": true}\', answered_at = now() '
        'WHERE id = %s AND answer IS NULL RETURNING id',
        (ask_id,),
    )
    if await cursor.fetchone() is not None:
        return None
    cursor = await connection.execute('SELECT answer FROM sammy.asks WHERE id = %s', (ask_id,))
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
        f'INSERT INTO sammy.schedules ({SCHEDULE_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) '
        f'RETURNING {SCHEDULE_COLUMNS}',
        (schedule_id, user_id, thread.id, name, cron, timezone, when, prompt, watch),
    )
    row = await cursor.fetchone()
    assert row is not None
    return schedule_from(row)


async def get_schedule(connection: Connection, user_id: str, schedule_id: str) -> Schedule | None:
    cursor = await connection.execute(
        f'SELECT {SCHEDULE_COLUMNS} FROM sammy.schedules WHERE id = %s AND user_id = %s', (schedule_id, user_id)
    )
    row = await cursor.fetchone()
    return None if row is None else schedule_from(row)


async def list_schedules(connection: Connection, user_id: str) -> list[Schedule]:
    cursor = await connection.execute(
        f'SELECT {SCHEDULE_COLUMNS} FROM sammy.schedules WHERE user_id = %s ORDER BY created_at', (user_id,)
    )
    return [schedule_from(row) for row in await cursor.fetchall()]


async def delete_schedule(connection: Connection, user_id: str, schedule_id: str) -> bool:
    """Its chat goes too if the schedule never ran: with nothing in it, it would only clutter the chat list."""
    cursor = await connection.execute(
        'DELETE FROM sammy.schedules WHERE id = %s AND user_id = %s RETURNING thread_id', (schedule_id, user_id)
    )
    row = await cursor.fetchone()
    if row is None:
        return False
    # Lock the thread first: a run starting in it right now must either be seen below or wait for this to commit.
    await connection.execute('SELECT 1 FROM sammy.threads WHERE id = %s FOR UPDATE', (row['thread_id'],))
    await connection.execute(
        'DELETE FROM sammy.threads t WHERE t.id = %s '
        'AND NOT EXISTS (SELECT 1 FROM sammy.runs r WHERE r.thread_id = t.id)',
        (row['thread_id'],),
    )
    return True


async def load_schedule(connection: Connection, schedule_id: str) -> Schedule | None:
    """For the schedule's own occurrences, which start from the schedule's id. None once it is deleted."""
    cursor = await connection.execute(f'SELECT {SCHEDULE_COLUMNS} FROM sammy.schedules WHERE id = %s', (schedule_id,))
    row = await cursor.fetchone()
    return None if row is None else schedule_from(row)


async def schedule_of_thread(connection: Connection, thread_id: str) -> Schedule | None:
    cursor = await connection.execute(
        f'SELECT {SCHEDULE_COLUMNS} FROM sammy.schedules WHERE thread_id = %s', (thread_id,)
    )
    row = await cursor.fetchone()
    return None if row is None else schedule_from(row)


# --- activity ---


async def add_activity(connection: Connection, run_id: str, text: str) -> None:
    await connection.execute('INSERT INTO sammy.activity (run_id, text) VALUES (%s, %s)', (run_id, text))


async def list_activity(connection: Connection, user_id: str, run_id: str) -> list[str]:
    cursor = await connection.execute(
        'SELECT a.text FROM sammy.activity a JOIN sammy.runs r ON r.id = a.run_id '
        'WHERE %s IN (r.id, r.parent_run_id) AND r.user_id = %s ORDER BY a.id',
        (run_id, user_id),
    )
    return [row['text'] for row in await cursor.fetchall()]


async def last_scheduled_runs(connection: Connection, user_id: str) -> dict[str, Run]:
    """The latest run each of the user's schedules started, by its thread id."""
    cursor = await connection.execute(
        f'SELECT DISTINCT ON (thread_id) {RUN_COLUMNS} FROM sammy.runs '
        "WHERE user_id = %s AND trigger = 'schedule' AND parent_run_id IS NULL ORDER BY thread_id, created_at DESC",
        (user_id,),
    )
    return {str(row['thread_id']): run_from(row) for row in await cursor.fetchall()}


async def search_threads(connection: Connection, user_id: str, query: str, limit: int = 50) -> list[str]:
    """The user's threads whose title, a task or a reply has `query` in it (any case), latest first, by id."""
    pattern = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
    cursor = await connection.execute(
        'SELECT t.id FROM sammy.threads t LEFT JOIN sammy.runs r ON r.thread_id = t.id '
        'WHERE t.user_id = %s AND (t.title ILIKE %s OR r.prompt ILIKE %s OR r.output ILIKE %s) '
        'GROUP BY t.id ORDER BY max(coalesce(r.created_at, t.created_at)) DESC LIMIT %s',
        (user_id, pattern, pattern, pattern, limit),
    )
    return [str(row['id']) for row in await cursor.fetchall()]


async def list_thread_activity(connection: Connection, user_id: str, thread_id: str) -> dict[str, list[str]]:
    """Every run's steps in a thread (a subagent's under its parent), by run id, oldest first."""
    cursor = await connection.execute(
        'SELECT coalesce(r.parent_run_id, r.id) AS run_id, a.text FROM sammy.activity a JOIN sammy.runs r '
        'ON r.id = a.run_id WHERE r.thread_id = %s AND r.user_id = %s ORDER BY a.id',
        (thread_id, user_id),
    )
    steps: dict[str, list[str]] = {}
    for row in await cursor.fetchall():
        steps.setdefault(str(row['run_id']), []).append(row['text'])
    return steps


# --- rows ---


def user_from(row: dict[str, Any]) -> User:
    return User(
        id=str(row['id']),
        email=row['email'],
        name=row['name'],
        timezone=row['timezone'],
        squirrel_name=row['squirrel_name'],
    )


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
        started_at=row['created_at'],
        completed_at=row['completed_at'],
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
