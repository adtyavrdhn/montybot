"""What each user spends on the model, and the caps on it (#136).

```
agent.run(...)                       each model request is a DBOS step (DBOSDurability)
  RecordUsage.after_model_request    a row in sammy.usage: the user, run and model, the tokens (cache reads and
                                     writes too) and the response's `usage.cost`, which Pydantic AI prices with
                                     genai-prices before this hook, the same number Logfire shows as `operation.cost`
step run.start  (sammy.workflows)    at a cap: the run is refused before any model call, and a schedule pauses
step run.finish                      a reply that took the user past `spend_warning` of a cap says so
```

The hook runs in workflow code, around the model's step, so a replay runs it again: a row is per (run, request), and
a second insert does nothing. Days and months are the user's own (their time zone). The caps come from settings
(`DAILY_SPEND_CAP`, `MONTHLY_SPEND_CAP`); a model genai-prices does not know records tokens without a cost.
A run that is already going when the user reaches a cap finishes (its own `UsageLimits` still bound it).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import ModelRequestContext

from sammy.db import Connection, Pool
from sammy.deps import RunDeps
from sammy.models import Run
from sammy.observability import timed
from sammy.settings import Settings

ZERO = Decimal(0)


@dataclass(frozen=True, kw_only=True)
class Spent:
    """What a user spent today and this month, in US dollars."""

    today: Decimal
    month: Decimal
    by_run: Decimal = ZERO
    """What the run asked about spent of it."""


@dataclass(frozen=True, kw_only=True)
class Spender:
    """One chat's (or schedule's) share of the month. `id` and `name` are None for chats since deleted."""

    id: str | None
    name: str | None
    cost: Decimal


@dataclass(frozen=True, kw_only=True)
class Month:
    """The user's month so far: what it cost, its tokens, and the cost by chat and by schedule, most first."""

    spent: Spent
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    threads: list[Spender]
    schedules: list[Spender]


# --- recording ---


@dataclass
class RecordUsage(AbstractCapability[RunDeps]):
    """Records every model request of a run (`record`)."""

    async def after_model_request(
        self, ctx: RunContext[RunDeps], *, request_context: ModelRequestContext, response: ModelResponse
    ) -> ModelResponse:
        await record(ctx.deps.resources.pool, ctx.deps.run, ctx.run_step, response)
        return response


@timed('usage.record')
async def record(pool: Pool, run: Run, request: int, response: ModelResponse) -> None:
    """Idempotent: the run's `request`-th model request is recorded once, however often a replay gets here."""
    usage = response.usage
    async with pool.connection() as connection:
        await connection.execute(
            'INSERT INTO sammy.usage (user_id, run_id, request, model, input_tokens, output_tokens, '
            'cache_read_tokens, cache_write_tokens, cost) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) '
            'ON CONFLICT (run_id, request) DO NOTHING',
            (
                run.user_id,
                run.id,
                request,
                response.model_name or 'unknown',
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
                usage.cost,
            ),
        )


# --- reading ---

# The start of the user's day and month, in their time zone.
_DAY = "date_trunc('day', now() AT TIME ZONE us.timezone) AT TIME ZONE us.timezone"
_MONTH = "date_trunc('month', now() AT TIME ZONE us.timezone) AT TIME ZONE us.timezone"


async def spent(connection: Connection, user_id: str, run_id: str | None = None) -> Spent:
    """What the user spent today and this month, and of that, what `run_id` spent."""
    cursor = await connection.execute(
        f'SELECT coalesce(sum(u.cost) FILTER (WHERE u.created_at >= {_DAY}), 0) AS today, '
        'coalesce(sum(u.cost), 0) AS month, coalesce(sum(u.cost) FILTER (WHERE u.run_id = %s), 0) AS by_run '
        f'FROM sammy.users us JOIN sammy.usage u ON u.user_id = us.id AND u.created_at >= {_MONTH} '
        'WHERE us.id = %s',
        (run_id, user_id),
    )
    row = await cursor.fetchone()
    assert row is not None  # an aggregate without GROUP BY always has a row
    return Spent(today=row['today'], month=row['month'], by_run=row['by_run'])


async def month(connection: Connection, user_id: str) -> Month:
    """The user's month so far, by chat and by schedule. A schedule's chat is a chat too; its schedule's share is what
    its occurrences spent, not what the user's own messages in it did."""
    cursor = await connection.execute(
        'SELECT r.thread_id, t.title, s.id AS schedule_id, s.name AS schedule_name, '
        'coalesce(sum(u.cost), 0) AS cost, '
        "coalesce(sum(u.cost) FILTER (WHERE r.trigger = 'schedule'), 0) AS scheduled, "
        'sum(u.input_tokens) AS input_tokens, sum(u.output_tokens) AS output_tokens, '
        'sum(u.cache_read_tokens) AS cache_read_tokens '
        'FROM sammy.usage u JOIN sammy.users us ON us.id = u.user_id '
        'LEFT JOIN sammy.runs r ON r.id = u.run_id LEFT JOIN sammy.threads t ON t.id = r.thread_id '
        'LEFT JOIN sammy.schedules s ON s.thread_id = r.thread_id '
        f'WHERE u.user_id = %s AND u.created_at >= {_MONTH} '
        'GROUP BY r.thread_id, t.title, s.id, s.name ORDER BY cost DESC',
        (user_id,),
    )
    rows = await cursor.fetchall()
    threads = [
        Spender(id=str(row['thread_id']) if row['thread_id'] else None, name=row['title'], cost=row['cost'])
        for row in rows
    ]
    by_schedule = [
        Spender(id=str(row['schedule_id']), name=row['schedule_name'], cost=row['scheduled'])
        for row in rows
        if row['schedule_id'] is not None and row['scheduled'] > 0
    ]
    return Month(
        spent=await spent(connection, user_id),
        input_tokens=sum(int(row['input_tokens']) for row in rows),
        output_tokens=sum(int(row['output_tokens']) for row in rows),
        cache_read_tokens=sum(int(row['cache_read_tokens']) for row in rows),
        threads=threads,
        schedules=sorted(by_schedule, key=lambda s: s.cost, reverse=True),
    )


# --- caps ---


@dataclass(frozen=True, kw_only=True)
class Cap:
    name: str
    """`daily` or `monthly`."""
    until: str
    """When it lifts, in words."""
    limit: Decimal
    used: Decimal


def caps(settings: Settings, spent: Spent) -> Iterator[Cap]:
    """The caps that are set, with what the user has used of each."""
    if settings.daily_spend_cap is not None:
        yield Cap(name='daily', until='tomorrow', limit=settings.daily_spend_cap, used=spent.today)
    if settings.monthly_spend_cap is not None:
        yield Cap(name='monthly', until='next month', limit=settings.monthly_spend_cap, used=spent.month)


def dollars(amount: Decimal) -> str:
    return f'${amount:,.2f}'


def refusal(settings: Settings, spent: Spent, *, scheduled: bool) -> str:
    """Why a new run may not start, in words for the chat; empty if it may."""
    for cap in caps(settings, spent):
        if cap.used >= cap.limit:
            text = (
                f"You've reached your {cap.name} spending limit of {dollars(cap.limit)}, so I can't start anything "
                f'new until {cap.until}.'
            )
            if scheduled:
                text += ' I paused this schedule: resume it from Schedules when you want it back.'
            return text
    return ''


def warning(settings: Settings, spent: Spent) -> str:
    """A note for the end of a reply whose run took the user past `spend_warning` of a cap; empty otherwise."""
    for cap in caps(settings, spent):
        before = cap.used - spent.by_run
        if before < cap.limit <= cap.used:
            return (
                f"\n\nYou've now reached your {cap.name} spending limit of {dollars(cap.limit)}. I won't start new "
                f'tasks until {cap.until}, and your schedules pause when they next run.'
            )
        line = cap.limit * Decimal(str(settings.spend_warning))
        if before < line <= cap.used:
            return (
                f'\n\nHeads up: you have used {dollars(cap.used)} of your {cap.name} spending limit of '
                f"{dollars(cap.limit)}. At the limit I won't start new tasks until {cap.until}, and your schedules "
                'pause.'
            )
    return ''
