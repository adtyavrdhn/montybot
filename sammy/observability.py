"""Logfire tracing: what is exported, and what never is.

Always exported: span names, model/provider/tool names, token usage and cost, the conversation's shape, the deploy's
commit and environment, the run/thread/user ids (random UUIDs), the site
(host) a browser step visits, outgoing HTTP calls made with httpx (method, URL, status; never headers or bodies; not
Discord's interaction callbacks or attachment downloads, whose URLs hold a token or a signature),
exception types, metrics and system metrics.

Exported only with `LOGFIRE_INCLUDE_CONTENT` (on by default for the demo): messages, replies, instructions (which
include the user's memories), the code the agent writes, page snapshots, and exception messages and tracebacks.

Never exported, whatever the settings: cookies and browser state, saved sign-ins, passwords typed in live view,
session cookies, app secrets and API keys (chat platform tokens included), hand-off ids and links, chat app link
codes, and push subscription URLs. None of these reach the agent. Chat app spans (`channel.receive`,
`channel.deliver`, `discord.event`) carry the platform's name or event type and nothing else that identifies anyone. HTTP server requests are not traced.
`tests/e2e/test_traces.py` holds these lines.

The web and Mac apps send their own telemetry through `/api/telemetry/v1/...` (`api.forward_telemetry`), which
forwards it to Logfire with the server's token. Forwarded data is not scrubbed here, so each client keeps to the same
lines itself (`sammy/static/telemetry.js`, `macos/Sources/SammyKit/Telemetry.swift`). A client that traces an
action sends `traceparent`, and `ClientTraceContext` puts the server's spans for that request in the client's trace.
A run started any other way is a trace of its own (`workflows.start`), and calls made all the time, such as the
database calls behind polling, are recorded only inside a trace (`timing(..., only_in_trace=True)`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Generator, Sequence
from contextlib import contextmanager
from functools import wraps
from types import CoroutineType
from typing import Any, Literal, ParamSpec, TypeVar

import logfire
from dbos._error import DBOSAwaitedWorkflowCancelledError, DBOSWorkflowCancelledError
from opentelemetry import context, trace
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.trace import Span, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic_monty import MontyError
from starlette.types import ASGIApp, Receive, Scope, Send

from sammy.browser.contract import BrowserError, LifecycleError
from sammy.settings import Settings

P = ParamSpec('P')
T = TypeVar('T')

_include_content = False


def configure_observability(settings: Settings, *, span_processors: Sequence[SpanProcessor] = ()) -> None:
    """`span_processors` is for tests, which read every span the app makes."""
    global _include_content
    _include_content = settings.logfire_include_content
    logfire.configure(
        service_name=settings.service_name,
        service_version=settings.commit or None,  # an image built without the COMMIT arg has it empty
        environment=settings.environment,
        token=settings.logfire_token.get_secret_value() if settings.logfire_token else None,
        send_to_logfire='if-token-present',
        console=False,
        additional_span_processors=span_processors,
    )
    logfire.instrument_pydantic_ai(include_content=_include_content, version=5)
    logfire.instrument_httpx()  # outgoing calls (model providers among them); never headers or bodies
    logfire.instrument_system_metrics()


def record_error(span: Span, error: BaseException) -> None:
    """The type always; the message and traceback, which can quote pages or rows, only with content. Only a failure
    of ours marks the span as an error (see `kind_of`), so error rates mean bugs."""
    kind = kind_of(error)
    span.set_attribute('error.type', type(error).__qualname__)
    if kind == 'error':
        span.set_status(StatusCode.ERROR)
    else:
        span.set_attribute('error.kind', kind)
        if kind == 'expected':
            span.set_attribute('logfire.level_num', _WARN)  # still easy to find, as a warning
    if _include_content and kind != 'cancelled':
        span.record_exception(error, escaped=True)


_WARN = 13
_CANCELLED = (asyncio.CancelledError, DBOSWorkflowCancelledError, DBOSAwaitedWorkflowCancelledError)


def kind_of(error: BaseException) -> Literal['error', 'expected', 'cancelled']:
    """`cancelled`: the user stopped the run. `expected`: an answer the caller handles, not a failure of ours: a page
    that did not load or a target that is not there (`ActionFailed`, `TargetNotFound`), what the browser service says
    by design (no browser to watch or close, a hand-off already given back, the user's browser busy), an engine's
    `NotSupported`, and errors in the agent's own code (`MontyError`), which the agent is shown. `error`: the rest,
    a `LifecycleError` (calling a backend out of order) included."""
    if isinstance(error, _CANCELLED):
        return 'cancelled'
    if isinstance(error, MontyError) or (isinstance(error, BrowserError) and not isinstance(error, LifecycleError)):
        return 'expected'
    return 'error'


@contextmanager
def timing(name: str, *, only_in_trace: bool = False) -> Generator[Span, None, None]:
    """`name` must be a literal operation label.

    `only_in_trace` is for what happens all the time, such as the database calls behind the apps' polling: it is
    recorded inside a trace (a run, a client's traced action) and never starts one of its own.
    """
    if only_in_trace and not trace.get_current_span().get_span_context().is_valid:
        yield trace.INVALID_SPAN
        return
    tracer = trace.get_tracer('sammy.timings')
    with tracer.start_as_current_span(name, record_exception=False, set_status_on_exception=False) as span:
        try:
            yield span
        except BaseException as error:
            record_error(span, error)
            raise


def timed(
    name: str, *, only_in_trace: bool = False
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, CoroutineType[Any, Any, T]]]:
    def decorate(function: Callable[P, Awaitable[T]]) -> Callable[P, CoroutineType[Any, Any, T]]:
        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
            with timing(name, only_in_trace=only_in_trace):
                return await function(*args, **kwargs)

        return wrapped

    return decorate


_TRACE_CONTEXT = TraceContextTextMapPropagator()
_TRACE_HEADERS = (b'traceparent', b'tracestate')


class ClientTraceContext:
    """Continue a client's trace (W3C `traceparent`) for one request, without a span of its own.

    The request's database spans, and a run it starts, join the client's trace. Requests without the header are
    untraced as before. Only trace context is read: client baggage would set span attributes. An unsampled one is
    ignored: following it would let a client switch off the server's own spans for its request and run.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        carrier = {
            name.decode('latin-1'): value.decode('latin-1')
            for name, value in scope.get('headers', ())
            if name in _TRACE_HEADERS
        }
        client = _TRACE_CONTEXT.extract(carrier) if 'traceparent' in carrier else None
        sampled = client is not None and trace.get_current_span(client).get_span_context().trace_flags.sampled
        if scope['type'] not in ('http', 'websocket') or client is None or not sampled:
            await self.app(scope, receive, send)
            return
        token = context.attach(client)
        try:
            await self.app(scope, receive, send)
        finally:
            context.detach(token)
