"""The account as a whole (#135): take everything of yours with you (sammy.export), or delete it all
(sammy.accounts). Both are in the web app's menu and the Mac app's Account settings."""

from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from sammy import accounts, auth, export, store
from sammy.api import apps_failed, password_matches, resources_of
from sammy.integrations import IntegrationError
from sammy.models import User

NO_STORE = {'Cache-Control': 'no-store'}


class Confirmation(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


@auth.signed_in
async def export_data(request: Request, user: User) -> Response:
    """GET. A zip of everything of the user's. When email is set up and the account is big, it is built in the
    background instead, and the answer is 202: the user gets an email with a link to it (`download_export`)."""
    resources = resources_of(request)
    settings = resources.settings
    if settings.smtp_url and await export.size(resources.pool, resources.workspaces, user.id) > (
        settings.export_inline_bytes
    ):
        await export.start(user.id)
        return JSONResponse({'emailed': True, 'email': user.email}, status_code=202)
    with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as file:
        target = Path(file.name)
    try:
        await export.write(
            target,
            pool=resources.pool,
            workspaces=resources.workspaces,
            integrations=resources.integrations,
            user_id=user.id,
        )
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    cleanup = BackgroundTask(target.unlink, missing_ok=True)
    return FileResponse(
        target, media_type='application/zip', filename=export.file_name(), headers=NO_STORE, background=cleanup
    )


async def download_export(request: Request) -> Response:
    """GET the link in the email. Not tied to a session: on a Mac the user opens it in their own browser. The signed
    link is the key, for a day."""
    path = export.ready(resources_of(request).settings, str(request.path_params['token']))
    if path is None:
        return JSONResponse({'detail': 'This link has expired. Export your data again from Sammy.'}, status_code=404)
    headers = NO_STORE | {'Referrer-Policy': 'no-referrer'}
    return FileResponse(path, media_type='application/zip', filename=export.file_name(), headers=headers)


@auth.signed_in
async def delete_account(request: Request, user: User) -> Response:
    """DELETE, with the user's password (`Confirmation`). Everything of theirs goes, and they are signed out."""
    body = Confirmation.model_validate_json(await request.body())
    resources = resources_of(request)
    async with resources.pool.connection() as connection:
        found = await store.find_login(connection, user.email)
    if found is None or not await password_matches(body.password, found[1]):
        return JSONResponse({'detail': 'That password is wrong.'}, status_code=403)
    try:
        await accounts.delete(resources, user.id)
    except IntegrationError as error:
        return apps_failed(error)
    auth.sign_out(request)
    return JSONResponse({'ok': True})
