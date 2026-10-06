"""`run_code` on local Monty: what the model sees when code fails, times out or floods output, and that the session's
variables survive what they should. The browser functions are not called here; the end-to-end tests cover them.

The user's files (#21) are checked on local Monty and, with `MONTYBOT_TEST_MONTY_URL`, on Full Monty, whose OS calls
come back over the WebSocket.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from montybot.code import OUTPUT_LIMIT, SESSION_LOST, looks_irreversible, open_monty, run_snippet
from montybot.settings import Settings
from montybot.workspaces import Workspaces

pytestmark = pytest.mark.anyio
USER = str(uuid.uuid4())
FULL_MONTY = os.environ.get('MONTYBOT_TEST_MONTY_URL')


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


def settings_on(monty_url: str | None, workspaces_dir: Path) -> Settings:
    return Settings(
        database_url='postgresql://unused',
        session_secret=SecretStr('x'),
        encryption_key=SecretStr('x'),
        code_compute_seconds=1,
        code_timeout_seconds=30,
        monty_url=monty_url,
        workspaces_dir=workspaces_dir,
    )


@pytest.fixture
async def resources(tmp_path: Path) -> AsyncIterator[Any]:
    settings = settings_on(None, tmp_path)
    async with open_monty(settings) as monty:
        yield SimpleNamespace(settings=settings, monty=monty, workspaces=Workspaces(tmp_path), browser=None, pool=None)


@pytest.fixture(params=['local', 'full'])
async def any_monty(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Any]:
    """Local Monty, and Full Monty when the test run names one."""
    if request.param == 'full' and not FULL_MONTY:
        pytest.skip('Full Monty: set MONTYBOT_TEST_MONTY_URL')
    settings = settings_on(FULL_MONTY if request.param == 'full' else None, tmp_path)
    async with open_monty(settings) as monty:
        yield SimpleNamespace(settings=settings, monty=monty, workspaces=Workspaces(tmp_path), browser=None, pool=None)


async def run(resources: Any, state: bytes | None, code: str, user_id: str = USER) -> tuple[str, bytes | None]:
    return await run_snippet(resources, 'run-1', user_id, state, code)


FILES = """
from pathlib import Path
Path('notes').mkdir()
Path('/work/notes/todo.txt').write_text('eggs\\n')
with open('notes/todo.txt', 'a') as f:
    f.write('milk\\n')
print(Path('/work/notes/todo.txt').read_text().split(), [p.name for p in Path('/work/notes').iterdir()])
print(Path('notes/todo.txt').stat().st_size, Path('/work/notes').is_dir(), Path('/work/nothing').exists())
"""


async def test_code_uses_the_users_files(any_monty: Any) -> None:
    out, _ = await run(any_monty, None, FILES)
    assert out == "['eggs', 'milk'] ['todo.txt']\n10 True False"
    directory = any_monty.workspaces.directory(USER)
    assert (directory / 'notes' / 'todo.txt').read_text() == 'eggs\nmilk\n'  # on our server, in the user's folder

    # Errors name the path code used, never the folder on our server.
    out, _ = await run(any_monty, None, "from pathlib import Path\nPath('/work/missing.csv').read_text()")
    assert out == "Error: FileNotFoundError: [Errno 2] No such file or directory: '/work/missing.csv'"
    assert str(directory) not in out


async def test_code_sees_only_its_users_files(any_monty: Any) -> None:
    other = str(uuid.uuid4())
    await run(any_monty, None, "from pathlib import Path\nPath('/work/secret.txt').write_text('A only')", other)

    out, _ = await run(any_monty, None, "from pathlib import Path\nPath('/work/secret.txt').exists()")
    assert out == 'False'
    for path in [
        f'/work/../{other}/secret.txt',
        str(any_monty.workspaces.directory(other) / 'secret.txt'),
        '/etc/hosts',
    ]:
        out, _ = await run(any_monty, None, f'from pathlib import Path\nPath({path!r}).read_text()')
        assert out.startswith('Error: PermissionError:'), out


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


async def test_one_snippet_uses_returned_pages_without_extra_reads(
    resources: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A known read/search/compute sequence needs one snippet, not a tool roundtrip per action.

    Fake only the browser session: execute real Monty and the real host-function guards.
    """
    from montybot import code as code_module
    from montybot.browser.contract import Navigate, Press, Ref, Type

    calls: list[str] = []

    class SearchSession:
        def __init__(self, *args: Any) -> None:
            self.resources = resources
            self.url = 'https://93.184.215.14/'
            self.searched = False

        async def activity(self, text: str) -> None:
            pass

        async def act(self, action: Any) -> None:
            if isinstance(action, Navigate):
                calls.append('goto')
            elif isinstance(action, Type):
                assert isinstance(action.target, Ref)
                assert action.target.ref == '1' and action.text == 'milk'
                calls.append('type')
            elif isinstance(action, Press):
                assert action.key == 'Enter'
                calls.append('enter')
                self.searched = True
            else:
                pytest.fail('irreversible action reached the browser')

        async def read(self) -> str:
            calls.append('snapshot')
            return '[2] button "Buy now"\nMilk: $3' if self.searched else '[1] searchbox "Search"'

    monkeypatch.setattr(code_module, 'Session', SearchSession)
    out, state = await run(
        resources,
        None,
        """page = await goto("https://93.184.215.14/")
if '[1] searchbox "Search"' in page:
    result = await type_text("1", "milk", press_enter=True)
    price = int(result.split("$")[1])
    print("https://93.184.215.14/", price * 2)
""",
    )
    assert out == 'https://93.184.215.14/ 6'
    assert state is not None
    assert calls == ['goto', 'snapshot', 'type', 'enter', 'snapshot']

    # Approval guard still runs inside a batched snippet, before any click reaches the browser.
    calls.clear()
    out, _ = await run(resources, state, 'await type_text("1", "milk", press_enter=True)\nawait click("2")')
    assert 'Use the `commit` tool' in out
    assert calls == ['snapshot', 'type', 'enter', 'snapshot']


@pytest.mark.parametrize(
    'snippet',
    [
        'await click("#primary")',
        'await click("3")',
        'await press_key("Enter")',
        'await press_key("Control+Enter")',
        'await press_key("Space")',
        'await type_text("1", "value", press_enter=True)',
    ],
)
async def test_ordinary_input_cannot_submit_checkout(
    resources: Any, snippet: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from montybot import code as code_module

    class Checkout:
        def __init__(self, *args: Any) -> None:
            self.resources = resources
            self.url = 'https://93.184.215.14/'

        async def read(self) -> str:
            return '[1] textbox "Quantity"\n[3] button "Place order"'

        async def act(self, action: Any) -> None:
            pytest.fail('Unapproved input reached the checkout')

    monkeypatch.setattr(code_module, 'Session', Checkout)
    out, _ = await run(resources, None, snippet)
    assert 'commit' in out
