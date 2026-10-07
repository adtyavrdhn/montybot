"""Logfire tracing: what is exported, and what never is.

Always exported: span names, model/provider/tool names, token usage and cost, the conversation's shape, the deploy's
commit and environment, the run/thread/user ids (random UUIDs), HTTP method, route template and status, the site
(host) a browser step visits, exception types, metrics and system metrics.

Exported only with `LOGFIRE_INCLUDE_CONTENT` (on by default for the demo): messages, replies, instructions (which
include the user's memories), the code the agent writes, page snapshots, and exception messages and tracebacks.

Never exported, whatever the settings: cookies and browser state, saved sign-ins, passwords typed in live view,
session cookies, app secrets and API keys, hand-off ids and links, and push subscription URLs. None of these reach
the agent, and HTTP spans carry the route template, never the raw path, query or headers.
`tests/e2e/test_traces.py` holds these lines.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Generator, Sequence
from contextlib import contextmanager
from functools import wraps
from types import CoroutineType
from typing import Any, ParamSpec, TypeVar

import logfire
from opentelemetry import trace
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.trace import Span, StatusCode
from starlette.types import ASGIApp, Receive, Scope, Send

from montybot.settings import Settings

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
    logfire.instrument_system_metrics()


def record_error(span: Span, error: BaseException) -> None:
    """The type always; the message and traceback, which can quote pages or rows, only with content."""
    span.set_status(StatusCode.ERROR)
    span.set_attribute('error.type', type(error).__qualname__)
    if _include_content:
        span.record_exception(error, escaped=True)


@contextmanager
def timing(name: str) -> Generator[Span, None, None]:
    """`name` must be a literal operation label."""
    tracer = trace.get_tracer('montybot.timings')
    with tracer.start_as_current_span(name, record_exception=False, set_status_on_exception=False) as span:
        try:
            yield span
        except BaseException as error:
            record_error(span, error)
            raise


def timed(name: str) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, CoroutineType[Any, Any, T]]]:
    def decorate(function: Callable[P, Awaitable[T]]) -> Callable[P, CoroutineType[Any, Any, T]]:
        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
            with timing(name):
                return await function(*args, **kwargs)

        return wrapped

    return decorate


def route_template(scope: Scope) -> str | None:
    """The matched route with its parameters as names, such as `/live/handoff/{handoff_id}`; None if none matched."""
    if 'endpoint' not in scope:
        return None
    path: str = scope['path']  # under a Mount too, Starlette keeps the full path here
    for name, value in scope.get('path_params', {}).items():
        path = path.replace(str(value), '{' + name + '}')
    return path


class HTTPtimings:
    """HTTP wall time, method, route template and status, including mounted live-view requests.

    Never the raw path (hand-off ids are in it), query, headers or body.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        with timing('http.server') as span:
            span.set_attribute('http.request.method', scope['method'])

            async def sent(message: Any) -> None:
                if message['type'] == 'http.response.start':
                    span.set_attribute('http.response.status_code', message['status'])
                await send(message)

            try:
                await self.app(scope, receive, sent)
            finally:
                if (route := route_template(scope)) is not None:
                    span.set_attribute('http.route', route)
