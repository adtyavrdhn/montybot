"""Steering: a message the user sends while a run is working joins that run, rather than waiting for it to end.

```
POST /api/threads/<id>/messages while a run works   (sammy.api)
  add(...)                 keep the message on the thread's unfinished run, its row locked so it cannot end meanwhile
run_thread (sammy.workflows), before each model request:  Steering.before_node_run
  step steer.<n>           the messages not read yet, marked read by request n: a replay gets the same ones
  ctx.enqueue(...)         Pydantic AI's pending-message drain puts them in this very request, as the user's
step run.finish            the messages the run never read start the thread's next run (`hand_on`)
```

A run that fails or is stopped reads none of the rest: they join its history with its prompt (`close`).
The table's SQL is here rather than in `sammy.store`, as for `sammy.attachments`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from dbos import DBOS
from pydantic_ai import ModelRequestNode, RunContext
from pydantic_ai.capabilities import AbstractCapability, AgentNode

from sammy import store
from sammy.db import Connection
from sammy.deps import RunDeps
from sammy.models import Run
from sammy.observability import timing
from sammy.resources import Resources

ENDED_UNREAD = 0
"""`read_by` of a message whose run ended (failed or stopped) without reading it."""


@dataclass(frozen=True, kw_only=True)
class Steer:
    """A message the user sent while a run was working."""

    text: str
    after_asks: int
    """How many asks the run had made when it was sent."""
    read: bool

    def json(self) -> dict[str, str | bool]:
        """As a line of the chat: an unread one is marked, as Sammy will see it next."""
        return {'role': 'user', 'text': self.text, **({} if self.read else {'unread': True})}


async def active_run(connection: Connection, thread_id: str) -> str | None:
    """The thread's unfinished run, its row locked for this transaction, so it cannot end before this commits."""
    cursor = await connection.execute(
        "SELECT id FROM sammy.runs WHERE thread_id = %s AND status IN ('queued', 'running', 'waiting') FOR UPDATE",
        (thread_id,),
    )
    row = await cursor.fetchone()
    return None if row is None else str(row['id'])


async def add(connection: Connection, thread_id: str, text: str) -> str | None:
    """Add the message to the thread's unfinished run; returns that run's id, or None if it has none. Call in a
    transaction."""
    run_id = await active_run(connection, thread_id)
    if run_id is not None:
        await connection.execute(
            'INSERT INTO sammy.steering (run_id, text, after_asks) '
            'VALUES (%s, %s, (SELECT count(*) FROM sammy.asks WHERE run_id = %s))',
            (run_id, text, run_id),
        )
    return run_id


async def read(resources: Resources, run_id: str, request: int) -> list[str]:
    """The run's messages not read yet, oldest first, now marked read by its `request`-th model request. A retried
    step gets the ones the first attempt marked."""
    with timing('run.steer', only_in_trace=True):
        async with resources.pool.connection() as connection:
            # The outer SELECT sees the table as it was before the UPDATE: no message comes back twice.
            cursor = await connection.execute(
                'WITH marked AS (UPDATE sammy.steering SET read_by = %(request)s '
                'WHERE run_id = %(run_id)s AND read_by IS NULL RETURNING id, text) '
                'SELECT id, text FROM marked UNION ALL '
                'SELECT id, text FROM sammy.steering WHERE run_id = %(run_id)s AND read_by = %(request)s ORDER BY id',
                {'run_id': run_id, 'request': request},
            )
            return [row['text'] for row in await cursor.fetchall()]


@dataclass
class Steering(AbstractCapability[RunDeps]):
    """Before each model request, the messages the user sent since join the run (see the module)."""

    async def before_node_run(self, ctx: RunContext[RunDeps], *, node: AgentNode[RunDeps]) -> AgentNode[RunDeps]:
        steered = ctx.deps.steered
        if isinstance(node, ModelRequestNode) and steered.on:
            request = steered.next()
            texts = await DBOS.run_step_async(
                {'name': f'steer.{request}'}, read, ctx.deps.resources, ctx.deps.run_id, request
            )
            for text in texts:
                ctx.enqueue(text)
        return node


def next_run_id(run_id: str) -> str:
    """The id of the run `hand_on` starts after `run_id`: the same on every attempt of the step."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'sammy:steered:{run_id}'))


async def hand_on(connection: Connection, run: Run) -> str | None:
    """The messages `run` never read start the thread's next run: the first is its prompt, and the others wait for
    it as messages sent while it works. Returns that run's id; None if there were none, or if another run (a
    schedule's) started meanwhile, which they join instead. Call in the transaction that finishes `run`."""
    cursor = await connection.execute(
        'SELECT id, text FROM sammy.steering WHERE run_id = %s AND read_by IS NULL ORDER BY id', (run.id,)
    )
    rows = await cursor.fetchall()
    if not rows:
        return None
    next_id = next_run_id(run.id)
    try:
        await store.create_run(
            connection,
            run_id=next_id,
            user_id=run.user_id,
            thread_id=run.thread_id,
            prompt=rows[0]['text'],
            trigger='message',
        )
    except store.ActiveRun:
        if (joined := await active_run(connection, run.thread_id)) is not None:
            await _move(connection, run.id, joined)
        return None
    await connection.execute('DELETE FROM sammy.steering WHERE id = %s', (rows[0]['id'],))
    await _move(connection, run.id, next_id)
    return next_id


async def _move(connection: Connection, from_run: str, to_run: str) -> None:
    """The messages `from_run` never read wait for `to_run` instead, before any of its asks."""
    await connection.execute(
        'UPDATE sammy.steering SET run_id = %s, after_asks = 0 WHERE run_id = %s AND read_by IS NULL',
        (to_run, from_run),
    )


async def started_after(connection: Connection, run_id: str) -> str | None:
    """The run `hand_on` started after `run_id`, if it did."""
    cursor = await connection.execute('SELECT 1 FROM sammy.runs WHERE id = %s', (next_run_id(run_id),))
    return next_run_id(run_id) if await cursor.fetchone() is not None else None


async def close(connection: Connection, run_id: str) -> list[str]:
    """For a run that ends with no reply of its own: every message the user sent it, oldest first. Those it never
    read are marked as such, so the chat no longer says Sammy will see them."""
    await connection.execute(
        'UPDATE sammy.steering SET read_by = %s WHERE run_id = %s AND read_by IS NULL', (ENDED_UNREAD, run_id)
    )
    cursor = await connection.execute('SELECT text FROM sammy.steering WHERE run_id = %s ORDER BY id', (run_id,))
    return [row['text'] for row in await cursor.fetchall()]


async def of_thread(connection: Connection, user_id: str, thread_id: str) -> dict[str, list[Steer]]:
    """The messages sent to each of the thread's runs while it worked, by run id, oldest first."""
    cursor = await connection.execute(
        'SELECT s.run_id, s.text, s.after_asks, s.read_by FROM sammy.steering s JOIN sammy.runs r ON r.id = s.run_id '
        'WHERE r.thread_id = %s AND r.user_id = %s ORDER BY s.id',
        (thread_id, user_id),
    )
    sent: dict[str, list[Steer]] = {}
    for row in await cursor.fetchall():
        steer = Steer(text=row['text'], after_asks=row['after_asks'], read=row['read_by'] is not None)
        sent.setdefault(str(row['run_id']), []).append(steer)
    return sent


async def unread(connection: Connection, run_id: str) -> int:
    """How many of the messages sent to the run it has not read yet."""
    cursor = await connection.execute(
        'SELECT count(*) AS n FROM sammy.steering WHERE run_id = %s AND read_by IS NULL', (run_id,)
    )
    row = await cursor.fetchone()
    return 0 if row is None else int(row['n'])
