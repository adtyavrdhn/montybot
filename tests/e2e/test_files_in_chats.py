"""Files in chats (`sammy.attachments`): the user attaches files to a message, the model sees what it can and the
run's code gets every file in /work/uploads; Sammy shares a file back with its reply. Another user can reach none of
it."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import psycopg
import pytest
from conftest import App, Client
from PIL import Image

pytestmark = pytest.mark.scripted

PDF = b'%PDF-1.4\n1 0 obj << /Type /Page >> endobj\ntrailer << >>\n%%EOF\n'


def png(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new('RGB', (width, height), (200, 30, 120)).save(out, format='PNG')
    return out.getvalue()


def upload(http: httpx.Client, name: str, data: bytes, media_type: str = 'application/octet-stream') -> dict[str, Any]:
    response = http.post(
        '/api/attachments', content=data, headers={'Content-Type': media_type, 'X-Filename': quote(name)}
    )
    assert response.status_code == 201, response.text
    return response.json()


def send(http: httpx.Client, text: str, files: list[dict[str, Any]], thread_id: str | None = None) -> httpx.Response:
    path = '/api/threads' if thread_id is None else f'/api/threads/{thread_id}/messages'
    return http.post(path, json={'text': text, 'attachments': [f['id'] for f in files]})


def test_the_model_sees_attached_files_and_the_code_gets_them(app: App, client: Client, workspaces_dir: Path) -> None:
    alice = client.sign_up()
    photo = upload(client.http, 'Holiday photo.png', png(3000, 1500), 'image/png')
    icon = upload(client.http, 'icon.png', png(40, 30), 'image/png')
    paper = upload(client.http, 'paper.pdf', PDF, 'application/pdf')
    notes = upload(client.http, 'notes.md', b'# Groceries\neggs, milk\n', '')
    sheet = upload(client.http, 'budget.xlsx', b'PK\x03\x04 not really a spreadsheet')
    fake = upload(client.http, 'fake.png', b'not an image at all', 'image/png')
    assert [f['kind'] for f in (photo, icon, paper, notes, sheet, fake)] == [
        'image',
        'image',
        'pdf',
        'text',
        'file',
        'file',
    ]
    assert notes['media_type'] == 'text/markdown' and sheet['media_type'].startswith('application/vnd.openxml')

    response = send(client.http, 'Describe what I attached', [photo, icon, paper, notes, sheet, fake])
    assert response.status_code == 201, response.text
    thread_id = response.json()['thread_id']
    seen = client.wait_for_reply(thread_id).splitlines()

    assert seen[0] == 'text: Describe what I attached'
    assert seen[1].startswith('note: [The user attached "Holiday photo.png" (image/png, ') and seen[1].endswith(
        'saved at /work/uploads/Holiday photo.png.]'
    )
    assert seen[2] == 'image/jpeg 1568x784'  # made small enough for the model, as a JPEG
    assert seen[4] == 'image/png 40x30'  # small already: as it was
    assert seen[6] == f'application/pdf {len(PDF)} bytes'
    assert seen[8] == 'file text: <file name="notes.md">\\n# Groceries\\neggs, milk\\n\\n</file>'
    # Sammy opens these with code: the model gets their notes only (a "PNG" that is not one is never sent as one).
    assert len(seen) == 11
    assert seen[9].startswith('note: [The user attached "budget.xlsx"')
    assert seen[10].startswith('note: [The user attached "fake.png"')

    # The run's code finds every file in the user's /work/uploads, byte for byte.
    uploads = workspaces_dir / alice['id'] / 'uploads'
    assert sorted(p.name for p in uploads.iterdir()) == sorted(
        ['Holiday photo.png', 'icon.png', 'paper.pdf', 'notes.md', 'budget.xlsx', 'fake.png']
    )
    assert (uploads / 'paper.pdf').read_bytes() == PDF

    # The chat shows the user's message with its files; a picture comes as itself, anything else as a download.
    message = client.thread(thread_id)['messages'][0]
    assert message['text'] == 'Describe what I attached'
    assert [f['name'] for f in message['files']] == [
        'Holiday photo.png',
        'icon.png',
        'paper.pdf',
        'notes.md',
        'budget.xlsx',
        'fake.png',
    ]
    shown = client.http.get(f'/api/attachments/{photo["id"]}')
    assert shown.content == png(3000, 1500) and shown.headers['content-type'] == 'image/png'
    assert shown.headers['content-disposition'].startswith('inline;')
    assert (
        shown.headers['x-content-type-options'] == 'nosniff' and 'sandbox' in shown.headers['content-security-policy']
    )
    saved = client.http.get(f'/api/attachments/{paper["id"]}')
    assert saved.headers['content-type'] == 'application/octet-stream'
    assert saved.headers['content-disposition'] == "attachment; filename*=UTF-8''paper.pdf"
    assert client.http.get(f'/api/attachments/{fake["id"]}').headers['content-type'] == 'application/octet-stream'

    # The stored history keeps a note per file, never the bytes; the next message gets the files back.
    with psycopg.connect(app.env['DATABASE_URL']) as connection:
        payloads = connection.execute(
            'SELECT payload FROM sammy.messages WHERE thread_id = %s', (thread_id,)
        ).fetchall()
    asked = json.dumps(
        [part for (payload,) in payloads for part in payload['parts'] if part['part_kind'] == 'user-prompt']
    )
    assert 'saved at /work/uploads/paper.pdf' in asked
    assert '"binary"' not in asked and 'Groceries' not in asked
    assert send(client.http, 'Describe what I attached', [], thread_id).status_code == 201
    again = client.wait_for_reply(thread_id).splitlines()
    assert again[: len(seen)] == seen


def test_a_message_of_files_only_is_titled_by_its_first_file(client: Client) -> None:
    client.sign_up()
    photo = upload(client.http, 'receipt.png', png(10, 10), 'image/png')
    response = send(client.http, '', [photo])
    assert response.status_code == 201, response.text
    thread_id = response.json()['thread_id']
    assert client.thread(thread_id)['title'] == 'receipt.png'
    assert client.thread(thread_id)['messages'][0] == {'role': 'user', 'text': '', 'files': [photo]}


def test_files_keep_the_order_they_were_sent_in(client: Client) -> None:
    """Uploads finish in any order; the message lists its files as the user added them."""
    client.sign_up()
    first = upload(client.http, 'first.txt', b'1', 'text/plain')
    second = upload(client.http, 'second.txt', b'2', 'text/plain')
    response = send(client.http, 'Describe what I attached', [second, first])
    assert response.status_code == 201, response.text
    thread_id = response.json()['thread_id']
    assert [f['name'] for f in client.thread(thread_id)['messages'][0]['files']] == ['second.txt', 'first.txt']
    seen = client.wait_for_reply(thread_id)
    assert seen.index('"second.txt"') < seen.index('"first.txt"')  # and the model reads them so


def test_sammy_shares_a_file_with_its_reply(client: Client) -> None:
    client.sign_up()
    thread_id = client.ask('Make me a report')
    reply = client.wait_for_reply(thread_id)
    assert 'Shared report.csv with the user' in reply
    shared = client.thread(thread_id)['messages'][-1]['files']
    assert [(f['name'], f['kind'], f['size']) for f in shared] == [('report.csv', 'text', 18)]
    assert client.http.get(f'/api/attachments/{shared[0]["id"]}').content == b'item,total\neggs,3\n'


def test_uploads_are_bounded_and_private(app: App, client: Client) -> None:
    client.sign_up()
    http = client.http
    # The file must be named: a page on another site cannot set the header.
    assert http.post('/api/attachments', content=b'x', headers={'Content-Type': 'text/plain'}).status_code == 400
    assert http.post('/api/attachments', content=b'', headers={'X-Filename': 'empty.txt'}).status_code == 400
    too_big = http.post('/api/attachments', content=b'x' * (20 * 1024 * 1024 + 1), headers={'X-Filename': 'big.bin'})
    assert too_big.status_code == 413
    assert http.post('/api/threads', json={'text': ''}).status_code == 422
    eleven = [upload(http, f'{n}.txt', b'hi', 'text/plain') for n in range(11)]
    assert send(http, 'Too many', eleven).status_code == 422
    unknown = {'id': '00000000-0000-4000-8000-000000000000'}
    assert send(http, 'Describe what I attached', [unknown]).status_code == 400
    mine = upload(http, 'mine.txt', b'secret', 'text/plain')

    bob = Client(app)
    try:
        bob.sign_up()
        assert bob.http.get(f'/api/attachments/{mine["id"]}').status_code == 404
        assert send(bob.http, 'Describe what I attached', [mine]).status_code == 400
    finally:
        bob.http.close()
    # Sent once, a file is in that message: it cannot go with another.
    first = send(http, 'Describe what I attached', [mine])
    assert first.status_code == 201
    client.wait_for_reply(first.json()['thread_id'])
    assert send(http, 'Describe what I attached', [mine]).status_code == 400
