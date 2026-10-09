"""A fake WhatsApp Cloud API (`CloudAPI`, on `PlatformServer`) and recorded webhooks (`tests/fixtures/whatsapp/`), for
testing `sammy.channels.whatsapp` offline with no real token.

```
pytest process                                            app process
  webhook(...) + POST /api/channels/whatsapp/webhook --->  WhatsAppChannel (CHANNEL_BACKENDS=fake_whatsapp:new_channel)
  CloudAPI: /v23.0/<phone number id>/messages, /media  <---  messages (text, buttons, image, document, template), media
            /v23.0/<media id>, /media-files/<media id>
```
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
from pathlib import Path

from fake_channel import Call, PlatformServer, Reply, reply

from sammy.channels.whatsapp import SIGNATURE_HEADER, WhatsAppChannel
from sammy.settings import Settings

TOKEN = 'EAAFakeTokenForTestsOnly0123456789'
PHONE_NUMBER_ID = '106540352242922'
APP_SECRET = 'whatsapp-app-secret'
VERIFY_TOKEN = 'whatsapp-verify-token'
TEMPLATE = 'sammy_update'
PAT = '16505551234'
VERSION = '/v23.0'
PAYLOADS = Path(__file__).parent / 'fixtures' / 'whatsapp'


def webhook(name: str, *, message_id: str | None = None, text: str | None = None) -> dict[str, object]:
    """A recorded webhook (`fixtures/whatsapp/<name>.json`), with its message's id and text replaced if given."""
    loaded = json.loads((PAYLOADS / f'{name}.json').read_text())
    assert isinstance(loaded, dict)
    made: dict[str, object] = copy.deepcopy(loaded)  # pyright: ignore[reportUnknownArgumentType]
    if message_id is not None:
        message_of(made)['id'] = message_id
    if text is not None:
        message_of(made)['text'] = {'body': text}
    return made


def _dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value  # pyright: ignore[reportUnknownVariableType]


def _first(value: object) -> dict[str, object]:
    assert isinstance(value, list) and value
    return _dict(value[0])  # pyright: ignore[reportUnknownArgumentType]


def value_of(body: dict[str, object]) -> dict[str, object]:
    """The `value` of a webhook's (first) change, to change it in place."""
    return _dict(_first(_first(body['entry'])['changes'])['value'])


def message_of(body: dict[str, object]) -> dict[str, object]:
    """The (first) message a webhook carries, to change it in place."""
    return _first(value_of(body)['messages'])


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    return 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def signed_headers(body: bytes, secret: str = APP_SECRET) -> dict[str, str]:
    return {SIGNATURE_HEADER: sign(body, secret), 'Content-Type': 'application/json'}


class CloudAPI(PlatformServer):
    """Answers the Cloud API calls Sammy makes, as Meta documents them, and serves media to download."""

    def __init__(self) -> None:
        self._ids = 0
        super().__init__(
            {
                ('POST', f'{VERSION}/{PHONE_NUMBER_ID}/messages'): self._message,
                ('POST', f'{VERSION}/{PHONE_NUMBER_ID}/media'): self._upload,
            }
        )

    @property
    def api_url(self) -> str:
        return f'{self.url}{VERSION}'

    def _message(self, call: Call) -> Reply:
        self._ids += 1
        to = str(call.json()['to'])
        return reply(
            {
                'messaging_product': 'whatsapp',
                'contacts': [{'input': to, 'wa_id': to}],
                'messages': [{'id': f'wamid.SAMMY{self._ids:04d}'}],
            }
        )

    def _upload(self, call: Call) -> Reply:
        self._ids += 1
        return reply({'id': f'media{self._ids:04d}'})

    def serve_media(self, media_id: str, data: bytes, media_type: str) -> None:
        """Media a user sent: its URL from `GET /<media id>`, then the bytes from that URL."""
        url = f'{self.url}/media-files/{media_id}'
        about = {
            'messaging_product': 'whatsapp',
            'url': url,
            'mime_type': media_type,
            'sha256': hashlib.sha256(data).hexdigest(),
            'file_size': len(data),
            'id': media_id,
        }
        self.routes[('GET', f'{VERSION}/{media_id}')] = lambda call: reply(about)
        self.routes[('GET', f'/media-files/{media_id}')] = lambda call: (200, data, media_type)

    def calls_to(self, path: str) -> list[Call]:
        with self.lock:
            return [c for c in self.calls if c.path.split('?', 1)[0] == path]

    def messages(self, to: str = PAT) -> list[dict[str, object]]:
        """Each message sent to `to`, in order."""
        return [body for c in self.calls_to(f'{VERSION}/{PHONE_NUMBER_ID}/messages') if (body := c.json())['to'] == to]

    def of_type(self, kind: str, to: str = PAT) -> list[dict[str, object]]:
        """What each message of `kind` (`template`, `document`, `interactive`...) sent to `to` carries, in order."""
        return [_dict(m[kind]) for m in self.messages(to) if m['type'] == kind]

    def texts(self, to: str = PAT) -> list[str]:
        """What each text or button message says."""
        found: list[str] = []
        for message in self.messages(to):
            text, interactive = message.get('text'), message.get('interactive')
            if isinstance(text, dict):
                found.append(str(text['body']))  # pyright: ignore[reportUnknownArgumentType]
            elif isinstance(interactive, dict):
                found.append(str(interactive['body']['text']))  # pyright: ignore[reportUnknownArgumentType]
        return found


def replies(interactive: dict[str, object]) -> list[dict[str, object]]:
    """The reply buttons of an interactive message: `{'id': ..., 'title': ...}` each."""
    buttons = _dict(interactive['action'])['buttons']
    assert isinstance(buttons, list)
    return [_dict(_dict(button)['reply']) for button in buttons]  # pyright: ignore[reportUnknownVariableType]


def new_channel(settings: Settings) -> WhatsAppChannel | None:
    """The real adapter, pointed at `CloudAPI` (FAKE_WHATSAPP_URL) with the fake credentials."""
    url = os.environ.get('FAKE_WHATSAPP_URL')
    if not url:
        return None
    return WhatsAppChannel(
        access_token=TOKEN,
        phone_number_id=PHONE_NUMBER_ID,
        app_secret=APP_SECRET,
        verify_token=VERIFY_TOKEN,
        template=TEMPLATE,
        template_language='en',
        api_url=url,
    )
