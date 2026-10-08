"""Fill and apply the PoC's engine-neutral BrowserState through Servo's W3C WebDriver server.

poc/sammy_poc/state.py is imported by path and not modified. Everything here is plain WebDriver:
Get All Cookies / Add Cookie / Execute Script / Navigate To / New Window.

What WebDriver forces on us (measured on servoshell 0.7.0 nightly 2026-10-05):

- Get All Cookies only returns cookies for the *current document's URL*, and Servo leaves HttpOnly cookies out
  (servo components/script/event_loop/webdriver_handlers.rs asks the cookie jar with `CookieSource::NonHTTP`).
  So `export_state` cannot see HttpOnly cookies; it reports that instead of pretending.
- Add Cookie only accepts a `domain` equal to the current document's host, so a cookie for `.shop.test` has to be
  added while a document from `shop.test` itself is loaded. HttpOnly cookies *can* be added.
- There is no init-script hook, so sessionStorage is written into the tab on a cheap same-origin document before the
  real page is loaded (sessionStorage survives same-tab navigation within the origin).

To visit an origin cheaply we load `<origin>/robots.txt` (any same-origin document works; a 404 page is fine too).
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from collections.abc import Callable
from urllib.parse import urlsplit

from wd import Session

_POC_STATE = pathlib.Path(__file__).resolve().parents[3] / 'poc' / 'sammy_poc' / 'state.py'
_spec = importlib.util.spec_from_file_location('sammy_poc_state', _POC_STATE)
assert _spec and _spec.loader
state_mod = importlib.util.module_from_spec(_spec)
sys.modules['sammy_poc_state'] = state_mod
_spec.loader.exec_module(state_mod)
BrowserState = state_mod.BrowserState
Cookie = state_mod.Cookie

READ_STORAGE = """return {local: Object.fromEntries(Object.entries(localStorage)),
                          session: Object.fromEntries(Object.entries(sessionStorage))}"""
WRITE_STORAGE = """const [kind, items] = arguments; const s = kind === 'local' ? localStorage : sessionStorage;
for (const [k, v] of Object.entries(items)) s.setItem(k, v); return s.length"""


def origin_of(url: str) -> str:
    u = urlsplit(url)
    return f'{u.scheme}://{u.netloc}'


def _landing(origin: str) -> str:
    return origin + '/robots.txt'


def _from_webdriver(c: dict, host: str) -> Cookie:
    # Servo reports a domain cookie as its bare domain ("shop.test") and a host-only cookie as the document host.
    domain = c.get('domain') or host
    if domain != host:
        domain = '.' + domain.lstrip('.')
    return Cookie(
        name=c['name'],
        value=c['value'],
        domain=domain,
        path=c.get('path', '/'),
        expires=float(c['expiry']) if c.get('expiry') is not None else -1,
        http_only=bool(c.get('httpOnly', False)),
        secure=bool(c.get('secure', False)),
        same_site=c.get('sameSite') or 'Lax',
    )


def export_state(d: Session, origins: list[str]) -> tuple[BrowserState, dict]:
    """Export cookies + localStorage for `origins` (plus the current tab's origin), and sessionStorage of the current tab.

    Other origins are visited in a separate tab (same cookie jar and storage) so the handed-over tab keeps its page.
    Returns the state and a report of what WebDriver could not see.
    """
    url = d.current_url()
    here = origin_of(url)
    report: dict = {'httponly_visible': False, 'origins_visited': []}
    cur = d.js(READ_STORAGE)
    cookies: dict[tuple[str, str, str], Cookie] = {}
    local: dict[str, dict[str, str]] = {}
    session = {here: cur['session']} if cur['session'] else {}

    def collect(origin: str, storage: dict | None = None) -> None:
        host = urlsplit(origin).hostname or ''
        for c in d.cookies():
            ck = _from_webdriver(c, host)
            cookies[(ck.name, ck.domain, ck.path)] = ck
            report['httponly_visible'] |= ck.http_only
        s = storage if storage is not None else d.js(READ_STORAGE)
        if s['local']:
            local[origin] = s['local']
        report['origins_visited'].append(origin)

    collect(here, cur)
    others = [o for o in dict.fromkeys(origins) if o != here]
    if others:
        main = d.cmd('GET', '/window')
        tab = d.new_window('tab')
        d.switch_window(tab)
        for o in others:
            d.get(_landing(o))
            collect(o)
        d.cmd('DELETE', '/window')
        d.switch_window(main)
    return BrowserState(url=url, cookies=list(cookies.values()), local_storage=local, session_storage=session), report


def apply_state(
    d: Session, state: BrowserState, *, origin_for_host: Callable[[str, bool], str] | None = None
) -> dict:
    """Seed a fresh Servo session with `state`, then load `state.url` in the current tab.

    `origin_for_host(host, secure)` maps a cookie host to an origin to visit (default https://host); tests use it to add
    a port.
    """
    origin_for_host = origin_for_host or (lambda host, secure: f'https://{host}')
    report: dict = {'added': [], 'failed': []}
    by_host: dict[str, list[Cookie]] = {}
    for c in state.cookies:
        by_host.setdefault(c.domain.lstrip('.'), []).append(c)
    for host, cs in by_host.items():
        d.get(_landing(origin_for_host(host, any(c.secure for c in cs))))
        for c in cs:
            wd_cookie = {'name': c.name, 'value': c.value, 'path': c.path, 'httpOnly': c.http_only, 'secure': c.secure,
                         'sameSite': c.same_site}
            if c.domain.startswith('.'):
                wd_cookie['domain'] = host  # equals the loaded document's host, so Servo accepts it as a domain cookie
            if c.expires and c.expires > 0:
                wd_cookie['expiry'] = int(c.expires)
            try:
                d.add_cookie(wd_cookie)
                report['added'].append(f'{c.name}@{c.domain}')
            except Exception as e:  # noqa: BLE001
                report['failed'].append(f'{c.name}@{c.domain}: {e}')
    for origin, items in state.local_storage.items():
        d.get(_landing(origin))
        d.js(WRITE_STORAGE, 'local', items)
    target = origin_of(state.url)
    if state.session_storage.get(target):
        d.get(_landing(target))
        d.js(WRITE_STORAGE, 'session', state.session_storage[target])
    d.get(state.url)
    return report
