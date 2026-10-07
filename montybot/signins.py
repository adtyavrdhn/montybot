"""Saved sign-ins in Postgres: `PostgresJar` and `PostgresLease`, the real `SignInJar` and `JarLease` (#4) behind the
browser service.

- **Encrypted per user** (`montybot.crypto`): the state is sealed with the user's data key, which is sealed with the
  deployment's key. A saved state is a set of live credentials, so it is never logged and never leaves this module
  except to the browser service.
- **Versioned**: each save bumps `version`; the row holds the latest.
- **One writer at a time**: a run holds the user's lease from its browser's start until it closes. The lease expires
  (`LEASE_SECONDS`) so a run that never ended cannot lock the user out forever; a live run renews it on every browser
  call and before every wait (`montybot.browsing.Session`, `montybot.approvals.save_browser`), and a wait is shorter.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from cryptography.exceptions import InvalidTag

from montybot import crypto
from montybot.browser.contract import ActionFailed
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
        return crypto.open_sealed(deployment_key, bytes(row['data_key']), label=f'{user_id}:data_key')
    key = crypto.deployment_key(crypto.new_key())
    sealed = crypto.seal(deployment_key, key, label=f'{user_id}:data_key')
    await connection.execute('UPDATE montybot.users SET data_key = %s WHERE id = %s', (sealed, user_id))
    return key


def state_label(user_id: str, version: int) -> str:
    """Associated data for a saved state: an older version of the same user's row does not open as the current one."""
    return f'{user_id}:sign_ins:{version}'


class UnreadableSignIns(ActionFailed):
    """The saved sign-ins do not decrypt: the deployment key changed, or the row was altered."""


async def load_state(connection: Connection, deployment_key: bytes, user_id: str) -> BrowserState | None:
    cursor = await connection.execute('SELECT state, version FROM montybot.sign_ins WHERE user_id = %s', (user_id,))
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
    cursor = await connection.execute('SELECT version FROM montybot.sign_ins WHERE user_id = %s', (user_id,))
    row = await cursor.fetchone()
    version = 1 if row is None else row['version'] + 1
    sealed = crypto.seal(key, json.dumps(state.to_json()).encode(), label=state_label(user_id, version))
    await connection.execute(
        'INSERT INTO montybot.sign_ins (user_id, version, state) VALUES (%s, %s, %s) '
        'ON CONFLICT (user_id) DO UPDATE SET version = EXCLUDED.version, state = EXCLUDED.state, updated_at = now()',
        (user_id, version, sealed),
    )
    return version


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

    async def renew(self, *, user_id: UserId, run_id: RunId) -> None:
        """Extend the run's lease if it still holds it. Never takes a free lease: a run that was stopped meanwhile
        must not lock the user's browser again."""
        async with self._pool.connection() as connection:
            await connection.execute(
                'UPDATE montybot.jar_leases SET expires_at = now() + %s WHERE user_id = %s AND run_id = %s',
                (self._ttl, user_id, run_id),
            )

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
