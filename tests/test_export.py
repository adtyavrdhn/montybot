"""#135: an export holds everything of the user's, and never their sign-ins, tokens, secrets or MCP server URLs."""

from __future__ import annotations

import base64
import json
import uuid
import zipfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sites.integrations import API_KEY, Account, FakeComposio

from sammy import attachments, auth, crypto, export, memory, notifications, signins, store
from sammy.browser.state import BrowserState, Cookie
from sammy.db import Pool, create_pool, migrate
from sammy.integrations import Integrations, mcp
from sammy.models import AskKind
from sammy.settings import Settings
from sammy.workspaces import Workspaces

pytestmark = pytest.mark.anyio
KEY = crypto.deployment_key(crypto.new_key())

COOKIE = 'sid-cookie-value-0001'
SITE_STORAGE = 'site-storage-value-0002'
MCP_URL = 'https://notes-host-0003.example.com/mcp?key=url-key-0004'
MCP_TOKEN = 'header-token-0005'
OAUTH_STATE = 'oauth-state-0006'
OAUTH_VERIFIER = 'oauth-verifier-0007'
PUSH_ENDPOINT = 'https://fcm.googleapis.com/fcm/send/push-endpoint-0008'
PUSH_AUTH = 'push-auth-0009'
RESET_CODE_HASH = 'reset-code-hash-0010'
HANDOFF_ID = 'handoff-id-0011'
LISTED_URL = 'https://listed-host-0012.example.com/mcp'
OUTSIDE = 'outside-the-workspace-0013'


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
async def pool(database_url: str) -> AsyncIterator[Pool]:
    await migrate(database_url)
    pool = create_pool(database_url)
    await pool.open()
    yield pool
    await pool.close()


@pytest.fixture
def composio() -> Iterator[FakeComposio]:
    fake = FakeComposio()
    fake.start()
    yield fake
    fake.stop()


def settings(database_url: str, composio: FakeComposio, tmp_path: Path) -> Settings:
    return Settings(
        database_url=database_url,
        session_secret='s',  # pyright: ignore[reportArgumentType]
        encryption_key=crypto.new_key(),  # pyright: ignore[reportArgumentType]
        composio_api_key=API_KEY,  # pyright: ignore[reportArgumentType]
        composio_url=composio.url,
        exports_dir=tmp_path / 'exports',
    )


async def test_an_export_has_everything_of_the_users_and_none_of_their_secrets(
    pool: Pool, composio: FakeComposio, database_url: str, tmp_path: Path
) -> None:
    password_hash = auth.hash_password('correct horse')
    async with pool.connection() as c:
        ada = await store.create_user(c, 'ada@example.test', password_hash, 'Ada')
        bob = await store.create_user(c, 'bob@example.test', 'x')
        assert ada is not None and bob is not None
        await store.set_squirrel_name(c, ada.id, 'Hazel')

        # A chat: a message with a file, what Sammy asked and was told, its steps and its reply.
        thread = await store.create_thread(c, ada.id, 'Groceries')
        run_id = str(uuid.uuid4())
        await store.create_run(
            c, run_id=run_id, user_id=ada.id, thread_id=thread.id, prompt='Buy eggs', trigger='message'
        )
        upload = await attachments.upload(c, ada.id, 'list.txt', 'text/plain', b'eggs, milk')
        await attachments.attach(c, ada.id, run_id, [upload.id])
        asked: list[tuple[AskKind, dict[str, object], dict[str, object]]] = [
            ('approval', {}, {'approved': True}),
            ('handoff', {'handoff_id': HANDOFF_ID}, {'done': True}),
            ('connect', {'integration': {'provider': 'mcp', 'name': 'Acme', 'url': LISTED_URL}}, {'connected': True}),
        ]
        for occurrence, (kind, details, answer) in enumerate(asked, start=1):
            ask_id = str(uuid.uuid4())
            await store.create_ask(
                c, ask_id=ask_id, run_id=run_id, user_id=ada.id, occurrence=occurrence, kind=kind, prompt=f'{kind}?',
                details=details,
            )  # fmt: skip
            await store.answer_ask(c, ada.id, ask_id, answer)
        await store.add_activity(c, run_id, 'Opened shop.test')
        await store.append_history(
            c,
            thread.id,
            [ModelRequest(parts=[UserPromptPart(content='Buy eggs')]), ModelResponse(parts=[TextPart('In the cart.')])],
        )
        await store.finish_run(c, run_id, 'done', output='Eggs are in the cart.')

        await memory.add_memory(c, ada.id, 'Buys brown eggs')
        await memory.add_memory(c, bob.id, 'Bob likes white eggs')
        await store.create_schedule(
            c, schedule_id=str(uuid.uuid4()), user_id=ada.id, name='Weekly eggs', cron='0 8 * * 5',
            timezone='Europe/London', when='Fridays at 08:00', prompt='Order eggs', watch=False,
        )  # fmt: skip

        # What must never leave: sign-in flows, MCP secrets, push subscriptions, reset codes.
        async with c.transaction():
            headers = {'Authorization': f'Bearer {MCP_TOKEN}'}
            await mcp.add(
                c, KEY, user_id=ada.id, name='Notes', auth='headers', status='ready',
                secret=mcp.Secret(url=MCP_URL, headers=headers),
            )  # fmt: skip
            wiki = await mcp.add(
                c, KEY, user_id=ada.id, name='Wiki', auth='oauth', status='needs_sign_in',
                secret=mcp.Secret(url=MCP_URL.replace('notes', 'wiki')),
            )  # fmt: skip
            await mcp.start_flow(c, KEY, wiki, OAUTH_STATE, OAUTH_VERIFIER)
        await notifications.add_subscription(c, ada.id, PUSH_ENDPOINT, {'p256dh': 'p256', 'auth': PUSH_AUTH})
        await store.start_password_reset(c, ada.id, RESET_CODE_HASH, 15)
    await signins.PostgresJar(pool, KEY).save(
        user_id=ada.id,
        state=BrowserState(
            url='https://shop.test/cart',
            cookies=[Cookie(name='sid', value=COOKIE, domain='shop.test')],
            local_storage={'https://shop.test': {'token': SITE_STORAGE}},
        ),
    )
    composio.accounts['ca_ada'] = Account(
        id='ca_ada', user_id=f'sammy:{ada.id}', toolkit='linear', auth_config_id='ac', status='ACTIVE'
    )

    # Their files, and links that lead out of them, to a file and a folder.
    workspaces = Workspaces(tmp_path / 'workspaces')
    home = workspaces.directory(ada.id)
    (home / 'notes').mkdir(parents=True)
    (home / 'notes' / 'todo.txt').write_text('call the vet')
    (tmp_path / 'outside.txt').write_text(OUTSIDE)
    (tmp_path / 'outside-folder').mkdir()
    (tmp_path / 'outside-folder' / 'secret.txt').write_text(OUTSIDE)
    (home / 'escape.txt').symlink_to(tmp_path / 'outside.txt')
    (home / 'escape-folder').symlink_to(tmp_path / 'outside-folder')

    integrations = Integrations(pool, KEY, settings(database_url, composio, tmp_path))
    target = tmp_path / 'export.zip'
    try:
        await export.write(target, pool=pool, workspaces=workspaces, integrations=integrations, user_id=ada.id)
    finally:
        await integrations.aclose()

    with zipfile.ZipFile(target) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    everything = b'\n'.join([*(name.encode() for name in entries), *entries.values()])

    async with pool.connection() as c:
        row = await (await c.execute('SELECT data_key FROM sammy.users WHERE id = %s', (ada.id,))).fetchone()
    assert row is not None and row['data_key'] is not None
    data_key = bytes(row['data_key'])
    never = [COOKIE, SITE_STORAGE, MCP_URL, 'notes-host-0003', MCP_TOKEN, OAUTH_STATE, OAUTH_VERIFIER, PUSH_ENDPOINT]
    never += [PUSH_AUTH, RESET_CODE_HASH, password_hash, HANDOFF_ID, LISTED_URL, OUTSIDE, 'Bob likes white eggs']
    leaked = [secret for secret in never if secret.encode() in everything]
    assert leaked == []
    assert data_key not in everything and base64.b64encode(data_key) not in everything

    # And everything of theirs is there.
    # Each chat in a folder of its own, the schedule's too: `chats/<date> <title> <id>`.
    folder, scheduled = (
        f'chats/{name}' for name in sorted({n.split('/')[1] for n in entries if n.startswith('chats/')})
    )
    assert folder.startswith('chats/') and folder.endswith(f' Groceries {thread.id}')
    assert ' Weekly eggs ' in scheduled
    assert sorted(entries) == sorted(
        [
            'account.json',
            'memories.json',
            'schedules.json',
            'integrations.json',
            f'{folder}/chat.md',
            f'{folder}/chat.json',
            f'{folder}/files/list.txt',
            f'{scheduled}/chat.md',
            f'{scheduled}/chat.json',
            'files/notes/todo.txt',
        ]
    )
    account = json.loads(entries['account.json'])
    assert (account['email'], account['name'], account['squirrel_name']) == ('ada@example.test', 'Ada', 'Hazel')
    assert [m['text'] for m in json.loads(entries['memories.json'])] == ['Buys brown eggs']
    [schedule] = json.loads(entries['schedules.json'])
    assert (schedule['name'], schedule['when'], schedule['task']) == ('Weekly eggs', 'Fridays at 08:00', 'Order eggs')
    assert json.loads(entries['integrations.json']) == [
        {'name': 'Linear', 'kind': 'composio'},
        {'name': 'Notes', 'kind': 'mcp'},
        {'name': 'Wiki', 'kind': 'mcp'},
    ]
    assert entries[f'{folder}/files/list.txt'] == b'eggs, milk'
    assert entries['files/notes/todo.txt'] == b'call the vet'
    chat = entries[f'{folder}/chat.md'].decode()
    assert chat.startswith('# Groceries\n')
    for line in ['**You:**', 'Buy eggs', '- [list.txt](<files/list.txt>)', '> You approved: approval?']:
        assert line in chat
    assert '> You connected Acme' in chat and 'Eggs are in the cart.' in chat
    detail = json.loads(entries[f'{folder}/chat.json'])
    assert [t['steps'] for t in detail['tasks']] == [['Opened shop.test']]
    assert [part['content'] for m in detail['model_messages'] for part in m['parts']] == ['Buy eggs', 'In the cart.']


async def test_an_emailed_link_opens_only_its_own_export(
    database_url: str, composio: FakeComposio, tmp_path: Path
) -> None:
    configured = settings(database_url, composio, tmp_path)
    user_id, export_id = str(uuid.uuid4()), str(uuid.uuid4())
    path = export.built(configured, user_id, export_id)
    path.parent.mkdir(parents=True)
    path.write_bytes(b'zip')
    token = export.signer(configured).dumps({'u': user_id, 'e': export_id})

    assert export.ready(configured, token) == path
    assert export.ready(configured, token[:-2] + ('AA' if not token.endswith('AA') else 'BB')) is None
    other = export.signer(configured).dumps({'u': user_id, 'e': str(uuid.uuid4())})
    assert export.ready(configured, other) is None  # not built, or swept
    assert export.ready(configured, export.signer(configured).dumps({'u': '../..', 'e': export_id})) is None
    elsewhere = configured.model_copy(update={'session_secret': configured.session_secret.__class__('other')})
    assert export.ready(elsewhere, token) is None
