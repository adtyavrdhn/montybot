"""Real DBOS/Postgres recovery with a gated, deterministic streaming model.

The gate synchronizes on observable progress, not elapsed time. The adjacent
counter contains only call markers, never prompts, tool arguments or output.
This module is also imported by the app via MODEL=script:e2e.test_streaming:model.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

if TYPE_CHECKING:
    from conftest import App, Client

PARTIAL = 'Let me check before answering.'
QUESTION = 'Which colour?'
FINAL = 'Your choice is green.'
PROMPT = 'Please ask me a question, then answer.'


@pytest.fixture
def app_env(tmp_path: Path) -> dict[str, str]:
    return {'MODEL': 'script:e2e.test_streaming:model', 'STREAM_GATE': str(tmp_path / 'stream-gate')}


def model() -> FunctionModel:
    gate = Path(os.environ['STREAM_GATE'])
    counter = gate.with_suffix('.calls')

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        with counter.open('a') as calls:
            calls.write('1\n')
        replies = [
            part
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == 'ask_user'
        ]
        if replies:
            assert len(replies) == 1
            assert replies[0].content == 'green'
            yield FINAL
            return
        yield 'Let me check '
        yield 'before answering.'
        while not gate.exists():
            await asyncio.sleep(0.05)
        if any(
            isinstance(message, ModelRequest)
            and any(
                isinstance(part, UserPromptPart) and part.content == 'Fail after preview.' for part in message.parts
            )
            for message in messages
        ):
            raise RuntimeError('private-provider-error-sentinel')
        yield {0: DeltaToolCall(name='ask_user', json_args=json.dumps({'question': QUESTION}), tool_call_id='colour')}

    return FunctionModel(stream_function=stream)


def sse_events(response: httpx.Response) -> Iterator[tuple[str, dict[str, Any]]]:
    """Parse SSE frames without buffering the response or including content in logs."""
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/event-stream')
    event = ''
    data: list[str] = []
    for line in response.iter_lines():
        if not line:
            if data:
                yield event, json.loads('\n'.join(data))
            event, data = '', []
        elif line.startswith('event:'):
            event = line.removeprefix('event:').strip()
        elif line.startswith('data:'):
            data.append(line.removeprefix('data:').lstrip())


def wait_event(
    events: Iterator[tuple[str, dict[str, Any]]],
    kind: str,
    matches: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    for event, value in events:
        if event == kind and matches(value):
            return value
        if time.monotonic() > deadline:
            break
    raise AssertionError(f'did not receive the expected {kind} event')


def test_streaming_preview_and_completed_model_step_survive_recovery(app: App, client: Client, tmp_path: Path) -> None:
    gate = tmp_path / 'stream-gate'
    counter = gate.with_suffix('.calls')
    client.sign_up()
    thread_id = client.ask(PROMPT)
    run_id = client.thread(thread_id)['run']['id']
    path = f'/api/runs/{run_id}/events'

    with client.http.stream('GET', path) as response:
        events = sse_events(response)
        status = wait_event(events, 'status', lambda value: value['status'] == 'running')
        assert status['output'] is None
        partial = wait_event(events, 'preview', lambda value: value['text'] == PARTIAL)
        assert partial['revision'] > 0
        assert not gate.exists()
        assert client.thread(thread_id)['run']['status'] == 'running'
        assert client.thread(thread_id)['messages'] == [{'role': 'user', 'text': PROMPT}]
        assert counter.read_text().splitlines() == ['1']
    # Closing the connection must not cancel the model or require a delivery cursor.
    # A new subscriber gets a full replacement snapshot, not another text delta.
    with client.http.stream('GET', path) as response:
        events = sse_events(response)
        wait_event(events, 'status', lambda value: value['status'] == 'running')
        replacement = wait_event(events, 'preview', lambda value: value['text'] == PARTIAL)
        assert replacement == partial

    with httpx.Client(base_url=app.url) as other:
        assert other.get(path).status_code == 401
        signed_up = other.post('/api/signup', json={'email': 'other@example.test', 'password': 'correct horse'})
        assert signed_up.status_code == 201
        assert other.get(path).status_code == 404

    # An interrupted model step has no completed DBOS result to replay: it
    # restarts once, replacing the provisional text rather than appending it.
    app.kill()
    app.start()
    with client.http.stream('GET', path) as response:
        events = sse_events(response)
        status = wait_event(events, 'status', lambda value: value['status'] == 'running')
        assert status['output'] is None
        retried = wait_event(events, 'preview', lambda value: value['text'] == PARTIAL)
        assert retried['text'] == replacement['text']
        assert not gate.exists()
        assert counter.read_text().splitlines() == ['1', '1']
        assert client.thread(thread_id)['messages'] == [{'role': 'user', 'text': PROMPT}]

    gate.touch()
    question = client.wait_for_ask(thread_id, 'question')
    assert question['prompt'] == QUESTION
    assert counter.read_text().splitlines() == ['1', '1']
    app.kill()
    app.start()
    assert client.wait_for_ask(thread_id, 'question')['id'] == question['id']

    with client.http.stream('GET', path) as response:
        events = sse_events(response)
        waiting = wait_event(events, 'status', lambda value: value['status'] == 'waiting')
        assert waiting['ask']['id'] == question['id']
        assert waiting['output'] is None
        # Previews are process-local. Replaying the completed model step must
        # neither reemit its text nor invoke the FunctionModel again.
        preview = wait_event(events, 'preview', lambda value: True)
        assert preview['text'] == ''
        assert counter.read_text().splitlines() == ['1', '1']
        client.answer(question, text='green')
        terminal = wait_event(events, 'status', lambda value: value['status'] in ('done', 'failed'))
        assert terminal['status'] == 'done'
        assert terminal['output'] == FINAL
        assert terminal['ask'] is None
        assert list(events) == []  # No stale preview after authoritative completion.

    assert client.wait_for_reply(thread_id) == FINAL
    thread = client.thread(thread_id)
    assert thread['run']['status'] == 'done'
    assert thread['messages'] == [
        {'role': 'user', 'text': PROMPT},
        {'role': 'assistant', 'text': FINAL},
    ]  # The initial assistant text + tool call is not a durable chat reply.
    assert counter.read_text().splitlines() == ['1', '1', '1']


def test_streaming_failure_reconciles_preview_and_reconnect(client: Client, tmp_path: Path) -> None:
    from montybot.workflows import FAILURE_NOTICE

    client.sign_up()
    prompt = 'Fail after preview.'
    thread_id = client.ask(prompt)
    run_id = client.thread(thread_id)['run']['id']
    path = f'/api/runs/{run_id}/events'
    with client.http.stream('GET', path) as response:
        events = sse_events(response)
        wait_event(events, 'preview', lambda value: value['text'] == PARTIAL)
        (tmp_path / 'stream-gate').touch()
        terminal = wait_event(events, 'status', lambda value: value['status'] == 'failed')
        assert terminal['output'] == FAILURE_NOTICE
        assert 'private-provider-error-sentinel' not in json.dumps(terminal)
        assert list(events) == []
    with client.http.stream('GET', path) as response:
        replay = list(sse_events(response))
        assert len(replay) == 1
        assert replay[0][0] == 'status'
        assert replay[0][1]['status'] == 'failed'
        assert replay[0][1]['output'] == FAILURE_NOTICE
    assert client.wait_for_reply(thread_id, failed=True) == FAILURE_NOTICE
    assert client.thread(thread_id)['messages'] == [
        {'role': 'user', 'text': prompt},
        {'role': 'assistant', 'text': FAILURE_NOTICE},
    ]
