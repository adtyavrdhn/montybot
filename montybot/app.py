# Grown from viktor c1896df (viktor/app.py): the same Starlette app and lifespan, with the web app's routes.
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypedDict

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

from montybot import api, approvals, workflows
from montybot.live import live_app
from montybot.resources import Resources, open_resources
from montybot.settings import Settings


class State(TypedDict):
    resources: Resources


def create_app(settings: Settings) -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[State]:
        async with open_resources(settings) as resources:
            await approvals.redeliver_answers(resources)
            await workflows.start_queued(resources)
            yield {'resources': resources}

    return Starlette(
        routes=[
            Route('/healthz', healthz),
            Route('/api/signup', api.sign_up, methods=['POST']),
            Route('/api/signin', api.sign_in, methods=['POST']),
            Route('/api/signout', api.sign_out, methods=['POST']),
            Route('/api/me', api.me),
            Route('/api/threads', api.list_threads),
            Route('/api/threads', api.create_thread, methods=['POST']),
            Route('/api/threads/{thread_id:uuid}', api.read_thread),
            Route('/api/threads/{thread_id:uuid}/messages', api.add_message, methods=['POST']),
            Route('/api/runs/{run_id:uuid}', api.read_run),
            Route('/api/asks/{ask_id:uuid}', api.answer_ask, methods=['POST']),
            Route('/api/runs/{run_id:uuid}/live', api.live_link, methods=['POST']),
            Route('/api/runs/{run_id:uuid}/screen', api.watch_screen),
            Route('/api/push/key', api.push_key),
            Route('/api/push/subscriptions', api.add_push_subscription, methods=['POST']),
            Route('/api/push/subscriptions', api.remove_push_subscription, methods=['DELETE']),
            Route('/sw.js', service_worker),
            Mount('/live', app=LiveApp()),
            Mount('/static', app=StaticFiles(directory=STATIC), name='static'),
            Route('/', index),
            Route('/api/sign-ins', api.read_sign_ins),
            Route('/api/sign-ins/{site:str}', api.forget_sign_in, methods=['DELETE']),
            Route('/api/schedules', api.list_schedules),
            Route('/api/schedules/{schedule_id:uuid}/pause', api.pause_schedule, methods=['POST']),
            Route('/api/schedules/{schedule_id:uuid}/resume', api.resume_schedule, methods=['POST']),
            Route('/api/schedules/{schedule_id:uuid}', api.delete_schedule, methods=['DELETE']),
            Route('/api/memories', api.read_memories),
            Route('/api/memories/{memory_id:uuid}', api.remove_memory, methods=['DELETE']),
        ],
        middleware=[
            Middleware(
                SessionMiddleware,
                secret_key=settings.session_secret.get_secret_value(),
                session_cookie='montybot_session',
                same_site='lax',
                https_only=settings.secure_cookies,
                max_age=30 * 24 * 60 * 60,
            )
        ],
        lifespan=lifespan,
        exception_handlers={ValidationError: invalid_body},
    )


STATIC = Path(__file__).parent / 'static'


async def index(request: Request) -> Response:
    """The web app: one page, mobile first (`montybot/static`)."""
    return FileResponse(STATIC / 'index.html', headers={'Cache-Control': 'no-store'})


async def service_worker(request: Request) -> Response:
    """Served from the root so it may show notifications for the whole app."""
    return FileResponse(STATIC / 'sw.js', media_type='text/javascript', headers={'Cache-Control': 'no-store'})


class LiveApp:
    """The live view (`montybot.live`), made on first use from the app's resources, which exist once it starts."""

    def __init__(self) -> None:
        self._app: Any = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._app is None:
            self._app = live_app(scope['state']['resources'])
        await self._app(scope, receive, send)


async def invalid_body(request: Request, error: Exception) -> Response:
    assert isinstance(error, ValidationError)
    return JSONResponse(
        {'detail': error.errors(include_url=False, include_input=False, include_context=False)}, status_code=422
    )


async def healthz(request: Request) -> Response:
    resources: Resources = request.state.resources
    async with resources.pool.connection() as connection:
        await connection.execute('SELECT 1')
    return JSONResponse({'status': 'ok'})
