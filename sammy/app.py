# Grown from viktor c1896df (viktor/app.py): the same Starlette app and lifespan, with the web app's routes.
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TypedDict

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from sammy import api, approvals, model_preferences_api, tunnel_api, workflows
from sammy.live import live_app
from sammy.observability import ClientTraceContext
from sammy.resources import Resources, open_resources
from sammy.settings import Settings


class State(TypedDict):
    resources: Resources


def create_app(settings: Settings) -> ASGIApp:
    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[State]:
        async with open_resources(settings) as resources:
            await approvals.redeliver_answers(resources)
            await workflows.start_queued(resources)
            yield {'resources': resources}

    app = Starlette(
        routes=[
            Route('/healthz', healthz),
            Route('/api/signup', api.sign_up, methods=['POST']),
            Route('/api/signin', api.sign_in, methods=['POST']),
            Route('/api/signout', api.sign_out, methods=['POST']),
            Route('/api/password/reset', api.request_password_reset, methods=['POST']),
            Route('/api/password/reset/confirm', api.confirm_password_reset, methods=['POST']),
            Route('/api/me', api.me),
            Route('/api/model-preferences', model_preferences_api.preferences, methods=['GET', 'PUT']),
            Route('/api/attachments', api.upload_attachment, methods=['POST']),
            Route('/api/attachments/{attachment_id:uuid}', api.read_attachment),
            Route('/api/threads', api.list_threads),
            Route('/api/search', api.search_threads),
            Route('/api/threads', api.create_thread, methods=['POST']),
            Route('/api/threads/{thread_id:uuid}', api.read_thread),
            Route('/api/threads/{thread_id:uuid}', api.rename_thread, methods=['PATCH']),
            Route('/api/threads/{thread_id:uuid}', api.delete_thread, methods=['DELETE']),
            Route('/api/threads/{thread_id:uuid}/messages', api.add_message, methods=['POST']),
            Route('/api/runs/{run_id:uuid}', api.read_run),
            Route('/api/runs/{run_id:uuid}/events', api.run_events),
            Route('/api/runs/{run_id:uuid}/stop', api.stop_run, methods=['POST']),
            Route('/api/asks/{ask_id:uuid}', api.answer_ask, methods=['POST']),
            Route('/api/runs/{run_id:uuid}/live', api.live_link, methods=['POST']),
            Route('/api/runs/{run_id:uuid}/screen', api.watch_screen),
            WebSocketRoute('/api/tunnel', tunnel_api.mac_tunnel),
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
            Route('/api/integrations', api.list_integrations),
            Route('/api/integrations/apps', api.list_apps),
            Route('/api/integrations/apps/{slug:str}/connect', api.connect_app, methods=['POST']),
            Route('/api/integrations/apps/accounts/{account_id:str}', api.disconnect_app, methods=['DELETE']),
            Route('/api/integrations/servers', api.add_server, methods=['POST']),
            Route('/api/integrations/servers/{server_id:uuid}/sign-in', api.sign_in_server, methods=['POST']),
            Route('/api/integrations/servers/{server_id:uuid}', api.remove_server, methods=['DELETE']),
            Route('/integrations/composio/callback', api.composio_callback),
            Route('/integrations/mcp/callback', api.mcp_callback),
            Route('/api/memories', api.read_memories),
            Route('/api/memories/{memory_id:uuid}', api.remove_memory, methods=['DELETE']),
            Route('/api/telemetry', api.telemetry_settings),
            Route('/api/telemetry/{path:path}', api.forward_telemetry, methods=['POST']),
        ],
        middleware=[
            Middleware(ClientTraceContext),
            Middleware(
                SessionMiddleware,
                secret_key=settings.session_secret.get_secret_value(),
                session_cookie='sammy_session',
                same_site='lax',
                https_only=settings.secure_cookies,
                max_age=30 * 24 * 60 * 60,
            ),
        ],
        lifespan=lifespan,
        exception_handlers={ValidationError: invalid_body},
    )
    return app


STATIC = Path(__file__).parent / 'static'


async def index(request: Request) -> Response:
    """The web app: one page, mobile first (`sammy/static`)."""
    # Only this app may frame it, so another site cannot trick a click on Approve or Delete.
    headers = {
        'Cache-Control': 'no-store',
        'Content-Security-Policy': "frame-ancestors 'self'",
        'X-Frame-Options': 'SAMEORIGIN',
    }
    return FileResponse(STATIC / 'index.html', headers=headers)


async def service_worker(request: Request) -> Response:
    """Served from the root so it may show notifications for the whole app."""
    return FileResponse(STATIC / 'sw.js', media_type='text/javascript', headers={'Cache-Control': 'no-store'})


class LiveApp:
    """The live view (`sammy.live`), made on first use from the app's resources, which exist once it starts."""

    def __init__(self) -> None:
        self._app: ASGIApp | None = None

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
