"""A stand-in for the speech providers (`sammy.voice`): the OpenAI and ElevenLabs endpoints Sammy calls, on 127.0.0.1.

It records every request, answers a transcription with `WORDS` and reads text aloud as `MP3:` and the text, so a test
can tell which part of a reply each provider call read. `failing` makes every call fail, with words in the error that
must never reach the user or the logs.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WORDS = 'add eggs and milk to my list'
SECRET = 'the provider quoted what you said'


@dataclass
class Heard:
    path: str
    headers: dict[str, str]
    body: bytes


class FakeSpeech:
    def __init__(self) -> None:
        self.heard: list[Heard] = []
        self.failing = False
        self.url = ''
        self._server: ThreadingHTTPServer | None = None

    def answer(self, heard: Heard) -> tuple[int, str, bytes]:
        if self.failing:
            return 500, 'application/json', json.dumps({'error': SECRET}).encode()
        path = heard.path.split('?', 1)[0]
        if path in ('/v1/audio/transcriptions', '/v1/speech-to-text'):
            return 200, 'application/json', json.dumps({'text': f' {WORDS} '}).encode()
        if path == '/v1/audio/speech':
            return 200, 'audio/mpeg', b'MP3:' + json.loads(heard.body)['input'].encode()
        if path.startswith('/v1/text-to-speech/'):
            return 200, 'audio/mpeg', b'MP3:' + json.loads(heard.body)['text'].encode()
        return 404, 'application/json', b'{}'

    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                heard = Heard(self.path, {k.lower(): v for k, v in self.headers.items()}, body)
                fake.heard.append(heard)
                status, media_type, data = fake.answer(heard)
                self.send_response(status)
                self.send_header('Content-Type', media_type)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self._server.server_address[1]}/v1'
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
