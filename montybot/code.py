"""`run_code`: the agent's Python runs in Monty, and the browser is a set of host functions inside it.

```
agent: run_code(code)                                     one DBOS step (montybot.code.run_code)
  checkout a Monty session                                Full Monty: monty-server -> monty-worker (MONTY_URL)
  load_session(the run's last state)                      the state is an id in monty-server's store
  feed_run(code, external_lookup=browser functions, os=the user's files)
    await goto(url) / read_page() / click(t) / ...        host functions: the run's own browser (montybot.browsing)
    Path('/work/a.csv').read_text()                       OS calls: the user's workspace (montybot.workspaces)
  dump() -> new state id                                  kept in the run's deps, recorded by DBOS with the step
  the session is closed: no worker is held between calls, during a hand-off, or during an approval
```

Without `MONTY_URL`, Monty runs as local subprocesses (`AsyncMonty`), with the same API; the state is then bytes.

The functions are bound to the run when the step starts. Code cannot name another run or user: there is nothing to
pass them. A snippet that fails returns its error, and the variables it set before the failing line are kept.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

from dbos import DBOS
from pydantic_ai import FunctionToolset, RunContext
from pydantic_monty import (
    AsyncMonty,
    AsyncMontySession,
    AsyncMontyWebsocket,
    MontyError,
    MontyRuntimeError,
    MontySyntaxError,
    MontyTypingError,
    ResourceLimits,
)

from montybot.browser.contract import BrowserError, Click, Navigate, Press, Type
from montybot.browser.service import UserBusy
from montybot.browser.state import BLANK_URL
from montybot.browsing import Session, host_of, refused_url, target_of
from montybot.deps import RunDeps
from montybot.observability import timed, timing
from montybot.resources import Resources, current
from montybot.settings import Settings
from montybot.workspaces import DOWNLOADS, VIRTUAL_ROOT

OUTPUT_LIMIT = 20_000

INSTRUCTIONS = f"""\
`run_code` runs Python (a safe subset, no imports beyond `asyncio`, `json`, `re`, `math`, `datetime`, `pathlib`) in a
session that keeps its variables for this whole task. Your browser is a set of async functions inside it; always
`await` them:

- `await goto(url) -> str`: open a page; returns what it shows, with numbered refs like [12] for things to click.
- `await read_page() -> str`: the current page again.
- `await click(target) -> str`: `target` must be a numbered ref from the latest page (`"12"`), not a CSS selector.
- `await type_text(target, text, press_enter=False) -> str`: replace a text box's value.
- `await press_key(key) -> str`: one key. Enter is only allowed for a just-typed searchbox;
  other submissions use approval or hand-off. Space activation is not available from code.

Each returns the page afterwards: use that returned snapshot instead of immediately calling `read_page()` again.
When the next steps are known, do them sequentially in one `run_code` call: open, inspect the returned page, search,
inspect the result, extract and compute. Do not guess refs or act on refs from a page you have left. Stop and return
what you found when the next action needs a decision, user input or approval. Do not batch non-repeatable actions:
an interrupted snippet may run again from the beginning.
Use code to read several pages, pull out what matters and compute the answer, and `print` only the relevant evidence
(including its source URL), not every full page. A browser error raises `RuntimeError` with a message you can act on.
Clicks that cannot be undone (placing an order, paying, sending) are refused from code: use the `commit` tool for those.

The user's files are in `{VIRTUAL_ROOT}`, kept from one task to the next: use `pathlib.Path` or `open` there. What the
browser downloads is saved in `{DOWNLOADS}`, and the page you get back after the click says where."""


class MontyRunner:
    """Opens Monty sessions: on Full Monty when `monty_url` is set, else in local subprocesses."""

    def __init__(self, pool: AsyncMonty | AsyncMontyWebsocket, *, remote: bool, limits: ResourceLimits) -> None:
        self._pool = pool
        self.remote = remote
        self.limits = limits

    def session(self) -> AsyncMontySession:
        if self.remote:
            assert isinstance(self._pool, AsyncMontyWebsocket)
            return self._pool.checkout(limits=self.limits, ephemeral=False)
        return self._pool.checkout(limits=self.limits)


@asynccontextmanager
async def open_monty(settings: Settings) -> AsyncGenerator[MontyRunner]:
    if settings.monty_url:
        async with AsyncMontyWebsocket(settings.monty_url, request_timeout=settings.code_timeout_seconds) as pool:
            yield MontyRunner(pool, remote=True, limits=limits_of(settings))
    else:
        async with AsyncMonty(request_timeout=settings.code_timeout_seconds) as pool:
            yield MontyRunner(pool, remote=False, limits=limits_of(settings))


def limits_of(settings: Settings) -> ResourceLimits:
    """Per session: compute time (not counting browser calls), memory, and how many host calls one snippet makes."""
    return ResourceLimits(
        max_feed_duration_secs=settings.code_compute_seconds, max_memory=256 * 1024 * 1024, max_suspensions=300
    )


IRREVERSIBLE = re.compile(
    r'place[ _-]?order|buy|purchase|pay|checkout|check[ _-]?out|confirm|book|send|submit[ _-]?order|subscribe|delete',
    re.IGNORECASE,
)


def looks_irreversible(target: str, page: str) -> str | None:
    """What the target is, if it looks like something that cannot be undone: by the selector's own words, or by the
    name the latest page gave a ref (`[12] button "Place order"`). A guard, not a classifier: `commit` is the way to do
    these, with the user's approval."""
    target = target.strip().removeprefix('[').removesuffix(']')
    if target.isdigit():
        line = next((ln for ln in page.splitlines() if ln.lstrip(' -').startswith(f'[{target}]')), '')
        return line.strip() if IRREVERSIBLE.search(line) else None
    return target if IRREVERSIBLE.search(target) else None


def browser_functions(session: Session) -> dict[str, Callable[..., Awaitable[str]]]:
    """The run's browser, as async functions for Monty. They take turns, so code that gathers several does not
    interleave actions on one tab. A failure raises `RuntimeError` in the sandbox, with a message safe to show."""
    lock = asyncio.Lock()
    allow_private = session.resources.settings.allow_private_networks
    last_page = ['']
    search_ready = [False]

    async def guarded(use: Callable[[], Awaitable[str]]) -> str:
        async with lock:
            try:
                page = await use()
            except UserBusy:
                raise RuntimeError(
                    'another of your tasks is using the browser; try again when it has finished'
                ) from None
            except BrowserError as error:
                raise RuntimeError(str(error)) from None
            # A click or a redirect can land anywhere a page links to; the agent may not stay on a private address.
            if (refused := await refused_url(session.url, allow_private=allow_private)) is not None:
                await session.act(Navigate(url=BLANK_URL))
                raise RuntimeError(refused.removeprefix('Error: '))
            last_page[0] = page
            return page

    @timed('code.browser.goto')
    async def goto(url: str) -> str:
        async def use() -> str:
            search_ready[0] = False
            refused = await refused_url(str(url), allow_private=allow_private)
            if refused is not None:
                raise RuntimeError(refused.removeprefix('Error: '))
            await session.activity(f'Opening {host_of(str(url))}')
            await session.act(Navigate(url=str(url)))
            return await session.read()

        return await guarded(use)

    @timed('code.browser.read')
    async def read_page() -> str:
        return await guarded(session.read)

    async def current_page() -> str:
        if not last_page[0]:
            last_page[0] = await session.read()
        return last_page[0]

    def line_for_ref(target: str, page: str) -> str:
        ref = target.strip().strip('[]')
        return next((line for line in page.splitlines() if line.lstrip(' -').startswith(f'[{ref}]')), '')

    def refuse_submission() -> None:
        raise RuntimeError('This input can submit an action. Use the `commit` tool for approval or hand_off.')

    @timed('code.browser.click')
    async def click(target: str) -> str:
        async def use() -> str:
            search_ready[0] = False
            ref = str(target).strip().strip('[]')
            if not ref.isdigit():
                raise RuntimeError(
                    'Clicks from code require a numbered ref from read_page, not a CSS selector. '
                    'Use the `commit` tool for an action requiring approval.'
                )
            page = await current_page()
            if not line_for_ref(ref, page):
                raise RuntimeError('That ref is not in the current page. Read the page before choosing again.')
            what = looks_irreversible(ref, page)
            if what is not None:
                raise RuntimeError('That control looks like it cannot be undone. Use the `commit` tool for approval.')
            await session.act(Click(target=target_of(ref)))
            return await session.read()

        return await guarded(use)

    @timed('code.browser.type')
    async def type_text(target: str, text: str, press_enter: bool = False) -> str:
        async def use() -> str:
            page = await current_page()
            line = line_for_ref(str(target), page)
            search_ready[0] = ' searchbox ' in line and not IRREVERSIBLE.search(page)
            if press_enter and not search_ready[0]:
                refuse_submission()  # do not type first and then refuse: typing itself may trigger site handlers
            await session.act(Type(text=str(text), target=target_of(str(target))))
            if press_enter:
                search_ready[0] = False
                await session.act(Press(key='Enter'))
            return await session.read()

        return await guarded(use)

    @timed('code.browser.press')
    async def press_key(key: str) -> str:
        async def use() -> str:
            key_text = str(key)
            activates = key_text == ' ' or any(
                part.strip().lower() in ('enter', 'return', 'numpadenter', 'space') for part in key_text.split('+')
            )
            if activates and not (key_text == 'Enter' and search_ready[0]):
                refuse_submission()
            search_ready[0] = False
            await session.act(Press(key=key_text))
            return await session.read()

        return await guarded(use)

    return {'goto': goto, 'read_page': read_page, 'click': click, 'type_text': type_text, 'press_key': press_key}


class Printed:
    """Collects what the sandbox prints, up to `OUTPUT_LIMIT` characters; the rest is counted, not kept."""

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.size = 0
        self.dropped = 0

    def __call__(self, stream: str, text: str) -> None:
        room = OUTPUT_LIMIT - self.size
        if room > 0:
            self.chunks.append(text[:room])
            self.size += min(len(text), room)
        self.dropped += max(0, len(text) - max(room, 0))


def shown(printed: Printed, result: Any, error: str | None, notes: Sequence[str] = ()) -> str:
    text = ''.join(printed.chunks)
    if printed.dropped:
        text += f'\n[{printed.dropped} more characters of output cut]'
    if error is not None:
        text += f'\nError: {error}'
    elif result is not None:
        text += result if isinstance(result, str) else repr(result)
    text = '\n'.join([*notes, text.strip() or '(no output; print what you want to see)'])
    return text if len(text) <= OUTPUT_LIMIT + 200 else text[: OUTPUT_LIMIT + 200] + '\n[output cut]'


SESSION_LOST = 'The code session was lost, so variables set before this call may be gone; set them again.'


async def run_snippet(
    resources: Resources, run_id: str, user_id: str, state: bytes | None, code: str
) -> tuple[str, bytes | None]:
    """Run `code` in the run's Monty session; returns what to show the model and the session's new state.

    The state only moves forward when the snippet ran to the end or raised an ordinary error, which keeps the variables
    set before the failing line. If the sandbox timed out, crashed or lost its connection, the session is dropped and
    the previous state stays, so the next call starts from before this one.
    """
    with timing('monty.snippet'):
        session = Session(resources, run_id, user_id)
        printed = Printed()
        notes: list[str] = []
        with timing('monty.session'):
            async with resources.monty.session() as monty:
                if state is not None:
                    try:
                        with timing('monty.load'):
                            await monty.load_session(state)
                    except MontyError:
                        notes.append(SESSION_LOST)
                        state = None
                try:
                    with timing('monty.run'):
                        async with asyncio.timeout(resources.settings.code_timeout_seconds):
                            result = await monty.feed_run(
                                code,
                                external_lookup=browser_functions(session),
                                print_callback=printed,
                                os=resources.workspaces.files(user_id),
                                cwd=VIRTUAL_ROOT,
                            )
                except MontySyntaxError as error:
                    return shown(printed, None, error.display('type-msg'), notes), state
                except MontyTypingError as error:
                    return shown(printed, None, error.display(), notes), state
                except MontyRuntimeError as error:
                    if isinstance(error.exception(), TimeoutError):
                        return shown(
                            printed, None, f'{error.display("type-msg")} (the code ran too long)', notes
                        ), state
                    with timing('monty.dump'):
                        new_state = await monty.dump()
                    return shown(printed, None, error.display('type-msg'), notes), new_state
                except (MontyError, TimeoutError) as error:  # crashed, disconnected, or over `code_timeout_seconds`
                    message = (
                        'the code took too long' if isinstance(error, TimeoutError) else 'the code session stopped'
                    )
                    return shown(printed, None, f'{message}. {SESSION_LOST}', notes), state
                with timing('monty.dump'):
                    new_state = await monty.dump()
                return shown(printed, result, None, notes), new_state


code_tools: FunctionToolset[RunDeps] = FunctionToolset(id='code')


@code_tools.tool
@timed('code.run')
async def run_code(ctx: RunContext[RunDeps], code: str) -> str:
    """Run Python in your session, with your browser as async functions inside it (see the instructions). Returns
    what the code printed and the value of its last expression, or the error."""
    deps = ctx.deps
    output, state = await DBOS.run_step_async(
        {'name': 'code.run'}, run_snippet, current(), deps.run_id, deps.user_id, deps.code.state, code
    )
    deps.code.state = state
    return output
