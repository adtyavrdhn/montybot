"""#4: sign-ins, cookies, hand-off ids, typed passwords and page contents never reach a trace.

The app runs in this process (on a thread) with the observability setup `montybot serve` uses, plus an in-memory
exporter, through a sign-in hand-off and an approved order. All exported span metadata, including status, links, resource and scope, is searched for secrets.
Standalone adapter tests require no database.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
import psycopg
import pytest
import uvicorn
from conftest import Client, Human
from helpers import eventually, free_port
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, SpanContext, Status, StatusCode
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from sites.shop import Shop
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import Message, Receive, Scope, Send

from montybot import observability
from montybot.app import create_app
from montybot.observability import HTTPtimings, UsageOnlyProvider, configure_observability, timed, timing
from montybot.settings import Settings


@dataclass
class InProcessApp:
    url: str


@pytest.fixture
def traced(database_url: str, workspaces_dir: Path) -> Iterator[tuple[InProcessApp, InMemorySpanExporter]]:
    exporter = InMemorySpanExporter()
    port = free_port()
    settings = Settings(
        database_url=database_url,
        port=port,
        session_secret=SecretStr('test-session-secret'),
        encryption_key=SecretStr('bW9udHlib3QtdGVzdC1rZXktMzItYnl0ZXMtbG9uZyE='),
        model='script:e2e.scripts:model',
        browser_backend='sites.html_browser:new_backend',
        allow_private_networks=True,
        workspaces_dir=workspaces_dir,
    )
    configure_observability(settings, span_processors=[SimpleSpanProcessor(exporter)])
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host='127.0.0.1', port=port, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{port}'

    def healthy() -> bool | None:
        try:
            return httpx.get(f'{url}/healthz').status_code == 200 or None
        except httpx.HTTPError:
            return None

    eventually(healthy, what='the app to start')
    yield InProcessApp(url), exporter
    server.should_exit = True
    thread.join(timeout=30)


def test_no_secrets_in_traces(traced: tuple[InProcessApp, InMemorySpanExporter], database_url: str) -> None:
    app, exporter = traced
    shop = Shop()
    shop.start()
    try:
        client = Client(app)  # pyright: ignore[reportArgumentType]
        client.sign_up(password='my own password 123')
        prompt = f'Order eggs from {shop.url}'
        thread = client.ask(prompt)
        client.wait_for_ask(thread, 'handoff')
        Human(client, client.thread(thread)['run']['id']).sign_in('alice', 'hunter2')
        client.answer(client.wait_for_ask(thread, 'approval'), approved=True)
        assert client.wait_for_reply(thread)
    finally:
        shop.stop()

    with psycopg.connect(database_url) as connection:
        row = connection.execute("SELECT details->>'handoff_id' FROM montybot.asks WHERE kind = 'handoff'").fetchone()
    assert row is not None
    (sid,) = shop.sessions
    secrets = {
        'full shop URL': shop.url,
        'model code': "await click('#add-eggs')",
        'tool arguments': 'Place the order for eggs ($3.20)',
        'hand-off reason': 'Please sign in to the shop, then hand the browser back.',
        'model reply': 'Done. Order #1',
        'typed password': 'hunter2',
        'account password': 'my own password 123',
        'session cookie': sid,
        'hand-off id': row[0],
        'page text': 'In cart: eggs',
        'page text (order)': 'Order #1',
        "the user's message": prompt,
    }

    eventually(
        lambda: any(span.name == 'run.lifecycle' for span in exporter.get_finished_spans()) or None,
        what='the traced run to finish cleanup',
    )
    spans = exporter.get_finished_spans()
    required = {
        'http.server',
        'db.query',
        'db.pool.acquire',
        'run.lifecycle',
        'run.agent',
        'browser.act',
        'browser.snapshot',
        'browser.launch_restore',
        'browser.state.load',
        'browser.state.store',
        'monty.run',
        'monty.dump',
        'model.request',
    }
    assert required <= {span.name for span in spans}
    assert all(
        span.end_time is not None and span.start_time is not None and span.end_time >= span.start_time for span in spans
    )
    assert any('agent run' in span.name or 'invoke_agent' in span.name for span in spans), [s.name for s in spans]
    dumped = dump_spans(spans)
    found = [what for what, secret in secrets.items() if secret in dumped]
    assert found == []


def dump_spans(spans: Sequence[ReadableSpan]) -> str:
    """Use raw values: SDK span JSON omits scope metadata and escapes content."""
    return '\n'.join(
        repr(
            {
                'name': span.name,
                'attributes': dict(span.attributes or {}),
                'status': (span.status.status_code, span.status.description),
                'events': [(event.name, dict(event.attributes or {})) for event in span.events],
                'links': [(link.context, dict(link.attributes or {})) for link in span.links],
                'resource': (span.resource.schema_url, dict(span.resource.attributes)),
                'scope': None
                if span.instrumentation_scope is None
                else (
                    span.instrumentation_scope.name,
                    span.instrumentation_scope.version,
                    span.instrumentation_scope.schema_url,
                    dict(span.instrumentation_scope.attributes or {}),
                ),
            }
        )
        for span in spans
    )


@pytest.fixture
def local_traces() -> Iterator[tuple[TracerProvider, InMemorySpanExporter]]:
    """No global configuration, network exporter or database."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        yield provider, exporter
    finally:
        provider.shutdown()


@pytest.mark.parametrize(
    ('prefix', 'operation'),
    [
        ('chat ', 'model.request'),
        ('generate ', 'model.request'),
        ('text_completion ', 'model.request'),
        ('invoke_agent ', 'invoke_agent'),
        ('agent run ', 'invoke_agent'),
        ('execute_tool ', 'tool.execute'),
        ('running tool ', 'tool.execute'),
        ('running output ', 'tool.execute'),
        ('unknown ', 'agent.operation'),
    ],
)
def test_usage_only_provider_content_free(
    local_traces: tuple[TracerProvider, InMemorySpanExporter], prefix: str, operation: str
) -> None:
    provider, exporter = local_traces
    secret = 'private-content-hunter2-https://shop.invalid/private?cookie=secret'
    tracer = UsageOnlyProvider(provider).get_tracer(
        secret, instrumenting_library_version=secret, schema_url=secret, attributes={'scope.secret': secret}
    )
    context = SpanContext(trace_id=1, span_id=2, is_remote=True)
    counters = {
        'gen_ai.usage.input_tokens': 11,
        'gen_ai.usage.output_tokens': 3,
        'gen_ai.usage.total_tokens': 14,
        'gen_ai.usage.cache_read.input_tokens': 4,
        'gen_ai.usage.cache_creation.input_tokens': 5,
        'gen_ai.usage.cache_read_input_tokens': 6,
        'gen_ai.usage.cache_creation_input_tokens': 7,
        'gen_ai.usage.input_tokens.cache_read': 8,
        'gen_ai.usage.input_tokens.cache_write': 9.5,
    }
    with tracer.start_as_current_span(
        prefix + secret,
        attributes={
            **counters,
            'gen_ai.input.messages': secret,
            'gen_ai.output.messages': secret,
            'gen_ai.tool.call.arguments': secret,
            'gen_ai.tool.call.result': secret,
            'gen_ai.request.model': secret,
            'server.address': secret,
            'arbitrary.numeric': 123,
        },
        links=[Link(context, {'link.secret': secret})],
    ) as span:
        span.update_name(secret)
        span.set_attribute('late.secret', secret)
        span.set_attributes({'other.secret': secret, 'gen_ai.usage.output_tokens': 12})
        span.add_event(secret, {'event.secret': secret})
        span.record_exception(RuntimeError(secret), attributes={'exception.secret': secret}, escaped=True)
        span.add_link(context, {'late.link.secret': secret})
        span.set_status(Status(StatusCode.ERROR, secret))
        span.set_status(StatusCode.ERROR, description=secret)
    (exported,) = exporter.get_finished_spans()
    assert exported.name == operation
    assert dict(exported.attributes or {}) == {**counters, 'gen_ai.usage.output_tokens': 12}
    assert exported.status.status_code == StatusCode.ERROR
    assert exported.status.description is None
    assert not exported.events
    assert not exported.links
    assert exported.instrumentation_scope is not None
    assert exported.instrumentation_scope.name == 'montybot.agent'
    assert secret not in dump_spans([exported])


@pytest.mark.parametrize('invalid_usage', ['private-token-count', True, [123], ['private-token-count']])
def test_usage_only_provider_rejects_non_numeric_usage(
    local_traces: tuple[TracerProvider, InMemorySpanExporter], invalid_usage: str | bool | list[int] | list[str]
) -> None:
    provider, exporter = local_traces
    tracer = UsageOnlyProvider(provider).get_tracer('test')
    with tracer.start_as_current_span(
        'chat private-model', attributes={'gen_ai.usage.input_tokens': invalid_usage}
    ) as span:
        span.set_attribute('gen_ai.usage.output_tokens', invalid_usage)
        span.set_attributes({'gen_ai.usage.cache_read_input_tokens': invalid_usage})
    (exported,) = exporter.get_finished_spans()
    assert dict(exported.attributes or {}) == {}


def test_usage_only_provider_exception_content_free(local_traces: tuple[TracerProvider, InMemorySpanExporter]) -> None:
    provider, exporter = local_traces
    tracer = UsageOnlyProvider(provider).get_tracer('private-scope')
    error = RuntimeError('private-model-tool-exception')
    with pytest.raises(RuntimeError) as caught, tracer.start_as_current_span('chat private-model'):
        raise error
    assert caught.value is error
    (exported,) = exporter.get_finished_spans()
    assert exported.status.status_code == StatusCode.ERROR
    assert exported.status.description is None
    assert not exported.events
    assert str(error) not in dump_spans([exported])


def test_timing_exceptions_content_free(
    local_traces: tuple[TracerProvider, InMemorySpanExporter], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, exporter = local_traces
    monkeypatch.setattr(observability.trace, 'get_tracer', provider.get_tracer)
    error = RuntimeError('private-path-query-header-body-exception')
    with pytest.raises(RuntimeError) as caught, timing('test.operation'):
        raise error
    assert caught.value is error

    @timed('test.async.operation')
    async def fail(secret_argument: str) -> None:
        raise error

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(fail('private-function-argument'))
    assert caught.value is error
    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == ['test.operation', 'test.async.operation']
    for span in spans:
        assert span.status.status_code == StatusCode.ERROR
        assert span.status.description is None
        assert not span.events
        assert dict(span.attributes or {}) == {}
    assert str(error) not in dump_spans(spans)
    assert 'private-function-argument' not in dump_spans(spans)


@pytest.mark.parametrize('fails', [False, True])
def test_http_timings_content_free(
    local_traces: tuple[TracerProvider, InMemorySpanExporter], monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    provider, exporter = local_traces
    monkeypatch.setattr(observability.trace, 'get_tracer', provider.get_tracer)
    secret = 'private-http-content-hunter2'
    error = RuntimeError(secret)
    scope: Scope = {
        'type': 'http',
        'method': 'POST',
        'path': f'/handoff/{secret}',
        'raw_path': f'/handoff/{secret}'.encode(),
        'query_string': f'token={secret}'.encode(),
        'headers': [(b'authorization', secret.encode()), (b'cookie', secret.encode())],
    }
    sent: list[Message] = []

    async def receive() -> Message:
        return {'type': 'http.request', 'body': secret.encode(), 'more_body': False}

    async def send(message: Message) -> None:
        sent.append(message)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert scope['path'].endswith(secret)
        assert (await receive())['body'] == secret.encode()
        await send(
            {
                'type': 'http.response.start',
                'status': 503 if fails else 201,
                'headers': [(b'set-cookie', secret.encode())],
            }
        )
        await send({'type': 'http.response.body', 'body': secret.encode()})
        if fails:
            raise error

    if fails:
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(HTTPtimings(app)(scope, receive, send))
        assert caught.value is error
    else:
        asyncio.run(HTTPtimings(app)(scope, receive, send))
    assert sent[-1]['body'] == secret.encode()
    (exported,) = exporter.get_finished_spans()
    assert exported.name == 'http.server'
    assert dict(exported.attributes or {}) == {'http.response.status_code': 503 if fails else 201}
    assert exported.status.status_code == (StatusCode.ERROR if fails else StatusCode.UNSET)
    assert exported.status.description is None
    assert not exported.events
    assert secret not in dump_spans([exported])


def test_trace_dump_includes_metadata_surfaces() -> None:
    """Guard the leak detector itself, especially scope metadata absent from SDK JSON."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider(
        resource=Resource({'resource.secret': 'private-resource'}, schema_url='private-resource-url')
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        tracer = provider.get_tracer(
            'private-scope-name', 'private-scope-version', 'private-scope-url', {'scope.secret': 'private-scope-attr'}
        )
        with tracer.start_as_current_span(
            'private-span-name',
            attributes={'secret': 'private-span-attr'},
            links=[Link(SpanContext(trace_id=1, span_id=2, is_remote=True), {'secret': 'private-link-attr'})],
        ) as span:
            span.set_status(Status(StatusCode.ERROR, 'private-status'))
            span.add_event('private-event-name', {'secret': 'private-event-attr'})
        dumped = dump_spans(exporter.get_finished_spans())
        for secret in (
            'private-resource',
            'private-resource-url',
            'private-scope-name',
            'private-scope-version',
            'private-scope-url',
            'private-scope-attr',
            'private-span-name',
            'private-span-attr',
            'private-link-attr',
            'private-status',
            'private-event-name',
            'private-event-attr',
        ):
            assert secret in dumped
    finally:
        provider.shutdown()


def test_configured_agent_content_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the actual Logfire/Pydantic AI integration, not just the OTel adapter."""
    monkeypatch.delenv('LOGFIRE_TOKEN', raising=False)
    monkeypatch.setenv('OTEL_RESOURCE_ATTRIBUTES', 'private-resource=https://private.invalid/?credential=private-key')
    exporter = InMemorySpanExporter()
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]  Settings runtime option: don't read local credentials.
        database_url='postgresql://unused',
        session_secret=SecretStr('private-session-key'),
        encryption_key=SecretStr('private-encryption-key'),
        LOGFIRE_TOKEN=None,
    )
    configure_observability(settings, span_processors=[SimpleSpanProcessor(exporter)])
    agent = Agent(
        TestModel(custom_output_text='private-model-response'),
        name='private-agent-name',
        instructions='private-instructions',
    )

    @agent.tool_plain
    def private_tool_name(private_argument: str) -> str:
        return 'private-tool-result'

    result = asyncio.run(agent.run('private-user-message'))
    assert result.output == 'private-model-response'
    spans = exporter.get_finished_spans()
    assert {'invoke_agent', 'model.request', 'tool.execute'} <= {span.name for span in spans}
    assert any((span.attributes or {}).get('gen_ai.usage.input_tokens', 0) for span in spans)
    assert 'private-' not in dump_spans(spans)
    assert all(dict(span.resource.attributes) == {'service.name': 'montybot'} for span in spans)


def test_http_timings_include_generated_500(
    local_traces: tuple[TracerProvider, InMemorySpanExporter], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, exporter = local_traces
    monkeypatch.setattr(observability.trace, 'get_tracer', provider.get_tracer)
    error = RuntimeError('private-endpoint-failure')

    async def endpoint(request: object) -> Response:
        raise error

    app = HTTPtimings(Starlette(routes=[Route('/', endpoint)]))
    scope: Scope = {'type': 'http', 'method': 'GET', 'path': '/', 'headers': [], 'query_string': b''}
    sent: list[Message] = []

    async def receive() -> Message:
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(message: Message) -> None:
        # The complete response is sent before the timing span ends.
        assert not exporter.get_finished_spans()
        sent.append(message)

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(app(scope, receive, send))
    assert caught.value is error
    assert sent[0]['status'] == 500
    (span,) = exporter.get_finished_spans()
    assert dict(span.attributes or {}) == {'http.response.status_code': 500}
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description is None
    assert not span.events
    assert 'private-' not in dump_spans([span])
