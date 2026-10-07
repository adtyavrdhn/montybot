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
import logging
import re
import secrets
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any, TypeVar
from urllib.parse import quote, urlsplit

from pydantic import AfterValidator, BaseModel, Field, StringConstraints
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from montybot import approvals, auth, schedules, store, streaming, workflows
from montybot.browser.contract import (
    BrowserError,
)
from montybot.browser.state import BLANK_URL, BrowserState
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


async def remember_timezone(connection: Any, user: User, timezone: str | None) -> None:
    """Keep the time zone the user's browser reports, if it is a real one and has changed."""
    if timezone is None or timezone == user.timezone or not schedules.is_timezone(timezone):
        return
    await store.set_timezone(connection, user.id, timezone)


class ThreadChange(BaseModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


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
        return JSONResponse({'detail': 'That email already has an account. Sign in instead.'}, status_code=409)
    auth.sign_in(request, user)
    return JSONResponse(user_json(user), status_code=201)


async def sign_in(request: Request) -> Response:
    if (refused := auth.refuse_non_json(request)) is not None:
        return refused
    body = Credentials.model_validate_json(await request.body())
    async with resources_of(request).pool.connection() as connection:
        found = await store.find_login(connection, body.email.strip().lower())
    if found is None or not auth.check_password(body.password, found[1]):
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
            await store.start_password_reset(connection, found[0].id, auth.hash_password(code), RESET_MINUTES)
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
        if code_hash is None or not auth.check_password(body.code.strip(), code_hash):
            return wrong
        await store.finish_password_reset(connection, found[0].id, auth.hash_password(body.password))
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
        await remember_timezone(connection, user, body.timezone)
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
        await remember_timezone(connection, user, body.timezone)
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
async def list_threads(request: Request, user: User) -> Response:
    """Each thread with the status of its unfinished run, if it has one: `running`, `waiting` (for the user) or
    `queued`; and otherwise how its latest run ended (`outcome`: `done`, `failed` or `stopped`)."""
    async with resources_of(request).pool.connection() as connection:
        threads = await store.list_threads(connection, user.id)
        active = await store.active_runs(connection, user.id)
        outcomes = await store.latest_outcomes(connection, user.id)
    return JSONResponse(
        [{'id': t.id, 'title': t.title, 'status': active.get(t.id), 'outcome': outcomes.get(t.id)} for t in threads]
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
    messages = chat_messages(runs, asks)
    return JSONResponse({'id': thread.id, 'title': thread.title, 'messages': messages, 'run': run_json})


def chat_messages(runs: list[Run], asks: list[Ask]) -> list[dict[str, str]]:
    """The chat as the user sees it. Each run is their message, what Monty asked them and how they answered, and
    Monty's reply once the run has finished. `event` lines record approvals and hand-offs."""
    asks_of_run: dict[str, list[Ask]] = {}
    for ask in asks:
        asks_of_run.setdefault(ask.run_id, []).append(ask)
    shown: list[dict[str, str]] = []
    for run in runs:
        shown.append({'role': 'user', 'text': run.prompt})
        for ask in asks_of_run.get(run.id, []):
            shown.extend(ask_messages(ask))
        if run.output:
            shown.append({'role': 'assistant', 'text': run.output})
    return shown


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
        'when': f'{schedule.when} ({schedule.timezone})',
        'paused': paused,
        'watch': schedule.watch,
        'thread_id': schedule.thread_id,
    }


def ask_json(ask: Ask) -> dict[str, Any]:
    """A hand-off's id stays on the server: the live view finds it from the signed-in user's open ask."""
    return {'id': ask.id, 'kind': ask.kind, 'prompt': ask.prompt}


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
