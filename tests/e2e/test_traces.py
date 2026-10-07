"""What reaches a trace (`montybot/observability.py`).

Never, whatever the settings: sign-in passwords, the account password, session cookies and hand-off ids.
Content (messages, replies, the agent's code, pages, exception messages) only with `logfire_include_content`.
Always: span names, run ids and the deploy's commit. HTTP server requests are not traced.
The apps' telemetry is forwarded with the server's token, and a client's `traceparent` joins its trace.

The app runs in this process (on a thread) with the observability setup `montybot serve` uses, plus an in-memory
exporter, through a sign-in hand-off and an approved order. All exported span metadata, including status, events,
links, resource and scope, is searched. Standalone tests require no database.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import logfire
import psycopg
import pytest
import uvicorn
from conftest import Client, Human
from helpers import eventually, free_port
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
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
    with serve_traced(database_url, workspaces_dir, logfire_include_content=include_content) as (app, exporter):
        yield app, exporter, include_content


@contextmanager
def serve_traced(
    database_url: str, workspaces_dir: Path, **settings: Any
) -> Iterator[tuple[InProcessApp, InMemorySpanExporter]]:
    """The app on a thread with `montybot serve`'s observability, plus an in-memory exporter."""
    exporter = InMemorySpanExporter()
    port = free_port()
    configured = Settings(
        database_url=database_url,
        port=port,
        session_secret=SecretStr('test-session-secret'),
        encryption_key=SecretStr('bW9udHlib3QtdGVzdC1rZXktMzItYnl0ZXMtbG9uZyE='),
        model='script:e2e.scripts:model',
        browser_backend='sites.html_browser:new_backend',
        allow_private_networks=True,
        workspaces_dir=workspaces_dir,
        commit='abc1234',
        environment='test',
        **settings,
    )
    configure_observability(configured, span_processors=[SimpleSpanProcessor(exporter)])
    # The tests' httpx client stands in for an app elsewhere. Instrumented in this process, it would trace every
    # request and send its own `traceparent`, over one a test sends.
    HTTPXClientInstrumentor().uninstrument()
    server = uvicorn.Server(uvicorn.Config(create_app(configured), host='127.0.0.1', port=port, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{port}'

    def healthy() -> bool | None:
        try:
            return httpx.get(f'{url}/healthz').status_code == 200 or None
        except httpx.HTTPError:
            return None

    eventually(healthy, what='the app to start')
    try:
        yield InProcessApp(url), exporter
    finally:
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
    # An untraced client's run is a trace of its own, the agent in it; requests and polling start none.
    assert lifecycle.parent is None
    (agent_run,) = [span for span in spans if span.name == 'invoke_agent montybot']
    assert agent_run.context and lifecycle.context
    assert agent_run.context.trace_id == lifecycle.context.trace_id
    roots = {span.name for span in spans if span.parent is None}
    assert not roots & {'db.query', 'db.pool.acquire', 'run.dispatch', 'browser.peek_screenshot'}, roots
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


def test_outgoing_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calls made with httpx, such as a model provider's, are spans: method, URL and status, never headers."""
    monkeypatch.delenv('LOGFIRE_TOKEN', raising=False)
    exporter = InMemorySpanExporter()
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]  Settings runtime option: don't read local credentials.
        database_url='postgresql://unused',
        session_secret=SecretStr('session-key'),
        encryption_key=SecretStr('encryption-key'),
        LOGFIRE_TOKEN=None,
    )
    configure_observability(settings, span_processors=[SimpleSpanProcessor(exporter)])
    with fake_logfire() as (base_url, _):

        async def call() -> int:
            async with httpx.AsyncClient() as client:
                response = await client.get(f'{base_url}/v1/models', headers={'authorization': 'private-api-key'})
                return response.status_code

        assert asyncio.run(call()) == 200
    (span,) = [span for span in exporter.get_finished_spans() if span.name == 'GET']
    assert (span.attributes or {})['http.status_code'] == 200
    assert 'private-api-key' not in dump_spans(exporter.get_finished_spans())


# --- the apps' own telemetry: forwarded with the server's token, and joined to the server's spans ---

CLIENT_TRACE_ID = '0af7651916cd43dd8448eb211c80319c'
CLIENT_SPAN_ID = 'b7ad6b7169203331'
TRACEPARENT = f'00-{CLIENT_TRACE_ID}-{CLIENT_SPAN_ID}-01'


@dataclass
class Received:
    path: str
    headers: dict[str, str]
    body: bytes


@contextmanager
def fake_logfire() -> Iterator[tuple[str, list[Received]]]:
    """Logfire's ingest, answering every export with an empty success."""
    received: list[Received] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get('content-length', 0)))
            received.append(Received(self.path, {k.lower(): v for k, v in self.headers.items()}, body))
            self.send_response(200)
            self.send_header('content-type', self.headers.get('content-type', 'application/x-protobuf'))
            self.end_headers()
            self.wfile.write(b'{}' if 'json' in self.headers.get('content-type', '') else b'')

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header('content-type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{}')

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_address[1]}', received
    finally:
        server.shutdown()


@pytest.fixture
def with_token(
    database_url: str, workspaces_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[InProcessApp, InMemorySpanExporter, list[Received]]]:
    with fake_logfire() as (base_url, received):
        monkeypatch.setenv('LOGFIRE_BASE_URL', base_url)
        token = SecretStr('test-logfire-token')
        with serve_traced(database_url, workspaces_dir, LOGFIRE_TOKEN=token) as (app, exporter):
            yield app, exporter, received
        logfire.configure(send_to_logfire=False, console=False)  # stop exporting to the fake before it goes


def test_forwarded_client_telemetry(with_token: tuple[InProcessApp, InMemorySpanExporter, list[Received]]) -> None:
    app, _, received = with_token
    client = Client(app)  # pyright: ignore[reportArgumentType]
    batch = b'{"resourceSpans": [{"marker": "client-batch"}]}'
    json_type = {'content-type': 'application/json'}
    assert client.http.get('/api/telemetry').status_code == 401
    assert client.http.post('/api/telemetry/v1/traces', content=batch, headers=json_type).status_code == 401

    client.sign_up()
    settings = client.http.get('/api/telemetry').json()
    assert settings == {'enabled': True, 'include_content': True, 'environment': 'test', 'version': 'abc1234'}
    text = {'content-type': 'text/plain'}
    assert client.http.post('/api/telemetry/v1/traces', content=batch, headers=text).status_code == 415
    assert client.http.post('/api/telemetry/v1/secrets', content=batch, headers=json_type).status_code == 400
    assert client.http.post('/api/telemetry/v1/traces', content=batch, headers=json_type).status_code == 200

    (forwarded,) = eventually(
        lambda: [r for r in received if r.body == batch] or None, what='the batch to reach Logfire'
    )
    assert forwarded.path == '/v1/traces'
    assert forwarded.headers['authorization'] == 'test-logfire-token'
    assert forwarded.headers['content-type'] == 'application/json'
    assert 'cookie' not in forwarded.headers
    assert not any('montybot_session' in value for value in forwarded.headers.values())


def test_client_trace_joins_server_spans(
    with_token: tuple[InProcessApp, InMemorySpanExporter, list[Received]],
) -> None:
    """A traced message: its database spans and the run it starts are in the client's trace, with no HTTP span."""
    app, exporter, _ = with_token
    client = Client(app)  # pyright: ignore[reportArgumentType]
    client.sign_up()
    exporter.clear()
    response = client.http.post(
        '/api/threads',
        json={'text': 'Say hello'},
        headers={'traceparent': TRACEPARENT, 'baggage': 'user_id=forged'},
    )
    assert response.status_code == 201, response.text
    assert client.wait_for_reply(response.json()['thread_id'])
    eventually(
        lambda: any(span.name == 'run.lifecycle' for span in exporter.get_finished_spans()) or None,
        what='the run to finish',
    )
    spans = exporter.get_finished_spans()
    joined = [span for span in spans if span.context is not None and span.context.trace_id == int(CLIENT_TRACE_ID, 16)]
    names = {span.name for span in joined}
    assert {'db.query', 'run.lifecycle', 'invoke_agent montybot'} <= names, names
    assert 'http.server' not in {span.name for span in spans}
    roots = [span for span in joined if span.parent is not None and span.parent.span_id == int(CLIENT_SPAN_ID, 16)]
    assert roots and all(span.parent is not None and span.parent.is_remote for span in roots)
    assert not any((span.attributes or {}).get('user_id') == 'forged' for span in spans)

    # Polling without traceparent makes no spans at all: its database calls start no trace.
    exporter.clear()
    assert client.http.get('/api/threads').status_code == 200
    assert exporter.get_finished_spans() == ()


def test_client_telemetry_off_without_token(traced: tuple[InProcessApp, InMemorySpanExporter, bool]) -> None:
    app, _, include_content = traced
    client = Client(app)  # pyright: ignore[reportArgumentType]
    client.sign_up()
    settings = client.http.get('/api/telemetry').json()
    assert settings == {
        'enabled': False,
        'include_content': include_content,
        'environment': 'test',
        'version': 'abc1234',
    }
    response = client.http.post('/api/telemetry/v1/traces', content=b'{}', headers={'content-type': 'application/json'})
    assert response.status_code == 403


def test_client_trace_context_websocket_and_lifespan(local_traces: InMemorySpanExporter) -> None:
    """The live view's WebSocket continues a client's trace too; other scopes pass through untouched."""
    seen: list[str] = []

    async def inner(scope: Scope, receive: Any, send: Any) -> None:
        with timing('inner'):
            seen.append(scope['type'])

    app = observability.ClientTraceContext(inner)

    async def call(scope: Scope) -> None:
        await app(scope, None, None)  # pyright: ignore[reportArgumentType]

    asyncio.run(call({'type': 'websocket', 'headers': [(b'traceparent', TRACEPARENT.encode())]}))
    asyncio.run(call({'type': 'lifespan'}))
    asyncio.run(call({'type': 'http', 'headers': [(b'traceparent', b'not-a-traceparent')]}))
    assert seen == ['websocket', 'lifespan', 'http']
    websocket, lifespan, invalid = local_traces.get_finished_spans()
    assert websocket.parent is not None and websocket.parent.span_id == int(CLIENT_SPAN_ID, 16)
    assert lifespan.parent is None and invalid.parent is None
