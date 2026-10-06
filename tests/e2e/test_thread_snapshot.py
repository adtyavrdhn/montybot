"""Force a final commit between the thread endpoint's history/status reads."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from starlette.requests import Request

from montybot import api, store
from montybot.db import Connection, create_pool


@pytest.mark.asyncio
async def test_thread_final_history_and_status_share_snapshot(
    app: Any, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The app fixture applies migrations; no browser/model work is needed here.
    assert app.process is not None
    pool = create_pool(database_url)
    await pool.open()
    try:
        async with pool.connection() as connection, connection.transaction():
            user = await store.create_user(connection, f'{uuid.uuid4()}@example.com', 'unused-test-hash')
            assert user is not None
            thread = await store.create_thread(connection, user.id, 'Snapshot race')
            run = await store.create_run(
                connection,
                run_id=str(uuid.uuid4()),
                user_id=user.id,
                thread_id=thread.id,
                prompt='Snapshot prompt.',
                trigger='message',
            )
            await store.set_run_status(connection, run.id, 'running')

        committed = False
        load_history = store.load_history

        async def finish_between_reads(connection: Connection, thread_id: str) -> list[ModelMessage]:
            nonlocal committed
            history = await load_history(connection, thread_id)
            if not committed:
                async with pool.connection() as writer, writer.transaction():
                    await store.append_history(
                        writer,
                        thread.id,
                        [
                            ModelRequest(parts=[UserPromptPart(content=run.prompt)]),
                            ModelResponse(parts=[TextPart(content='Authoritative final.')]),
                        ],
                    )
                    await store.finish_run(writer, run.id, 'done', output='Authoritative final.')
                committed = True
            return history

        monkeypatch.setattr(store, 'load_history', finish_between_reads)
        request = Request(
            {
                'type': 'http',
                'method': 'GET',
                'path': f'/api/threads/{thread.id}',
                'headers': [],
                'path_params': {'thread_id': thread.id},
                'session': {'user_id': user.id},
            }
        )
        request.state.resources = type('TestResources', (), {'pool': pool})()
        first = json.loads(bytes((await api.read_thread(request)).body))
        assert first['run']['status'] == 'running'
        assert first['messages'] == [{'role': 'user', 'text': run.prompt}]
        second = json.loads(bytes((await api.read_thread(request)).body))
        assert second['run']['status'] == 'done'
        assert second['messages'] == [
            {'role': 'user', 'text': run.prompt},
            {'role': 'assistant', 'text': 'Authoritative final.'},
        ]
    finally:
        await pool.close()
