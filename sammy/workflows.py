# Grown from viktor c1896df (viktor/handlers.py `execute`): a run is a DBOS workflow instead of a pgtask task.
"""A run is one DBOS workflow, whose id is the run's id.

```
POST /api/threads/<id>/messages  (sammy.api)
  store.create_run, then DBOS.start_workflow(run_thread, run_id) with workflow id = run_id
run_thread(run_id)                                   @DBOS.workflow
  step run.start        status running, load the run and the thread's history, the message's files into /work/uploads
  agent.run(...)        model requests are steps (DBOSDurability); browser and memory calls are our own steps;
                        ask_user, approvals and hand-offs wait in DBOS.recv (sammy.approvals)
  step run.finish       append the new messages (files as notes: sammy.attachments), status done (or failed)
  step run.close        the browser service saves the user's sign-ins and closes the run's browser
```

If the app stops, DBOS starts the workflow again when the app comes back (same `executor_id`), and every finished
step returns its recorded result instead of running again.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import logfire
from dbos import DBOS, SetWorkflowID, StepOptions, WorkflowHandleAsync
from dbos._error import DBOSAwaitedWorkflowCancelledError, DBOSException, DBOSWorkflowCancelledError
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from sammy import attachments, store, streaming
from sammy.browser.contract import BrowserError
from sammy.browser.service import UnknownRun
from sammy.deps import RunDeps
from sammy.models import FINISHED, Attachment, Run, RunStatus, Schedule
from sammy.observability import timed, timing
from sammy.resources import Resources, current

RETRIED: StepOptions = {'retries_allowed': True, 'max_attempts': 5, 'interval_seconds': 1.0}
"""For the steps that end a run: a passing database error must not leave a run unfinished and the browser open."""

logger = logging.getLogger(__name__)


class HideStoppedRuns(logging.Filter):
    """DBOS logs a cancelled workflow as an error with its traceback. A run the user stopped (`stop`) is cancelled on
    purpose, so that would bury real errors in noise."""

    def filter(self, record: logging.LogRecord) -> bool:
        error = record.exc_info[1] if record.exc_info else None
        return not isinstance(error, (DBOSAwaitedWorkflowCancelledError, DBOSWorkflowCancelledError))


logging.getLogger('dbos').addFilter(HideStoppedRuns())

FAILURE_NOTICE = 'Something went wrong while working on this, and I could not finish. Please try again.'
STOPPED_NOTICE = 'You stopped this.'

# Why a task failed, in words the user can act on, by the error's type (its text stays private: it can quote the
# user's content). A type not listed gets FAILURE_NOTICE.
_SERVICE = 'The AI service I use returned an error, so I could not finish. Please try again in a little while.'
_BROWSER = 'My browser stopped working partway through, so I could not finish. Please try again.'
FAILURE_NOTICES = {
    'ClaudeCodeSignInExpiredError': (
        "I can't use the AI service right now: this server's sign-in to it has expired and needs renewing. "
        'Please try again later.'
    ),
    'ModelHTTPError': _SERVICE,
    'ModelAPIError': _SERVICE,
    'ConcurrencyLimitExceeded': _SERVICE,
    'FallbackExceptionGroup': _SERVICE,
    'UsageLimitExceeded': (
        'This took more steps than I am allowed for one task, so I stopped. '
        'Try again, or ask for a smaller part of it first.'
    ),
    'UnexpectedModelBehavior': (
        'I got muddled partway through and could not finish. Please try again, perhaps in other words.'
    ),
    'ContentFilterError': "The AI service I use declined to help with this, so I couldn't finish.",
    'TimeoutError': 'A page or service took too long to answer, so I could not finish. Please try again.',
    'UserBusy': 'My browser was busy with another of your tasks. Please try again when that one is done.',
    'BrowserError': _BROWSER,
    'ActionFailed': _BROWSER,
    'LifecycleError': _BROWSER,
    'TargetNotFound': _BROWSER,
    'CDPError': _BROWSER,
    'CDPClosed': _BROWSER,
}


def failure_notice(error: BaseException) -> str:
    """What the user reads when a task fails with this error: the notice of its type, or of the nearest type it
    derives from that has one (`IncompleteToolCall` is an `UnexpectedModelBehavior`)."""
    for kind in type(error).__mro__:
        if kind.__name__ in FAILURE_NOTICES:
            return FAILURE_NOTICES[kind.__name__]
    return FAILURE_NOTICE


@DBOS.workflow(name='sammy.run_thread_stream')  # the name runs were recorded under; keep it so they resume
async def run_thread(run_id: str) -> str:
    # Baggage puts run_id on every span of the run, model and browser calls included.
    with timing('run.lifecycle') as lifecycle, logfire.set_baggage(run_id=run_id):
        resources = current()
        streaming.reset(run_id)
        started = await DBOS.run_step_async({'name': 'run.start'}, start_run, resources, run_id)
        # Runs recorded before the user's local time was added replay a 3-tuple.
        run, history_json, schedule = started[:3]
        local_time = started[3] if len(started) > 3 else ''
        squirrel_name = started[4] if len(started) > 4 else ''  # and before the squirrel's name, a 4-tuple
        files: list[Attachment] = started[5] if len(started) > 5 else []  # and before files, a 5-tuple
        if run.status in FINISHED:
            return run.status  # stopped before its workflow started
        lifecycle.set_attributes({'thread_id': run.thread_id, 'user_id': run.user_id, 'trigger': run.trigger})
        history = recent(ModelMessagesTypeAdapter.validate_json(history_json), resources.settings.history_limit)
        # The files go in outside a step, so DBOS records no file's bytes: a replay reads the same rows again.
        asked = ModelRequest(parts=[UserPromptPart(content=attachments.prompt(run.prompt, files))])
        *history, asked = await attachments.with_files(resources.pool, run.user_id, [*history, asked])
        prompt = next(p.content for p in asked.parts if isinstance(p, UserPromptPart))
        deps = RunDeps(
            resources=resources, run=run, schedule=schedule, local_time=local_time, squirrel_name=squirrel_name
        )
        try:
            try:
                with timing('run.agent'):
                    result = await resources.agent.run(prompt, deps=deps, message_history=history)
            except Exception as error:
                logfire.error('Run {run_id} failed: {error_type}', run_id=run_id, error_type=type(error).__qualname__)
                # Also in this process's own log, for running without Logfire. The type only: an error's text can
                # quote the user's content.
                logger.warning('Run %s failed: %s', run_id, type(error).__qualname__)
                await DBOS.run_step_async(
                    {**RETRIED, 'name': 'run.failed'},
                    fail_run,
                    resources,
                    run,
                    type(error).__name__,
                    failure_notice(error),
                )
                if isinstance(error, DBOSException):
                    raise  # a replay that does not match its recording is a bug to see, not a failed task
                return 'failed'
            new_messages = ModelMessagesTypeAdapter.dump_json(attachments.without_files(result.new_messages()))
            await DBOS.run_step_async(
                {**RETRIED, 'name': 'run.finish'}, finish_run, resources, run, new_messages, result.output
            )
            return 'done'
        finally:
            try:
                await DBOS.run_step_async({**RETRIED, 'name': 'run.close'}, close_browser, resources, run)
            finally:
                streaming.discard(run_id)


# The workflow starts in this context: a run an app traced joins the app's trace; any other run is a trace of its own,
# with `run.lifecycle` at the top.
@timed('run.dispatch', only_in_trace=True)
async def start(run_id: str) -> WorkflowHandleAsync[str]:
    with SetWorkflowID(run_id):
        return await DBOS.start_workflow_async(run_thread, run_id)


async def start_run(
    resources: Resources, run_id: str
) -> tuple[Run, bytes, Schedule | None, str, str, list[Attachment]]:
    with timing('run.start'):
        async with resources.pool.connection() as connection, connection.transaction():
            run = await store.load_run(connection, run_id)
            await store.set_run_status(connection, run_id, 'running')
            history = await store.load_history(connection, run.thread_id)
            schedule = await store.schedule_of_thread(connection, run.thread_id) if run.trigger == 'schedule' else None
            user = await store.get_user(connection, run.user_id)
        timezone = user.timezone if user is not None else 'UTC'
        squirrel_name = user.squirrel_name if user is not None else ''
        files = await attachments.into_workspace(resources, run.user_id, run.id)
        history_json = ModelMessagesTypeAdapter.dump_json(history)
        return run, history_json, schedule, local_time_in(timezone), squirrel_name, files


def local_time_in(timezone: str) -> str:
    """The time now in `timezone`, in words: "Tuesday 6 October 2026, 21:40 (Europe/London)"."""
    now = datetime.now(ZoneInfo(timezone))
    return f'{now:%A} {now.day} {now:%B %Y, %H:%M} ({timezone})'


async def finish_run(resources: Resources, run: Run, new_messages: bytes, output: str) -> None:
    # A visible reply promises the user's lease is free. Keep existing DBOS step order for paused-run replay;
    # cleanup belongs to this retried terminal step, and the final run.close remains idempotent.
    await close_browser(resources, run)
    with timing('run.finish'):
        async with resources.pool.connection() as connection, connection.transaction():
            if await store.lock_finished(connection, run.id):
                return  # this step ran before and committed, but DBOS had not recorded it
            await store.append_history(connection, run.thread_id, ModelMessagesTypeAdapter.validate_json(new_messages))
            await store.finish_run(connection, run.id, 'done', output=output)


async def fail_run(resources: Resources, run: Run, error_type: str, notice: str = FAILURE_NOTICE) -> None:
    await close_browser(resources, run)
    with timing('run.fail'):
        await end_run(resources, run, 'failed', notice, error=error_type)


async def stop(resources: Resources, run: Run) -> bool:
    """The user stops their run, whatever it is doing or waiting for. False if it had finished already.

    DBOS cancels the workflow at its next step, so it makes no more model calls or browser actions; a workflow that
    wakes up later finds the run finished and changes nothing. The browser is saved and closed here, which frees it
    for the user's other chats."""
    await DBOS.cancel_workflow_async(run.id)
    stopped = await end_run(resources, run, 'stopped', STOPPED_NOTICE)
    await close_browser(resources, run)
    return stopped


async def end_run(resources: Resources, run: Run, status: RunStatus, notice: str, error: str | None = None) -> bool:
    """Finish a run that has no reply of its own: the thread gets its prompt and `notice` as the reply. False if the
    run had finished already."""
    async with resources.pool.connection() as connection, connection.transaction():
        if await store.lock_finished(connection, run.id):
            return False
        files = await attachments.users_files(connection, run.id)
        await store.append_history(
            connection,
            run.thread_id,
            [
                ModelRequest(parts=[UserPromptPart(content=attachments.prompt(run.prompt, files))]),
                ModelResponse(parts=[TextPart(content=notice)]),
            ],
        )
        await store.finish_run(connection, run.id, status, output=notice, error=error)
        await store.close_open_asks(connection, run.id)
    return True


async def close_browser(resources: Resources, run: Run) -> None:
    """Save the user's sign-ins and close the run's browser, if it opened one."""
    with timing('run.close'):
        try:
            await resources.browser.close(run_id=run.id, user_id=run.user_id)
        except UnknownRun:
            pass
        except BrowserError:  # the browser is closed either way; the lease is released
            logfire.warn('Closing the browser of run {run_id} failed', run_id=run.id)


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


@timed('run.start_queued')
async def start_queued(resources: Resources) -> int:
    """Start the workflow of every run still queued, in case the app stopped between recording a run and starting
    it. Starting a workflow that exists is harmless."""
    async with resources.pool.connection() as connection:
        cursor = await connection.execute("SELECT id FROM sammy.runs WHERE status = 'queued'")
        run_ids = [str(row['id']) for row in await cursor.fetchall()]
    for run_id in run_ids:
        await start(run_id)
    return len(run_ids)
