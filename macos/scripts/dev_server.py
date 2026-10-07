"""A local montybot for working on the Mac app: the real app, the scripted model and the fixture sites.

    uv run python macos/scripts/dev_server.py            # headless Chromium, so takeover shows real pages
    uv run python macos/scripts/dev_server.py --fake     # the HTML fake browser: faster, blank pictures

The sites' addresses are also written to data/mac-dev-sites.json, for the app's integration tests.

Postgres is `--postgres` (or MONTYBOT_TEST_POSTGRES) when given; otherwise an embedded Postgres in
data/mac-dev-pg (the `pgserver` package, no Docker), which keeps running for next time. The app gets its own
database, `montybot_mac_dev`, created on first use. A message that starts with one of the scripted prompts
(tests/e2e/scripts.py) runs that path; they are printed with the fixture sites' addresses filled in. With
MONTYBOT_TEST_MODEL set, a real model answers instead.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / 'tests'
sys.path.insert(0, str(TESTS))

from sites.checkpoint import Checkpoint
from sites.flights import Flights
from sites.invoices import Invoices
from sites.shop import Shop
from sites.slots import Slots

DATABASE = 'montybot_mac_dev'
PROMPTS = {
    'shop': ['Order eggs from {url}', 'Fill my cart at {url} with eggs and milk'],
    'flights': ['Find the three cheapest flights to Lisbon next Friday at {url}'],
    'checkpoint': ['What is on offer today at {url}'],
    'invoices': ['Download my last three invoices from {url}'],
    'slots': ['Tell me when a delivery slot opens at {url}'],
}


EMBEDDED = """
import pathlib, pgserver
directory = pathlib.Path(%r)
directory.mkdir(parents=True, exist_ok=True)
print(pgserver.get_server(str(directory), cleanup_mode=None).get_uri())
"""


def embedded_postgres() -> str:
    """Starts (or finds running) the embedded Postgres in data/mac-dev-pg; its URL, on a Unix socket."""
    directory = ROOT / 'data' / 'mac-dev-pg'
    started = subprocess.run(
        [
            'uv',
            'run',
            '--no-project',
            '--python',
            '3.12',
            '--with',
            'pgserver',
            'python',
            '-c',
            EMBEDDED % str(directory),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return started.stdout.strip().splitlines()[-1]


def database_url(server: str) -> str:
    """`server`'s `montybot_mac_dev` database, created if it is missing."""
    with psycopg.connect(server, autocommit=True) as connection:
        found = connection.execute('SELECT 1 FROM pg_database WHERE datname = %s', (DATABASE,)).fetchone()
        if found is None:
            connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(DATABASE)))
    parts = urlsplit(server)
    return urlunsplit(parts._replace(path=f'/{DATABASE}'))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--fake', action='store_true', help='the HTML fake browser instead of headless Chromium')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument(
        '--postgres',
        default=os.environ.get('MONTYBOT_TEST_POSTGRES'),
        help='a Postgres server URL whose user may create databases (default: an embedded one)',
    )
    args = parser.parse_args()

    sites = {'shop': Shop(), 'flights': Flights(), 'checkpoint': Checkpoint(), 'invoices': Invoices(), 'slots': Slots()}
    print('Scripted prompts:')
    for name, site in sites.items():
        url = site.start()
        for prompt in PROMPTS[name]:
            print(f'  {prompt.format(url=url)}')
    print('  Say hello / Ask me my favourite colour / Fail please', flush=True)

    data = ROOT / 'data'
    workspaces = data / 'dev-workspaces'
    workspaces.mkdir(parents=True, exist_ok=True)
    # Where the Mac app's integration tests find the sites (`swift test` with MONTY_TEST_SERVER set).
    (data / 'mac-dev-sites.json').write_text(json.dumps({name: site.url for name, site in sites.items()}))
    url = f'http://127.0.0.1:{args.port}'
    env = {
        **os.environ,
        'DATABASE_URL': database_url(args.postgres or embedded_postgres()),
        'SESSION_SECRET': 'dev-session-secret',
        'ENCRYPTION_KEY': 'bW9udHlib3QtdGVzdC1rZXktMzItYnl0ZXMtbG9uZyE=',
        'MODEL': os.environ.get('MONTYBOT_TEST_MODEL', 'script:e2e.scripts:model'),
        'BROWSER_BACKEND': 'sites.html_browser:new_backend' if args.fake else 'montybot.engines:chromium_headless',
        'PYTHONPATH': os.pathsep.join([str(TESTS), str(TESTS / 'e2e'), os.environ.get('PYTHONPATH', '')]),
        'EXECUTOR_ID': 'local',
        'ALLOW_PRIVATE_NETWORKS': 'true',  # the fixture sites are on 127.0.0.1
        'WORKSPACES_DIR': str(workspaces),
        'PORT': str(args.port),
        'PUBLIC_URL': url,
    }
    print(f'montybot on {url}', flush=True)
    app = subprocess.Popen([sys.executable, '-m', 'montybot', 'serve'], cwd=ROOT, env=env, start_new_session=True)
    try:
        app.wait()
    except KeyboardInterrupt:
        os.killpg(app.pid, signal.SIGINT)
        app.wait(timeout=20)
    finally:
        for site in sites.values():
            site.stop()


if __name__ == '__main__':
    main()
