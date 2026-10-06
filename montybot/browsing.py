"""The agent's browser: thin calls to the browser service, bound to the run's own browser.

Every call that touches the browser is a DBOS step, so a run that recovers after a restart replays the recorded result
instead of clicking again. The service keys browsers by run id, so the browser outlives an attempt of the run and
waits through a hand-off. Monty never sees a run id or a user id: the functions here fill them in from the run.

What the agent reads is a snapshot of the page (`montybot.browser.contract.Snapshot`). It never reaches a log or a
trace; only the host of a page the agent opens goes into the run's activity, which the user sees.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from dbos import DBOS
from pydantic_ai import FunctionToolset, RunContext

from montybot import approvals, store
from montybot.browser.contract import (
    BrowserError,
    Click,
    ElementTarget,
    Navigate,
    Press,
    Ref,
    Selector,
    Type,
)
from montybot.browser.host import BrowserHost
from montybot.browser.service import HandoffNotActive, Restarted, UserBusy
from montybot.deps import RunDeps
from montybot.resources import Resources, current

SNAPSHOT_LIMIT = 12_000

browser_tools: FunctionToolset[RunDeps] = FunctionToolset(id='browser')


def target_of(target: str) -> ElementTarget:
    """A ref from the latest snapshot (`12`) or a CSS selector (`#add-eggs`)."""
    target = target.strip().removeprefix('[').removesuffix(']')
    return Ref(ref=target) if target.isdigit() else Selector(css=target)


class Session:
    """The browser calls of one run, for code that runs inside a DBOS step."""

    def __init__(self, resources: Resources, run_id: str, user_id: str) -> None:
        self.browser: BrowserHost = resources.browser
        self.resources = resources
        self.run_id = run_id
        self.user_id = user_id
        self.notes: list[str] = []

    def _note(self, restarted: Restarted | None) -> None:
        if restarted is not None:
            self.notes.append(f'Note: the browser was restarted ({restarted.reason}) and reopened at {restarted.url}.')

    async def start(self) -> None:
        started = await self.browser.start(run_id=self.run_id, user_id=self.user_id)
        self._note(started.restarted)

    async def act(self, action: Navigate | Click | Type | Press) -> None:
        await self.start()
        result = await self.browser.act(run_id=self.run_id, user_id=self.user_id, action=action)
        self._note(result.restarted)

    async def read(self) -> str:
        await self.start()
        result = await self.browser.snapshot(run_id=self.run_id, user_id=self.user_id)
        self._note(result.restarted)
        snapshot = result.snapshot
        text = snapshot.text if len(snapshot.text) <= SNAPSHOT_LIMIT else snapshot.text[:SNAPSHOT_LIMIT] + '\n[cut]'
        return '\n'.join([*self.notes, f'URL: {snapshot.url}', f'Title: {snapshot.title}', '', text])

    async def activity(self, text: str) -> None:
        async with self.resources.pool.connection() as connection:
            await store.add_activity(connection, self.run_id, text)


async def browser_step(ctx: RunContext[RunDeps], name: str, use: Callable[[Session], Awaitable[str]]) -> str:
    """Run `use` as one DBOS step on the run's browser. Errors the browser raises on purpose are safe to show the
    model, so they come back as text it can act on."""

    async def step() -> str:
        session = Session(current(), ctx.deps.run_id, ctx.deps.user_id)
        try:
            return await use(session)
        except UserBusy:
            return 'Error: another of your tasks is using the browser right now. Try again when it has finished.'
        except BrowserError as error:
            return f'Error: {error}'

    return await DBOS.run_step_async({'name': f'browser.{name}'}, step)


def host_of(url: str) -> str:
    return urlsplit(url).hostname or url


@browser_tools.tool
async def open_page(ctx: RunContext[RunDeps], url: str) -> str:
    """Open `url` (absolute, with https://) in your browser and return what the page shows."""

    async def use(session: Session) -> str:
        await session.activity(f'Opening {host_of(url)}')
        await session.act(Navigate(url=url))
        return await session.read()

    return await browser_step(ctx, 'open', use)


@browser_tools.tool
async def read_page(ctx: RunContext[RunDeps]) -> str:
    """What the current page shows: its text, and numbered refs such as [12] for the things you can click or type
    into."""
    return await browser_step(ctx, 'read', lambda session: session.read())


@browser_tools.tool
async def click(ctx: RunContext[RunDeps], target: str) -> str:
    """Click a ref from the latest page (`12`) or a CSS selector (`#add-eggs`), then return the page.

    Not for anything that spends money or sends something as the user: use `commit` for that."""

    async def use(session: Session) -> str:
        await session.act(Click(target=target_of(target)))
        return await session.read()

    return await browser_step(ctx, 'click', use)


@browser_tools.tool
async def type_text(ctx: RunContext[RunDeps], target: str, text: str, press_enter: bool = False) -> str:
    """Replace the value of a text box (a ref or a CSS selector) with `text`, optionally press Enter, then return the
    page. Never type passwords: hand the browser to the user instead."""

    async def use(session: Session) -> str:
        await session.act(Type(text=text, target=target_of(target)))
        if press_enter:
            await session.act(Press(key='Enter'))
        return await session.read()

    return await browser_step(ctx, 'type', use)


@browser_tools.tool
async def press_key(ctx: RunContext[RunDeps], key: str) -> str:
    """Press one key, such as `Enter`, `Escape` or `ArrowDown`, then return the page."""

    async def use(session: Session) -> str:
        await session.act(Press(key=key))
        return await session.read()

    return await browser_step(ctx, 'press', use)


@browser_tools.tool(requires_approval=True)
async def commit(ctx: RunContext[RunDeps], target: str, description: str) -> str:
    """Click something that cannot be undone, such as placing an order, paying, booking or sending a message as the
    user. The user is asked first, with `description` ("Place the order for eggs, milk and bread: $12.40")."""

    async def use(session: Session) -> str:
        await session.activity(description)
        await session.act(Click(target=target_of(target)))
        return await session.read()

    return await browser_step(ctx, 'commit', use)


@browser_tools.tool
async def hand_off(ctx: RunContext[RunDeps], reason: str) -> str:
    """Give the browser to the user for a step you must not or cannot do: a password, a 2FA code, a CAPTCHA or
    press-and-hold check, a payment form. `reason` is shown to them ("Please sign in to the shop"). Waits until they
    hand it back, then returns the page they left it on."""

    async def start(session: Session) -> str:
        await session.start()
        await session.browser.save_state(run_id=session.run_id, user_id=session.user_id)
        handoff = await session.browser.start_handoff(run_id=session.run_id, user_id=session.user_id, reason=reason)
        return handoff.handoff_id

    handoff_id = await browser_step(ctx, 'handoff.start', start)
    if handoff_id.startswith('Error: '):
        return handoff_id
    reply = await approvals.ask(ctx, 'handoff', reason, {'handoff_id': handoff_id})

    async def end(session: Session) -> str:
        try:
            await session.browser.end_handoff(run_id=session.run_id, user_id=session.user_id, handoff_id=handoff_id)
        except HandoffNotActive:
            pass  # ended already, by an earlier attempt of this step
        return await session.read()

    page = await browser_step(ctx, 'handoff.end', end)
    if reply is None:
        return f'The user did not take the browser in time. The page now:\n{page}'
    note = str(reply.get('note') or '').strip()
    said = f' They said: "{note}".' if note else ''
    return f'The user handed the browser back.{said} The page now:\n{page}'
