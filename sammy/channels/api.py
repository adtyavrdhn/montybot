"""The chat apps' HTTP side: each platform's webhook, and the web app's endpoints for linking accounts.

A webhook request is size-capped, then signature-checked before anything in it is read; each message it carries is
recorded as a DBOS workflow (`inbound.receive`), and the platform gets its answer at once.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, StrictBool, StringConstraints
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from sammy import auth
from sammy.channels import inbound, linking
from sammy.channels import store as channel_store
from sammy.channels.base import Outgoing, RawRequest
from sammy.channels.store import Chat
from sammy.models import User
from sammy.resources import Resources

MAX_WEBHOOK_BYTES = 1024 * 1024
NOT_FOUND = JSONResponse({'detail': 'not found'}, status_code=404)
BAD_CODE = JSONResponse(
    {'detail': 'That link has expired or was used already. Message Sammy again for a new one.'}, 404
)


def _resources(request: Request) -> Resources:
    return request.state.resources


async def webhook(request: Request) -> Response:
    """POST (and GET, for a platform's URL handshake) `/api/channels/<name>/webhook`."""
    channel = _resources(request).channels.get(str(request.path_params['name']))
    if channel is None:
        return NOT_FOUND
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_WEBHOOK_BYTES:
            return JSONResponse({'detail': 'too large'}, status_code=413)
    raw = RawRequest(
        method=request.method,
        path=request.url.path,
        headers={name.lower(): value for name, value in request.headers.items()},
        query=dict(request.query_params),
        body=bytes(body),
    )
    if (answer := channel.challenge(raw)) is not None:
        return answer
    if not channel.verify(raw):
        return JSONResponse({'detail': 'bad signature'}, status_code=401)
    try:
        messages = channel.parse(raw)
    except ValueError:
        return JSONResponse({'detail': 'unreadable'}, status_code=400)
    for message in messages:
        await inbound.receive(channel.name, message)
    return channel.ack()


class LinkBody(BaseModel):
    code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class NotifyBody(BaseModel):
    notify: StrictBool


@auth.signed_in
async def link_account(request: Request, user: User) -> Response:
    """POST `{code}` (from `/#/link/<code>`): a code for this user that only the platform account that got the link
    can send to the bot, which links it. Opening someone else's link therefore links nothing on its own."""
    body = LinkBody.model_validate_json(await request.body())
    async with _resources(request).pool.connection() as connection, connection.transaction():
        taken = await channel_store.take_sender_code(connection, linking.hash_code(body.code))
        if taken is None:
            return BAD_CODE
        channel, sender, chat_id = taken
        code = await linking.code_for_user(connection, user.id, channel, sender=sender)
        if chat_id is not None:
            chat = Chat(channel=channel, chat_id=chat_id)
            outgoing = Outgoing(text=inbound.SEND_THE_CODE)
            await channel_store.enqueue(connection, f'confirm:{linking.hash_code(body.code)}', chat, outgoing)
    return JSONResponse({'channel': channel, 'code': code, 'minutes': linking.CODE_MINUTES})


@auth.signed_in
async def new_code(request: Request, user: User) -> Response:
    """POST: a code for this user to send to the bot (`/start <code>`)."""
    name = str(request.path_params['name'])
    if _resources(request).channels.get(name) is None:
        return NOT_FOUND
    async with _resources(request).pool.connection() as connection:
        code = await linking.code_for_user(connection, user.id, name)
    return JSONResponse({'code': code, 'minutes': linking.CODE_MINUTES})


@auth.signed_in
async def list_channels(request: Request, user: User) -> Response:
    """The platforms this server talks to, and the user's linked accounts."""
    async with _resources(request).pool.connection() as connection:
        linked = await channel_store.identities_of(connection, user.id)
    return JSONResponse(
        {
            'enabled': _resources(request).channels.names,
            'linked': [
                {'channel': i.channel, 'notify': i.notify, 'pings': i.notify_chat_id is not None,
                 'linked_at': i.linked_at.isoformat()}
                for i in linked
            ],
        }
    )  # fmt: skip


@auth.signed_in
async def set_notify(request: Request, user: User) -> Response:
    """PUT `{notify}`: whether Sammy pings the user in this app."""
    body = NotifyBody.model_validate_json(await request.body())
    async with _resources(request).pool.connection() as connection:
        changed = await channel_store.set_notify(connection, user.id, str(request.path_params['name']), body.notify)
    return JSONResponse({'ok': True}) if changed else NOT_FOUND


@auth.signed_in
async def unlink(request: Request, user: User) -> Response:
    async with _resources(request).pool.connection() as connection, connection.transaction():
        removed = await channel_store.unlink(connection, user.id, str(request.path_params['name']))
    return JSONResponse({'ok': True}) if removed else NOT_FOUND
