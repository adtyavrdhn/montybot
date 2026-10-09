"""Voice: dictating messages and hearing replies (#137).

The web and Mac apps listen and speak with the device's own speech recognition and voices where they can. With
`VOICE_PROVIDER` set, the server also turns recorded audio into text, for a browser without speech recognition, and
reads replies aloud in the provider's voice. The provider's key stays here, never in an app.

Audio is never stored: it is held in memory for the one request, sent to the provider and dropped. Spans record the
provider, sizes and timings, never the audio, the words heard or the text read aloud (`sammy/observability.py`).
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Protocol

import httpx
from pydantic import BaseModel, StringConstraints
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from sammy import auth
from sammy.models import User
from sammy.observability import timing
from sammy.resources import Resources
from sammy.settings import Settings

logger = logging.getLogger(__name__)

MAX_AUDIO_BYTES = 10 * 1024 * 1024
"""About ten minutes of compressed speech. Both providers take more (25 MB and up)."""
MAX_SPEECH_CHARS = 20_000
"""As long as a message may be."""
PART_CHARS = 4_000
"""The most text one provider call reads aloud: OpenAI takes 4,096 characters."""
AUDIO_TYPES = {
    'audio/webm': 'webm',
    'audio/ogg': 'ogg',
    'audio/mp4': 'mp4',
    'audio/m4a': 'm4a',
    'audio/x-m4a': 'm4a',
    'audio/mpeg': 'mp3',
    'audio/wav': 'wav',
    'audio/x-wav': 'wav',
    'audio/wave': 'wav',
    'audio/flac': 'flac',
}
"""What browsers and the Mac app record, and the file extension the providers tell the format by."""
FAILED = 'Speech is not working right now. Please try again in a little while.'


class SpeechError(Exception):
    """The provider failed, or refused the request. The message is for the logs, not for the user."""


class Speech(Protocol):
    name: str
    """The provider, for spans."""

    async def transcribe(self, audio: bytes, media_type: str) -> str:
        """The words in `audio`, one of `AUDIO_TYPES`."""
        ...

    async def speak(self, text: str) -> bytes:
        """`text`, at most `PART_CHARS` long, read aloud as MP3."""
        ...

    async def aclose(self) -> None: ...


class OpenAISpeech:
    """OpenAI's audio API, or a server that speaks it at `base_url`."""

    name = 'openai'
    TRANSCRIBE_MODEL = 'gpt-4o-mini-transcribe'
    SPEECH_MODEL = 'gpt-4o-mini-tts'

    def __init__(self, api_key: str, *, voice: str | None = None, base_url: str | None = None) -> None:
        self.voice = voice or 'alloy'
        self._http = httpx.AsyncClient(
            base_url=base_url or 'https://api.openai.com/v1',
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=60,
        )

    async def transcribe(self, audio: bytes, media_type: str) -> str:
        files = {'file': (f'speech.{AUDIO_TYPES[media_type]}', audio, media_type)}
        response = await _post(self._http, '/audio/transcriptions', files=files, data={'model': self.TRANSCRIBE_MODEL})
        return _text_of(response)

    async def speak(self, text: str) -> bytes:
        body = {'model': self.SPEECH_MODEL, 'voice': self.voice, 'input': text, 'response_format': 'mp3'}
        return (await _post(self._http, '/audio/speech', json=body)).content

    async def aclose(self) -> None:
        await self._http.aclose()


class ElevenLabsSpeech:
    """ElevenLabs: Scribe for speech to text, and a voice of the user's account (`voice` is its id)."""

    name = 'elevenlabs'
    TRANSCRIBE_MODEL = 'scribe_v1'
    SPEECH_MODEL = 'eleven_flash_v2_5'
    DEFAULT_VOICE = 'JBFqnCBsd6RMkjVDRZzb'
    """George, one of the voices every account has."""

    def __init__(self, api_key: str, *, voice: str | None = None, base_url: str | None = None) -> None:
        self.voice = voice or self.DEFAULT_VOICE
        self._http = httpx.AsyncClient(
            base_url=base_url or 'https://api.elevenlabs.io/v1', headers={'xi-api-key': api_key}, timeout=60
        )

    async def transcribe(self, audio: bytes, media_type: str) -> str:
        files = {'file': (f'speech.{AUDIO_TYPES[media_type]}', audio, media_type)}
        response = await _post(self._http, '/speech-to-text', files=files, data={'model_id': self.TRANSCRIBE_MODEL})
        return _text_of(response)

    async def speak(self, text: str) -> bytes:
        path = f'/text-to-speech/{self.voice}?output_format=mp3_44100_128'
        return (await _post(self._http, path, json={'text': text, 'model_id': self.SPEECH_MODEL})).content

    async def aclose(self) -> None:
        await self._http.aclose()


def open_speech(settings: Settings) -> Speech | None:
    """The configured provider, or None without `VOICE_PROVIDER`. Close it with `aclose()`."""
    if settings.voice_provider is None:
        return None
    if settings.voice_api_key is None:
        raise ValueError(f'VOICE_PROVIDER={settings.voice_provider} needs VOICE_API_KEY')
    provider = OpenAISpeech if settings.voice_provider == 'openai' else ElevenLabsSpeech
    return provider(
        settings.voice_api_key.get_secret_value(), voice=settings.voice_name, base_url=settings.voice_base_url
    )


Upload = dict[str, tuple[str, bytes, str]]
"""Multipart files: field name to (file name, bytes, media type)."""


async def _post(
    http: httpx.AsyncClient,
    path: str,
    *,
    json: dict[str, str] | None = None,
    files: Upload | None = None,
    data: dict[str, str] | None = None,
) -> httpx.Response:
    try:
        response = await http.post(path, json=json, files=files, data=data)
    except httpx.HTTPError as error:
        raise SpeechError(f'{type(error).__qualname__} calling {path}') from error
    if response.is_error:
        raise SpeechError(f'{response.status_code} from {path}')  # never the body: it can quote what was said
    return response


def _text_of(response: httpx.Response) -> str:
    try:
        text = response.json()['text']
    except (ValueError, KeyError, TypeError) as error:
        raise SpeechError('a transcription without text') from error
    if not isinstance(text, str):
        raise SpeechError('a transcription without text')
    return text.strip()


def parts(text: str, size: int = PART_CHARS) -> list[str]:
    """`text` in pieces of at most `size` characters, each read aloud by one provider call. A piece ends at the last
    line or sentence that fits, else at a word, so the voice never stops mid-sentence when it need not."""
    pieces: list[str] = []
    rest = text.strip()
    while len(rest) > size:
        window = rest[: size + 1]
        cut = max(window.rfind('\n'), *(m.end() for m in re.finditer(r'[.!?]\s', window)), 0)
        if cut < size // 2:
            cut = window.rfind(' ')
        if cut <= 0:
            cut = size
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        pieces.append(rest)
    return pieces


# --- the endpoints ---


class SpeechRequest(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_SPEECH_CHARS)]
    """Plain text, as the reply reads on screen: no Markdown."""


NO_PROVIDER = JSONResponse({'detail': 'This server has no speech provider.'}, status_code=404)


def speech_of(request: Request) -> Speech | None:
    resources: Resources = request.state.resources
    return resources.speech


@auth.signed_in
async def voice_settings(request: Request, user: User) -> Response:
    """What the server can do for the apps, which do the rest on the device."""
    available = speech_of(request) is not None
    return JSONResponse({'transcribe': available, 'speak': available})


@auth.signed_in_audio
async def transcribe(request: Request, user: User) -> Response:
    """POST recorded audio (`Content-Type` one of `AUDIO_TYPES`). Answers the words in it: `{"text": ...}`."""
    speech = speech_of(request)
    if speech is None:
        return NO_PROVIDER
    media_type = request.headers['content-type'].split(';')[0].strip().lower()
    if media_type not in AUDIO_TYPES:
        return JSONResponse({'detail': 'Send audio as WebM, Ogg, MP4, MP3, WAV or FLAC.'}, status_code=415)
    audio = bytearray()
    async for chunk in request.stream():
        audio.extend(chunk)
        if len(audio) > MAX_AUDIO_BYTES:
            return JSONResponse({'detail': 'That recording is too long. Try a shorter one.'}, status_code=413)
    if not audio:
        return JSONResponse({'detail': 'That recording is empty.'}, status_code=400)
    with timing('voice.transcribe') as span:
        span.set_attributes({'voice.provider': speech.name, 'voice.audio_bytes': len(audio)})
        try:
            text = await speech.transcribe(bytes(audio), media_type)
        except SpeechError as error:
            return failed(error)
    return JSONResponse({'text': text}, headers={'Cache-Control': 'no-store'})


@auth.signed_in
async def speak(request: Request, user: User) -> Response:
    """POST `{"text": ...}`. Answers the text read aloud, as MP3."""
    speech = speech_of(request)
    if speech is None:
        return NO_PROVIDER
    body = SpeechRequest.model_validate_json(await request.body())
    audio = bytearray()
    with timing('voice.speak') as span:
        span.set_attributes({'voice.provider': speech.name, 'voice.text_chars': len(body.text)})
        try:
            for part in parts(body.text):
                audio.extend(await speech.speak(part))  # MP3 frames play on from one part to the next
        except SpeechError as error:
            return failed(error)
    return Response(bytes(audio), media_type='audio/mpeg', headers={'Cache-Control': 'no-store'})


def failed(error: SpeechError) -> Response:
    logger.warning('The speech provider failed: %s', error)
    return JSONResponse({'detail': FAILED}, status_code=502)
