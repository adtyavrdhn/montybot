"""A stand-in for the web `web_search` and `web_fetch` reach (`sammy.web`), on 127.0.0.1 with a port of its own: a
pharmacy's page, Tavily's search API (`POST /search`, behind a bearer key) whose one result is that page, and pages
that redirect, never end, answer slowly or should never be reached. It records every path asked for."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from sites.integrations import Served
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

SEARCH_KEY = 'tvly-test-key'
PHARMACY = """<!doctype html><html><head><title>Corner Pharmacy</title><script>var tracking = 1;</script></head>
<body><h1>Corner Pharmacy</h1><p>Opening hours</p>
<table><tr><th>Day</th><th>Hours</th></tr><tr><td>Today</td><td>Closes at 6pm</td></tr></table>
<a href="/prescriptions">Prescriptions</a></body></html>"""
CHUNK = b'a' * 65536


class FakeWeb(Served):
    def __init__(self) -> None:
        self.paths: list[str] = []
        self.searches: list[dict[str, object]] = []
        self.streamed = 0
        """How many bytes `/endless` sent before the reader stopped."""
        routes = [
            Route('/pharmacy', self.pharmacy),
            Route('/search', self.search, methods=['POST']),
            Route('/redirect', self.redirect),
            Route('/loop', self.loop),
            Route('/endless', self.endless),
            Route('/slow', self.slow),
            Route('/secret', self.secret),
            Route('/leaflet.pdf', self.pdf),
        ]
        super().__init__(Starlette(routes=routes))

    def _seen(self, request: Request) -> None:
        self.paths.append(request.url.path)

    async def pharmacy(self, request: Request) -> Response:
        self._seen(request)
        return HTMLResponse(PHARMACY)

    async def search(self, request: Request) -> Response:
        self._seen(request)
        if request.headers.get('authorization') != f'Bearer {SEARCH_KEY}':
            return JSONResponse({'detail': 'Unauthorized'}, status_code=401)
        body = await request.json()
        self.searches.append(body)
        result = {'title': 'Corner Pharmacy', 'url': f'{self.url}/pharmacy', 'content': 'Opening hours', 'score': 0.9}
        return JSONResponse({'query': body['query'], 'results': [result] * 20, 'response_time': 0.1})

    async def redirect(self, request: Request) -> Response:
        self._seen(request)
        return RedirectResponse(request.query_params['to'], status_code=302)

    async def loop(self, request: Request) -> Response:
        self._seen(request)
        return RedirectResponse('/loop', status_code=302)

    async def endless(self, request: Request) -> Response:
        self._seen(request)

        async def body() -> AsyncIterator[bytes]:
            for _ in range(2000):  # 125 MB, unless the reader goes away first
                if await request.is_disconnected():
                    return
                self.streamed += len(CHUNK)
                yield CHUNK

        return StreamingResponse(body(), media_type='text/plain')

    async def slow(self, request: Request) -> Response:
        self._seen(request)
        await asyncio.sleep(3)  # longer than tests wait; a stop waits for it
        return HTMLResponse('<p>Too late</p>')

    async def secret(self, request: Request) -> Response:
        self._seen(request)
        return HTMLResponse('<p>The internal admin page</p>')

    async def pdf(self, request: Request) -> Response:
        self._seen(request)
        return Response(b'%PDF-1.4', media_type='application/pdf')
