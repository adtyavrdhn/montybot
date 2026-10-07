"""What reaches a trace (`montybot/observability.py`).

Never, whatever the settings: sign-in passwords, the account password, session cookies and hand-off ids.
Content (messages, replies, the agent's code, pages, exception messages) only with `logfire_include_content`.
Always: span names, run ids and the deploy's commit. HTTP server requests are not traced.

The app runs in this process (on a thread) with the observability setup `montybot serve` uses, plus an in-memory
exporter, through a sign-in hand-off and an approved order. All exported span metadata, including status, events,
links, resource and scope, is searched. Standalone tests require no database.
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
from starlette.types import ASGIApp, Message, Scope

from montybot import observability
from montybot.app import create_app
from montybot.browser.contract import ActionFailed, LifecycleError
from montybot.browser.service import UnknownRun
from montybot.observability import configure_observability, timed, timing
from montybot.settings import Settings


@dataclass
class InProcessApp:
    url: str


@pytest.fixture(params=[True, False], ids=['content', 'no-content'])
def traced(
    request: pytest.FixtureRequest, database_url: str, workspaces_dir: Path
) -> Iterator[tuple[InProcessApp, InMemorySpanExporter, bool]]:
    include_content: bool = request.param
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
        logfire_include_content=include_content,
        commit='abc1234',
        environment='test',
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
    yield InProcessApp(url), exporter, include_content
    server.should_exit = True
    thread.join(timeout=30)


def test_traces(traced: tuple[InProcessApp, InMemorySpanExporter, bool], database_url: str) -> None:
    app, exporter, include_content = traced
    shop = Shop()
    shop.start()
    try:
        client = Client(app)  # pyright: ignore[reportArgumentType]
        client.sign_up(password='my own password 123')
        prompt = f'Order eggs from {shop.url}'
        thread = client.ask(prompt)
        client.wait_for_ask(thread, 'handoff')
        run_id = client.thread(thread)['run']['id']
        Human(client, run_id).sign_in('alice', 'hunter2')
        client.answer(client.wait_for_ask(thread, 'approval'), approved=True)
        assert client.wait_for_reply(thread)
    finally:
        shop.stop()

    with psycopg.connect(database_url) as connection:
        row = connection.execute("SELECT details->>'handoff_id' FROM montybot.asks WHERE kind = 'handoff'").fetchone()
        user = connection.execute('SELECT user_id FROM montybot.runs WHERE id = %s', (run_id,)).fetchone()
    assert row is not None and user is not None
    (sid,) = shop.sessions
    never = {
        'typed password': 'hunter2',
        'account password': 'my own password 123',
        'session cookie': sid,
        'hand-off id': row[0],
    }
    content = {
        "the user's message": prompt,
        'model code': '#add-eggs',
        'tool arguments': 'Place the order for eggs ($3.20)',
        'model reply': 'Done. Order #1',
        'page text': 'In cart: eggs',
    }

    eventually(
        lambda: any(span.name == 'run.lifecycle' for span in exporter.get_finished_spans()) or None,
        what='the traced run to finish cleanup',
    )
    spans = exporter.get_finished_spans()
    names = {span.name for span in spans}
    required = {
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
        'chat scripted',
        'execute_tool run_code',
        'invoke_agent montybot',
    }
    assert required <= names, sorted(names)
    assert all(
        span.end_time is not None and span.start_time is not None and span.end_time >= span.start_time for span in spans
    )

    (lifecycle,) = [span for span in spans if span.name == 'run.lifecycle']
    assert (lifecycle.attributes or {})['user_id'] == str(user[0])
    model_requests = [span for span in spans if span.name == 'chat scripted']
    assert all((span.attributes or {}).get('run_id') == run_id for span in model_requests)
    assert 'http.server' not in names
    assert any((span.attributes or {}).get('browser.site') == '127.0.0.1' for span in spans)
    resource = spans[0].resource.attributes
    assert (resource['service.version'], resource['deployment.environment.name']) == ('abc1234', 'test')

    dumped = dump_spans(spans)
    assert [what for what, secret in never.items() if secret in dumped] == []
    found = [what for what, text in content.items() if text in dumped]
    assert found == (list(content) if include_content else [])


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
def local_traces(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    """No global configuration, network exporter or database."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(observability.trace, 'get_tracer', provider.get_tracer)
    try:
        yield exporter
    finally:
        provider.shutdown()


@pytest.mark.parametrize('include_content', [True, False])
def test_timing_exceptions(
    local_traces: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch, include_content: bool
) -> None:
    monkeypatch.setattr(observability, '_include_content', include_content)
    error = RuntimeError('page-or-row-text-in-the-message')
    with pytest.raises(RuntimeError) as caught, timing('test.operation'):
        raise error
    assert caught.value is error

    @timed('test.async.operation')
    async def fail(argument: str) -> None:
        raise error

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(fail('function-argument'))
    assert caught.value is error
    spans = local_traces.get_finished_spans()
    assert [span.name for span in spans] == ['test.operation', 'test.async.operation']
    for span in spans:
        assert span.status.status_code == StatusCode.ERROR
        assert dict(span.attributes or {}) == {'error.type': 'RuntimeError'}
        assert [event.name for event in span.events] == (['exception'] if include_content else [])
    assert (str(error) in dump_spans(spans)) is include_content
    assert 'function-argument' not in dump_spans(spans)


def test_only_our_failures_are_errors(local_traces: InMemorySpanExporter) -> None:
    """A page that would not load, the browser service's answers by design, the agent's own code errors and a user's
    stop are not failures of ours: error rates count only the rest."""
    cases: list[tuple[BaseException, StatusCode, dict[str, object]]] = [
        (
            ActionFailed('could not load the page'),
            StatusCode.UNSET,
            {'error.kind': 'expected', 'logfire.level_num': 13},
        ),
        (UnknownRun('no browser to watch'), StatusCode.UNSET, {'error.kind': 'expected', 'logfire.level_num': 13}),
        (asyncio.CancelledError(), StatusCode.UNSET, {'error.kind': 'cancelled'}),
        (LifecycleError('not open'), StatusCode.ERROR, {}),
        (KeyError('bug'), StatusCode.ERROR, {}),
    ]
    for error, _, _ in cases:
        with pytest.raises(type(error)), timing('test.outcome'):
            raise error
    for span, (error, status, attributes) in zip(local_traces.get_finished_spans(), cases, strict=True):
        assert span.status.status_code == status, error
        assert dict(span.attributes or {}) == {'error.type': type(error).__qualname__, **attributes}


def asgi_call(app: ASGIApp, path: str, secret: str, *, method: str = 'POST') -> list[Message]:
    scope: Scope = {
        'type': 'http',
        'method': method,
        'path': path,
        'raw_path': path.encode(),
        'root_path': '',
        'query_string': f'token={secret}'.encode(),
        'headers': [(b'authorization', secret.encode()), (b'cookie', secret.encode())],
    }
    sent: list[Message] = []

    async def receive() -> Message:
        return {'type': 'http.request', 'body': secret.encode(), 'more_body': False}

    async def send(message: Message) -> None:
        sent.append(message)

    async def call() -> None:
        await app(scope, receive, send)

    asyncio.run(call())
    return sent


@pytest.mark.parametrize(('path', 'status'), [('/', 200), ('/static/app.css', 200), ('/missing', 404)])
def test_http_requests_not_traced(local_traces: InMemorySpanExporter, path: str, status: int) -> None:
    settings = Settings(
        database_url='postgresql://unused',
        session_secret=SecretStr('test-session-secret'),
        encryption_key=SecretStr('unused'),
    )
    sent = asgi_call(create_app(settings), path, 'request-secret', method='GET')
    assert sent[0]['status'] == status
    assert local_traces.get_finished_spans() == ()


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


@pytest.mark.parametrize('include_content', [True, False])
def test_configured_agent(monkeypatch: pytest.MonkeyPatch, include_content: bool) -> None:
    """The actual Logfire/Pydantic AI integration: names, models and usage always; content only when included."""
    monkeypatch.delenv('LOGFIRE_TOKEN', raising=False)
    exporter = InMemorySpanExporter()
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]  Settings runtime option: don't read local credentials.
        database_url='postgresql://unused',
        session_secret=SecretStr('session-key'),
        encryption_key=SecretStr('encryption-key'),
        LOGFIRE_TOKEN=None,
        logfire_include_content=include_content,
    )
    configure_observability(settings, span_processors=[SimpleSpanProcessor(exporter)])
    agent = Agent(TestModel(custom_output_text='content-model-response'), name='agent', instructions='content-instr')

    @agent.tool_plain
    def lookup(query: str) -> str:
        return 'content-tool-result'

    result = asyncio.run(agent.run('content-user-message'))
    assert result.output == 'content-model-response'
    spans = exporter.get_finished_spans()
    assert {'invoke_agent agent', 'chat test', 'execute_tool lookup'} <= {span.name for span in spans}
    chat = next(span for span in spans if span.name == 'chat test')
    assert (chat.attributes or {})['gen_ai.request.model'] == 'test'
    assert (chat.attributes or {})['gen_ai.usage.input_tokens']
    dumped = dump_spans(spans)
    for text in ('content-model-response', 'content-instr', 'content-tool-result', 'content-user-message'):
        assert (text in dumped) is include_content, text
