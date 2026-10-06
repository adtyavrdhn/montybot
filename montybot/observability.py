"""Content-free timings. Do not enable HTTP/SQL auto-instrumentation or argument capture.

Pydantic AI's content flag does not cover exception text, span names or arbitrary model metadata.
The OTel adapter below admits only numeric usage counters and fixed operation names, before Logfire sees them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from functools import wraps
from types import CoroutineType
from typing import Any, ParamSpec, TypeVar

import logfire
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Span as SDKSpan
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.trace import Link, Span, SpanContext, SpanKind, Status, StatusCode, Tracer, TracerProvider

# Match the OTel Tracer context manager, which also supports decorating async functions.
from opentelemetry.util._decorator import _agnosticcontextmanager  # pyright: ignore[reportPrivateUsage]
from opentelemetry.util.types import Attributes, AttributeValue
from starlette.types import ASGIApp, Receive, Scope, Send

from montybot.settings import Settings

P = ParamSpec('P')
T = TypeVar('T')


def configure_observability(settings: Settings, *, span_processors: Sequence[SpanProcessor] = ()) -> None:
    """`span_processors` is for tests, which read every span the app makes."""
    logfire.configure(
        service_name=settings.service_name,
        token=settings.logfire_token.get_secret_value() if settings.logfire_token else None,
        send_to_logfire='if-token-present',
        console=False,
        inspect_arguments=False,
        add_baggage_to_attributes=False,
        additional_span_processors=[FixedResource(), *span_processors],
        advanced=logfire.AdvancedOptions(emit_configuration_span=False, resource_detectors=[]),
    )
    logfire.instrument_pydantic_ai(
        include_content=False,
        include_binary_content=False,
        include_model_request_parameters=False,
        tracer_provider=UsageOnlyProvider(trace.get_tracer_provider()),
        # Metrics carry model/server metadata. Version 5 emits spans, not content logs.
        meter_provider=NoOpMeterProvider(),
        version=5,
    )


class FixedResource(SpanProcessor):
    """Drop environment/detector resource metadata before any processor sees a span."""

    def on_start(self, span: SDKSpan, parent_context: Context | None = None) -> None:
        # SDK Span.resource is read-only. Source-verified SDK storage, guarded by export regression tests;
        # Logfire registers additional processors before its exporters (including pending spans).
        span._resource = Resource({'service.name': 'montybot'})  # pyright: ignore[reportPrivateUsage]


@contextmanager
def timing(name: str) -> Generator[Span, None, None]:
    """`name` must be a literal operation label. Exceptions are counted, never serialized."""
    tracer = trace.get_tracer('montybot.timings')
    with tracer.start_as_current_span(name, record_exception=False, set_status_on_exception=False) as span:
        try:
            yield span
        except BaseException:
            span.set_status(StatusCode.ERROR)
            raise


def timed(name: str) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, CoroutineType[Any, Any, T]]]:
    def decorate(function: Callable[P, Awaitable[T]]) -> Callable[P, CoroutineType[Any, Any, T]]:
        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
            with timing(name):
                return await function(*args, **kwargs)

        return wrapped

    return decorate


class HTTPtimings:
    """HTTP wall time and status only, including mounted live-view requests. No path, headers or body."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        with timing('http.server') as span:

            async def sent(message: Any) -> None:
                if message['type'] == 'http.response.start':
                    span.set_attribute('http.response.status_code', message['status'])
                await send(message)

            await self.app(scope, receive, sent)


# Deliberately no strings, raw usage JSON, tool call ids, model names, URLs, schemas or events.
REQUEST_USAGE = frozenset(
    {
        'gen_ai.usage.input_tokens',
        'gen_ai.usage.output_tokens',
        'gen_ai.usage.cache_read.input_tokens',
        'gen_ai.usage.cache_creation.input_tokens',
        'gen_ai.usage.cache_read_input_tokens',
        'gen_ai.usage.cache_creation_input_tokens',
        'gen_ai.usage.input_tokens.cache_read',
        'gen_ai.usage.input_tokens.cache_write',
        'gen_ai.usage.total_tokens',
    }
)


# Agent-run totals use this separate namespace to avoid counting model requests twice.
USAGE = REQUEST_USAGE | frozenset(key.replace('gen_ai.usage.', 'gen_ai.aggregated_usage.') for key in REQUEST_USAGE)


def usage(attributes: Attributes) -> dict[str, AttributeValue]:
    return {
        key: value
        for key, value in (attributes or {}).items()
        if key in USAGE and isinstance(value, (int, float)) and not isinstance(value, bool)
    }


class UsageOnlySpan(Span):
    def __init__(self, span: Span) -> None:
        self.span = span

    def end(self, end_time: int | None = None) -> None:
        self.span.end(end_time)

    def get_span_context(self) -> SpanContext:
        return self.span.get_span_context()

    def is_recording(self) -> bool:
        return self.span.is_recording()

    def set_attribute(self, key: str, value: AttributeValue) -> None:
        self.set_attributes({key: value})

    def set_attributes(self, attributes: Mapping[str, AttributeValue]) -> None:
        self.span.set_attributes(usage(attributes))

    def add_event(self, name: str, attributes: Attributes = None, timestamp: int | None = None) -> None:
        pass

    def update_name(self, name: str) -> None:
        pass

    def set_status(self, status: Status | StatusCode, description: str | None = None) -> None:
        self.span.set_status(status.status_code if isinstance(status, Status) else status)

    def record_exception(
        self,
        exception: BaseException,
        attributes: Attributes = None,
        timestamp: int | None = None,
        escaped: bool = False,
    ) -> None:
        self.span.set_status(StatusCode.ERROR)

    def add_link(self, context: SpanContext, attributes: Attributes = None) -> None:
        pass


class UsageOnlyTracer(Tracer):
    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer

    def start_span(
        self,
        name: str,
        context: Context | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
    ) -> Span:
        if name.startswith(('chat ', 'generate ', 'text_completion ')):
            operation = 'model.request'
        elif name.startswith(('invoke_agent', 'agent run')):
            operation = 'invoke_agent'
        elif name.startswith(('execute_tool', 'running tool', 'running output')):
            operation = 'tool.execute'
        else:
            operation = 'agent.operation'
        return UsageOnlySpan(
            self.tracer.start_span(
                operation,
                context=context,
                kind=kind,
                attributes=usage(attributes),
                start_time=start_time,
                record_exception=False,
                set_status_on_exception=False,
            )
        )

    @_agnosticcontextmanager
    def start_as_current_span(
        self,
        name: str,
        context: Context | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
        end_on_exit: bool = True,
    ) -> Generator[Span, None, None]:
        span = self.start_span(name, context, kind, attributes, links, start_time)
        with trace.use_span(span, end_on_exit=end_on_exit, record_exception=False, set_status_on_exception=False):
            try:
                yield span
            except BaseException:
                span.set_status(StatusCode.ERROR)
                raise


class UsageOnlyProvider(TracerProvider):
    def __init__(self, provider: TracerProvider) -> None:
        self.provider = provider

    def get_tracer(
        self,
        instrumenting_module_name: str,
        instrumenting_library_version: str | None = None,
        schema_url: str | None = None,
        attributes: Attributes = None,
    ) -> Tracer:
        # Even instrumentation scope attributes can contain arbitrary metadata.
        return UsageOnlyTracer(self.provider.get_tracer('montybot.agent'))
