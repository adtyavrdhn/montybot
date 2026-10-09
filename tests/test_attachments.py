"""Files in chats (`sammy.attachments`): what a file is, by its bytes, and the stored history keeping notes, not
files. Uploading, the model seeing files and sharing them back are end to end, in `e2e/test_attachments.py`."""

from __future__ import annotations

import io

import pytest
from PIL import Image
from pydantic_ai.messages import BinaryContent, ModelRequest, ModelResponse, TextContent, TextPart, UserPromptPart

from sammy.attachments import MODEL_IMAGE_SIDE, inspect, note, prompt, without_files
from sammy.models import Attachment


def image(width: int, height: int, format: str = 'PNG', mode: str = 'RGB', orientation: int | None = None) -> bytes:
    out = io.BytesIO()
    made = Image.new(mode, (width, height))
    exif = made.getexif()
    if orientation:
        exif[0x0112] = orientation
    made.save(out, format=format, exif=exif)
    return out.getvalue()


@pytest.mark.parametrize(
    ('name', 'declared', 'data', 'expected'),
    [
        ('scan.pdf', 'application/octet-stream', b'%PDF-1.7\n...', ('application/pdf', 'pdf')),
        ('photo', '', image(20, 10, 'JPEG'), ('image/jpeg', 'image')),
        ('wrong-name.png', 'image/png', image(20, 10, 'WEBP'), ('image/webp', 'image')),
        ('fake.png', 'image/png', b'not an image', ('image/png', 'file')),
        ('picture.bmp', 'image/bmp', image(20, 10, 'BMP'), ('image/bmp', 'file')),  # not a format models see
        ('notes.md', '', b'# Notes', ('text/markdown', 'text')),
        ('data.json', 'application/json; charset=utf-8', b'{"a": 1}', ('application/json', 'text')),
        ('main.py', 'text/x-python', '\ufeffprint("hé")'.encode('utf-8-sig'), ('text/x-python', 'text')),
        ('latin.csv', 'text/csv', 'café'.encode('latin-1'), ('text/csv', 'file')),  # not UTF-8: read with code
        ('binary.txt', 'text/plain', b'a\x00b', ('text/plain', 'file')),
        (
            'budget.xlsx',
            '',
            b'PK\x03\x04',
            ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'file'),
        ),
        ('mystery', '', b'\x01\x02', ('application/octet-stream', 'file')),
    ],
)
def test_a_file_is_what_its_bytes_say(name: str, declared: str, data: bytes, expected: tuple[str, str]) -> None:
    media_type, kind, _ = inspect(name, declared, data)
    assert (media_type, kind) == expected


def test_a_large_image_is_made_small_enough_for_the_model_the_right_way_up() -> None:
    _, kind, smaller = inspect('photo.jpg', 'image/jpeg', image(4000, 1000, 'JPEG', orientation=6))
    assert kind == 'image' and smaller is not None
    with Image.open(io.BytesIO(smaller)) as made:
        # Orientation 6 is a phone held upright: the picture is turned to stand as it was taken.
        assert (made.format, made.size) == ('JPEG', (MODEL_IMAGE_SIDE // 4, MODEL_IMAGE_SIDE))
    _, _, smaller = inspect('logo.png', 'image/png', image(3000, 300, 'PNG', mode='RGBA'))
    assert smaller is not None
    with Image.open(io.BytesIO(smaller)) as made:
        assert made.format == 'PNG' and made.mode == 'RGBA'  # keeps its transparency
    assert inspect('small.png', '', image(800, 600))[2] is None  # sent as it is


def test_a_decompression_bomb_is_not_shown_to_the_model() -> None:
    assert inspect('bomb.png', 'image/png', image(12_000, 9_000, mode='1'))[1] == 'file'


def test_the_stored_history_keeps_the_notes_and_drops_the_files() -> None:
    photo = Attachment(
        id='a1', name='photo.png', media_type='image/png', kind='image', size=2048, path='/work/uploads/photo.png'
    )
    assert prompt('Hi', []) == 'Hi'
    stored = prompt('What is this?', [photo])
    assert stored == ['What is this?', note(photo)]
    assert isinstance(stored[1], TextContent)
    assert stored[1].content == '[The user attached "photo.png" (image/png, 2.0 KB), saved at /work/uploads/photo.png.]'
    shown = [
        *stored,
        BinaryContent(b'\x89PNG', media_type='image/png', identifier='attachment:a1'),
        TextContent('<file name="notes.md">...</file>', metadata={'attachment_body': 'a2'}),
    ]
    tool_image = BinaryContent(b'\x89PNG', media_type='image/png')  # not one of the user's files: kept
    messages = [
        ModelRequest(parts=[UserPromptPart(content=[*shown, tool_image])]),
        ModelResponse(parts=[TextPart('A squirrel.')]),
        ModelRequest(parts=[UserPromptPart(content='Thanks')]),
    ]
    kept = without_files(messages)
    first = kept[0]
    assert isinstance(first, ModelRequest) and isinstance(first.parts[0], UserPromptPart)
    assert first.parts[0].content == [*stored, tool_image]
    assert kept[1:] == messages[1:]
