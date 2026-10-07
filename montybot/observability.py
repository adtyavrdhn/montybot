"""Logfire tracing: what is exported, and what never is.

Always exported: span names, model/provider/tool names, token usage and cost, the conversation's shape, the deploy's
commit and environment, the run/thread/user ids (random UUIDs), the site
(host) a browser step visits, exception types, metrics and system metrics.

Exported only with `LOGFIRE_INCLUDE_CONTENT` (on by default for the demo): messages, replies, instructions (which
include the user's memories), the code the agent writes, page snapshots, and exception messages and tracebacks.

Never exported, whatever the settings: cookies and browser state, saved sign-ins, passwords typed in live view,
session cookies, app secrets and API keys, hand-off ids and links, and push subscription URLs. None of these reach
the agent. HTTP server requests are not traced.
`tests/e2e/test_traces.py` holds these lines.
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
from opentelemetry import trace
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.trace import Span, StatusCode
from pydantic_monty import MontyError

from montybot.browser.contract import BrowserError, LifecycleError
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
