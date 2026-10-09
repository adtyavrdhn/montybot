"""Teach by showing (#128): a lesson from the live view (`sammy.liveview.recording`) becomes a draft skill.

```
live view: the user presses Teach Sammy, names the goal, does the task, presses Stop
  Lesson           the steps in words; nothing typed into a password or other sensitive field
  DbTeacher.draft  a model turns the lesson into a skill (`SkillDraft`), saved as a draft of the user's
Skills page        the user reads it, edits it, and saves it; only then does the agent see it
```

The draft's model call runs in the live view's connection, not in a run: there is nothing to resume if the app stops
meanwhile, and the user can teach again. Logfire traces it as any agent run (`invoke_agent sammy_teach`), with its
content only with `LOGFIRE_INCLUDE_CONTENT`.
"""

from __future__ import annotations

import logfire
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model

from sammy.browser.service import UserId
from sammy.liveview.recording import Drafted, TeachingFailed
from sammy.observability import timing
from sammy.resources import Resources
from sammy.skills import SkillText, add_skill

PROMPT = 'Write a skill from this lesson.'
"""The draft's user prompt starts with this, before the lesson itself."""

INSTRUCTIONS = """\
You are Sammy, a browser assistant. The user just showed you how they do a task on a website, in Sammy's own browser.
Turn the recording of what they did into a skill: a short playbook you will follow next time, with no recording to
hand. Write each step as an instruction to yourself that says where things are on the site ("On Your orders, press
Reorder on the newest order"), not as pixels or key counts. Drop detours and repeated keys. Where the user typed
a secret (a password, a code, card details), the step is to hand the browser to the user for it; never guess one.
Say in `approvals` which steps cannot be undone (orders, payments, messages) and so need the user's say-so first."""


class SkillDraft(BaseModel):
    """A skill as the model drafts it from a lesson."""

    name: str = Field(description='A short name, such as "Buy again at the corner shop".')
    when_to_use: str = Field(description='The requests it is for, in a sentence.')
    steps: str = Field(description='Numbered steps, each on its own line.')
    inputs: str = Field(default='', description='What the task needs from the user, if anything.')
    verify: str = Field(default='', description='How to check it worked.')
    returns: str = Field(default='', description='What to tell the user at the end.')
    approvals: str = Field(default='', description="What needs the user's say-so first.")
    failures: str = Field(default='', description='What to do when a step fails.')


class DbTeacher:
    """`sammy.liveview.recording.Teacher`: drafts with the app's model and keeps the draft in `sammy.skills`."""

    def __init__(self, resources: Resources) -> None:
        self._resources = resources
        model = resources.agent.model
        assert isinstance(model, Model | str), 'the agent is built with its model'
        self._agent = Agent(model, name='sammy_teach', output_type=SkillDraft, instructions=INSTRUCTIONS)

    async def draft(self, *, user_id: UserId, goal: str, lesson: str) -> Drafted:
        with timing('skill.draft'):
            try:
                result = await self._agent.run(f'{PROMPT}\n\n{lesson}')
            except Exception as error:  # the model's errors are many; the user can try again whatever they were
                logfire.warn('Drafting a skill failed: {error_type}', error_type=type(error).__qualname__)
                raise TeachingFailed('Sammy could not write a draft from that just now. Please try again.') from error
            draft = result.output
            text = SkillText(
                name=draft.name.strip()[:200] or goal,
                when_to_use=draft.when_to_use.strip() or goal,
                steps=draft.steps,
                inputs=draft.inputs,
                verify=draft.verify,
                returns=draft.returns,
                approvals=draft.approvals,
                failures=draft.failures,
            )
            if not text.steps.strip():
                raise TeachingFailed('Sammy could not make steps out of that. Try showing it again.')
            async with self._resources.pool.connection() as connection:
                skill = await add_skill(connection, user_id, text, draft=True)
        return Drafted(skill_id=skill.id, name=skill.text.name)
