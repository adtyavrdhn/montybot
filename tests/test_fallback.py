"""A fallback chain (`MODEL=chain:NAME`): when the first model fails with a provider error, the next one answers, the
span says which, a warning says a fallback happened, and a DBOS replay keeps the recorded answer."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dbos import DBOS, SetWorkflowID
from logfire.testing import CaptureLogfire
from pydantic_ai.exceptions import ModelHTTPError, UserError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.instrumented import InstrumentationSettings

from sammy import agent as agent_module
from sammy import cli, streaming
from sammy.chains import chain, fall_back
from sammy.deps import RunDeps
from sammy.resources import load_model
from sammy.vendor.claude_code import ClaudeCodeSignInExpiredError

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
def deps(monkeypatch: pytest.MonkeyPatch) -> Iterator[RunDeps]:
    # Recall and the connected integrations are instructions read from the database, which this test does not need.
    async def nothing(ctx: object) -> str:
        return ''

    monkeypatch.setattr(agent_module, 'recall', nothing)
    monkeypatch.setattr(agent_module, 'connected_integrations', nothing)
    run_id = str(uuid4())
    streaming.reset(run_id)
    fake = SimpleNamespace(
        run_id=run_id, run=SimpleNamespace(id=run_id, prompt='hello'), schedule=None, local_time='', squirrel_name=''
    )
    yield fake  # pyright: ignore[reportReturnType]  # only what the instructions and the stream handler read
    streaming.discard(run_id)


def scripted(name: str, answer: str | None, calls: list[str]) -> FunctionModel:
    """A model that answers `answer`, or is rate limited when it is None."""

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        calls.append(name)
        if answer is None:
            raise ModelHTTPError(429, name, 'rate limited')
        yield answer

    return FunctionModel(stream_function=stream, model_name=name)


def answered_by(messages: list[ModelMessage]) -> list[str]:
    return [message.model_name or '' for message in messages if isinstance(message, ModelResponse)]


async def test_a_failing_model_falls_back_and_replay_keeps_the_answer(
    database_url: str, deps: RunDeps, capfire: CaptureLogfire
) -> None:
    calls: list[str] = []
    agent = agent_module.build_agent(
        chain([scripted('primary', None, calls), scripted('backup', 'Answered by the backup.', calls)])
    )
    agent.instrument = InstrumentationSettings(version=5)

    @DBOS.workflow(name='test.fallback.run')
    async def run() -> tuple[str, list[str]]:
        result = await agent.run('hello', deps=deps)
        return result.output, answered_by(result.new_messages())

    DBOS(config={'name': 'fallback-test', 'system_database_url': database_url, 'enable_otlp': False})
    try:
        DBOS.launch()
        with SetWorkflowID('fallback-fixture'):
            first = await run()
        assert first == ('Answered by the backup.', ['backup'])
        assert calls == ['primary', 'backup']

        # Run the workflow again on its recorded steps: the model step returns the backup's answer, calling no model.
        replay = await (await DBOS.fork_workflow_async('fallback-fixture', 1000)).get_result()
        assert tuple(replay) == first
        assert calls == ['primary', 'backup']
    finally:
        DBOS.destroy(workflow_completion_timeout_sec=0)

    spans = capfire.exporter.exported_spans_as_dict()
    chat = [span['attributes'] for span in spans if span['name'].startswith('chat ')]
    # The model span of the run and of its replay both say the backup answered.
    assert [attributes['gen_ai.response.model'] for attributes in chat] == ['backup', 'backup']
    assert chat[0]['gen_ai.request.model'] == 'backup'
    warnings = [span['attributes'] for span in spans if span['attributes'].get('logfire.level_num') == 13]
    assert [(w['model'], w['error_type']) for w in warnings] == [('primary', 'ModelHTTPError')]


async def test_other_errors_do_not_fall_back(deps: RunDeps) -> None:
    """A bug of ours, or a refusal, is not the provider being down: the run fails rather than trying the next model."""
    calls: list[str] = []

    async def broken(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        calls.append('primary')
        raise UserError('misconfigured')
        yield ''

    agent = agent_module.build_agent(
        chain([FunctionModel(stream_function=broken), scripted('backup', 'unused', calls)])
    )
    with pytest.raises(UserError, match='misconfigured'):
        await agent.run('hello', deps=deps)
    assert calls == ['primary']


def test_an_expired_claude_code_sign_in_falls_back(capfire: CaptureLogfire) -> None:
    assert fall_back(ClaudeCodeSignInExpiredError('sign in again'))
    assert not fall_back(UserError('misconfigured'))
    [warning] = capfire.exporter.exported_spans_as_dict()
    assert (warning['attributes']['model'], warning['attributes']['error_type']) == (
        'claude-code',
        'ClaudeCodeSignInExpiredError',
    )


@pytest.mark.parametrize(
    ('model', 'chains', 'exit_code'),
    [
        ('claude-code:claude-opus-5-5', '{}', 0),
        ('chain:main', '{"main": ["anthropic:claude-opus-5-5", "claude-code:claude-opus-5-5"]}', 0),
        ('anthropic:claude-opus-5-5', '{"other": ["claude-code:claude-opus-5-5"]}', 1),
        ('chain:main', '{"main": ["anthropic:claude-opus-5-5"], "other": ["claude-code:claude-opus-5-5"]}', 1),
    ],
)
def test_the_deploy_checks_the_claude_code_sign_in_of_a_chain(
    model: str, chains: str, exit_code: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`deploy/deploy.sh` asks for the server's Claude Code sign-in when `sammy uses-claude-code` exits 0: for a
    Claude Code model, or a chain with one, and not for a chain this `MODEL` does not run."""
    monkeypatch.chdir(tmp_path)  # no .env
    for name, value in {
        'DATABASE_URL': 'postgresql://unused',
        'SESSION_SECRET': 'secret',
        'ENCRYPTION_KEY': 'key',
        'MODEL': model,
        'MODEL_CHAINS': chains,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, 'argv', ['sammy', 'uses-claude-code'])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == exit_code


def test_a_chain_is_named_in_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('OPENAI_API_KEY', 'unused')  # building the model needs a key; nothing is sent
    model = load_model('chain:main', {'main': ['openai:gpt-5', 'test']})
    assert isinstance(model, FallbackModel)
    assert [member.model_name for member in model.models] == ['gpt-5', 'test']
    assert load_model('chain:solo', {'solo': ['openai:gpt-5']}) == 'openai:gpt-5'
    with pytest.raises(UserError, match="No chain named 'other'"):
        load_model('chain:other', {'main': ['openai:gpt-5']})
    with pytest.raises(ValueError, match='cannot contain another chain'):
        load_model('chain:main', {'main': ['chain:main']})
