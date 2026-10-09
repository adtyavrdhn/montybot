"""The automatic reviewer only ever fails towards asking the user, and never sees a secret."""

from __future__ import annotations

import asyncio

from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from sammy.approval_rules import Action
from sammy.reviewer import approves, redact
from sammy.settings import Settings

ISSUE = Action(tool='call_integration_tool', scope='linear', name='LINEAR_CREATE_LINEAR_ISSUE', risk=None)


def settings(timeout: float = 30) -> Settings:
    """A long timeout, so a slow test machine does not count as a reviewer that timed out."""
    return Settings(
        database_url='postgresql://unused',
        session_secret=SecretStr('x'),
        encryption_key=SecretStr('x'),
        approval_reviewer_timeout_seconds=timeout,
    )


def answering(verdict: str, confidence: float, seen: list[str] | None = None, delay: float = 0) -> FunctionModel:
    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if seen is not None:
            seen.append(str(messages[-1].parts[-1].content))  # pyright: ignore[reportAttributeAccessIssue]
        await asyncio.sleep(delay)
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {'verdict': verdict, 'confidence': confidence})]
        )

    return FunctionModel(respond)


def review(model: FunctionModel, action: Action = ISSUE, timeout: float = 30) -> bool:
    return asyncio.run(approves(model, settings(timeout), task='Create an issue', action=action, arguments={}))


def test_a_sure_approval_approves() -> None:
    assert review(answering('approve', 0.95))


def test_unsure_or_ask_asks_the_user() -> None:
    assert not review(answering('approve', 0.6))
    assert not review(answering('ask', 0.99))


def test_a_reviewer_past_its_timeout_asks_the_user() -> None:
    assert not review(answering('approve', 0.99, delay=30), timeout=0.5)


def test_a_failing_reviewer_asks_the_user() -> None:
    def fail(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError('the provider is down')

    assert not review(FunctionModel(fail))


def test_a_risky_action_is_never_reviewed() -> None:
    seen: list[str] = []
    send = Action(tool='call_integration_tool', scope='gmail', name='GMAIL_SEND_EMAIL', risk='send')
    assert not review(answering('approve', 1.0, seen), send)
    assert seen == []


def test_secrets_are_redacted_from_the_reviewers_input() -> None:
    seen: list[str] = []
    model = answering('approve', 0.95, seen)
    arguments: dict[str, object] = {
        'integration': 'linear',
        'arguments': {
            'title': 'Groceries',
            'password': 'hunter2-secret',
            'apiKey': 'plain-looking-key',
            'headers': {'Cookie': 'session=cookie-value', 'Authorization': 'Bearer bearer-value'},
            'note': 'my token is tok-12345 and ghp_abcdefghijklmnopqrstuvwxyz0123456789',
            'lines': ['card cvv: 999', 'shipping to home'],
        },
    }
    task = 'Log in with password: swordfish and file the groceries issue'
    assert asyncio.run(approves(model, settings(), task=task, action=ISSUE, arguments=arguments))
    [prompt] = seen
    for secret in (
        'hunter2-secret',
        'plain-looking-key',
        'cookie-value',
        'bearer-value',
        'tok-12345',
        'ghp_abcdefghijklmnopqrstuvwxyz0123456789',
        '999',
        'swordfish',
    ):
        assert secret not in prompt
    for kept in ('Groceries', 'shipping to home', 'LINEAR_CREATE_LINEAR_ISSUE', 'file the groceries issue'):
        assert kept in prompt


def test_redact_keeps_what_is_not_secret() -> None:
    assert redact({'author': 'Ann', 'count': 3, 'done': True, 'shipping': 'home', 'token': 5}) == {
        'author': 'Ann',
        'count': 3,
        'done': True,
        'shipping': 'home',
        'token': '[redacted]',
    }
