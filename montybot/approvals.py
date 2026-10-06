# Grown from viktor c1896df (viktor/approvals.py): pgtask's wait_for_signal/emit_signal became DBOS.recv/DBOS.send,
# and questions and browser hand-offs wait the same way approvals do.
"""Everything that pauses a run for the user: a question, an approval, a browser hand-off.

```
workflow (montybot.workflows.run_thread)              web app (montybot.api.answer)
  ask(...)
    step: record the ask (montybot.asks), status waiting
    DBOS.recv('ask-<n>') ......................... POST /api/asks/<id>
                                                     store.answer_ask (first answer wins)
                                                     DBOS.send(run_id, answer, 'ask-<n>')
    step: status running
  <- the answer
```

`DBOS.recv` must be called from workflow code, not from inside a step. Pydantic AI runs plain function tools and
`HandleDeferredToolCalls` handlers in workflow code under `DBOSDurability`, so both can wait here. The n-th ask of a
run has the same id and topic on every replay, so a restarted run finds its ask and its answer again.
"""

from __future__ import annotations

import uuid
from typing import Any

from dbos import DBOS
from pydantic_ai import DeferredToolRequests, DeferredToolResults, RunContext, ToolDenied

from montybot import store
from montybot.deps import RunDeps
from montybot.models import AskKind
from montybot.resources import Resources


def ask_id(run_id: str, occurrence: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'montybot:ask:{run_id}:{occurrence}'))


def topic(occurrence: int) -> str:
    return f'ask-{occurrence}'


async def ask(
    ctx: RunContext[RunDeps], kind: AskKind, prompt: str, details: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Ask the run's user and wait for the answer. None if nobody answered in time."""
    deps = ctx.deps
    occurrence = deps.asked.next()
    the_id = ask_id(deps.run_id, occurrence)
    await DBOS.run_step_async(
        {'name': f'ask.open.{occurrence}'},
        open_ask,
        deps.resources,
        the_id,
        deps.run_id,
        deps.user_id,
        occurrence,
        kind,
        prompt,
        details or {},
    )
    answer = await DBOS.recv_async(topic(occurrence), timeout_seconds=deps.resources.settings.ask_timeout_seconds)
    late = await DBOS.run_step_async(
        {'name': f'ask.close.{occurrence}'}, close_ask, deps.resources, the_id, deps.run_id, answer is None
    )
    return answer if answer is not None else late


async def open_ask(
    resources: Resources,
    the_id: str,
    run_id: str,
    user_id: str,
    occurrence: int,
    kind: AskKind,
    prompt: str,
    details: dict[str, Any],
) -> None:
    async with resources.pool.connection() as connection, connection.transaction():
        await store.create_ask(
            connection,
            ask_id=the_id,
            run_id=run_id,
            user_id=user_id,
            occurrence=occurrence,
            kind=kind,
            prompt=prompt,
            details=details,
        )
        await store.set_run_status(connection, run_id, 'waiting')


async def close_ask(resources: Resources, the_id: str, run_id: str, timed_out: bool) -> dict[str, Any] | None:
    """Back to running. After a timeout, the ask expires, unless the user answered just as the wait ended: that
    answer is returned, so the run does what the user was told it would."""
    async with resources.pool.connection() as connection, connection.transaction():
        late = await store.expire_ask(connection, the_id) if timed_out else None
        await store.set_run_status(connection, run_id, 'running')
    return late


async def answer(resources: Resources, user_id: str, the_id: str, value: dict[str, Any]) -> bool:
    """Record the user's answer and wake the run. False if the ask is not theirs, or was answered already."""
    async with resources.pool.connection() as connection:
        answered = await store.answer_ask(connection, user_id, the_id, value)
        if answered is None:
            # Answered already. The same answer again is a retry after a failed send: send it again (the idempotency
            # key makes that harmless). A different answer is too late.
            answered = await store.get_ask(connection, user_id, the_id)
            if answered is None or answered.answer != value:
                return False
    await deliver(answered.run_id, answered.occurrence, answered.id, value)
    return True


async def deliver(run_id: str, occurrence: int, the_id: str, value: dict[str, Any]) -> None:
    # The ask's id as the idempotency key makes a resend after a crash harmless (see `redeliver_answers`).
    await DBOS.send_async(run_id, value, topic(occurrence), idempotency_key=the_id)


async def redeliver_answers(resources: Resources) -> int:
    """Send again every answer whose run still waits, in case the app stopped between recording and sending it."""
    async with resources.pool.connection() as connection:
        cursor = await connection.execute(
            'SELECT a.id, a.run_id, a.occurrence, a.answer FROM montybot.asks a '
            "JOIN montybot.runs r ON r.id = a.run_id WHERE r.status = 'waiting' AND a.answer IS NOT NULL"
        )
        rows = await cursor.fetchall()
    for row in rows:
        await deliver(str(row['run_id']), row['occurrence'], str(row['id']), row['answer'])
    return len(rows)


async def handle_approvals(ctx: RunContext[RunDeps], requests: DeferredToolRequests) -> DeferredToolResults:
    """Ask the user to approve each call that needs it, one card per call, and wait."""
    verdicts: dict[str, bool | ToolDenied] = {}
    for call in requests.approvals:
        args = call.args_as_dict()
        what = str(args.get('description') or call.tool_name)
        reply = await ask(ctx, 'approval', what, {'tool': call.tool_name, 'target': str(args.get('target', ''))})
        if reply is None:
            verdicts[call.tool_call_id] = ToolDenied('The user did not answer in time, so this was not done.')
        elif reply.get('approved'):
            verdicts[call.tool_call_id] = True
        else:
            reason = str(reply.get('reason') or 'no reason given')
            verdicts[call.tool_call_id] = ToolDenied(f'The user said no: {reason}')
    return requests.build_results(approvals=verdicts)
