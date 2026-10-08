"""Shared fixtures: a Postgres server for the whole session, and a fresh database per test.

Postgres: `SAMMY_TEST_POSTGRES` (a server URL whose user may create databases), else a `postgres:17` container
this session starts with Docker.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from helpers import eventually, free_port
from psycopg import sql


def _reachable(url: str) -> bool:
    try:
        psycopg.connect(url, connect_timeout=2).close()
    except psycopg.OperationalError:
        return False
    return True


@pytest.fixture(scope='session')
def postgres() -> Iterator[str]:
    """A Postgres server URL whose user may create databases."""
    url = os.environ.get('SAMMY_TEST_POSTGRES')
    if url:
        yield url
        return
    if shutil.which('docker') is None:
        pytest.skip('set SAMMY_TEST_POSTGRES or install Docker')
    port = free_port()
    name = f'sammy-test-{uuid.uuid4().hex[:8]}'
    subprocess.run(
        [
            'docker',
            'run',
            '-d',
            '--rm',
            '--name',
            name,
            '-e',
            'POSTGRES_PASSWORD=postgres',
            '-p',
            f'{port}:5432',
            'postgres:17',
            '-c',
            'max_connections=500',
        ],
        check=True,
        capture_output=True,
    )
    url = f'postgresql://postgres:postgres@127.0.0.1:{port}/postgres'
    try:
        eventually(lambda: _reachable(url) or None, timeout=60, what='Postgres to start')
        yield url
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, check=False)


@pytest.fixture
def database_url(postgres: str) -> Iterator[str]:
    name = f't_{uuid.uuid4().hex[:12]}'
    with psycopg.connect(postgres, autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    yield postgres.rsplit('/', 1)[0] + f'/{name}'
    with psycopg.connect(postgres, autocommit=True) as connection:
        connection.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(name)))
