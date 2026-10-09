# Grown from viktor c1896df (viktor/resources.py): one pool, one browser service and one agent per process.
"""What the app holds for its lifetime, and how DBOS workflows reach it.

DBOS workflows are plain module-level functions that recover by id after a restart, so they cannot take these as
arguments. `open_resources` sets them once per process, before `DBOS.launch()` recovers anything, and `current()`
hands them out.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from dbos import DBOS, DBOSConfig
from pydantic_ai import Agent
from pydantic_ai.models import Model

from sammy import store
from sammy.browser.contract import BrowserBackend, TabsBackend
from sammy.browser.host import BrowserHost, Detour
from sammy.browser.tunnel import NOWHERE, Place, Tunnels
from sammy.crypto import deployment_key
from sammy.db import Pool, create_pool, migrate
from sammy.imports import import_object
from sammy.integrations import Integrations
from sammy.settings import Settings
from sammy.signins import PostgresJar, PostgresLease
from sammy.workspaces import Workspaces

if TYPE_CHECKING:
    from sammy.code import MontyRunner
    from sammy.voice import Speech


@dataclass(frozen=True, kw_only=True)
class Resources:
    settings: Settings
    pool: Pool
    browser: BrowserHost
    jar: PostgresJar
    lease: PostgresLease
    agent: Agent[Any, str]
    monty: MontyRunner
    workspaces: Workspaces
    tunnels: Tunnels | None = None
    """The users' Mac tunnels; None when `mac_tunnel` is off or the engine has no egress proxy to swap."""
    integrations: Integrations
    speech: Speech | None = None
    """The speech provider (`sammy.voice`); None without `VOICE_PROVIDER`."""
    # Retained for the experimental Jev helpers; production never constructs or calls this model.
    jev_model: Model | None = None


_current: Resources | None = None


def current() -> Resources:
    assert _current is not None, 'the app sets its resources before DBOS runs any workflow'
    return _current


CLAUDE_CODE_PREFIX = 'claude-code:'


def load_model(name: str) -> Model | str:
    """A model name for Pydantic AI, `claude-code:NAME` for a Claude Code subscription model, or
    `script:module:attribute` for a `Model` object (or a function making one)."""
    if name.startswith(CLAUDE_CODE_PREFIX):
        from sammy.vendor.claude_code import ClaudeCodeModel

        return ClaudeCodeModel(name.removeprefix(CLAUDE_CODE_PREFIX))
    if not name.startswith('script:'):
        return name
    obj = import_object(name.removeprefix('script:'))
    return cast(Model, obj) if isinstance(obj, Model) else cast(Callable[[], Model], obj)()


def backend_factory(name: str) -> Callable[[], BrowserBackend]:
    return cast(Callable[[], BrowserBackend], import_object(name))


def routed_backend_factory(
    name: str, place_of: Callable[[Path], Place] = lambda _: NOWHERE
) -> Callable[[Path], BrowserBackend] | None:
    """The engine made with another egress proxy socket, for the Mac tunnel; None for an engine without a proxy. An
    engine that can also takes the Mac's time zone and language (`place_of` the socket), so they match its address."""
    factory = cast(Callable[..., BrowserBackend], import_object(name))
    parameters = inspect.signature(factory).parameters
    if 'egress_socket' not in parameters:
        return None
    if 'timezone' not in parameters:
        return lambda socket: factory(egress_socket=socket)

    def routed(socket: Path) -> BrowserBackend:
        place = place_of(socket)
        return factory(egress_socket=socket, timezone=place.timezone, locale=place.locale)

    return routed


def mac_route(pool: Pool, tunnels: Tunnels) -> Callable[[str, str], Awaitable[Path | None]]:
    """A browser goes out through its user's Mac while the Mac is connected, for a run the user started. A scheduled
    run always goes out through the server: it runs whether the Mac is awake or not."""

    async def route(run_id: str, user_id: str) -> Path | None:
        if not tunnels.connected(user_id):
            return None
        async with pool.connection() as connection:
            run = await store.get_run(connection, user_id, run_id)
        if run is None or run.trigger == 'schedule':
            return None
        return await tunnels.egress(user_id)

    return route


@asynccontextmanager
async def open_resources(settings: Settings) -> AsyncGenerator[Resources]:
    global _current
    from sammy.agent import build_agent  # the agent imports the tools, which import this module
    from sammy.code import open_monty
    from sammy.voice import open_speech  # the voice endpoints import this module

    await migrate(settings.database_url)
    pool = create_pool(settings.database_url)
    await pool.open()
    key = deployment_key(settings.encryption_key.get_secret_value())
    jar = PostgresJar(pool, key)
    integrations = Integrations(pool, key, settings)
    # Runs of one user share the lease, and one browser in tabs, only on this server.
    lease = PostgresLease(pool, owner=settings.executor_id)
    new_backend = backend_factory(settings.browser_backend)
    tunnels = Tunnels(settings.tunnel_dir) if settings.mac_tunnel else None
    routed = routed_backend_factory(settings.browser_backend, tunnels.place_of) if tunnels is not None else None
    if routed is None:
        tunnels = None  # an engine without a proxy cannot go out through the Mac
    browser = BrowserHost(
        new_backend=new_backend,
        jar=jar,
        lease=lease,
        idle_timeout=settings.browser_idle_timeout_seconds,
        max_open_browsers=settings.browser_max_open,
        keep_open=settings.browser_keep_open,
        share_browser=isinstance(new_backend(), TabsBackend),  # a closed backend: making one starts nothing
        detour=Detour(route=mac_route(pool, tunnels), new_backend=routed) if routed and tunnels else None,
    )
    DBOS(
        config=DBOSConfig(
            name='sammy',
            system_database_url=settings.database_url,
            executor_id=settings.executor_id,
            application_version='1',
            enable_otlp=False,
        )
    )
    model = load_model(settings.model)
    agent = build_agent(model)
    speech = open_speech(settings)
    async with browser, open_monty(settings) as monty:
        _current = Resources(
            settings=settings,
            pool=pool,
            browser=browser,
            jar=jar,
            lease=lease,
            agent=agent,
            monty=monty,
            workspaces=Workspaces(settings.workspaces_dir),
            tunnels=tunnels,
            integrations=integrations,
            speech=speech,
        )
        try:
            DBOS.launch()
            yield _current
        finally:
            DBOS.destroy(workflow_completion_timeout_sec=0)
            _current = None
            if tunnels is not None:
                await tunnels.aclose()
            await integrations.aclose()
            if speech is not None:
                await speech.aclose()
            await pool.close()
