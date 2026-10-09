"""A fake Slack Web API (`SlackAPI`, on `PlatformServer`), recorded Events API and interactivity payloads
(`tests/fixtures/slack/`), and Slack's request signing, for testing `sammy.channels.slack` offline with no real token.

```
pytest process                                          app process
  signed(...) + POST /api/channels/slack/webhook  --->  SlackChannel (CHANNEL_BACKENDS=fake_slack:new_channel)
  SlackAPI: /api/<method>, /files-pri/..., /upload/ <---  chat.postMessage, chat.update, files.info, file uploads
```
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from fake_channel import Call, PlatformServer, Reply, reply

from sammy.channels.slack import SIGNATURE, TIMESTAMP, SlackChannel
from sammy.channels.slack_mrkdwn import unescape
from sammy.settings import Settings

TOKEN = 'fake-bot-token-for-tests-only'
"""Not shaped like a real `xoxb-` token, so secret scanners leave it alone."""
SIGNING_SECRET = 'fake-slack-signing-secret-0123456789'
PAYLOADS = Path(__file__).parent / 'fixtures' / 'slack'
METHODS = (
    'chat.postMessage',
    'chat.update',
    'files.info',
    'files.getUploadURLExternal',
    'files.completeUploadExternal',
)


def payload(name: str) -> dict[str, object]:
    """A recorded payload (`fixtures/slack/<name>.json`), a fresh copy to change."""
    loaded = json.loads((PAYLOADS / f'{name}.json').read_text())
    assert isinstance(loaded, dict)
    return copy.deepcopy(loaded)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]


def signed(body: bytes, *, secret: str = SIGNING_SECRET, timestamp: int | None = None) -> dict[str, str]:
    """Slack's signature headers for `body`, signed now (or at `timestamp`)."""
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    digest = hmac.new(secret.encode(), f'v0:{stamp}:'.encode() + body, hashlib.sha256).hexdigest()
    return {TIMESTAMP: stamp, SIGNATURE: f'v0={digest}'}


def form(interaction: dict[str, object]) -> bytes:
    """An interactivity payload as Slack posts it: form-encoded `payload=<json>`."""
    return urlencode({'payload': json.dumps(interaction)}).encode()


def fields(call: Call) -> dict[str, str]:
    """A form-encoded call's fields."""
    return {key: values[0] for key, values in parse_qs(call.body.decode()).items()}


class SlackAPI(PlatformServer):
    """Answers each Web API method Sammy calls as Slack documents it, checking the bot token; serves private files
    and takes uploads."""

    def __init__(self) -> None:
        self.posted: list[str] = []
        """The `ts` Slack gave each message, in the order of `called('chat.postMessage')`."""
        self.uploads: dict[str, bytes] = {}
        super().__init__({('POST', f'/api/{method}'): self._answer for method in METHODS})

    def _answer(self, call: Call) -> Reply:
        if call.headers.get('authorization') != f'Bearer {TOKEN}':
            return reply({'ok': False, 'error': 'invalid_auth'})
        method = call.path.removeprefix('/api/')
        if method in ('chat.postMessage', 'chat.update'):
            body = call.json()
            ts = str(body.get('ts') or f'{1767226001 + len(self.posted)}.000100')
            if method == 'chat.postMessage':
                self.posted.append(ts)
            return reply({'ok': True, 'channel': body['channel'], 'ts': ts, 'message': {'text': body['text']}})
        if method == 'files.info':
            file_id = fields(call)['file']
            if ('GET', f'/files-pri/T0SAMMY01-{file_id}/download/file') not in self.routes:
                return reply({'ok': False, 'error': 'file_not_found'})
            url = f'{self.url}/files-pri/T0SAMMY01-{file_id}/download/file'
            return reply({'ok': True, 'file': {'id': file_id, 'url_private': url, 'url_private_download': url}})
        if method == 'files.getUploadURLExternal':
            file_id = f'F0UPLOAD{len(self.uploads) + 1:03d}'
            self.uploads[file_id] = b''
            self.routes[('POST', f'/upload/v1/{file_id}')] = self._upload
            return reply({'ok': True, 'upload_url': f'{self.url}/upload/v1/{file_id}', 'file_id': file_id})
        shared = json.loads(fields(call)['files'])
        return reply({'ok': True, 'files': shared})

    def _upload(self, call: Call) -> Reply:
        self.uploads[call.path.rsplit('/', 1)[1]] = call.body
        return 200, b'OK - 23', 'text/plain'

    def serve_file(self, file_id: str, data: bytes) -> None:
        """A file Pat shared, which only the bot token may download."""

        def download(call: Call) -> Reply:
            if call.headers.get('authorization') != f'Bearer {TOKEN}':
                return 302, b'', 'text/html'
            return 200, data, 'application/octet-stream'

        self.routes[('GET', f'/files-pri/T0SAMMY01-{file_id}/download/file')] = download

    def called(self, method: str) -> list[Call]:
        with self.lock:
            return [c for c in self.calls if c.path == f'/api/{method}']

    def ts_of(self, call: Call) -> str:
        with self.lock:
            return self.posted[[c for c in self.calls if c.path == '/api/chat.postMessage'].index(call)]

    def messages(self, channel: str) -> list[dict[str, object]]:
        """Each `chat.postMessage` body to `channel`, in order."""
        return [body for c in self.called('chat.postMessage') if (body := c.json())['channel'] == channel]

    def texts(self, channel: str) -> list[str]:
        """What the user sees of each message: Slack's escapes undone."""
        return [unescape(str(m['text'])) for m in self.messages(channel)]


def new_channel(settings: Settings) -> SlackChannel | None:
    """The real adapter, pointed at `SlackAPI` (FAKE_SLACK_URL) with the fake token and signing secret."""
    url = os.environ.get('FAKE_SLACK_URL')
    return SlackChannel(TOKEN, SIGNING_SECRET, api_url=f'{url}/api') if url else None
