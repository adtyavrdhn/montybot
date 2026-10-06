"""The scripted model for end-to-end tests, as in `poc/montybot_poc/remote.py`'s `scripted_shopper`.

The model reads the user's message and picks a script for it, as a real model picks a plan. A script looks only at
what its tools returned in this run, so it behaves the same whether a step ran or was replayed after a restart. The
same messages run against a real model in the nightly tests.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel


@dataclass
class Turn:
    prompt: str
    returns: list[ToolReturnPart]

    @property
    def last(self) -> str:
        return str(self.returns[-1].content) if self.returns else ''

    def called(self, tool: str) -> int:
        return sum(r.tool_name == tool for r in self.returns)

    def result_of(self, tool: str) -> str:
        return next((str(r.content) for r in reversed(self.returns) if r.tool_name == tool), '')

    @property
    def url(self) -> str:
        match = re.search(r'https?://\S+?(?=[\s,.]*(?:\s|$))', self.prompt)
        assert match, f'the message names no site: {self.prompt!r}'
        return match.group(0).rstrip('/')


Script = Callable[[Turn], ModelResponse]


def call(tool: str, **args: object) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name=tool, args=args)])


def say(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(text)])


def line_with(page: str, needle: str) -> str:
    return next((line for line in page.splitlines() if needle in line), '')


# --- scripts, by the message they answer ---


def hello(turn: Turn) -> ModelResponse:
    return say('Hello! I am monty-bot.')


def favourite_colour(turn: Turn) -> ModelResponse:
    if not turn.called('ask_user'):
        return call('ask_user', question='What is your favourite colour?')
    colour = turn.result_of('ask_user')
    if not turn.called('remember'):
        return call('remember', fact=f'Favourite colour: {colour}')
    return say(f'Got it: your favourite colour is {colour}.')


def order_eggs(turn: Turn) -> ModelResponse:
    if not turn.returns:
        return call('open_page', url=f'{turn.url}/')
    if turn.last.find('Title: Sign in') != -1 and not turn.called('hand_off'):
        return call('hand_off', reason='Please sign in to the shop, then hand the browser back.')
    if 'In cart: eggs' not in turn.last and not turn.called('commit'):
        return call('click', target='#add-eggs')
    if not turn.called('commit'):
        return call('commit', target='#place-order', description='Place the order for eggs ($3.20)')
    result = turn.result_of('commit')
    order = re.search(r'Order #\d+: [a-z, ]+, \$\d+\.\d\d', result)
    if order is None:
        return say(f'I did not place the order. {result}')
    return say(f'Done. {order.group(0)}')


FLIGHT = re.compile(r'\b([A-Z0-9]{2} \d{3,4})\b\W+(\d\d:\d\d)\W+€\s?(\d+)')


def cheapest_flights(turn: Turn) -> ModelResponse:
    if not turn.returns:
        return call('open_page', url=f'{turn.url}/results?to=Lisbon&date=next+Friday')
    if 'Next page' in turn.last:
        return call('click', target='#next')
    flights = {m.group(1): (m.group(2), int(m.group(3))) for r in turn.returns for m in FLIGHT.finditer(str(r.content))}
    best = sorted(flights.items(), key=lambda f: f[1][1])[:3]
    rows = '\n'.join(f'| {flight} | {departs} | €{price} |' for flight, (departs, price) in best)
    return say(
        f'The three cheapest flights to Lisbon next Friday:\n\n| Flight | Departs | Price |\n|---|---|---|\n{rows}'
    )


def todays_offer(turn: Turn) -> ModelResponse:
    if not turn.returns:
        return call('open_page', url=f'{turn.url}/')
    if 'Press and hold' in turn.last and not turn.called('hand_off'):
        return call(
            'hand_off', reason='The store wants a press-and-hold check. Please hold the button, then hand back.'
        )
    offer = line_with(turn.last, 'Today only')
    return say(offer.strip(' -') if offer else f'I could not get past the check. {turn.last}')


def fail(turn: Turn) -> ModelResponse:
    raise RuntimeError('the model provider is down')


SCRIPTS: dict[str, Script] = {
    'Fail please': fail,
    'Say hello': hello,
    'Ask me my favourite colour': favourite_colour,
    'Order eggs from': order_eggs,
    'Find the three cheapest flights to Lisbon next Friday': cheapest_flights,
    'What is on offer today at': todays_offer,
}


def current_turn(messages: list[ModelMessage]) -> Turn:
    """The user's latest message and the tool results since."""
    start = max(
        i
        for i, m in enumerate(messages)
        if isinstance(m, ModelRequest) and any(isinstance(p, UserPromptPart) for p in m.parts)
    )
    prompt = next(
        str(p.content)
        for p in messages[start].parts
        if isinstance(p, UserPromptPart)  # pyright: ignore[reportAttributeAccessIssue]
    )
    returns = [
        part
        for message in messages[start:]
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    return Turn(prompt=prompt, returns=returns)


def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    turn = current_turn(messages)
    for start, script in SCRIPTS.items():
        if turn.prompt.startswith(start):
            return script(turn)
    return say(f'I have no script for {turn.prompt!r}.')


model = FunctionModel(respond, model_name='scripted')
