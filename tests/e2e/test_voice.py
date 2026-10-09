"""Voice through the server (`sammy.voice`): a recording becomes words for the message box, and a reply is read aloud,
both by the speech provider the server is set up with. The provider's key stays on the server, the recording is not
kept, and without a provider the apps use the device's own speech. What the apps do with these is in
`e2e/test_frontend.py` (web) and the Mac app's own tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from conftest import App, Client
from psycopg import sql
from sites.speech import SECRET, WORDS, FakeSpeech

from sammy.voice import FAILED, MAX_AUDIO_BYTES, parts

RECORDING = b'\x1aE\xdf\xa3 a recording of someone asking for eggs \x00\xff'


@pytest.fixture
def provider() -> Iterator[FakeSpeech]:
    fake = FakeSpeech()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def app_env(provider: FakeSpeech) -> dict[str, str]:
    return {'VOICE_PROVIDER': 'openai', 'VOICE_API_KEY': 'sk-voice', 'VOICE_BASE_URL': provider.url}


def test_a_recording_becomes_words_and_a_reply_is_read_aloud(
    app: App, client: Client, provider: FakeSpeech, database_url: str, workspaces_dir: Path
) -> None:
    assert client.http.get('/api/voice').status_code == 401
    refused = client.http.post('/api/voice/transcriptions', content=RECORDING, headers={'Content-Type': 'audio/webm'})
    assert refused.status_code == 401 and not provider.heard
    client.sign_up()
    assert client.http.get('/api/voice').json() == {'transcribe': True, 'speak': True}

    heard = client.http.post(
        '/api/voice/transcriptions', content=RECORDING, headers={'Content-Type': 'audio/webm;codecs=opus'}
    )
    assert heard.status_code == 200, heard.text
    assert heard.json() == {'text': WORDS}
    [call] = provider.heard
    assert call.path == '/v1/audio/transcriptions' and call.headers['authorization'] == 'Bearer sk-voice'
    assert RECORDING in call.body and b'filename="speech.webm"' in call.body

    reply = 'Your cart has eggs, milk and bread. ' * 250  # longer than one provider call reads
    spoken = client.http.post('/api/voice/speech', json={'text': reply})
    assert spoken.status_code == 200, spoken.text
    assert spoken.headers['content-type'] == 'audio/mpeg' and spoken.headers['cache-control'] == 'no-store'
    pieces = parts(reply)
    assert len(pieces) > 1 and spoken.content == b''.join(b'MP3:' + piece.encode() for piece in pieces)
    assert [c.path for c in provider.heard[1:]] == ['/v1/audio/speech'] * len(pieces)

    # The recording went to the provider and nowhere else: no file, no attachment, nothing in the database.
    with psycopg.connect(database_url) as connection:
        assert connection.execute('SELECT count(*) FROM sammy.attachments').fetchone() == (0,)
        tables = connection.execute(
            'SELECT table_schema, table_name, column_name FROM information_schema.columns'
            " WHERE data_type = 'bytea' AND table_schema IN ('sammy', 'dbos')"
        ).fetchall()
        for schema, table, column in tables:
            query = sql.SQL('SELECT count(*) FROM {} WHERE position(%s::bytea in {}) > 0').format(
                sql.Identifier(schema, table), sql.Identifier(column)
            )
            found = connection.execute(query, [RECORDING]).fetchone()
            assert found == (0,), (schema, table, column)
    assert not [path for path in workspaces_dir.rglob('*') if path.is_file()]
    assert 'sk-voice' not in app.log.read_text()


def test_what_is_not_a_recording_or_cannot_be_heard_is_refused(app: App, client: Client, provider: FakeSpeech) -> None:
    client.sign_up()

    def transcribe(data: bytes, content_type: str) -> tuple[int, str]:
        response = client.http.post('/api/voice/transcriptions', content=data, headers={'Content-Type': content_type})
        return response.status_code, response.json()['detail']

    # A form on another site can post text with the user's cookie, never audio.
    assert transcribe(RECORDING, 'text/plain') == (415, 'send audio')
    assert transcribe(RECORDING, 'audio/aac')[0] == 415  # audio no provider tells apart
    assert transcribe(b'', 'audio/webm') == (400, 'That recording is empty.')
    assert transcribe(b'\0' * (MAX_AUDIO_BYTES + 1), 'audio/webm')[0] == 413
    assert client.http.post('/api/voice/speech', content='{"text": "hi"}').status_code == 415
    assert client.http.post('/api/voice/speech', json={'text': '  '}).status_code == 422
    assert client.http.post('/api/voice/speech', json={'text': 'x' * 20_001}).status_code == 422
    assert not provider.heard

    provider.failing = True
    assert transcribe(RECORDING, 'audio/mp4') == (502, FAILED)
    failed = client.http.post('/api/voice/speech', json={'text': 'Your cart is ready.'})
    assert (failed.status_code, failed.json()) == (502, {'detail': FAILED})
    assert SECRET not in app.log.read_text()


@pytest.mark.parametrize('app_env', [{}])
def test_without_a_provider_the_apps_use_the_devices_own_speech(app: App, client: Client) -> None:
    client.sign_up()
    assert client.http.get('/api/voice').json() == {'transcribe': False, 'speak': False}
    heard = client.http.post('/api/voice/transcriptions', content=RECORDING, headers={'Content-Type': 'audio/webm'})
    assert heard.status_code == 404
    assert client.http.post('/api/voice/speech', json={'text': 'Your cart is ready.'}).status_code == 404
