"""Reminding the user about a run left waiting on them (#131).

```
DBOS schedule `sammy-reminders`, at each time `reminder_cron` names, on one replica
  remind_waiting(scheduled_at, None)                     @DBOS.workflow
    step reminders.send   every unanswered ask of a waiting run, asked `remind_after_seconds` ago or more and not
                          reminded yet: mark it reminded, then push and email "Sammy is still waiting for you"
```

Each ask (a question, an approval, a hand-off, a connect) is one waiting episode and gets one reminder at most: one
UPDATE both picks and marks it, so a sweep that overlaps another, or runs again, finds it marked. An answered ask is
never picked. At `ask_timeout_seconds` the run stops by itself (`sammy.approvals.Unanswered`, `sammy.workflows`).
"""

from __future__ import annotations

from datetime import datetime

import logfire
from dbos import DBOS

from sammy.notifications import remind
from sammy.observability import timing
from sammy.resources import Resources, current
from sammy.settings import Settings

SCHEDULE = 'sammy-reminders'
"""The sweep's DBOS schedule; not `sammy.schedules.PREFIX`, so it is never one of a user's."""


async def schedule(settings: Settings) -> None:
    """Create the sweep's schedule, or update its cron. Safe on every start of every replica."""
    await DBOS.apply_schedules_async(
        [{'schedule_name': SCHEDULE, 'workflow_fn': remind_waiting, 'schedule': settings.reminder_cron}]
    )


@DBOS.workflow(name='sammy.remind_waiting')
async def remind_waiting(scheduled_at: datetime, context: object) -> None:
    await DBOS.run_step_async({'name': 'reminders.send'}, send_reminders, current())


async def send_reminders(resources: Resources) -> int:
    """Remind the user of each ask that has waited long enough. Returns how many reminders were sent."""
    with timing('run.remind', only_in_trace=True):  # every few minutes: no trace of its own
        async with resources.pool.connection() as connection:
            cursor = await connection.execute(
                'UPDATE sammy.asks a SET reminded_at = now() FROM sammy.runs r '
                "WHERE r.id = a.run_id AND r.status = 'waiting' AND a.answer IS NULL AND a.reminded_at IS NULL "
                'AND a.created_at <= now() - make_interval(secs => %s) RETURNING a.id, a.user_id, a.kind, r.thread_id',
                (resources.settings.remind_after_seconds,),
            )
            due = await cursor.fetchall()
        for row in due:
            # A tag of its own, so the device alerts again rather than quietly replacing the first notification.
            tag = f'reminder-{row["id"]}'
            thread_id = str(row['thread_id'])
            await remind(resources, user_id=str(row['user_id']), thread_id=thread_id, kind=row['kind'], tag=tag)
        if due:
            logfire.info('Sent {count} reminders about waiting runs', count=len(due))
        return len(due)
