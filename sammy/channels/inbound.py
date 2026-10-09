"""What Sammy does with a message from a chat: one DBOS workflow per delivery.

```
receive(name, inbound)                 the webhook (sammy.channels.api), or a platform's own gateway loop
  DBOS workflow id channel:<name>:<delivery id>: a delivery the platform sends twice is handled once
receive_message                        @DBOS.workflow
  a platform with a reply window:      step channel.seen: the chat's window opens; messages held for it go out
  step channel.who                     the linked Sammy user, if any
  unlinked: step channel.link          a link code in a direct chat (or a web-issued code: linked); nothing else
  a button press: step channel.answer  the ask's answer (first answer wins, on any surface), then the ask's
                                       message says what was chosen (step channel.edit)
  text while the chat's run asks:      step channel.reply: the text answers it
  otherwise: step channel.file.<i>     each file downloaded and kept (sammy.attachments)
             step channel.start        the chat's thread and the message's run, as `POST /api/.../messages`
             workflows.start(run_id)
```

An unlinked sender never starts a run and never reaches a user's data: the answer it gets is the same for every
sender, but for its own code.
"""

from __future__ import annotations

import uuid

from dbos import DBOS, SetWorkflowID

from sammy import approvals, attachments, store, workflows
from sammy.channels import linking
from sammy.channels import store as channel_store
from sammy.channels.base import ButtonAnswer, Channel, Inbound, InboundFile, Outgoing
from sammy.channels.outbound import RECORD, SEND, ask_message, thread_url
from sammy.channels.store import Chat
from sammy.db import Connection
from sammy.models import Ask
from sammy.observability import timing
from sammy.resources import Resources, current

LINK_ELSEWHERE = 'Message me directly to link your account.'
LINKED = 'Linked. Say hi.'
SEND_THE_CODE = 'Almost there: send me the code the Sammy web page shows you.'
BUSY = 'Sammy is still working on the last message in this chat.'
ANSWERED = 'That was answered already.'
APPROVE_WORDS = ('1', 'yes', 'approve')
DECLINE_WORDS = ('2', 'no', 'decline')
ANSWER = dict[str, str | bool]


def link_message(public_url: str, code: str) -> str:
    return f'To use Sammy here, link your account: {public_url}/#/link/{code}'


async def receive(name: str, inbound: Inbound) -> None:
    """Handle one delivery from platform `name`, once however often it comes. Returns once it is recorded."""
    with SetWorkflowID(f'channel:{name}:{inbound.delivery_id}'):
        await DBOS.start_workflow_async(receive_message, name, inbound)


@DBOS.workflow(name='sammy.channel_receive')
async def receive_message(name: str, inbound: Inbound) -> str:
    resources = current()
    with timing('channel.receive') as span:
        span.set_attribute('channel', name)
        channel = resources.channels.get(name)
        if channel is None:
            return 'off'
        chat = Chat(channel=name, chat_id=inbound.chat_id)
        key = f'inbound:{name}:{inbound.delivery_id}'
        if channel.capabilities.reply_window is not None:
            await DBOS.run_step_async({**RECORD, 'name': 'channel.seen'}, seen, resources, chat, inbound)
        user_id = await DBOS.run_step_async({'name': 'channel.who'}, who, resources, name, inbound.sender_id)
        if user_id is None:
            await DBOS.run_step_async({**RECORD, 'name': 'channel.link'}, unlinked, resources, chat, inbound, key)
            return 'unlinked'
        if inbound.answer is not None:
            edit = await DBOS.run_step_async(
                {**RECORD, 'name': 'channel.answer'}, answer_button, resources, user_id, inbound.answer
            )
            if edit is not None and channel.capabilities.edits:
                await DBOS.run_step_async({**SEND, 'name': 'channel.edit'}, edit_message, name, *edit)
            return 'answered'
        if not inbound.text.strip() and not inbound.files:
            return 'empty'  # such as a press of a platform's re-engagement button: it only opened the window
        if await DBOS.run_step_async(
            {**RECORD, 'name': 'channel.reply'}, answer_text, resources, chat, user_id, inbound.text, key
        ):
            return 'answered'
        kept: list[str] = []
        notes: list[str] = []
        for i, file in enumerate(inbound.files):
            if i >= attachments.MAX_FILES:
                notes.append(f'Only the first {attachments.MAX_FILES} files were kept.')
                break
            found = await DBOS.run_step_async({**SEND, 'name': f'channel.file.{i}'}, keep_file, name, user_id, file)
            if found is None:
                notes.append(f'{file.name} is over {attachments.MAX_FILE_BYTES >> 20} MB, so it was left out.')
            else:
                kept.append(found)
        if not inbound.text.strip() and not kept:
            return 'empty'
        run_id = await DBOS.run_step_async(
            {**RECORD, 'name': 'channel.start'}, start, resources, chat, user_id, inbound, kept, notes, key
        )
        if run_id is None:
            return 'busy'
        await workflows.start(run_id)
        return 'started'


async def seen(resources: Resources, chat: Chat, inbound: Inbound) -> None:
    async with resources.pool.connection() as connection, connection.transaction():
        await channel_store.seen(connection, chat, inbound.sent_at)


async def who(resources: Resources, name: str, sender_id: str) -> str | None:
    async with resources.pool.connection() as connection:
        found = await channel_store.identity(connection, name, sender_id)
    return None if found is None else found.user_id


async def unlinked(resources: Resources, chat: Chat, inbound: Inbound, key: str) -> None:
    """Outside a direct chat, only where to link (a code there could be used by anyone in it). In a direct chat, a
    code the user got on the web links them; anything else gets their link code. Nothing here reads any user's data,
    so every sender gets the same answer, but for their own code."""
    settings = resources.settings
    async with resources.pool.connection() as connection, connection.transaction():
        if not inbound.direct:
            await channel_store.enqueue(connection, key, chat, Outgoing(text=LINK_ELSEWHERE))
            return
        code = linking.code_in(inbound.text)
        user_id = (
            await channel_store.take_user_code(connection, chat.channel, linking.hash_code(code), inbound.sender_id)
            if code
            else None
        )
        if user_id is not None:
            await channel_store.link(
                connection,
                channel=chat.channel,
                external_user_id=inbound.sender_id,
                user_id=user_id,
                notify_chat_id=inbound.chat_id,
            )
            await channel_store.enqueue(connection, key, chat, Outgoing(text=LINKED), user_id=user_id)
            return
        code = await linking.code_for_sender(
            connection,
            settings.session_secret.get_secret_value(),
            channel=chat.channel,
            external_user_id=inbound.sender_id,
            chat_id=inbound.chat_id,
        )
        await channel_store.enqueue(
            connection, key, chat, Outgoing(text=link_message(settings.public_url, code), secret=True)
        )


async def answer_button(resources: Resources, user_id: str, pressed: ButtonAnswer) -> tuple[str, str, str] | None:
    """Answer the ask; returns what to edit its message to (chat, message id, text), if it was sent to a chat."""
    async with resources.pool.connection() as connection:
        ask = await store.get_ask(connection, user_id, pressed.ask_id)
    if ask is None or ask.kind != 'approval':
        return None
    approved = pressed.choice == 'approve'
    answered = await approvals.answer(resources, user_id, ask.id, {'approved': approved, 'reason': ''})
    async with resources.pool.connection() as connection:
        sent = await channel_store.ask_message(connection, ask.id)
    if sent is None:
        return None
    chat, message_id = sent
    verdict = ('You approved.' if approved else 'You declined.') if answered else ANSWERED
    return chat.chat_id, message_id, f'{ask.prompt}\n\n{verdict}'


async def edit_message(name: str, chat_id: str, message_id: str, text: str) -> None:
    channel: Channel | None = current().channels.get(name)
    if channel is not None:
        await channel.edit(chat_id, message_id, channel.format(text))


def text_answer(ask: Ask, text: str) -> ANSWER | None:
    """What a text reply answers, or None if it does not answer this ask."""
    if ask.kind == 'question':
        return {'text': text.strip()} if text.strip() else None
    if ask.kind != 'approval':
        return None
    first, _, reason = text.strip().partition(' ')
    word = first.lower().strip('.,!')
    if word in APPROVE_WORDS:
        return {'approved': True, 'reason': ''}
    if word in DECLINE_WORDS:
        return {'approved': False, 'reason': reason.strip()}
    return None


async def answer_text(resources: Resources, chat: Chat, user_id: str, text: str, key: str) -> bool:
    """True if the chat's run is waiting for the user: the text answers it, or the ask is sent again."""
    async with resources.pool.connection() as connection:
        ask = await _open_ask(connection, chat, user_id)
    if ask is None:
        return False
    value = text_answer(ask, text)
    if value is not None and await approvals.answer(resources, user_id, ask.id, dict(value)):
        return True
    channel = resources.channels.get(chat.channel)
    if channel is None:
        return True
    async with resources.pool.connection() as connection:
        thread_id = await channel_store.thread_of_chat(connection, chat.channel, chat.chat_id, user_id)
        link = thread_url(resources.settings.public_url, thread_id or '')
        outgoing = ask_message(ask, channel.capabilities, link)
        await channel_store.enqueue(connection, key, chat, outgoing, user_id=user_id)
    return True


async def _open_ask(connection: Connection, chat: Chat, user_id: str) -> Ask | None:
    thread_id = await channel_store.thread_of_chat(connection, chat.channel, chat.chat_id, user_id)
    run = await store.latest_run(connection, user_id, thread_id) if thread_id else None
    if run is None or run.status != 'waiting':
        return None
    return await store.open_ask(connection, user_id, run.id)


async def keep_file(name: str, user_id: str, file: InboundFile) -> str | None:
    """Download the file and keep it for the message; its attachment id, or None if it is too big. Only the id is
    returned, so DBOS records no file's bytes."""
    resources = current()
    channel = resources.channels.get(name)
    if channel is None or (file.size is not None and file.size > attachments.MAX_FILE_BYTES):
        return None
    data = await channel.download(file)
    if len(data) > attachments.MAX_FILE_BYTES or not data:
        return None
    async with resources.pool.connection() as connection:
        kept = await attachments.upload(connection, user_id, file.name, file.media_type, data)
    return kept.id


def run_id_of(chat: Chat, delivery_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'sammy:channel-run:{chat.channel}:{chat.chat_id}:{delivery_id}'))


async def start(
    resources: Resources,
    chat: Chat,
    user_id: str,
    inbound: Inbound,
    kept: list[str],
    notes: list[str],
    key: str,
) -> str | None:
    """The message's run, in the chat's thread (made on first use); None if the thread is busy, which the chat is
    told. The same path as a message on the web (`workflows.create_message_run`)."""
    run_id = run_id_of(chat, inbound.delivery_id)
    text = inbound.text.strip()
    async with resources.pool.connection() as connection, connection.transaction():
        if await store.get_run(connection, user_id, run_id) is not None:
            return run_id  # an earlier attempt of this step committed
        if notes:
            await channel_store.enqueue(connection, f'{key}:notes', chat, Outgoing(text='\n'.join(notes)))
        thread_id = await channel_store.thread_of_chat(connection, chat.channel, chat.chat_id, user_id)
        if thread_id is None:
            thread = await store.create_thread(connection, user_id, text.splitlines()[0] if text else '')
            thread_id = thread.id
            await channel_store.add_chat(
                connection, channel=chat.channel, chat_id=chat.chat_id, user_id=user_id, thread_id=thread_id
            )
        try:
            async with connection.transaction():
                names = await workflows.create_message_run(
                    connection, user_id=user_id, thread_id=thread_id, run_id=run_id, text=text, attachment_ids=kept
                )
                await channel_store.add_run(connection, run_id, chat.channel, chat.chat_id)
        except store.ActiveRun:
            await channel_store.enqueue(connection, key, chat, Outgoing(text=BUSY), user_id=user_id)
            return None
        except (attachments.AttachmentGone, attachments.TooMuch) as error:
            await channel_store.enqueue(connection, key, chat, Outgoing(text=str(error)), user_id=user_id)
            return None
        if not text and names:
            await store.rename_thread(connection, user_id, thread_id, names[0][:120])
    return run_id
