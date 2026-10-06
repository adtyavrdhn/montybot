"""Recover recorded legacy/intermediate child identities across dispatcher upgrades."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from conftest import App, Client
from dbos import DBOS, DBOSClient, SetWorkflowID
from helpers import eventually
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from montybot import store, workflows
from montybot.db import create_pool


def model() -> FunctionModel:
    # The first process reproduces the old dispatcher, not a fake DBOS journal.
    mode = os.environ.get('UPGRADE_DISPATCH_MODE')
    if mode:

        async def previous_start(run_id: str) -> Any:
            target = workflows.run_thread if mode == 'legacy' else workflows.run_thread_stream
            with SetWorkflowID(run_id):
                return await DBOS.start_workflow_async(target, run_id)

        workflows.start = previous_start
    if gate_path := os.environ.get('UPGRADE_START_GATE'):
        original = workflows.start_run

        async def gated_start(resources: Any, run_id: str) -> Any:
            gate = Path(gate_path)
            gate.with_suffix('.entered').touch()
            while not gate.exists():
                await asyncio.sleep(0.05)
            return await original(resources, run_id)

        workflows.start_run = gated_start

    def respond(messages: Any, info: Any) -> ModelResponse:
        if any(
            isinstance(message, ModelRequest) and any(isinstance(part, ToolReturnPart) for part in message.parts)
            for message in messages
        ):
            return ModelResponse(parts=[TextPart('Upgrade recovered.')])
        return ModelResponse(
            parts=[ToolCallPart('ask_user', {'question': 'Continue upgrade?'}, tool_call_id='upgrade-ask')]
        )

    return FunctionModel(respond)


@pytest.fixture
def dispatch_mode() -> str:
    return 'legacy'


@pytest.fixture
def app_env(dispatch_mode: str, request: pytest.FixtureRequest, tmp_path: Path) -> dict[str, str]:
    env = {'MODEL': 'script:e2e.test_streaming_upgrade:model', 'UPGRADE_DISPATCH_MODE': dispatch_mode}
    if 'queued' in request.node.name:
        env['UPGRADE_START_GATE'] = str(tmp_path / 'start-gate')
    return env


def restart_upgraded(app: App, *, keep_gate: bool = False) -> None:
    app.kill()
    app.env.pop('UPGRADE_DISPATCH_MODE', None)
    if not keep_gate:
        app.env.pop('UPGRADE_START_GATE', None)
    app.start()


def answer_and_check(client: Client, thread_id: str) -> None:
    ask = client.wait_for_ask(thread_id, 'question')
    client.answer(ask, text='Continue')
    assert client.wait_for_reply(thread_id) == 'Upgrade recovered.'
    assert [message['text'] for message in client.thread(thread_id)['messages'] if message['role'] == 'assistant'] == [
        'Upgrade recovered.'
    ]


def test_legacy_queued_identity_survives_startup_reconciliation(app: App, client: Client, tmp_path: Path) -> None:
    client.sign_up()
    thread_id = client.ask('Recover the queued run.')
    eventually(lambda: (tmp_path / 'start-gate.entered').exists(), what='legacy run.start to enter its step')
    assert client.thread(thread_id)['run']['status'] == 'queued'
    # Keep recovery's run.start blocked so startup reconciliation must see the
    # queued legacy row. Otherwise recovery could advance it first and mask the bug.
    restart_upgraded(app, keep_gate=True)
    assert client.thread(thread_id)['run']['status'] == 'queued'
    (tmp_path / 'start-gate').touch()
    answer_and_check(client, thread_id)


@pytest.mark.parametrize('dispatch_mode', ['legacy', 'stream'])
def test_recorded_scheduled_parent_keeps_child_checkpoint(
    app: App, client: Client, database_url: str, dispatch_mode: str
) -> None:
    user_id = client.sign_up()['id']

    async def seed() -> Any:
        pool = create_pool(database_url)
        await pool.open()
        try:
            async with pool.connection() as connection, connection.transaction():
                return await store.create_schedule(
                    connection,
                    schedule_id=str(uuid.uuid4()),
                    user_id=user_id,
                    name='Upgrade test',
                    cron='0 0 1 1 *',
                    timezone='UTC',
                    when='New year',
                    prompt='Recover scheduled child.',
                    watch=False,
                )
        finally:
            await pool.close()

    schedule = asyncio.run(seed())
    dbos = DBOSClient(system_database_url=database_url)
    try:
        name = f'montybot-schedule-{schedule.id}'
        dbos.create_schedule(
            schedule_name=name,
            workflow_name='montybot.run_schedule',
            schedule=schedule.cron,
            context={'schedule_id': schedule.id},
            cron_timezone='UTC',
        )
        parent = dbos.trigger_schedule(name)
        client.wait_for_ask(schedule.thread_id, 'question')
        # Both names have a genuinely recorded parent child-start operation here.
        restart_upgraded(app)
        answer_and_check(client, schedule.thread_id)
        status = eventually(
            lambda: result if (result := parent.get_status().status) not in ('ENQUEUED', 'PENDING') else None,
            what=f'{dispatch_mode} scheduled parent to finish after replay',
        )
        assert status == 'SUCCESS'
        # Content-free log check: operation-name conflicts must never occur.
        log = app.log.read_text()
        assert not any(name in log for name in ('DBOSUnexpectedStepError', 'DBOSConflictingWorkflowError'))
    finally:
        dbos.destroy()
