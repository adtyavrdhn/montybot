"""The Sammy side: an agent drives a headless browser and hands it to the user when stuck.

Run: uv run python -m sammy_poc.remote [--engine servo]
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from sammy_poc import demo_site, servo
from sammy_poc.browser import export_state, open_page, seeded_context
from sammy_poc.state import BrowserState
from sammy_poc.wire import DEFAULT_PORT, LINE_LIMIT, recv, send

SHOPPING_LIST = ['eggs', 'milk', 'bread']


class HandoffServer:
    """Accepts the user's machine on a socket and hands it the browser when the agent is stuck."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._clients: asyncio.Queue[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = asyncio.Queue()

    async def on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            hello = await recv(reader)
        except (ConnectionError, ValueError):
            writer.close()
            return
        if hello.get('type') != 'hello' or not secrets.compare_digest(str(hello.get('token', '')), self._token):
            print('[remote] rejected a connection with a bad token')
            writer.close()
            return
        print("[remote] user's machine connected")
        await self._clients.put((reader, writer))

    async def hand_off(self, reason: str, state: BrowserState) -> tuple[BrowserState, str]:
        """Send `state` to the user's machine and wait for the state it sends back."""
        if self._clients.empty():
            print("[remote] waiting for the user's machine to connect...")
        while True:
            reader, writer = await self._clients.get()
            try:
                await send(writer, {'type': 'handoff', 'reason': reason, 'state': state.to_json()})
                reply = await recv(reader)
            except ConnectionError:
                print("[remote] user's machine disconnected; waiting for it to reconnect...")
                continue
            await self._clients.put((reader, writer))
            return BrowserState.from_json(reply['state']), str(reply.get('note', ''))

    def close(self) -> None:
        """Disconnect the user's machine; the server cannot finish closing while it is connected."""
        while not self._clients.empty():
            _, writer = self._clients.get_nowait()
            writer.close()


class AgentBrowser(Protocol):
    """The headless browser the agent drives. Only one side holds the session at a time."""

    async def open(self, state: BrowserState | None) -> None: ...

    async def release(self) -> BrowserState:
        """Export the session and close this side's copy, so only the user's window can change it."""
        ...

    async def goto(self, url: str) -> None: ...

    async def click(self, selector: str) -> None: ...

    async def describe(self) -> str: ...

    async def close(self) -> None: ...


@dataclass
class ChromiumBrowser:
    """Headless Chromium through Playwright."""

    browser: Browser
    context: BrowserContext | None = None
    _page: Page | None = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError('the browser is handed off to the user')
        return self._page

    async def open(self, state: BrowserState | None) -> None:
        self.context = await seeded_context(self.browser, state)
        self._page = await open_page(self.context, state)

    async def release(self) -> BrowserState:
        assert self.context is not None
        state = await export_state(self.context, self._page)
        await self.close()
        return state

    async def goto(self, url: str) -> None:
        await self.page.goto(url)

    async def click(self, selector: str) -> None:
        await self.page.click(selector, timeout=5000)

    async def describe(self) -> str:
        text = await self.page.inner_text('body')
        return f'URL: {self.page.url}\n\n{text[:4000]}'

    async def close(self) -> None:
        if self.context is not None:
            await self.context.close()
        self.context = self._page = None


@dataclass
class Run:
    browser: AgentBrowser
    handoff: HandoffServer


async def navigate(ctx: RunContext[Run], url: str) -> str:
    """Open a URL and return the page's text."""
    print(f'[agent] navigate({url!r})')
    await ctx.deps.browser.goto(url)
    return await ctx.deps.browser.describe()


async def click(ctx: RunContext[Run], selector: str) -> str:
    """Click the element matching a CSS selector, such as `#add-eggs`, and return the page's text."""
    print(f'[agent] click({selector!r})')
    await ctx.deps.browser.click(selector)
    return await ctx.deps.browser.describe()


async def read_page(ctx: RunContext[Run]) -> str:
    """Return the current page's URL and text."""
    print('[agent] read_page()')
    return await ctx.deps.browser.describe()


async def ask_user(ctx: RunContext[Run], reason: str) -> str:
    """Hand the browser to the user for a step you cannot do (a password, 2FA, a CAPTCHA, a payment).

    Waits until the user hands it back, then returns the page they left it on.
    """
    print(f'[agent] ask_user({reason!r})')
    state = await ctx.deps.browser.release()
    print(f'[remote]   sent:     {state.summary()}')
    returned, note = await ctx.deps.handoff.hand_off(reason, state)
    print(f'[remote]   received: {returned.summary()}')
    await ctx.deps.browser.open(returned)
    return f'The user handed the browser back ({note}).\n\n{await ctx.deps.browser.describe()}'


def scripted_shopper(shop: str) -> FunctionModel:
    """A deterministic stand-in for an LLM, so the proof of concept runs without an API key."""

    def next_step(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [
            part
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]

        def call(tool: str, **args: str) -> ModelResponse:
            return ModelResponse(parts=[ToolCallPart(tool_name=tool, args=args)])

        if not returns:
            return call('navigate', url=f'{shop}/shop')
        last = returns[-1]
        page = str(last.content)
        if 'Sign in to continue' in page:
            if last.tool_name == 'ask_user':
                return ModelResponse(parts=[TextPart('The user did not sign in, so I stopped.')])
            return call(
                'ask_user', reason='The shop wants a password I do not have. Please sign in, then click Return.'
            )
        added = sum(r.tool_name == 'click' for r in returns)
        if added < len(SHOPPING_LIST):
            return call('click', selector=f'#add-{SHOPPING_LIST[added]}')
        if last.tool_name == 'click':
            return call('navigate', url=f'{shop}/cart')
        return ModelResponse(parts=[TextPart(f'Done. The cart page says:\n{page}')])

    return FunctionModel(next_step)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--port', type=int, default=DEFAULT_PORT, help='hand-off socket port')
    parser.add_argument('--site-port', type=int, default=8765, help='demo shop port')
    parser.add_argument('--token', help='hand-off token (default: random)')
    parser.add_argument('--model', help='a real model, such as anthropic:claude-sonnet-4-5 (default: scripted)')
    parser.add_argument('--engine', choices=['chromium', 'servo'], default='chromium', help='the remote browser')
    parser.add_argument('--servo-binary', type=Path, default=servo.DEFAULT_BINARY, help='path to servoshell')
    args = parser.parse_args()

    shop = demo_site.start(args.site_port)
    token = args.token or secrets.token_urlsafe(12)
    handoff = HandoffServer(token)
    server = await asyncio.start_server(handoff.on_client, '127.0.0.1', args.port, limit=LINE_LIMIT)
    print(f'[remote] demo shop: {shop} (password: {demo_site.PASSWORD})')
    print(f'[remote] hand-off socket: 127.0.0.1:{args.port}')
    print(f'[remote] on the user machine: uv run python -m sammy_poc.local --port {args.port} --token {token}')

    agent = Agent(
        args.model or scripted_shopper(shop),
        deps_type=Run,
        tools=[navigate, click, read_page, ask_user],
        instructions=(
            f'You shop for the user at {shop}/shop in a headless browser. Add items with their buttons, '
            'for example #add-eggs. If the site needs a password, 2FA, a CAPTCHA or a payment, call ask_user; '
            f'never guess credentials. When you are done, open {shop}/cart and report what is in it.'
        ),
    )
    async with server, AsyncExitStack() as stack:
        remote: AgentBrowser
        if args.engine == 'servo':
            remote = servo.ServoBrowser(args.servo_binary)
        else:
            playwright = await stack.enter_async_context(async_playwright())
            remote = ChromiumBrowser(await playwright.chromium.launch(headless=True))
        await remote.open(None)
        try:
            result = await agent.run(
                f'Put my shopping list in the cart: {", ".join(SHOPPING_LIST)}.', deps=Run(remote, handoff)
            )
            print(f'[remote] agent finished:\n{result.output}')
        finally:
            handoff.close()
            await remote.close()


if __name__ == '__main__':
    asyncio.run(main())
