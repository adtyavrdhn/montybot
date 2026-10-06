"""`run_code` on local Monty: what the model sees when code fails, times out or floods output, and that the session's
variables survive what they should. The browser functions are not called here; the end-to-end tests cover them."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from montybot.code import OUTPUT_LIMIT, SESSION_LOST, looks_irreversible, open_monty, run_snippet
from montybot.settings import Settings

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
async def resources() -> AsyncIterator[Any]:
    settings = Settings(
        database_url='postgresql://unused',
        session_secret=SecretStr('x'),
        encryption_key=SecretStr('x'),
        code_compute_seconds=1,
        code_timeout_seconds=30,
    )
    async with open_monty(settings) as monty:
        yield SimpleNamespace(settings=settings, monty=monty, browser=None, pool=None, lease=None)


async def run(resources: Any, state: bytes | None, code: str) -> tuple[str, bytes | None]:
    return await run_snippet(resources, 'run-1', 'user-1', state, code)


async def test_variables_last_and_errors_keep_what_ran_before(resources: Any) -> None:
    out, state = await run(resources, None, 'shop = "http://shop.test"\nprint("set")')
    assert out == 'set'
    out, state = await run(resources, state, 'count = 2\n1 / 0')
    assert out == 'Error: ZeroDivisionError: division by zero'
    out, state = await run(resources, state, 'shop, count')
    assert out == "('http://shop.test', 2)"


async def test_a_syntax_error_changes_nothing(resources: Any) -> None:
    _, state = await run(resources, None, 'x = 1')
    out, after = await run(resources, state, 'x = (')
    assert out.startswith('Error: ') and after == state


async def test_code_that_runs_too_long_is_stopped_and_the_session_goes_on(resources: Any) -> None:
    _, state = await run(resources, None, 'kept = "yes"')
    out, after = await run(resources, state, 'while True:\n    pass')
    assert 'TimeoutError' in out and after == state
    out, _ = await run(resources, after, 'kept')
    assert out == 'yes'


async def test_a_lost_session_starts_afresh_and_says_so(resources: Any) -> None:
    out, state = await run(resources, b'not a session', 'print(1 + 1)')
    assert out == f'{SESSION_LOST}\n2' and state is not None


async def test_output_is_capped(resources: Any) -> None:
    out, _ = await run(resources, None, 'for _ in range(100):\n    print("x" * 10_000)')
    assert len(out) < OUTPUT_LIMIT + 300 and 'more characters of output cut' in out


@pytest.mark.parametrize(
    ('target', 'page', 'refused'),
    [
        ('#place-order', '', True),
        ('#checkout-button', '', True),
        ('#add-eggs', '', False),
        ('3', '[3] button "Place order ($4.30)"', True),
        ('3', '- [3] link "Pay now"', True),
        ('2', '[2] button "Add milk"\n[3] button "Place order"', False),
    ],
)
def test_irreversible_clicks_are_spotted(target: str, page: str, refused: bool) -> None:
    assert (looks_irreversible(target, page) is not None) == refused
