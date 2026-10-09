"""Telegram: a bot that talks to Sammy, through the Bot API's webhook (plain httpx, no SDK).

```
setWebhook(url=<PUBLIC_URL>/api/channels/telegram/webhook, secret_token=TELEGRAM_WEBHOOK_SECRET)   `sammy telegram-webhook`
Telegram  -- POST update, X-Telegram-Bot-Api-Secret-Token -->  verify (the header), parse (one update, id = delivery id)
Sammy     -- sendMessage (MarkdownV2, inline keyboard) / editMessageText / sendPhoto / sendDocument / getFile -->
```

Private chats are one thread each. In a group, only a message that mentions the bot or replies to one of its messages is
for Sammy; that needs the bot's id and username, which `getMe` gives once, the first time a group message needs them.
A button press is a `callback_query`; the ask's message is then edited, which also takes its buttons away.

The token is in every Bot API URL (`/bot<token>/<method>`), so these calls are never traced, and an error says only the
method and Telegram's own description, never the URL.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Sequence
from typing import Literal

import httpx
from opentelemetry.instrumentation.utils import suppress_http_instrumentation
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.responses import Response

from sammy.channels.base import Button, ButtonAnswer, Capabilities, Inbound, InboundFile, RawRequest
from sammy.channels.telegram_markdown import plain, to_markdown_v2
from sammy.settings import Settings

API_URL = 'https://api.telegram.org'
SECRET_HEADER = 'x-telegram-bot-api-secret-token'
UPDATES = ('message', 'callback_query')
"""What the webhook asks Telegram for (`allowed_updates`)."""
MAX_PHOTO_BYTES = 10 * 1024 * 1024
PHOTO_TYPES = ('image/jpeg', 'image/png', 'image/webp')
"""Sent with `sendPhoto`, shown inline; anything else (a GIF would lose its animation) goes with `sendDocument`."""
TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class TelegramError(Exception):
    """A Bot API call that failed: the method and Telegram's description only (the URL holds the token)."""

    def __init__(self, method: str, status: int, description: str) -> None:
        super().__init__(f'{method}: {status} {description}')
        self.status = status
        self.description = description


class _Untraced(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        with suppress_http_instrumentation():
            return await super().handle_async_request(request)


class _UntracedSync(httpx.HTTPTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        with suppress_http_instrumentation():
            return super().handle_request(request)


# --- the Bot API's shapes, as much of them as Sammy reads ---


class _Model(BaseModel):
    model_config = ConfigDict(extra='ignore', populate_by_name=True)


class User(_Model):
    id: int
    is_bot: bool = False
    username: str | None = None


class Chat(_Model):
    id: int
    type: Literal['private', 'group', 'supergroup', 'channel']


class Entity(_Model):
    type: str
    offset: int
    length: int
    user: User | None = None


class File(_Model):
    file_id: str
    file_size: int | None = None
    file_name: str | None = None
    mime_type: str | None = None


class Message(_Model):
    message_id: int
    sender: User | None = Field(default=None, alias='from')
    chat: Chat
    text: str | None = None
    caption: str | None = None
    entities: list[Entity] = []
    caption_entities: list[Entity] = []
    reply_to_message: Message | None = None
    document: File | None = None
    photo: list[File] = []
    audio: File | None = None
    video: File | None = None
    voice: File | None = None


class CallbackQuery(_Model):
    id: str
    sender: User = Field(alias='from')
    message: Message | None = None
    data: str | None = None


class Update(_Model):
    update_id: int
    message: Message | None = None
    callback_query: CallbackQuery | None = None


def _utf16_slice(text: str, offset: int, length: int) -> str:
    """Telegram counts entity offsets in UTF-16 code units."""
    return text.encode('utf-16-le')[offset * 2 : (offset + length) * 2].decode('utf-16-le', errors='ignore')


def files_of(message: Message) -> tuple[InboundFile, ...]:
    def one(file: File, name: str, media_type: str) -> InboundFile:
        return InboundFile(
            id=file.file_id,
            name=file.file_name or name,
            media_type=file.mime_type or media_type,
            size=file.file_size,
        )

    found: list[InboundFile] = []
    if message.photo:  # each size of one photo, smallest first
        found.append(one(message.photo[-1], f'photo-{message.message_id}.jpg', 'image/jpeg'))
    for file, name, media_type in (
        (message.document, 'file', 'application/octet-stream'),
        (message.audio, 'audio.mp3', 'audio/mpeg'),
        (message.video, 'video.mp4', 'video/mp4'),
        (message.voice, 'voice.ogg', 'audio/ogg'),
    ):
        if file is not None:
            found.append(one(file, name, media_type))
    return tuple(found)


class TelegramChannel:
    name = 'telegram'
    capabilities = Capabilities(
        max_text=4096, max_buttons=8, edits=True, threads=False, max_file_bytes=50 * 1024 * 1024
    )

    def __init__(self, token: str, webhook_secret: str, *, api_url: str = API_URL) -> None:
        self._secret = webhook_secret.encode()
        self._api_url = api_url.rstrip('/')
        self._bot = f'{self._api_url}/bot{token}'
        self._files = f'{self._api_url}/file/bot{token}'
        self._http = httpx.AsyncClient(transport=_Untraced(), timeout=TIMEOUT)
        self._me: User | None = None

    # --- inbound ---

    def verify(self, request: RawRequest) -> bool:
        """Telegram signs nothing: it sends back the secret `setWebhook` was given, in a header."""
        return hmac.compare_digest(request.headers.get(SECRET_HEADER, '').encode(), self._secret)

    def challenge(self, request: RawRequest) -> Response | None:
        return None  # Telegram has no handshake: `setWebhook` is all

    def parse(self, request: RawRequest) -> list[Inbound]:
        try:
            update = Update.model_validate_json(request.body)
        except ValidationError as error:
            raise ValueError('not a Telegram update') from error
        delivery_id = str(update.update_id)
        if (query := update.callback_query) is not None:
            answer = ButtonAnswer.from_data(query.data or '')
            if answer is None or query.message is None:
                return []
            return [
                Inbound(
                    delivery_id=delivery_id,
                    sender_id=str(query.sender.id),
                    chat_id=str(query.message.chat.id),
                    direct=query.message.chat.type == 'private',
                    answer=answer,
                    answer_message_id=str(query.message.message_id),
                )
            ]
        message = update.message
        if message is None or message.sender is None or message.sender.is_bot:
            return []  # edits, channel posts, joins, other bots: not for Sammy
        direct = message.chat.type == 'private'
        text = message.text if message.text is not None else (message.caption or '')
        if not direct:
            addressed = self._addressed(message, text)
            if addressed is None:
                return []
            text = addressed
        reply_to = message.reply_to_message
        return [
            Inbound(
                delivery_id=delivery_id,
                sender_id=str(message.sender.id),
                chat_id=str(message.chat.id),
                direct=direct,
                text=text,
                files=files_of(message),
                reply_to=str(reply_to.message_id) if reply_to is not None else None,
            )
        ]

    def _addressed(self, message: Message, text: str) -> str | None:
        """In a group: the text without the bot's @mention if the message is for Sammy (it mentions the bot, or
        replies to one of its messages), else None."""
        reply = message.reply_to_message
        entities = message.entities if message.text is not None else message.caption_entities
        if not entities and (reply is None or reply.sender is None or not reply.sender.is_bot):
            return None  # nothing could address the bot: no need to ask Telegram who it is
        me = self._whoami()
        mention = f'@{me.username}'.lower()
        mentioned = False
        for entity in entities:
            if entity.type == 'text_mention' and entity.user is not None and entity.user.id == me.id:
                mentioned = True
            elif entity.type in ('mention', 'bot_command'):
                said = _utf16_slice(text, entity.offset, entity.length).lower()
                mentioned = mentioned or said == mention or said.endswith(mention)
        replied = reply is not None and reply.sender is not None and reply.sender.id == me.id
        if not mentioned and not replied:
            return None
        return re.sub(rf'\s*{re.escape(mention)}\b', '', text, flags=re.IGNORECASE).strip()

    def _whoami(self) -> User:
        """The bot, from `getMe`, once per process. `parse` is synchronous, so this one call blocks; a failure is a
        `ValueError`, which answers 400, and Telegram delivers the update again later."""
        if self._me is None:
            try:
                with httpx.Client(transport=_UntracedSync(), timeout=10) as client:
                    response = client.post(f'{self._bot}/getMe')
                self._me = User.model_validate(_result('getMe', response))
            except (httpx.HTTPError, ValueError, TelegramError) as error:
                raise ValueError(f'getMe: {type(error).__name__}') from None
        return self._me

    def ack(self) -> Response:
        return Response(status_code=200)

    # --- outbound ---

    def format(self, markdown: str) -> str:
        return to_markdown_v2(markdown)

    async def send(self, chat_id: str, text: str, buttons: Sequence[Button], reply_to: str | None) -> str:
        body: dict[str, object] = {'chat_id': chat_id, 'text': text, 'parse_mode': 'MarkdownV2'}
        if buttons:
            row = [{'text': button.label, 'callback_data': button.data} for button in buttons]
            body['reply_markup'] = {'inline_keyboard': [row]}
        if reply_to is not None:
            body['reply_parameters'] = {'message_id': int(reply_to), 'allow_sending_without_reply': True}
        result = await self._markdown_call('sendMessage', body)
        return str(result['message_id'])

    async def edit(self, chat_id: str, message_id: str, text: str) -> None:
        """The new text, and no keyboard: an answered ask takes no more presses."""
        body: dict[str, object] = {
            'chat_id': chat_id,
            'message_id': int(message_id),
            'text': text,
            'parse_mode': 'MarkdownV2',
        }
        await self._markdown_call('editMessageText', body)

    async def send_file(self, chat_id: str, name: str, media_type: str, data: bytes) -> str:
        photo = media_type in PHOTO_TYPES and len(data) <= MAX_PHOTO_BYTES
        method, field = ('sendPhoto', 'photo') if photo else ('sendDocument', 'document')
        result = await self._call(method, data={'chat_id': chat_id}, files={field: (name, data, media_type)})
        return str(result['message_id'])

    async def download(self, file: InboundFile) -> bytes:
        found = await self._call('getFile', json={'file_id': file.id})
        path = found.get('file_path')
        if not isinstance(path, str):
            raise TelegramError('getFile', 200, 'no file_path')
        try:
            response = await self._http.get(f'{self._files}/{path}')
        except httpx.HTTPError as error:
            raise TelegramError('file', 0, type(error).__name__) from None
        if response.status_code != 200:
            raise TelegramError('file', response.status_code, 'download failed')
        return response.content

    async def set_webhook(self, url: str) -> None:
        """Point the bot at `url`, with the secret Telegram must send back on every update."""
        await self._call(
            'setWebhook',
            json={'url': url, 'secret_token': self._secret.decode(), 'allowed_updates': list(UPDATES)},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _markdown_call(self, method: str, body: dict[str, object]) -> dict[str, object]:
        """A call with MarkdownV2 text. Should Telegram still refuse it (an entity cut in two where a long reply was
        split), the same text goes again as plain text, so the user gets it rather than nothing."""
        try:
            return await self._call(method, json=body)
        except TelegramError as error:
            if error.status != 400 or "can't parse entities" not in error.description:
                raise
        text = body['text']
        assert isinstance(text, str)
        return await self._call(
            method, json={k: v for k, v in body.items() if k != 'parse_mode'} | {'text': plain(text)}
        )

    async def _call(
        self,
        method: str,
        *,
        json: dict[str, object] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> dict[str, object]:
        try:
            response = await self._http.post(f'{self._bot}/{method}', json=json, data=data, files=files)
        except httpx.HTTPError as error:
            raise TelegramError(method, 0, type(error).__name__) from None
        return _result(method, response)


class _Reply(_Model):
    ok: bool
    result: dict[str, object] | bool | None = None
    description: str | None = None


def _result(method: str, response: httpx.Response) -> dict[str, object]:
    """A Bot API answer's `result`, or a `TelegramError` with its description."""
    try:
        reply = _Reply.model_validate_json(response.content)
    except ValidationError:
        raise TelegramError(method, response.status_code, 'not a Bot API answer') from None
    if not reply.ok:
        raise TelegramError(method, response.status_code, reply.description or 'not ok')
    return reply.result if isinstance(reply.result, dict) else {}


def new_channel(settings: Settings) -> TelegramChannel | None:
    """On only with both the bot's token and the webhook's secret."""
    token, secret = settings.telegram_bot_token, settings.telegram_webhook_secret
    if token is None or secret is None:
        return None
    return TelegramChannel(token.get_secret_value(), secret.get_secret_value())
