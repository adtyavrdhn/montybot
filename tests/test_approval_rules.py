"""Remembered approvals against Postgres: a rule covers exactly the action it was made for, on its own site or app,
and an approval decided once (by a rule, the reviewer or the user) stays decided when the run replays."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from sammy import approval_rules, store
from sammy.approval_rules import Action, commit_action, integration_action
from sammy.approvals import ask_id, decide
from sammy.browser.service import UnknownRun
from sammy.db import Pool, create_pool, migrate
from sammy.models import Run
from sammy.resources import Resources
from sammy.settings import Settings

pytestmark = pytest.mark.anyio

PAGE = '[11] link "Groceries"\n  - [12] button "Add to cart ($3.20)"\n[13] button "Place order ($12.40)"'
ADD_TO_CART = Action(tool='commit', scope='walmart.com', name='add to cart', risk=None)
CREATE_ISSUE = {'integration': 'linear', 'tool': 'LINEAR_CREATE_LINEAR_ISSUE', 'arguments': {'title': 'Groceries'}}


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
async def pool(database_url: str) -> AsyncIterator[Pool]:
    await migrate(database_url)
    pool = create_pool(database_url)
    await pool.open()
    yield pool
    await pool.close()


async def new_run(pool: Pool, prompt: str = 'Put eggs in my walmart.com cart') -> Run:
    async with pool.connection() as c:
        user = await store.create_user(c, f'{uuid.uuid4().hex[:8]}@example.test', 'x')
        assert user is not None
        thread = await store.create_thread(c, user.id, 'Groceries')
        return await store.create_run(
            c, run_id=str(uuid.uuid4()), user_id=user.id, thread_id=thread.id, prompt=prompt, trigger='message'
        )


def reviewer(verdict: str, confidence: float) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {'verdict': verdict, 'confidence': confidence})]
        )

    return FunctionModel(respond)


def resources(pool: Pool, url: str = 'https://www.walmart.com/cart', model: FunctionModel | None = None) -> Resources:
    async def snapshot(**kwargs: str) -> SimpleNamespace:
        if not url:
            raise UnknownRun('no browser yet')
        return SimpleNamespace(snapshot=SimpleNamespace(url=url, text=PAGE))

    settings = Settings(
        database_url='postgresql://unused', session_secret=SecretStr('x'), encryption_key=SecretStr('x')
    )
    fake = SimpleNamespace(
        pool=pool, settings=settings, browser=SimpleNamespace(snapshot=snapshot), reviewer_model=model
    )
    return cast(Resources, fake)


def test_what_commit_clicks_is_the_control_on_the_page_not_the_description() -> None:
    added = commit_action('12', 'Add eggs to the cart', 'https://www.walmart.com/cart', PAGE)
    assert added == ADD_TO_CART  # the price is not part of it: next week's total differs
    placed = commit_action('[13]', 'Add eggs to the cart', 'https://walmart.com/cart', PAGE)
    assert placed == Action(tool='commit', scope='walmart.com', name='place order', risk='money')
    assert commit_action('#place-order', 'Order', 'https://shop.test/', PAGE) == Action(
        tool='commit', scope='shop.test', name='#place-order', risk='money'
    )
    assert commit_action('99', 'Add to cart', 'https://walmart.com/', PAGE) is None  # not on the page
    assert integration_action(CREATE_ISSUE) == Action(
        tool='call_integration_tool', scope='linear', name='LINEAR_CREATE_LINEAR_ISSUE', risk=None
    )
    assert integration_action({'integration': 'gmail', 'tool': 'GMAIL_SEND_EMAIL'}).risk == 'send'  # pyright: ignore[reportOptionalMemberAccess]
    assert integration_action({'integration': 'mcp:notes', 'tool': 'deleteNote'}).risk == 'delete'  # pyright: ignore[reportOptionalMemberAccess]


async def test_a_rule_matches_its_action(pool: Pool) -> None:
    run = await new_run(pool)
    async with pool.connection() as c:
        rule = await approval_rules.add_rule(c, run.user_id, ADD_TO_CART, allow_risky=False)
        assert await approval_rules.matching_rule(c, run.user_id, ADD_TO_CART) == rule
        assert [r.json() for r in await approval_rules.list_rules(c, run.user_id)] == [
            {'id': rule.id, **ADD_TO_CART.json(), 'allow_risky': False}
        ]
    decision = await decide(resources(pool), run, 1, 'commit', {'target': '12', 'description': 'Add eggs'}, 'Add eggs')
    assert decision.by == 'rule'
    async with pool.connection() as c:
        recorded = await store.get_ask(c, run.user_id, ask_id(run.id, 1))
    assert recorded is not None and recorded.answer == {'approved': True, 'auto': 'rule'}  # shown in the chat


async def test_a_rule_scoped_to_another_site_or_control_does_not_match(pool: Pool) -> None:
    run = await new_run(pool)
    async with pool.connection() as c:
        await approval_rules.add_rule(c, run.user_id, ADD_TO_CART, allow_risky=False)
        for scope in ('walmart.com.evil.test', 'evil.test', 'shop.walmart.com'):
            elsewhere = Action(tool='commit', scope=scope, name='add to cart', risk=None)
            assert await approval_rules.matching_rule(c, run.user_id, elsewhere) is None
        other = Action(tool='commit', scope='walmart.com', name='place order', risk='money')
        assert await approval_rules.matching_rule(c, run.user_id, other) is None
    decision = await decide(
        resources(pool, url='https://evil.test/cart'), run, 1, 'commit', {'target': '12', 'description': 'x'}, 'x'
    )
    assert decision.by is None
    assert decision.details['rule'] == {
        **ADD_TO_CART.json(),
        'scope': 'evil.test',
        'summary': 'Click "add to cart" on evil.test',
    }


async def test_a_risky_action_needs_a_rule_that_allows_the_risk(pool: Pool) -> None:
    run = await new_run(pool)
    pay = Action(tool='commit', scope='walmart.com', name='place order', risk='money')
    async with pool.connection() as c:
        await approval_rules.add_rule(c, run.user_id, pay, allow_risky=False)
        assert await approval_rules.matching_rule(c, run.user_id, pay) is None
        await approval_rules.add_rule(c, run.user_id, pay, allow_risky=True)  # the user said so for this rule
        assert await approval_rules.matching_rule(c, run.user_id, pay) is not None
        assert len(await approval_rules.list_rules(c, run.user_id)) == 1


async def test_a_decision_stays_made_when_the_run_replays(pool: Pool) -> None:
    run = await new_run(pool)
    args = {'target': '12', 'description': 'Add eggs'}
    first = await decide(resources(pool), run, 1, 'commit', args, 'Add eggs')
    assert first.by is None  # no rule yet: the user is asked
    async with pool.connection() as c:
        await approval_rules.add_rule(c, run.user_id, ADD_TO_CART, allow_risky=False)
    replayed = await decide(resources(pool), run, 1, 'commit', args, 'Add eggs')
    assert replayed == first  # the rule made meanwhile does not change an ask the run made already
    assert (await decide(resources(pool), run, 2, 'commit', args, 'Add eggs')).by == 'rule'


async def test_no_page_or_a_schedule_cannot_be_remembered(pool: Pool) -> None:
    run = await new_run(pool)
    no_browser = await decide(resources(pool, url=''), run, 1, 'commit', {'target': '12'}, 'x')
    assert no_browser.by is None and 'rule' not in no_browser.details
    schedule = await decide(resources(pool), run, 2, 'schedule_task', {'name': 'Weekly'}, 'x')
    assert schedule.by is None and 'rule' not in schedule.details


async def test_the_reviewer_approves_only_for_users_who_turned_it_on(pool: Pool) -> None:
    run = await new_run(pool, 'Create a Linear issue for groceries')
    model = reviewer('approve', 0.97)
    assert (await decide(resources(pool, model=model), run, 1, 'call_integration_tool', CREATE_ISSUE, 'x')).by is None
    async with pool.connection() as c:
        await approval_rules.set_reviewer(c, run.user_id, True)
    decided = await decide(resources(pool, model=model), run, 2, 'call_integration_tool', CREATE_ISSUE, 'x')
    assert decided.by == 'reviewer'
    unsure = await decide(
        resources(pool, model=reviewer('approve', 0.5)), run, 3, 'call_integration_tool', CREATE_ISSUE, 'x'
    )
    assert unsure.by is None
    send = {'integration': 'gmail', 'tool': 'GMAIL_SEND_EMAIL', 'arguments': {}}
    assert (await decide(resources(pool, model=model), run, 4, 'call_integration_tool', send, 'x')).by is None
