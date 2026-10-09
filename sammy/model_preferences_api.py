"""Authenticated model picker endpoints shared by the web and Mac clients."""

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from sammy import auth, model_preferences_store
from sammy.model_preferences import Preference, envelope, validate
from sammy.models import User
from sammy.resources import Resources


@auth.signed_in
async def preferences(request: Request, user: User) -> Response:
    resources: Resources = request.state.resources
    settings = resources.settings
    async with resources.pool.connection() as connection:
        if request.method == 'PUT':
            try:
                preference = validate(Preference.model_validate_json(await request.body()), settings)
            except ValidationError:
                return JSONResponse({'detail': 'Invalid model settings.'}, status_code=422)
            except ValueError as error:
                return JSONResponse({'detail': str(error)}, status_code=422)
            await model_preferences_store.save(connection, user.id, preference)
        else:
            preference = await model_preferences_store.read(connection, user.id, settings)
    return JSONResponse(envelope(preference, settings), headers={'Cache-Control': 'no-store'})
