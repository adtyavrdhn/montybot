"""Voice (`sammy.voice`): the speech providers' calls, reading a long reply in parts, and the settings. The endpoints,
signed in and out, are end to end in `e2e/test_voice.py`."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from sites.speech import SECRET, WORDS, FakeSpeech
from starlette.requests import Request

from sammy.auth import refuse_non_audio
from sammy.settings import Settings
from sammy.voice import ElevenLabsSpeech, OpenAISpeech, SpeechError, open_speech, parts

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
def provider() -> Iterator[FakeSpeech]:
    fake = FakeSpeech()
    fake.start()
    yield fake
    fake.stop()


@pytest.mark.parametrize(
    ('make', 'path', 'key_header', 'key', 'model'),
    [
        (OpenAISpeech, '/v1/audio/transcriptions', 'authorization', 'Bearer sk-test', b'gpt-4o-mini-transcribe'),
        (ElevenLabsSpeech, '/v1/speech-to-text', 'xi-api-key', 'sk-test', b'scribe_v1'),
    ],
)
async def test_a_recording_is_sent_in_its_own_format_and_comes_back_as_words(
    provider: FakeSpeech,
    make: type[OpenAISpeech | ElevenLabsSpeech],
    path: str,
    key_header: str,
    key: str,
    model: bytes,
) -> None:
    speech = make('sk-test', base_url=provider.url)
    try:
        assert await speech.transcribe(b'\x00recorded audio\xff', 'audio/mp4') == WORDS
    finally:
        await speech.aclose()
    [heard] = provider.heard
    assert heard.path == path
    assert heard.headers[key_header] == key
    assert heard.headers['content-type'].startswith('multipart/form-data')
    assert b'filename="speech.mp4"' in heard.body  # the providers tell the format by the name
    assert b'\x00recorded audio\xff' in heard.body and model in heard.body


async def test_openai_reads_text_aloud_in_the_chosen_voice(provider: FakeSpeech) -> None:
    speech = OpenAISpeech('sk-test', voice='nova', base_url=provider.url)
    try:
        assert await speech.speak('Your cart is ready.') == b'MP3:Your cart is ready.'
    finally:
        await speech.aclose()
    [heard] = provider.heard
    assert heard.path == '/v1/audio/speech'
    assert json.loads(heard.body) == {
        'model': 'gpt-4o-mini-tts',
        'voice': 'nova',
        'input': 'Your cart is ready.',
        'response_format': 'mp3',
    }


async def test_elevenlabs_reads_text_aloud_in_the_chosen_voice(provider: FakeSpeech) -> None:
    speech = ElevenLabsSpeech('sk-test', voice='voice123', base_url=provider.url)
    try:
        assert await speech.speak('Your cart is ready.') == b'MP3:Your cart is ready.'
    finally:
        await speech.aclose()
    [heard] = provider.heard
    assert heard.path == '/v1/text-to-speech/voice123?output_format=mp3_44100_128'
    assert heard.headers['xi-api-key'] == 'sk-test'
    assert json.loads(heard.body) == {'text': 'Your cart is ready.', 'model_id': 'eleven_flash_v2_5'}


@pytest.mark.parametrize('make', [OpenAISpeech, ElevenLabsSpeech])
async def test_a_provider_that_fails_is_an_error_without_its_words(
    provider: FakeSpeech, make: type[OpenAISpeech | ElevenLabsSpeech]
) -> None:
    provider.failing = True
    speech = make('sk-test', base_url=provider.url)
    try:
        with pytest.raises(SpeechError) as failed:
            await speech.transcribe(b'audio', 'audio/webm')
        assert '500' in str(failed.value) and SECRET not in str(failed.value)
        with pytest.raises(SpeechError):
            await speech.speak('Hello')
    finally:
        await speech.aclose()


async def test_a_provider_out_of_reach_is_an_error() -> None:
    speech = OpenAISpeech('sk-test', base_url='http://127.0.0.1:9/v1')
    try:
        with pytest.raises(SpeechError):
            await speech.speak('Hello')
    finally:
        await speech.aclose()


def test_a_long_reply_is_read_in_parts_that_end_at_sentences() -> None:
    sentence = 'Eggs are in your cart at the usual store. '
    text = sentence * 300  # about 12,600 characters
    pieces = parts(text)
    assert len(pieces) == 4
    assert all(len(piece) <= 4_000 and piece.endswith('store.') for piece in pieces)
    assert ' '.join(pieces).split() == text.split()  # nothing lost, nothing read twice


def test_parts_end_at_a_word_without_sentences_and_anywhere_without_words() -> None:
    words = parts('word ' * 1_000, size=100)
    assert all(len(piece) <= 100 and piece.split() == ['word'] * len(piece.split()) for piece in words)
    assert parts('x' * 250, size=100) == ['x' * 100, 'x' * 100, 'x' * 50]
    assert parts('  Short.  ') == ['Short.']
    assert parts('   ') == []


def settings(**voice: str) -> Settings:
    return Settings.model_validate({'database_url': 'x', 'session_secret': 's', 'encryption_key': 'k', **voice})


async def test_the_provider_comes_from_the_settings() -> None:
    assert open_speech(settings()) is None
    assert open_speech(settings(voice_provider='', voice_api_key='')) is None  # as `.env.example` leaves them
    with pytest.raises(ValueError, match='VOICE_API_KEY'):
        open_speech(settings(voice_provider='openai'))
    for name, kind in (('openai', OpenAISpeech), ('elevenlabs', ElevenLabsSpeech)):
        speech = open_speech(settings(voice_provider=name, voice_api_key='sk-test'))
        assert isinstance(speech, kind)
        await speech.aclose()


@pytest.mark.parametrize(
    ('content_type', 'refused'),
    [
        ('audio/webm;codecs=opus', False),
        ('Audio/MP4', False),
        ('text/plain', True),  # what a form on another site can send
        ('multipart/form-data; boundary=x', True),
        ('', True),
    ],
)
def test_only_audio_can_be_posted_as_a_recording(content_type: str, refused: bool) -> None:
    request = Request({'type': 'http', 'method': 'POST', 'headers': [(b'content-type', content_type.encode())]})
    assert (refuse_non_audio(request) is not None) == refused
