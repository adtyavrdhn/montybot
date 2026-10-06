"""A tiny shop that stands in for walmart.com.

- Sign-in sets an HttpOnly `sid` cookie, so only the browser engine can carry it, not page JS.
- The cart lives in localStorage.
- A per-tab view counter lives in sessionStorage.
"""

from __future__ import annotations

import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

PASSWORD = 'hunter2'
ITEMS = ['eggs', 'milk', 'bread', 'coffee']

_sessions: dict[str, str] = {}

_STYLE = '<style>body{font:16px system-ui;margin:40px;max-width:640px}button{margin:4px}</style>'

_SIGN_IN_WALL = f"""<!doctype html><title>Shop</title>{_STYLE}
<h1>Sign in to continue</h1><p><a href="/login">Sign in</a> to see prices and your cart.</p>"""

_LOGIN = f"""<!doctype html><title>Sign in</title>{_STYLE}
<h1>Sign in</h1>
<form method="post" action="/login">
  <p><input id="username" name="username" placeholder="username"></p>
  <p><input id="password" name="password" type="password" placeholder="password (hunter2)"></p>
  <p><button type="submit">Sign in</button></p>
</form>"""

_CART_JS = """<script>
const cart = () => JSON.parse(localStorage.getItem('cart') || '[]');
function add(item) { localStorage.setItem('cart', JSON.stringify([...cart(), item])); render(); }
function render() { document.getElementById('cart').textContent = cart().join(', ') || 'empty'; }
const views = Number(sessionStorage.getItem('shop_views') || 0) + 1;
sessionStorage.setItem('shop_views', String(views));
window.addEventListener('DOMContentLoaded', () => {
  render();
  document.getElementById('views').textContent = String(views);
});
</script>"""


def _shop(user: str) -> str:
    buttons = ''.join(f'<button id="add-{i}" onclick="add(\'{i}\')">Add {i}</button>' for i in ITEMS)
    return f"""<!doctype html><title>Shop</title>{_STYLE}{_CART_JS}
<h1>Shop</h1><p>Signed in as {user}.</p><p>{buttons}</p>
<p>In cart: <span id="cart"></span></p><p>Shop views in this tab: <span id="views"></span></p>
<p><a href="/cart">Go to cart</a></p>"""


def _cart(user: str) -> str:
    return f"""<!doctype html><title>Cart</title>{_STYLE}{_CART_JS}
<h1>Cart for {user}</h1><p>In cart: <span id="cart"></span></p>
<p>Shop views in this tab: <span id="views"></span></p>"""


class _Handler(BaseHTTPRequestHandler):
    def _user(self) -> str | None:
        cookie = SimpleCookie(self.headers.get('Cookie', ''))
        sid = cookie.get('sid')
        return _sessions.get(sid.value) if sid else None

    def _send(self, body: str, *, status: int = 200, headers: dict[str, str] | None = None) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        user = self._user()
        if self.path == '/login':
            self._send(_LOGIN)
        elif self.path in ('/', '/shop'):
            self._send(_shop(user) if user else _SIGN_IN_WALL)
        elif self.path == '/cart':
            self._send(_cart(user) if user else _SIGN_IN_WALL)
        else:
            self._send('not found', status=404)

    def do_POST(self) -> None:
        length = int(self.headers.get('Content-Length', 0))
        form = parse_qs(self.rfile.read(length).decode())
        if self.path == '/login' and form.get('password', [''])[0] == PASSWORD:
            sid = secrets.token_urlsafe(16)
            _sessions[sid] = form.get('username', ['user'])[0] or 'user'
            self._send(
                '',
                status=303,
                headers={'Location': '/shop', 'Set-Cookie': f'sid={sid}; HttpOnly; Path=/; SameSite=Lax'},
            )
        else:
            self._send(_LOGIN.replace('<h1>Sign in</h1>', '<h1>Sign in</h1><p>Wrong password.</p>'), status=401)

    def log_message(self, format: str, *args: object) -> None:
        pass


def start(port: int) -> str:
    """Serve the shop on a daemon thread and return its origin."""
    server = ThreadingHTTPServer(('127.0.0.1', port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f'http://127.0.0.1:{port}'
