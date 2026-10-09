"""Offline picker policy, real Postgres persistence, and scripted DBOS run selection."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from dbos import DBOS
from pydantic import JsonValue, SecretStr, ValidationError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from starlette.types import Receive, Scope, Send

from sammy import model_preferences_store as preferences_store
from sammy import store, workflows
from sammy.app import create_app
from sammy.crypto import new_key
from sammy.model_preferences import (
    PRIVATE_FIELDS,
    Preference,
    RunModel,
    defaults,
    envelope,
    resolve,
    thinking_levels,
    validate,
)
from sammy.models import Run, Trigger
from sammy.resources import Resources, open_resources
from sammy.settings import Settings

GPT = 'openai:gpt-5.2'
CLAUDE = 'anthropic:claude-sonnet-4-6'
PLAIN = 'openai:gpt-4.1'
FIRST = 'script:test_model_preferences:first_model'
SECOND = 'script:test_model_preferences:second_model'
PATH = '/api/model-preferences'
CALLS: list[tuple[str, dict[str, object]]] = []


def first_reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    CALLS.append(('first', dict(info.model_settings or {})))
    return ModelResponse(parts=[TextPart('first answer')])


def second_reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    CALLS.append(('second', dict(info.model_settings or {})))
    return ModelResponse(parts=[TextPart('second answer')])


first_model = FunctionModel(first_reply, model_name='picker-first')
second_model = FunctionModel(second_reply, model_name='picker-second')


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


def settings_for(database_url: str = 'postgresql://unused/test', workspaces: Path = Path('/unused')) -> Settings:
    # Explicit inputs keep developer credentials and deployment model choices out of these tests.
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]  don't read local credentials
        database_url=database_url,
        session_secret=SecretStr('model-preferences-tests'),
        encryption_key=SecretStr(new_key()),
        model=FIRST,
        allowed_models=[FIRST, SECOND, GPT, CLAUDE, PLAIN],
        browser_backend='sammy.browser.fake:FakeBrowser',
        mac_tunnel=False,
        monty_url=None,
        workspaces_dir=workspaces,
        composio_api_key=None,
    )


@pytest.mark.parametrize('model', [GPT, CLAUDE, 'claude-code:claude-opus-4-6'])
def test_generic_thinking_defaults_and_scheduled_low(model: str) -> None:
    settings = settings_for().model_copy(update={'model': model, 'allowed_models': [model]})
    assert thinking_levels(model) == ['low', 'medium', 'high']
    assert defaults(model)['thinking'] == 'medium'
    untouched = validate(Preference(model=model, settings={}), settings)
    assert untouched.settings == {}  # only overrides are saved; defaults apply at run start
    assert resolve(untouched).resolved['thinking'] == 'medium'
    assert resolve(untouched, scheduled=True).resolved['thinking'] == 'low'  # routine runs are cheap by default
    for level in ('low', 'medium', 'high'):
        preference = validate(Preference(model=model, settings={'thinking': level}), settings)
        interactive = resolve(preference)
        scheduled = resolve(preference, scheduled=True)
        assert interactive.settings['thinking'] == interactive.resolved['thinking'] == level
        assert scheduled.resolved['thinking'] == level  # a level the user picked wins
        assert preference.settings == {'thinking': level}
        assert RunModel.model_validate_json(interactive.model_dump_json()) == interactive
    # Sammy's CACHE owns prompt caching: clai2's automatic caching default would conflict with it.
    assert not any(key.startswith('anthropic_cache') for key in resolve(untouched).resolved)
    if model == GPT:
        assert defaults(model)['openai_text_verbosity'] == 'low'


def test_plain_model_has_no_thinking_and_envelope_is_safe() -> None:
    settings = settings_for()
    preference = validate(Preference(model=PLAIN, settings={}), settings)
    assert thinking_levels(PLAIN) == []
    assert 'thinking' not in resolve(preference, scheduled=True).resolved
    result = envelope(preference, settings)
    assert set(result) == {'model', 'settings', 'models'}
    models = result['models']
    assert isinstance(models, list)
    assert [entry['id'] for entry in models] == list(settings.model_choices)
    for entry in models:
        assert set(entry) == {'id', 'name', 'thinking', 'options', 'defaults'}
        assert entry['name']
        assert not PRIVATE_FIELDS.intersection(entry['options'])
        assert not PRIVATE_FIELDS.intersection(entry['defaults'])
        assert set(entry['thinking']) <= {'low', 'medium', 'high'}
        assert all(
            choices and all(isinstance(value, str) for value in choices) for choices in entry['options'].values()
        )


@pytest.mark.parametrize('field', sorted(PRIVATE_FIELDS))
def test_private_provider_overrides_are_rejected_even_when_null(field: str) -> None:
    with pytest.raises(ValueError, match='Unsupported settings'):
        validate(Preference(model=GPT, settings={field: None}), settings_for())


@pytest.mark.parametrize(
    'model,values',
    [
        (GPT, {'thinking': 'xhigh'}),
        (GPT, {'thinking': 'minimal'}),
        (GPT, {'thinking': True}),
        (GPT, {'thinking': False}),
        (GPT, {'thinking': 1}),
        (PLAIN, {'thinking': 'low'}),
        (GPT, {'temperature': 0.5}),
        (CLAUDE, {'openai_text_verbosity': 'low'}),
        (GPT, {'anthropic_cache': True}),
        (GPT, {'unknown': 'value'}),
        (GPT, {'max_tokens': 0}),
        (GPT, {'max_tokens': -1}),
        (GPT, {'max_tokens': '128'}),
        (GPT, {'max_tokens': True}),
        (GPT, {'service_tier': 'invalid'}),
        (PLAIN, {'temperature': 3}),
    ],
)
def test_invalid_or_model_incompatible_settings(model: str, values: dict[str, JsonValue]) -> None:
    with pytest.raises(ValueError):
        validate(Preference(model=model, settings=values), settings_for())


def test_disallowed_models_and_extra_envelope_fields_are_rejected() -> None:
    with pytest.raises(ValueError, match='not allowed'):
        validate(Preference(model='openai:not-allowed', settings={}), settings_for())
    with pytest.raises(ValidationError):
        Preference.model_validate({'model': GPT, 'settings': {}, 'resolved': {'thinking': 'high'}})
    with pytest.raises(ValidationError):
        Preference(model='', settings={})


@pytest.fixture
async def resources(database_url: str, tmp_path: Path) -> AsyncIterator[Resources]:
    CALLS.clear()
    async with open_resources(settings_for(database_url, tmp_path)) as resources:
        yield resources


@pytest.fixture
async def client(resources: Resources) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(resources.settings)

    async def with_resources(scope: Scope, receive: Receive, send: Send) -> None:
        # ASGITransport does not run lifespan. The fixture owns real resources, including DBOS.
        scope['state'] = {'resources': resources}
        await app(scope, receive, send)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=with_resources), base_url='http://test') as client:
        yield client


async def signup(client: httpx.AsyncClient, email: str) -> str:
    response = await client.post('/api/signup', json={'email': email, 'password': 'test-password-123'})
    assert response.status_code == 201, response.text
    return str(response.json()['id'])


async def save(client: httpx.AsyncClient, model: str, values: dict[str, JsonValue]) -> Preference:
    response = await client.put(PATH, json={'model': model, 'settings': values})
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    body = response.json()
    assert set(body) == {'model', 'settings', 'models'}
    return Preference(model=body['model'], settings=body['settings'])


async def new_run(resources: Resources, user_id: str, trigger: Trigger = 'message') -> Run:
    async with resources.pool.connection() as connection:
        thread = await store.create_thread(connection, user_id, 'model selection')
        return await store.create_run(
            connection,
            run_id=str(uuid.uuid4()),
            user_id=user_id,
            thread_id=thread.id,
            prompt='Reply without tools.',
            trigger=trigger,
        )


@pytest.mark.anyio
async def test_api_auth_per_user_persistence_and_reset(client: httpx.AsyncClient, resources: Resources) -> None:
    assert (await client.get(PATH)).status_code == 401
    assert (await client.put(PATH, json={'model': GPT, 'settings': {}})).status_code == 401
    alice = await signup(client, 'alice@example.test')
    alice_cookie = client.cookies.get('sammy_session')
    initial = await client.get(PATH)
    assert initial.status_code == 200
    assert initial.headers['cache-control'] == 'no-store'
    assert initial.json() == envelope(Preference(model=FIRST, settings={}), resources.settings)
    saved = await save(client, GPT, {'thinking': 'high', 'max_tokens': 321})
    assert saved.settings == {'thinking': 'high', 'max_tokens': 321}
    client.cookies.clear()
    bob = await signup(client, 'bob@example.test')
    assert bob != alice
    assert (await client.get(PATH)).json() == initial.json()
    bob_saved = await save(client, CLAUDE, {'thinking': 'low'})
    client.cookies.clear()
    assert alice_cookie is not None
    client.cookies.set('sammy_session', alice_cookie)
    assert (await client.get(PATH)).json()['settings'] == saved.settings
    # A separate pool connection verifies committed storage, not request-local state.
    async with resources.pool.connection() as connection:
        assert await preferences_store.read(connection, alice, resources.settings) == saved
        assert await preferences_store.read(connection, bob, resources.settings) == bob_saved
    reset = await save(client, GPT, {})
    assert reset.settings == {}
    client.cookies.set('sammy_session', 'tampered')
    assert (await client.get(PATH)).status_code == 401


@pytest.mark.anyio
async def test_api_rejected_writes_do_not_change_preferences(client: httpx.AsyncClient) -> None:
    await signup(client, 'alice@example.test')
    await save(client, GPT, {'thinking': 'high'})
    before = (await client.get(PATH)).json()
    bodies: list[object] = [
        {'model': 'openai:disallowed', 'settings': {}},
        {'model': GPT, 'settings': {'thinking': 'xhigh'}},
        {'model': GPT, 'settings': {'custom_params': {'api_key': 'secret'}}},
        {'model': GPT, 'settings': {'max_tokens': '100'}},
        {'model': GPT, 'settings': {'anthropic_cache': True}},
        {'model': GPT, 'settings': {}, 'user_id': 'someone-else'},
        {'model': GPT, 'settings': {}, 'resolved': {}},
        {'model': GPT},
        [],
        None,
    ]
    for body in bodies:
        response = await client.put(PATH, json=body, headers={'content-type': 'application/json'})
        assert response.status_code == 422, response.text
        assert 'secret' not in response.text
        assert (await client.get(PATH)).json() == before
    assert (await client.put(PATH, content='{', headers={'content-type': 'application/json'})).status_code == 422
    assert (await client.put(PATH, content='{}', headers={'content-type': 'text/plain'})).status_code == 415
    assert (await client.get(PATH)).json() == before


@pytest.mark.anyio
async def test_start_snapshot_is_write_once_and_new_runs_read_preferences(
    client: httpx.AsyncClient,
    resources: Resources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice = await signup(client, 'alice@example.test')
    original = await save(client, GPT, {'thinking': 'high', 'max_tokens': 321})
    run = await new_run(resources, alice)
    started = await workflows.start_run(resources, run.id)
    assert len(started) == 7
    assert started[0].id == run.id
    assert started[1] == b'[]'
    assert started[2] is None
    assert started[5] == []
    assert started[6] == resolve(original)
    changed = await save(client, CLAUDE, {'thinking': 'medium'})
    next_run = await new_run(resources, alice)
    assert (await workflows.start_run(resources, next_run.id))[6] == resolve(changed)
    scheduled = await new_run(resources, alice, 'schedule')
    assert (await workflows.start_run(resources, scheduled.id))[6] == resolve(changed, scheduled=True)
    assert (await client.get(PATH)).json()['settings']['thinking'] == 'medium'
    # Revoke the original model and change conversion code: an existing run must read neither.
    resources.settings.allowed_models = [FIRST, SECOND, CLAUDE]

    def unexpected_resolve(preference: Preference, *, scheduled: bool = False) -> RunModel:
        raise AssertionError('a saved run must not be resolved again')

    monkeypatch.setattr(preferences_store, 'resolve', unexpected_resolve)
    assert (await workflows.start_run(resources, run.id))[6] == started[6]
    async with resources.pool.connection() as connection:
        rows = await (
            await connection.execute(
                'SELECT selection FROM sammy.run_models WHERE run_id = %s',
                (run.id,),
            )
        ).fetchall()
        assert len(rows) == 1
        assert RunModel.model_validate(rows[0]['selection']) == started[6]


@pytest.mark.anyio
async def test_revoked_preference_falls_back_for_new_runs_only(
    client: httpx.AsyncClient,
    resources: Resources,
) -> None:
    alice = await signup(client, 'alice@example.test')
    await save(client, GPT, {'thinking': 'high'})
    old_run = await new_run(resources, alice)
    old_selection = (await workflows.start_run(resources, old_run.id))[6]
    resources.settings.allowed_models = [FIRST, SECOND]
    assert (await client.get(PATH)).json()['model'] == FIRST
    fresh = await new_run(resources, alice)
    assert (await workflows.start_run(resources, fresh.id))[6].model == FIRST
    assert (await workflows.start_run(resources, old_run.id))[6] == old_selection


@pytest.mark.anyio
async def test_stored_overrides_a_model_no_longer_accepts_fall_back_to_its_defaults(
    client: httpx.AsyncClient, resources: Resources
) -> None:
    alice = await signup(client, 'alice@example.test')
    async with resources.pool.connection() as connection:
        await preferences_store.save(connection, alice, Preference(model=PLAIN, settings={'thinking': 'high'}))
        assert await preferences_store.read(connection, alice, resources.settings) == Preference(
            model=PLAIN, settings={}
        )


@pytest.mark.anyio
async def test_scripted_next_run_and_real_dbos_step_replay(
    client: httpx.AsyncClient,
    resources: Resources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice = await signup(client, 'alice@example.test')
    await save(client, FIRST, {'max_tokens': 111})
    original = await new_run(resources, alice)
    handle = await workflows.start(original.id)
    assert await asyncio.wait_for(handle.get_result(), 30) == 'done'
    assert [(name, settings['max_tokens']) for name, settings in CALLS] == [('first', 111)]
    # The run's settings merge over the agent's prompt caching instead of replacing it.
    assert CALLS[0][1]['anthropic_cache_messages'] is True
    steps = await DBOS.list_workflow_steps_async(original.id)
    finish = next(step for step in steps if step['function_name'] == 'run.finish')
    start = next(step for step in steps if step['function_name'] == 'run.start')
    output = start['output']
    assert isinstance(output, tuple) and isinstance(output[6], RunModel)
    assert output[6].model == FIRST
    await save(client, SECOND, {'max_tokens': 222})
    fresh = await new_run(resources, alice)
    handle = await workflows.start(fresh.id)
    assert await asyncio.wait_for(handle.get_result(), 30) == 'done'
    assert [(name, settings['max_tokens']) for name, settings in CALLS] == [('first', 111), ('second', 222)]

    async def unexpected_start(resources: Resources, run_id: str) -> None:
        raise AssertionError('DBOS must replay the recorded run.start result')

    monkeypatch.setattr(workflows, 'start_run', unexpected_start)
    # Forking at finish executes the workflow body, replaying all prior persisted steps.
    # This is not just retrieving a completed workflow's cached final result.
    replay = await DBOS.fork_workflow_async(original.id, finish['function_id'])
    assert await asyncio.wait_for(replay.get_result(), 30) == 'done'
    assert [name for name, _ in CALLS] == ['first', 'second']
    async with resources.pool.connection() as connection:
        history = await store.load_history(connection, original.thread_id)
        assert len(history) == 2
        run = await store.load_run(connection, original.id)
        assert run.output == 'first answer'
