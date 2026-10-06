"""Local test server for the engine spike.

Serves ../servo/site/login.html at /login.html, plus two endpoints for the storage round trip:

- GET /set    sets an HttpOnly cookie `sid=spike-httponly` and returns a tiny page
- GET /check  returns the Cookie header the browser sent, as JSON

usage: python3 scripts/server.py [port]   (default 8766, binds 127.0.0.1)
"""

import json
import pathlib
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

SITE = pathlib.Path(__file__).resolve().parents[2] / 'servo' / 'site'


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE), **kwargs)

    def do_GET(self):
        if self.path.startswith('/set'):
            body = b'<!doctype html><title>set</title><p>cookie set</p>'
            self.send_response(200)
            self.send_header('Set-Cookie', 'sid=spike-httponly; Path=/; HttpOnly; SameSite=Lax')
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith('/check'):
            body = json.dumps({'cookie': self.headers.get('Cookie', '')}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def log_message(self, format, *args):
        pass


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
