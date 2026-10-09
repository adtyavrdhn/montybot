# Grown from viktor c1896df (viktor/approvals.py): pgtask's wait_for_signal/emit_signal became DBOS.recv/DBOS.send,
# and questions and browser hand-offs wait the same way approvals do.
"""Everything that pauses a run for the user: a question, an approval, a browser hand-off.

```
workflow (sammy.workflows.run_thread)              web app (sammy.api.answer)
  ask(...)
    step: record the ask (sammy.asks), status waiting
    DBOS.recv('ask-<n>') ......................... POST /api/asks/<id>
                                                     store.answer_ask (first answer wins)
                                                     DBOS.send(run_id, answer, 'ask-<n>')
    step: status running
  <- the answer
```

An approval may go through without the user (`decide`): when a rule they made with "Always allow this" covers the
action (`sammy.approval_rules`), or when they turned on the automatic reviewer and it approves (`sammy.reviewer`). The
ask is then recorded as approved already (`auto`), so the chat shows what went through, and traced (`approval.auto`).

`DBOS.recv` must be called from workflow code, not from inside a step. Pydantic AI runs plain function tools and
`HandleDeferredToolCalls` handlers in workflow code under `DBOSDurability`, so both can wait here. The n-th ask of a
run has the same id and topic on every replay, so a restarted run finds its ask and its answer again.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from dbos import DBOS
from pydantic_ai import DeferredToolRequests, DeferredToolResults, RunContext, ToolDenied

from sammy import approval_rules, reviewer, store
from sammy.approval_rules import Action, commit_action, integration_action
from sammy.browser.contract import BrowserError
from sammy.browser.service import UnknownRun
from sammy.deps import RunDeps
from sammy.models import AskKind, Run
from sammy.notifications import notify
from sammy.observability import timing
from sammy.resources import Resources


def ask_id(run_id: str, occurrence: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'sammy:ask:{run_id}:{occurrence}'))


def topic(occurrence: int) -> str:
    return f'ask-{occurrence}'


async def ask(
    ctx: RunContext[RunDeps],
    kind: AskKind,
    prompt: str,
    details: dict[str, Any] | None = None,
    *,
    occurrence: int | None = None,
) -> dict[str, Any] | None:
    """Ask the run's user and wait for the answer. None if nobody answered in time. `occurrence` is for an ask the
    run counted already (`handle_approvals`)."""
    deps = ctx.deps
    occurrence = deps.asked.next() if occurrence is None else occurrence
    the_id = ask_id(deps.run_id, occurrence)
    await DBOS.run_step_async(
        {'name': f'ask.save.{occurrence}'}, save_browser, deps.resources, deps.run_id, deps.user_id
    )
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


async def save_browser(resources: Resources, run_id: str, user_id: str) -> None:
    """Save the run's browser before it waits, so a restart during the wait reopens it where it was."""
    try:
        await resources.browser.save_state(run_id=run_id, user_id=user_id)
    except (UnknownRun, BrowserError):
        return  # no browser yet, or one that cannot export: nothing more to keep
    await resources.lease.renew(user_id=user_id, run_id=run_id)  # renewed for at least as long as the wait


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
        run = await store.load_run(connection, run_id)
    await notify(resources, user_id=user_id, thread_id=run.thread_id, kind=kind, tag=the_id)


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
            'SELECT a.id, a.run_id, a.occurrence, a.answer FROM sammy.asks a '
            "JOIN sammy.runs r ON r.id = a.run_id WHERE r.status = 'waiting' AND a.answer IS NOT NULL"
        )
        rows = await cursor.fetchall()
    for row in rows:
        await deliver(str(row['run_id']), row['occurrence'], str(row['id']), row['answer'])
    return len(rows)


async def connected(resources: Resources, user_id: str, *, provider: str, key: str = '') -> int:
    """The user connected something: answer each of their runs waiting for it, so they carry on. An app counts for
    the asks that offered that app (`key`); any MCP server of theirs counts for the asks to add one. Returns how many
    were answered. The run checks for itself what is connected, so an answer here only wakes it."""
    async with resources.pool.connection() as connection:
        asks = await store.open_connect_asks(connection, user_id)
    woken = 0
    for waiting in asks:
        offered = waiting.integration
        if offered.get('provider') == provider and (provider == 'mcp' or offered.get('key') == key):
            woken += await answer(resources, user_id, waiting.id, {'connected': True})
    return woken


def describe(tool: str, args: dict[str, Any]) -> str:
    """What the user is asked to approve."""
    if tool == 'call_integration_tool':
        arguments = json.dumps(args.get('arguments') or {}, ensure_ascii=False)
        if len(arguments) > 600:
            arguments = arguments[:600] + '…'
        return f'Use {args.get("integration")}: {args.get("tool")} {arguments}'
    if tool == 'schedule_task':
        kind = 'Watch' if args.get('watch') else 'Run'
        what = f'{kind} "{args.get("name")}" {args.get("when")} ({args.get("timezone")}): {args.get("prompt")}'
        # What the user approves here covers every run's own steps: `commit` does not ask in a scheduled run.
        return f'{what.rstrip(".")}. Each run does this without asking you again, orders and messages included.'
    return str(args.get('description') or tool)


async def handle_approvals(ctx: RunContext[RunDeps], requests: DeferredToolRequests) -> DeferredToolResults:
    """Ask the user to approve each call that needs it, one card per call, and wait; unless one of their rules, or
    the automatic reviewer, approves it (`decide`)."""
    deps = ctx.deps
    verdicts: dict[str, bool | ToolDenied] = {}
    for call in requests.approvals:
        args = call.args_as_dict()
        what = describe(call.tool_name, args)
        occurrence = deps.asked.next()
        decision = await decide(deps.resources, deps.run, occurrence, call.tool_name, args, what)
        if decision.by is not None:
            verdicts[call.tool_call_id] = True
            continue
        reply = await ask(ctx, 'approval', what, decision.details, occurrence=occurrence)
        if reply is None:
            verdicts[call.tool_call_id] = ToolDenied('The user did not answer in time, so this was not done.')
        elif reply.get('approved'):
            verdicts[call.tool_call_id] = True
        else:
            reason = str(reply.get('reason') or 'no reason given')
            verdicts[call.tool_call_id] = ToolDenied(f'The user said no: {reason}')
    return requests.build_results(approvals=verdicts)


# --- approving without the user: remembered rules and the automatic reviewer ---

AutoBy = Literal['rule', 'reviewer']


@dataclass(frozen=True)
class Decision:
    by: AutoBy | None
    """What approved the call without the user; None to ask them."""
    details: dict[str, object]
    """The ask's details: the tool, the target, and the action "Always allow this" would remember (`rule`)."""


async def decide(
    resources: Resources, run: Run, occurrence: int, tool: str, args: Mapping[str, object], prompt: str
) -> Decision:
    """Whether a rule of the user's, or the reviewer, approves this call. Decided once and kept on the ask's own row
    (approved already, `auto`, when it went through), so a replay decides the same. Not in DBOS steps: a run that was
    waiting for an approval before rules existed replays the steps it recorded."""
    the_id = ask_id(run.id, occurrence)
    async with resources.pool.connection() as connection:
        recorded = await store.get_ask(connection, run.user_id, the_id)
    if recorded is not None:
        auto = (recorded.answer or {}).get('auto')
        return Decision(by=auto if auto in ('rule', 'reviewer') else None, details=recorded.details)
    action = await action_of(resources, run, tool, args)
    by = None if action is None else await automatic(resources, run, action, args)
    details: dict[str, object] = {'tool': tool, 'target': str(args.get('target', ''))}
    if action is not None:
        details['rule'] = action.json()
    async with resources.pool.connection() as connection, connection.transaction():
        await store.create_ask(
            connection,
            ask_id=the_id,
            run_id=run.id,
            user_id=run.user_id,
            occurrence=occurrence,
            kind='approval',
            prompt=prompt,
            details=details,
        )
        if by is not None and action is not None:
            with timing('approval.auto') as span:  # shown in the chat as an approval the user did not give
                span.set_attributes({'approval.by': by, 'approval.tool': tool, 'approval.scope': action.scope})
                await store.answer_ask(connection, run.user_id, the_id, {'approved': True, 'auto': by})
    return Decision(by=by, details=details)


async def action_of(resources: Resources, run: Run, tool: str, args: Mapping[str, object]) -> Action | None:
    """What a rule would cover; None for a call no rule can cover, such as setting up a schedule."""
    if tool == 'call_integration_tool':
        return integration_action(args)
    if tool != 'commit':
        return None
    try:  # refs keep their numbers across snapshots, so reading the page changes nothing for the agent
        page = (await resources.browser.snapshot(run_id=run.id, user_id=run.user_id)).snapshot
    except (UnknownRun, BrowserError):
        return None
    return commit_action(str(args.get('target', '')), str(args.get('description', '')), page.url, page.text)


async def automatic(resources: Resources, run: Run, action: Action, args: Mapping[str, object]) -> AutoBy | None:
    async with resources.pool.connection() as connection:
        if await approval_rules.matching_rule(connection, run.user_id, action) is not None:
            return 'rule'
        model = resources.reviewer_model
        if (
            model is None
            or action.risk is not None
            or not await approval_rules.reviewer_enabled(connection, run.user_id)
        ):
            return None
    approved = await reviewer.approves(model, resources.settings, task=run.prompt, action=action, arguments=args)
    return 'reviewer' if approved else None
