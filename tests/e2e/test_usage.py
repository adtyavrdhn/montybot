"""#136: what each user spends on the model (`sammy/usage.py`), and the caps on it.

The app runs in this process with an in-memory span exporter (`test_traces.serve_traced`), so a run's recorded cost
can be held against what Logfire gets: the `operation.cost` of each model request span. The model is the e2e scripted
model under a priced name, with fixed token counts, cache reads included; it counts its calls, so a refused run can be
shown to make none.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from conftest import Client
from dbos import DBOSClient
from e2e import scripts
from helpers import eventually
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage
from test_traces import InProcessApp, serve_traced

from sammy import usage
from sammy.db import create_pool
from sammy.models import Run
from sammy.settings import Settings

MODEL = 'claude-sonnet-4-5'
calls: list[str] = []
"""The prompt of each model request the app in this process made."""


def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    response = scripts.respond(messages, info)
    calls.append(scripts.current_turn(messages).prompt)
    response.usage = RequestUsage(input_tokens=2000, cache_read_tokens=1500, cache_write_tokens=200, output_tokens=100)
    return response


priced = FunctionModel(respond, model_name=MODEL)


@dataclass
class Served:
    app: InProcessApp
    exporter: InMemorySpanExporter
    client: Client
    database_url: str

    def user_id(self) -> str:
        return self.client.http.get('/api/me').json()['id']

    def spend(self, dollars: str) -> None:
        """What the user spent earlier today, in a chat since deleted."""
        with psycopg.connect(self.database_url) as connection:
            connection.execute(
                'INSERT INTO sammy.usage (user_id, request, model, input_tokens, output_tokens, cache_read_tokens, '
                "cache_write_tokens, cost) VALUES (%s, 0, 'earlier', 0, 0, 0, 0, %s)",
                (self.user_id(), Decimal(dollars)),
            )

    def rows(self, run_id: str) -> list[tuple[int, str, int, int, int, int, Decimal]]:
        with psycopg.connect(self.database_url) as connection:
            return connection.execute(
                'SELECT request, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost '
                'FROM sammy.usage WHERE run_id = %s ORDER BY request',
                (run_id,),
            ).fetchall()

    def run_id(self, thread_id: str) -> str:
        return self.client.thread(thread_id)['run']['id']

    def model_spans(self, run_id: str) -> list[ReadableSpan]:
        # The run's spans are all exported once its top span is.
        eventually(
            lambda: any(span.name == 'run.lifecycle' for span in self.exporter.get_finished_spans()) or None,
            what='the run to finish cleanup',
        )
        return [
            span
            for span in self.exporter.get_finished_spans()
            if span.name == f'chat {MODEL}' and (span.attributes or {}).get('run_id') == run_id
        ]


def logfire_cost(span: ReadableSpan) -> float:
    """What Logfire shows a model request cost."""
    cost = (span.attributes or {})['operation.cost']
    assert isinstance(cost, float)
    return cost


@contextmanager
def served(database_url: str, workspaces_dir: Path, **caps: Decimal) -> Iterator[Served]:
    with serve_traced(database_url, workspaces_dir, model='script:test_usage:priced', **caps) as (app, exporter):
        client = Client(app)  # pyright: ignore[reportArgumentType]  # it needs only the app's url
        try:
            client.sign_up()
            calls.clear()
            yield Served(app, exporter, client, database_url)
        finally:
            client.http.close()


def test_recorded_cost_matches_logfire(database_url: str, workspaces_dir: Path) -> None:
    with served(database_url, workspaces_dir) as s:
        thread = s.client.ask('What are my schedules?')  # two model requests: the tool call, then the answer
        s.client.wait_for_reply(thread)
        run_id = s.run_id(thread)
        rows = s.rows(run_id)
        spans = s.model_spans(run_id)
        assert len(rows) == len(spans) == 2
        for (_, model, input_tokens, output_tokens, cache_read, cache_write, cost), span in zip(
            rows, spans, strict=True
        ):
            attributes = span.attributes or {}
            assert model == MODEL
            assert (input_tokens, output_tokens) == (2000, 100)
            assert (cache_read, cache_write) == (1500, 200)
            assert attributes['gen_ai.usage.input_tokens'] == input_tokens
            assert attributes['gen_ai.usage.cache_read.input_tokens'] == cache_read
            assert float(cost) == pytest.approx(logfire_cost(span), rel=1e-9)
        logfire_total = sum(logfire_cost(span) for span in spans)
        recorded_total = sum(row[6] for row in rows)
        assert recorded_total > 0
        assert float(recorded_total) == pytest.approx(logfire_total, rel=1e-9)

        # The usage view: this month, by chat; no schedule has spent anything.
        shown = s.client.http.get('/api/usage').json()
        assert shown['month'] == shown['today'] == pytest.approx(float(recorded_total))
        assert shown['daily_cap'] is None and shown['monthly_cap'] is None
        assert shown['tokens'] == {'input': 4000, 'output': 200, 'cache_read': 3000}
        assert shown['threads'] == [{'id': thread, 'name': 'What are my schedules?', 'cost': shown['month']}]
        assert shown['schedules'] == []

        # A workflow replaying its first model request records it once.
        run = Run(id=run_id, user_id=s.user_id(), thread_id=thread, trigger='message', prompt='', status='done')
        replayed = ModelResponse(parts=[TextPart('again')], model_name=MODEL, usage=RequestUsage(input_tokens=1))

        async def replay() -> None:
            async with create_pool(database_url) as pool:
                await usage.record(pool, run, rows[0][0], replayed)

        asyncio.run(replay())
        assert s.rows(run_id) == rows


def test_near_the_cap_a_reply_warns_and_at_the_cap_a_run_is_refused(database_url: str, workspaces_dir: Path) -> None:
    with served(database_url, workspaces_dir, daily_spend_cap=Decimal(1)) as s:
        s.spend('0.799')  # one request (about $0.004) takes the user past 80% of the cap
        thread = s.client.ask('Say hello')
        reply = s.client.wait_for_reply(thread)
        assert 'Heads up: you have used $0.80 of your daily spending limit of $1.00.' in reply
        s.client.ask('Say hello', thread)
        assert 'Heads up' not in s.client.wait_for_reply(thread)  # once, not on every reply after

        s.spend('0.2')
        made = len(calls)
        s.client.ask('Say hello again', thread)
        reply = s.client.wait_for_reply(thread, failed=True)
        assert (
            reply == "You've reached your daily spending limit of $1.00, so I can't start anything new until tomorrow."
        )
        run_id = s.run_id(thread)
        assert len(calls) == made, 'no model call'
        assert s.rows(run_id) == [] and s.model_spans(run_id) == []

        shown = s.client.http.get('/api/usage').json()
        assert shown['daily_cap'] == 1.0 and shown['today'] >= 1.0
        assert {'id': None, 'name': None, 'cost': pytest.approx(0.999)} in shown['threads']  # the deleted chat


def test_a_schedule_at_the_cap_pauses(database_url: str, workspaces_dir: Path) -> None:
    with served(database_url, workspaces_dir, monthly_spend_cap=Decimal(5)) as s:
        chat = s.client.ask('Tell me when a delivery slot opens at http://127.0.0.1:9')
        s.client.answer(s.client.wait_for_ask(chat, 'approval'), approved=True)
        assert 'Scheduled' in s.client.wait_for_reply(chat)
        (schedule,) = s.client.http.get('/api/schedules').json()
        assert not schedule['paused']

        s.spend('5')
        made = len(calls)
        dbos = DBOSClient(system_database_url=database_url)
        try:
            handle = dbos.trigger_schedule(f'sammy-schedule-{schedule["id"]}')
            eventually(lambda: handle.get_status().status == 'SUCCESS' or None, what='the occurrence to finish')
        finally:
            dbos.destroy()
        assert len(calls) == made, 'no model call'
        (schedule,) = s.client.http.get('/api/schedules').json()
        assert schedule['paused'] and schedule['last_status'] == 'failed'
        reply = s.client.thread(schedule['thread_id'])['messages'][-1]['text']
        assert reply == (
            "You've reached your monthly spending limit of $5.00, so I can't start anything new until next month. "
            'I paused this schedule: resume it from Schedules when you want it back.'
        )


# --- the caps, without a database ---


def settings(*, daily: Decimal | None = None, monthly: Decimal | None = None) -> Settings:
    """With these caps; with neither, the caps the environment sets."""
    required = Settings(
        database_url='postgresql://unused', session_secret=SecretStr('s'), encryption_key=SecretStr('k')
    )
    if daily is None and monthly is None:
        return required
    return required.model_copy(update={'daily_spend_cap': daily, 'monthly_spend_cap': monthly})


def test_caps_are_off_unless_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('DAILY_SPEND_CAP', '')
    monkeypatch.setenv('MONTHLY_SPEND_CAP', '25.50')
    configured = settings()
    assert configured.daily_spend_cap is None and configured.monthly_spend_cap == Decimal('25.50')
    monkeypatch.delenv('MONTHLY_SPEND_CAP')
    spent = usage.Spent(today=Decimal(1000), month=Decimal(1000))
    assert usage.refusal(settings(), spent, scheduled=True) == ''
    assert usage.warning(settings(), spent) == ''


def test_the_first_cap_reached_says_why() -> None:
    capped = settings(daily=Decimal(2), monthly=Decimal(30))
    assert usage.refusal(capped, usage.Spent(today=Decimal('1.99'), month=Decimal(29)), scheduled=False) == ''
    daily = usage.refusal(capped, usage.Spent(today=Decimal(2), month=Decimal(2)), scheduled=False)
    assert 'daily spending limit of $2.00' in daily and 'paused' not in daily
    monthly = usage.refusal(capped, usage.Spent(today=Decimal(0), month=Decimal(31)), scheduled=True)
    assert 'monthly spending limit of $30.00' in monthly and 'until next month' in monthly
    assert 'I paused this schedule' in monthly


def test_a_reply_warns_only_when_its_run_crossed_a_line() -> None:
    capped = settings(monthly=Decimal(10))
    crossed = usage.Spent(today=Decimal(1), month=Decimal('8.5'), by_run=Decimal(1))
    assert 'Heads up: you have used $8.50 of your monthly spending limit of $10.00.' in usage.warning(capped, crossed)
    already = usage.Spent(today=Decimal(1), month=Decimal('8.5'), by_run=Decimal('0.1'))
    assert usage.warning(capped, already) == ''
    reached = usage.Spent(today=Decimal(1), month=Decimal('10.2'), by_run=Decimal('0.5'))
    assert "You've now reached your monthly spending limit of $10.00." in usage.warning(capped, reached)
