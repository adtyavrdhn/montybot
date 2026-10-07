# Grown from viktor c1896df (viktor/resources.py): one pool, one browser service and one agent per process.
"""What the app holds for its lifetime, and how DBOS workflows reach it.

DBOS workflows are plain module-level functions that recover by id after a restart, so they cannot take these as
arguments. `open_resources` sets them once per process, before `DBOS.launch()` recovers anything, and `current()`
hands them out.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from dbos import DBOS, DBOSConfig
from pydantic_ai import Agent
from pydantic_ai.models import Model

from montybot.browser.contract import BrowserBackend
from montybot.browser.host import BrowserHost
from montybot.crypto import deployment_key
from montybot.db import Pool, create_pool, migrate
from montybot.imports import import_object
from montybot.settings import Settings
from montybot.signins import PostgresJar, PostgresLease
from montybot.workspaces import Workspaces

if TYPE_CHECKING:
    from montybot.code import MontyRunner


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
        from montybot.vendor.claude_code import ClaudeCodeModel

        return ClaudeCodeModel(name.removeprefix(CLAUDE_CODE_PREFIX))
    if not name.startswith('script:'):
        return name
    obj = import_object(name.removeprefix('script:'))
    return cast(Model, obj) if isinstance(obj, Model) else cast(Callable[[], Model], obj)()


def backend_factory(name: str) -> Callable[[], BrowserBackend]:
    return cast(Callable[[], BrowserBackend], import_object(name))


@asynccontextmanager
async def open_resources(settings: Settings) -> AsyncGenerator[Resources]:
    global _current
    from montybot.agent import build_agent  # the agent imports the tools, which import this module
    from montybot.code import open_monty

    await migrate(settings.database_url)
    pool = create_pool(settings.database_url)
    await pool.open()
    jar = PostgresJar(pool, deployment_key(settings.encryption_key.get_secret_value()))
    lease = PostgresLease(pool)
    browser = BrowserHost(
        new_backend=backend_factory(settings.browser_backend),
        jar=jar,
        lease=lease,
        idle_timeout=settings.browser_idle_timeout_seconds,
    )
    DBOS(
        config=DBOSConfig(
            name='montybot',
            system_database_url=settings.database_url,
            executor_id=settings.executor_id,
            application_version='1',
            enable_otlp=False,
        )
    )
    model = load_model(settings.model)
    from montybot.jev import make_model

    jev_model = make_model(settings)
    agent = build_agent(model, jev=jev_model is not None)
    async with browser, open_monty(settings) as monty:
        _current = Resources(
            settings=settings,
            pool=pool,
            browser=browser,
            jar=jar,
            lease=lease,
            agent=agent,
            jev_model=jev_model,
            monty=monty,
            workspaces=Workspaces(settings.workspaces_dir),
        )
        try:
            DBOS.launch()
            yield _current
        finally:
            DBOS.destroy(workflow_completion_timeout_sec=0)
            _current = None
            await pool.close()
