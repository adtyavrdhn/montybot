"""#128: the skills table against Postgres (one user's skills, saved names unique, drafts out of the index), and what
the agent reads: the index and a loaded skill."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from sammy import skills, store
from sammy.db import Pool, create_pool, migrate
from sammy.skills import SkillText

pytestmark = pytest.mark.anyio

REORDER = SkillText(
    name=' Reorder groceries ',
    when_to_use='When the user wants their usual groceries again.',
    steps='1. Account > Past orders.\n2. Press Reorder on the newest.',
    approvals='Placing the order.',
)


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


async def new_user(pool: Pool, email: str) -> str:
    async with pool.connection() as c:
        user = await store.create_user(c, email, 'x')
    assert user is not None
    return user.id


async def test_a_users_skills(pool: Pool) -> None:
    alice, bob = await new_user(pool, 'alice@example.test'), await new_user(pool, 'bob@example.test')
    async with pool.connection() as c:
        saved = await skills.add_skill(c, alice, REORDER)
        assert saved.text.name == 'Reorder groceries' and not saved.draft  # trimmed
        draft = await skills.add_skill(c, alice, REORDER, draft=True)  # a draft may share a saved skill's name
        with pytest.raises(skills.NameTaken):
            await skills.add_skill(c, alice, SkillText(name='reorder GROCERIES', when_to_use='x', steps='y'))
        await skills.add_skill(c, bob, REORDER)  # another user's names are theirs

        assert [s.draft for s in await skills.list_skills(c, alice)] == [True, False]  # drafts first
        found = await skills.find_skill(c, alice, 'REORDER groceries')
        assert found is not None and found.id == saved.id  # the saved one, never the draft
        assert await skills.find_skill(c, alice, 'Something else') is None

        # Saving the draft under the same name clashes; under its own name it is a skill too.
        with pytest.raises(skills.NameTaken):
            await skills.update_skill(c, alice, draft.id, draft.text, draft=False)
        renamed = SkillText(name='Reorder, weekly', when_to_use='Weekly.', steps='1. Reorder.')
        updated = await skills.update_skill(c, alice, draft.id, renamed, draft=False)
        assert updated is not None and updated.text == renamed and not updated.draft
        assert await skills.update_skill(c, bob, draft.id, renamed, draft=False) is None  # not bob's

        assert not await skills.delete_skill(c, bob, saved.id)
        assert await skills.delete_skill(c, alice, saved.id)
        assert [s.text.name for s in await skills.list_skills(c, alice)] == ['Reorder, weekly']


async def test_adding_the_same_skill_again_changes_nothing(pool: Pool) -> None:
    """A retried step adds its skill once."""
    alice = await new_user(pool, 'alice@example.test')
    async with pool.connection() as c:
        first = await skills.add_skill(c, alice, REORDER, skill_id='5b0e3cb8-6b8c-4a9e-9a43-6f0b5a4d2c11')
        again = await skills.add_skill(c, alice, REORDER, skill_id=first.id)
        assert again == first
        assert len(await skills.list_skills(c, alice)) == 1


def test_what_the_agent_reads() -> None:
    saved = skills.Skill(id='1', text=REORDER.stripped(), draft=False)
    draft = skills.Skill(id='2', text=SkillText(name='Draft', when_to_use='Never', steps='1.'), draft=True)
    index = skills.index_of([saved, draft])
    assert index.splitlines()[1:] == ['- Reorder groceries: When the user wants their usual groceries again.']
    assert skills.index_of([draft]) == ''  # drafts wait for the user's review
    many = [
        skills.Skill(id=str(n), text=SkillText(name=f'S{n}', when_to_use='w', steps='s'), draft=False)
        for n in range(53)
    ]
    assert skills.index_of(many).splitlines()[-1] == '- ...and 3 more, which `load_skill` finds by name.'

    assert saved.text.as_text() == (
        '# Skill: Reorder groceries\n\nWhen to use it: When the user wants their usual groceries again.\n\n'
        'Steps:\n1. Account > Past orders.\n2. Press Reorder on the newest.\n\nAsk the user first before:\nPlacing the order.'
    )
