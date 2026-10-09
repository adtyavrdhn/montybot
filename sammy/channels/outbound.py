"""What Sammy sends to chats: replies, asks and pings go into `sammy.channel_outbox`, and a DBOS workflow per row
sends it.

```
producers (inside steps that exist already, so no run's step numbering changes)
  workflows.finish_run / end_run      enqueue_reply: the reply and the files Sammy shared, for a run from a chat
  notifications.notify                notify: an ask of a run from a chat goes to that chat; anything else is a ping
                                      to each linked direct chat that wants pings
Pump (in the app, every 0.5 s)        the oldest unsent row of each chat -> deliver(row_id), workflow id
                                      channel-send:<row id>, so every replica and restart starts it once
deliver(row_id)                       @DBOS.workflow
  split the formatted text at the platform's limit (pure: a replay splits the same way)
  step channel.send.<i>               one per part, retried
  step channel.file.<i>               one per file (too big for the platform: a link to the chat instead)
  step channel.sent                   sent_at and the platform's message ids
```

Exactly once, as far as it goes: a finished step is never run again, so a restart in the middle of a long reply
sends each part once. A crash in the instant between the platform accepting a part and DBOS recording its step can
send that one part again: platform APIs take no idempotency key, so nothing can rule that out.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import Self

import logfire
from dbos import DBOS, SetWorkflowID, StepOptions

from sammy import attachments, store
from sammy.channels import store as channel_store
from sammy.channels.base import Button, Capabilities, Channel, Outgoing, button_data
from sammy.channels.store import Chat, OutboxRow
from sammy.channels.text import split
from sammy.db import Connection
from sammy.models import Ask, NoticeKind
from sammy.observability import timing
from sammy.resources import Resources, current

SEND: StepOptions = {'retries_allowed': True, 'max_attempts': 5, 'interval_seconds': 1.0, 'backoff_rate': 2.0}
"""A platform call: retried with backoff, as a platform's API can be briefly down or rate limited."""
RECORD: StepOptions = {'retries_allowed': True, 'max_attempts': 5, 'interval_seconds': 1.0}
POLL_SECONDS = 0.5
ASK_KINDS: tuple[NoticeKind, ...] = ('question', 'approval', 'handoff', 'connect')
APPROVE_BY_TEXT = 'Reply 1 to approve or 2 to decline. You can add a reason after it.'

logger = logging.getLogger(__name__)


def thread_url(public_url: str, thread_id: str) -> str:
    return f'{public_url}/#/t/{thread_id}'


# --- producers ---


async def enqueue_reply(connection: Connection, public_url: str, *, run_id: str, user_id: str, thread_id: str,
                        output: str) -> None:  # fmt: skip
    """The run's reply and the files Sammy shared, if the run came from a chat. In the transaction that ends it."""
    chat = await channel_store.chat_of_run(connection, run_id)
    if chat is None:
        return
    files = (await attachments.files_of_runs(connection, user_id, [run_id])).get(run_id, {}).get('sammy', [])
    outgoing = Outgoing(text=output, files=tuple(str(f['id']) for f in files), link=thread_url(public_url, thread_id))
    await channel_store.enqueue(connection, f'reply:{run_id}', chat, outgoing, user_id=user_id)


def ask_message(ask: Ask, capabilities: Capabilities, link: str) -> Outgoing:
    """An ask as a chat message. The hand-off and connect links open the chat on the web, which needs the user to be
    signed in there: the link alone grants nothing."""
    match ask.kind:
        case 'question':
            return Outgoing(text=f'{ask.prompt}\n\nReply with your answer.')
        case 'approval':
            text = f'Sammy needs your approval:\n\n{ask.prompt}'
            if capabilities.max_buttons < 2:
                return Outgoing(text=f'{text}\n\n{APPROVE_BY_TEXT}')
            buttons = (
                Button(label='Approve', data=button_data(ask.id, 'approve')),
                Button(label='Decline', data=button_data(ask.id, 'decline')),
            )
            return Outgoing(text=text, buttons=buttons)
        case 'handoff':
            return Outgoing(text=f'Sammy needs you to take over the browser: {ask.prompt}\n\nTake over here: {link}')
        case 'connect':
            return Outgoing(text=f'{ask.prompt}\n\nConnect it here: {link}')


async def notify(resources: Resources, *, user_id: str, thread_id: str, kind: NoticeKind, tag: str, what: str) -> None:
    """For an ask (`tag` is its id) of a run from a chat: the ask, in that chat. Otherwise a ping (`what`, which says
    no more than a web push does, and the chat's link) to each linked direct chat that wants pings."""
    link = thread_url(resources.settings.public_url, thread_id)
    async with resources.pool.connection() as connection, connection.transaction():
        ask = await store.get_ask(connection, user_id, tag) if kind in ASK_KINDS else None
        chat = await channel_store.chat_of_run(connection, ask.run_id) if ask is not None else None
        channel = resources.channels.get(chat.channel) if chat is not None else None
        if ask is not None and chat is not None and channel is not None:
            outgoing = ask_message(ask, channel.capabilities, link)
            await channel_store.enqueue(connection, f'ask:{ask.id}', chat, outgoing, user_id=user_id, ask_id=ask.id)
            return
        for identity in await channel_store.identities_of(connection, user_id):
            if not identity.notify or identity.notify_chat_id is None or not resources.channels.get(identity.channel):
                continue
            chat = Chat(channel=identity.channel, chat_id=identity.notify_chat_id)
            key = f'ping:{kind}:{tag}:{identity.channel}:{identity.external_user_id}'
            await channel_store.enqueue(connection, key, chat, Outgoing(text=f'{what}\n\n{link}'), user_id=user_id)


# --- delivery ---


@DBOS.workflow(name='sammy.channel_deliver')
async def deliver(row_id: str) -> str:
    resources = current()
    with timing('channel.deliver') as span:
        try:
            return await _deliver(resources, row_id, span.set_attribute)
        except Exception as error:  # noqa: BLE001  recorded by type; the chat's next message goes on
            # Any failure, not only a platform's, marks the row failed: a row left unsent would hold back every later
            # message to its chat, as the pump sends each chat's oldest unsent row first.
            logfire.warn('A chat message was not sent: {error_type}', error_type=type(error).__name__)
            await DBOS.run_step_async(
                {**RECORD, 'name': 'channel.failed'}, failed, resources, row_id, type(error).__name__
            )
            return 'failed'


async def _deliver(resources: Resources, row_id: str, annotate: Callable[[str, str], object]) -> str:
    # Read outside a step, so DBOS records no message text (or link code); the row does not change until the last
    # step below.
    async with resources.pool.connection() as connection:
        row = await channel_store.load_row(connection, row_id)
    if row is None or row.sent:
        return 'gone'
    annotate('channel', row.channel)
    channel = _channel(row.channel)
    outgoing = row.outgoing
    parts = split(channel.format(outgoing.text), channel.capabilities.max_text) if outgoing.text else []
    buttons = outgoing.buttons[: channel.capabilities.max_buttons]
    sent: list[str] = []
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        step: StepOptions = {**SEND, 'name': f'channel.send.{i}'}
        sent.append(await DBOS.run_step_async(step, send_part, row, part, buttons if last else ()))
    for i, file_id in enumerate(outgoing.files):
        sent.append(await DBOS.run_step_async({**SEND, 'name': f'channel.file.{i}'}, send_file, row, file_id))
    await DBOS.run_step_async({**RECORD, 'name': 'channel.sent'}, record_sent, resources, row_id, sent, outgoing.secret)
    return 'sent'


def _channel(name: str) -> Channel:
    channel = current().channels.get(name)
    if channel is None:
        raise LookupError(f'the chat platform {name!r} is off')
    return channel


async def send_part(row: OutboxRow, text: str, buttons: Sequence[Button]) -> str:
    return await _channel(row.channel).send(row.chat_id, text, buttons, None)


async def send_file(row: OutboxRow, file_id: str) -> str:
    """The file, or a line with the chat's link if it is too big for the platform. Returns the message's id."""
    resources = current()
    channel = _channel(row.channel)
    if row.user_id is None:
        raise LookupError('a file needs its owner')
    async with resources.pool.connection() as connection:
        found = await attachments.read(connection, row.user_id, file_id)
    if found is None:
        raise LookupError('the file is gone')
    attachment, data = found
    if len(data) > channel.capabilities.max_file_bytes:
        text = f'{attachment.name} is too big to send here. It is in the chat: {row.outgoing.link}'
        return await channel.send(row.chat_id, text, (), None)
    return await channel.send_file(row.chat_id, attachment.name, attachment.media_type, data)


async def record_sent(resources: Resources, row_id: str, message_ids: list[str], secret: bool) -> None:
    async with resources.pool.connection() as connection:
        await channel_store.mark_sent(connection, row_id, message_ids, forget_text=secret)


async def failed(resources: Resources, row_id: str, error_type: str) -> None:
    """The type only: an error's text can quote the message."""
    async with resources.pool.connection() as connection:
        await channel_store.mark_failed(connection, row_id, error_type)


class Pump:
    """Starts the delivery of each chat's oldest unsent row, in the app's process (as `BrowserHost` reaps)."""

    def __init__(self, resources: Resources, interval: float = POLL_SECONDS) -> None:
        self._resources = resources
        self._interval = interval
        self._started: set[str] = set()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        if self._resources.channels.names:
            self._task = asyncio.create_task(self._pump_forever())
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _pump_forever(self) -> None:
        while True:
            try:
                await self.pump()
            except Exception as error:  # noqa: BLE001  the next round tries again
                logger.warning('Chat delivery round failed: %s', type(error).__qualname__)
            await asyncio.sleep(self._interval)

    async def pump(self) -> int:
        """Start the workflow of each chat's next row; returns how many were started."""
        async with self._resources.pool.connection() as connection:
            rows = await channel_store.next_rows(connection, self._resources.channels.names)
        self._started &= set(rows)
        started = 0
        for row_id in rows:
            if row_id in self._started:
                continue
            with SetWorkflowID(f'channel-send:{row_id}'):
                await DBOS.start_workflow_async(deliver, row_id)
            self._started.add(row_id)
            started += 1
        return started
