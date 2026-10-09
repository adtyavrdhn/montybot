"""`/api/approvals`: the user's remembered approvals (`sammy.approval_rules`), and whether the automatic reviewer
(`sammy.reviewer`) may approve low-risk actions for them.

A rule is made by answering an approval with `remember` (`sammy.api.answer_ask`), from what that approval offered
to remember; it cannot be made here. Here the user sees their rules, removes them, and turns the reviewer on or off.
"""

from __future__ import annotations

from pydantic import BaseModel, StrictBool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from sammy import approval_rules, auth
from sammy.models import User
from sammy.resources import Resources


class ReviewerChange(BaseModel):
    enabled: StrictBool


def resources_of(request: Request) -> Resources:
    return request.state.resources


@auth.signed_in
async def read_approvals(request: Request, user: User) -> Response:
    """`rules`, oldest first; `reviewer`: whether this server has one (`available`) and the user turned it on."""
    resources = resources_of(request)
    async with resources.pool.connection() as connection:
        rules = await approval_rules.list_rules(connection, user.id)
        enabled = await approval_rules.reviewer_enabled(connection, user.id)
    available = resources.reviewer_model is not None
    return JSONResponse(
        {'rules': [rule.json() for rule in rules], 'reviewer': {'available': available, 'enabled': enabled}}
    )


@auth.signed_in
async def remove_rule(request: Request, user: User) -> Response:
    async with resources_of(request).pool.connection() as connection:
        deleted = await approval_rules.delete_rule(connection, user.id, str(request.path_params['rule_id']))
    return JSONResponse({'ok': True}) if deleted else JSONResponse({'detail': 'not found'}, status_code=404)


@auth.signed_in
async def set_reviewer(request: Request, user: User) -> Response:
    """POST `{"enabled": true}` to let the reviewer approve low-risk actions no rule covers, `false` to stop it."""
    body = ReviewerChange.model_validate_json(await request.body())
    async with resources_of(request).pool.connection() as connection:
        await approval_rules.set_reviewer(connection, user.id, body.enabled)
    return JSONResponse({'enabled': body.enabled})
