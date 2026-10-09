"""Everything of the user's, to take with them (#135): `GET /api/export` (sammy.account_api).

```
sammy-export-<date>.zip
  account.json              email, name, time zone, the squirrel's name, when they joined
  memories.json             what Sammy remembers about them
  schedules.json            their recurring tasks and watches
  integrations.json         the names of the apps and servers they connected
  chats/<date> <title> <id>/
    chat.md                 the chat as the apps show it
    chat.json               that, each task's steps, and the model's messages
    files/                  what they attached, and what Sammy shared with them
  files/                    their files, as Sammy's code sees them at /work
```

Never in it: saved sign-ins (cookies and site storage), passwords and reset codes, the user's data key, an MCP server's
URL, headers or tokens, push subscriptions, and hand-off ids. This module never reads the columns that hold them, so
none of them can end up in the zip. An integration is only its name and kind.

A small account is zipped while the request waits. When email is set up, a bigger one (`EXPORT_INLINE_BYTES`) is built
by the `export_account` workflow in `EXPORTS_DIR`, and the user gets an email with a link that works for a day.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import stat
import time
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import logfire
from dbos import DBOS, SetWorkflowID
from itsdangerous import BadSignature, URLSafeTimedSerializer

from sammy import attachments, store
from sammy.api import chat_messages
from sammy.db import Connection, Pool
from sammy.integrations import IntegrationError, Integrations, mcp
from sammy.memory import list_memories
from sammy.models import Schedule
from sammy.notifications import send_email
from sammy.observability import timed
from sammy.resources import Resources, current
from sammy.settings import Settings
from sammy.workflows import RETRIED
from sammy.workspaces import Workspaces, download_name

LINK_SECONDS = 24 * 60 * 60
"""How long an emailed link works, and how long its zip is kept."""
WORKFLOW_PREFIX = 'sammy-export-'

Put = Callable[[str, bytes], Awaitable[None]]


def file_name() -> str:
    return f'sammy-export-{datetime.now(UTC):%Y-%m-%d}.zip'


# --- what goes in ---


@timed('export.write')
async def write(target: Path, *, pool: Pool, workspaces: Workspaces, integrations: Integrations, user_id: str) -> None:
    """Zip everything of the user's into `target`."""
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:

        async def put(name: str, data: bytes) -> None:
            await asyncio.to_thread(archive.writestr, name, data)

        async with pool.connection() as connection:
            await put('account.json', as_json(await account(connection, user_id)))
            await put('memories.json', as_json(await list_memories(connection, user_id)))
            await put(
                'schedules.json', as_json([schedule_json(s) for s in await store.list_schedules(connection, user_id)])
            )
            cursor = await connection.execute(
                'SELECT id, title, created_at FROM sammy.threads WHERE user_id = %s ORDER BY created_at', (user_id,)
            )
            for row in await cursor.fetchall():
                folder = f'chats/{row["created_at"]:%Y-%m-%d} {download_name(row["title"] or "Chat")[:60]} {row["id"]}'
                await put_chat(put, connection, user_id, str(row['id']), folder)
            servers = await mcp.list_for(connection, user_id)
        await put('integrations.json', as_json(await integration_names(integrations, user_id, servers)))
        async with workspaces.lock(user_id):
            await asyncio.to_thread(put_files, archive, workspaces.directory(user_id))


def as_json(value: object) -> bytes:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str).encode()


async def account(connection: Connection, user_id: str) -> dict[str, str]:
    cursor = await connection.execute(
        'SELECT email, name, timezone, squirrel_name, created_at FROM sammy.users WHERE id = %s', (user_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    return {
        'email': row['email'],
        'name': row['name'],
        'timezone': row['timezone'],
        'squirrel_name': row['squirrel_name'],
        'created_at': row['created_at'].isoformat(),
    }


def schedule_json(schedule: Schedule) -> dict[str, str | bool]:
    return {
        'name': schedule.name,
        'when': schedule.when,
        'cron': schedule.cron,
        'timezone': schedule.timezone,
        'task': schedule.prompt,
        'watch': schedule.watch,
        'chat': schedule.thread_id,
    }


async def put_chat(put: Put, connection: Connection, user_id: str, thread_id: str, folder: str) -> None:
    thread = await store.get_thread(connection, user_id, thread_id)
    assert thread is not None
    runs = await store.list_runs(connection, user_id, thread_id)
    files = await attachments.files_of_runs(connection, user_id, [run.id for run in runs])
    messages, _ = chat_messages(runs, await store.list_answered_asks(connection, user_id, thread_id), files)
    activity = await store.list_thread_activity(connection, user_id, thread_id)
    cursor = await connection.execute(
        'SELECT payload FROM sammy.messages WHERE thread_id = %s ORDER BY position', (thread_id,)
    )
    history = [row['payload'] for row in await cursor.fetchall()]

    # Each file once, under its own name, or with its id when two share one.
    paths: dict[str, str] = {}
    for kept in (f for by_sender in files.values() for sent in by_sender.values() for f in sent):
        name = str(kept['name'])
        paths[str(kept['id'])] = name if name not in paths.values() else f'{kept["id"]} {name}'
    for attachment_id, name in paths.items():
        found = await attachments.read(connection, user_id, attachment_id)
        if found is not None:
            await put(f'{folder}/files/{name}', found[1])

    runs_json = [
        {
            'task': run.prompt,
            'status': run.status,
            'reply': run.output,
            'started_at': run.started_at.isoformat() if run.started_at else None,
            'completed_at': run.completed_at.isoformat() if run.completed_at else None,
            'steps': activity.get(run.id, []),
        }
        for run in runs
    ]
    chat = {'id': thread.id, 'title': thread.title, 'messages': messages, 'tasks': runs_json, 'model_messages': history}
    await put(f'{folder}/chat.json', as_json(chat))
    await put(f'{folder}/chat.md', markdown(thread.title, messages, paths).encode())


def markdown(title: str, messages: list[dict[str, object]], paths: dict[str, str]) -> str:
    lines = [f'# {title or "Chat"}', '']
    for message in messages:
        text = str(message['text'])
        match message['role']:
            case 'event':
                lines += [f'> {text}', '']
            case role:
                lines += [f'**{"You" if role == "user" else "Sammy"}:**', '', text, '']
        sent = message.get('files')
        if isinstance(sent, list):
            for kept in cast(list[dict[str, object]], sent):  # `attachments.files_of_runs`
                if name := paths.get(str(kept['id'])):
                    lines.append(f'- [{name}](<files/{name}>)')
            lines.append('')
    return '\n'.join(lines)


async def integration_names(
    integrations: Integrations, user_id: str, servers: list[mcp.Server]
) -> list[dict[str, str]]:
    """Names and kinds only. Apps connected through Composio are left out if Composio cannot be reached now."""
    try:
        connections = await integrations.connections(user_id)
    except IntegrationError as error:
        logfire.warn('Exporting without connected apps: {error_type}', error_type=type(error).__name__)
        return [{'name': server.name, 'kind': 'mcp'} for server in servers]
    return [{'name': c.name, 'kind': c.provider} for c in connections]


def put_files(archive: zipfile.ZipFile, root: Path) -> None:
    """Regular files only, and never through a link: code in the CPython tier can make links that lead out of the
    user's directory. `os.walk` does not enter a linked folder, and `O_NOFOLLOW` does not open a linked file."""
    for folder, _, names in os.walk(root):
        for name in names:
            path = os.path.join(folder, name)
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError:
                continue  # a link, or gone meanwhile
            with os.fdopen(fd, 'rb') as file:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    continue
                entry = 'files/' + Path(os.path.relpath(path, root)).as_posix()
                with archive.open(entry, 'w', force_zip64=True) as out:
                    shutil.copyfileobj(file, out)


async def size(pool: Pool, workspaces: Workspaces, user_id: str) -> int:
    """About how many bytes the export holds before compression: files in chats, the model's messages, and files."""
    async with pool.connection() as connection:
        cursor = await connection.execute(
            'SELECT (SELECT coalesce(sum(size), 0) FROM sammy.attachments WHERE user_id = %s AND run_id IS NOT NULL) '
            '+ (SELECT coalesce(sum(pg_column_size(m.payload)), 0) FROM sammy.messages m '
            'JOIN sammy.threads t ON t.id = m.thread_id WHERE t.user_id = %s) AS bytes',
            (user_id, user_id),
        )
        row = await cursor.fetchone()
    assert row is not None
    return int(row['bytes']) + await asyncio.to_thread(files_size, workspaces.directory(user_id))


def files_size(root: Path) -> int:
    total = 0
    for folder, _, names in os.walk(root):
        for name in names:
            with contextlib.suppress(OSError):
                found = os.lstat(os.path.join(folder, name))
                total += found.st_size if stat.S_ISREG(found.st_mode) else 0
    return total


# --- built in the background, and emailed ---


def workflow_prefix(user_id: str) -> str:
    return f'{WORKFLOW_PREFIX}{uuid.UUID(user_id)}-'


def user_folder(settings: Settings, user_id: str) -> Path:
    return settings.exports_dir.expanduser().absolute() / str(uuid.UUID(user_id))


def built(settings: Settings, user_id: str, export_id: str) -> Path:
    return user_folder(settings, user_id) / f'{uuid.UUID(export_id)}.zip'


def signer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret.get_secret_value(), salt='sammy.export')


async def start(user_id: str) -> None:
    export_id = str(uuid.uuid4())
    with SetWorkflowID(workflow_prefix(user_id) + export_id):
        await DBOS.start_workflow_async(export_account, user_id, export_id)


@DBOS.workflow(name='sammy.export_account')
async def export_account(user_id: str, export_id: str) -> None:
    resources = current()
    await DBOS.run_step_async({**RETRIED, 'name': 'export.build'}, build, resources, user_id, export_id)
    await DBOS.run_step_async({**RETRIED, 'name': 'export.email'}, email_link, resources, user_id, export_id)


async def build(resources: Resources, user_id: str, export_id: str) -> None:
    target = built(resources.settings, user_id, export_id)
    await asyncio.to_thread(sweep, resources.settings.exports_dir.expanduser().absolute())
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    partial = target.with_suffix('.part')
    await write(
        partial,
        pool=resources.pool,
        workspaces=resources.workspaces,
        integrations=resources.integrations,
        user_id=user_id,
    )
    partial.replace(target)


async def email_link(resources: Resources, user_id: str, export_id: str) -> None:
    async with resources.pool.connection() as connection:
        user = await store.get_user(connection, user_id)
    if user is None:
        return  # the account was deleted meanwhile
    settings = resources.settings
    token = signer(settings).dumps({'u': user_id, 'e': export_id})
    await asyncio.to_thread(
        send_email,
        settings,
        user.email,
        'Your Sammy data is ready to download',
        f'Everything of yours from Sammy, in one zip file:\n\n{settings.public_url}/api/exports/{token}\n\n'
        f'The link works for {LINK_SECONDS // 3600} hours. Anyone with it can download your data, so keep it to '
        'yourself. If you did not ask for this, change your password.',
    )


def ready(settings: Settings, token: str) -> Path | None:
    """The zip an emailed link names, while the link works."""
    try:
        payload = signer(settings).loads(token, max_age=LINK_SECONDS)
        path = built(settings, str(payload['u']), str(payload['e']))
    except (BadSignature, KeyError, TypeError, ValueError):
        return None
    return path if path.is_file() else None


def sweep(root: Path) -> None:
    """Delete the zips whose links have expired, of every user, and any left half built."""
    cutoff = time.time() - LINK_SECONDS
    for path in root.glob('*/*.*'):
        with contextlib.suppress(FileNotFoundError):
            if path.stat().st_mtime < cutoff:
                path.unlink()
