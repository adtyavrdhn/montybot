"""What the Integrations page lists: the integrations people use most, by kind, then every other app Composio connects.

A listed integration is connected through Composio (its key is the app's slug) or, for a service Composio does not
sign users in to, through the service's own MCP server with an OAuth sign-in (its `url`): one click either way. The
web and Mac apps draw what this says, so adding one here adds it to both.
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
    'analytics': 'Analytics and monitoring',
    'sales': 'Sales and support',
}
"""The groups the page shows, in order."""


@dataclass(frozen=True, kw_only=True)
class Listed:
    key: str
    name: str
    kind: str
    provider: Literal['composio', 'mcp'] = 'composio'
    url: str = ''
    """For an MCP server: its address, signed in to with OAuth."""
    description: str = ''
    """For an MCP server; an app's comes from Composio."""

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ''


FEATURED = (
    Listed(key='slack', name='Slack', kind='chat'),
    Listed(key='microsoft_teams', name='Microsoft Teams', kind='chat'),
    Listed(key='discord', name='Discord', kind='chat'),
    Listed(key='github', name='GitHub', kind='code'),
    Listed(key='gitlab', name='GitLab', kind='code'),
    Listed(key='linear', name='Linear', kind='issues'),
    Listed(key='jira', name='Jira', kind='issues'),
    Listed(key='asana', name='Asana', kind='issues'),
    Listed(key='notion', name='Notion', kind='docs'),
    Listed(key='googledrive', name='Google Drive', kind='docs'),
    Listed(key='googledocs', name='Google Docs', kind='docs'),
    Listed(key='confluence', name='Confluence', kind='docs'),
    Listed(key='gmail', name='Gmail', kind='email'),
    Listed(key='outlook', name='Outlook', kind='email'),
    Listed(key='googlecalendar', name='Google Calendar', kind='email'),
    Listed(
        key='posthog',
        name='PostHog',
        kind='analytics',
        provider='mcp',
        url='https://mcp.posthog.com/mcp',
        description='Product analytics, feature flags, session replay and experiments.',
    ),
    Listed(key='sentry', name='Sentry', kind='analytics'),
    Listed(key='hubspot', name='HubSpot', kind='sales'),
    Listed(key='salesforce', name='Salesforce', kind='sales'),
    Listed(key='zendesk', name='Zendesk', kind='sales'),
)


def mcp_preset(name: str) -> Listed | None:
    """The listed MCP server for a service the model named ("PostHog", "posthog events"), if there is one."""
    wanted = ''.join(ch for ch in name.lower() if ch.isalnum())
    return next(
        (
            listed
            for listed in FEATURED
            if listed.provider == 'mcp' and (wanted == listed.key or wanted.startswith(listed.key))
        ),
        None,
    )


def entries(apps: dict[str, Toolkit]) -> list[dict[str, object]]:
    """The page's list: the featured integrations that can be connected here, by kind, then every other app.

    Each has `key`, `name`, `logo`, `description`, `kind` and `kind_label` (none for the other apps), `featured`,
    `provider` and, for an MCP server, `url` and `host`. `slug` and `categories` are kept for apps from before
    this list (the same as `key`, and Composio's categories).
    """
    shown: list[dict[str, object]] = []
    for listed in FEATURED:
        app = apps.get(listed.key)
        if listed.provider == 'composio' and app is None:
            continue  # Composio is not set up here, or no longer signs users in to it
        shown.append(
            {
                'key': listed.key,
                'slug': listed.key,
                'name': listed.name,
                'logo': app.logo if app else f'{LOGOS}{listed.key}',
                'description': app.description if app else listed.description,
                'categories': list(app.categories) if app else [],
                'kind': listed.kind,
                'kind_label': KINDS[listed.kind],
                'featured': True,
                'provider': listed.provider,
                'url': listed.url or None,
                'host': listed.host or None,
            }
        )
    featured = {listed.key for listed in FEATURED}
    for app in sorted(apps.values(), key=lambda app: app.name.lower()):
        if app.slug in featured:
            continue
        shown.append(
            {
                'key': app.slug,
                'slug': app.slug,
                'name': app.name,
                'logo': app.logo,
                'description': app.description,
                'categories': list(app.categories),
                'kind': None,
                'kind_label': None,
                'featured': False,
                'provider': 'composio',
                'url': None,
                'host': None,
            }
        )
    return shown
