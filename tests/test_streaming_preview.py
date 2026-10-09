"""Streaming previews use real Pydantic AI events, without a model provider or timing sleeps.

The replay test substitutes an in-memory journal at the DBOS bound-operation boundary;
it is not a Postgres/DBOS recovery test.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelResponse,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolCallPartDelta,
    ToolReturnPart,
)
from pydantic_ai.models.function import FunctionModel

from sammy import agent as agent_module
from sammy import streaming

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
def run_id() -> Iterator[str]:
    value = str(uuid4())
    streaming.reset(value)
    yield value
    streaming.discard(value)


def context(run_id: str) -> Any:
    return SimpleNamespace(deps=SimpleNamespace(run_id=run_id))


async def events(*items: AgentStreamEvent) -> AsyncIterator[AgentStreamEvent]:
    for item in items:
        yield item


@pytest.fixture
def deps(run_id: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    # Recall and the connected integrations are instruction providers with a database dependency, not part of
    # streaming.
    async def nothing(ctx: Any) -> str:
        return ''

    monkeypatch.setattr(agent_module, 'recall', nothing)
    monkeypatch.setattr(agent_module, 'connected_integrations', nothing)
    return SimpleNamespace(
        run_id=run_id,
        run=SimpleNamespace(id=run_id, prompt='hello'),
        schedule=None,
        local_time='',
        squirrel_name='',
        # `run_subagents` is offered only where the user's runs share a browser in tabs.
        resources=SimpleNamespace(browser=SimpleNamespace(shares_browser=False)),
    )


async def test_handler_publishes_text_before_event_stream_finishes(run_id: str) -> None:
    before = streaming.snapshot(run_id)
    observations = []

    async def unfinished() -> AsyncIterator[AgentStreamEvent]:
        yield PartStartEvent(index=0, part=TextPart('Hello'))
        # The handler must update before asking the producer for the next event.
        observations.append(streaming.snapshot(run_id))
        yield PartDeltaEvent(index=0, delta=TextPartDelta(' world'))

    await streaming.handler(context(run_id), unfinished())
    partial = observations[0]
    final = streaming.snapshot(run_id)
    assert partial['text'] == 'Hello'
    assert final['text'] == 'Hello world'
    assert final['revision'] > partial['revision']
    if before is not None:
        assert partial['revision'] > before['revision']
    # A snapshot must not be a live reference that mutates when later chunks arrive.
    assert partial['text'] == 'Hello'


async def test_tool_arguments_thoughts_and_results_are_not_previewed(run_id: str) -> None:
    tool = ToolCallPart('run_code', args='{"code":"private-argument"}', tool_call_id='call-1')
    await streaming.handler(
        context(run_id),
        events(
            PartStartEvent(index=0, part=ThinkingPart('private-thought')),
            PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta='private-thought-delta')),
            PartStartEvent(index=1, part=tool),
            PartDeltaEvent(index=1, delta=ToolCallPartDelta(args_delta='private-argument-delta')),
            FunctionToolCallEvent(tool),
            FunctionToolResultEvent(ToolReturnPart('run_code', 'private-tool-result', tool_call_id='call-1')),
            PartStartEvent(index=2, part=TextPart('Public answer')),
        ),
    )
    snapshot = streaming.snapshot(run_id)
    assert snapshot['text'] == 'Public answer'
    encoded = json.dumps(snapshot)
    for excluded in ('private-thought', 'private-argument', 'private-tool-result'):
        assert excluded not in encoded


async def test_reset_replaces_failed_attempt_instead_of_duplicating(run_id: str) -> None:
    await streaming.handler(context(run_id), events(PartStartEvent(index=0, part=TextPart('Attempt'))))
    previous = streaming.snapshot(run_id)
    streaming.reset(run_id)
    await streaming.handler(context(run_id), events(PartStartEvent(index=0, part=TextPart('Attempt succeeded'))))
    current = streaming.snapshot(run_id)
    assert current['text'] == 'Attempt succeeded'
    assert current['revision'] > previous['revision']


async def test_runs_are_isolated_and_discard_removes_preview(run_id: str) -> None:
    other = str(uuid4())
    try:
        streaming.reset(other)
        await streaming.handler(context(run_id), events(PartStartEvent(index=0, part=TextPart('first'))))
        await streaming.handler(context(other), events(PartStartEvent(index=0, part=TextPart('second'))))
        streaming.discard(run_id)
        assert streaming.snapshot(run_id) == {'revision': 0, 'text': '', 'activity': ''}
        assert streaming.snapshot(other)['text'] == 'second'
        streaming.discard(run_id)  # Cleanup is safe more than once.
    finally:
        streaming.discard(other)


async def test_build_agent_streams_partial_text_before_model_completes(run_id: str, deps: Any) -> None:
    partials = []

    async def stream(messages: Any, info: Any) -> AsyncIterator[str]:
        yield 'Hello'
        partials.append(streaming.snapshot(run_id))
        yield ' world'

    agent = agent_module.build_agent(FunctionModel(stream_function=stream))
    result = await agent.run('hello', deps=deps)
    assert partials[0]['text'] == 'Hello'
    assert result.output == 'Hello world'
    assert streaming.snapshot(run_id)['text'] == result.output


async def test_build_agent_preserves_function_only_nonstreaming_model(deps: Any) -> None:
    calls = 0

    def respond(messages: Any, info: Any) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(parts=[TextPart('nonstream answer')])

    agent = agent_module.build_agent(FunctionModel(respond))
    result = await agent.run('hello', deps=deps)
    assert result.output == 'nonstream answer'
    assert calls == 1


async def test_new_model_attempt_replaces_previous_partial(run_id: str) -> None:
    # A retried model step opens a fresh handler iterator even without workflow reset.
    await streaming.handler(context(run_id), events(PartStartEvent(index=0, part=TextPart('stale partial'))))
    before = streaming.snapshot(run_id)
    await streaming.handler(
        context(run_id),
        events(
            PartStartEvent(index=0, part=TextPart('replacement')),
            PartDeltaEvent(index=0, delta=TextPartDelta(' answer')),
        ),
    )
    after = streaming.snapshot(run_id)
    assert after['text'] == 'replacement answer'
    assert after['revision'] > before['revision']


async def test_text_and_stored_parts_are_bounded(run_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(streaming, 'MAX_TEXT', 32)
    await streaming.handler(
        context(run_id),
        events(
            PartStartEvent(index=0, part=TextPart('a' * 20)),
            PartDeltaEvent(index=0, delta=TextPartDelta('b' * 100)),
            PartStartEvent(index=1, part=TextPart('c' * 100)),
        ),
    )
    assert len(streaming.snapshot(run_id)['text']) == 32
    assert sum(map(len, streaming._previews[run_id].parts.values())) <= 32

    # Empty parts cannot exhaust memory while staying under the character bound.
    await streaming.handler(
        context(run_id), events(*(PartStartEvent(index=index, part=TextPart('')) for index in range(200)))
    )
    assert len(streaming._previews[run_id].parts) <= 128


async def test_run_count_and_expiry_are_bounded_without_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(streaming, '_previews', OrderedDict())
    monkeypatch.setattr(streaming, 'MAX_RUNS', 2)
    clock = [100.0]
    monkeypatch.setattr(streaming, 'monotonic', lambda: clock[0])
    for run_id in ('oldest', 'middle', 'newest'):
        streaming.reset(run_id)
    assert streaming.snapshot('oldest')['revision'] == 0
    assert len(streaming._previews) == 2
    assert streaming.snapshot('newest')['revision'] > 0
    clock[0] += streaming.TTL_SECONDS + 1
    assert streaming.snapshot('newest')['revision'] == 0
    assert not streaming._previews


async def test_completed_model_step_replay_does_not_duplicate_preview(
    run_id: str, deps: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic_ai.durable_exec._operation import ModelRequestId
    from pydantic_ai.durable_exec.dbos import DBOSDurability
    from pydantic_ai.durable_exec.dbos._operation_backend import DBOSBoundOperation

    journal: dict[Any, Any] = {}
    calls = 0
    replayed = 0

    async def journal_operation(self: Any, params: Any, *, config: Any = None) -> Any:
        nonlocal replayed
        operation = self.operation
        # Only the model request is journaled here: no DBOS serialization, database,
        # transactions or recovery scheduler is exercised by this in-memory substitute.
        if isinstance(operation.operation_id, ModelRequestId):
            key = operation.operation_id
            if key in journal:
                replayed += 1
                return journal[key]
            result = await operation.handler(params)
            journal[key] = result
            return result
        return await operation.handler(params)

    monkeypatch.setattr(DBOSBoundOperation, '__call__', journal_operation)
    monkeypatch.setattr(DBOSDurability, 'in_durable_context', property(lambda self: True))
    monkeypatch.setattr(DBOSDurability, '_default_run_id', lambda self: run_id)

    async def stream(messages: Any, info: Any) -> AsyncIterator[str]:
        nonlocal calls
        calls += 1
        yield 'durable '
        yield 'answer'

    agent = agent_module.build_agent(FunctionModel(stream_function=stream))
    first = await agent.run('hello', deps=deps)
    assert streaming.snapshot(run_id)['text'] == 'durable answer'
    streaming.reset(run_id)  # As at workflow entry, or after losing process-local state.
    replay = await agent.run('hello', deps=deps)
    assert replay.output == first.output == 'durable answer'
    assert calls == 1
    assert replayed == 1
    assert streaming.snapshot(run_id)['text'] == ''  # Completed steps do not reemit live events.


async def test_final_write_skips_already_finished_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the final-write guard, not real Postgres transaction/locking semantics."""
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from pydantic_ai.messages import ModelMessagesTypeAdapter

    from sammy import store, workflows

    @asynccontextmanager
    async def transaction() -> AsyncIterator[None]:
        yield

    connection = SimpleNamespace(transaction=transaction)

    @asynccontextmanager
    async def connect() -> AsyncIterator[Any]:
        yield connection

    resources: Any = SimpleNamespace(pool=SimpleNamespace(connection=connect))
    run: Any = SimpleNamespace(id='finished-run', thread_id='thread')
    messages: list[ModelMessage] = [ModelResponse(parts=[TextPart('final answer')])]
    encoded = ModelMessagesTypeAdapter.dump_json(messages)
    lock = AsyncMock(side_effect=[False, True])
    append = AsyncMock()
    finish = AsyncMock()
    monkeypatch.setattr(store, 'lock_finished', lock)
    monkeypatch.setattr(store, 'append_history', append)
    monkeypatch.setattr(store, 'finish_run', finish)
    # Main's browser-before-terminal fix is independent of the final-history guard.
    monkeypatch.setattr(workflows, 'close_browser', AsyncMock())

    await workflows.finish_run(resources, run, encoded, 'final answer')
    # The database committed but DBOS did not record the step: execute it again.
    await workflows.finish_run(resources, run, encoded, 'final answer')

    assert lock.await_count == 2
    append.assert_awaited_once_with(connection, run.thread_id, messages)
    finish.assert_awaited_once_with(connection, run.id, 'done', output='final answer')


async def test_part_end_reconciles_text_without_appending_it_twice(run_id: str) -> None:
    await streaming.handler(
        context(run_id),
        events(
            PartStartEvent(index=0, part=TextPart('Hello')),
            PartDeltaEvent(index=0, delta=TextPartDelta(' world')),
            PartEndEvent(index=0, part=TextPart('Hello world')),
        ),
    )
    assert streaming.snapshot(run_id)['text'] == 'Hello world'


async def test_tool_activity_is_blank_and_preserves_current_text(run_id: str) -> None:
    await streaming.handler(context(run_id), events(PartStartEvent(index=0, part=TextPart('Working on it'))))
    before = streaming.snapshot(run_id)
    await streaming.handler(
        context(run_id), events(FunctionToolCallEvent(ToolCallPart('run_code', {'code': 'private-argument'})))
    )
    running = streaming.snapshot(run_id)
    assert running['activity'] == ''  # the web app shows the run's own activity log instead
    assert running['text'] == before['text']
    assert running['revision'] > before['revision']
    await streaming.handler(
        context(run_id), events(FunctionToolResultEvent(ToolReturnPart('run_code', 'private-result')))
    )
    completed = streaming.snapshot(run_id)
    assert completed['activity'] == ''
    assert completed['text'] == before['text']
    assert completed['revision'] > running['revision']


def test_concurrent_producer_and_snapshot_readers(run_id: str) -> None:
    """Separate loops/threads, synchronized by phases rather than elapsed time."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    rounds = 32
    phases = Barrier(3, timeout=10)  # Timeout only prevents a deadlock from hanging the suite.

    async def produce() -> None:
        async def chunks() -> AsyncIterator[AgentStreamEvent]:
            yield PartStartEvent(index=0, part=TextPart('x'))
            for _ in range(rounds):
                phases.wait()
                yield PartDeltaEvent(index=0, delta=TextPartDelta('x'))
                phases.wait()

        await streaming.handler(context(run_id), chunks())

    def read() -> list[streaming.Snapshot]:
        snapshots = []
        for _ in range(rounds):
            phases.wait()
            snapshots.append(streaming.snapshot(run_id))
            phases.wait()
        return snapshots

    with ThreadPoolExecutor(max_workers=3) as executor:
        writer = executor.submit(lambda: asyncio.run(produce()))
        readers = [executor.submit(read) for _ in range(2)]
        writer.result()
        observations = [reader.result() for reader in readers]

    for snapshots in observations:
        assert len(snapshots) == rounds
        for index, snapshot in enumerate(snapshots):
            # A read may precede or follow its phase's one atomic update.
            assert snapshot['text'] in ('x' * (index + 1), 'x' * (index + 2))
            assert snapshot['activity'] == 'Writing response'
        revisions = [snapshot['revision'] for snapshot in snapshots]
        assert revisions == sorted(revisions)
    assert streaming.snapshot(run_id)['text'] == 'x' * (rounds + 1)
