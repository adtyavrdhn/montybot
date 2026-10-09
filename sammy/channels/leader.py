"""Platform connections that must be open on one replica only (Discord's gateway), and the Postgres lock that picks it.

```
Listeners (in the app, next to the outbox Pump)
  each enabled platform that is a `base.Listener`: lead('sammy:channel:<name>', listen(inbound.receive for it))
lead(key, work)
  a connection of its own (not the pool's): pg_try_advisory_lock(hashtextextended(key, 0))
  got it:     run work, checking the connection every CHECK_SECONDS; if it is lost, work is cancelled
  did not:    close, wait RETRY_SECONDS, try again (a follower takes over within that once the leader is gone)
```

An advisory lock rather than a lease row (`signins.PostgresLease`): Postgres frees a session's lock the moment its
connection ends, so a replica that crashes or is killed frees it at once, with no expiry to tune and no renewals to
miss. The cost: it needs a real session to Postgres (a transaction-pooling PgBouncer would not hold it). If the
leader's connection breaks without Postgres noticing yet, nobody leads until it does; if Postgres frees the lock
first, two replicas can be connected for up to CHECK_SECONDS, which is harmless as every delivery is handled once
by its id (`inbound.receive`).
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable, Coroutine
from typing import Self

from psycopg import AsyncConnection

from sammy.channels import inbound
from sammy.channels.base import Listener
from sammy.resources import Resources

RETRY_SECONDS = 5.0
CHECK_SECONDS = 10.0

logger = logging.getLogger(__name__)

Work = Callable[[], Coroutine[object, object, None]]


async def lead(
    database_url: str, key: str, work: Work, *, retry: float = RETRY_SECONDS, check: float = CHECK_SECONDS
) -> None:
    """Run `work` only while this replica holds the lock `key`. Runs until cancelled: work that ends or fails gives
    the lock up and starts again after `retry`, on whichever replica takes the lock."""
    while True:
        try:
            async with await AsyncConnection.connect(database_url, autocommit=True) as connection:
                if await _try_lock(connection, key):
                    logger.info('Leading %s', key)
                    await _while_held(connection, work, check)
        except Exception as error:  # noqa: BLE001  the next round tries again
            logger.warning('Leading %s stopped: %s', key, type(error).__qualname__)
        await asyncio.sleep(retry)


async def _try_lock(connection: AsyncConnection, key: str) -> bool:
    cursor = await connection.execute('SELECT pg_try_advisory_lock(hashtextextended(%s::text, 0))', (key,))
    row = await cursor.fetchone()
    return row is not None and row[0] is True


async def _while_held(connection: AsyncConnection, work: Work, check: float) -> None:
    task = asyncio.create_task(work())
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=check)
            if done:
                task.result()
                return
            # The lock lives as long as this connection: if it is gone, so may the lock be.
            await asyncio.wait_for(connection.execute('SELECT 1'), timeout=check)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class Listeners:
    """Runs each enabled `Listener` platform's connection, on whichever replica leads it, while the app runs."""

    def __init__(self, resources: Resources, *, retry: float = RETRY_SECONDS) -> None:
        self._resources = resources
        self._retry = retry
        self._tasks: list[asyncio.Task[None]] = []

    async def __aenter__(self) -> Self:
        for name in self._resources.channels.names:
            channel = self._resources.channels.get(name)
            if isinstance(channel, Listener):
                listen = functools.partial(channel.listen, functools.partial(inbound.receive, name))
                key = f'sammy:channel:{name}'
                made = lead(self._resources.settings.database_url, key, listen, retry=self._retry)
                self._tasks.append(asyncio.create_task(made))
        return self

    async def __aexit__(self, *_: object) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
