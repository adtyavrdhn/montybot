"""Try the E2B Desktop fallback for real, and measure it. Not collected by pytest.

    uv run python tests/fallback/e2b_desktop_trial.py [--paused-for SECONDS] [--show-stream-url]

Needs `E2B_API_KEY`. Without it, it prints a report with `"run": false` and exits 0.

What it does, in two short-lived desktops it always kills:

1. Starts desktop A and Chrome on it (start time), and serves a tiny shop inside the VM on 127.0.0.1.
2. Signs in with xdotool typing and clicks (the session cookie is HttpOnly), adds to a localStorage cart, and takes
   a viewport screenshot (CDP) and a whole-screen screenshot (SDK).
3. Gets the noVNC link for a human. Only its host is printed, unless `--show-stream-url`: the link holds a password.
4. Exports the state, pauses (pause time), waits, resumes (resume time), and checks the tab, an in-memory click
   counter, the cookies and the storage are all still there.
5. Releases the state, starts desktop B (second start time), opens it there, and checks the shop sees the user and
   the cart: the session moved between machines.

Prints one JSON report, and writes the screenshots to a temporary folder it names.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from montybot.browser.contract import Click, Navigate, Selector, Type
from montybot.browser.state import BLANK_URL, BrowserState
from montybot.fallback.e2b_desktop import (
    DESKTOP_MEMORY_MIB,
    DESKTOP_VCPUS,
    E2BDesktop,
    E2BDesktopBrowser,
    cost_per_hour,
)

SITE_PORT = 8765  # inside the VM only; nothing on this machine binds it
SHOP = f'http://127.0.0.1:{SITE_PORT}'

# A stateless shop, so a session made in one VM is valid in another: the HttpOnly `sid` cookie is signed, not stored.
SITE = r'''
import hashlib, hmac, html, sys
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

KEY = b'montybot-trial'
PAGE = '<!doctype html><title>%s</title><body style="font:20px sans-serif;margin:40px">%s</body>'
LOGIN = ('<form method=post action=/login><p><input id=user name=user placeholder=user></p>'
         '<p><input id=password name=password type=password placeholder=password></p>'
         '<p><button id=submit>Sign in</button></p></form>')
SHOP = """<p>Signed in as %s</p><p><button id=add>Add eggs</button></p>
<p>Cart: <span id=cart></span></p><p>Clicks on this page: <span id=clicks>0</span></p>
<script>
let clicks = 0;
const cart = () => JSON.parse(localStorage.getItem('cart') || '[]');
const show = () => {
  document.getElementById('cart').textContent = cart().join(', ') || 'empty';
  document.getElementById('clicks').textContent = clicks;
};
document.getElementById('add').onclick = () => {
  clicks++; localStorage.setItem('cart', JSON.stringify([...cart(), 'eggs'])); show();
};
show();
</script>"""

def sign(user):
    return user + '.' + hmac.new(KEY, user.encode(), hashlib.sha256).hexdigest()[:16]

class Handler(BaseHTTPRequestHandler):
    def user(self):
        sid = SimpleCookie(self.headers.get('Cookie', '')).get('sid')
        if sid and '.' in sid.value:
            user = sid.value.rsplit('.', 1)[0]
            return user if hmac.compare_digest(sign(user), sid.value) else None
        return None

    def reply(self, status, body, headers=()):
        data = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        user = self.user()
        if user:
            self.reply(200, PAGE % ('Shop', SHOP % html.escape(user)))
        else:
            self.reply(200, PAGE % ('Sign in', LOGIN))

    def do_POST(self):
        form = parse_qs(self.rfile.read(int(self.headers.get('Content-Length', 0))).decode())
        if form.get('password') == ['hunter2']:
            cookie = 'sid=%s; HttpOnly; Path=/; SameSite=Lax' % sign(form.get('user', ['user'])[0])
            self.reply(303, '', [('Location', '/shop'), ('Set-Cookie', cookie)])
        else:
            self.reply(401, PAGE % ('Sign in', '<p>Wrong password</p>' + LOGIN))

    def log_message(self, *args):
        pass

ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
'''


def empty_report() -> dict[str, Any]:
    return {
        'run': False,
        'why_not': None,
        'start_seconds': None,
        'second_start_seconds': None,
        'pause_seconds': None,
        'resume_seconds': None,
        'paused_for_seconds': None,
        'tab_survived_pause': None,
        'page_memory_survived_pause': None,
        'cookies_survived_pause': None,
        'local_storage_survived_pause': None,
        'http_only_cookie_exported': None,
        'state_moved_to_second_desktop': None,
        'vcpus': None,
        'memory_mib': None,
        'usd_per_hour_running': None,
        'usd_per_hour_template_default': round(cost_per_hour(vcpus=DESKTOP_VCPUS, memory_mib=DESKTOP_MEMORY_MIB), 4),
        'stream_url_host': None,
        'screenshots': None,
    }


async def serve_shop(browser: E2BDesktopBrowser) -> None:
    desktop = browser.desktop
    assert desktop is not None
    await asyncio.to_thread(desktop.write_file, '/tmp/montybot/trial_site.py', SITE)
    await asyncio.to_thread(desktop.launch, f'python3 /tmp/montybot/trial_site.py {SITE_PORT} >/dev/null 2>&1')
    await asyncio.to_thread(
        desktop.run,
        f'for i in $(seq 50); do curl -sf {SHOP}/ >/dev/null && exit 0; sleep 0.1; done; exit 1',
        timeout=30,
    )


async def text_of(browser: E2BDesktopBrowser) -> str:
    return (await browser.snapshot()).text


async def trial(report: dict[str, Any], *, paused_for: float, show_stream_url: bool) -> None:
    desktops: list[E2BDesktop] = []

    def start() -> E2BDesktop:
        desktop = E2BDesktop.start()
        desktops.append(desktop)
        return desktop

    first = E2BDesktopBrowser(start_desktop=start)
    second = E2BDesktopBrowser(start_desktop=start)
    try:
        began = time.monotonic()
        await first.open(None)
        report['start_seconds'] = round(time.monotonic() - began, 2)
        info = await asyncio.to_thread(desktops[0].sandbox.get_info)
        report['vcpus'], report['memory_mib'] = info.cpu_count, info.memory_mb
        report['usd_per_hour_running'] = round(cost_per_hour(vcpus=info.cpu_count, memory_mib=info.memory_mb), 4)

        await serve_shop(first)
        stream_url = await first.stream_url()
        report['stream_url_host'] = urlsplit(stream_url).hostname
        if show_stream_url:
            print(f'noVNC (holds a password): {stream_url}')

        await first.act(Navigate(url=f'{SHOP}/shop'))
        await first.act(Type(text='mike', target=Selector(css='#user')))
        await first.act(Type(text='hunter2', target=Selector(css='#password')))
        await first.act(Click(target=Selector(css='#submit')))
        assert 'Signed in as mike' in await text_of(first), 'sign-in through xdotool failed'
        await first.act(Click(target=Selector(css='#add')))
        await first.act(Click(target=Selector(css='#add')))
        assert 'Clicks on this page: 2' in await text_of(first)

        folder = Path(tempfile.mkdtemp(prefix='montybot-e2b-trial-'))
        viewport = await first.screenshot()
        whole = await first.desktop_screenshot()
        await asyncio.to_thread((folder / 'viewport.png').write_bytes, viewport.png)
        await asyncio.to_thread((folder / 'desktop.png').write_bytes, whole)
        report['screenshots'] = {
            'folder': str(folder),
            'viewport_css_pixels': [viewport.width, viewport.height],
            'desktop_png_bytes': len(whole),
        }

        before = await first.export()
        report['http_only_cookie_exported'] = any(c.name == 'sid' and c.http_only for c in before.cookies)

        began = time.monotonic()
        await first.pause()
        report['pause_seconds'] = round(time.monotonic() - began, 2)
        await asyncio.sleep(paused_for)
        report['paused_for_seconds'] = paused_for
        began = time.monotonic()
        await first.resume()
        report['resume_seconds'] = round(time.monotonic() - began, 2)

        after_text = await text_of(first)
        after = await first.export()
        report['tab_survived_pause'] = after.url == before.url
        report['page_memory_survived_pause'] = 'Clicks on this page: 2' in after_text
        report['cookies_survived_pause'] = sorted(map(repr, after.cookies)) == sorted(map(repr, before.cookies))
        report['local_storage_survived_pause'] = after.local_storage == before.local_storage

        state: BrowserState = await first.release()
        began = time.monotonic()
        await second.open(replace(state, url=BLANK_URL))  # the shop is not up in B yet; cookies and storage are
        report['second_start_seconds'] = round(time.monotonic() - began, 2)
        await serve_shop(second)
        await second.act(Navigate(url=f'{SHOP}/shop'))
        moved = await text_of(second)
        report['state_moved_to_second_desktop'] = 'Signed in as mike' in moved and 'Cart: eggs, eggs' in moved
        report['run'] = True
    finally:
        # close() kills the sandbox, paused or not, and logs if it cannot.
        await first.close()
        await second.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--paused-for', type=float, default=10, help='seconds to stay paused (default 10)')
    parser.add_argument('--show-stream-url', action='store_true', help='print the noVNC link, password included')
    args = parser.parse_args()
    report = empty_report()
    if not os.environ.get('E2B_API_KEY'):
        report['why_not'] = 'E2B_API_KEY is not set'
        print('E2B_API_KEY is not set: skipped, nothing was measured.')
        print(json.dumps(report, indent=2))
        return
    try:
        asyncio.run(trial(report, paused_for=args.paused_for, show_stream_url=args.show_stream_url))
    finally:
        print(json.dumps(report, indent=2))  # what was measured before any failure


if __name__ == '__main__':
    main()
