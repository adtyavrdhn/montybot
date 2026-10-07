# Grown from viktor c1896df (viktor/scheduler.py): a DBOS schedule per task instead of a pgtask schedule, owned by a
# user instead of a workspace, and DBOS evaluates the cron in the user's time zone, so there is no UTC conversion.
"""The user's recurring tasks and watches. Each is a row in `montybot.schedules` and a DBOS schedule; DBOS keeps its
schedules in Postgres, so they outlive the app.

```
schedule_task (approved in chat; montybot.schedule_tools)
  create: store.create_schedule (the row and the schedule's thread), DBOS.create_schedule(run_schedule, cron, zone)
DBOS scheduler, at each time the cron names
  run_schedule(scheduled_at, {'schedule_id': ...})       @DBOS.workflow
    step schedule.start   skip if the schedule is gone, paused, or its last occurrence is still going;
                          else store.create_run(trigger='schedule') in the schedule's thread
    run_thread(run_id)    the usual run (montybot.workflows), as a child workflow with the run's id
    step schedule.notify  a recurring task tells the user it finished (or failed); a watch tells them itself when it
                          finds something (notify_user, which also pauses it), and otherwise only if it failed
```

A run reuses the user's saved sign-ins like any other and hands off only when a site asks to sign in again; the user
hears of the hand-off by push and email (montybot.notifications).

Overlap: an occurrence that finds the schedule's previous one still going (waiting for a hand-off, say) is skipped:
the thread has one unfinished run at a time. An occurrence that starts while another task of the user's has the
browser gets the browser's "busy" error, like any run, and says so in its reply.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import logfire
from dbos import DBOS
from dbos._croniter import croniter  # pyright: ignore[reportPrivateUsage]  # the parser DBOS's scheduler uses

from montybot import store, workflows
from montybot.db import Pool
from montybot.models import Schedule
from montybot.notifications import notify
from montybot.resources import Resources, current

PREFIX = 'montybot-schedule-'


class InvalidSchedule(ValueError):
    """The cron expression or zone cannot be a schedule."""


def dbos_name(schedule_id: str) -> str:
    return PREFIX + schedule_id


def check(cron: str, timezone: str) -> None:
    """Five-field cron (minute first) in an IANA zone."""
    if len(cron.split()) != 5 or not croniter.is_valid(cron):  # pyright: ignore[reportUnknownMemberType]
        raise InvalidSchedule(f'{cron!r} is not a five-field cron expression, such as `0 9 * * 1`')
    if not is_timezone(timezone):
        raise InvalidSchedule(f'{timezone!r} is not a known time zone')


def is_timezone(name: str) -> bool:
    """An IANA zone such as `Europe/London`."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):  # OSError: a name such as 'America' is a directory
        return False
    return True


def is_uuid(value: str) -> bool:
    """Ids come from the model as free text, and Postgres rejects anything that is not a uuid."""
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


async def create(
    pool: Pool,
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
    """Idempotent, so a retried step does not make a second schedule."""
    check(cron, timezone)
    async with pool.connection() as connection, connection.transaction():
        schedule = await store.create_schedule(
            connection,
            schedule_id=schedule_id,
            user_id=user_id,
            name=name,
            cron=cron,
            timezone=timezone,
            when=when,
            prompt=prompt,
            watch=watch,
        )
    # Unless an earlier try of this step made it. If every try fails here, the row stays without a DBOS schedule: it
    # lists as paused, cannot be resumed, and can be deleted.
    if await DBOS.get_schedule_async(dbos_name(schedule.id)) is None:
        await DBOS.create_schedule_async(
            schedule_name=dbos_name(schedule.id),
            workflow_fn=run_schedule,
            schedule=schedule.cron,
            context={'schedule_id': schedule.id},
            cron_timezone=schedule.timezone,
        )
    return schedule


async def list_for(pool: Pool, user_id: str) -> list[tuple[Schedule, bool]]:
    """The user's schedules, each with whether it is paused."""
    async with pool.connection() as connection:
        schedules = await store.list_schedules(connection, user_id)
    if not schedules:
        return []
    found = await DBOS.list_schedules_async(schedule_name_prefix=[dbos_name(s.id) for s in schedules])
    active = {s['schedule_name'] for s in found if s['status'] == 'ACTIVE'}
    return [(s, dbos_name(s.id) not in active) for s in schedules]


async def set_paused(pool: Pool, user_id: str, schedule_id: str, paused: bool) -> Schedule | None:
    """None if the user has no such schedule (or it was never completed, see `create`)."""
    if not is_uuid(schedule_id):
        return None
    async with pool.connection() as connection:
        schedule = await store.get_schedule(connection, user_id, schedule_id)
    if schedule is None or await DBOS.get_schedule_async(dbos_name(schedule.id)) is None:
        return None
    change = DBOS.pause_schedule if paused else DBOS.resume_schedule
    await asyncio.to_thread(change, dbos_name(schedule.id))
    return schedule


async def delete(pool: Pool, user_id: str, schedule_id: str) -> bool:
    """False if the user has no such schedule. Its thread stays if it ran, with what earlier occurrences said."""
    if not is_uuid(schedule_id):
        return False
    async with pool.connection() as connection:
        schedule = await store.get_schedule(connection, user_id, schedule_id)
        if schedule is None:
            return False
        await DBOS.delete_schedule_async(dbos_name(schedule.id))  # first, so a crash leaves nothing that still fires
        return await store.delete_schedule(connection, user_id, schedule.id)


# --- an occurrence ---


@DBOS.workflow(name='montybot.run_schedule')
async def run_schedule(scheduled_at: datetime, context: dict[str, str]) -> None:
    """What DBOS starts at each time the schedule's cron names (and what `DBOS.trigger_schedule` starts now)."""
    resources = current()
    started = await DBOS.run_step_async(
        {'name': 'schedule.start'}, start_occurrence, resources, context['schedule_id'], DBOS.workflow_id or ''
    )
    if started is None:
        return
    run_id, schedule = started
    handle = await workflows.start(run_id)
    outcome = await handle.get_result()
    if not schedule.watch or outcome == 'failed':
        await DBOS.run_step_async(
            {**workflows.RETRIED, 'name': 'schedule.notify'}, notify_ended, resources, schedule, run_id, outcome
        )


async def start_occurrence(resources: Resources, schedule_id: str, workflow_id: str) -> tuple[str, Schedule] | None:
    """Record this occurrence's run, or None to skip it."""
    run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'montybot:occurrence:{workflow_id}'))
    async with resources.pool.connection() as connection, connection.transaction():
        schedule = await store.load_schedule(connection, schedule_id)
        if schedule is None:
            return None  # deleted since DBOS enqueued this occurrence
        if await store.get_run(connection, schedule.user_id, run_id) is not None:
            return run_id, schedule  # recorded by an earlier attempt of this step
        found = await DBOS.get_schedule_async(dbos_name(schedule_id))
        if found is None or found['status'] != 'ACTIVE':
            logfire.info('Schedule {schedule_id} is paused: occurrence skipped', schedule_id=schedule_id)
            return None
        try:
            await store.create_run(
                connection,
                run_id=run_id,
                user_id=schedule.user_id,
                thread_id=schedule.thread_id,
                prompt=schedule.prompt,
                trigger='schedule',
            )
        except store.ActiveRun:
            logfire.info('Schedule {schedule_id} is still busy: occurrence skipped', schedule_id=schedule_id)
            return None
    return run_id, schedule


async def notify_ended(resources: Resources, schedule: Schedule, run_id: str, outcome: str) -> None:
    kind = 'failed' if outcome == 'failed' else 'finished'
    await notify(resources, user_id=schedule.user_id, thread_id=schedule.thread_id, kind=kind, tag=run_id)


async def notify_found(resources: Resources, schedule: Schedule, run_id: str) -> None:
    """A watch found what the user waits for: tell them, and pause it, so it tells them once."""
    await notify(resources, user_id=schedule.user_id, thread_id=schedule.thread_id, kind='found', tag=run_id)
    await asyncio.to_thread(DBOS.pause_schedule, dbos_name(schedule.id))
