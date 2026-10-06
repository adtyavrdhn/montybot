"""A tiny framework for the fixture sites: stdlib HTTP on a thread, routes, forms and cookies.

Each site is server-rendered HTML, so the same pages work in a real browser and in `HtmlBrowser` (the fake engine
that reads HTML). Every site serves on 127.0.0.1 with a port of its own, and records what happened to it (orders
placed, sign-ins) for tests to assert on.
"""

from __future__ import annotations

import html
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

STYLE = """<style>
body{font:16px system-ui;margin:24px;max-width:720px}button,input{font:inherit;margin:4px 0}
table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px}
</style>"""


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    form: dict[str, str]
    cookies: dict[str, str]


@dataclass
class Response:
    body: str = ''
    status: int = 200
    headers: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    content_type: str = 'text/html; charset=utf-8'
    data: bytes | None = None


Handler = Callable[[Request], Response]


def page(title: str, body: str, *, script: str = '', status: int = 200) -> Response:
    return Response(
        f'<!doctype html><html><head><meta name="viewport" content="width=device-width">'
        f'<title>{html.escape(title)}</title>{STYLE}</head><body>{body}{script}</body></html>',
        status=status,
    )


def redirect(location: str, *, cookie: str | None = None) -> Response:
    headers = [('Location', location)]
    if cookie is not None:
        headers.append(('Set-Cookie', cookie))
    return Response(status=303, headers=headers)


def esc(text: object) -> str:
    return html.escape(str(text))


class Site:
    """Routes by (method, path); a path ending in `/*` matches any path under it."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Handler] = {}
        self.url = ''
        self._server: ThreadingHTTPServer | None = None

    def route(self, method: str, path: str) -> Callable[[Handler], Handler]:
        def register(handler: Handler) -> Handler:
            self.routes[(method, path)] = handler
            return handler

        return register

    def handle(self, request: Request) -> Response:
        handler = self.routes.get((request.method, request.path))
        if handler is None:
            for (method, path), candidate in self.routes.items():
                if method == request.method and path.endswith('/*') and request.path.startswith(path[:-1]):
                    handler = candidate
                    break
        if handler is None:
            return page('Not found', '<h1>Not found</h1>', status=404)
        return handler(request)

    def start(self) -> str:
        site = self

        class _Handler(BaseHTTPRequestHandler):
            def _serve(self, method: str) -> None:
                parts = urlsplit(self.path)
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length).decode() if length else ''
                cookies = {k: m.value for k, m in SimpleCookie(self.headers.get('Cookie', '')).items()}
                request = Request(
                    method=method,
                    path=parts.path,
                    query={k: v[0] for k, v in parse_qs(parts.query).items()},
                    form={k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()},
                    cookies=cookies,
                )
                response = site.handle(request)
                data = response.data if response.data is not None else response.body.encode()
                self.send_response(response.status)
                self.send_header('Content-Type', response.content_type)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                for key, value in response.headers:
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._serve('GET')

            def do_POST(self) -> None:
                self._serve('POST')

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self._server.server_address[1]}'
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
