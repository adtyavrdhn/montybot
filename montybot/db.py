# Copied from viktor c1896df (viktor/db.py). The pgtask schema step is gone: DBOS creates its own tables on launch.
from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Self, cast

from psycopg import AsyncConnection, AsyncCursor
from psycopg.abc import Params, Query, QueryNoTemplate
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from montybot.observability import timed


class TimedCursor(AsyncCursor[DictRow]):
    """Time queries without exporting SQL, parameters, results or exception text."""

    @timed('db.query', only_in_trace=True)
    async def execute(
        self,
        query: Query,
        params: Params | None = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> Self:
        if params is None:
            return await super().execute(query, prepare=prepare, binary=binary)
        # Psycopg rejects templates with parameters at runtime, as on the uninstrumented cursor.
        return await super().execute(cast(QueryNoTemplate, query), params, prepare=prepare, binary=binary)


Connection = AsyncConnection[DictRow]


class Pool(AsyncConnectionPool[Connection]):
    @timed('db.pool.acquire', only_in_trace=True)
    async def getconn(self, timeout: float | None = None) -> Connection:
        return await super().getconn(timeout=timeout)


MIGRATIONS_DIR = Path(__file__).parent / 'migrations'
MIGRATION_LOCK = 0x6D6F6E74
MIGRATION_FILE = re.compile(r'^(\d{4})_[a-z0-9_]+\.sql$')


def create_pool(database_url: str) -> Pool:
    return Pool(
        database_url,
        open=False,
        min_size=1,
        max_size=10,
        connection_class=Connection,
        kwargs={'row_factory': dict_row, 'cursor_factory': TimedCursor},
    )


@timed('db.migrate')
async def migrate(database_url: str, migrations_dir: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply pending SQL migrations under an advisory lock."""
    applied: list[int] = []
    async with await AsyncConnection.connect(database_url, autocommit=True) as connection:
        await connection.execute('SELECT pg_advisory_lock(%s)', (MIGRATION_LOCK,))
        try:
            await connection.execute('CREATE SCHEMA IF NOT EXISTS montybot')
            await connection.execute(
                'CREATE TABLE IF NOT EXISTS montybot.schema_migrations '
                '(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())'
            )
            cursor = await connection.execute('SELECT version FROM montybot.schema_migrations')
            done = {row[0] for row in await cursor.fetchall()}
            for version, path in migration_files(migrations_dir):
                if version in done:
                    continue
                async with connection.transaction():
                    await connection.execute(path.read_bytes())
                    await connection.execute('INSERT INTO montybot.schema_migrations (version) VALUES (%s)', (version,))
                applied.append(version)
        finally:
            await connection.execute('SELECT pg_advisory_unlock(%s)', (MIGRATION_LOCK,))
    return applied


def migration_files(migrations_dir: Path) -> Iterator[tuple[int, Path]]:
    """Two branches can each add the next number. A database that has applied one of them would skip
    the other without a word, so a repeated number is an error."""
    seen: dict[int, str] = {}
    for path in sorted(migrations_dir.glob('*.sql')):
        match = MIGRATION_FILE.match(path.name)
        if match is None:
            raise ValueError(f'migration file name {path.name!r} must look like 0001_name.sql')
        version = int(match.group(1))
        if version in seen:
            raise ValueError(f'migrations {seen[version]!r} and {path.name!r} share the number {version:04d}')
        seen[version] = path.name
        yield version, path
