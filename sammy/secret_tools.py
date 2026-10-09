"""The agent's tools for the user's secrets (`sammy.vault`): ask for one, and forget one. Neither ever sees a value;
`http_request` in `run_code` (`sammy.http_calls`) is where one is used.

```
request_secret('todoist', why, 'api.todoist.com')
  step secrets.find                  saved already, for that host? Then just use it.
  ask 'secret' (sammy.approvals)     the chat shows a password field; the API seals what the user types
  <- {saved: true} or {saved: false}
forget_secret('todoist')             asks the user first (an approval), then step secrets.forget
```
"""

from __future__ import annotations

from dbos import DBOS
from pydantic_ai import ApprovalRequired, FunctionToolset, ModelRetry, RunContext

from sammy import approvals, vault
from sammy.deps import RunDeps
from sammy.resources import current

INSTRUCTIONS = """\
When a task needs an API key, a token or a webhook URL that no integration covers, never ask for it in the chat: call
`request_secret`. The user types it into a private field, and you never see it. Then write `{{secret:NAME}}` where it
goes in `http_request`'s URL, headers or body: the value is put in on the way out, only for the host it was saved for,
and replaced by the placeholder again in what comes back."""

secret_tools: FunctionToolset[RunDeps] = FunctionToolset(id='secrets')


def checked_name(name: str) -> str:
    found = vault.name_of(name)
    if found is None:
        raise ModelRetry('A secret name is lowercase letters, digits and underscores, such as `todoist_token`.')
    return found


@secret_tools.tool
async def request_secret(ctx: RunContext[RunDeps], name: str, why: str, host: str) -> str:
    """Ask the user for a secret (an API token, a webhook URL) through a private field, and wait until they give it.
    You never see it: use it as `{{secret:NAME}}` in `http_request`. If they saved it already, this says so at once.

    Args:
        name: What to save it as: lowercase, such as `todoist_token`.
        why: A short sentence to the user: "Paste your Todoist API token so I can add the tasks."
        host: The one host it may be sent to, such as `api.todoist.com`.
    """
    name = checked_name(name)
    to = vault.host_of(host)
    if to is None:
        raise ModelRetry('`host` is the host the secret is sent to, such as `api.todoist.com`.')
    user_id = ctx.deps.user_id

    async def find() -> dict[str, str] | None:
        saved = await vault.find(current(), user_id, name)
        return None if saved is None else saved.json()

    saved = await DBOS.run_step_async({'name': 'secrets.find'}, find)
    use = f'Use `{vault.placeholder(name)}` in `http_request` to {to}.'
    if saved is not None and saved['host'] == to:
        return f'The user saved `{name}` already. {use}'
    reply = await approvals.ask(ctx, 'secret', why, {'name': name, 'host': to})
    if reply is None:
        return f'The user did not give `{name}` in time.'
    if not reply.get('saved'):
        return f'The user chose not to give `{name}`. Do what you can without it, and say what you could not.'
    return f'The user saved `{name}`. {use}'


@secret_tools.tool
async def forget_secret(ctx: RunContext[RunDeps], name: str) -> str:
    """Delete one of the user's saved secrets. Asks the user first.

    Args:
        name: The secret's name, such as `todoist_token`.
    """
    name = checked_name(name)
    user_id = ctx.deps.user_id

    async def find() -> bool:
        return await vault.find(current(), user_id, name) is not None

    if not await DBOS.run_step_async({'name': 'secrets.find'}, find):
        return f'The user has no secret named `{name}`.'
    if not ctx.tool_call_approved:
        raise ApprovalRequired

    async def forget() -> bool:
        return await vault.forget(current(), user_id, name)

    await DBOS.run_step_async({'name': 'secrets.forget'}, forget)
    return f'Forgot `{name}`.'
