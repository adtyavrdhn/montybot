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

from montybot import approvals, streaming
from montybot.browsing import browser_tools
from montybot.code import INSTRUCTIONS as CODE_INSTRUCTIONS
from montybot.code import code_tools
from montybot.cpython import INSTRUCTIONS as CPYTHON_INSTRUCTIONS
from montybot.cpython import cpython_tools
from montybot.deps import RunDeps
from montybot.jev import jev_tools
from montybot.memory import memory_tools, recall
from montybot.schedule_tools import INSTRUCTIONS as SCHEDULE_INSTRUCTIONS
from montybot.schedule_tools import schedule_tools, scheduled_run

INSTRUCTIONS = """\
You are monty-bot, a personal assistant that does things for the user on the web, in your own browser.

- Work in your browser through `run_code`. Read the page before you act, and read it again after.
- Never type a password, a 2FA code or card details, and never solve a CAPTCHA or a "press and hold" check yourself.
  Call `hand_off` with a short reason; the user does that step in your browser and hands it back.
- Anything that cannot be undone (placing an order, paying, booking, sending a message as the user) goes through
  `commit` (a native tool, with a ref or selector), which asks the user first. Everything else, just do.
- If something is unclear, ask with `ask_user`; otherwise do not stop to ask.
- Make every step safe to repeat: before adding to a cart, check what is in it.
- Answer in plain words and keep it short. Tables are fine."""

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


CACHE = AnthropicModelSettings(
    # Automatic caching conflicts with explicit message breakpoints. Override model defaults too.
    anthropic_cache=False,
    anthropic_cache_instructions=True,
    anthropic_cache_tool_definitions=True,
    anthropic_cache_messages=True,
)
"""Anthropic prompt caching on everything that repeats between a run's model calls: the instructions, the tool
definitions and the conversation so far (page snapshots included). Other providers ignore these keys."""


def build_agent(model: Model | str, *, jev: bool = False) -> Agent[RunDeps, str]:
    """Tools run one at a time: they number their DBOS steps as they go, and an ask must be the run's only one."""
    return Agent[RunDeps, str](
        model,
        name='montybot_stream',  # DBOS records model steps under this name; keep it so paused runs resume
        deps_type=RunDeps,
        instructions=[
            INSTRUCTIONS,
            (
                'Optional Jev tools advise on user direction and ambiguous navigation. Use classify_intent when direction '
                'is unclear, suggest_navigation when choosing a link is unclear. They do not click or grant approval.'
                if jev
                else ''
            ),
            CODE_INSTRUCTIONS,
            CPYTHON_INSTRUCTIONS,
            SCHEDULE_INSTRUCTIONS,
            user_time,
            recall,
            scheduled_run,
        ],
        toolsets=[
            code_tools,
            cpython_tools,
            browser_tools,
            user_tools,
            memory_tools,
            schedule_tools,
            *([jev_tools] if jev else []),
        ],
        capabilities=[
            HandleDeferredToolCalls(handler=approvals.handle_approvals),
            DBOSDurability(
                parallel_execution_mode='sequential',
                # Scripted models without a stream function cannot stream.
                event_stream_handler=(
                    None if isinstance(model, FunctionModel) and model.stream_function is None else streaming.handler
                ),
            ),
        ],
        model_settings=CACHE,
    )
