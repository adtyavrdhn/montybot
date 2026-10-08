"""Saved sign-ins in Postgres: `PostgresJar` and `PostgresLease`, the real `SignInJar` and `JarLease` (#4) behind the
browser service.

- **Encrypted per user** (`sammy.crypto`): the state is sealed with the user's data key, which is sealed with the
  deployment's key. A saved state is a set of live credentials, so it is never logged and never leaves this module
  except to the browser service.
- **Versioned**: each save bumps `version`; the row holds the latest.
- **One browser at a time**: a run holds the user's lease from its browser's start until it closes. Runs whose tabs
  share one browser on this server hold it together (`shared`, the lease's `owner`). The lease expires
  (`LEASE_SECONDS`) so a run that never ended cannot lock the user out forever; a live run renews it on every browser
  call and before every wait (`sammy.browsing.Session`, `sammy.approvals.save_browser`), and a wait is shorter.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from cryptography.exceptions import InvalidTag

from sammy import crypto
from sammy.browser.contract import ActionFailed
from sammy.browser.service import RunId, UserId
from sammy.browser.state import BrowserState
from sammy.db import Connection, Pool

LEASE_SECONDS = 26 * 60 * 60


async def user_key(connection: Connection, deployment_key: bytes, user_id: str) -> bytes:
    """The user's data key, made and stored (sealed) on first use. Call inside a transaction."""
    cursor = await connection.execute('SELECT data_key FROM sammy.users WHERE id = %s FOR UPDATE', (user_id,))
    row = await cursor.fetchone()
    if row is None:
        raise LookupError('no such user')
    if row['data_key'] is not None:
        return crypto.open_sealed(deployment_key, bytes(row['data_key']), label=f'{user_id}:data_key')
    key = crypto.deployment_key(crypto.new_key())
    sealed = crypto.seal(deployment_key, key, label=f'{user_id}:data_key')
    await connection.execute('UPDATE sammy.users SET data_key = %s WHERE id = %s', (sealed, user_id))
    return key


def state_label(user_id: str, version: int) -> str:
    """Associated data for a saved state: an older version of the same user's row does not open as the current one."""
    return f'{user_id}:sign_ins:{version}'


class UnreadableSignIns(ActionFailed):
    """The saved sign-ins do not decrypt: the deployment key changed, or the row was altered."""


async def load_state(connection: Connection, deployment_key: bytes, user_id: str) -> BrowserState | None:
    cursor = await connection.execute('SELECT state, version FROM sammy.sign_ins WHERE user_id = %s', (user_id,))
    row = await cursor.fetchone()
    if row is None:
        return None
    try:
        key = await user_key(connection, deployment_key, user_id)
        plain = crypto.open_sealed(key, bytes(row['state']), label=state_label(user_id, row['version']))
    except (InvalidTag, ValueError) as error:
        raise UnreadableSignIns('the saved sign-ins could not be read; the user needs to sign in again') from error
    data: Any = json.loads(plain)
    return BrowserState.from_json(data)


async def save_state(connection: Connection, deployment_key: bytes, user_id: str, state: BrowserState) -> int:
    """Store `state` as the user's latest. Returns the new version. Call inside a transaction."""
    key = await user_key(connection, deployment_key, user_id)  # locks the user's row, so saves take turns
    cursor = await connection.execute('SELECT version FROM sammy.sign_ins WHERE user_id = %s', (user_id,))
    row = await cursor.fetchone()
    version = 1 if row is None else row['version'] + 1
    sealed = crypto.seal(key, json.dumps(state.to_json()).encode(), label=state_label(user_id, version))
    await connection.execute(
        'INSERT INTO sammy.sign_ins (user_id, version, state) VALUES (%s, %s, %s) '
        'ON CONFLICT (user_id) DO UPDATE SET version = EXCLUDED.version, state = EXCLUDED.state, updated_at = now()',
        (user_id, version, sealed),
    )
    return version


class PostgresJar:
    """`SignInJar` in `sammy.sign_ins`, encrypted per user."""

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
    """`JarLease` in `sammy.jar_leases`, one row per holding run. An expired row counts as free. `owner` is the
    server (its `EXECUTOR_ID`): runs share the lease only on the same one, where they share one browser."""

    def __init__(self, pool: Pool, seconds: float = LEASE_SECONDS, *, owner: str = '') -> None:
        self._pool = pool
        self._ttl = timedelta(seconds=seconds)
        self._owner = owner

    async def acquire(self, *, user_id: UserId, run_id: RunId, shared: bool = False) -> bool:
        async with self._pool.connection() as connection, connection.transaction():
            # One acquire per user at a time, so two runs cannot both see the other's row missing.
            await connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))', (user_id,))
            await connection.execute(
                'DELETE FROM sammy.jar_leases WHERE user_id = %s AND expires_at < now()', (user_id,)
            )
            cursor = await connection.execute(
                'SELECT 1 FROM sammy.jar_leases WHERE user_id = %s AND run_id <> %s '
                'AND NOT (%s AND shared AND owner = %s) LIMIT 1',
                (user_id, run_id, shared, self._owner),
            )
            if await cursor.fetchone() is not None:
                return False
            await connection.execute(
                'INSERT INTO sammy.jar_leases (user_id, run_id, expires_at, shared, owner) '
                'VALUES (%s, %s, now() + %s, %s, %s) ON CONFLICT (user_id, run_id) '
                'DO UPDATE SET expires_at = EXCLUDED.expires_at, shared = EXCLUDED.shared, owner = EXCLUDED.owner',
                (user_id, run_id, self._ttl, shared, self._owner),
            )
            return True

    async def renew(self, *, user_id: UserId, run_id: RunId) -> None:
        """Extend the run's lease if it still holds it. Never takes a free lease: a run that was stopped meanwhile
        must not lock the user's browser again."""
        async with self._pool.connection() as connection:
            await connection.execute(
                'UPDATE sammy.jar_leases SET expires_at = now() + %s WHERE user_id = %s AND run_id = %s',
                (self._ttl, user_id, run_id),
            )

    async def holds(self, *, user_id: UserId, run_id: RunId) -> bool:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                'SELECT 1 FROM sammy.jar_leases WHERE user_id = %s AND run_id = %s AND expires_at >= now()',
                (user_id, run_id),
            )
            return await cursor.fetchone() is not None

    async def release(self, *, user_id: UserId, run_id: RunId) -> None:
        async with self._pool.connection() as connection:
            await connection.execute(
                'DELETE FROM sammy.jar_leases WHERE user_id = %s AND run_id = %s', (user_id, run_id)
            )
