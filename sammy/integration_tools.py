"""The agent's tools for the user's integrations (`sammy.integrations`): apps through Composio and the user's own
MCP servers, all behind three tools that never change, whatever the user has connected.

```
user: "yo what's on my linear"
  instructions say what is connected                          step integrations.list
  connect_integration('Linear', ...)  Linear is not connected: step integrations.find
    ask 'connect' (sammy.approvals)                         the chat shows a "Connect Linear" card
    ... the user connects; the callback answers the ask        sammy.approvals.connected
    step integrations.find again                               it is connected now: carry on
  list_integration_tools('linear', 'issue')                    step integrations.tools
  call_integration_tool('linear', 'LINEAR_LIST_LINEAR_ISSUES', {...})
                                                               step integrations.tool, then integrations.call
```

Why not one toolset per connection: under DBOS a run's toolsets are fixed when the agent is built, and its tool
definitions are cached with the prompt. A connection's tools are listed and called in DBOS steps instead, so a
restarted run replays what they returned and calls nothing twice.

The user id is always the run's. A tool that changes something (anything not marked read-only) asks the user first,
as `commit` does, unless a schedule the user approved started the run.
"""

from __future__ import annotations

from typing import Any

from dbos import DBOS
from pydantic_ai import ApprovalRequired, FunctionToolset, RunContext, ToolFailed

from sammy import approvals
from sammy.deps import RunDeps
from sammy.integrations import Connection, IntegrationError, Offer, Tool
from sammy.resources import current

INSTRUCTIONS = """\
The user can connect apps (Linear, GitHub, Gmail, Google Calendar, Notion, Slack, Jira and many more) and their own MCP
servers. When a request is about a connected service, use it through `list_integration_tools` and
`call_integration_tool`, not its website. When a request needs a service that is not connected (their Linear issues,
their GitHub repos, their inbox), call `connect_integration` at once: the user gets a button in the chat to connect
it, and you carry on when they have. Do not ask them in words first."""

MAX_TOOLS = 12
"""Tools `list_integration_tools` returns at most: search to narrow it."""
MAX_DESCRIPTION = 400

integration_tools: FunctionToolset[RunDeps] = FunctionToolset(id='integrations')


async def connected_integrations(ctx: RunContext[RunDeps]) -> str:
    """What the user has connected, as instructions. Looked up once per run (in a step) and again after a connect."""
    deps = ctx.deps
    if deps.connected.text is None:
        user_id = deps.user_id

        async def step() -> str:
            try:
                connections = await current().integrations.connections(user_id)
            except IntegrationError as error:
                return f'Integrations could not be listed just now: {error}'
            return in_words(connections)

        deps.connected.text = await DBOS.run_step_async({'name': 'integrations.list'}, step)
    return deps.connected.text


def in_words(connections: list[Connection]) -> str:
    if not connections:
        return 'The user has no integrations connected yet.'
    lines = [
        f'- `{c.key}`: {c.name}{"" if c.state == "connected" else " (needs the user to sign in again)"}'
        for c in connections
    ]
    return "The user's connected integrations:\n" + '\n'.join(lines)


@integration_tools.tool
async def list_integration_tools(ctx: RunContext[RunDeps], integration: str, search: str = '') -> list[dict[str, Any]]:
    """What one of the user's integrations can do: its tools, with the arguments each takes.

    Args:
        integration: The integration's key, as listed: `linear`, `github`, `mcp:notes`.
        search: Words to look for in the tools' names and descriptions, such as "issue create".
    """
    user_id = ctx.deps.user_id

    async def step() -> list[dict[str, Any]] | str:
        try:
            tools = await current().integrations.tools(user_id, integration)
        except IntegrationError as error:
            return str(error)
        return [tool_json(tool) for tool in matching(tools, search)[:MAX_TOOLS]]

    found = await DBOS.run_step_async({'name': 'integrations.tools'}, step)
    if isinstance(found, str):
        raise ToolFailed(found)
    return found


def matching(tools: list[Tool], search: str) -> list[Tool]:
    words = search.lower().split()
    if not words:
        return tools
    scored = [
        (sum(word in f'{tool.name} {tool.title} {tool.description}'.lower() for word in words), tool) for tool in tools
    ]
    return [tool for score, tool in sorted(scored, key=lambda pair: -pair[0]) if score]


def tool_json(tool: Tool) -> dict[str, Any]:
    description = (
        tool.description if len(tool.description) <= MAX_DESCRIPTION else tool.description[:MAX_DESCRIPTION] + '…'
    )
    return {
        'name': tool.name,
        'title': tool.title,
        'description': description,
        'read_only': tool.read_only,
        'parameters': tool.parameters,
    }


@integration_tools.tool
async def call_integration_tool(
    ctx: RunContext[RunDeps], integration: str, tool: str, arguments: dict[str, Any] | None = None
) -> str:
    """Use a tool of one of the user's integrations. One that changes something (creates, updates, sends, deletes)
    asks the user first; one that only reads does not.

    Args:
        integration: The integration's key: `linear`, `mcp:notes`.
        tool: The tool's `name`, from `list_integration_tools`.
        arguments: The tool's arguments, as its `parameters` describe them.
    """
    user_id = ctx.deps.user_id
    arguments = arguments or {}

    async def find() -> Tool | str:
        try:
            return await current().integrations.tool(user_id, integration, tool)
        except IntegrationError as error:
            return str(error)

    found = await DBOS.run_step_async({'name': 'integrations.tool'}, find)
    if isinstance(found, str):
        raise ToolFailed(found)
    # The user approved a schedule's task when they set it up, and is not there when it runs (as for `commit`).
    if not found.read_only and ctx.deps.schedule is None and not ctx.tool_call_approved:
        raise ApprovalRequired

    async def call() -> tuple[bool, str]:
        try:
            return True, await current().integrations.call(user_id, integration, found, arguments)
        except IntegrationError as error:
            return False, str(error)

    # Not retried: a call that reached the service may have done what it does.
    done, result = await DBOS.run_step_async({'name': 'integrations.call'}, call)
    if not done:
        raise ToolFailed(result)
    return result


@integration_tools.tool
async def connect_integration(ctx: RunContext[RunDeps], service: str, reason: str) -> str:
    """Ask the user to connect a service this task needs, with a button in the chat, and wait until they have. Use it
    as soon as a request needs a service that is not connected; if it is connected already, this says so.

    Args:
        service: The service, as the user would name it: "Linear", "GitHub", "Gmail".
        reason: Why, in a short sentence to the user: "Connect Linear so I can look up your issues."
    """
    deps = ctx.deps
    user_id = deps.user_id

    async def find() -> dict[str, str] | str:
        try:
            found = await current().integrations.offer(user_id, service)
        except IntegrationError as error:
            return str(error)
        return {'type': 'connection' if isinstance(found, Connection) else 'offer', **found.json()}

    found = await DBOS.run_step_async({'name': 'integrations.find'}, find)
    if isinstance(found, str):
        raise ToolFailed(found)
    if found['type'] == 'connection' and found['state'] == 'connected':
        return f'{found["name"]} is connected already, as `{found["key"]}`. Use it.'
    offer = offer_of(found)
    if deps.schedule is not None:
        return (
            f'{offer["name"]} is not connected, and the user is not here to connect it (a schedule started this task). '
            'Tell them to connect it from Integrations in Sammy.'
        )
    reply = await approvals.ask(ctx, 'connect', reason, {'integration': offer})
    deps.connected.text = None  # what is connected may have changed: look again
    if reply is None:
        return f'The user did not connect {offer["name"]} in time.'
    if not reply.get('connected'):
        return f'The user chose not to connect {offer["name"]} now. Do what you can without it, and say what you could not.'
    after = await DBOS.run_step_async({'name': 'integrations.find'}, find)
    if isinstance(after, dict) and after['type'] == 'connection' and after['state'] == 'connected':
        return f'{after["name"]} is connected now, as `{after["key"]}`. Find its tools with `list_integration_tools`.'
    if offer['provider'] == 'mcp':
        return 'The user added an MCP server. The connected integrations are listed in your instructions now.'
    return f'{offer["name"]} is still not connected. Tell the user, and do what you can without it.'


def offer_of(found: dict[str, str]) -> dict[str, str]:
    """What the chat's card offers: the app to connect, or the user's own server to sign in to again, or (for a
    service Composio does not have) an MCP server to add."""
    if found['type'] == 'offer':
        return {key: found[key] for key in ('provider', 'key', 'name', 'logo')}
    # Connected once, but broken now: connect it again.
    offer = Offer(
        provider='composio' if found['provider'] == 'composio' else 'mcp',
        key=found['key'],
        name=found['name'],
        logo=found['logo'],
    )
    return {**offer.json(), **({'server_id': found['id']} if found['provider'] == 'mcp' else {})}
