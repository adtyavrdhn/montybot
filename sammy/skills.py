"""Skills: a user's playbooks for tasks Sammy has been shown or told how to do (#128).

```
instructions  skill_index    the saved skills' names and when to use each, looked up once per run (a step)
tool          load_skill     one whole skill by name: inputs, steps, how to check, what to return, approvals, failures
tool          save_skill     the agent offers to save what it just did as a skill; the user approves it first
live view     Teach Sammy    a lesson the user gives in the browser becomes a draft (`sammy.teach`), saved on review
API           /api/skills    the web and Mac apps list, edit, save and delete them (`sammy.skill_api`)
```

The index stays short, so the instructions stay small and cached; a skill's steps reach the model only when it loads
one. Drafts are never in the index. Database calls the agent makes are DBOS steps, so a recovered run does not save a
skill twice.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass

from dbos import DBOS
from psycopg.errors import UniqueViolation
from pydantic_ai import FunctionToolset, RunContext, ToolFailed
from pydantic_ai.tools import ToolDefinition

from sammy.db import Connection
from sammy.deps import RunDeps
from sammy.resources import current

INDEX_LIMIT = 50
"""The most skills the index lists; the rest still load by name."""

COLUMNS = 'id, name, when_to_use, inputs, steps, verify, returns, approvals, failures, draft'

INSTRUCTIONS = """\
Skills are playbooks the user saved or taught you, for tasks on sites they use. When a request matches one listed,
call `load_skill` with its name before you start, and follow it: it knows where things are on the site. After a task
of several steps on a site that no skill covered, you may offer to save how you did it with `save_skill` (the user
approves it first). If the user wants to show you how they do something, open the site and `hand_off` saying they can
press "Teach Sammy" and do it once; their lesson becomes a draft skill they review."""


class NameTaken(Exception):
    """The user has a saved skill of that name already."""


@dataclass(frozen=True, kw_only=True)
class SkillText:
    """What a skill says. Only `name`, `when_to_use` and `steps` must be filled in."""

    name: str
    when_to_use: str
    steps: str
    inputs: str = ''
    verify: str = ''
    returns: str = ''
    approvals: str = ''
    failures: str = ''

    def stripped(self) -> SkillText:
        return SkillText(**{key: value.strip() for key, value in asdict(self).items()})

    def as_text(self) -> str:
        """The whole skill, as `load_skill` gives it to the model."""
        parts = [f'# Skill: {self.name}', f'When to use it: {self.when_to_use}']
        for label, value in (
            ('What it needs', self.inputs),
            ('Steps', self.steps),
            ('How to check it worked', self.verify),
            ('What to give back', self.returns),
            ('Ask the user first before', self.approvals),
            ('If something goes wrong', self.failures),
        ):
            if value:
                parts.append(f'{label}:\n{value}')
        return '\n\n'.join(parts)


@dataclass(frozen=True, kw_only=True)
class Skill:
    id: str
    text: SkillText
    draft: bool

    def json(self) -> dict[str, str | bool]:
        return {'id': self.id, **asdict(self.text), 'draft': self.draft}


def skill_from(row: dict[str, object]) -> Skill:
    fields = ('name', 'when_to_use', 'steps', 'inputs', 'verify', 'returns', 'approvals', 'failures')
    return Skill(
        id=str(row['id']), text=SkillText(**{key: str(row[key]) for key in fields}), draft=row['draft'] is True
    )


# --- the table ---


async def add_skill(
    connection: Connection, user_id: str, text: SkillText, *, draft: bool = False, skill_id: str | None = None
) -> Skill:
    """Raises `NameTaken` for a saved skill whose name the user has already. Adding the same `skill_id` again
    changes nothing, so a retried step adds it once."""
    text = text.stripped()
    skill_id = skill_id or str(uuid.uuid4())
    try:
        async with connection.transaction():
            await connection.execute(
                'INSERT INTO sammy.skills (id, user_id, name, when_to_use, inputs, steps, verify, returns, approvals, '
                'failures, draft) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING',
                (
                    skill_id,
                    user_id,
                    text.name,
                    text.when_to_use,
                    text.inputs,
                    text.steps,
                    text.verify,
                    text.returns,
                    text.approvals,
                    text.failures,
                    draft,
                ),
            )
    except UniqueViolation as error:
        raise NameTaken(text.name) from error
    cursor = await connection.execute(
        f'SELECT {COLUMNS} FROM sammy.skills WHERE user_id = %s AND id = %s', (user_id, skill_id)
    )
    row = await cursor.fetchone()
    assert row is not None
    return skill_from(row)


async def update_skill(
    connection: Connection, user_id: str, skill_id: str, text: SkillText, *, draft: bool
) -> Skill | None:
    """None if the user has no such skill. Raises `NameTaken` as `add_skill` does."""
    text = text.stripped()
    try:
        async with connection.transaction():
            cursor = await connection.execute(
                'UPDATE sammy.skills SET name = %s, when_to_use = %s, inputs = %s, steps = %s, verify = %s, '
                'returns = %s, approvals = %s, failures = %s, draft = %s, updated_at = now() '
                f'WHERE user_id = %s AND id = %s RETURNING {COLUMNS}',
                (
                    text.name,
                    text.when_to_use,
                    text.inputs,
                    text.steps,
                    text.verify,
                    text.returns,
                    text.approvals,
                    text.failures,
                    draft,
                    user_id,
                    skill_id,
                ),
            )
            row = await cursor.fetchone()
    except UniqueViolation as error:
        raise NameTaken(text.name) from error
    return None if row is None else skill_from(row)


async def list_skills(connection: Connection, user_id: str) -> list[Skill]:
    """Drafts first, as they wait for the user's review; then by name."""
    cursor = await connection.execute(
        f'SELECT {COLUMNS} FROM sammy.skills WHERE user_id = %s ORDER BY draft DESC, lower(name), created_at',
        (user_id,),
    )
    return [skill_from(row) for row in await cursor.fetchall()]


async def find_skill(connection: Connection, user_id: str, name: str) -> Skill | None:
    """The saved skill of that name, whatever its case."""
    cursor = await connection.execute(
        f'SELECT {COLUMNS} FROM sammy.skills WHERE user_id = %s AND NOT draft AND lower(name) = lower(%s)',
        (user_id, name.strip()),
    )
    row = await cursor.fetchone()
    return None if row is None else skill_from(row)


async def delete_skill(connection: Connection, user_id: str, skill_id: str) -> bool:
    cursor = await connection.execute('DELETE FROM sammy.skills WHERE user_id = %s AND id = %s', (user_id, skill_id))
    return cursor.rowcount == 1


def index_of(skills: list[Skill]) -> str:
    """The saved skills in a line each, for the instructions."""
    saved = [skill.text for skill in skills if not skill.draft]
    if not saved:
        return ''
    lines = [f'- {text.name}: {text.when_to_use}' for text in saved[:INDEX_LIMIT]]
    more = len(saved) - INDEX_LIMIT
    if more > 0:
        lines.append(f'- ...and {more} more, which `load_skill` finds by name.')
    return "The user's skills (call `load_skill` with the name first when one fits):\n" + '\n'.join(lines)


# --- the agent's side ---

skill_tools: FunctionToolset[RunDeps] = FunctionToolset(id='skills')


async def skill_index(ctx: RunContext[RunDeps]) -> str:
    """The saved skills, as instructions. Looked up once per run, in a step, so a replay sees the same."""
    deps = ctx.deps
    if deps.skills.text is None:
        user_id = deps.user_id

        async def step() -> str:
            async with current().pool.connection() as connection:
                return index_of(await list_skills(connection, user_id))

        deps.skills.text = await DBOS.run_step_async({'name': 'skills.index'}, step)
    return deps.skills.text


@skill_tools.tool
async def load_skill(ctx: RunContext[RunDeps], name: str) -> str:
    """The whole of one of the user's skills, by its name in the list: what it needs, its steps, how to check it
    worked, what to give back, what to ask first and what to do if something goes wrong."""
    user_id = ctx.deps.user_id

    async def step() -> str | None:
        async with current().pool.connection() as connection:
            skill = await find_skill(connection, user_id, name)
        return None if skill is None else skill.text.as_text()

    found = await DBOS.run_step_async({'name': 'skills.load'}, step)
    if found is None:
        raise ToolFailed(f'The user has no skill called {name!r}. Use a name from the list.')
    return found


async def only_with_the_user(ctx: RunContext[RunDeps], tool: ToolDefinition) -> ToolDefinition | None:
    """Saving asks the user, who is not there for a scheduled run."""
    return tool if ctx.deps.schedule is None else None


@skill_tools.tool(requires_approval=True, prepare=only_with_the_user)
async def save_skill(
    ctx: RunContext[RunDeps],
    name: str,
    when_to_use: str,
    steps: str,
    inputs: str = '',
    verify: str = '',
    returns: str = '',
    approvals: str = '',
    failures: str = '',
) -> str:
    """Save how you just did a task as one of the user's skills, so next time goes faster. The user approves it first.

    Args:
        name: A short name, such as "Reorder groceries at Tesco".
        when_to_use: The requests it is for, in a sentence.
        steps: Numbered steps, with where things are on the site ("Account > Past orders, then Reorder").
        inputs: What the task needs from the user, if anything.
        verify: How to check it worked.
        returns: What to tell the user at the end.
        approvals: What needs the user's say-so first, such as placing the order.
        failures: What to do when a step fails.
    """
    deps = ctx.deps
    text = SkillText(
        name=name,
        when_to_use=when_to_use,
        steps=steps,
        inputs=inputs,
        verify=verify,
        returns=returns,
        approvals=approvals,
        failures=failures,
    )
    if not (text.name.strip() and text.when_to_use.strip() and text.steps.strip()):
        raise ToolFailed('A skill needs a name, when to use it, and its steps.')
    # One tool call always makes the same skill, so a replayed call cannot save a second one.
    skill_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'sammy:skill:{deps.run_id}:{ctx.tool_call_id}'))

    async def step() -> str:
        try:
            async with current().pool.connection() as connection:
                await add_skill(connection, deps.user_id, text, skill_id=skill_id)
        except NameTaken:
            return f'Error: the user already has a skill called {text.name.strip()!r}; pick another name.'
        return f'Saved the skill {text.name.strip()!r}.'

    result = await DBOS.run_step_async({'name': 'skills.save'}, step)
    if result.startswith('Error: '):
        raise ToolFailed(result.removeprefix('Error: '))
    return result
