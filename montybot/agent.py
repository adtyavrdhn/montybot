# Grown from viktor c1896df (viktor/agent.py): new instructions and tools, and DBOSDurability in place of
# PGTaskDurability.
"""The agent. Built once per process, before `DBOS.launch()`, so DBOSDurability can register its steps."""

from __future__ import annotations

from pydantic_ai import Agent, FunctionToolset, RunContext
from pydantic_ai.capabilities import HandleDeferredToolCalls
from pydantic_ai.durable_exec.dbos import DBOSDurability
from pydantic_ai.models import Model

from montybot import approvals
from montybot.browsing import browser_tools
from montybot.deps import RunDeps
from montybot.memory import memory_tools, recall

INSTRUCTIONS = """\
You are monty-bot, a personal assistant that does things for the user on the web, in your own browser.

- Work in the browser with your tools. Read the page before you act, and read it again after.
- Never type a password, a 2FA code or card details, and never solve a CAPTCHA or a "press and hold" check yourself.
  Call `hand_off` with a short reason; the user does that step in your browser and hands it back.
- Anything that cannot be undone (placing an order, paying, booking, sending a message as the user) goes through
  `commit`, which asks the user first. Everything else, just do.
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


def build_agent(model: Model | str) -> Agent[RunDeps, str]:
    return Agent[RunDeps, str](
        model,
        name='montybot',
        deps_type=RunDeps,
        instructions=[INSTRUCTIONS, recall],
        toolsets=[browser_tools, user_tools, memory_tools],
        capabilities=[HandleDeferredToolCalls(handler=approvals.handle_approvals), DBOSDurability()],
    )
