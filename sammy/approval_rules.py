"""Remembered approvals: the user's "Always allow this" rules, and what an action is to them.

A rule covers one action, never a tool as a whole: `commit` on a site (its host) for one control, or one tool of one
integration. The control is what the latest page calls it (`[12] button "Add to cart"`, numbers and prices left out,
so "Place order ($12.40)" and "Place order ($9.10)" are the same) or the CSS selector the agent named. The model's
own description never decides what a rule covers: only what is clicked or called does.

An action that spends money, sends something as the user or deletes data (`Risk`, by the words in its name) is
covered only by a rule the user made knowing that (`allow_risky`), and the automatic reviewer never approves one.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast
from urllib.parse import urlsplit

from sammy.db import Connection

Risk = Literal['money', 'send', 'delete']
RuleTool = Literal['commit', 'call_integration_tool']

RISKS: tuple[tuple[Risk, re.Pattern[str]], ...] = (
    (
        'money',
        re.compile(
            r'\b(?:orders?|buy|purchase|pay|payment|check ?out|book|booking|subscribe|donate|transfer|charge|tip)\b'
        ),
    ),
    (
        'send',
        re.compile(
            r'\b(?:send|post|reply|message|email|mail|tweet|publish|share|invite|comment|forward|submit|chat)\b'
        ),
    ),
    ('delete', re.compile(r'\b(?:delete|remove|cancel|unsubscribe|archive|trash|erase|destroy|purge|close)\b')),
)
RISK_WORDS: dict[Risk, str] = {'money': 'spends money', 'send': 'sends something as you', 'delete': 'deletes data'}


def words(text: str) -> str:
    """Lowercase words only: `GMAIL_SEND_EMAIL` and `sendEmail` are `gmail send email` and `send email`."""
    spaced = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', text)
    return ' '.join(re.findall(r'[a-z]+', spaced.lower()))


def risk_of(*texts: str) -> Risk | None:
    said = ' '.join(words(text) for text in texts)
    return next((risk for risk, pattern in RISKS if pattern.search(said)), None)


@dataclass(frozen=True, kw_only=True)
class Action:
    """What an approval would let happen, as a rule can cover it."""

    tool: RuleTool
    scope: str
    """The site's host (`walmart.com`) for `commit`; the integration's key (`linear`) for an integration's tool."""
    name: str
    """The control clicked (`add to cart`, or a CSS selector), or the integration's tool."""
    risk: Risk | None

    @property
    def summary(self) -> str:
        if self.tool == 'call_integration_tool':
            return f'Use {self.scope}: {self.name}'
        return f'Click "{self.name}" on {self.scope}'

    def json(self) -> dict[str, str | None]:
        return {'tool': self.tool, 'scope': self.scope, 'name': self.name, 'risk': self.risk, 'summary': self.summary}

    @classmethod
    def from_json(cls, value: object) -> Action | None:
        """The action an ask offered to remember (its `details['rule']`), if it offered one."""
        if not isinstance(value, dict):
            return None
        shown = cast(dict[str, object], value)
        tool, scope, name, risk = shown.get('tool'), shown.get('scope'), shown.get('name'), shown.get('risk')
        if tool not in ('commit', 'call_integration_tool') or not isinstance(scope, str) or not isinstance(name, str):
            return None
        if risk not in ('money', 'send', 'delete', None):
            return None
        return cls(tool=tool, scope=scope, name=name, risk=risk)


def site_of(url: str) -> str:
    host = (urlsplit(url).hostname or '').lower()
    return host.removeprefix('www.')


REF_LINE = r'^\s*-?\s*\[{ref}\]\s+\S+\s+("(?:[^"\\]|\\.)*")'


def commit_action(target: str, description: str, url: str, page: str) -> Action | None:
    """What `commit` would click on the page the browser shows: None if the page has no such ref or no site."""
    scope = site_of(url)
    target = target.strip().removeprefix('[').removesuffix(']')
    if not scope or not target:
        return None
    if target.isdigit():
        line = re.search(REF_LINE.format(ref=target), page, re.MULTILINE)
        label = json.loads(line.group(1)) if line else ''
        name = words(label) if isinstance(label, str) else ''
    else:
        name = target  # a CSS selector: what the agent clicks, whatever the page calls it
    if not name:
        return None
    return Action(tool='commit', scope=scope, name=name, risk=risk_of(name, description))


def integration_action(args: Mapping[str, object]) -> Action | None:
    integration, tool = args.get('integration'), args.get('tool')
    if not isinstance(integration, str) or not isinstance(tool, str) or not integration or not tool:
        return None
    return Action(tool='call_integration_tool', scope=integration, name=tool, risk=risk_of(tool))


@dataclass(frozen=True, kw_only=True)
class Rule:
    id: str
    action: Action
    allow_risky: bool

    def json(self) -> dict[str, str | bool | None]:
        return {'id': self.id, **self.action.json(), 'allow_risky': self.allow_risky}


RULE_COLUMNS = 'id, tool, scope, name, risk, allow_risky'


def rule_from(row: Mapping[str, object]) -> Rule:
    action = Action(
        tool=cast(RuleTool, row['tool']),
        scope=str(row['scope']),
        name=str(row['name']),
        risk=cast(Risk | None, row['risk']),
    )
    return Rule(id=str(row['id']), action=action, allow_risky=bool(row['allow_risky']))


async def add_rule(connection: Connection, user_id: str, action: Action, *, allow_risky: bool) -> Rule:
    """Idempotent: the same action again keeps one rule, with what the user said last."""
    cursor = await connection.execute(
        'INSERT INTO sammy.approval_rules (user_id, tool, scope, name, risk, allow_risky) VALUES (%s, %s, %s, %s, %s, %s) '
        'ON CONFLICT (user_id, tool, scope, name) DO UPDATE SET risk = EXCLUDED.risk, allow_risky = EXCLUDED.allow_risky '
        f'RETURNING {RULE_COLUMNS}',
        (user_id, action.tool, action.scope, action.name, action.risk, allow_risky),
    )
    row = await cursor.fetchone()
    assert row is not None
    return rule_from(row)


async def list_rules(connection: Connection, user_id: str) -> list[Rule]:
    cursor = await connection.execute(
        f'SELECT {RULE_COLUMNS} FROM sammy.approval_rules WHERE user_id = %s ORDER BY created_at', (user_id,)
    )
    return [rule_from(row) for row in await cursor.fetchall()]


async def delete_rule(connection: Connection, user_id: str, rule_id: str) -> bool:
    cursor = await connection.execute(
        'DELETE FROM sammy.approval_rules WHERE id = %s AND user_id = %s', (rule_id, user_id)
    )
    return cursor.rowcount == 1


async def matching_rule(connection: Connection, user_id: str, action: Action) -> Rule | None:
    """The user's rule for exactly this action. A risky action needs a rule that allows the risk."""
    cursor = await connection.execute(
        f'SELECT {RULE_COLUMNS} FROM sammy.approval_rules '
        'WHERE user_id = %s AND tool = %s AND scope = %s AND name = %s AND (allow_risky OR %s)',
        (user_id, action.tool, action.scope, action.name, action.risk is None),
    )
    row = await cursor.fetchone()
    return None if row is None else rule_from(row)


async def reviewer_enabled(connection: Connection, user_id: str) -> bool:
    cursor = await connection.execute('SELECT 1 FROM sammy.approval_reviewers WHERE user_id = %s', (user_id,))
    return await cursor.fetchone() is not None


async def set_reviewer(connection: Connection, user_id: str, enabled: bool) -> None:
    if enabled:
        await connection.execute(
            'INSERT INTO sammy.approval_reviewers (user_id) VALUES (%s) ON CONFLICT DO NOTHING', (user_id,)
        )
    else:
        await connection.execute('DELETE FROM sammy.approval_reviewers WHERE user_id = %s', (user_id,))
