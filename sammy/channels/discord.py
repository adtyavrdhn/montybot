"""Discord: a bot that talks to Sammy in direct messages and in servers (plain httpx and a websocket, no SDK).

```
Discord gateway (websocket, on one replica: sammy.channels.leader)     `listen` -> discord_gateway.Gateway
  MESSAGE_CREATE      a direct message, or in a server one that mentions the bot (answered in a thread made from
                      it) or one in such a thread: an `Inbound`, delivery id the message's id
  INTERACTION_CREATE  a button click: answered at once (a deferred update, Discord allows 3 seconds), then an
                      `Inbound` with the answer, delivery id the interaction's id
  GUILD_CREATE, THREAD_CREATE, THREAD_UPDATE   which threads the bot made, so it reads them without a mention
Sammy -> HTTP API     POST /channels/{chat}/messages (text and buttons, or files as multipart attachments)
                      PATCH /channels/{chat}/messages/{id} (the answer; the buttons go)
                      POST /channels/{channel}/messages/{id}/threads (a server conversation's thread)
```

A direct chat is one Sammy thread, and so is each Discord thread the bot makes; the chat id is the Discord channel
(or thread) id. Normal messages only arrive over the gateway, so the webhook route refuses everything (`verify`).

The bot token goes in a header, which is never traced. An interaction's token is in its callback URL and an
attachment's URL is signed, so those two calls are not traced at all.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence

import httpx
from opentelemetry.instrumentation.utils import suppress_http_instrumentation
from pydantic import BaseModel, ConfigDict
from starlette.responses import Response

from sammy import attachments
from sammy.channels.base import Button, ButtonAnswer, Capabilities, Deliver, Inbound, InboundFile, RawRequest
from sammy.channels.discord_gateway import Gateway
from sammy.observability import timing
from sammy.settings import Settings

API_URL = 'https://discord.com/api/v10'
FILES_URL = 'https://cdn.discordapp.com'
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
"""What a bot may upload where the server has no boost: Discord's default."""
MAX_TEXT = 2000
THREAD_EXISTS = 160004
"""Discord's error code for a message that has a thread already."""
MESSAGE_TYPES = (0, 19)
"""A plain message and a reply; anything else (a pin, a thread made, a member joining) is not for Sammy."""
COMPONENT = 3
"""An interaction from a message component: a button click."""
DEFERRED_UPDATE = 6
"""The answer to a click that will change the message later (the edit, once the ask is answered)."""
USER_AGENT = 'DiscordBot (https://github.com/adtyavrdhn/montybot, 1)'
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
NO_PINGS: dict[str, list[str]] = {'parse': []}
"""`allowed_mentions` for everything Sammy sends: a reply quoting `@everyone` or a user pings nobody."""

logger = logging.getLogger(__name__)


class DiscordError(Exception):
    """A call that failed: what it was, the status and Discord's error code. Never the URL or the body."""

    def __init__(self, what: str, status: int, code: int | None) -> None:
        super().__init__(f'{what}: {status} (code {code})')
        self.status = status
        self.code = code


# --- Discord's shapes, as much of them as Sammy reads ---


class _Model(BaseModel):
    model_config = ConfigDict(extra='ignore')


class User(_Model):
    id: str
    bot: bool = False


class Member(_Model):
    user: User | None = None


class Attachment(_Model):
    id: str
    filename: str
    url: str
    size: int | None = None
    content_type: str | None = None


class Reference(_Model):
    message_id: str | None = None


class Message(_Model):
    id: str
    channel_id: str
    guild_id: str | None = None
    author: User
    type: int = 0
    content: str = ''
    attachments: list[Attachment] = []
    mentions: list[User] = []
    message_reference: Reference | None = None


class ClickedMessage(_Model):
    id: str


class ComponentData(_Model):
    custom_id: str = ''


class Interaction(_Model):
    id: str
    token: str
    type: int
    channel_id: str | None = None
    guild_id: str | None = None
    user: User | None = None
    """In a direct message; in a server, the clicker is `member.user`."""
    member: Member | None = None
    message: ClickedMessage | None = None
    data: ComponentData | None = None


class Thread(_Model):
    id: str
    owner_id: str | None = None


class Guild(_Model):
    threads: list[Thread] = []


class ReadyUser(_Model):
    id: str


class Ready(_Model):
    user: ReadyUser


class Created(_Model):
    id: str


class ErrorBody(_Model):
    code: int | None = None


def thread_name(text: str) -> str:
    """A thread's name (1 to 100 characters): the message's first line."""
    first = text.strip().splitlines()[0].strip() if text.strip() else ''
    return first[:100] or 'Sammy'


def files_of(message: Message) -> tuple[InboundFile, ...]:
    return tuple(
        InboundFile(
            id=a.url,
            name=a.filename,
            media_type=a.content_type or 'application/octet-stream',
            size=a.size,
        )
        for a in message.attachments
    )


def components(buttons: Sequence[Button]) -> list[dict[str, object]]:
    """One row of buttons: Approve green, Decline red, anything else grey."""
    styles = {'approve': 3, 'decline': 4}

    def style(button: Button) -> int:
        answer = ButtonAnswer.from_data(button.data)
        return styles[answer.choice] if answer is not None else 2

    row = [{'type': 2, 'style': style(b), 'label': b.label[:80], 'custom_id': b.data} for b in buttons[:5]]
    return [{'type': 1, 'components': row}]


class DiscordChannel:
    """The adapter. `api_url` and `files_url` are Discord's own unless a test points them at a fake."""

    name = 'discord'

    def __init__(
        self,
        token: str,
        *,
        api_url: str = API_URL,
        files_url: str = FILES_URL,
        max_upload_bytes: int = MAX_UPLOAD_BYTES,
        backoff: float = 1.0,
    ) -> None:
        self._token = token
        self._files_url = files_url.rstrip('/')
        self._backoff = backoff
        self.capabilities = Capabilities(
            max_text=MAX_TEXT, max_buttons=5, edits=True, threads=True, max_file_bytes=max_upload_bytes
        )
        headers = {'Authorization': f'Bot {token}', 'User-Agent': USER_AGENT}
        self._http = httpx.AsyncClient(base_url=api_url.rstrip('/'), headers=headers, timeout=TIMEOUT)
        self._files = httpx.AsyncClient(headers={'User-Agent': USER_AGENT}, timeout=TIMEOUT)
        """For attachments: no token, which their host does not need."""
        self._bot_id: str | None = None
        self._threads: set[str] = set()
        """The threads the bot made, which it reads without being mentioned."""

    # --- the webhook: not used, as Discord sends messages over the gateway only ---

    def verify(self, request: RawRequest) -> bool:
        return False

    def challenge(self, request: RawRequest) -> Response | None:
        return None

    def parse(self, request: RawRequest) -> list[Inbound]:
        return []

    def ack(self) -> Response:
        return Response(status_code=204)

    # --- the gateway ---

    async def listen(self, deliver: Deliver) -> None:
        async def dispatch(event: str, data: object) -> None:
            await self.handle(event, data, deliver)

        await Gateway(self._token, self.gateway_url, dispatch, backoff=self._backoff).run()

    async def gateway_url(self) -> str:
        response = await self._http.get('/gateway/bot')
        self._check(response, 'gateway')
        return str(response.json()['url'])

    async def handle(self, event: str, data: object, deliver: Deliver) -> None:
        """One gateway event. Raises `ValidationError` for one that cannot be read."""
        if event == 'READY':
            self._bot_id = Ready.model_validate(data).user.id
        elif event == 'GUILD_CREATE':
            self._threads.update(t.id for t in Guild.model_validate(data).threads if t.owner_id == self._bot_id)
        elif event in ('THREAD_CREATE', 'THREAD_UPDATE'):
            thread = Thread.model_validate(data)
            if thread.owner_id is not None and thread.owner_id == self._bot_id:
                self._threads.add(thread.id)
        elif event in ('MESSAGE_CREATE', 'INTERACTION_CREATE'):
            with timing('discord.event') as span:
                span.set_attribute('event', event)
                if event == 'MESSAGE_CREATE':
                    found = await self.message_of(data)
                else:
                    found = await self.interaction_of(data)
                span.set_attribute('for_sammy', found is not None)
                if found is not None:
                    await deliver(found)

    async def message_of(self, data: object) -> Inbound | None:
        """A MESSAGE_CREATE as an `Inbound`, if it is for Sammy: every direct message; in a server, one in a thread
        the bot made, or one that mentions the bot, for which it makes that thread."""
        message = Message.model_validate(data)
        bot_id = self._bot_id
        if bot_id is None or message.author.bot or message.author.id == bot_id or message.type not in MESSAGE_TYPES:
            return None
        text = re.sub(rf'<@!?{re.escape(bot_id)}>', '', message.content).strip()
        if message.guild_id is None or message.channel_id in self._threads:
            chat_id = message.channel_id
        elif any(m.id == bot_id for m in message.mentions):
            chat_id = await self._thread_for(message, text)
        else:
            return None
        return Inbound(
            delivery_id=message.id,
            sender_id=message.author.id,
            chat_id=chat_id,
            direct=message.guild_id is None,
            text=text,
            files=files_of(message),
            reply_to=message.message_reference.message_id if message.message_reference else None,
        )

    async def _thread_for(self, message: Message, text: str) -> str:
        """The thread made from the message (its id is the message's): made now, or earlier, before a replay. Where
        Discord cannot make one (the message is in a thread already, or the bot may not), the answer goes in place."""
        path = f'/channels/{message.channel_id}/messages/{message.id}/threads'
        try:
            response = await self._http.post(path, json={'name': thread_name(text), 'auto_archive_duration': 1440})
        except httpx.HTTPError as error:
            logger.warning('Could not make a Discord thread: %s', type(error).__qualname__)
            return message.channel_id
        if response.is_success:
            thread_id = Created.model_validate_json(response.content).id
        elif self._error_code(response) == THREAD_EXISTS:
            thread_id = message.id
        else:
            code = self._error_code(response)
            logger.warning('Could not make a Discord thread: %s (code %s)', response.status_code, code)
            return message.channel_id
        self._threads.add(thread_id)
        return thread_id

    async def interaction_of(self, data: object) -> Inbound | None:
        """An INTERACTION_CREATE: a button click, answered at once; as an `Inbound`, if it is one of Sammy's buttons."""
        interaction = Interaction.model_validate(data)
        if interaction.type != COMPONENT:
            return None
        await self._defer(interaction)
        user = interaction.user or (interaction.member.user if interaction.member else None)
        answer = ButtonAnswer.from_data(interaction.data.custom_id) if interaction.data else None
        if user is None or answer is None or interaction.channel_id is None or interaction.message is None:
            return None
        return Inbound(
            delivery_id=interaction.id,
            sender_id=user.id,
            chat_id=interaction.channel_id,
            direct=interaction.guild_id is None,
            answer=answer,
            answer_message_id=interaction.message.id,
        )

    async def _defer(self, interaction: Interaction) -> None:
        """Tell Discord the click arrived, within its 3 seconds. A replayed click was answered already, or its token
        has expired: that error is expected, and the click still counts (once, by its id)."""
        path = f'/interactions/{interaction.id}/{interaction.token}/callback'
        try:
            with suppress_http_instrumentation():  # the URL holds the interaction's token
                response = await self._http.post(path, json={'type': DEFERRED_UPDATE})
        except httpx.HTTPError as error:
            logger.warning('Could not answer a Discord click: %s', type(error).__qualname__)
            return
        if not response.is_success:
            logger.info('Discord did not take the answer to a click: %s', response.status_code)

    # --- sending ---

    def format(self, markdown: str) -> str:
        """Discord renders Markdown itself (bold, italics, code, headings, lists, links)."""
        return markdown

    async def send(self, chat_id: str, text: str, buttons: Sequence[Button], reply_to: str | None) -> str:
        body: dict[str, object] = {'content': text, 'allowed_mentions': NO_PINGS}
        if buttons:
            body['components'] = components(buttons)
        if reply_to is not None:
            body['message_reference'] = {'message_id': reply_to, 'fail_if_not_exists': False}
        response = await self._http.post(f'/channels/{chat_id}/messages', json=body)
        self._check(response, 'send')
        return Created.model_validate_json(response.content).id

    async def edit(self, chat_id: str, message_id: str, text: str) -> None:
        body: dict[str, object] = {'content': text, 'components': [], 'allowed_mentions': NO_PINGS}
        response = await self._http.patch(f'/channels/{chat_id}/messages/{message_id}', json=body)
        self._check(response, 'edit')

    async def send_file(self, chat_id: str, name: str, media_type: str, data: bytes) -> str:
        payload: dict[str, object] = {'attachments': [{'id': 0, 'filename': name}], 'allowed_mentions': NO_PINGS}
        response = await self._http.post(
            f'/channels/{chat_id}/messages',
            data={'payload_json': json.dumps(payload)},
            files={'files[0]': (name, data, media_type)},
        )
        self._check(response, 'send_file')
        return Created.model_validate_json(response.content).id

    async def download(self, file: InboundFile) -> bytes:
        """An attachment from Discord's file host only (its URL came in the message), at most the attachment limit."""
        if not file.id.startswith(f'{self._files_url}/'):
            raise DiscordError('download: not a Discord attachment', 0, None)
        data = bytearray()
        with suppress_http_instrumentation():  # the URL is signed
            async with self._files.stream('GET', file.id) as response:
                if not response.is_success:
                    raise DiscordError('download', response.status_code, None)
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > attachments.MAX_FILE_BYTES:
                        break  # too big: the shared layer leaves it out
        return bytes(data)

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._files.aclose()

    @staticmethod
    def _error_code(response: httpx.Response) -> int | None:
        try:
            return ErrorBody.model_validate_json(response.content).code
        except ValueError:
            return None

    def _check(self, response: httpx.Response, what: str) -> None:
        if not response.is_success:
            raise DiscordError(what, response.status_code, self._error_code(response))


def new_channel(settings: Settings) -> DiscordChannel | None:
    """On only with the bot token (`DISCORD_BOT_TOKEN`)."""
    token = settings.discord_bot_token
    return DiscordChannel(token.get_secret_value()) if token is not None else None
