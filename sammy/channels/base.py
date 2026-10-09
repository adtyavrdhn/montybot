"""What a chat platform adapter implements (`Channel`), and the shapes the shared layer passes it.

A platform adapter only knows its own wire format: how to check a webhook's signature, read it into `Inbound`
messages, and send, edit and upload through its API. Linking, threads, asks, the outbox and retries are shared
(`sammy.channels`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict
from starlette.responses import Response

Choice = Literal['approve', 'decline']
CHOICES: tuple[Choice, ...] = ('approve', 'decline')


@dataclass(frozen=True, kw_only=True)
class Capabilities:
    max_text: int
    """The longest message the platform takes, in characters; longer replies are split (`text.split`)."""
    max_buttons: int
    """Buttons one message can carry; 0 means none, and asks are answered with a numbered text reply."""
    edits: bool
    """Whether a sent message can be edited, so an answered ask's buttons can be replaced by the answer."""
    threads: bool
    """Whether conversations can be threads inside a channel (Slack, Discord)."""
    max_file_bytes: int
    """The largest file the bot can send; a larger one is sent as a link to the chat on the web."""
    reply_window: timedelta | None = None
    """How long after a chat's last message the bot may write to it freely (WhatsApp: 24 hours); None for no limit.
    Outside it the chat's messages are held, the platform's re-engagement message is sent once (`Reopens`), and the
    held messages go out when the chat writes again (`sammy.channels.outbound`)."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class InboundFile(_Frozen):
    id: str
    """The platform's id for the file, which `Channel.download` takes."""
    name: str
    media_type: str = 'application/octet-stream'
    size: int | None = None
    """In bytes, when the platform says; a file over the limit is skipped before it is downloaded."""


class ButtonAnswer(_Frozen):
    ask_id: str
    choice: Choice

    @classmethod
    def from_data(cls, data: str) -> ButtonAnswer | None:
        """The answer a button's `data` (`button_data`) carries; None for data that is not one of ours."""
        ask_id, _, choice = data.partition(':')
        for known in CHOICES:
            if choice == known and ask_id:
                return cls(ask_id=ask_id, choice=known)
        return None


def button_data(ask_id: str, choice: Choice) -> str:
    """What a button carries back: under 64 bytes, Telegram's limit for callback data."""
    return f'{ask_id}:{choice}'


class Inbound(_Frozen):
    """One message (or button press) addressed to Sammy."""

    delivery_id: str
    """The platform's id for this delivery: the same delivery twice is handled once."""
    sender_id: str
    chat_id: str
    """The conversation: a direct chat, or a thread in a channel. Platforms encode it as they need."""
    direct: bool
    """A direct chat with the bot: the only place a link code or a ping is ever sent."""
    text: str = ''
    files: tuple[InboundFile, ...] = ()
    reply_to: str | None = None
    answer: ButtonAnswer | None = None
    """A button press on an ask."""
    answer_message_id: str | None = None
    """The message whose button was pressed."""
    sent_at: datetime | None = None
    """When the user sent it, if the platform says; a reply window counts from it (else from when it arrived)."""


class Button(_Frozen):
    label: str
    data: str


class Outgoing(_Frozen):
    """One outbox entry: Markdown text, buttons on its last part, and files Sammy shared (attachment ids)."""

    text: str = ''
    buttons: tuple[Button, ...] = ()
    files: tuple[str, ...] = ()
    link: str | None = None
    """The chat on the web, for a file too big for the platform."""
    secret: bool = False
    """The text holds a link code: it is cleared from the outbox once sent."""


@dataclass(frozen=True, kw_only=True)
class RawRequest:
    method: str
    path: str
    headers: Mapping[str, str]
    """Lower-case names."""
    query: Mapping[str, str]
    body: bytes


class Channel(Protocol):
    """A chat platform. Enabled only when all its credentials are set (its factory returns None otherwise)."""

    @property
    def name(self) -> str:
        """The platform's name in URLs and tables: `slack`, `telegram`."""
        ...

    @property
    def capabilities(self) -> Capabilities: ...

    def verify(self, request: RawRequest) -> bool:
        """Whether the request really comes from the platform, checked before anything is parsed."""
        ...

    def challenge(self, request: RawRequest) -> Response | None:
        """The answer to a URL verification handshake; None for any other request."""
        ...

    def parse(self, request: RawRequest) -> list[Inbound]:
        """The messages for Sammy in a verified request. Raises `ValueError` for one that cannot be read."""
        ...

    def ack(self) -> Response:
        """What the webhook answers once the messages are recorded."""
        ...

    def format(self, markdown: str) -> str:
        """Markdown in the platform's own flavour."""
        ...

    async def send(self, chat_id: str, text: str, buttons: Sequence[Button], reply_to: str | None) -> str:
        """Send one message; returns the platform's id for it."""
        ...

    async def edit(self, chat_id: str, message_id: str, text: str) -> None: ...

    async def send_file(self, chat_id: str, name: str, media_type: str, data: bytes) -> str: ...

    async def download(self, file: InboundFile) -> bytes: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class Reopens(Protocol):
    """What a platform with a reply window (`Capabilities.reply_window`) implements as well."""

    async def reopen(self, chat_id: str) -> str:
        """Send the one message the platform allows outside the window (WhatsApp: an approved template), which asks
        the user to write back; returns the platform's id for it."""
        ...
