# Grown from viktor c1896df (viktor/api.py): the same JSON-over-Starlette style, scoped to the signed-in user instead
# of an API token and a workspace.
"""The web app's API. Every endpoint but sign-up and sign-in acts as the signed-in user, on that user's rows only;
another user's thread, run or ask answers 404, the same as one that does not exist."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import re
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any, TypeVar
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, Field, StringConstraints
from pydantic_ai.messages import ModelMessage, ModelRequest, TextPart, UserPromptPart
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from montybot import approvals, auth, schedules, store, streaming, workflows
from montybot.browser.contract import (
    BrowserError,
)
from montybot.browser.state import BLANK_URL
from montybot.memory import delete_memory, list_memories
from montybot.models import ACTIVE, Ask, Run, Schedule, User
from montybot.notifications import TakenEndpoint, add_subscription, remove_subscription
from montybot.resources import Resources
from montybot.signins import PostgresLease

T = TypeVar('T')
NOT_FOUND = JSONResponse({'detail': 'not found'}, status_code=404)


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=1024)
    name: str = Field(default='', max_length=120)


class NewMessage(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)]


class Answer(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)] | None = None
    """For a question."""
    approved: bool | None = None
    """For an approval."""
    reason: str | None = Field(default=None, max_length=2_000)
    done: bool | None = None
    """For a hand-off: the user hands the browser back."""
    note: str | None = Field(default=None, max_length=2_000)


def resources_of(request: Request) -> Resources:
    return request.state.resources


# --- accounts ---


async def sign_up(request: Request) -> Response:
    if (refused := auth.refuse_non_json(request)) is not None:
        return refused
    body = Credentials.model_validate_json(await request.body())
    async with resources_of(request).pool.connection() as connection:
        user = await store.create_user(
            connection, body.email.strip().lower(), auth.hash_password(body.password), body.name.strip()
        )
    if user is None:
        return JSONResponse({'detail': 'that email already has an account'}, status_code=409)
    auth.sign_in(request, user)
    return JSONResponse(user_json(user), status_code=201)


async def sign_in(request: Request) -> Response:
    if (refused := auth.refuse_non_json(request)) is not None:
        return refused
    body = Credentials.model_validate_json(await request.body())
    async with resources_of(request).pool.connection() as connection:
        found = await store.find_login(connection, body.email.strip().lower())
    if found is None or not auth.check_password(body.password, found[1]):
        return JSONResponse({'detail': 'wrong email or password'}, status_code=401)
    auth.sign_in(request, found[0])
    return JSONResponse(user_json(found[0]))


async def sign_out(request: Request) -> Response:
    if (refused := auth.refuse_non_json(request)) is not None:
        return refused
    auth.sign_out(request)
    return JSONResponse({'ok': True})


@auth.signed_in
async def me(request: Request, user: User) -> Response:
    return JSONResponse(user_json(user))


# --- threads and runs ---


@auth.signed_in
async def create_thread(request: Request, user: User) -> Response:
    body = NewMessage.model_validate_json(await request.body())
    resources = resources_of(request)
    run_id = str(uuid.uuid4())
    async with resources.pool.connection() as connection, connection.transaction():
        thread = await store.create_thread(connection, user.id, body.text.splitlines()[0])
        await store.create_run(
            connection, run_id=run_id, user_id=user.id, thread_id=thread.id, prompt=body.text, trigger='message'
        )
    await workflows.start(run_id)
    return JSONResponse({'thread_id': thread.id, 'run_id': run_id}, status_code=201)


@auth.signed_in
async def add_message(request: Request, user: User) -> Response:
    body = NewMessage.model_validate_json(await request.body())
    resources = resources_of(request)
    run_id = str(uuid.uuid4())
    async with resources.pool.connection() as connection, connection.transaction():
        thread = await store.get_thread(connection, user.id, request.path_params['thread_id'])
        if thread is None:
            return NOT_FOUND
        try:
            await store.create_run(
                connection, run_id=run_id, user_id=user.id, thread_id=thread.id, prompt=body.text, trigger='message'
            )
        except store.ActiveRun as error:
            return JSONResponse({'detail': str(error)}, status_code=409)
    await workflows.start(run_id)
    return JSONResponse({'thread_id': thread.id, 'run_id': run_id}, status_code=201)


@auth.signed_in
async def list_threads(request: Request, user: User) -> Response:
    async with resources_of(request).pool.connection() as connection:
        threads = await store.list_threads(connection, user.id)
    return JSONResponse([{'id': t.id, 'title': t.title} for t in threads])


@auth.signed_in
async def read_thread(request: Request, user: User) -> Response:
    async with resources_of(request).pool.connection() as connection:
        thread = await store.get_thread(connection, user.id, request.path_params['thread_id'])
        if thread is None:
            return NOT_FOUND
        history = await store.load_history(connection, thread.id)
        run = await store.latest_run(connection, user.id, thread.id)
        run_json = None if run is None else await run_view(connection, user, run)
    messages = chat_messages(history)
    if run is not None and run.status in ACTIVE:
        messages.append({'role': 'user', 'text': run.prompt})  # not in the history until the run finishes
    return JSONResponse({'id': thread.id, 'title': thread.title, 'messages': messages, 'run': run_json})


@auth.signed_in
async def read_run(request: Request, user: User) -> Response:
    async with resources_of(request).pool.connection() as connection:
        run = await store.get_run(connection, user.id, request.path_params['run_id'])
        if run is None:
            return NOT_FOUND
        return JSONResponse(await run_view(connection, user, run))


@auth.signed_in
async def run_events(request: Request, user: User) -> Response:
    """Full replacement previews are provisional; only the stored run decides completion.

    Signed cookies are stateless: a connection cannot observe logout/cookie replacement in another request.
    Recheck the account and run each poll; reconnect every five minutes to revalidate cookie signature/age.
    The UI closes its own connection on logout. Immediate server-side session revocation is not available.
    """
    resources = resources_of(request)
    run_id = str(request.path_params['run_id'])
    async with resources.pool.connection() as connection:
        if await store.get_run(connection, user.id, run_id) is None:
            return NOT_FOUND  # before sending streaming headers, including for another user's run

    async def events() -> AsyncIterator[str]:
        previous_view: dict[str, Any] | None = None
        previous_preview: dict[str, Any] | None = None
        # Periodically reconnect so SessionMiddleware validates the cookie signature/age again.
        deadline = asyncio.get_running_loop().time() + 300
        while asyncio.get_running_loop().time() < deadline and not await request.is_disconnected():
            # SessionMiddleware cookies are stateless: another request's logout cannot revoke this cookie.
            # Recheck the account and ownership nonetheless; no connection is held while sending or sleeping.
            current_user = await auth.signed_in_user(request)
            if current_user is None or current_user.id != user.id:
                return
            async with resources.pool.connection() as connection:
                run = await store.get_run(connection, user.id, run_id)
                if run is None:
                    return
                view = await run_view(connection, current_user, run)
            if view != previous_view:
                yield f'event: status\ndata: {json.dumps(view)}\n\n'
                previous_view = view
            if run.status not in ACTIVE:
                return  # never send a stale preview after authoritative completion
            preview = streaming.snapshot(run_id)
            if preview != previous_preview:
                yield f'event: preview\ndata: {json.dumps(preview)}\n\n'
                previous_preview = dict(preview)
            else:
                yield ': keep-alive\n\n'
            await asyncio.sleep(0.75)

    return StreamingResponse(
        events(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'},
    )


async def run_view(connection: Any, user: User, run: Run) -> dict[str, Any]:
    ask = await store.open_ask(connection, user.id, run.id) if run.status == 'waiting' else None
    return {
        'id': run.id,
        'thread_id': run.thread_id,
        'status': run.status,
        'output': run.output,
        'activity': await store.list_activity(connection, user.id, run.id),
        'ask': None if ask is None else ask_json(ask),
    }


# --- answering the run ---


@auth.signed_in
async def answer_ask(request: Request, user: User) -> Response:
    body = Answer.model_validate_json(await request.body())
    resources = resources_of(request)
    async with resources.pool.connection() as connection:
        ask = await store.get_ask(connection, user.id, request.path_params['ask_id'])
    if ask is None:
        return NOT_FOUND
    match ask.kind:
        case 'question':
            if body.text is None:
                return JSONResponse({'detail': 'answer with text'}, status_code=422)
            value: dict[str, Any] = {'text': body.text}
        case 'approval':
            if body.approved is None:
                return JSONResponse({'detail': 'answer with approved: true or false'}, status_code=422)
            value = {'approved': body.approved, 'reason': body.reason or ''}
        case 'handoff':
            if not body.done:
                return JSONResponse({'detail': 'answer with done: true when you hand the browser back'}, 422)
            value = {'done': True, 'note': body.note or ''}
    if not await approvals.answer(resources, user.id, ask.id, value):
        return JSONResponse({'detail': 'that was answered already'}, status_code=409)
    return JSONResponse({'ok': True})


# --- the live view: the user drives the run's browser during a hand-off (montybot.live) ---


async def open_handoff(request: Request, user: User) -> tuple[Run, Ask] | None:
    """The run and its hand-off ask, if the run is the user's and waits for them to hand the browser back."""
    async with resources_of(request).pool.connection() as connection:
        run = await store.get_run(connection, user.id, str(request.path_params['run_id']))
        if run is None or run.status != 'waiting':
            return None
        ask = await store.open_ask(connection, user.id, run.id)
    if ask is None or ask.kind != 'handoff':
        return None
    return run, ask


async def active_handoff(resources: Resources, run: Run, ask: Ask) -> str | None:
    """The run's active hand-off id, recorded on the ask. If the browser service lost the hand-off (it restarted), a
    new one starts on the saved page and its id replaces the old. The browser starts first, outside any transaction;
    then the ask's row is locked to record the id, so an answer waits for that to commit and the run then ends the
    hand-off recorded here. If the ask was answered meanwhile, a hand-off started here for it is ended again and the
    result is None."""
    browser = resources.browser
    await browser.start(run_id=run.id, user_id=run.user_id)
    handoff = await browser.start_handoff(run_id=run.id, user_id=run.user_id, reason=ask.prompt)
    async with resources.pool.connection() as connection, connection.transaction():
        if await store.lock_open_ask(connection, ask.id):
            if handoff.handoff_id != ask.details.get('handoff_id'):
                await store.set_handoff(connection, ask.id, handoff.handoff_id)
            return handoff.handoff_id
        owner = await store.find_handoff(connection, handoff.handoff_id)
    if owner is None or owner.id == ask.id:  # not a later ask's hand-off
        with contextlib.suppress(BrowserError):
            await browser.end_handoff(run_id=run.id, user_id=run.user_id, handoff_id=handoff.handoff_id)
    return None


@auth.signed_in
async def live_link(request: Request, user: User) -> Response:
    """POST. Where the user takes over the run's browser: `/live/handoff/<id>`. The link names the hand-off; the page
    only opens for this user. A POST, as it may start the browser and a hand-off."""
    found = await open_handoff(request, user)
    if found is None:
        return NOT_FOUND
    run, ask = found
    try:
        handoff_id = await active_handoff(resources_of(request), run, ask)
    except BrowserError as error:
        return JSONResponse({'detail': str(error)}, status_code=409)
    if handoff_id is None:
        return NOT_FOUND
    return JSONResponse({'url': f'/live/handoff/{handoff_id}', 'reason': ask.prompt})


@auth.signed_in
async def watch_screen(request: Request, user: User) -> Response:
    """The bot's browser as it works, for the user to watch: read only, and only while the run is running (during a
    hand-off the user has the live view instead)."""
    resources = resources_of(request)
    async with resources.pool.connection() as connection:
        run = await store.get_run(connection, user.id, str(request.path_params['run_id']))
    if run is None or run.status != 'running':
        return NOT_FOUND
    try:
        screenshot = await resources.browser.peek_screenshot(run_id=run.id, user_id=user.id)
    except BrowserError:
        return NOT_FOUND  # no open browser, busy with a call, or a hand-off began meanwhile
    return Response(screenshot.png, media_type='image/png', headers={'Cache-Control': 'no-store'})


# --- notifications ---


PUSH_HOSTS = ('fcm.googleapis.com', 'web.push.apple.com')
PUSH_HOST_SUFFIXES = ('.push.services.mozilla.com', '.notify.windows.com')
"""The push services of Chrome, Safari, Firefox and Edge. A subscription elsewhere is refused."""


def push_endpoint(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    host = parts.hostname or ''
    if parts.scheme != 'https' or parts.port is not None or parts.username is not None:
        raise ValueError('not a push service endpoint')
    if host not in PUSH_HOSTS and not host.endswith(PUSH_HOST_SUFFIXES):
        raise ValueError('not a push service endpoint')
    return endpoint


def base64url_of(length: int) -> Callable[[str], str]:
    """A base64url string (padding optional) that decodes to `length` bytes."""

    def check(value: str) -> str:
        if re.fullmatch(r'[A-Za-z0-9_-]+={0,2}', value) is None:
            raise ValueError('not base64url')
        try:
            decoded = base64.urlsafe_b64decode(value.rstrip('=') + '=' * (-len(value.rstrip('=')) % 4))
        except binascii.Error:
            raise ValueError('not base64url') from None
        if len(decoded) != length:
            raise ValueError(f'must be base64url of {length} bytes')
        return value

    return check


class PushKeys(BaseModel):
    p256dh: Annotated[str, Field(max_length=100), AfterValidator(base64url_of(65))]
    """The browser's P-256 public key, uncompressed."""
    auth: Annotated[str, Field(max_length=40), AfterValidator(base64url_of(16))]


class PushSubscription(BaseModel):
    endpoint: Annotated[str, Field(max_length=2_000), AfterValidator(push_endpoint)]
    keys: PushKeys


class PushEndpoint(BaseModel):
    endpoint: str = Field(max_length=2_000)


@auth.signed_in
async def push_key(request: Request, user: User) -> Response:
    return JSONResponse({'public_key': resources_of(request).settings.vapid_public_key})


@auth.signed_in
async def add_push_subscription(request: Request, user: User) -> Response:
    body = PushSubscription.model_validate_json(await request.body())
    async with resources_of(request).pool.connection() as connection:
        try:
            await add_subscription(connection, user.id, body.endpoint, body.keys.model_dump())
        except TakenEndpoint:
            return JSONResponse({'detail': 'that push subscription belongs to another account'}, status_code=409)
    return JSONResponse({'ok': True}, status_code=201)


@auth.signed_in
async def remove_push_subscription(request: Request, user: User) -> Response:
    body = PushEndpoint.model_validate_json(await request.body())
    async with resources_of(request).pool.connection() as connection:
        removed = await remove_subscription(connection, user.id, body.endpoint)
    return JSONResponse({'ok': True}) if removed else NOT_FOUND


# --- saved sign-ins ---


def site_of(domain: str) -> str:
    return domain.lstrip('.')


@auth.signed_in
async def read_sign_ins(request: Request, user: User) -> Response:
    """The sites the user's saved browser holds cookies for. Names only, never values."""
    state = await resources_of(request).jar.load(user_id=user.id)
    sites = sorted({site_of(c.domain) for c in state.cookies}) if state is not None else []
    return JSONResponse([{'site': site} for site in sites])


@auth.signed_in
async def forget_sign_in(request: Request, user: User) -> Response:
    """Drop the cookies and storage of one site. Refused while a task of the user's is using the browser."""
    resources = resources_of(request)
    site = str(request.path_params['site'])
    holder = f'forget:{uuid.uuid4()}'
    lease = PostgresLease(resources.pool, seconds=60)  # short: a crash here must not lock the user out for long
    if not await lease.acquire(user_id=user.id, run_id=holder):
        return JSONResponse({'detail': 'a task is using your browser; try again when it has finished'}, 409)
    try:
        state = await resources.jar.load(user_id=user.id)
        if state is None or not any(site_of(c.domain) == site for c in state.cookies):
            return NOT_FOUND

        def of_site(origin: str) -> bool:
            host = urlsplit(origin).hostname or ''
            return host == site or host.endswith('.' + site)

        state.cookies = [c for c in state.cookies if site_of(c.domain) != site]
        state.local_storage = {o: items for o, items in state.local_storage.items() if not of_site(o)}
        state.session_storage = {o: items for o, items in state.session_storage.items() if not of_site(o)}
        if of_site(state.url):
            state.url = BLANK_URL
        await resources.jar.save(user_id=user.id, state=state)
    finally:
        await lease.release(user_id=user.id, run_id=holder)
    return JSONResponse({'ok': True})


# --- schedules ---


@auth.signed_in
async def list_schedules(request: Request, user: User) -> Response:
    found = await schedules.list_for(resources_of(request).pool, user.id)
    return JSONResponse([schedule_json(s, paused) for s, paused in found])


@auth.signed_in
async def pause_schedule(request: Request, user: User) -> Response:
    return await set_paused(request, user, True)


@auth.signed_in
async def resume_schedule(request: Request, user: User) -> Response:
    return await set_paused(request, user, False)


async def set_paused(request: Request, user: User, paused: bool) -> Response:
    schedule_id = str(request.path_params['schedule_id'])
    schedule = await schedules.set_paused(resources_of(request).pool, user.id, schedule_id, paused)
    return NOT_FOUND if schedule is None else JSONResponse(schedule_json(schedule, paused))


@auth.signed_in
async def delete_schedule(request: Request, user: User) -> Response:
    deleted = await schedules.delete(resources_of(request).pool, user.id, str(request.path_params['schedule_id']))
    return JSONResponse({'ok': True}) if deleted else NOT_FOUND


# --- memory ---


@auth.signed_in
async def read_memories(request: Request, user: User) -> Response:
    async with resources_of(request).pool.connection() as connection:
        return JSONResponse(await list_memories(connection, user.id))


@auth.signed_in
async def remove_memory(request: Request, user: User) -> Response:
    async with resources_of(request).pool.connection() as connection:
        deleted = await delete_memory(connection, user.id, str(request.path_params['memory_id']))
    return JSONResponse({'ok': True}) if deleted else NOT_FOUND


# --- shapes ---


def user_json(user: User) -> dict[str, str]:
    return {'id': user.id, 'email': user.email, 'name': user.name}


def schedule_json(schedule: Schedule, paused: bool) -> dict[str, Any]:
    return {
        'id': schedule.id,
        'name': schedule.name,
        'when': f'{schedule.when} ({schedule.cron}, {schedule.timezone})',
        'paused': paused,
        'watch': schedule.watch,
        'thread_id': schedule.thread_id,
    }


def ask_json(ask: Ask) -> dict[str, Any]:
    """A hand-off's id stays on the server: the live view finds it from the signed-in user's open ask."""
    return {'id': ask.id, 'kind': ask.kind, 'prompt': ask.prompt}


def chat_messages(history: list[ModelMessage]) -> list[dict[str, str]]:
    """What the user sees of the history: their messages and the agent's words, not its tool calls."""
    shown: list[dict[str, str]] = []
    for message in history:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    shown.append({'role': 'user', 'text': part.content})
        else:
            text = '\n'.join(p.content for p in message.parts if isinstance(p, TextPart)).strip()
            if text and not message.tool_calls:
                shown.append({'role': 'assistant', 'text': text})
    return shown
