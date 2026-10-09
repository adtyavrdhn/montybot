"""Deleting an account (#135): `DELETE /api/account` (sammy.account_api), once the user has typed their password.

```
1. disconnect their apps in Composio     first: the only step another service can refuse, before anything is gone
2. stop their runs                       workflows.stop: no more model calls, and each run's browser is closed
3. delete their schedules                schedules.delete: nothing of theirs fires again
4. close the browser kept for them       it holds their cookies in memory
5. delete their DBOS workflows           runs, schedule occurrences and exports: their inputs and steps hold the
                                         user's data, and DBOS keeps them in its own tables
6. delete the user's row                 every table of ours references sammy.users ON DELETE CASCADE, so every row
                                         of theirs goes with it, in one statement
7. delete their files and exports
```

If a step fails, the user can try again: each step finds only what is left.
"""

from __future__ import annotations

import asyncio
import shutil

from dbos import DBOS

from sammy import export, schedules, store, workflows
from sammy.models import ACTIVE, Run
from sammy.observability import timed
from sammy.resources import Resources

UNFINISHED = ['PENDING', 'ENQUEUED', 'DELAYED']
"""DBOS workflow states that may still run, so are cancelled before they are deleted."""


@timed('account.delete')
async def delete(resources: Resources, user_id: str) -> None:
    """Raises `IntegrationError` if Composio cannot disconnect the user's apps; nothing else is deleted then."""
    await resources.integrations.disconnect_all(user_id)
    async with resources.pool.connection() as connection:
        cursor = await connection.execute(f'SELECT {store.RUN_COLUMNS} FROM sammy.runs WHERE user_id = %s', (user_id,))
        runs = [store.run_from(row) for row in await cursor.fetchall()]
        found = await store.list_schedules(connection, user_id)
    for run in runs:
        if run.status in ACTIVE:
            await workflows.stop(resources, run)
    for schedule in found:
        await schedules.delete(resources.pool, user_id, schedule.id)
    await resources.browser.discard_parked(user_id)
    await forget_workflows(user_id, runs, [schedules.dbos_name(s.id) for s in found])
    async with resources.pool.connection() as connection:
        await connection.execute('DELETE FROM sammy.users WHERE id = %s', (user_id,))
    for folder in (resources.workspaces.directory(user_id), export.user_folder(resources.settings, user_id)):
        await asyncio.to_thread(shutil.rmtree, folder, ignore_errors=True)


async def forget_workflows(user_id: str, runs: list[Run], schedule_names: list[str]) -> None:
    """Each run's workflow has the run's id; an occurrence of a schedule is its parent, and is found by the schedule's
    name; an export is found by its id's prefix. Children (an occurrence's run) go with their parents."""
    occurrences = (
        await DBOS.list_workflows_async(schedule_name=schedule_names, load_input=False, load_output=False)
        if schedule_names
        else []
    )
    exports = await DBOS.list_workflows_async(
        workflow_id_prefix=export.workflow_prefix(user_id), load_input=False, load_output=False
    )
    for workflow in (*occurrences, *exports):
        if workflow.status in UNFINISHED:
            await DBOS.cancel_workflow_async(workflow.workflow_id)
    ids = [run.id for run in runs] + [w.workflow_id for w in (*occurrences, *exports)]
    if ids:
        await DBOS.delete_workflows_async(ids, delete_children=True)
