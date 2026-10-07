# Grown from viktor c1896df (viktor/scheduler_tools.py): the same tools for one user's schedules, a `watch` flag, and
# `notify_user` for a watch that found what the user waits for.
"""The agent's tools for the user's schedules (`montybot.schedules`). Setting one up needs the user's approval; each
call's database and DBOS work is one DBOS step, so a recovered run does not do it twice."""

from __future__ import annotations

import uuid
from typing import Any

from dbos import DBOS
from pydantic_ai import FunctionToolset, ModelRetry, RunContext, ToolFailed
from pydantic_ai.tools import ToolDefinition

from montybot import schedules
from montybot.deps import RunDeps
from montybot.resources import current
from montybot.workflows import RETRIED

INSTRUCTIONS = """\
For anything the user wants done again and again ("every Monday, fill my cart"), or watched ("tell me when a slot
opens"), set up a schedule with `schedule_task`. Each run of it happens in its own chat, without the user there."""

SCHEDULED = """\
This task was started by a schedule the user set up ("{name}", {when}), not by a message: the user is not watching.
Use the saved sign-ins; hand off only if a site asks to sign in again. The user approved this task when they set up
the schedule, so `commit` does not ask them again: commit only what the task asks for, and nothing else that cannot
be undone. Reply with what you did, in a line or two."""

WATCH = """\
This schedule is a watch. Check, and if what the user waits for is there, call `notify_user`, then reply with what
you found. If it is not there yet, reply "Not yet." and nothing else: the user is not told."""

schedule_tools: FunctionToolset[RunDeps] = FunctionToolset(id='schedules')


def scheduled_run(ctx: RunContext[RunDeps]) -> str | None:
    """Instructions for a run that a schedule started."""
    schedule = ctx.deps.schedule
    if schedule is None:
        return None
    told = SCHEDULED.format(name=schedule.name, when=schedule.when)
    return f'{told}\n\n{WATCH}' if schedule.watch else told


def check_schedule(
    ctx: RunContext[RunDeps], name: str, cron: str, timezone: str, when: str, prompt: str, watch: bool = False
) -> None:
    """Reject a bad cron or zone before the call asks for approval, so the model retries instead of the user approving
    a schedule that can never exist."""
    try:
        schedules.check(cron, timezone)
    except schedules.InvalidSchedule as error:
        if ctx.tool_call_approved:  # a retry now would ask for approval a second time
            raise ToolFailed(str(error)) from error
        raise ModelRetry(str(error)) from error


@schedule_tools.tool(requires_approval=True, args_validator=check_schedule)
async def schedule_task(
    ctx: RunContext[RunDeps], name: str, cron: str, timezone: str, when: str, prompt: str, watch: bool = False
) -> str:
    """Run a task on a schedule, for the user. They are asked to approve it first; its runs then do what it asks,
    orders and messages included, without asking again.

    Args:
        name: A short label, such as "Weekly groceries".
        cron: Five-field cron in `timezone`, minute first: `0 9 * * 1` is Mondays at 09:00.
        timezone: An IANA zone such as `Europe/London`. The user's own zone unless they name another.
        when: The schedule in plain words, for the user: "Mondays at 09:00".
        prompt: What to do each time, written as the user would ask it, with the site's address.
        watch: True to watch for something ("tell me when a slot opens"): the user hears only when it is found, and
            the watch then pauses.
    """
    deps = ctx.deps
    # One tool call always makes the same schedule, so a replayed call cannot make a second one.
    schedule_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'montybot:schedule:{deps.run_id}:{ctx.tool_call_id}'))

    async def step() -> str:
        try:
            schedule = await schedules.create(
                current().pool,
                schedule_id=schedule_id,
                user_id=deps.user_id,
                name=name,
                cron=cron,
                timezone=timezone,
                when=when,
                prompt=prompt,
                watch=watch,
            )
        except schedules.InvalidSchedule as error:
            return f'Error: {error}'
        return f'Scheduled {schedule.name!r} (id {schedule.id}): {schedule.when}, as `{cron}` in {timezone}.'

    result = await DBOS.run_step_async({**RETRIED, 'name': 'schedules.create'}, step)  # idempotent
    if result.startswith('Error: '):
        raise ToolFailed(result.removeprefix('Error: '))
    return result


@schedule_tools.tool
async def list_schedules(ctx: RunContext[RunDeps]) -> list[dict[str, Any]]:
    """The user's schedules."""
    user_id = ctx.deps.user_id

    async def step() -> list[dict[str, Any]]:
        found = await schedules.list_for(current().pool, user_id)
        return [
            {'id': s.id, 'name': s.name, 'when': s.when, 'prompt': s.prompt, 'watch': s.watch, 'paused': paused}
            for s, paused in found
        ]

    return await DBOS.run_step_async({'name': 'schedules.list'}, step)


@schedule_tools.tool
async def pause_schedule(ctx: RunContext[RunDeps], schedule_id: str) -> str:
    """Pause one of the user's schedules by id. It can be resumed."""
    return await set_paused(ctx, schedule_id, True)


@schedule_tools.tool
async def resume_schedule(ctx: RunContext[RunDeps], schedule_id: str) -> str:
    """Resume a paused schedule by id."""
    return await set_paused(ctx, schedule_id, False)


async def set_paused(ctx: RunContext[RunDeps], schedule_id: str, paused: bool) -> str:
    user_id = ctx.deps.user_id

    async def step() -> str:
        schedule = await schedules.set_paused(current().pool, user_id, schedule_id, paused)
        if schedule is None:
            return NOT_FOUND
        return f'{"Paused" if paused else "Resumed"} {schedule.name!r}.'

    return await DBOS.run_step_async({'name': 'schedules.pause' if paused else 'schedules.resume'}, step)


@schedule_tools.tool
async def delete_schedule(ctx: RunContext[RunDeps], schedule_id: str) -> str:
    """Delete one of the user's schedules by id, for good. Its chat stays."""
    user_id = ctx.deps.user_id

    async def step() -> str:
        return 'Deleted.' if await schedules.delete(current().pool, user_id, schedule_id) else NOT_FOUND

    return await DBOS.run_step_async({'name': 'schedules.delete'}, step)


async def only_in_a_watch(ctx: RunContext[RunDeps], tool: ToolDefinition) -> ToolDefinition | None:
    return tool if ctx.deps.schedule is not None and ctx.deps.schedule.watch else None


@schedule_tools.tool(prepare=only_in_a_watch)
async def notify_user(ctx: RunContext[RunDeps]) -> str:
    """Tell the user (by push and email) that what this watch waits for is there. Put what you found in your reply.
    The watch pauses, so they are told once."""
    schedule = ctx.deps.schedule
    assert schedule is not None  # only offered in a watch
    told = 'The user was notified, and the watch is paused. Now reply with what you found.'
    if ctx.deps.notified.done:
        return told  # once per run, whatever the model does
    ctx.deps.notified.done = True  # before the step: two calls in one response run one after the other
    run_id = ctx.deps.run_id
    await DBOS.run_step_async(
        {**RETRIED, 'name': 'schedules.found'}, schedules.notify_found, current(), schedule, run_id
    )
    return told


NOT_FOUND = 'No schedule of yours with that id.'
