"""Slack: a Slack app that talks to Sammy in direct messages and in channel threads (plain httpx, no SDK).

```
Slack -- POST event (JSON) or button press (form, payload=<json>), signed -->  /api/channels/slack/webhook
          challenge: a signed url_verification is answered with its challenge
          verify:    X-Slack-Signature = v0=HMAC-SHA256(signing secret, v0:<timestamp>:<body>), timestamp within 5 min
          parse:     message.im (a DM), app_mention (a channel thread), block_actions (an ask's button)
Sammy -- chat.postMessage / chat.update / files.info + url_private / files.getUploadURLExternal + upload +
         files.completeUploadExternal, with the bot token as a Bearer header -->  Slack
```

A DM is one Sammy thread (the chat id is the DM's channel id). In a channel Sammy only sees messages that mention it,
and answers in the message's Slack thread: the chat id is `<channel>:<thread ts>`, the thread's first message, so each
Slack thread is a Sammy thread. A delivery's id is the event's `event_id` (Slack retries carry the same one), or a
button press's `action_ts` and user.

The token is only ever in the `Authorization` header, which is never traced. File transfers make no HTTP span: their
URLs hold the file's name, or a one-time upload link.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Sequence
from typing import cast
from urllib.parse import parse_qs, urlsplit

import httpx
from opentelemetry.instrumentation.utils import suppress_http_instrumentation
from pydantic import BaseModel, ConfigDict
from starlette.responses import PlainTextResponse, Response

from sammy.channels.base import Button, ButtonAnswer, Capabilities, Inbound, InboundFile, RawRequest
from sammy.channels.slack_mrkdwn import from_slack, to_mrkdwn
from sammy.observability import timing
from sammy.settings import Settings

API_URL = 'https://slack.com/api'
SIGNATURE = 'x-slack-signature'
TIMESTAMP = 'x-slack-request-timestamp'
MAX_AGE_SECONDS = 5 * 60
"""Slack's advice: a request signed longer ago than this may be a replay."""
FILE_HOSTS = ('files.slack.com',)
"""Where a file is downloaded from with the bot token; nowhere else gets it."""
MAX_TEXT = 3000
"""A section block's text limit. Plain messages could be longer (Slack truncates at 40,000 characters and advises
4,000), but an ask's text goes in a section next to its buttons, so every part keeps to one limit."""
TIMEOUT = httpx.Timeout(60.0, connect=10.0)
FILE_SHARE = 'file_share'
"""The only message subtype that is a person writing; edits, deletions, joins and bots' messages are not."""


class SlackError(Exception):
    """A Slack call that failed: the method and Slack's error code only."""

    def __init__(self, method: str, error: str) -> None:
        super().__init__(f'{method}: {error}')
        self.error = error


# --- Slack's shapes, as much of them as Sammy reads ---


class _Model(BaseModel):
    model_config = ConfigDict(extra='ignore')


class SlackFile(_Model):
    id: str
    name: str | None = None
    mimetype: str | None = None
    size: int | None = None


class Event(_Model):
    type: str
    user: str | None = None
    bot_id: str | None = None
    subtype: str | None = None
    channel: str | None = None
    channel_type: str | None = None
    text: str = ''
    ts: str | None = None
    thread_ts: str | None = None
    files: list[SlackFile] = []


class Authorization(_Model):
    user_id: str | None = None


class Envelope(_Model):
    """An Events API request: `url_verification`, or an `event_callback` with one event."""

    type: str
    challenge: str | None = None
    event_id: str | None = None
    event: Event | None = None
    authorizations: list[Authorization] = []


class Ref(_Model):
    id: str


class Container(_Model):
    message_ts: str | None = None
    thread_ts: str | None = None


class Message(_Model):
    ts: str | None = None
    thread_ts: str | None = None


class Action(_Model):
    value: str | None = None
    action_ts: str | None = None


class Interaction(_Model):
    """An interactivity request (`payload`): Sammy reads `block_actions`, a button pressed."""

    type: str
    user: Ref
    channel: Ref | None = None
    container: Container | None = None
    message: Message | None = None
    actions: list[Action] = []


def place(chat_id: str) -> tuple[str, str | None]:
    """The Slack channel and thread of a chat id: `D...` for a DM, `<channel>:<thread ts>` for a channel thread."""
    channel, _, thread_ts = chat_id.partition(':')
    return channel, thread_ts or None


def files_of(event: Event) -> tuple[InboundFile, ...]:
    return tuple(
        InboundFile(
            id=file.id,
            name=file.name or file.id,
            media_type=file.mimetype or 'application/octet-stream',
            size=file.size,
        )
        for file in event.files
    )


def blocks(text: str, buttons: Sequence[Button] = ()) -> list[dict[str, object]]:
    """The text in a section, and the buttons (if any) under it, Approve green and Decline red."""
    made: list[dict[str, object]] = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}]
    if not buttons:
        return made
    elements: list[dict[str, object]] = []
    for i, button in enumerate(buttons):
        element: dict[str, object] = {
            'type': 'button',
            'action_id': f'sammy-{i}',
            'text': {'type': 'plain_text', 'text': button.label[:75]},
            'value': button.data,
        }
        if (answer := ButtonAnswer.from_data(button.data)) is not None:
            element['style'] = 'primary' if answer.choice == 'approve' else 'danger'
        elements.append(element)
    return [*made, {'type': 'actions', 'elements': elements}]


class SlackChannel:
    name = 'slack'
    capabilities = Capabilities(
        max_text=MAX_TEXT, max_buttons=5, edits=True, threads=True, max_file_bytes=1024 * 1024 * 1024
    )

    def __init__(self, token: str, signing_secret: str, *, api_url: str = API_URL) -> None:
        self._secret = signing_secret.encode()
        self._api_url = api_url.rstrip('/')
        self._auth = {'Authorization': f'Bearer {token}'}
        self._http = httpx.AsyncClient(timeout=TIMEOUT)

    # --- inbound ---

    def verify(self, request: RawRequest) -> bool:
        """Slack's signature over the timestamp and the raw body, and a timestamp within 5 minutes of now."""
        stamp = request.headers.get(TIMESTAMP, '')
        if not (stamp.isascii() and stamp.isdigit()) or abs(time.time() - int(stamp)) > MAX_AGE_SECONDS:
            return False
        signed = b'v0:' + stamp.encode() + b':' + request.body
        expected = 'v0=' + hmac.new(self._secret, signed, hashlib.sha256).hexdigest()
        return hmac.compare_digest(request.headers.get(SIGNATURE, '').encode(), expected.encode())

    def challenge(self, request: RawRequest) -> Response | None:
        """Slack's `url_verification` handshake, which is signed like any event: answered only once verified."""
        if b'"url_verification"' not in request.body or not self.verify(request):
            return None
        try:
            envelope = Envelope.model_validate_json(request.body)
        except ValueError:
            return None
        if envelope.type != 'url_verification' or envelope.challenge is None:
            return None
        return PlainTextResponse(envelope.challenge)

    def parse(self, request: RawRequest) -> list[Inbound]:
        """A form body is a button press (`payload=<json>`); anything else is an Events API request. Pydantic's
        `ValidationError` is a `ValueError`, which the webhook answers with 400."""
        if request.headers.get('content-type', '').startswith('application/x-www-form-urlencoded'):
            payload = parse_qs(request.body.decode()).get('payload')
            if not payload:
                raise ValueError('no payload')
            return self._pressed(Interaction.model_validate_json(payload[0]))
        return self._event(Envelope.model_validate_json(request.body))

    def _event(self, envelope: Envelope) -> list[Inbound]:
        event = envelope.event
        if envelope.type != 'event_callback' or event is None or envelope.event_id is None:
            return []
        sender, channel, ts = event.user, event.channel, event.ts
        if event.bot_id or event.subtype not in (None, FILE_SHARE) or not sender or not channel or not ts:
            return []  # Sammy's own messages and other bots', edits, deletions, joins
        if event.type == 'message' and event.channel_type == 'im':
            chat_id, direct = channel, True
        elif event.type == 'app_mention' and not channel.startswith('D'):
            chat_id, direct = f'{channel}:{event.thread_ts or ts}', False
        else:
            return []
        bot = envelope.authorizations[0].user_id if envelope.authorizations else None
        return [
            Inbound(
                delivery_id=envelope.event_id,
                sender_id=sender,
                chat_id=chat_id,
                direct=direct,
                text=from_slack(event.text, bot),
                files=files_of(event),
            )
        ]

    def _pressed(self, interaction: Interaction) -> list[Inbound]:
        if interaction.type != 'block_actions' or interaction.channel is None or interaction.container is None:
            return []
        channel, message = interaction.channel.id, interaction.message or Message()
        message_ts = interaction.container.message_ts or message.ts
        direct = channel.startswith('D')
        thread_ts = message.thread_ts or interaction.container.thread_ts or message_ts
        chat_id = channel if direct else f'{channel}:{thread_ts}'
        pressed: list[Inbound] = []
        for action in interaction.actions:
            answer = ButtonAnswer.from_data(action.value or '')
            if answer is None or action.action_ts is None or message_ts is None:
                continue  # not one of Sammy's buttons
            pressed.append(
                Inbound(
                    delivery_id=f'{action.action_ts}:{interaction.user.id}',
                    sender_id=interaction.user.id,
                    chat_id=chat_id,
                    direct=direct,
                    answer=answer,
                    answer_message_id=message_ts,
                )
            )
        return pressed

    def ack(self) -> Response:
        return Response(status_code=200)

    # --- outbound ---

    def format(self, markdown: str) -> str:
        return to_mrkdwn(markdown)

    async def send(self, chat_id: str, text: str, buttons: Sequence[Button], reply_to: str | None) -> str:
        """A message in the DM, or in the channel thread; with buttons, the text and the buttons are blocks."""
        channel, thread_ts = place(chat_id)
        body: dict[str, object] = {'channel': channel, 'text': text}
        if buttons:
            body['blocks'] = blocks(text, buttons)
        if thread_ts is not None:
            body['thread_ts'] = thread_ts
        sent = await self._call('chat.postMessage', json=body)
        ts = sent.get('ts')
        if not isinstance(ts, str):
            raise SlackError('chat.postMessage', 'no ts')
        return ts

    async def edit(self, chat_id: str, message_id: str, text: str) -> None:
        """The new text in a section and nothing else, so an answered ask's buttons are gone. A section holds
        `MAX_TEXT` characters: a longer text keeps its end, which says what was chosen."""
        channel, _ = place(chat_id)
        shown = text if len(text) <= MAX_TEXT else f'\N{HORIZONTAL ELLIPSIS}{text[1 - MAX_TEXT :]}'
        await self._call('chat.update', json={'channel': channel, 'ts': message_id, 'text': shown,
                                              'blocks': blocks(shown)})  # fmt: skip

    async def send_file(self, chat_id: str, name: str, media_type: str, data: bytes) -> str:
        """Slack's external upload: ask for an upload URL, post the bytes there, then share the file in the chat."""
        channel, thread_ts = place(chat_id)
        ticket = await self._call('files.getUploadURLExternal', data={'filename': name, 'length': str(len(data))})
        upload_url, file_id = ticket.get('upload_url'), ticket.get('file_id')
        if not isinstance(upload_url, str) or not isinstance(file_id, str):
            raise SlackError('files.getUploadURLExternal', 'no upload_url')
        with timing('slack.upload'), suppress_http_instrumentation():
            try:
                uploaded = await self._http.post(upload_url, files={'file': (name, data, media_type)})
            except httpx.HTTPError as error:
                raise SlackError('upload', type(error).__name__) from None
        if uploaded.status_code != 200:
            raise SlackError('upload', f'status {uploaded.status_code}')
        shared: dict[str, str] = {'files': json.dumps([{'id': file_id, 'title': name}]), 'channel_id': channel}
        if thread_ts is not None:
            shared['thread_ts'] = thread_ts
        await self._call('files.completeUploadExternal', data=shared)
        return file_id

    async def download(self, file: InboundFile) -> bytes:
        """`files.info` for the file's private URL, then the file with the bot token, only from Slack's file host."""
        info = await self._call('files.info', data={'file': file.id})
        found = _object(info.get('file'))
        url = found.get('url_private_download') or found.get('url_private')
        if not isinstance(url, str) or not self._may_send_token(url):
            raise SlackError('files.info', 'no private url on a Slack file host')
        with timing('slack.download'), suppress_http_instrumentation():
            try:
                response = await self._http.get(url, headers=self._auth)
            except httpx.HTTPError as error:
                raise SlackError('download', type(error).__name__) from None
        if response.status_code != 200:
            raise SlackError('download', f'status {response.status_code}')
        return response.content

    def _may_send_token(self, url: str) -> bool:
        parts, api = urlsplit(url), urlsplit(self._api_url)
        return (parts.scheme == 'https' and parts.hostname in FILE_HOSTS) or (
            (parts.scheme, parts.netloc) == (api.scheme, api.netloc)
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(
        self, method: str, *, json: dict[str, object] | None = None, data: dict[str, str] | None = None
    ) -> dict[str, object]:
        """A Web API call's answer, or a `SlackError` with Slack's error code (`ok: false`) or the HTTP status."""
        try:
            response = await self._http.post(f'{self._api_url}/{method}', json=json, data=data, headers=self._auth)
        except httpx.HTTPError as error:
            raise SlackError(method, type(error).__name__) from None
        try:
            answer = _object(response.json())
        except ValueError:
            answer = {}
        if answer.get('ok') is not True:
            raise SlackError(method, str(answer.get('error') or f'status {response.status_code}'))
        return answer


def _object(value: object) -> dict[str, object]:
    """A JSON object's fields, or none for anything else."""
    if not isinstance(value, dict):
        return {}
    found = cast('dict[object, object]', value)
    return {str(key): item for key, item in found.items()}


def new_channel(settings: Settings) -> SlackChannel | None:
    """On only with both the bot token and the signing secret."""
    token, secret = settings.slack_bot_token, settings.slack_signing_secret
    if token is None or secret is None:
        return None
    return SlackChannel(token.get_secret_value(), secret.get_secret_value())
