"""The browser state that is saved per user and moves between engines and machines.

Promoted from `poc/sammy_poc/state.py`. The shape is engine-neutral on purpose: cookies match what W3C WebDriver's
Get All Cookies returns (HttpOnly included), and storage is plain key/value maps per origin, so Chromium (Playwright),
Servo (WebDriver) and anything else can fill it. Engine-specific conversions, such as Playwright's `storage_state`,
belong in that engine's backend, not here.

The state is a set of live credentials. Never log it, put it in a trace, or show it to the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypedDict
from urllib.parse import urlsplit

BLANK_URL = 'about:blank'
"""The URL of a browser that has no page open, and of a fresh `BrowserState`."""

SameSite = Literal['Strict', 'Lax', 'None']


def origin_of(url: str) -> str | None:
    """The storage key for `url`: `scheme://host[:port]` for an http(s) URL, else None."""
    parts = urlsplit(url)
    return f'{parts.scheme}://{parts.netloc}' if parts.scheme in ('http', 'https') else None


@dataclass(frozen=True, kw_only=True)
class Cookie:
    name: str
    value: str
    domain: str
    """The host, such as `shop.example`, for a host-only cookie; with a leading dot, `.example`, for a cookie that
    also applies to subdomains. The same convention as Playwright's and WebDriver's cookie exports."""
    path: str = '/'
    expires: float = -1
    """Seconds since the Unix epoch, or -1 for a session cookie."""
    http_only: bool = False
    secure: bool = False
    same_site: SameSite = 'Lax'


@dataclass(kw_only=True)
class BrowserState:
    """One tab's worth of a user's browser: where it is, and what it is signed into."""

    url: str = BLANK_URL
    """The page the tab is on."""
    cookies: list[Cookie] = field(default_factory=list[Cookie])
    """Every cookie, for every domain, HttpOnly included."""
    local_storage: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    """Origin, such as `https://shop.example`, to items. Origins with no items are left out."""
    session_storage: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    """Origin to items, for this one tab only. Origins with no items are left out."""

    def to_json(self) -> BrowserStateJSON:
        return {
            'url': self.url,
            'cookies': [
                {
                    'name': c.name,
                    'value': c.value,
                    'domain': c.domain,
                    'path': c.path,
                    'expires': c.expires,
                    'http_only': c.http_only,
                    'secure': c.secure,
                    'same_site': c.same_site,
                }
                for c in self.cookies
            ],
            'local_storage': {origin: dict(items) for origin, items in self.local_storage.items()},
            'session_storage': {origin: dict(items) for origin, items in self.session_storage.items()},
        }

    @classmethod
    def from_json(cls, data: BrowserStateJSON) -> BrowserState:
        """Read what `to_json` wrote. Also reads `poc/`'s format, which is the same."""
        return cls(
            url=data['url'],
            cookies=[Cookie(**c) for c in data['cookies']],
            local_storage={origin: dict(items) for origin, items in data['local_storage'].items()},
            session_storage={origin: dict(items) for origin, items in data['session_storage'].items()},
        )


class CookieJSON(TypedDict):
    name: str
    value: str
    domain: str
    path: str
    expires: float
    http_only: bool
    secure: bool
    same_site: SameSite


class BrowserStateJSON(TypedDict):
    url: str
    cookies: list[CookieJSON]
    local_storage: dict[str, dict[str, str]]
    session_storage: dict[str, dict[str, str]]
