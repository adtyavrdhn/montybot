"""Saved sign-ins in Postgres: `PostgresJar` and `PostgresLease`, the real `SignInJar` and `JarLease` (#4) behind the
browser service.

- **Encrypted per user** (`montybot.crypto`): the state is sealed with the user's data key, which is sealed with the
  deployment's key. A saved state is a set of live credentials, so it is never logged and never leaves this module
  except to the browser service.
- **Versioned**: each save bumps `version`; the row holds the latest.
- **One writer at a time**: a run holds the user's lease from its browser's start until it closes. The lease expires
  (`LEASE_SECONDS`, longer than a run waits for the user) so a run that never ended cannot lock the user out forever.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from montybot import crypto
from montybot.browser.service import RunId, UserId
from montybot.browser.state import BrowserState
from montybot.db import Connection, Pool

LEASE_SECONDS = 26 * 60 * 60


async def user_key(connection: Connection, deployment_key: bytes, user_id: str) -> bytes:
    """The user's data key, made and stored (sealed) on first use. Call inside a transaction."""
    cursor = await connection.execute('SELECT data_key FROM montybot.users WHERE id = %s FOR UPDATE', (user_id,))
    row = await cursor.fetchone()
    if row is None:
        raise LookupError('no such user')
    if row['data_key'] is not None:
        return crypto.open_sealed(deployment_key, bytes(row['data_key']), user_id=user_id)
    key = crypto.deployment_key(crypto.new_key())
    sealed = crypto.seal(deployment_key, key, user_id=user_id)
    await connection.execute('UPDATE montybot.users SET data_key = %s WHERE id = %s', (sealed, user_id))
    return key


async def load_state(connection: Connection, deployment_key: bytes, user_id: str) -> BrowserState | None:
    cursor = await connection.execute('SELECT state FROM montybot.sign_ins WHERE user_id = %s', (user_id,))
    row = await cursor.fetchone()
    if row is None:
        return None
    key = await user_key(connection, deployment_key, user_id)
    data: Any = json.loads(crypto.open_sealed(key, bytes(row['state']), user_id=user_id))
    return BrowserState.from_json(data)


async def save_state(connection: Connection, deployment_key: bytes, user_id: str, state: BrowserState) -> int:
    """Store `state` as the user's latest. Returns the new version. Call inside a transaction."""
    key = await user_key(connection, deployment_key, user_id)
    sealed = crypto.seal(key, json.dumps(state.to_json()).encode(), user_id=user_id)
    cursor = await connection.execute(
        'INSERT INTO montybot.sign_ins (user_id, version, state) VALUES (%s, 1, %s) '
        'ON CONFLICT (user_id) DO UPDATE SET version = montybot.sign_ins.version + 1, state = EXCLUDED.state, '
        'updated_at = now() RETURNING version',
        (user_id, sealed),
    )
    row = await cursor.fetchone()
    assert row is not None
    return row['version']


class PostgresJar:
    """`SignInJar` in `montybot.sign_ins`, encrypted per user."""

    def __init__(self, pool: Pool, deployment_key: bytes) -> None:
        self._pool = pool
        self._key = deployment_key

    async def load(self, *, user_id: UserId) -> BrowserState | None:
        async with self._pool.connection() as connection, connection.transaction():
            return await load_state(connection, self._key, user_id)

    async def save(self, *, user_id: UserId, state: BrowserState) -> None:
        async with self._pool.connection() as connection, connection.transaction():
            await save_state(connection, self._key, user_id, state)


class PostgresLease:
    """`JarLease` in `montybot.jar_leases`. An expired lease counts as free."""

    def __init__(self, pool: Pool, seconds: float = LEASE_SECONDS) -> None:
        self._pool = pool
        self._ttl = timedelta(seconds=seconds)

    async def acquire(self, *, user_id: UserId, run_id: RunId) -> bool:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                'INSERT INTO montybot.jar_leases (user_id, run_id, expires_at) VALUES (%s, %s, now() + %s) '
                'ON CONFLICT (user_id) DO UPDATE SET run_id = EXCLUDED.run_id, expires_at = EXCLUDED.expires_at '
                'WHERE montybot.jar_leases.run_id = EXCLUDED.run_id OR montybot.jar_leases.expires_at < now() '
                'RETURNING run_id',
                (user_id, run_id, self._ttl),
            )
            return await cursor.fetchone() is not None

    async def holder(self, *, user_id: UserId) -> RunId | None:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                'SELECT run_id FROM montybot.jar_leases WHERE user_id = %s AND expires_at >= now()', (user_id,)
            )
            row = await cursor.fetchone()
            return None if row is None else row['run_id']

    async def release(self, *, user_id: UserId, run_id: RunId) -> None:
        async with self._pool.connection() as connection:
            await connection.execute(
                'DELETE FROM montybot.jar_leases WHERE user_id = %s AND run_id = %s', (user_id, run_id)
            )
