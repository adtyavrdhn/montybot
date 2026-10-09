"""`/api/skills`: the web and Mac apps list the user's skills, edit them, save a draft and delete them (#128).

```
GET    /api/skills          drafts first, then by name
PUT    /api/skills/<id>     the whole skill; `draft: false` saves a draft, so the agent sees it from the next task
DELETE /api/skills/<id>
```

As everywhere in the API, another user's skill answers 404, the same as one that does not exist.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, StrictBool, StringConstraints
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from sammy import auth
from sammy.models import User
from sammy.resources import Resources
from sammy.skills import NameTaken, SkillText, delete_skill, list_skills, update_skill

Line = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=10_000)]


class SkillChange(BaseModel):
    name: Line
    when_to_use: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000)]
    steps: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000)]
    inputs: Text = ''
    verify: Text = ''
    returns: Text = ''
    approvals: Text = ''
    failures: Text = ''
    draft: StrictBool = False

    def text(self) -> SkillText:
        return SkillText(**self.model_dump(exclude={'draft'}))


def resources_of(request: Request) -> Resources:
    return request.state.resources


@auth.signed_in
async def read_skills(request: Request, user: User) -> Response:
    async with resources_of(request).pool.connection() as connection:
        return JSONResponse([skill.json() for skill in await list_skills(connection, user.id)])


@auth.signed_in
async def change_skill(request: Request, user: User) -> Response:
    change = SkillChange.model_validate_json(await request.body())
    try:
        async with resources_of(request).pool.connection() as connection:
            skill = await update_skill(
                connection, user.id, str(request.path_params['skill_id']), change.text(), draft=change.draft
            )
    except NameTaken:
        return JSONResponse({'detail': f'You already have a skill called “{change.name}”.'}, status_code=409)
    if skill is None:
        return JSONResponse({'detail': 'not found'}, status_code=404)
    return JSONResponse(skill.json())


@auth.signed_in
async def remove_skill(request: Request, user: User) -> Response:
    async with resources_of(request).pool.connection() as connection:
        deleted = await delete_skill(connection, user.id, str(request.path_params['skill_id']))
    return JSONResponse({'ok': True}) if deleted else JSONResponse({'detail': 'not found'}, status_code=404)
