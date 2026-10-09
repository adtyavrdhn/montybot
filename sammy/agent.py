# Grown from viktor c1896df (viktor/agent.py): new instructions and tools, and DBOSDurability in place of
# PGTaskDurability.
"""The agent. Built once per process, before `DBOS.launch()`, so DBOSDurability can register its steps."""

from __future__ import annotations

from pydantic_ai import Agent, FunctionToolset, RunContext
from pydantic_ai.capabilities import HandleDeferredToolCalls
from pydantic_ai.durable_exec.dbos import DBOSDurability
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModelSettings
from pydantic_ai.models.function import FunctionModel

from sammy import approvals, streaming
from sammy.attachments import INSTRUCTIONS as FILE_INSTRUCTIONS
from sammy.attachments import file_tools
from sammy.browsing import browser_tools
from sammy.code import INSTRUCTIONS as CODE_INSTRUCTIONS
from sammy.code import code_tools
from sammy.cpython import INSTRUCTIONS as CPYTHON_INSTRUCTIONS
from sammy.cpython import cpython_tools
from sammy.deps import RunDeps
from sammy.integration_tools import INSTRUCTIONS as INTEGRATION_INSTRUCTIONS
from sammy.integration_tools import connected_integrations, integration_tools
from sammy.memory import memory_tools, recall
from sammy.schedule_tools import INSTRUCTIONS as SCHEDULE_INSTRUCTIONS
from sammy.schedule_tools import schedule_tools, scheduled_run
from sammy.steering import Steering

INSTRUCTIONS = """\
You are Sammy, a flying squirrel with a browser and opinions. You do things for the user on the web, in your own
browser, and you are good at it.

How you talk:
- Sassy, playful and warm: a sharp friend who happens to be great at errands, never a corporate help desk. A little
  teasing is fine; being mean, smug or snide about the user is not.
- Short and direct. Lead with the answer or what you did, then the joke if there is room for one. One quip beats three.
- No assistant boilerplate: no "Certainly!", "Great question", "I'd be happy to help", "As an AI", "I hope this
  helps" or "Let me know if you need anything else". No apologising for existing. Do not narrate what you are about to
  do; do it.
- Have opinions when they help ("the cheaper one is fine, the extra £40 buys you a nicer logo"), and say plainly when
  something went wrong.
- Questions, approvals and hand-offs stay crystal clear: the user must know exactly what you ask or are about to do.
  Keep the sass out of anything about money, passwords or sending things as the user.

How you work:
- Work in your browser through `run_code`. Read the page before you act, and read it again after.
- Never type a password, a 2FA code or card details, and never solve a CAPTCHA or a "press and hold" check yourself.
  Call `hand_off` with a short reason; the user does that step in your browser and hands it back.
- Anything that cannot be undone (placing an order, paying, booking, sending a message as the user) goes through
  `commit` (a native tool, with a ref or selector), which asks the user first. Everything else, just do.
- If something is unclear, ask with `ask_user`; otherwise do not stop to ask.
- Make every step safe to repeat: before adding to a cart, check what is in it.
- Answer in plain words. Tables are fine."""

user_tools: FunctionToolset[RunDeps] = FunctionToolset(id='user')


@user_tools.tool
async def ask_user(ctx: RunContext[RunDeps], question: str) -> str:
    """Ask the user a question and wait for the answer. Only when you cannot reasonably go on without it."""
    reply = await approvals.ask(ctx, 'question', question)
    if reply is None:
        return 'The user did not answer in time.'
    return str(reply.get('text', ''))


def user_time(ctx: RunContext[RunDeps]) -> str:
    """The user's date, time and time zone, so "today", "next Friday" and "9am" mean what they mean to the user."""
    if not ctx.deps.local_time:
        return ''
    return (
        f'For the user it is now {ctx.deps.local_time}. Use their time zone for dates, times and schedules unless '
        'they name another.'
    )


def your_name(ctx: RunContext[RunDeps]) -> str:
    """The name the user gave their squirrel in the Mac app: that squirrel is Sammy, so Sammy answers to it."""
    name = ctx.deps.squirrel_name
    if not name:
        return ''
    return (
        f'The user named you {name!r}: that is your name now (Sammy is just the app). Answer to it, and use it when '
        "it is natural, such as signing off or talking about yourself; don't shoehorn it into every reply."
    )


CACHE = AnthropicModelSettings(
    # Automatic caching conflicts with explicit message breakpoints. Override model defaults too.
    anthropic_cache=False,
    anthropic_cache_instructions=True,
    anthropic_cache_tool_definitions=True,
    anthropic_cache_messages=True,
)
"""Anthropic prompt caching on everything that repeats between a run's model calls: the instructions, the tool
definitions and the conversation so far (page snapshots included). Other providers ignore these keys."""


def build_agent(model: Model | str) -> Agent[RunDeps, str]:
    """Tools run one at a time: they number their DBOS steps as they go, and an ask must be the run's only one."""
    return Agent[RunDeps, str](
        model,
        name='sammy',  # what Logfire shows (`invoke_agent sammy`); DBOS step names are DBOSDurability's `name`
        deps_type=RunDeps,
        instructions=[
            INSTRUCTIONS,
            CODE_INSTRUCTIONS,
            CPYTHON_INSTRUCTIONS,
            FILE_INSTRUCTIONS,
            SCHEDULE_INSTRUCTIONS,
            INTEGRATION_INSTRUCTIONS,
            user_time,
            your_name,
            connected_integrations,
            recall,
            scheduled_run,
        ],
        toolsets=[
            code_tools,
            cpython_tools,
            file_tools,
            browser_tools,
            user_tools,
            memory_tools,
            schedule_tools,
            integration_tools,
        ],
        capabilities=[
            HandleDeferredToolCalls(handler=approvals.handle_approvals),
            Steering(),  # what the user sends while the run works joins its next model request
            DBOSDurability(
                # The name runs' model steps were recorded under, from when there was a streaming and a
                # non-streaming agent; keep it so paused runs resume.
                name='sammy_stream',
                parallel_execution_mode='sequential',
                # Scripted models without a stream function cannot stream.
                event_stream_handler=(
                    None if isinstance(model, FunctionModel) and model.stream_function is None else streaming.handler
                ),
            ),
        ],
        model_settings=CACHE,
    )
