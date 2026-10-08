"""Files in chats: what the user attaches to a message, and what Sammy shares back with its reply.

```
the composer: a file dropped, pasted or picked
  POST /api/attachments (the bytes)                 `upload`: a row with no run yet; the user can still drop it
  POST /api/threads/<id>/messages {attachments}     `attach`: the rows join the message's run
run_thread (sammy.workflows)
  step run.start                                    `into_workspace`: each file saved in /work/uploads for the code
  agent.run([text, note, image | PDF | text, ...])  `with_files`: the model sees what it can; each note names the path
  step run.finish                                   `without_files`: the history keeps the notes, not the bytes
the next run of the chat                            `with_files` again: earlier files come back while they fit
share_file(path)                                    a row of Sammy's, shown with its reply
```

The bytes stay in Postgres, where they are deleted with the chat, so the stored history (and what DBOS records of
each run's start) holds only a short note per file, never base64. A file's note says where the run's code finds it, so
a file the model is not shown (a spreadsheet, or one over the budget) can still be read with `run_python`.
"""

from __future__ import annotations

import asyncio
import io
import mimetypes
import posixpath
import re
import uuid
import warnings
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any, cast

from dbos import DBOS
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic_ai import FunctionToolset, RunContext
from pydantic_ai.messages import BinaryContent, ModelMessage, ModelRequest, TextContent, UserContent, UserPromptPart

from sammy.db import Connection, Pool
from sammy.deps import RunDeps
from sammy.models import Attachment, AttachmentKind
from sammy.resources import Resources, current
from sammy.workspaces import MAX_DOWNLOAD_BYTES, UPLOADS, VIRTUAL_ROOT, FileTooLarge, download_name, save_in

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES = 10
"""The most files one message may carry."""
MAX_MESSAGE_BYTES = 50 * 1024 * 1024
"""The most all of one message's files may hold together."""

MODEL_IMAGE_TYPES = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'GIF': 'image/gif', 'WEBP': 'image/webp'}
"""The image formats every model we use can see, by Pillow's name for them."""
MODEL_IMAGE_SIDE = 1568
"""Anthropic scales a longer image down to this anyway; sending it smaller saves bytes and gives the same tokens."""
MODEL_IMAGE_BYTES = 3_500_000
"""Under Anthropic's 5 MB per image once base64 adds a third."""
MAX_IMAGE_PIXELS = 100_000_000
"""Pillow refuses to open a larger image: a small file can unpack to gigabytes."""
MODEL_PDF_BYTES = 10 * 1024 * 1024
MODEL_PDF_PAGES = 100
"""Anthropic's limit per PDF; a longer one is read with code instead."""
MODEL_TEXT_CHARS = 100_000
"""About 25k tokens: the model reads the start of a longer text file, and the rest with code."""
MODEL_BUDGET = 16 * 1024 * 1024
"""The most file bytes one model request carries, under Anthropic's 32 MB per request with base64 and the rest."""

TEXT_TYPES = {
    'application/json',
    'application/xml',
    'application/x-yaml',
    'application/yaml',
    'application/toml',
    'application/javascript',
    'application/x-sh',
    'application/sql',
    'application/x-ndjson',
    'image/svg+xml',
}
TEXT_EXTENSIONS = {
    'txt', 'md', 'markdown', 'csv', 'tsv', 'json', 'jsonl', 'ndjson', 'xml', 'yaml', 'yml', 'toml', 'ini', 'cfg', 'conf',
    'log', 'html', 'htm', 'css', 'js', 'mjs', 'ts', 'tsx', 'jsx', 'py', 'rb', 'go', 'rs', 'java', 'kt', 'swift', 'c',
    'h', 'cc', 'cpp', 'hpp', 'cs', 'php', 'sh', 'bash', 'zsh', 'sql', 'r', 'tex', 'rst', 'svg', 'ics', 'vcf', 'srt',
    'vtt', 'env', 'gitignore', 'dockerfile',
}  # fmt: skip
MEDIA_TYPE = re.compile(r'^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,127}$')

COLUMNS = 'id, name, media_type, kind, size, path'

INSTRUCTIONS = f"""\
Files the user attaches to a message are saved in `{UPLOADS}`, and their message names each one with its path. You
see images, PDFs and text files directly while they fit; anything else (spreadsheets, Word documents, archives) or a
file you were not shown, read from its path with your code tools.
To give the user a file (one you made, or one your browser downloaded), call `share_file` with its path: it appears
with your reply, for them to open or save. Share files rather than pasting long contents into your reply."""


class AttachmentGone(ValueError):
    """An attachment of the message is not the user's upload waiting to be sent: unknown, sent already, or deleted."""


class TooMuch(ValueError):
    """The message's files are too many or too large together."""


# --- what a file is ---


def media_type_of(name: str, declared: str) -> str:
    """The type the user's app declared, unless it is missing or says nothing; else a guess from the name."""
    declared = declared.split(';')[0].strip().lower()
    if MEDIA_TYPE.fullmatch(declared) and declared != 'application/octet-stream':
        return declared
    return mimetypes.guess_type(name, strict=False)[0] or 'application/octet-stream'


def inspect(name: str, declared: str, data: bytes) -> tuple[str, AttachmentKind, bytes | None]:
    """The file's media type, how the model reads it, and an image made small enough for the model (None if the file
    goes as it is). Decided by the bytes, not the name: a model is never sent an image it cannot open."""
    media_type = media_type_of(name, declared)
    if data.startswith(b'%PDF-'):
        return 'application/pdf', 'pdf', None
    image = _image(data)
    if image is not None:
        return image
    if _is_text(name, media_type, data):
        return media_type, 'text', None
    return media_type, 'file', None


def _image(data: bytes) -> tuple[str, AttachmentKind, bytes | None] | None:
    try:
        with warnings.catch_warnings():  # Pillow warns of a large image as it opens; the size is checked here
            warnings.simplefilter('ignore', Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(data))
        with image:
            if image.format not in MODEL_IMAGE_TYPES:
                return None
            media_type = MODEL_IMAGE_TYPES[image.format]
            if image.width * image.height > MAX_IMAGE_PIXELS:
                return media_type, 'file', None
            if max(image.size) <= MODEL_IMAGE_SIDE and len(data) <= MODEL_IMAGE_BYTES:
                image.verify()  # a truncated or corrupt image would fail the model call
                return media_type, 'image', None
            return media_type, 'image', _smaller(image)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, SyntaxError):
        return None


def _smaller(image: Image.Image) -> bytes:
    """The image within `MODEL_IMAGE_SIDE`, the right way up (a phone's photo is often stored sideways): PNG if it
    has transparency, else JPEG. An animated GIF keeps its first frame."""
    upright = ImageOps.exif_transpose(image)
    upright.thumbnail((MODEL_IMAGE_SIDE, MODEL_IMAGE_SIDE))
    out = io.BytesIO()
    if upright.mode in ('RGBA', 'LA', 'PA') or (upright.mode == 'P' and 'transparency' in upright.info):
        upright.convert('RGBA').save(out, format='PNG', optimize=True)
    else:
        upright.convert('RGB').save(out, format='JPEG', quality=85)
    return out.getvalue()


def _is_text(name: str, media_type: str, data: bytes) -> bool:
    extension = name.rsplit('.', 1)[-1].lower() if '.' in name else name.lower()
    if not (media_type.startswith('text/') or media_type in TEXT_TYPES or extension in TEXT_EXTENSIONS):
        return False
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return False
    return '\x00' not in text


def _pdf_pages(data: bytes) -> int:
    """Page objects written out in the PDF; 0 when they are packed in object streams, which hides them."""
    return len(re.findall(rb'/Type\s*/Page(?![a-zA-Z])', data))


# --- the table ---


async def upload(connection: Connection, user_id: str, name: str, declared: str, data: bytes) -> Attachment:
    """Keep a file the user is about to send. Uploads never sent go after a day."""
    name = download_name(name)
    media_type, kind, for_model = await asyncio.to_thread(inspect, name, declared, data)
    attachment = Attachment(id=str(uuid.uuid4()), name=name, media_type=media_type, kind=kind, size=len(data))
    await connection.execute(
        "DELETE FROM sammy.attachments WHERE run_id IS NULL AND created_at < now() - interval '1 day'"
    )
    await connection.execute(
        'INSERT INTO sammy.attachments (id, user_id, sender, name, media_type, kind, size, data, for_model) '
        "VALUES (%s, %s, 'user', %s, %s, %s, %s, %s, %s)",
        (attachment.id, user_id, name, media_type, kind, len(data), data, for_model),
    )
    return attachment


async def attach(connection: Connection, user_id: str, run_id: str, ids: Sequence[str]) -> list[str]:
    """The user's uploads join their message's run; returns their names, in order. Call in the transaction that made
    the run."""
    if not ids:
        return []
    if len(ids) > MAX_FILES:
        raise TooMuch(f'Attach up to {MAX_FILES} files to one message.')
    cursor = await connection.execute(
        'UPDATE sammy.attachments AS a SET run_id = %s, position = given.position '
        'FROM unnest(%s::uuid[]) WITH ORDINALITY AS given (id, position) '
        "WHERE a.id = given.id AND a.user_id = %s AND a.run_id IS NULL AND a.sender = 'user' "
        'RETURNING a.id, a.name, a.size',
        (run_id, list(dict.fromkeys(ids)), user_id),
    )
    rows = {str(row['id']): row for row in await cursor.fetchall()}
    if len(rows) != len(set(ids)):
        raise AttachmentGone('One of your files is no longer here. Remove it and attach it again.')
    if sum(row['size'] for row in rows.values()) > MAX_MESSAGE_BYTES:
        raise TooMuch(f'The files of one message can be up to {MAX_MESSAGE_BYTES >> 20} MB together.')
    return [rows[i]['name'] for i in dict.fromkeys(ids)]


async def files_of_runs(connection: Connection, user_id: str, run_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Each run's files, by run id: `{'user': [...], 'sammy': [...]}` as JSON, in the order they came."""
    cursor = await connection.execute(
        f'SELECT run_id, sender, {COLUMNS} FROM sammy.attachments '
        'WHERE user_id = %s AND run_id = ANY(%s::uuid[]) ORDER BY position, created_at, id',
        (user_id, list(run_ids)),
    )
    found: dict[str, dict[str, Any]] = {}
    for row in await cursor.fetchall():
        found.setdefault(str(row['run_id']), {'user': [], 'sammy': []})[row['sender']].append(_from(row).json())
    return found


async def users_files(connection: Connection, run_id: str) -> list[Attachment]:
    """The files the user attached to the run's message."""
    cursor = await connection.execute(
        f"SELECT {COLUMNS} FROM sammy.attachments WHERE run_id = %s AND sender = 'user' ORDER BY position, created_at, id",
        (run_id,),
    )
    return [_from(row) for row in await cursor.fetchall()]


async def read(connection: Connection, user_id: str, attachment_id: str) -> tuple[Attachment, bytes] | None:
    cursor = await connection.execute(
        f'SELECT {COLUMNS}, data FROM sammy.attachments WHERE id = %s AND user_id = %s', (attachment_id, user_id)
    )
    row = await cursor.fetchone()
    return None if row is None else (_from(row), bytes(row['data']))


def _from(row: dict[str, Any]) -> Attachment:
    return Attachment(
        id=str(row['id']),
        name=row['name'],
        media_type=row['media_type'],
        kind=row['kind'],
        size=row['size'],
        path=row['path'],
    )


# --- the run ---


async def into_workspace(resources: Resources, user_id: str, run_id: str) -> list[Attachment]:
    """Save each file of the run's message in the user's `/work/uploads`, for the run's code; returns them with their
    paths. Safe to repeat: a file saved already keeps its path."""
    async with resources.pool.connection() as connection:
        cursor = await connection.execute(
            'SELECT id, name, data FROM sammy.attachments '
            "WHERE run_id = %s AND sender = 'user' AND path IS NULL ORDER BY position, created_at, id",
            (run_id,),
        )
        unsaved = await cursor.fetchall()
    if unsaved:
        files = resources.workspaces.files(user_id)
        for row in unsaved:
            path = await save_in(files, UPLOADS, row['name'], bytes(row['data']))
            async with resources.pool.connection() as connection:
                await connection.execute('UPDATE sammy.attachments SET path = %s WHERE id = %s', (path, row['id']))
    async with resources.pool.connection() as connection:
        return await users_files(connection, run_id)


def note(attachment: Attachment) -> TextContent:
    """What the model reads of a file in the stored history, and before the file itself when it is shown."""
    where = f', saved at {attachment.path}' if attachment.path else ''
    return TextContent(
        f'[The user attached "{attachment.name}" ({attachment.media_type}, {shown_size(attachment.size)}){where}.]',
        metadata={'attachment': attachment.id},
    )


def prompt(text: str, files: Sequence[Attachment]) -> str | list[UserContent]:
    """The user's message as stored: their text, and a note for each file. Just the text when there are no files."""
    if not files:
        return text
    return [*([text] if text else []), *(note(file) for file in files)]


def without_files(messages: Sequence[ModelMessage]) -> list[ModelMessage]:
    """The messages to store: the files' notes stay, what `with_files` put in goes."""

    def take_out(content: list[UserContent]) -> list[UserContent]:
        return [item for item in content if not _shown_file(item)]

    return [_mapped(message, take_out) for message in messages]


async def with_files(pool: Pool, user_id: str, messages: Sequence[ModelMessage]) -> list[ModelMessage]:
    """The messages with each file noted in them put in after its note, as the model can take it: newest first, until
    `MODEL_BUDGET` is spent. A file deleted since, or over the budget, is left as its note."""
    ids = [attachment_id for c in _user_contents(messages) if (attachment_id := _note_id(c)) is not None]
    if not ids:
        return list(messages)
    async with pool.connection() as connection:
        cursor = await connection.execute(
            'SELECT id, name, kind, media_type, path, coalesce(for_model, data) AS data FROM sammy.attachments '
            'WHERE user_id = %s AND id = ANY(%s::uuid[])',
            (user_id, ids),
        )
        rows = {str(row['id']): row for row in await cursor.fetchall()}
    budget = MODEL_BUDGET
    shown: dict[str, list[UserContent]] = {}
    for attachment_id in reversed(ids):
        row = rows.get(attachment_id)
        parts = _for_model(row) if row is not None else []
        size = sum(_size(part) for part in parts)
        if parts and size <= budget:
            shown[attachment_id] = parts
            budget -= size

    def put_in(content: list[UserContent]) -> list[UserContent]:
        out: list[UserContent] = []
        for item in content:
            if _shown_file(item):
                continue  # the history was stored with it (not by us): put in afresh, within the budget
            out.append(item)
            if (attachment_id := _note_id(item)) is not None:
                out.extend(shown.get(attachment_id, []))
        return out

    return [_mapped(message, put_in) for message in messages]


def _for_model(row: dict[str, Any]) -> list[UserContent]:
    data: bytes = bytes(row['data'])
    marker = f'attachment:{row["id"]}'
    match row['kind']:
        case 'image' if len(data) <= MODEL_IMAGE_BYTES:
            # Made smaller, an image may have become a PNG or a JPEG.
            media_type = 'image/png' if data.startswith(b'\x89PNG') else row['media_type']
            media_type = 'image/jpeg' if data.startswith(b'\xff\xd8\xff') else media_type
            return [BinaryContent(data, media_type=media_type, identifier=marker)]
        case 'pdf' if len(data) <= MODEL_PDF_BYTES and _pdf_pages(data) <= MODEL_PDF_PAGES:
            return [BinaryContent(data, media_type='application/pdf', identifier=marker)]
        case 'text':
            text = data.decode('utf-8-sig', errors='replace')
            more = ''
            if len(text) > MODEL_TEXT_CHARS:
                text = text[:MODEL_TEXT_CHARS]
                more = f'\n[... the rest is in {row["path"] or "the file"}]'
            body = f'<file name="{row["name"]}">\n{text}{more}\n</file>'
            return [TextContent(body, metadata={'attachment_body': str(row['id'])})]
        case _:
            return []


def _size(item: UserContent) -> int:
    if isinstance(item, BinaryContent):
        return len(item.data)
    return len(item.content.encode()) if isinstance(item, TextContent) else 0


def _marks(item: UserContent) -> dict[str, Any]:
    """What we noted on a text item (`TextContent.metadata`, which the model does not see)."""
    metadata: object = item.metadata if isinstance(item, TextContent) else None
    return cast(dict[str, Any], metadata) if isinstance(metadata, dict) else {}


def _note_id(item: UserContent) -> str | None:
    found = _marks(item).get('attachment')
    return found if isinstance(found, str) else None


def _shown_file(item: UserContent) -> bool:
    if isinstance(item, BinaryContent):
        return item.identifier.startswith('attachment:')
    return 'attachment_body' in _marks(item)


def _user_contents(messages: Sequence[ModelMessage]) -> list[UserContent]:
    return [
        item
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and not isinstance(part.content, str)
        for item in part.content
    ]


def _mapped(message: ModelMessage, change: Callable[[list[UserContent]], list[UserContent]]) -> ModelMessage:
    """The message with the content of each of its user prompts changed by `change`."""
    if not isinstance(message, ModelRequest):
        return message
    if not any(isinstance(p, UserPromptPart) and not isinstance(p.content, str) for p in message.parts):
        return message
    parts = [
        replace(part, content=change(list(part.content)))
        if isinstance(part, UserPromptPart) and not isinstance(part.content, str)
        else part
        for part in message.parts
    ]
    return replace(message, parts=parts)


def shown_size(size: int) -> str:
    if size < 1024:
        return f'{size} bytes'
    if size < 1024 * 1024:
        return f'{size / 1024:.1f} KB'
    return f'{size / 1024 / 1024:.1f} MB'


# --- Sammy gives the user a file ---

file_tools: FunctionToolset[RunDeps] = FunctionToolset(id='files')


@file_tools.tool
async def share_file(ctx: RunContext[RunDeps], path: str) -> str:
    """Give the user a file from their files, such as a report you made or something your browser downloaded: it
    appears with your reply, for them to open or save. Up to 20 MB. `path` as your code sees it, under /work."""
    run = ctx.deps.run
    path = posixpath.normpath(path if path.startswith('/') else f'{VIRTUAL_ROOT}/{path}')
    # The same id when a replayed run shares again: the call's id comes from the recorded model response.
    attachment_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'sammy:{run.id}:{ctx.tool_call_id}'))

    async def step() -> str:
        resources = current()
        try:
            data = await resources.workspaces.files(run.user_id).read_result(path)
        except FileTooLarge:
            return f'{path} is over {MAX_DOWNLOAD_BYTES >> 20} MB, too large to share. Make a smaller file.'
        except (OSError, ValueError):
            return f'There is no file at {path} to share. Check the path with your code tools.'
        name = download_name(path)
        media_type, kind, _ = await asyncio.to_thread(inspect, name, '', data)
        async with resources.pool.connection() as connection:
            await connection.execute(
                'INSERT INTO sammy.attachments (id, user_id, run_id, sender, name, media_type, kind, size, data, path) '
                "VALUES (%s, %s, %s, 'sammy', %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (attachment_id, run.user_id, run.id, name, media_type, kind, len(data), data, path),
            )
        return f'Shared {name} with the user: it appears with your reply.'

    return await DBOS.run_step_async({'name': 'files.share'}, step)
