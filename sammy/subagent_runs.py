"""Subagents' runs in Postgres (`sammy.subagents`): making and stopping a run's jobs, and finding the ones still going,
for the live screen. A job's run is a row of `sammy.runs` with `parent_run_id`, in its parent's thread. The thread's
own views in `sammy.store` leave the jobs out of its runs and show their asks and activity as the parent's."""

from __future__ import annotations

from collections.abc import Sequence

from sammy.db import Connection
from sammy.models import Run
from sammy.store import RUN_COLUMNS, close_open_asks, run_from


async def create(connection: Connection, parent: Run, tasks: Sequence[tuple[str, str]]) -> None:
    """A run for each `(run id, task)` of the parent's jobs, in its thread and started the same way. Idempotent: a
    retried step finds the runs it made the first time."""
    for run_id, task in tasks:
        await connection.execute(
            'INSERT INTO sammy.runs (id, user_id, thread_id, trigger, prompt, parent_run_id) '
            'VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING',
            (run_id, parent.user_id, parent.thread_id, parent.trigger, task, parent.id),
        )


async def stop(connection: Connection, run_id: str, notice: str) -> list[Run]:
    """End the run's unfinished jobs as stopped and close what they ask; returns them, to close their tabs."""
    cursor = await connection.execute(
        "UPDATE sammy.runs SET status = 'stopped', output = %s, completed_at = now() "
        f"WHERE parent_run_id = %s AND status IN ('queued', 'running', 'waiting') RETURNING {RUN_COLUMNS}",
        (notice, run_id),
    )
    stopped = [run_from(row) for row in await cursor.fetchall()]
    for run in stopped:
        await close_open_asks(connection, run.id)
    return stopped


async def active_jobs(connection: Connection, user_id: str, run_id: str) -> list[str]:
    """The ids of the run's jobs that are still going, always in the same order (jobs of one call share a start)."""
    cursor = await connection.execute(
        'SELECT id FROM sammy.runs WHERE parent_run_id = %s AND user_id = %s '
        "AND status IN ('queued', 'running', 'waiting') ORDER BY created_at, id",
        (run_id, user_id),
    )
    return [str(row['id']) for row in await cursor.fetchall()]
