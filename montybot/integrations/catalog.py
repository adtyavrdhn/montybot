"""What the Integrations page lists: the integrations people use most, by kind, then every other app Composio connects.

A listed integration is connected through Composio (its key is the app's slug) or through the service's own hosted MCP
server (its `url`). The MCP servers are the ones pydantic-ai-harness has first-class integrations for (`harness` names
the module), and come first: where one is listed, it takes the place of Composio's app for the same service, which
stays under More apps ("Linear via Composio") for the accounts connected through it. Most of these servers let Monty
sign the user in with OAuth, in one click; one that does not register clients that way takes a token the user pastes
(`auth='token'`). The web and Mac apps draw what this says, so adding one here adds it to both.

The harness's own `'oauth'` mode signs in through a browser on the machine it runs on, which a server cannot do: Monty
signs in itself (`montybot.integrations.oauth`), so only the servers' addresses come from the harness. Google
Workspace's servers are not listed: Google does not let clients register themselves, and its access tokens last an
hour, so neither way works there (Composio connects Gmail, Calendar, Drive and Docs instead).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from montybot.integrations.composio import Toolkit

LOGOS = 'https://logos.composio.dev/api/'

KINDS = {
    'chat': 'Chat',
    'code': 'Code',
    'issues': 'Issue tracking',
    'docs': 'Docs and files',
    'email': 'Email and calendar',
    'meetings': 'Meetings and CRM',
    'analytics': 'Analytics and monitoring',
    'sales': 'Sales and support',
}
"""The groups the page shows, in order."""

# The hosted MCP servers of pydantic-ai-harness's integrations, as each module has them (a test checks they still do).
LINEAR_MCP_URL = 'https://mcp.linear.app/mcp'
NOTION_MCP_URL = 'https://mcp.notion.com/mcp'
GITHUB_MCP_URL = 'https://api.githubcopilot.com/mcp/'
POSTHOG_MCP_URL = 'https://mcp.posthog.com/mcp'
LOGFIRE_US_MCP_URL = 'https://logfire-us.pydantic.dev/mcp'
LOGFIRE_EU_MCP_URL = 'https://logfire-eu.pydantic.dev/mcp'
GRAIN_MCP_URL = 'https://api.grain.com/_/mcp'
DAY_AI_MCP_URL = 'https://day.ai/api/mcp'
PYLON_MCP_URL = 'https://mcp.usepylon.com'


@dataclass(frozen=True, kw_only=True)
class Listed:
    key: str
    name: str
    kind: str
    provider: Literal['composio', 'mcp'] = 'composio'
    url: str = ''
    """For an MCP server: its address."""
    description: str = ''
    """For an MCP server; an app's comes from Composio."""
    harness: str = ''
    """For an MCP server: the pydantic-ai-harness module that integrates it (`linear`, `logfire_mcp`)."""
    auth: Literal['oauth', 'token'] = 'oauth'
    """For an MCP server: how the user signs in. `token` is a key they paste, sent as `Bearer <token>` in
    `token_header`, as the harness sends it."""
    token_hint: str = ''
    """For a token: what it is and where it comes from, said beside the field."""
    token_header: str = 'Authorization'
    logo_key: str = ''
    """The logo's name, where it is not the key."""

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ''

    @property
    def logo(self) -> str:
        return f'{LOGOS}{self.logo_key or self.key}'

    @property
    def needs_token(self) -> bool:
        return self.provider == 'mcp' and self.auth == 'token'


FEATURED = (
    Listed(key='slack', name='Slack', kind='chat'),
    Listed(key='microsoft_teams', name='Microsoft Teams', kind='chat'),
    Listed(key='discord', name='Discord', kind='chat'),
    Listed(
        key='github',
        name='GitHub',
        kind='code',
        provider='mcp',
        url=GITHUB_MCP_URL,
        description='Repositories, issues, pull requests and Actions.',
        harness='github',
        auth='token',  # GitHub's sign-in does not let Monty register itself
        token_hint='A GitHub personal access token (github.com/settings/tokens)',
    ),
    Listed(key='gitlab', name='GitLab', kind='code'),
    Listed(
        key='linear',
        name='Linear',
        kind='issues',
        provider='mcp',
        url=LINEAR_MCP_URL,
        description='Issues, projects and cycles.',
        harness='linear',
    ),
    Listed(key='jira', name='Jira', kind='issues'),
    Listed(key='asana', name='Asana', kind='issues'),
    Listed(
        key='notion',
        name='Notion',
        kind='docs',
        provider='mcp',
        url=NOTION_MCP_URL,
        description='Pages, databases and comments in your workspace.',
        harness='notion',
    ),
    Listed(key='googledrive', name='Google Drive', kind='docs'),
    Listed(key='googledocs', name='Google Docs', kind='docs'),
    Listed(key='confluence', name='Confluence', kind='docs'),
    Listed(key='gmail', name='Gmail', kind='email'),
    Listed(key='outlook', name='Outlook', kind='email'),
    Listed(key='googlecalendar', name='Google Calendar', kind='email'),
    Listed(
        key='grain',
        name='Grain',
        kind='meetings',
        provider='mcp',
        url=GRAIN_MCP_URL,
        description='Meeting recordings, transcripts and notes.',
        harness='grain',
    ),
    Listed(
        key='day_ai',
        name='Day AI',
        kind='meetings',
        provider='mcp',
        url=DAY_AI_MCP_URL,
        description='The CRM that keeps itself: contacts, companies, deals and meetings.',
        harness='day_ai',
    ),
    Listed(
        key='posthog',
        name='PostHog',
        kind='analytics',
        provider='mcp',
        url=POSTHOG_MCP_URL,
        description='Product analytics, feature flags, session replay and experiments.',
        harness='posthog',
    ),
    Listed(
        key='logfire',
        name='Logfire',
        kind='analytics',
        provider='mcp',
        url=LOGFIRE_US_MCP_URL,
        description="Traces, logs and metrics from Pydantic Logfire's US region.",
        harness='logfire_mcp',
    ),
    Listed(
        key='logfire_eu',
        name='Logfire EU',
        kind='analytics',
        provider='mcp',
        url=LOGFIRE_EU_MCP_URL,
        description="Traces, logs and metrics from Pydantic Logfire's EU region.",
        harness='logfire_mcp',
        logo_key='logfire',
    ),
    Listed(key='sentry', name='Sentry', kind='analytics'),
    Listed(
        key='pylon',
        name='Pylon',
        kind='sales',
        provider='mcp',
        url=PYLON_MCP_URL,
        description='Support issues, accounts and the knowledge base.',
        harness='pylon',
    ),
    Listed(key='hubspot', name='HubSpot', kind='sales'),
    Listed(key='salesforce', name='Salesforce', kind='sales'),
    Listed(key='zendesk', name='Zendesk', kind='sales'),
)

SERVERS = {listed.key: listed for listed in FEATURED if listed.provider == 'mcp'}
"""The listed MCP servers, by key. Composio's app with the same key is under More apps."""


def normalized(text: str) -> str:
    return ''.join(ch for ch in text.lower() if ch.isalnum())


def mcp_preset(name: str) -> Listed | None:
    """The listed MCP server for a service the model named ("Linear", "linear issues", "Logfire EU"), if there is
    one: the longest key or name the words start with."""
    wanted = normalized(name)
    found = [
        (len(alias), listed)
        for listed in SERVERS.values()
        for alias in {normalized(listed.key), normalized(listed.name)}
        if wanted.startswith(alias)
    ]
    return max(found, key=lambda pair: pair[0])[1] if found else None


def entries(apps: dict[str, Toolkit]) -> list[dict[str, object]]:
    """The page's list: the featured integrations that can be connected here, by kind, then every other app.

    Each has `key`, `name`, `logo`, `description`, `kind` and `kind_label` (none for the other apps), `featured`,
    `provider`; for an MCP server, `url`, `host` and `auth` (`oauth` or `token`), and for a token, `token_hint` and
    `token_header`. `slug` and `categories` are kept for apps from before this list (the same as `key`, and
    Composio's categories). A key is one provider's: Composio's app for a listed MCP server's service has the same
    key, and is under More apps as "<name> via Composio".
    """
    shown: list[dict[str, object]] = []
    for listed in FEATURED:
        app = apps.get(listed.key) if listed.provider == 'composio' else None
        if listed.provider == 'composio' and app is None:
            continue  # Composio is not set up here, or no longer signs users in to it
        mcp = listed.provider == 'mcp'
        shown.append(
            {
                'key': listed.key,
                'slug': listed.key,
                'name': listed.name,
                'logo': app.logo if app else listed.logo,
                'description': app.description if app else listed.description,
                'categories': list(app.categories) if app else [],
                'kind': listed.kind,
                'kind_label': KINDS[listed.kind],
                'featured': True,
                'provider': listed.provider,
                'url': listed.url if mcp else None,
                'host': listed.host if mcp else None,
                'auth': listed.auth if mcp else None,
                'token_hint': listed.token_hint if listed.needs_token else None,
                'token_header': listed.token_header if listed.needs_token else None,
            }
        )
    featured = {listed.key for listed in FEATURED if listed.provider == 'composio'}
    for app in sorted(apps.values(), key=lambda app: app.name.lower()):
        if app.slug in featured:
            continue
        shown.append(
            {
                'key': app.slug,
                'slug': app.slug,
                'name': f'{app.name} via Composio' if app.slug in SERVERS else app.name,
                'logo': app.logo,
                'description': app.description,
                'categories': list(app.categories),
                'kind': None,
                'kind_label': None,
                'featured': False,
                'provider': 'composio',
                'url': None,
                'host': None,
                'auth': None,
                'token_hint': None,
                'token_header': None,
            }
        )
    return shown
