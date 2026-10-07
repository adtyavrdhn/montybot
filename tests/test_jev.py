"""Finite Jev advice, uncertainty fallback and content boundaries, without external keys."""

from __future__ import annotations

import asyncio
import runpy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from montybot.jev import Intent, candidates, decide, likelihood, make_model, suggest_navigation
from montybot.settings import Settings


def settings(**kwargs: Any) -> Settings:
    return Settings(
        database_url='postgresql://unused', session_secret=SecretStr('x'), encryption_key=SecretStr('x'), **kwargs
    )


def model(choice: str, probability: float) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tool = info.output_tools[0]
        field = next(iter(tool.parameters_json_schema['properties']))
        return ModelResponse(
            parts=[ToolCallPart(tool.name, {field: choice})],
            provider_details={'probabilities': {field: {choice: probability}}, 'confidence': {field: 1}},
        )

    return FunctionModel(respond)


@pytest.mark.parametrize(
    'choice, probability, expected', [('read', 0.95, 'read'), ('act', 0.3, None), ('navigate', 0.85, 'navigate')]
)
def test_intent_probability_not_sureness(choice: str, probability: float, expected: str | None) -> None:
    assert asyncio.run(decide(model(choice, probability), Intent, 'Latest request', settings())) == expected


def test_errors_and_missing_metadata_abstain() -> None:
    def fail(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError('private-error-content')

    assert asyncio.run(decide(FunctionModel(fail), Intent, 'private-request', settings())) is None
    assert likelihood(None, 'direction', 'read') == 0
    assert likelihood({'probabilities': {'direction': {'read': float('nan')}}}, 'direction', 'read') == 0
    assert make_model(settings()) is None
    assert make_model(settings(typesafe_api_key=SecretStr('  '))) is None


def test_on_whenever_a_key_is_set() -> None:
    assert make_model(settings(typesafe_api_key=SecretStr('key'))) is not None


def test_only_unique_link_labels_are_candidates() -> None:
    assert candidates(
        'private page body\n[1] textbox "Password" value="private"\n[2] button "Buy"\n[3] link "Orders"\n[4] link "Account"\n[5] link "Account"\n[6] link "Other" disabled'
    ) == {'3': 'Orders'}


def test_navigation_is_durable_advice_not_a_click(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, Any] = {}
    calls: list[str] = []

    async def step(options: dict[str, Any], function: Any) -> Any:
        name = options['name']
        if name not in recorded:
            recorded[name] = await function()
        return recorded[name]

    async def snapshot(**kwargs: Any) -> Any:
        calls.append('snapshot')
        return SimpleNamespace(snapshot=SimpleNamespace(text='[17] link "Orders"\n[18] textbox "password"'))

    async def run() -> None:
        resources = SimpleNamespace(
            jev_model=model('17', 0.95), settings=settings(), browser=SimpleNamespace(snapshot=snapshot)
        )
        ctx: Any = SimpleNamespace(deps=SimpleNamespace(resources=resources, run_id='run', user_id='user'))
        monkeypatch.setattr('montybot.jev.DBOS.run_step_async', step)
        first = await suggest_navigation(ctx, 'Show orders')
        assert first == await suggest_navigation(ctx, 'Show orders')
        assert '[17]' in first and 'Not clicked' in first
        assert calls == ['snapshot']
        assert list(recorded) == ['jev.navigation']

    asyncio.run(run())


def test_secret_updates_keep_unset_and_unrelated_values(tmp_path: Path) -> None:
    module = runpy.run_path(str(Path(__file__).parents[1] / 'deploy/update-secrets.py'))
    path = tmp_path / '.env'
    path.write_text('MODEL=existing\nLOGFIRE_TOKEN=old\nTYPESAFE_API_KEY=old\n')
    module['update'](path, {'TYPESAFE_API_KEY': 'new-key'})
    assert path.read_text() == "MODEL=existing\nLOGFIRE_TOKEN=old\nTYPESAFE_API_KEY='new-key'\n"
    assert path.stat().st_mode & 0o777 == 0o600
    before = path.read_text()
    for bad in [{'OTHER': 'x'}, {'LOGFIRE_TOKEN': '$(command)'}, {'LOGFIRE_TOKEN': 'x\nMODEL=bad'}]:
        with pytest.raises(ValueError, match='Invalid secret update'):
            module['update'](path, bad)
    assert path.read_text() == before


def test_typesafe_request_schema_and_field_probability(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic_ai.models.decision import ChoiceAnswer, DecisionRequest, DecisionResponse
    from pydantic_ai.models.typesafe import TypeSafeModel
    from pydantic_ai.providers.typesafe import TypeSafeProvider

    backend = TypeSafeModel('jev-latest', provider=TypeSafeProvider(api_key='fixture-key'))

    async def respond(request: DecisionRequest, model_settings: Any) -> DecisionResponse:
        assert len(request.questions) == 1
        name = next(iter(request.questions))
        return DecisionResponse(
            model_name='jev-latest',
            answers={
                name: ChoiceAnswer(
                    choice='files',
                    confidence=0.1,
                    probabilities={'files': 0.95, 'read': 0.05},
                )
            },
        )

    monkeypatch.setattr(backend, 'decide', respond)
    assert asyncio.run(decide(backend, Intent, 'Please total the invoices', settings())) == 'files'


@pytest.mark.anyio
async def test_dbos_replay_does_not_repeat_navigation(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from dbos import DBOS, SetWorkflowID

    calls: list[str] = []

    async def snapshot(**kwargs: Any) -> Any:
        calls.append('snapshot')
        return SimpleNamespace(snapshot=SimpleNamespace(text='[7] link "Orders"'))

    resources = SimpleNamespace(
        jev_model=model('7', 0.95), settings=settings(), browser=SimpleNamespace(snapshot=snapshot)
    )
    ctx: Any = SimpleNamespace(deps=SimpleNamespace(resources=resources, run_id='run', user_id='user'))

    @DBOS.workflow(name='test.jev.navigation')
    async def workflow() -> str:
        return await suggest_navigation(ctx, 'Read orders')

    DBOS(
        config={
            'name': 'jev-test',
            'system_database_url': database_url,
            'enable_otlp': False,
            'log_level': 'ERROR',
        }
    )
    try:
        DBOS.launch()
        with SetWorkflowID('jev-replay-fixture'):
            first = await workflow()
        with SetWorkflowID('jev-replay-fixture'):
            second = await workflow()
        assert first == second and '[7]' in first
        assert calls == ['snapshot']
    finally:
        DBOS.destroy(workflow_completion_timeout_sec=0)
