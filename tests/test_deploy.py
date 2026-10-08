"""The deployed stack (deploy/compose.yaml) on this machine's Docker, end to end. Skipped unless
`MONTYBOT_TEST_DEPLOY=1`: it builds the images and needs the host set up as deploy/README.md says (on Ubuntu 24.04,
the bwrap AppArmor profile) and the internet (example.com).

```
pytest --> https://localhost:8443 (Caddy, its own CA) --> app (scripted model, Chromium in bwrap) --> Postgres
       --> docker compose exec / restart                                                        --> backup
```

One stack for the module, under the compose project `montybot-smoke`, removed afterwards with its volumes.
"""

from __future__ import annotations

import os
import secrets
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from helpers import eventually

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / 'deploy'
PROJECT = 'montybot-smoke'

pytestmark = [
    pytest.mark.deploy,
    pytest.mark.skipif(os.environ.get('MONTYBOT_TEST_DEPLOY') != '1', reason='set MONTYBOT_TEST_DEPLOY=1'),
]


@dataclass
class Stack:
    env_file: Path
    url: str
    auth: tuple[str, str]

    def compose(self, *args: str, input: str | None = None) -> str:
        command = ['docker', 'compose', '-p', PROJECT, '--env-file', str(self.env_file)]
        command += ['-f', str(DEPLOY / 'compose.yaml'), '-f', str(DEPLOY / 'compose.smoke.yaml'), *args]
        result = subprocess.run(command, input=input, capture_output=True, text=True, check=False)
        assert result.returncode == 0, f'{" ".join(args)} failed:\n{result.stdout}\n{result.stderr}'
        return result.stdout

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.url, auth=self.auth, verify=False, timeout=30)

    def healthy(self) -> None:
        def ok() -> bool | None:
            try:
                return httpx.get(f'{self.url}/healthz', verify=False, timeout=5).status_code == 200 or None
            except httpx.HTTPError:
                return None

        eventually(ok, timeout=180, what='the app behind Caddy')


@pytest.fixture(scope='module')
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    port = os.environ.get('MONTYBOT_TEST_DEPLOY_PORT', '8443')
    password = secrets.token_urlsafe(16)
    hashed = subprocess.run(
        ['docker', 'run', '--rm', 'caddy:2', 'caddy', 'hash-password', '--plaintext', password],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    env_file = tmp_path_factory.mktemp('deploy') / '.env'
    env_file.write_text(
        '\n'.join(
            [
                'DOMAIN=localhost',
                f'HTTPS_PORT={port}',
                f'POSTGRES_PASSWORD={secrets.token_hex(16)}',
                f'SESSION_SECRET={secrets.token_hex(32)}',
                'ENCRYPTION_KEY=bW9udHlib3QtdGVzdC1rZXktMzItYnl0ZXMtbG9uZyE=',
                'BASIC_AUTH_USER=smoke',
                f"BASIC_AUTH_HASH='{hashed}'",
                'MODEL=script:e2e.scripts:model',
                f'BACKUP_DIR=/tmp/{PROJECT}-backups',
                '',
            ]
        )
    )
    stack = Stack(env_file=env_file, url=f'https://localhost:{port}', auth=('smoke', password))
    stack.compose('down', '--volumes', '--remove-orphans')  # from scratch, as on a new server
    try:
        stack.compose('up', '-d', '--build', '--wait', '--wait-timeout', '300')
        yield stack
    finally:
        if os.environ.get('MONTYBOT_TEST_DEPLOY_KEEP') != '1':
            stack.compose('down', '--volumes', '--remove-orphans')


def sign_up(http: httpx.Client) -> None:
    response = http.post(
        '/api/signup', json={'email': f'smoke-{secrets.token_hex(4)}@example.test', 'password': 'correct horse'}
    )
    assert response.status_code == 201, response.text


def run_of(http: httpx.Client, thread_id: str) -> dict[str, Any]:
    response = http.get(f'/api/threads/{thread_id}')
    assert response.status_code == 200, response.text
    return response.json()


def ask(http: httpx.Client, text: str) -> str:
    response = http.post('/api/threads', json={'text': text})
    assert response.status_code == 201, response.text
    return response.json()['thread_id']


def reply(http: httpx.Client, thread_id: str) -> str:
    def done() -> str | None:
        thread = run_of(http, thread_id)
        status = thread['run']['status']
        if status == 'failed':
            raise AssertionError(f'the run failed: {thread["messages"][-1]["text"]}')
        return thread['messages'][-1]['text'] if status == 'done' else None

    return eventually(done, timeout=120, what='the reply')


def test_the_web_app_is_served_over_https_behind_a_login(stack: Stack) -> None:
    assert httpx.get(stack.url, verify=False).status_code == 401
    # An integration's sign-in comes back to Monty in the user's own browser, without this login: those pages (which
    # act only on a state Monty made) and their stylesheet are open, and nothing else is.
    returned = httpx.get(f'{stack.url}/integrations/composio/callback?state=made-up', verify=False)
    assert returned.status_code == 400 and 'Not connected' in returned.text
    assert httpx.get(f'{stack.url}/integrations/mcp/callback?state=made-up&code=x', verify=False).status_code == 400
    assert httpx.get(f'{stack.url}/static/app.css', verify=False).status_code == 200
    for closed in ('/static/app.js', '/api/integrations', '/integrations/composio/callbackx', '/integrations/'):
        assert httpx.get(f'{stack.url}{closed}', verify=False).status_code == 401, closed
    with stack.client() as http:
        page = http.get('/')
        assert page.status_code == 200 and '<html' in page.text.lower()
        assert http.get('/healthz').json() == {'status': 'ok'}


# Run inside the app container: the same Chromium-in-bwrap the app uses, on public and private addresses, for the
# default engine (our CDP pipe) and the Playwright one kept to roll back to.
ENGINES = ('chromium_cdp_server', 'chromium_server')
PROBE = """
import asyncio
from montybot import engines
from montybot.browser.contract import ActionFailed, Navigate

async def main(name):
    browser = getattr(engines, name)()
    await browser.open(None)
    for url in ['chrome://sandbox', 'https://example.com/', 'http://postgres:5432/', 'http://10.0.0.1/',
                'http://169.254.169.254/', 'http://127.0.0.1:8000/healthz']:
        try:
            await browser.act(Navigate(url=url))
            page = await browser.snapshot()
            print(name, url, 'OPENED', page.title, page.text.replace(chr(10), ' ')[:300])
        except ActionFailed as error:
            print(name, url, 'REFUSED', error)
    await browser.close()

for name in ENGINES:
    asyncio.run(main(name))
""".replace('ENGINES', repr(ENGINES))


def test_the_browser_runs_in_bwrap_and_reaches_only_public_addresses(stack: Stack) -> None:
    lines = {
        tuple(line.split(' ', 2)[:2]): line
        for line in stack.compose('exec', '-T', 'app', 'python3', '-', input=PROBE).splitlines()
        if line.split(' ', 1)[0] in ENGINES
    }
    for engine in ENGINES:
        sandbox, public = lines[engine, 'chrome://sandbox'], lines[engine, 'https://example.com/']
        assert 'Layer 1 Sandbox Namespace' in sandbox, sandbox  # Chrome's own sandbox, inside bwrap
        assert 'OPENED' in public and 'Example Domain' in public, public
        for url in (
            'http://postgres:5432/',
            'http://10.0.0.1/',
            'http://169.254.169.254/',
            'http://127.0.0.1:8000/healthz',
        ):
            line = lines[engine, url]
            assert 'REFUSED' in line and 'ERR_SOCKS_CONNECTION_FAILED' in line, line


def test_a_run_reads_a_public_page(stack: Stack) -> None:
    with stack.client() as http:
        sign_up(http)
        thread = ask(http, 'What is on offer today at https://example.com')
        assert 'Example Domain' in reply(http, thread)


def test_a_waiting_run_survives_restarting_the_app(stack: Stack) -> None:
    with stack.client() as http:
        sign_up(http)
        thread = ask(http, 'Ask me my favourite colour and remember it.')

        def question() -> dict[str, Any] | None:
            ask = run_of(http, thread)['run']['ask']
            return ask if ask is not None and ask['kind'] == 'question' else None

        before = eventually(question, what='the question')
        stack.compose('restart', 'app')
        stack.healthy()
        assert eventually(question, what='the question after the restart')['id'] == before['id']
        response = http.post(f'/api/asks/{before["id"]}', json={'text': 'blue'})
        assert response.status_code == 200, response.text
        assert 'blue' in reply(http, thread).lower()


def test_a_backup_restores_into_a_new_database(stack: Stack) -> None:
    with stack.client() as http:
        sign_up(http)
    written = stack.compose('exec', '-T', 'backup', 'backup', 'now')
    dump = written.split('wrote ', 1)[1].split(' ', 1)[0]
    stack.compose('exec', '-T', 'backup', 'dropdb', '--if-exists', 'montybot_restored')
    stack.compose('exec', '-T', 'backup', 'createdb', 'montybot_restored')
    stack.compose(
        'exec', '-T', 'backup', 'pg_restore', '--no-owner', '--exit-on-error', '-d', 'montybot_restored', dump
    )
    count = 'SELECT count(*) FROM montybot.users'
    live = stack.compose('exec', '-T', 'backup', 'psql', '-tAc', count)
    restored = stack.compose('exec', '-T', 'backup', 'psql', '-d', 'montybot_restored', '-tAc', count)
    assert int(restored) == int(live) >= 1
