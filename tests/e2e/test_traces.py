"""#4: sign-ins, cookies, hand-off ids, typed passwords and page contents never reach a trace.

The app runs in this process (on a thread) with the observability setup `montybot serve` uses, plus an in-memory
exporter, through a sign-in hand-off and an approved order. Then every span's name, attributes and events are searched
for the secrets.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import psycopg
import pytest
import uvicorn
from conftest import Client, Human
from helpers import eventually, free_port
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr
from sites.shop import Shop

from montybot.app import create_app
from montybot.observability import configure_observability
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
        'typed password': 'hunter2',
        'account password': 'my own password 123',
        'session cookie': sid,
        'hand-off id': row[0],
        'page text': 'In cart: eggs',
        'page text (order)': 'Order #1',
        "the user's message": prompt,
    }

    spans = exporter.get_finished_spans()
    assert any('agent run' in span.name or 'invoke_agent' in span.name for span in spans), [s.name for s in spans]
    dumped = '\n'.join(
        f'{span.name} {dict(span.attributes or {})} {[(e.name, dict(e.attributes or {})) for e in span.events]}'
        for span in spans
    )
    found = [what for what, secret in secrets.items() if secret in dumped]
    assert found == []
