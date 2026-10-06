"""The browser state that travels between the remote browser and the user's machine.

The shape is engine-neutral on purpose. Cookies match what WebDriver's "Get All Cookies"
returns (HttpOnly included), and storage is plain key/value maps per origin, so a Servo
backend driven over WebDriver could fill it as easily as Playwright does.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(kw_only=True)
class Cookie:
    name: str
    value: str
    domain: str
    path: str = '/'
    expires: float = -1
    http_only: bool = False
    secure: bool = False
    same_site: str = 'Lax'


@dataclass(kw_only=True)
class BrowserState:
    url: str
    cookies: list[Cookie] = field(default_factory=list[Cookie])
    local_storage: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    """Origin -> items."""
    session_storage: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    """Origin -> items, for the tab being handed over only."""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> BrowserState:
        return cls(
            url=data['url'],
            cookies=[Cookie(**c) for c in data['cookies']],
            local_storage=data['local_storage'],
            session_storage=data['session_storage'],
        )

    def to_playwright(self) -> dict[str, Any]:
        """Playwright's `storage_state` format."""
        return {
            'cookies': [
                {
                    'name': c.name,
                    'value': c.value,
                    'domain': c.domain,
                    'path': c.path,
                    'expires': c.expires,
                    'httpOnly': c.http_only,
                    'secure': c.secure,
                    'sameSite': c.same_site,
                }
                for c in self.cookies
            ],
            'origins': [
                {'origin': origin, 'localStorage': [{'name': k, 'value': v} for k, v in items.items()]}
                for origin, items in self.local_storage.items()
            ],
        }

    @classmethod
    def from_playwright(
        cls, storage: dict[str, Any], *, url: str, session_storage: dict[str, dict[str, str]]
    ) -> BrowserState:
        return cls(
            url=url,
            cookies=[
                Cookie(
                    name=c['name'],
                    value=c['value'],
                    domain=c['domain'],
                    path=c['path'],
                    expires=c['expires'],
                    http_only=c['httpOnly'],
                    secure=c['secure'],
                    same_site=c['sameSite'],
                )
                for c in storage['cookies']
            ],
            local_storage={
                o['origin']: {item['name']: item['value'] for item in o['localStorage']} for o in storage['origins']
            },
            session_storage=session_storage,
        )

    def summary(self) -> str:
        names = ', '.join(f'{c.name}{" (HttpOnly)" if c.http_only else ""}' for c in self.cookies) or 'none'
        local = sum(len(items) for items in self.local_storage.values())
        session = sum(len(items) for items in self.session_storage.values())
        return f'{self.url} | cookies: {names} | localStorage keys: {local} | sessionStorage keys: {session}'
