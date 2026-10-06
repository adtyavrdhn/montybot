# Grown from viktor c1896df (viktor/handlers.py `execute`): a run is a DBOS workflow instead of a pgtask task.
"""A run is one DBOS workflow, whose id is the run's id.

```
POST /api/threads/<id>/messages  (montybot.api)
  store.create_run, then DBOS.start_workflow(run_thread, run_id) with workflow id = run_id
run_thread(run_id)                                   @DBOS.workflow
  step run.start        status running, load the run and the thread's history
  agent.run(...)        model requests are steps (DBOSDurability); browser and memory calls are our own steps;
                        ask_user, approvals and hand-offs wait in DBOS.recv (montybot.approvals)
  step run.finish       append the new messages, status done (or failed)
  step run.close        the browser service saves the user's sign-ins and closes the run's browser
```

If the app stops, DBOS starts the workflow again when the app comes back (same `executor_id`), and every finished
step returns its recorded result instead of running again.
"""

from __future__ import annotations

import logfire
from dbos import DBOS, SetWorkflowID, StepOptions, WorkflowHandleAsync
from dbos._error import DBOSException
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from montybot import store
from montybot.browser.contract import BrowserError
from montybot.browser.service import UnknownRun
from montybot.deps import RunDeps
from montybot.models import Run, Schedule
from montybot.resources import Resources, current

RETRIED: StepOptions = {'retries_allowed': True, 'max_attempts': 5, 'interval_seconds': 1.0}
"""For the steps that end a run: a passing database error must not leave a run unfinished and the browser open."""

FAILURE_NOTICE = 'Something went wrong while working on this, and I could not finish. Please try again.'


@DBOS.workflow(name='montybot.run_thread')
async def run_thread(run_id: str) -> str:
    resources = current()
    run, history_json, schedule = await DBOS.run_step_async({'name': 'run.start'}, start_run, resources, run_id)
    history = recent(ModelMessagesTypeAdapter.validate_json(history_json), resources.settings.history_limit)
    deps = RunDeps(resources=resources, run=run, schedule=schedule)
    # The browser closes (saving the sign-ins and freeing the user's lease) before the run is marked finished, so
    # once the user sees the reply, their browser and sign-ins are free again.
    try:
        result = await resources.agent.run(run.prompt, deps=deps, message_history=history)
    except Exception as error:  # any failure ends the run, and the user is told
        logfire.error('Run {run_id} failed: {error_type}', run_id=run_id, error_type=type(error).__name__)
        await DBOS.run_step_async({**RETRIED, 'name': 'run.close'}, close_browser, resources, run)
        await DBOS.run_step_async({**RETRIED, 'name': 'run.failed'}, fail_run, resources, run, type(error).__name__)
        if isinstance(error, DBOSException):
            raise  # a replay that does not match its recording is a bug to see, not a failed task
        return 'failed'
    new_messages = ModelMessagesTypeAdapter.dump_json(result.new_messages())
    await DBOS.run_step_async({**RETRIED, 'name': 'run.close'}, close_browser, resources, run)
    await DBOS.run_step_async(
        {**RETRIED, 'name': 'run.finish'}, finish_run, resources, run, new_messages, result.output
    )
    return 'done'


async def start(run_id: str) -> WorkflowHandleAsync[str]:
    """Start the run's workflow. Starting it twice is harmless: the second start finds the first."""
    with SetWorkflowID(run_id):
        return await DBOS.start_workflow_async(run_thread, run_id)


async def start_run(resources: Resources, run_id: str) -> tuple[Run, bytes, Schedule | None]:
    async with resources.pool.connection() as connection, connection.transaction():
        run = await store.load_run(connection, run_id)
        await store.set_run_status(connection, run_id, 'running')
        history = await store.load_history(connection, run.thread_id)
        schedule = await store.schedule_of_thread(connection, run.thread_id) if run.trigger == 'schedule' else None
    return run, ModelMessagesTypeAdapter.dump_json(history), schedule


async def finish_run(resources: Resources, run: Run, new_messages: bytes, output: str) -> None:
    async with resources.pool.connection() as connection, connection.transaction():
        if await store.lock_finished(connection, run.id):
            return  # this step ran before and committed, but DBOS had not recorded it
        await store.append_history(connection, run.thread_id, ModelMessagesTypeAdapter.validate_json(new_messages))
        await store.finish_run(connection, run.id, 'done', output=output)


async def fail_run(resources: Resources, run: Run, error_type: str) -> None:
    async with resources.pool.connection() as connection, connection.transaction():
        if await store.lock_finished(connection, run.id):
            return
        await store.append_history(
            connection,
            run.thread_id,
            [
                ModelRequest(parts=[UserPromptPart(content=run.prompt)]),
                ModelResponse(parts=[TextPart(content=FAILURE_NOTICE)]),
            ],
        )
        await store.finish_run(connection, run.id, 'failed', output=FAILURE_NOTICE, error=error_type)


async def close_browser(resources: Resources, run: Run) -> None:
    """Save the user's sign-ins and close the run's browser, if it opened one."""
    try:
        await resources.browser.close(run_id=run.id, user_id=run.user_id)
    except UnknownRun:
        pass
    except BrowserError as error:  # the browser is closed either way; the lease is released
        logfire.warn(
            'Closing the browser of run {run_id}: {error_type}', run_id=run.id, error_type=type(error).__name__
        )


def recent(history: list[ModelMessage], limit: int) -> list[ModelMessage]:
    """About the last `limit` messages, starting at a user's message so no tool call is cut from its return. If the
    last turn alone is longer than `limit`, all of it."""
    starts = [
        index
        for index, message in enumerate(history)
        if isinstance(message, ModelRequest) and any(isinstance(p, UserPromptPart) for p in message.parts)
    ]
    if len(history) <= limit or not starts:
        return history
    within = [index for index in starts if index >= len(history) - limit]
    return history[within[0] if within else starts[-1] :]


async def start_queued(resources: Resources) -> int:
    """Start the workflow of every run still queued, in case the app stopped between recording a run and starting
    it. Starting a workflow that exists is harmless."""
    async with resources.pool.connection() as connection:
        cursor = await connection.execute("SELECT id FROM montybot.runs WHERE status = 'queued'")
        run_ids = [str(row['id']) for row in await cursor.fetchall()]
    for run_id in run_ids:
        await start(run_id)
    return len(run_ids)
