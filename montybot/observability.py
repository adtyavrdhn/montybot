# Copied from viktor c1896df (viktor/observability.py). Pydantic AI spans leave out message and tool content, and
# httpx and psycopg are not instrumented: pages, typed text, cookies and hand-off ids must never reach a trace.
from __future__ import annotations

from collections.abc import Sequence

import logfire
from opentelemetry.sdk.trace import SpanProcessor

from montybot.settings import Settings


def configure_observability(settings: Settings, *, span_processors: Sequence[SpanProcessor] = ()) -> None:
    """`span_processors` is for tests, which read every span the app makes."""
    logfire.configure(
        service_name=settings.service_name,
        token=settings.logfire_token.get_secret_value() if settings.logfire_token else None,
        send_to_logfire='if-token-present',
        console=False,
        additional_span_processors=span_processors,
    )
    logfire.instrument_pydantic_ai(include_content=False, include_binary_content=False)
