"""Subagents: the agent splits a task into jobs that run side by side, each in a run of its own (#132).

```
run_thread (the parent's workflow)
  run_subagents(jobs)                        a tool, in workflow code
    step subagents.create.<n>                a run per job, in the parent's thread (store.create_subagent_runs)
    DBOS.start_workflow_async per job  --->  run_subagent(run_id)                   @DBOS.workflow, id = the run's id
                                               step subagent.start                  status running
                                               the subagent agent.run(task)         its own context, its own tab
                                                 asks and hand-offs (sammy.approvals), shown in the parent's thread
                                               step subagent.finish                 status done, with the answer
                                               step subagent.close                  save the sign-ins, close the tab
    handle.get_result() per job        <---  (answer, tokens used)
  <- only the answers
```

Each job is a DBOS child workflow whose id is its run's id, so a restart resumes it, and the parent's replay finds
the same children and their results. A job's run belongs to the parent's user, so it has the user's sign-ins, a tab
of the user's browser (`BrowserHost` with `share_browser`; without tabs the tool is not offered), and, in a scheduled
run, the schedule's approval of `commit`. Its asks wait in its own `DBOS.recv`, and the web app shows them in the
parent's thread (`store.open_ask`). The parent's context gets the answers only, never a job's pages.

One call starts at most `SUBAGENT_MAX_JOBS` jobs. All the jobs of one run share `SUBAGENT_TOKEN_BUDGET` tokens: a call
splits what is left between its jobs, and a job that reaches its share stops.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

import logfire
from dbos import DBOS, SetWorkflowID, WorkflowHandleAsync
from dbos._error import DBOSException
from pydantic_ai import FunctionToolset, RunContext, ToolDefinition
from pydantic_ai.usage import UsageLimits

from sammy import store
from sammy.deps import RunDeps
from sammy.models import FINISHED, Run, RunStatus, Schedule
from sammy.observability import timing
from sammy.resources import Resources, current
from sammy.workflows import RETRIED, STOPPED_NOTICE, close_browser, failure_notice, local_time_in

ANSWER_LIMIT = 4_000
"""The most characters of one job's answer the parent gets."""
MIN_TOKENS = 10_000
"""A job gets at least this many tokens, or the call is refused: less is not enough to read one page."""

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
You are a helper: Sammy gave you one part of a bigger task, to do in your own browser tab while other helpers do the
other parts. Do only your part. When it is done, answer with just what the task asks for (facts, prices, URLs) in a
few lines; your answer goes to Sammy, not to the user. If you cannot finish, say why in one line."""


@dataclass
class Job:
    task: str
    """What to find or do and what to answer with, complete on its own: the helper sees nothing else."""
    sites: list[str] = field(default_factory=list[str])
    """The sites to use, such as `https://shop.example`, if the task names them."""


subagent_tools: FunctionToolset[RunDeps] = FunctionToolset(id='subagents')


async def only_with_tabs(ctx: RunContext[RunDeps], tool: ToolDefinition) -> ToolDefinition | None:
    """Offered only where a user's runs share one browser in tabs: elsewhere a second run of the user's waits for
    the first to free the browser."""
    return tool if ctx.deps.resources.browser.shares_browser else None


@subagent_tools.tool(prepare=only_with_tabs)
async def run_subagents(ctx: RunContext[RunDeps], jobs: list[Job]) -> str:
    """Hand independent parts of a task to helpers that work at the same time, each in its own tab of your browser
    with the user's sign-ins, and get back only their answers. Use it when a task splits into parts that do not
    depend on each other, such as the same product on several sites: one job per site. Each job's `task` must stand
    alone, as the helper sees nothing else: what to find or do, and what to answer with ("Find the price of the Acme
    kettle at https://shop.example; answer with the price and the product's URL"). Helpers ask the user and hand
    over the browser themselves when they must. A few jobs per call; the tool says if there are too many."""
    deps = ctx.deps
    settings = deps.resources.settings
    if not jobs:
        return 'Error: give at least one job.'
    if len(jobs) > settings.subagent_max_jobs:
        return f'Error: at most {settings.subagent_max_jobs} jobs at a time; split them over several calls.'
    tokens = (settings.subagent_token_budget - deps.subagents.tokens) // len(jobs)
    if tokens < MIN_TOKENS:
        return 'Error: the helpers have used up their budget for this task. Do the rest yourself.'
    call = deps.subagents.next()
    tasks = [(child_id(deps.run_id, call, index), task_of(job)) for index, job in enumerate(jobs)]
    await DBOS.run_step_async({'name': f'subagents.create.{call}'}, create_runs, deps.resources, deps.run, tasks)
    handles: list[WorkflowHandleAsync[tuple[str, int]]] = []
    for run_id, _ in tasks:
        with SetWorkflowID(run_id):
            handles.append(await DBOS.start_workflow_async(run_subagent, run_id, tokens))
    answers: list[str] = []
    for number, (job, handle) in enumerate(zip(jobs, handles, strict=True), start=1):
        answer, used = await handle.get_result()
        deps.subagents.tokens += used
        answers.append(f'Job {number}: {job.task}\nAnswer: {answer}')
    return '\n\n'.join(answers)


def child_id(run_id: str, call: int, index: int) -> str:
    """The same on every replay of the parent, so a restart finds the jobs it started."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'sammy:subagent:{run_id}:{call}:{index}'))


def task_of(job: Job) -> str:
    sites = ', '.join(site.strip() for site in job.sites if site.strip())
    return f'{job.task.strip()}\n\nSites: {sites}' if sites else job.task.strip()


async def create_runs(resources: Resources, parent: Run, tasks: list[tuple[str, str]]) -> None:
    with timing('subagents.create'):
        async with resources.pool.connection() as connection, connection.transaction():
            await store.create_subagent_runs(connection, parent, tasks)


@DBOS.workflow(name='sammy.run_subagent')
async def run_subagent(run_id: str, tokens: int) -> tuple[str, int]:
    """One job, in a run of its own with at most `tokens` tokens. Returns its answer for the parent and the tokens it
    used: all of `tokens` if it failed, as nothing says how many it had used."""
    with timing('subagent.lifecycle'), logfire.set_baggage(run_id=run_id):
        resources = current()
        run, schedule, local_time = await DBOS.run_step_async({'name': 'subagent.start'}, start, resources, run_id)
        if run.status in FINISHED:
            return run.output or STOPPED_NOTICE, 0  # stopped before its workflow started
        deps = RunDeps(resources=resources, run=run, schedule=schedule, local_time=local_time)
        try:
            try:
                with timing('subagent.agent'):
                    result = await resources.subagent.run(
                        run.prompt, deps=deps, usage_limits=UsageLimits(total_tokens_limit=tokens)
                    )
            except Exception as error:
                # The type only: an error's text can quote the user's content.
                logfire.warn('Subagent {run_id} failed: {error_type}', run_id=run_id, error_type=type(error).__name__)
                logger.warning('Subagent %s failed: %s', run_id, type(error).__qualname__)
                notice = failure_notice(error)
                await DBOS.run_step_async(
                    {**RETRIED, 'name': 'subagent.failed'},
                    end,
                    resources,
                    run_id,
                    'failed',
                    notice,
                    type(error).__name__,
                )
                if isinstance(error, DBOSException):
                    raise  # a replay that does not match its recording is a bug to see, not a failed job
                return f'The helper could not finish: {notice}', tokens
            answer = result.output.strip()[:ANSWER_LIMIT] or '(no answer)'
            await DBOS.run_step_async({**RETRIED, 'name': 'subagent.finish'}, end, resources, run_id, 'done', answer)
            return answer, result.usage.total_tokens
        finally:
            await DBOS.run_step_async({**RETRIED, 'name': 'subagent.close'}, close_browser, resources, run)


async def start(resources: Resources, run_id: str) -> tuple[Run, Schedule | None, str]:
    with timing('subagent.start'):
        async with resources.pool.connection() as connection, connection.transaction():
            run = await store.load_run(connection, run_id)
            await store.set_run_status(connection, run_id, 'running')
            schedule = await store.schedule_of_thread(connection, run.thread_id) if run.trigger == 'schedule' else None
            user = await store.get_user(connection, run.user_id)
        return run, schedule, local_time_in(user.timezone if user is not None else 'UTC')


async def end(resources: Resources, run_id: str, status: RunStatus, output: str, error: str | None = None) -> None:
    with timing('subagent.finish'):
        async with resources.pool.connection() as connection, connection.transaction():
            if await store.lock_finished(connection, run_id):
                return  # stopped meanwhile, or this step ran before and committed
            await store.finish_run(connection, run_id, status, output=output, error=error)
