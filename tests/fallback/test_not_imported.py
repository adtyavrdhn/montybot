"""The fallback is built on the side: nothing under `montybot/` outside `montybot/fallback/` may import it (#22)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).parents[2] / 'montybot'
FALLBACK = PACKAGE / 'fallback'


def imported_modules(source: str, path: Path) -> set[str]:
    """Every module `source`, living at `path`, imports. Relative imports are resolved, and `from a import b` counts
    as importing both `a` and `a.b`."""
    package = path.relative_to(PACKAGE.parent).with_suffix('').parts[:-1]
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = list(package[: len(package) - node.level + 1]) if node.level else []
            module = '.'.join([*base, *([node.module] if node.module else [])])
            found.add(module)
            found.update(f'{module}.{alias.name}' for alias in node.names)
    return found


def is_fallback(module: str) -> bool:
    return module == 'montybot.fallback' or module.startswith('montybot.fallback.')


def test_nothing_outside_the_fallback_imports_it() -> None:
    offenders: list[str] = []
    for path in sorted(PACKAGE.rglob('*.py')):
        if FALLBACK in path.parents:
            continue
        source = path.read_text()
        # The text check also catches `importlib.import_module('montybot.fallback...')`.
        if any(map(is_fallback, imported_modules(source, path))) or 'montybot.fallback' in source:
            offenders.append(str(path.relative_to(PACKAGE.parent)))
    assert offenders == []


@pytest.mark.parametrize(
    'source',
    [
        'import montybot.fallback.e2b_desktop',
        'from montybot.fallback import e2b_desktop',
        'from montybot import fallback',
        'from ..fallback import e2b_desktop',
        'from .. import fallback',
    ],
)
def test_the_check_sees_every_kind_of_import(source: str) -> None:
    assert any(map(is_fallback, imported_modules(source, PACKAGE / 'agent' / 'tools.py')))


def test_the_check_ignores_lookalikes() -> None:
    assert not any(map(is_fallback, imported_modules('import montybot.fallbacks', PACKAGE / 'agent.py')))
    assert not any(map(is_fallback, imported_modules('from . import fallback', PACKAGE / 'browser' / 'x.py')))
