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
from dbos import DBOS, SetWorkflowID
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from montybot import store
from montybot.browser.service import UnknownRun
from montybot.deps import RunDeps
from montybot.models import Run
from montybot.resources import Resources, current

FAILURE_NOTICE = 'Something went wrong while working on this, and I could not finish. Please try again.'


@DBOS.workflow(name='montybot.run_thread')
async def run_thread(run_id: str) -> str:
    resources = current()
    run, history_json = await DBOS.run_step_async({'name': 'run.start'}, start_run, resources, run_id)
    history = recent(ModelMessagesTypeAdapter.validate_json(history_json), resources.settings.history_limit)
    deps = RunDeps(resources=resources, run=run)
    try:
        result = await resources.agent.run(run.prompt, deps=deps, message_history=history)
    except Exception as error:  # noqa: BLE001  any failure ends the run, and the user is told
        logfire.error('Run {run_id} failed: {error_type}', run_id=run_id, error_type=type(error).__name__)
        await DBOS.run_step_async({'name': 'run.failed'}, fail_run, resources, run, type(error).__name__)
        await DBOS.run_step_async({'name': 'run.close'}, close_browser, resources, run)
        return 'failed'
    new_messages = ModelMessagesTypeAdapter.dump_json(result.new_messages())
    await DBOS.run_step_async({'name': 'run.finish'}, finish_run, resources, run, new_messages, result.output)
    await DBOS.run_step_async({'name': 'run.close'}, close_browser, resources, run)
    return 'done'


async def start(run_id: str) -> None:
    """Start the run's workflow. Starting it twice is harmless: the second start finds the first."""
    with SetWorkflowID(run_id):
        await DBOS.start_workflow_async(run_thread, run_id)


async def start_run(resources: Resources, run_id: str) -> tuple[Run, bytes]:
    async with resources.pool.connection() as connection, connection.transaction():
        run = await store.load_run(connection, run_id)
        await store.set_run_status(connection, run_id, 'running')
        history = await store.load_history(connection, run.thread_id)
    return run, ModelMessagesTypeAdapter.dump_json(history)


async def finish_run(resources: Resources, run: Run, new_messages: bytes, output: str) -> None:
    async with resources.pool.connection() as connection, connection.transaction():
        await store.append_history(connection, run.thread_id, ModelMessagesTypeAdapter.validate_json(new_messages))
        await store.finish_run(connection, run.id, 'done', output=output)


async def fail_run(resources: Resources, run: Run, error_type: str) -> None:
    async with resources.pool.connection() as connection, connection.transaction():
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


def recent(history: list[ModelMessage], limit: int) -> list[ModelMessage]:
    """The last `limit` messages or so, starting at a user's message so no tool call is cut from its return."""
    if len(history) <= limit:
        return history
    for index in range(len(history) - limit, len(history)):
        message = history[index]
        if isinstance(message, ModelRequest) and any(isinstance(p, UserPromptPart) for p in message.parts):
            return history[index:]
    return []
