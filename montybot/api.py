# Grown from viktor c1896df (viktor/api.py): the same JSON-over-Starlette style, scoped to the signed-in user instead
# of an API token and a workspace.
"""The web app's API. Every endpoint but sign-up and sign-in acts as the signed-in user, on that user's rows only;
another user's thread, run or ask answers 404, the same as one that does not exist."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import html
import json
import logging
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, TypeVar
from urllib.parse import quote, urlsplit

import logfire
from pydantic import AfterValidator, BaseModel, Field, StrictBool, StringConstraints
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from montybot import approvals, auth, schedules, store, streaming, workflows
from montybot.browser.contract import (
    BrowserError,
)
from montybot.browser.state import BLANK_URL, BrowserState
from montybot.integrations import IntegrationError, mcp
from montybot.memory import delete_memory, list_memories
from montybot.models import ACTIVE, Ask, Run, Schedule, User
from montybot.notifications import TakenEndpoint, add_subscription, remove_subscription, send_email
from montybot.resources import Resources
from montybot.signins import PostgresLease
from montybot.workspaces import MAX_DOWNLOAD_BYTES, FileTooLarge, download_name

T = TypeVar('T')
logger = logging.getLogger(__name__)
NOT_FOUND = JSONResponse({'detail': 'not found'}, status_code=404)


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=1024)
    name: str = Field(default='', max_length=120)


class NewMessage(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)]
    timezone: str | None = Field(default=None, max_length=64)
    """The IANA time zone of the user's browser, such as `Europe/London`."""
    squirrel_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=24)] | None = None
    """What the user named their squirrel in the Mac app; empty to forget it. The web app sends none."""


async def remember_about_user(connection: Any, user: User, message: NewMessage) -> None:
    """Keep what the user's app reports along with the message, where it has changed: the time zone (if it is a
    real one) and the squirrel's name."""
    timezone = message.timezone
    if timezone is not None and timezone != user.timezone and schedules.is_timezone(timezone):
        await store.set_timezone(connection, user.id, timezone)
    if message.squirrel_name is not None and message.squirrel_name != user.squirrel_name:
        await store.set_squirrel_name(connection, user.id, message.squirrel_name)


class ThreadChange(BaseModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class Answer(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)] | None = None
    """For a question."""
    approved: StrictBool | None = None
    """For an approval."""
    reason: str | None = Field(default=None, max_length=2_000)
    done: StrictBool | None = None
    """For a hand-off: the user hands the browser back."""
    note: str | None = Field(default=None, max_length=2_000)
    connected: StrictBool | None = None
    """For a connect ask: true once they have connected it (the run checks), false for not now."""


async def hashed(secret: str) -> str:
    """scrypt takes tens of milliseconds of CPU: off the event loop, so other requests and streams keep going."""
    return await asyncio.to_thread(auth.hash_password, secret)


async def password_matches(secret: str, stored: str) -> bool:
    return await asyncio.to_thread(auth.check_password, secret, stored)


def resources_of(request: Request) -> Resources:
    return request.state.resources


# --- accounts ---


async def sign_up(request: Request) -> Response:
    if (refused := auth.refuse_non_json(request)) is not None:
        return refused
    body = Credentials.model_validate_json(await request.body())
    async with resources_of(request).pool.connection() as connection:
        user = await store.create_user(
            connection, body.email.strip().lower(), await hashed(body.password), body.name.strip()
        )
    if user is None:
        return JSONResponse({'detail': 'That email already has an account. Sign in instead.'}, status_code=409)
    auth.sign_in(request, user)
    return JSONResponse(user_json(user), status_code=201)


async def sign_in(request: Request) -> Response:
    if (refused := auth.refuse_non_json(request)) is not None:
        return refused
    body = Credentials.model_validate_json(await request.body())
    async with resources_of(request).pool.connection() as connection:
        found = await store.find_login(connection, body.email.strip().lower())
    if found is None or not await password_matches(body.password, found[1]):
        return JSONResponse({'detail': 'Wrong email or password.'}, status_code=401)
    auth.sign_in(request, found[0])
    return JSONResponse(user_json(found[0]))


class ResetRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class ResetConfirm(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    code: str = Field(min_length=1, max_length=20)
    password: str = Field(min_length=8, max_length=1024)


RESET_MINUTES = 15
RESET_ATTEMPTS = 5


async def request_password_reset(request: Request) -> Response:
    """POST. Emails a 6-digit code to the account's address. The answer is the same whether or not there is an
    account, so the endpoint does not tell who has one."""
    if (refused := auth.refuse_non_json(request)) is not None:
        return refused
    body = ResetRequest.model_validate_json(await request.body())
    resources = resources_of(request)
    code = f'{secrets.randbelow(1_000_000):06d}'
    async with resources.pool.connection() as connection:
        found = await store.find_login(connection, body.email.strip().lower())
        if found is not None:
            await store.start_password_reset(connection, found[0].id, await hashed(code), RESET_MINUTES)
    if found is not None:
        await asyncio.to_thread(
            send_email,
            resources.settings,
            found[0].email,
            'Your Monty password reset code',
            f'Your code is {code}. It works for {RESET_MINUTES} minutes.\n\n'
            'If you did not ask to reset your password, ignore this email; your password stays as it is.',
        )
    return JSONResponse({'ok': True, 'email_configured': bool(resources.settings.smtp_url)})


async def confirm_password_reset(request: Request) -> Response:
    """POST. With the emailed code, sets a new password and signs in."""
    if (refused := auth.refuse_non_json(request)) is not None:
        return refused
    body = ResetConfirm.model_validate_json(await request.body())
    wrong = JSONResponse({'detail': 'that code is wrong or has expired; ask for a new one'}, status_code=400)
    async with resources_of(request).pool.connection() as connection:
        found = await store.find_login(connection, body.email.strip().lower())
        if found is None:
            return wrong
        code_hash = await store.password_reset(connection, found[0].id, RESET_ATTEMPTS)
        if code_hash is None or not await password_matches(body.code.strip(), code_hash):
            return wrong
        await store.finish_password_reset(connection, found[0].id, await hashed(body.password))
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
        await remember_about_user(connection, user, body)
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
        await remember_about_user(connection, user, body)
        try:
            await store.create_run(
                connection, run_id=run_id, user_id=user.id, thread_id=thread.id, prompt=body.text, trigger='message'
            )
        except store.ActiveRun as error:
            return JSONResponse({'detail': str(error)}, status_code=409)
        except store.ThreadGone:
            return NOT_FOUND
    await workflows.start(run_id)
    return JSONResponse({'thread_id': thread.id, 'run_id': run_id}, status_code=201)


@auth.signed_in
async def search_threads(request: Request, user: User) -> Response:
    """GET `?q=`. The ids of the user's threads whose title, tasks or replies mention `q`, latest first."""
    query = request.query_params.get('q', '').strip()[:200]
    if len(query) < 2:
        return JSONResponse({'ids': []})
    async with resources_of(request).pool.connection() as connection:
        return JSONResponse({'ids': await store.search_threads(connection, user.id, query)})


@auth.signed_in
async def list_threads(request: Request, user: User) -> Response:
    """Each thread with the status of its unfinished run, if it has one: `running`, `waiting` (for the user) or
    `queued`; and otherwise how its latest run ended (`outcome`: `done`, `failed` or `stopped`); and when it last had
    something happen (`updated_at`, ISO 8601), which is also the order of the list; and for a waiting thread, what
    it waits for (`waiting_for`: `question`, `approval` or `handoff`)."""
    async with resources_of(request).pool.connection() as connection:
        threads = await store.list_threads(connection, user.id)
        active = await store.active_runs(connection, user.id)
        outcomes = await store.latest_outcomes(connection, user.id)
        last_active = await store.last_active(connection, user.id)
        waiting = await store.waiting_for(connection, user.id)
    return JSONResponse(
        [
            {
                'id': t.id,
                'title': t.title,
                'status': active.get(t.id),
                'outcome': outcomes.get(t.id),
                'updated_at': at.isoformat() if (at := last_active.get(t.id)) else None,
                'waiting_for': waiting.get(t.id) if active.get(t.id) == 'waiting' else None,
            }
            for t in threads
        ]
    )


@auth.signed_in
async def rename_thread(request: Request, user: User) -> Response:
    """PATCH. A new title for the thread."""
    body = ThreadChange.model_validate_json(await request.body())
    async with resources_of(request).pool.connection() as connection:
        renamed = await store.rename_thread(connection, user.id, str(request.path_params['thread_id']), body.title)
    return JSONResponse({'ok': True}) if renamed else NOT_FOUND


@auth.signed_in
async def delete_thread(request: Request, user: User) -> Response:
    """DELETE. The thread and everything in it. A run still going is stopped first (which frees its browser), and a
    schedule that reports to the thread is deleted with it, so nothing fires for a thread that is gone."""
    resources = resources_of(request)
    thread_id = str(request.path_params['thread_id'])
    async with resources.pool.connection() as connection:
        if await store.get_thread(connection, user.id, thread_id) is None:
            return NOT_FOUND
        run = await store.latest_run(connection, user.id, thread_id)
        schedule = await store.thread_schedule(connection, user.id, thread_id)
    if run is not None and run.status in ACTIVE:
        await workflows.stop(resources, run)
    if schedule is not None:
        await schedules.delete(resources.pool, user.id, schedule.id)
    async with resources.pool.connection() as connection:
        deleted = await store.delete_thread(connection, user.id, thread_id)
    # A schedule that never ran takes its empty thread with it (store.delete_schedule): gone either way.
    return JSONResponse({'ok': True}) if deleted or schedule is not None else NOT_FOUND


@auth.signed_in
async def read_thread(request: Request, user: User) -> Response:
    async with resources_of(request).pool.connection() as connection:
        # Messages and status must describe the same instant, even if a workflow finishes between the reads.
        await connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        thread = await store.get_thread(connection, user.id, request.path_params['thread_id'])
        if thread is None:
            return NOT_FOUND
        runs = await store.list_runs(connection, user.id, thread.id)
        asks = await store.list_answered_asks(connection, user.id, thread.id)
        run_json = None if not runs else await run_view(connection, user, runs[-1])
        activity = await store.list_thread_activity(connection, user.id, thread.id)
    messages, replies = chat_messages(runs, asks)
    # What Monty did for each earlier reply (the latest run's steps are in `run`): `after` is the reply's position in
    # `messages`, where an app shows them folded.
    steps = [
        {
            'after': replies[run.id],
            'activity': activity[run.id],
            'started_at': run.started_at.isoformat() if run.started_at else None,
            'completed_at': run.completed_at.isoformat() if run.completed_at else None,
        }
        for run in runs[:-1]
        if run.id in replies and activity.get(run.id)
    ]
    return JSONResponse({'id': thread.id, 'title': thread.title, 'messages': messages, 'run': run_json, 'steps': steps})


def chat_messages(runs: list[Run], asks: list[Ask]) -> tuple[list[dict[str, str]], dict[str, int]]:
    """The chat as the user sees it, and where each run's reply is in it, by run id. Each run is their message, what
    Monty asked them and how they answered, and Monty's reply once the run has finished. `event` lines record
    approvals and hand-offs."""
    asks_of_run: dict[str, list[Ask]] = {}
    for ask in asks:
        asks_of_run.setdefault(ask.run_id, []).append(ask)
    shown: list[dict[str, str]] = []
    replies: dict[str, int] = {}
    for run in runs:
        shown.append({'role': 'user', 'text': run.prompt})
        for ask in asks_of_run.get(run.id, []):
            shown.extend(ask_messages(ask))
        if run.output:
            replies[run.id] = len(shown)
            shown.append({'role': 'assistant', 'text': run.output})
    return shown, replies


def ask_messages(ask: Ask) -> list[dict[str, str]]:
    """An answered ask as chat lines."""
    answer = ask.answer or {}
    if answer.get('closed'):
        return []  # the run ended (it was stopped) before the user answered
    if answer.get('expired'):
        return [{'role': 'event', 'text': f'Not answered in time: {ask.prompt}'}]
    match ask.kind:
        case 'question':
            return [{'role': 'assistant', 'text': ask.prompt}, {'role': 'user', 'text': str(answer.get('text', ''))}]
        case 'approval':
            verdict = 'You approved' if answer.get('approved') else 'You said no to'
            return [{'role': 'event', 'text': f'{verdict}: {ask.prompt}'}]
        case 'handoff':
            return [{'role': 'event', 'text': f'You took over the browser: {ask.prompt}'}]
        case 'connect':
            name = ask.integration.get('name', 'it')
            verdict = f'You connected {name}' if answer.get('connected') else f'You chose not to connect {name}'
            return [{'role': 'event', 'text': verdict}]


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
        yield 'retry: 1000\n\n'  # reconnect after a second, well before the page would warn about a lost connection
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
    # Answered, and about to carry on: for the user it is working again, not waiting for them.
    status = 'running' if run.status == 'waiting' and ask is None else run.status
    return {
        'id': run.id,
        'thread_id': run.thread_id,
        'status': status,
        'prompt': run.prompt,  # what was asked, so an app can offer to try it again as it was
        # How long it has been working, or worked: "Working for 12s", "Worked for 1m 3s".
        'started_at': run.started_at.isoformat() if run.started_at else None,
        'completed_at': run.completed_at.isoformat() if run.completed_at else None,
        'output': run.output,
        'activity': await store.list_activity(connection, user.id, run.id),
        'ask': None if ask is None else ask_json(ask),
    }


@auth.signed_in
async def stop_run(request: Request, user: User) -> Response:
    """POST. Stop the user's run, whatever it is doing or waiting for."""
    resources = resources_of(request)
    async with resources.pool.connection() as connection:
        run = await store.get_run(connection, user.id, str(request.path_params['run_id']))
    if run is None:
        return NOT_FOUND
    if not await workflows.stop(resources, run):
        return JSONResponse({'detail': 'That task has finished already.'}, status_code=409)
    return JSONResponse({'ok': True})


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
        case 'connect':
            if body.connected is None:
                return JSONResponse({'detail': 'answer with connected: true or false'}, status_code=422)
            value = {'connected': body.connected}
    if not await approvals.answer(resources, user.id, ask.id, value):
        return JSONResponse({'detail': 'That was answered already.'}, status_code=409)
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
        # The error's own words are for us (they can name engines and paths), not for the user.
        logger.warning('Opening the live view of run %s failed: %s', run.id, type(error).__qualname__)
        return JSONResponse({'detail': "Monty's browser could not be opened. Please try again in a moment."}, 409)
    if handoff_id is None:
        return NOT_FOUND
    return JSONResponse({'url': f'/live/handoff/{handoff_id}', 'reason': ask.prompt})


@auth.signed_in
async def watch_screen(request: Request, user: User) -> Response:
    """The bot's browser as it works, for the user to watch: read only. Once the run has ended, the user's browser
    kept open for their next run (during a hand-off the user has the live view instead)."""
    resources = resources_of(request)
    async with resources.pool.connection() as connection:
        run = await store.get_run(connection, user.id, str(request.path_params['run_id']))
    if run is None:
        return NOT_FOUND
    try:
        screenshot = await resources.browser.peek_screenshot(run_id=run.id, user_id=user.id)
    except BrowserError:
        return Response(status_code=204)  # no picture now: no browser yet, busy with a call, or a hand-off began
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
            return JSONResponse({'detail': 'Notifications on this device belong to another account.'}, status_code=409)
    return JSONResponse({'ok': True}, status_code=201)


@auth.signed_in
async def remove_push_subscription(request: Request, user: User) -> Response:
    body = PushEndpoint.model_validate_json(await request.body())
    async with resources_of(request).pool.connection() as connection:
        removed = await remove_subscription(connection, user.id, body.endpoint)
    return JSONResponse({'ok': True}) if removed else NOT_FOUND


# --- saved sign-ins ---


def site_of(domain: str) -> str:
    return domain.lstrip('.').rstrip('.').lower()


def saved_sites(state: BrowserState) -> set[str]:
    sites = {site_of(cookie.domain) for cookie in state.cookies}
    sites.update(
        host for origin in (*state.local_storage, *state.session_storage) if (host := urlsplit(origin).hostname)
    )
    return sites


@auth.signed_in
async def read_sign_ins(request: Request, user: User) -> Response:
    """The sites the user's saved browser holds cookies for. Names only, never values."""
    state = await resources_of(request).jar.load(user_id=user.id)
    sites = sorted(saved_sites(state)) if state is not None else []
    return JSONResponse([{'site': site} for site in sites])


@auth.signed_in
async def forget_sign_in(request: Request, user: User) -> Response:
    """Drop the cookies and storage of one site. Refused while a task of the user's is using the browser."""
    resources = resources_of(request)
    site = site_of(str(request.path_params['site']))
    holder = f'forget:{uuid.uuid4()}'
    lease = PostgresLease(resources.pool, seconds=60)  # short: a crash here must not lock the user out for long
    if not await lease.acquire(user_id=user.id, run_id=holder):
        return JSONResponse({'detail': 'A task is using your browser. Try again when it has finished.'}, 409)
    try:
        state = await resources.jar.load(user_id=user.id)
        if state is None or site not in saved_sites(state):
            return NOT_FOUND

        def of_site(origin: str) -> bool:
            host = urlsplit(origin).hostname or ''
            return host == site or host.endswith('.' + site)

        state.cookies = [
            c for c in state.cookies if site_of(c.domain) != site and not site_of(c.domain).endswith('.' + site)
        ]
        state.local_storage = {o: items for o, items in state.local_storage.items() if not of_site(o)}
        state.session_storage = {o: items for o, items in state.session_storage.items() if not of_site(o)}
        if of_site(state.url):
            state.url = BLANK_URL
        # The browser kept open after the user's last run still has the site's cookies: it goes, so the next run starts
        # from what is saved now.
        await resources.browser.discard_parked(user.id)
        await resources.jar.save(user_id=user.id, state=state)
    finally:
        await lease.release(user_id=user.id, run_id=holder)
    return JSONResponse({'ok': True})


# --- schedules ---


@auth.signed_in
async def list_schedules(request: Request, user: User) -> Response:
    pool = resources_of(request).pool
    found = await schedules.list_for(pool, user.id)
    async with pool.connection() as connection:
        last = await store.last_scheduled_runs(connection, user.id)
    now = datetime.now(UTC)
    return JSONResponse([schedule_json(s, paused, last.get(s.thread_id), now) for s, paused in found])


@auth.signed_in
async def pause_schedule(request: Request, user: User) -> Response:
    return await set_paused(request, user, True)


@auth.signed_in
async def resume_schedule(request: Request, user: User) -> Response:
    return await set_paused(request, user, False)


async def set_paused(request: Request, user: User, paused: bool) -> Response:
    schedule_id = str(request.path_params['schedule_id'])
    pool = resources_of(request).pool
    schedule = await schedules.set_paused(pool, user.id, schedule_id, paused)
    if schedule is None:
        return NOT_FOUND
    async with pool.connection() as connection:
        last = await store.last_scheduled_runs(connection, user.id)
    return JSONResponse(schedule_json(schedule, paused, last.get(schedule.thread_id), datetime.now(UTC)))


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


# --- integrations: apps through Composio, and the user's own MCP servers (montybot.integrations) ---

HEADER_NAME = re.compile(r'^[A-Za-z0-9!#$%&\'*+.^_`|~-]{1,100}$')
"""An HTTP header name (RFC 9110 token)."""


def check_headers(headers: dict[str, str]) -> dict[str, str]:
    if len(headers) > 10:
        raise ValueError('at most 10 headers')
    for name, value in headers.items():
        if HEADER_NAME.fullmatch(name) is None or name.lower() in ('host', 'content-length', 'content-type'):
            raise ValueError('not a header you can set')
        if len(value) > 4_000 or any(ch in value for ch in '\r\n\0'):
            raise ValueError('not a header value')
    return headers


class NewServer(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    url: str = Field(min_length=8, max_length=2_000)
    headers: Annotated[dict[str, str], AfterValidator(check_headers)] = Field(default_factory=dict)
    """Such as `{"Authorization": "Bearer ..."}`. Left empty for a server with an OAuth sign-in."""


APPS_FAILED = 'Connecting apps is not working right now. Please try again in a little while.'


def apps_failed(error: IntegrationError) -> Response:
    logger.warning('Composio failed: %s', error)
    return JSONResponse({'detail': APPS_FAILED}, status_code=502)


@auth.signed_in
async def list_integrations(request: Request, user: User) -> Response:
    """The user's connections, apps and MCP servers, and whether this server can connect apps at all."""
    integrations = resources_of(request).integrations
    try:
        connections = await integrations.connections(user.id)
    except IntegrationError as error:
        return apps_failed(error)
    return JSONResponse(
        {'apps_available': integrations.composio is not None, 'connections': [c.json() for c in connections]},
        headers={'Cache-Control': 'no-store'},
    )


@auth.signed_in
async def list_apps(request: Request, user: User) -> Response:
    """What the Integrations page lists (`montybot.integrations.catalog.entries`): the integrations people use most,
    by kind, then every other app the user can connect with one click."""
    try:
        return JSONResponse(await resources_of(request).integrations.listing())
    except IntegrationError as error:
        return apps_failed(error)


@auth.signed_in
async def connect_app(request: Request, user: User) -> Response:
    """POST. Where the user signs in to the app (`url`), to open in a new window. Composio sends them back to
    `/integrations/composio/callback`, which wakes any chat waiting for the app."""
    try:
        url = await resources_of(request).integrations.connect_link(user.id, str(request.path_params['slug']))
    except IntegrationError as error:
        return apps_failed(error)
    return JSONResponse({'url': url})


@auth.signed_in
async def disconnect_app(request: Request, user: User) -> Response:
    try:
        removed = await resources_of(request).integrations.disconnect(user.id, str(request.path_params['account_id']))
    except IntegrationError as error:
        return apps_failed(error)
    return JSONResponse({'ok': True}) if removed else NOT_FOUND


@auth.signed_in
async def add_server(request: Request, user: User) -> Response:
    """POST. Adds the server once it answers as an MCP server. One with an OAuth sign-in comes back with `sign_in_url`
    for the user to open; it is ready once they have signed in."""
    body = NewServer.model_validate_json(await request.body())
    resources = resources_of(request)
    try:
        connection, sign_in_url = await resources.integrations.add_server(
            user.id, name=body.name, url=body.url, headers=body.headers
        )
    except mcp.NameTaken:
        return JSONResponse({'detail': f'You have a server called {body.name} already.'}, status_code=409)
    except (IntegrationError, ValueError) as error:
        return JSONResponse({'detail': str(error)}, status_code=400)
    if sign_in_url is None:
        await approvals.connected(resources, user.id, provider='mcp')
    return JSONResponse({'connection': connection.json(), 'sign_in_url': sign_in_url}, status_code=201)


@auth.signed_in
async def sign_in_server(request: Request, user: User) -> Response:
    """POST. Where the user signs in to their server (again)."""
    try:
        url = await resources_of(request).integrations.sign_in_link(user.id, str(request.path_params['server_id']))
    except IntegrationError as error:
        return JSONResponse({'detail': str(error)}, status_code=400)
    return JSONResponse({'url': url}) if url else NOT_FOUND


@auth.signed_in
async def remove_server(request: Request, user: User) -> Response:
    removed = await resources_of(request).integrations.remove_server(user.id, str(request.path_params['server_id']))
    return JSONResponse({'ok': True}) if removed else NOT_FOUND


# The pages a sign-in comes back to. Not tied to the web app's session: on a Mac the user signs in in their own
# browser. Only the state counts: signed by us (Composio), or one we stored and use once (MCP OAuth).


async def composio_callback(request: Request) -> Response:
    resources = resources_of(request)
    try:
        user_id, toolkit, connected = await resources.integrations.composio_returned(
            request.query_params.get('state', '')
        )
    except IntegrationError as error:
        return returned_page(False, str(error))
    try:
        name = next((a.name for a in await resources.integrations.catalog() if a.slug == toolkit), toolkit)
    except IntegrationError:
        name = toolkit  # only its name is missing; the connection is what counts
    if not connected:
        return returned_page(False, f'{name} was not connected. Go back to Monty and try again.')
    await approvals.connected(resources, user_id, provider='composio', key=toolkit)
    return returned_page(True, f'{name} is connected. You can close this window and go back to Monty.')


async def mcp_callback(request: Request) -> Response:
    resources = resources_of(request)
    query = request.query_params
    if 'error' in query or 'code' not in query:
        return returned_page(False, 'The sign-in was not finished. Go back to Monty and try again.')
    try:
        user_id, connection = await resources.integrations.server_signed_in(
            query.get('state', ''), query['code'], query.get('iss')
        )
    except IntegrationError as error:
        return returned_page(False, str(error))
    await approvals.connected(resources, user_id, provider='mcp')
    return returned_page(True, f'{connection.name} is connected. You can close this window and go back to Monty.')


def returned_page(ok: bool, text: str) -> Response:
    """Tells the user how the sign-in went. Opened from the web app, it tells the app (on a same-origin
    BroadcastChannel: the app cut the window's `opener`, so the sign-in pages could not reach the app) and closes."""
    title = 'Connected' if ok else 'Not connected'
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} | Monty</title><link rel="stylesheet" href="/static/app.css"></head>
<body class="returned"><main class="returned-card"><span class="monty-mark" aria-hidden="true">m<span>•</span></span>
<h1>{title}</h1><p>{html.escape(text)}</p></main>
<script>
if ('BroadcastChannel' in window) new BroadcastChannel('montybot-integrations').postMessage({{ ok: {'true' if ok else 'false'} }});
{'setTimeout(() => window.close(), 1500);' if ok else ''}
</script></body></html>"""
    headers = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'}
    return HTMLResponse(body, status_code=200 if ok else 400, headers=headers)


# --- the apps' own telemetry (`montybot/observability.py`) ---

OTLP_TYPES = ('application/json', 'application/x-protobuf')
MAX_TELEMETRY_BYTES = 5 * 1024 * 1024
TELEMETRY_PER_MINUTE = (120, 10 * 1024 * 1024)
"""Exports and bytes one user may send a minute: far above what the apps send, and a bound on the rest."""
_telemetry_sent: dict[str, tuple[float, int, int]] = {}  # user id: the minute's start, exports and bytes so far


def telemetry_allowed(user_id: str, size: int, now: float) -> bool:
    """Whether this export fits in the user's minute. Each server process counts its own."""
    for stale in [key for key, (start, _, _) in _telemetry_sent.items() if now - start >= 60]:
        del _telemetry_sent[stale]
    start, exports, sent = _telemetry_sent.get(user_id, (now, 0, 0))
    max_exports, max_bytes = TELEMETRY_PER_MINUTE
    if exports + 1 > max_exports or sent + size > max_bytes:
        return False
    _telemetry_sent[user_id] = (start, exports + 1, sent + size)
    return True


@auth.signed_in
async def telemetry_settings(request: Request, user: User) -> Response:
    """Whether the apps should send telemetry, and with content or not: the server's own settings."""
    settings = resources_of(request).settings
    return JSONResponse(
        {
            'enabled': settings.logfire_token is not None,
            'include_content': settings.logfire_include_content,
            'environment': settings.environment,
            'version': settings.commit or None,
        }
    )


async def forward_telemetry(request: Request) -> Response:
    """OTLP from a signed-in app (`/api/telemetry/v1/traces`, `metrics` or `logs`), sent on to Logfire with the
    server's token, so no token is in the apps. Only the body and its content type are forwarded, never cookies.

    Not `auth.signed_in`, which takes only JSON: OTLP protobuf needs a CORS preflight too, so it is as safe.
    Without `LOGFIRE_TOKEN` the answer is 403 and nothing is sent. Anyone may sign up, so each user's sending is
    bounded (`TELEMETRY_PER_MINUTE`); what they send is not checked, so their spans are only as true as their app.
    """
    user = await auth.signed_in_user(request)
    if user is None:
        return JSONResponse({'detail': 'sign in first'}, status_code=401)
    if request.headers.get('content-type', '').split(';')[0].strip() not in OTLP_TYPES:
        return JSONResponse({'detail': 'send OTLP'}, status_code=415)
    try:
        size = int(request.headers['content-length'])
    except (KeyError, ValueError):
        size = MAX_TELEMETRY_BYTES  # unknown: count it as the most it may be
    if not telemetry_allowed(user.id, size, time.monotonic()):
        return JSONResponse({'detail': 'too much telemetry'}, status_code=429, headers={'Retry-After': '60'})
    return await logfire.forward_export_request_starlette(request, max_body_size=MAX_TELEMETRY_BYTES)


# --- shapes ---


def user_json(user: User) -> dict[str, str]:
    return {'id': user.id, 'email': user.email, 'name': user.name}


def schedule_json(schedule: Schedule, paused: bool, last: Run | None, now: datetime) -> dict[str, Any]:
    """With when it runs next (none while paused) and how its latest run went."""
    return {
        'id': schedule.id,
        'name': schedule.name,
        'when': f'{schedule.when} ({schedule.timezone})',
        'paused': paused,
        'watch': schedule.watch,
        'thread_id': schedule.thread_id,
        'next_run_at': None if paused else schedules.next_run(schedule, now).isoformat(),
        'last_run_at': last.started_at.isoformat() if last and last.started_at else None,
        'last_status': last.status if last else None,
    }


def ask_json(ask: Ask) -> dict[str, Any]:
    """A hand-off's id stays on the server: the live view finds it from the signed-in user's open ask. A connect ask
    says what to connect (`integration`: `provider`, `key`, `name`, `logo`, and `server_id` to sign in to a server
    again)."""
    shown: dict[str, Any] = {'id': ask.id, 'kind': ask.kind, 'prompt': ask.prompt}
    if ask.kind == 'connect':
        shown['integration'] = ask.integration
    return shown


# --- workspace results (no filenames in request URLs or telemetry) ---


class FileDownload(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


@auth.signed_in
async def list_files(request: Request, user: User) -> Response:
    files, truncated = await resources_of(request).workspaces.files(user.id).list_results()
    return JSONResponse(
        {'files': files, 'truncated': truncated, 'max_download_bytes': MAX_DOWNLOAD_BYTES},
        headers={'Cache-Control': 'no-store'},
    )


@auth.signed_in
async def download_file(request: Request, user: User) -> Response:
    headers = {'Cache-Control': 'no-store'}
    # Bound JSON too; never echo invalid paths through validation responses.
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 8192:
            return JSONResponse({'detail': 'request too large'}, status_code=413, headers=headers)
        body.extend(chunk)
    try:
        path = FileDownload.model_validate_json(body).path
        data = await resources_of(request).workspaces.files(user.id).read_result(path)
    except FileTooLarge:
        return JSONResponse({'detail': 'file exceeds the 20 MiB download limit'}, status_code=413, headers=headers)
    except (OSError, ValueError):
        return JSONResponse({'detail': 'file unavailable'}, status_code=404, headers=headers)
    headers['Content-Disposition'] = "attachment; filename*=UTF-8''" + quote(download_name(path), safe='')
    headers['X-Content-Type-Options'] = 'nosniff'
    return Response(data, media_type='application/octet-stream', headers=headers)
