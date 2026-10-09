"""Long chats (#126): the oldest messages go into a summary instead of being dropped, so a run gets the summary and
the latest messages, and any message can still be read word for word. Against Postgres, with scripted models."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx2
import pytest
from anthropic import AsyncAnthropic
from pydantic_ai import Agent
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextContent,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from sammy import history, store
from sammy.agent import CACHE
from sammy.db import Pool, create_pool, migrate
from sammy.settings import Settings

pytestmark = pytest.mark.anyio

PARTY = 'We agreed the party is at the boathouse on Saturday at 7.'
PREFIX = 'Summary of previous conversation:\n\n'  # how SummarizingCompaction starts a summary
MODEL = 'claude-opus-5-5'


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
async def pool(database_url: str) -> AsyncIterator[Pool]:
    await migrate(database_url)
    pool = create_pool(database_url)
    await pool.open()
    yield pool
    await pool.close()


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(database_url=database_url, session_secret='secret', encryption_key='key')  # pyright: ignore[reportArgumentType]


class Summarizer:
    """A scripted summary model: keeps each prompt it got and answers with `reply`."""

    def __init__(self, reply: str = 'The user keeps notes about apples.') -> None:
        self.prompts: list[str] = []
        self.reply = reply
        self.model = FunctionModel(self.respond, model_name='summarizer')

    def respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        self.prompts.extend(str(p.content) for p in request.parts if isinstance(p, UserPromptPart))
        return ModelResponse(parts=[TextPart(self.reply)])


def turn(number: int, position: int) -> list[ModelMessage]:
    """The user's message and Sammy's reply; in turns 1, 4, 7... Sammy uses a tool between them. The user's message
    at position 10 (turn 4) is `PARTY`."""
    asked = ModelRequest(parts=[UserPromptPart(PARTY if position == 10 else f'Note {number}: buy {number} apples.')])
    reply = ModelResponse(parts=[TextPart(f'Noted: {number} apples.')])
    if number % 3 != 1:
        return [asked, reply]
    call = ToolCallPart(tool_name='run_code', args={'code': 'print(1)'}, tool_call_id=f'call-{number}')
    returned = ToolReturnPart(tool_name='run_code', content='1', tool_call_id=f'call-{number}')
    return [asked, ModelResponse(parts=[call]), ModelRequest(parts=[returned]), reply]


async def new_thread(pool: Pool) -> str:
    async with pool.connection() as c:
        user = await store.create_user(c, 'a@example.test', 'x')
        assert user is not None
        return (await store.create_thread(c, user.id, 'A long chat')).id


async def chat(pool: Pool, thread_id: str, messages: int) -> None:
    """Add turns to the thread until it has at least `messages` messages."""
    written = 0
    while written < messages:
        added = turn(written, written)
        async with pool.connection() as c:
            await store.append_history(c, thread_id, added)
        written += len(added)


async def test_a_long_chat_keeps_a_stable_prompt(pool: Pool, settings: Settings) -> None:
    """As the chat grows to 300 messages, each run gets the summary and between a window and a window plus a batch
    of the latest messages, starting at a user's message; every message stays stored."""
    thread_id = await new_thread(pool)
    summarizer = Summarizer()
    window, most = settings.history_window, history.limit(settings)
    written = number = 0
    while written < 300:
        added = turn(number, written)
        async with pool.connection() as c:
            await store.append_history(c, thread_id, added)
        written, number = written + len(added), number + 1
        while await history.compact_once(pool, settings, thread_id, summarizer.model):  # as the workflow does
            pass
        async with pool.connection() as c:
            seen = await history.for_run(c, thread_id, settings)
            summary, compacted_up_to = await history.summary_of(c, thread_id)
        assert len(seen) <= most
        first = seen[0]
        assert isinstance(first, ModelRequest) and any(isinstance(p, UserPromptPart) for p in first.parts)
        if compacted_up_to:
            assert window <= len(seen) and compacted_up_to + len(seen) == written  # nothing between summary and window
            opening = first.parts[0]
            assert isinstance(opening, SystemPromptPart)
            assert opening.content == f'{summary}\n\n{history.SUMMARY_NOTE.format(last=compacted_up_to - 1)}'
            assert len(summary) <= settings.history_summary_chars

    assert compacted_up_to >= 300 - most
    assert summary == PREFIX + summarizer.reply
    # A batch at a time, each summary updating the one before rather than starting again.
    assert len(summarizer.prompts) == pytest.approx((300 - most) / settings.history_batch, abs=1)
    assert all('<previous-summary>' in prompt for prompt in summarizer.prompts[1:])
    assert PARTY in summarizer.prompts[0]
    async with pool.connection() as c:
        assert len(await store.load_history(c, thread_id)) == written


async def test_message_10_can_be_found_and_read_word_for_word(pool: Pool, settings: Settings) -> None:
    thread_id = await new_thread(pool)
    await chat(pool, thread_id, 300)
    while await history.compact_once(pool, settings, thread_id, Summarizer().model):
        pass
    async with pool.connection() as c:
        _, compacted_up_to = await history.summary_of(c, thread_id)
        assert compacted_up_to > 10  # not in what a run sees
        found = await history.search_messages(c, thread_id, 'where is the party venue')
        read = await history.load_messages(c, thread_id, 10, 10)
    assert [history.render(position, message) for position, message in found] == [f'#10 user: {PARTY}']
    assert [history.render(position, message) for position, message in read] == [f'#10 user: {PARTY}']


async def test_summaries_never_see_file_bytes_and_are_capped() -> None:
    summarizer = Summarizer(reply='x' * 500)
    note = TextContent('[The user attached "cat.png" (image/png, 1 KB).]', metadata={'attachment': 'a1'})
    image = BinaryContent(b'\x89PNG secret pixels', media_type='image/png', identifier='attachment:a1')
    shown = [
        ModelRequest(parts=[UserPromptPart(['What is this?', note, image])]),
        ModelResponse(parts=[TextPart('A cat.')]),
    ]

    first = await history.summarize(summarizer.model, '', shown, max_chars=100)
    assert 'secret pixels' not in summarizer.prompts[0] and 'cat.png' in summarizer.prompts[0]
    assert len(first) == 100 and first.startswith(PREFIX) and first.endswith(history.CUT)

    await history.summarize(summarizer.model, first, shown, max_chars=100)
    assert f'<previous-summary>\n{first.removeprefix(PREFIX)}\n</previous-summary>' in summarizer.prompts[1]


async def test_the_summary_is_a_cached_prefix_of_every_model_call_in_a_run() -> None:
    """With Sammy's cache settings, Anthropic gets the summary in the cached system prompt, the same in each of a
    run's model calls, so the calls after the first read it from the cache."""
    bodies: list[dict[str, object]] = []
    replies = iter(
        [
            ({'type': 'tool_use', 'id': 'call-1', 'name': 'look', 'input': {}}, 'input_json_delta', 'tool_use'),
            ({'type': 'text', 'text': ''}, 'text_delta', 'end_turn'),
        ]
    )

    def answer(request: httpx2.Request) -> httpx2.Response:
        """Anthropic's streamed reply: one tool call, then one text."""
        bodies.append(json.loads(request.content))
        block, delta, stop_reason = next(replies)
        usage = {'input_tokens': 1, 'output_tokens': 1}
        message = {'id': 'msg', 'type': 'message', 'role': 'assistant', 'model': MODEL, 'content': []}
        events = [
            {'type': 'message_start', 'message': {**message, 'stop_reason': None, 'usage': usage}},
            {'type': 'content_block_start', 'index': 0, 'content_block': block},
            {
                'type': 'content_block_delta',
                'index': 0,
                'delta': {'type': delta, 'partial_json': '{}'}
                if delta == 'input_json_delta'
                else {'type': delta, 'text': 'At the boathouse.'},
            },
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'message_delta', 'delta': {'stop_reason': stop_reason}, 'usage': usage},
            {'type': 'message_stop'},
        ]
        stream = ''.join(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n' for event in events)
        return httpx2.Response(200, text=stream, headers={'content-type': 'text/event-stream'})

    client = AsyncAnthropic(api_key='test', http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(answer)))
    model = AnthropicModel(MODEL, provider=AnthropicProvider(anthropic_client=client))
    agent = Agent(model, instructions=['Static instructions.', lambda: 'Instructions made for this run.'])

    @agent.tool_plain
    def look() -> str:
        return 'Nothing to see.'

    latest: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart('Note 99: buy apples.')]),
        ModelResponse(parts=[TextPart('Noted.')]),
    ]
    summary = f'{PREFIX}The party is at the boathouse.'
    await agent.run(
        'Where is the party?', message_history=history.with_summary(latest, summary, 100), model_settings=CACHE
    )

    first, second = bodies
    system = first['system']
    assert isinstance(system, list) and system == second['system']
    # The summary opens the system prompt, and a cache breakpoint is on it or after it, so it is in the cached prefix.
    assert 'boathouse' in str(system[0]) and any('cache_control' in block for block in system)
    sent_first, sent_second = messages_of(first), messages_of(second)
    assert 'boathouse' not in str(sent_first)
    assert sent_second[: len(sent_first)] == sent_first  # the conversation only grows


def messages_of(body: dict[str, object]) -> list[object]:
    messages = uncached(body['messages'])
    assert isinstance(messages, list)
    return messages


def uncached(value: object) -> object:
    """`value` without its cache breakpoints, which move to the latest message on each call."""
    if isinstance(value, dict):
        return {key: uncached(item) for key, item in value.items() if key != 'cache_control'}
    if isinstance(value, list):
        return [uncached(item) for item in value]
    return value
