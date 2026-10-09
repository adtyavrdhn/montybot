"""WhatsApp: talk to Sammy through the WhatsApp Business Cloud API (plain httpx, no SDK).

```
Meta   -- GET  hub.mode=subscribe, hub.verify_token, hub.challenge -->  challenge: hub.challenge, if the token matches
Meta   -- POST messages, X-Hub-Signature-256 (HMAC-SHA256 of the body with the app secret) -->  verify, parse
Sammy  -- POST /<phone number id>/messages: text, reply buttons, image, document, template -->
          POST /<phone number id>/media (upload); GET /<media id>, then the URL it gives (download)
```

A phone number (`wa_id`) is one direct chat and one Sammy thread. The message id (`wamid...`) is the delivery id;
delivery and read statuses are ignored. An approval is an interactive message with reply buttons, and a press comes
back as an `interactive.button_reply` message. WhatsApp cannot edit a message.

The 24-hour window: a business may write to a user freely only within 24 hours of the user's last message. Outside it,
the shared layer holds the chat's messages (`Capabilities.reply_window`) and calls `reopen`, which sends the one
approved template (`WHATSAPP_TEMPLATE`, "Sammy has an update for you"). The user's reply, or a press of the template's
quick reply button, opens the window again and the held messages go out.

The access token goes only in the `Authorization` header, which is never traced; a media file's download URL is
signed, so that call is not traced at all.
"""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import httpx
from opentelemetry.instrumentation.utils import suppress_http_instrumentation
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.responses import PlainTextResponse, Response

from sammy import attachments
from sammy.channels.base import Button, ButtonAnswer, Capabilities, Inbound, InboundFile, RawRequest
from sammy.channels.whatsapp_markdown import to_whatsapp
from sammy.settings import Settings

API_URL = 'https://graph.facebook.com/v23.0'
SIGNATURE_HEADER = 'x-hub-signature-256'
MAX_BUTTON_BODY = 1024
"""The longest body an interactive message takes; a longer ask is sent as text, then its buttons on a short line."""
MAX_BUTTON_TITLE = 20
CHOOSE = 'Approve or decline?'
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_TYPES = ('image/jpeg', 'image/png')
"""Sent as an image, shown inline; anything else goes as a document (up to 100 MB)."""
TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_MIME_TYPES = mimetypes.MimeTypes()
"""Python's own table only, not the system's, so a file is named the same on every machine."""


class WhatsAppError(Exception):
    """A Cloud API call that failed: what was called, the status and Meta's error code, never the token."""

    def __init__(self, what: str, status: int, code: int | None = None) -> None:
        super().__init__(f'{what}: {status}' + (f' (error {code})' if code is not None else ''))
        self.status = status
        self.code = code


# --- the webhook's shapes, as much of them as Sammy reads ---


class _Model(BaseModel):
    model_config = ConfigDict(extra='ignore', populate_by_name=True)


class Text(_Model):
    body: str = ''


class Media(_Model):
    id: str
    mime_type: str | None = None
    filename: str | None = None
    caption: str | None = None


class Reply(_Model):
    id: str
    title: str = ''


class Interactive(_Model):
    type: str
    button_reply: Reply | None = None
    list_reply: Reply | None = None


class Context(_Model):
    id: str | None = None


class Message(_Model):
    sender: str = Field(alias='from')
    id: str
    timestamp: str | None = None
    type: str
    text: Text | None = None
    interactive: Interactive | None = None
    context: Context | None = None
    image: Media | None = None
    document: Media | None = None
    audio: Media | None = None
    video: Media | None = None
    sticker: Media | None = None


class Metadata(_Model):
    phone_number_id: str


class Value(_Model):
    metadata: Metadata | None = None
    messages: list[Message] = []


class Change(_Model):
    field: str = ''
    value: Value


class Entry(_Model):
    changes: list[Change] = []


class Notification(_Model):
    object: str
    entry: list[Entry] = []


def _sent_at(timestamp: str | None) -> datetime | None:
    return datetime.fromtimestamp(int(timestamp), UTC) if timestamp and timestamp.isdigit() else None


def _media(message: Message) -> Media | None:
    return {
        'image': message.image,
        'document': message.document,
        'audio': message.audio,
        'video': message.video,
        'sticker': message.sticker,
    }.get(message.type)


def _file(message: Message, media: Media) -> InboundFile:
    media_type = (media.mime_type or 'application/octet-stream').split(';', 1)[0].strip()
    name = media.filename or f'{message.type}{_MIME_TYPES.guess_extension(media_type) or ""}'
    return InboundFile(id=media.id, name=name, media_type=media_type)


class WhatsAppChannel:
    name = 'whatsapp'
    capabilities = Capabilities(
        max_text=4096,
        max_buttons=3,
        edits=False,
        threads=False,
        max_file_bytes=100 * 1024 * 1024,
        reply_window=timedelta(hours=24),
    )

    def __init__(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        app_secret: str,
        verify_token: str,
        template: str,
        template_language: str,
        api_url: str = API_URL,
    ) -> None:
        self._phone_number_id = phone_number_id
        self._app_secret = app_secret.encode()
        self._verify_token = verify_token.encode()
        self._template = template
        self._template_language = template_language
        self._http = httpx.AsyncClient(
            base_url=api_url.rstrip('/'), headers={'Authorization': f'Bearer {access_token}'}, timeout=TIMEOUT
        )

    # --- inbound ---

    def verify(self, request: RawRequest) -> bool:
        """`X-Hub-Signature-256` is `sha256=` and the HMAC-SHA256 of the raw body with the app secret."""
        expected = 'sha256=' + hmac.new(self._app_secret, request.body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(request.headers.get(SIGNATURE_HEADER, '').encode(), expected.encode())

    def challenge(self, request: RawRequest) -> Response | None:
        """The subscription handshake, answered only when its token is ours: anything else goes on to `verify`."""
        query = request.query
        if request.method != 'GET' or query.get('hub.mode') != 'subscribe' or 'hub.challenge' not in query:
            return None
        if not hmac.compare_digest(query.get('hub.verify_token', '').encode(), self._verify_token):
            return None
        return PlainTextResponse(query['hub.challenge'])

    def parse(self, request: RawRequest) -> list[Inbound]:
        try:
            notification = Notification.model_validate_json(request.body)
        except ValidationError as error:
            raise ValueError('not a WhatsApp webhook') from error
        if notification.object != 'whatsapp_business_account':
            return []
        found: list[Inbound] = []
        for entry in notification.entry:
            for change in entry.changes:
                value = change.value
                if change.field != 'messages' or value.metadata is None:
                    continue
                if value.metadata.phone_number_id != self._phone_number_id:
                    continue  # another number on the same app
                found.extend(inbound for m in value.messages if (inbound := _inbound(m)) is not None)
        return found

    def ack(self) -> Response:
        return Response(status_code=200)

    # --- outbound ---

    def format(self, markdown: str) -> str:
        return to_whatsapp(markdown)

    async def send(self, chat_id: str, text: str, buttons: Sequence[Button], reply_to: str | None) -> str:
        plain: dict[str, object] = {'type': 'text', 'text': {'preview_url': False, 'body': text}}
        if not buttons:
            return await self._message(chat_id, plain, reply_to)
        if len(text) > MAX_BUTTON_BODY:
            await self._message(chat_id, plain, reply_to)
            text, reply_to = CHOOSE, None
        replies = [
            {'type': 'reply', 'reply': {'id': button.data, 'title': button.label[:MAX_BUTTON_TITLE]}}
            for button in buttons[: self.capabilities.max_buttons]
        ]
        interactive = {'type': 'button', 'body': {'text': text}, 'action': {'buttons': replies}}
        return await self._message(chat_id, {'type': 'interactive', 'interactive': interactive}, reply_to)

    async def edit(self, chat_id: str, message_id: str, text: str) -> None:
        raise NotImplementedError('WhatsApp cannot edit a message')  # never called: `capabilities.edits` is False

    async def send_file(self, chat_id: str, name: str, media_type: str, data: bytes) -> str:
        """Upload it, then send it by its media id: an image inline, anything else as a document."""
        response = await self._call(
            'media',
            'POST',
            f'/{self._phone_number_id}/media',
            data={'messaging_product': 'whatsapp', 'type': media_type},
            files={'file': (name, data, media_type)},
        )
        media_id = _Id.model_validate_json(response.content).id
        if media_type in IMAGE_TYPES and len(data) <= MAX_IMAGE_BYTES:
            return await self._message(chat_id, {'type': 'image', 'image': {'id': media_id}})
        return await self._message(chat_id, {'type': 'document', 'document': {'id': media_id, 'filename': name}})

    async def download(self, file: InboundFile) -> bytes:
        """The media's URL from its id, then the file from that URL, both with the token."""
        response = await self._call('media url', 'GET', f'/{file.id}')
        media = _MediaURL.model_validate_json(response.content)
        if media.file_size is not None and media.file_size > attachments.MAX_FILE_BYTES:
            return b''  # not fetched at all: the shared layer tells the user it was too big to keep
        with suppress_http_instrumentation():  # the URL is signed
            return (await self._call('media download', 'GET', media.url)).content

    async def reopen(self, chat_id: str) -> str:
        """The approved template, which needs no open window: "Sammy has an update for you"."""
        template = {'name': self._template, 'language': {'code': self._template_language}}
        return await self._message(chat_id, {'type': 'template', 'template': template})

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _message(self, to: str, content: dict[str, object], reply_to: str | None = None) -> str:
        body: dict[str, object] = {'messaging_product': 'whatsapp', 'recipient_type': 'individual', 'to': to, **content}
        if reply_to is not None:
            body['context'] = {'message_id': reply_to}
        response = await self._call('messages', 'POST', f'/{self._phone_number_id}/messages', json=body)
        try:
            return _Sent.model_validate_json(response.content).messages[0].id
        except (ValidationError, IndexError):
            raise WhatsAppError('messages', response.status_code) from None

    async def _call(
        self,
        what: str,
        method: str,
        url: str,
        *,
        json: dict[str, object] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._http.request(method, url, json=json, data=data, files=files)
        except httpx.HTTPError:
            raise WhatsAppError(what, 0, None) from None  # its text can hold the signed media URL
        if response.status_code >= 400:
            try:
                code = _Failed.model_validate_json(response.content).error.code
            except ValidationError:
                code = None
            raise WhatsAppError(what, response.status_code, code)
        return response


def _inbound(message: Message) -> Inbound | None:
    """What a message says to Sammy; None for what is not for it (reactions, locations, contacts, unsupported)."""
    text = ''
    file = None
    answer = None
    if message.type == 'text' and message.text is not None:
        text = message.text.body
    elif message.type == 'interactive' and message.interactive is not None:
        pressed = message.interactive.button_reply or message.interactive.list_reply
        answer = ButtonAnswer.from_data(pressed.id) if pressed is not None else None
        if answer is None:
            return None
    elif (media := _media(message)) is not None:
        file = _file(message, media)
        text = media.caption or ''
    elif message.type != 'button':  # a press of the template's quick reply: it only opens the window
        return None
    context = message.context.id if message.context is not None else None
    return Inbound(
        delivery_id=message.id,
        sender_id=message.sender,
        chat_id=message.sender,
        direct=True,
        text=text,
        files=(file,) if file is not None else (),
        reply_to=None if answer is not None else context,
        answer=answer,
        answer_message_id=context if answer is not None else None,
        sent_at=_sent_at(message.timestamp),
    )


class _Id(_Model):
    id: str


class _Sent(_Model):
    messages: list[_Id]


class _MediaURL(_Model):
    url: str
    file_size: int | None = None


class _ErrorBody(_Model):
    code: int | None = None


class _Failed(_Model):
    error: _ErrorBody


def new_channel(settings: Settings) -> WhatsAppChannel | None:
    """On only with the access token, the phone number id, the app secret and the verify token."""
    token, secret, verify = settings.whatsapp_access_token, settings.whatsapp_app_secret, settings.whatsapp_verify_token
    phone_number_id = settings.whatsapp_phone_number_id
    if token is None or secret is None or verify is None or not phone_number_id:
        return None
    return WhatsAppChannel(
        access_token=token.get_secret_value(),
        phone_number_id=phone_number_id,
        app_secret=secret.get_secret_value(),
        verify_token=verify.get_secret_value(),
        template=settings.whatsapp_template,
        template_language=settings.whatsapp_template_language,
    )
