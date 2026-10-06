"""`run_code`: the agent's Python runs in Monty, and the browser is a set of host functions inside it.

```
agent: run_code(code)                                     one DBOS step (montybot.code.run_code)
  checkout a Monty session                                Full Monty: monty-server -> monty-worker (MONTY_URL)
  load_session(the run's last state)                      the state is an id in monty-server's store
  feed_run(code, external_lookup=browser functions)
    await goto(url) / read_page() / click(t) / ...        host functions: the run's own browser (montybot.browsing)
  dump() -> new state id                                  kept in the run's deps, recorded by DBOS with the step
  the session is closed: no worker is held between calls, during a hand-off, or during an approval
```

Without `MONTY_URL`, Monty runs as local subprocesses (`AsyncMonty`), with the same API; the state is then bytes.

The functions are bound to the run when the step starts. Code cannot name another run or user: there is nothing to
pass them. A snippet that fails returns its error, and the variables it set before the failing line are kept.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

from dbos import DBOS
from pydantic_ai import FunctionToolset, RunContext
from pydantic_monty import (
    AsyncMonty,
    AsyncMontySession,
    AsyncMontyWebsocket,
    CollectStreams,
    MontyError,
    ResourceLimits,
)

from montybot.browser.contract import BrowserError, Click, Navigate, Press, Type
from montybot.browsing import Session, host_of, refused_url, target_of
from montybot.deps import RunDeps
from montybot.resources import Resources, current
from montybot.settings import Settings

OUTPUT_LIMIT = 20_000
LIMITS = ResourceLimits(max_feed_duration_secs=60, max_memory=256 * 1024 * 1024)

INSTRUCTIONS = """\
`run_code` runs Python (a safe subset, no imports beyond `asyncio`, `json`, `re`, `math`, `datetime`) in a session
that keeps its variables for this whole task. Your browser is a set of async functions inside it; always `await` them:

- `await goto(url) -> str`: open a page; returns what it shows, with numbered refs like [12] for things to click.
- `await read_page() -> str`: the current page again.
- `await click(target) -> str`: `target` is a ref from the latest page (`"12"`) or a CSS selector (`"#add-eggs"`).
- `await type_text(target, text, press_enter=False) -> str`: replace a text box's value.
- `await press_key(key) -> str`: one key, such as `"Enter"` or `"Escape"`.

Each returns the page afterwards. Use code to read several pages, pull out what matters and compute the answer, and
`print` only what you need to see. A browser error raises `BrowserError` with a message you can act on."""


class MontyRunner:
    """Opens Monty sessions: on Full Monty when `monty_url` is set, else in local subprocesses."""

    def __init__(self, pool: AsyncMonty | AsyncMontyWebsocket, *, remote: bool) -> None:
        self._pool = pool
        self.remote = remote

    def session(self) -> AsyncMontySession:
        if self.remote:
            assert isinstance(self._pool, AsyncMontyWebsocket)
            return self._pool.checkout(limits=LIMITS, ephemeral=False)
        return self._pool.checkout(limits=LIMITS)


@asynccontextmanager
async def open_monty(settings: Settings) -> AsyncGenerator[MontyRunner]:
    if settings.monty_url:
        async with AsyncMontyWebsocket(settings.monty_url, request_timeout=settings.code_timeout_seconds) as pool:
            yield MontyRunner(pool, remote=True)
    else:
        async with AsyncMonty(request_timeout=settings.code_timeout_seconds) as pool:
            yield MontyRunner(pool, remote=False)


class BrowserFailed(Exception):
    """Raised into the sandbox for a browser error. Its message is safe to show the model."""


def browser_functions(session: Session) -> dict[str, Callable[..., Awaitable[str]]]:
    """The run's browser, as async functions for Monty."""

    async def guarded(use: Callable[[], Awaitable[str]]) -> str:
        try:
            return await use()
        except BrowserError as error:
            raise BrowserFailed(str(error)) from None

    async def goto(url: str) -> str:
        async def use() -> str:
            refused = await refused_url(url, allow_private=session.resources.settings.allow_private_networks)
            if refused is not None:
                raise BrowserFailed(refused.removeprefix('Error: '))
            await session.activity(f'Opening {host_of(url)}')
            await session.act(Navigate(url=url))
            return await session.read()

        return await guarded(use)

    async def read_page() -> str:
        return await guarded(session.read)

    async def click(target: str) -> str:
        async def use() -> str:
            await session.act(Click(target=target_of(str(target))))
            return await session.read()

        return await guarded(use)

    async def type_text(target: str, text: str, press_enter: bool = False) -> str:
        async def use() -> str:
            await session.act(Type(text=str(text), target=target_of(str(target))))
            if press_enter:
                await session.act(Press(key='Enter'))
            return await session.read()

        return await guarded(use)

    async def press_key(key: str) -> str:
        async def use() -> str:
            await session.act(Press(key=str(key)))
            return await session.read()

        return await guarded(use)

    return {'goto': goto, 'read_page': read_page, 'click': click, 'type_text': type_text, 'press_key': press_key}


def shown(prints: Sequence[tuple[str, str]], result: Any, error: str | None) -> str:
    text = ''.join(chunk for _, chunk in prints)
    if error is not None:
        text += f'\nError: {error}'
    elif result is not None:
        text += repr(result) if not isinstance(result, str) else result
    text = text.strip() or '(no output; print what you want to see)'
    return text if len(text) <= OUTPUT_LIMIT else text[:OUTPUT_LIMIT] + '\n[output cut]'


async def run_snippet(
    resources: Resources, run_id: str, user_id: str, state: bytes | None, code: str
) -> tuple[str, bytes | None]:
    """Run `code` in the run's Monty session; returns what to show the model and the session's new state."""
    session = Session(resources, run_id, user_id)
    output = CollectStreams()
    async with resources.monty.session() as monty:
        if state is not None:
            await monty.load_session(state)
        error: str | None = None
        result: Any = None
        try:
            result = await monty.feed_run(code, external_lookup=browser_functions(session), print_callback=output)
        except MontyError as raised:
            error = str(raised)
        new_state = await monty.dump()
    return shown(output.output, result, error), new_state


code_tools: FunctionToolset[RunDeps] = FunctionToolset(id='code')


@code_tools.tool
async def run_code(ctx: RunContext[RunDeps], code: str) -> str:
    """Run Python in your session, with your browser as async functions inside it (see the instructions). Returns
    what the code printed and the value of its last expression, or the error."""
    deps = ctx.deps
    output, state = await DBOS.run_step_async(
        {'name': 'code.run'}, run_snippet, current(), deps.run_id, deps.user_id, deps.code.state, code
    )
    deps.code.state = state
    return output
