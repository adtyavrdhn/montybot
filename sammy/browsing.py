"""The agent's browser: thin calls to the browser service, bound to the run's own browser. Reading and clicking happen
in Monty (`sammy.code`); the two tools here pause the run or need the user's approval, so they are native tools.

Every call that touches the browser runs inside a DBOS step, so a run that recovers after a restart replays the
recorded result instead of clicking again. A `run_code` snippet is one step: if the app dies while it runs, the
recovered run runs the whole snippet again, clicks included, which is why every step must be safe to repeat. The service keys browsers by run id, so the browser outlives an attempt of the run and
waits through a hand-off. Monty never sees a run id or a user id: the functions here fill them in from the run.

What the agent reads is a snapshot of the page (`sammy.browser.contract.Snapshot`). It never reaches a log or a
trace; only the host of a page the agent opens goes into the run's activity, which the user sees.
"""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from dbos import DBOS
from pydantic_ai import ApprovalRequired, FunctionToolset, RunContext
from pydantic_ai.workspaces import WorkspaceError

from sammy import approvals, store
from sammy.browser.contract import (
    MAX_DOWNLOAD_BYTES,
    BrowserError,
    Click,
    ElementTarget,
    Navigate,
    NotSupported,
    Press,
    Ref,
    Selector,
    Type,
)
from sammy.browser.host import BrowserHost
from sammy.browser.service import HandoffNotActive, Restarted, UnknownRun, UserBusy
from sammy.browser.snapshot import DEFAULT_BUDGET
from sammy.deps import RunDeps
from sammy.resources import Resources, current
from sammy.workspaces import download_name, save_download

SNAPSHOT_LIMIT = DEFAULT_BUDGET

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
        self.downloaded: list[str] = []
        """What the browser downloaded since the last `read()`, saved in the user's files (#21)."""
        self.url = ''
        """The page the browser was on at the last `read()`."""

    def _note(self, restarted: Restarted | None) -> None:
        if restarted is not None:
            self.notes.append(f'Note: the browser was restarted ({restarted.reason}) and reopened at {restarted.url}.')

    async def start(self) -> None:
        started = await self.browser.start(run_id=self.run_id, user_id=self.user_id)
        self._note(started.restarted)
        # Renew the run's lease on the user's sign-ins (after `start`, which takes it on the first call).
        await self.resources.lease.renew(user_id=self.user_id, run_id=self.run_id)

    async def act(self, action: Navigate | Click | Type | Press) -> None:
        await self.start()
        result = await self.browser.act(run_id=self.run_id, user_id=self.user_id, action=action)
        self._note(result.restarted)

    async def read(self) -> str:
        await self.start()
        result = await self.browser.snapshot(run_id=self.run_id, user_id=self.user_id)
        self._note(result.restarted)
        await self.save_downloads()
        snapshot = result.snapshot
        self.url = snapshot.url
        text = snapshot.text if len(snapshot.text) <= SNAPSHOT_LIMIT else snapshot.text[:SNAPSHOT_LIMIT] + '\n[cut]'
        downloaded, self.downloaded = self.downloaded, []
        return '\n'.join([*self.notes, *downloaded, f'URL: {snapshot.url}', f'Title: {snapshot.title}', '', text])

    async def save_downloads(self) -> None:
        """Save what the browser downloaded into the user's files, and say where in the next `read()`."""
        downloads = await self.browser.take_downloads(run_id=self.run_id, user_id=self.user_id)
        if not downloads:
            return
        files = self.resources.workspaces.files(self.user_id)
        for download in downloads:
            name = download_name(download.name)
            if download.too_large or len(download.data) > MAX_DOWNLOAD_BYTES:
                self.downloaded.append(f'Download not saved: {name} is over {MAX_DOWNLOAD_BYTES >> 20} MB.')
                continue
            try:
                path = await save_download(files, download.name, download.data)
            except (OSError, WorkspaceError) as error:  # such as a file the code made where the folder goes
                reason = error.strerror if isinstance(error, OSError) and error.strerror else 'it could not be written'
                self.downloaded.append(f'Download not saved: {name}: {reason}.')
                continue
            self.downloaded.append(f'Downloaded: {path}')

    async def activity(self, text: str) -> None:
        async with self.resources.pool.connection() as connection:
            await store.add_activity(connection, self.run_id, text)


BROWSER_BUSY = (
    "the browser is in use by another of the user's chats, which is still working or waiting for them. Do not try "
    'again in this run: tell the user to finish or stop that chat, then ask again.'
)
"""What the agent is told when another run of the user's holds the browser. Retrying cannot help: that run may wait
for the user for hours."""


async def browser_step(ctx: RunContext[RunDeps], name: str, use: Callable[[Session], Awaitable[str]]) -> str:
    """Run `use` as one DBOS step on the run's browser. Errors the browser raises on purpose are safe to show the
    model, so they come back as text it can act on."""

    async def step() -> str:
        session = Session(current(), ctx.deps.run_id, ctx.deps.user_id)
        try:
            return await use(session)
        except UserBusy:
            return f'Error: {BROWSER_BUSY}'
        except BrowserError as error:
            return f'Error: {error}'

    return await DBOS.run_step_async({'name': f'browser.{name}'}, step)


def host_of(url: str) -> str:
    return urlsplit(url).hostname or url


async def refused_url(url: str, *, allow_private: bool) -> str | None:
    """Why the agent may not open `url`, or None. Only http(s), and only public addresses, so a page cannot steer the
    agent into our own network. On the server the browser's egress proxy (`browser/egress.py`) blocks the same ranges
    for redirects and subresources; this check gives the model a clear answer first."""
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return 'Error: only http and https addresses can be opened.'
    if allow_private:
        return None
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, parts.port or 443)
    except OSError:
        return None  # the browser reports the failed lookup itself
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            return 'Error: that address is on a private network, which you cannot open.'
    return None


@browser_tools.tool
async def commit(ctx: RunContext[RunDeps], target: str, description: str) -> str:
    """Click something that cannot be undone, such as placing an order, paying, booking or sending a message as the
    user. The user is asked first, with `description` ("Place the order for eggs, milk and bread: $12.40"), unless a
    schedule they approved started this task."""
    # The user approved a schedule's task when they set it up, and is not there when it runs: asking again would
    # leave every run waiting on them.
    if ctx.deps.schedule is None and not ctx.tool_call_approved:
        raise ApprovalRequired

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
        try:
            await session.browser.save_state(run_id=session.run_id, user_id=session.user_id)
        except NotSupported as error:
            if error.feature != 'export':
                raise
            # An engine that cannot export (stock Servo) keeps the session only while its browser is open, as
            # `approvals.save_browser` does for other waits: the hand-off still works, it just is not saved.
        handoff = await session.browser.start_handoff(run_id=session.run_id, user_id=session.user_id, reason=reason)
        return handoff.handoff_id

    handoff_id = await browser_step(ctx, 'handoff.start', start)
    if handoff_id.startswith('Error: '):
        return handoff_id
    reply = await approvals.ask(ctx, 'handoff', reason, {'handoff_id': handoff_id})
    the_ask = approvals.ask_id(ctx.deps.run_id, ctx.deps.asked.count)

    async def end(session: Session) -> str:
        # The live view starts a new hand-off if the browser service lost this one in a restart, and records its id
        # on the ask, so end whichever is current.
        async with session.resources.pool.connection() as connection:
            current_id = await store.handoff_of(connection, the_ask) or handoff_id
        try:
            await session.browser.end_handoff(run_id=session.run_id, user_id=session.user_id, handoff_id=current_id)
        except (HandoffNotActive, UnknownRun):
            pass  # ended already by an earlier attempt of this step, or lost in a restart
        return await session.read()

    page = await browser_step(ctx, 'handoff.end', end)
    note = str(reply.get('note') or '').strip()
    said = f' They said: "{note}".' if note else ''
    return f'The user handed the browser back.{said} The page now:\n{page}'
