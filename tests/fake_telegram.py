"""A fake Telegram Bot API (`TelegramAPI`, on `PlatformServer`) and recorded updates (`tests/fixtures/telegram/`), for
testing `sammy.channels.telegram` offline with no real token.

```
pytest process                                           app process
  update(...) + POST /api/channels/telegram/webhook --->  TelegramChannel (CHANNEL_BACKENDS=fake_telegram:new_channel)
  TelegramAPI: /bot<TOKEN>/<method>, /file/bot<TOKEN>/  <---  sendMessage, editMessageText, sendDocument, getFile...
```
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from fake_channel import Call, PlatformServer, Reply, reply

from sammy.channels.telegram import TelegramChannel
from sammy.channels.telegram_markdown import plain
from sammy.settings import Settings

TOKEN = '123456789:AAFakeTokenForTestsOnly_0123456789ab'
BOT = {'id': 123456789, 'is_bot': True, 'first_name': 'Sammy', 'username': 'sammy_test_bot'}
PAYLOADS = Path(__file__).parent / 'fixtures' / 'telegram'
METHODS = ('getMe', 'sendMessage', 'editMessageText', 'sendPhoto', 'sendDocument', 'getFile', 'setWebhook')


def update(name: str, update_id: int | None = None) -> dict[str, object]:
    """A recorded update (`fixtures/telegram/<name>.json`), with a fresh `update_id` if given."""
    loaded = json.loads((PAYLOADS / f'{name}.json').read_text())
    assert isinstance(loaded, dict)
    made: dict[str, object] = copy.deepcopy(loaded)  # pyright: ignore[reportUnknownArgumentType]
    if update_id is not None:
        made['update_id'] = update_id
    return made


class TelegramAPI(PlatformServer):
    """Answers each Bot API method Sammy calls, as Telegram documents it, and serves files to download."""

    def __init__(self) -> None:
        self._ids = 100
        super().__init__({('POST', f'/bot{TOKEN}/{method}'): self._answer for method in METHODS})

    def _answer(self, call: Call) -> Reply:
        method = call.path.rsplit('/', 1)[1]
        if method == 'getMe':
            return reply({'ok': True, 'result': BOT})
        if method == 'getFile':
            file_id = str(call.json()['file_id'])
            return reply({'ok': True, 'result': {'file_id': file_id, 'file_path': f'documents/{file_id}.bin'}})
        if method in ('setWebhook', 'editMessageText'):
            return reply({'ok': True, 'result': True})
        self._ids += 1
        return reply({'ok': True, 'result': {'message_id': self._ids, 'date': 1767225600, 'chat': {'id': 0}}})

    def serve_file(self, file_id: str, data: bytes) -> None:
        self.routes[('GET', f'/file/bot{TOKEN}/documents/{file_id}.bin')] = lambda call: (200, data, 'text/plain')

    def called(self, method: str) -> list[Call]:
        with self.lock:
            return [c for c in self.calls if c.path == f'/bot{TOKEN}/{method}']

    def messages(self, chat_id: int) -> list[dict[str, object]]:
        """Each `sendMessage` body to `chat_id`, in order."""
        return [body for c in self.called('sendMessage') if (body := c.json())['chat_id'] == str(chat_id)]

    def texts(self, chat_id: int) -> list[str]:
        """What the user sees of each message: the MarkdownV2 escapes taken out."""
        return [plain(str(m['text'])) for m in self.messages(chat_id)]

    def message_id(self, call: Call) -> int:
        """The id Telegram gave the message `call` sent: the calls answered in order, from 101."""
        with self.lock:
            sent = [c for c in self.calls if c.path.endswith(('/sendMessage', '/sendPhoto', '/sendDocument'))]
        return 101 + sent.index(call)


def new_channel(settings: Settings) -> TelegramChannel | None:
    """The real adapter, pointed at `TelegramAPI` (FAKE_TELEGRAM_URL) with the fake token."""
    url, secret = os.environ.get('FAKE_TELEGRAM_URL'), os.environ.get('FAKE_TELEGRAM_SECRET')
    return TelegramChannel(TOKEN, secret, api_url=url) if url and secret else None
