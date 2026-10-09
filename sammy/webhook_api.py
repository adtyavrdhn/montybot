"""The API for webhook triggers (`sammy.webhooks`): the user's own, from the web app, and the URL other services send
their events to. That URL needs no sign-in: the request's signature is the proof. The user's agent can call the same
API as the web app. The secrets are shown in a response, never sent through the chat, so no model or trace sees them."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, StringConstraints
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from sammy import auth, webhooks
from sammy.models import User, Webhook, WebhookSource
from sammy.resources import Resources

NOT_FOUND = JSONResponse({'detail': 'not found'}, status_code=404)
SHOWN_ONCE = {'Cache-Control': 'no-store'}


class NewWebhook(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    prompt: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)]
    """What Sammy does with each event, written as the user would ask it."""
    source: WebhookSource = 'hmac'


def resources_of(request: Request) -> Resources:
    return request.state.resources


# --- the user's webhooks ---


@auth.signed_in
async def list_webhooks(request: Request, user: User) -> Response:
    return JSONResponse([webhook_json(w) for w in await webhooks.list_for(resources_of(request).pool, user.id)])


@auth.signed_in
async def create_webhook(request: Request, user: User) -> Response:
    """POST. The webhook, with its URL and secret: the only time either is shown."""
    body = NewWebhook.model_validate_json(await request.body())
    resources = resources_of(request)
    webhook, keys = await webhooks.create(
        resources, user_id=user.id, name=body.name, prompt=body.prompt, source=body.source
    )
    return JSONResponse(with_keys(resources, webhook, keys), status_code=201, headers=SHOWN_ONCE)


@auth.signed_in
async def pause_webhook(request: Request, user: User) -> Response:
    return await set_paused(request, user, True)


@auth.signed_in
async def resume_webhook(request: Request, user: User) -> Response:
    return await set_paused(request, user, False)


async def set_paused(request: Request, user: User, paused: bool) -> Response:
    webhook_id = str(request.path_params['webhook_id'])
    webhook = await webhooks.set_paused(resources_of(request).pool, user.id, webhook_id, paused)
    return NOT_FOUND if webhook is None else JSONResponse(webhook_json(webhook))


@auth.signed_in
async def rotate_webhook(request: Request, user: User) -> Response:
    """POST. A new URL and secret, shown this once; the old ones stop working."""
    resources = resources_of(request)
    rotated = await webhooks.rotate(resources, user.id, str(request.path_params['webhook_id']))
    if rotated is None:
        return NOT_FOUND
    return JSONResponse(with_keys(resources, *rotated), headers=SHOWN_ONCE)


@auth.signed_in
async def delete_webhook(request: Request, user: User) -> Response:
    deleted = await webhooks.delete(resources_of(request).pool, user.id, str(request.path_params['webhook_id']))
    return JSONResponse({'ok': True}) if deleted else NOT_FOUND


def webhook_json(webhook: Webhook) -> dict[str, str | bool]:
    return {
        'id': webhook.id,
        'name': webhook.name,
        'prompt': webhook.prompt,
        'source': webhook.source,
        'paused': webhook.paused,
        'thread_id': webhook.thread_id,
    }


def with_keys(resources: Resources, webhook: Webhook, keys: webhooks.Keys) -> dict[str, str | bool]:
    url = f'{resources.settings.public_url}/hooks/{keys.token}'
    return {**webhook_json(webhook), 'url': url, 'secret': keys.secret}


# --- an event from another service ---

ANSWERS: dict[webhooks.Outcome, tuple[int, str]] = {
    'started': (202, 'Started.'),
    'seen': (200, 'Already received.'),
    'ping': (200, 'Ready for events.'),
    'unknown': (404, 'not found'),
    'unsigned': (401, 'The signature does not match.'),
    'paused': (409, 'This trigger is paused.'),
    'busy': (503, 'Still working on the last event. Send this one again later.'),
}


async def receive(request: Request) -> Response:
    """POST /hooks/<token>, from the service the user gave the URL to."""
    too_large = JSONResponse({'detail': f'Events can be up to {webhooks.MAX_BODY_BYTES >> 10} KB.'}, status_code=413)
    declared = request.headers.get('content-length', '')
    if declared.isdigit() and int(declared) > webhooks.MAX_BODY_BYTES:
        return too_large
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > webhooks.MAX_BODY_BYTES:
            return too_large
    token = str(request.path_params['token'])
    outcome = await webhooks.receive(resources_of(request), token, request.headers, bytes(body))
    status, detail = ANSWERS[outcome]
    headers = {'Retry-After': '60'} if outcome == 'busy' else None
    return JSONResponse({'detail': detail}, status_code=status, headers=headers)
