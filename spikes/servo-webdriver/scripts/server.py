"""Local test server for the Servo WebDriver spike (based on ../engines/scripts/server.py).

Serves ../servo/site/login.html at /login.html, plus:

- GET /set     sets an HttpOnly cookie `sid=spike-httponly` and returns a tiny page (same as the engines spike)
- GET /setall  sets `sid` (HttpOnly, host-only), `pref` (Domain=<parent domain>) and `plain` (host-only)
- GET /check   returns the Cookie header the browser sent, as JSON
- GET /events  a page that logs pointer/mouse/key events into `window.log` (press-and-hold test)

usage: python3 scripts/server.py [port]   (default 8767, binds 127.0.0.1)
Use with servoshell --host-file=scripts/hosts.txt to get www.shop.test / api.shop.test on 127.0.0.1.
"""

import json
import pathlib
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

SITE = pathlib.Path(__file__).resolve().parents[2] / 'servo' / 'site'

EVENTS = """<!doctype html><title>events</title>
<style>body{margin:0;font:14px sans-serif} #hold{position:absolute;left:100px;top:100px;width:200px;height:80px;
background:#c33;color:#fff} #t{position:absolute;left:100px;top:250px;width:300px}</style>
<div id=hold>press and hold</div><input id=t>
<script>
window.log = []; let down = 0;
const hold = document.getElementById('hold');
for (const ev of ['pointerdown', 'pointerup', 'mousedown', 'mouseup', 'click', 'pointermove']) hold.addEventListener(ev, e => {
  if (ev === 'pointerdown') down = performance.now();
  log.push({type: ev, x: e.clientX, y: e.clientY, held: ev === 'pointerup' ? Math.round(performance.now() - down) : undefined,
            trusted: e.isTrusted, buttons: e.buttons});
});
const t = document.getElementById('t');
t.addEventListener('keydown', e => log.push({type: 'keydown', key: e.key, trusted: e.isTrusted}));
</script>"""


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE), **kwargs)

    def _send(self, body: bytes, ctype: str, cookies: list[str] = ()):
        self.send_response(200)
        for c in cookies:
            self.send_header('Set-Cookie', c)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith('/setall'):
            host = (self.headers.get('Host') or '').split(':')[0]
            parent = '.'.join(host.split('.')[-2:])
            self._send(
                b'<!doctype html><title>setall</title><p>cookies set</p>',
                'text/html',
                [
                    'sid=srv-httponly; Path=/; HttpOnly; SameSite=Lax',
                    f'pref=dark; Path=/; Domain={parent}; Max-Age=3600',
                    'plain=1; Path=/',
                ],
            )
        elif self.path.startswith('/set'):
            self._send(
                b'<!doctype html><title>set</title><p>cookie set</p>',
                'text/html',
                ['sid=spike-httponly; Path=/; HttpOnly; SameSite=Lax'],
            )
        elif self.path.startswith('/check'):
            self._send(json.dumps({'cookie': self.headers.get('Cookie', '')}).encode(), 'application/json')
        elif self.path.startswith('/events'):
            self._send(EVENTS.encode(), 'text/html')
        else:
            super().do_GET()

    def log_message(self, format, *args):
        pass


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8767
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
