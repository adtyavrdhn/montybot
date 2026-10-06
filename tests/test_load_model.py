"""`MODEL=claude-code:NAME` builds the vendored Claude Code model, signed in from a token file, with no network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic_ai.exceptions import UserError

from montybot.resources import load_model
from montybot.settings import Settings
from montybot.vendor.claude_code import ClaudeCodeModel


@pytest.fixture
def token_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / 'auth.json'
    monkeypatch.setenv('CLAUDE_CODE_CREDENTIALS', 'file')
    monkeypatch.setenv('CLAUDE_CODE_AUTH_FILE', str(path))
    return path


def test_claude_code_model_from_token_file(token_file: Path) -> None:
    token_file.write_text(json.dumps({'access_token': 'access', 'refresh_token': 'refresh', 'expires_at': None}))
    model = load_model('claude-code:claude-opus-5-5')
    assert isinstance(model, ClaudeCodeModel)
    assert model.model_name == 'claude-opus-5-5'
    assert model.system == 'claude-code'


def test_claude_code_model_without_sign_in(token_file: Path) -> None:
    with pytest.raises(UserError, match='No Claude Code credentials'):
        load_model('claude-code:claude-opus-5-5')


def test_other_names_pass_through() -> None:
    assert load_model('openai:gpt-5') == 'openai:gpt-5'


def test_default_model_is_claude_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('MODEL', raising=False)
    settings = Settings(
        database_url='postgresql://unused',
        session_secret='secret',  # pyright: ignore[reportArgumentType]
        encryption_key='key',  # pyright: ignore[reportArgumentType]
        _env_file=None,  # pyright: ignore[reportCallIssue]
    )
    assert settings.model == 'claude-code:claude-opus-5-5'
