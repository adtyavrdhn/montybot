"""Tenant-scoped preferences and write-once run snapshots, alongside the main store."""

from psycopg.types.json import Jsonb

from sammy.db import Connection
from sammy.model_preferences import Preference, RunModel, resolve, validate
from sammy.models import Run
from sammy.settings import Settings


async def read(connection: Connection, user_id: str, settings: Settings) -> Preference:
    cursor = await connection.execute(
        'SELECT model, settings FROM sammy.model_preferences WHERE user_id = %s', (user_id,)
    )
    row = await cursor.fetchone()
    if row is not None and row['model'] in settings.model_choices:
        try:
            return validate(Preference.model_validate(row), settings)
        except ValueError:
            # Overrides the model no longer accepts (after an upgrade) fall back to its defaults.
            return Preference(model=row['model'], settings={})
    # Revoked models affect new runs, never already snapshotted runs.
    return Preference(model=settings.model, settings={})


async def save(connection: Connection, user_id: str, preference: Preference) -> None:
    await connection.execute(
        'INSERT INTO sammy.model_preferences (user_id, model, settings) VALUES (%s, %s, %s) '
        'ON CONFLICT (user_id) DO UPDATE SET model = EXCLUDED.model, settings = EXCLUDED.settings',
        (user_id, preference.model, Jsonb(preference.settings)),
    )


async def snapshot(connection: Connection, run: Run, settings: Settings) -> RunModel:
    """Call in run.start's transaction. Lock before reading preferences, including after a lost step result."""
    await connection.execute('SELECT id FROM sammy.runs WHERE id = %s FOR UPDATE', (run.id,))
    cursor = await connection.execute('SELECT selection FROM sammy.run_models WHERE run_id = %s', (run.id,))
    row = await cursor.fetchone()
    if row is not None:
        return RunModel.model_validate(row['selection'])
    selection = resolve(await read(connection, run.user_id, settings), scheduled=run.trigger == 'schedule')
    await connection.execute(
        'INSERT INTO sammy.run_models (run_id, selection) VALUES (%s, %s)',
        (run.id, Jsonb(selection.model_dump())),
    )
    return selection
