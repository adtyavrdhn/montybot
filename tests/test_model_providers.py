"""Models from the Pydantic AI Gateway and the operator's OpenAI-compatible servers, offline.

The OpenAI-compatible server is a fake on 127.0.0.1: it lists two models, refuses tools for one of them (as vLLM
does without `--enable-auto-tool-choice`), and answers every chat with the same scripted reply.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
import uvicorn
from anthropic import AsyncAnthropic
from helpers import eventually, free_port
from pydantic import SecretStr, ValidationError
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from test_model_preferences import client_for, new_run, save, settings_for, signup

from sammy import store, workflows
from sammy.model_providers import Providers
from sammy.resources import Resources, open_resources
from sammy.settings import Endpoint, Settings

KEY = 'fake-server-key'
GATEWAY_KEY = 'pylf_v1_us_sammytestkey'
ANSWER = 'Hello from the tiny model.'


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@dataclass
class FakeServer:
    """What the server was asked: (path, Authorization header, JSON body)."""

    requests: list[tuple[str, str | None, dict[str, object]]] = field(default_factory=list)

    def app(self) -> Starlette:
        return Starlette(
            routes=[Route('/v1/models', self.models), Route('/v1/chat/completions', self.chat, methods=['POST'])]
        )

    async def models(self, request: Request) -> Response:
        self.requests.append(('/v1/models', request.headers.get('authorization'), {}))
        if request.headers.get('authorization') != f'Bearer {KEY}':
            return JSONResponse({'error': {'message': 'Invalid key'}}, status_code=401)
        listed = [{'id': name, 'object': 'model', 'owned_by': 'fake'} for name in ('tiny-tools', 'tiny-plain')]
        return JSONResponse({'object': 'list', 'data': listed})

    async def chat(self, request: Request) -> Response:
        body = cast(dict[str, object], await request.json())
        self.requests.append(('/v1/chat/completions', request.headers.get('authorization'), body))
        if request.headers.get('authorization') != f'Bearer {KEY}':
            return JSONResponse({'error': {'message': 'Invalid key'}}, status_code=401)
        if body.get('tools') and body['model'] == 'tiny-plain':
            message = '"auto" tool choice requires --enable-auto-tool-choice'
            return JSONResponse({'error': {'message': message}}, status_code=400)
        reply = {'id': 'chat-1', 'created': 0, 'model': body['model']}
        usage = {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}
        if not body.get('stream'):
            choice = {'index': 0, 'message': {'role': 'assistant', 'content': ANSWER}, 'finish_reason': 'stop'}
            return JSONResponse({**reply, 'object': 'chat.completion', 'choices': [choice], 'usage': usage})
        chunks = [
            {'index': 0, 'delta': {'role': 'assistant', 'content': ANSWER}, 'finish_reason': None},
            {'index': 0, 'delta': {}, 'finish_reason': 'stop'},
        ]
        lines = [
            f'data: {json.dumps({**reply, "object": "chat.completion.chunk", "choices": [chunk], "usage": usage})}\n\n'
            for chunk in chunks
        ]
        return StreamingResponse(iter([*lines, 'data: [DONE]\n\n']), media_type='text/event-stream')


@contextmanager
def serving(app: Starlette) -> Iterator[str]:
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning', lifespan='off'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    eventually(lambda: server.started or None, timeout=30, what='the fake server')
    try:
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def fake() -> Iterator[tuple[FakeServer, str]]:
    server = FakeServer()
    with serving(server.app()) as url:
        yield server, url


def with_endpoints(settings: Settings, *endpoints: Endpoint) -> Settings:
    return settings.model_copy(update={'openai_compatible': list(endpoints)})


def test_a_gateway_model_uses_the_configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('PYDANTIC_AI_GATEWAY_API_KEY', raising=False)
    monkeypatch.delenv('PYDANTIC_AI_GATEWAY_BASE_URL', raising=False)
    settings = settings_for().model_copy(update={'pydantic_ai_gateway_api_key': SecretStr(GATEWAY_KEY)})
    providers = Providers(settings)
    claude = providers.model('gateway/anthropic:claude-sonnet-4-6')
    assert isinstance(claude, AnthropicModel)
    assert isinstance(claude.client, AsyncAnthropic)
    assert claude.client.auth_token == GATEWAY_KEY
    assert str(claude.client.base_url).startswith('https://gateway-us.pydantic.dev/proxy')
    gpt = providers.model('gateway/openai-chat:gpt-5.2')
    assert isinstance(gpt, OpenAIChatModel)
    assert gpt.client.api_key == GATEWAY_KEY
    assert str(gpt.client.base_url).startswith('https://gateway-us.pydantic.dev/proxy')
    # Without a key, the name goes to Pydantic AI as it is, which reads the environment's key.
    assert Providers(settings_for()).model('gateway/openai:gpt-5.2') == 'gateway/openai:gpt-5.2'


def test_endpoints_are_validated_and_vllm_is_shorthand_for_one() -> None:
    settings = settings_for().model_copy(update={'vllm_url': 'http://vllm.internal:8000/', 'vllm_api_key': None})
    assert settings.endpoints == [Endpoint(name='vllm', base_url='http://vllm.internal:8000/v1')]
    model = Providers(settings).model('vllm:qwen3-32b')
    assert isinstance(model, OpenAIChatModel)
    assert model.model_name == 'qwen3-32b'
    assert str(model.client.base_url) == 'http://vllm.internal:8000/v1/'
    for bad in ('ftp://host', 'http://user:secret@host', 'http://host/?key=secret'):
        with pytest.raises(ValidationError):
            Endpoint(name='local', base_url=bad)
    with pytest.raises(ValidationError):
        Endpoint(name='Has Spaces', base_url='http://host')
    for names in (['local', 'local'], ['script'], ['claude-code']):
        endpoints = [{'name': name, 'base_url': 'http://host'} for name in names]
        with pytest.raises(ValidationError, match='unique names'):
            Settings.model_validate({**settings_for().model_dump(), 'openai_compatible': endpoints})


@pytest.mark.anyio
async def test_discovery_offers_only_models_with_tools(fake: tuple[FakeServer, str]) -> None:
    server, url = fake
    providers = Providers(with_endpoints(settings_for(), Endpoint(name='local', base_url=url, api_key=SecretStr(KEY))))
    await providers.discover()
    assert providers.discovered == ('local:tiny-tools',)
    assert providers.choices[-1] == 'local:tiny-tools'
    asked = [str(body['model']) for path, _, body in server.requests if path == '/v1/chat/completions']
    assert sorted(asked) == ['tiny-plain', 'tiny-tools']  # one probe each
    assert {auth for _, auth, _ in server.requests} == {f'Bearer {KEY}'}
    # A list of models narrows what is offered and probed; a wrong key or a server that is down offers nothing.
    server.requests.clear()
    only = Endpoint(name='local', base_url=url, api_key=SecretStr(KEY), models=['tiny-plain'])
    wrong = Endpoint(name='wrong', base_url=url, api_key=SecretStr('not-the-key'))
    down = Endpoint(name='down', base_url=f'http://127.0.0.1:{free_port()}')
    providers = Providers(with_endpoints(settings_for(), only, wrong, down))
    await providers.discover()
    assert providers.discovered == ()
    assert [body['model'] for path, _, body in server.requests if path == '/v1/chat/completions'] == ['tiny-plain']


@pytest.fixture
async def resources(database_url: str, tmp_path: Path, fake: tuple[FakeServer, str]) -> AsyncIterator[Resources]:
    endpoint = Endpoint(name='local', base_url=fake[1], api_key=SecretStr(KEY))
    async with open_resources(with_endpoints(settings_for(database_url, tmp_path), endpoint)) as resources:
        yield resources


@pytest.mark.anyio
async def test_a_run_on_a_discovered_model(resources: Resources, fake: tuple[FakeServer, str]) -> None:
    server, _ = fake
    assert 'local:tiny-tools' in resources.providers.choices
    async with client_for(resources) as client:
        alice = await signup(client, 'alice@example.test')
        picker = (await client.get('/api/model-preferences')).json()
        assert 'local:tiny-tools' in [model['id'] for model in picker['models']]
        assert 'local:tiny-plain' not in [model['id'] for model in picker['models']]
        assert (await client.put('/api/model-preferences', json={'model': 'local:tiny-plain', 'settings': {}})).is_error
        await save(client, 'local:tiny-tools', {})
    server.requests.clear()
    run = await new_run(resources, alice)
    handle = await workflows.start(run.id)
    assert await asyncio.wait_for(handle.get_result(), 60) == 'done'
    async with resources.pool.connection() as connection:
        assert (await store.load_run(connection, run.id)).output == ANSWER
    [(_, auth, body)] = [request for request in server.requests if request[0] == '/v1/chat/completions']
    assert auth == f'Bearer {KEY}'
    assert body['model'] == 'tiny-tools'
    tools = cast(list[dict[str, dict[str, str]]], body['tools'])
    assert 'run_code' in {tool['function']['name'] for tool in tools}  # the run had Sammy's tools
