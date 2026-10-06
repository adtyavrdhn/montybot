# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright", "aiohttp"]
# ///
"""Hand a headless Chromium to a human, then save what they signed in to.

Run: uv run takeover.py URL --identity alice
Open the printed link, sign in, press "Return control". The browser state is saved to identities/alice.json.

1. Start headless Chromium with the identity's saved state, if there is one, and open URL.
2. Serve a one-time link. The page shows a CDP screencast of the tab and sends mouse and keys back.
3. "Return control" saves `storage_state` (cookies incl. HttpOnly, localStorage, IndexedDB) and stops.
"""

import argparse
import asyncio
import json
import secrets
from pathlib import Path

from aiohttp import WSMsgType, web
from playwright.async_api import BrowserContext, Page, async_playwright

VIEWPORT = {'width': 1280, 'height': 800}
IDENTITIES = Path(__file__).parent / 'identities'
SPECIAL_KEYS = {
    'Enter', 'Backspace', 'Tab', 'Escape', 'Delete', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End',
}

PAGE_HTML = """<!doctype html><meta charset=utf-8><title>Take over</title>
<style>body{margin:0;background:#111;color:#eee;font:14px system-ui}header{padding:8px;display:flex;gap:12px;align-items:center}
canvas{display:block;outline:none;cursor:default}button{font:inherit;padding:6px 12px}</style>
<header><span id=status>Connecting</span><button id=done>Return control</button></header>
<canvas id=c width=1280 height=800 tabindex=0></canvas>
<script>
const c = document.getElementById('c'), g = c.getContext('2d'), status = document.getElementById('status');
const ws = new WebSocket(`ws://${location.host}/ws${location.search}`);
ws.onopen = () => { status.textContent = 'You are in control. The agent is paused.'; c.focus(); };
ws.onclose = () => { status.textContent = 'Control returned. You can close this tab.'; };
ws.onmessage = e => { const img = new Image(); img.onload = () => g.drawImage(img, 0, 0); img.src = 'data:image/jpeg;base64,' + e.data; };
const send = m => ws.readyState === 1 && ws.send(JSON.stringify(m));
const pos = e => { const r = c.getBoundingClientRect(); return {x: (e.clientX - r.left) * c.width / r.width, y: (e.clientY - r.top) * c.height / r.height}; };
c.onmousedown = e => { c.focus(); send({type: 'click', ...pos(e)}); };
c.onwheel = e => { e.preventDefault(); send({type: 'wheel', dy: e.deltaY}); };
c.onkeydown = e => { e.preventDefault(); send({type: 'key', key: e.key}); };
document.getElementById('done').onclick = () => send({type: 'done'});
</script>"""


class Takeover:
    def __init__(self, context: BrowserContext, page: Page, identity_path: Path, token: str):
        self.context, self.page, self.identity_path, self.token = context, page, identity_path, token
        self.done = asyncio.Event()
        context.on('page', self._follow)  # a sign-in popup becomes the page being shown

    def _follow(self, page: Page) -> None:
        self.page = page

    async def index(self, request: web.Request) -> web.Response:
        if request.query.get('token') != self.token:
            raise web.HTTPForbidden()
        return web.Response(text=PAGE_HTML, content_type='text/html')

    async def ws(self, request: web.Request) -> web.WebSocketResponse:
        if request.query.get('token') != self.token:
            raise web.HTTPForbidden()
        self.token = secrets.token_urlsafe(16)  # one-time: the link stops working once used
        sock = web.WebSocketResponse()
        await sock.prepare(request)
        cdp = await self.context.new_cdp_session(self.page)

        async def on_frame(frame: dict) -> None:
            await sock.send_str(frame['data'])
            await cdp.send('Page.screencastFrameAck', {'sessionId': frame['sessionId']})

        cdp.on('Page.screencastFrame', lambda f: asyncio.ensure_future(on_frame(f)))
        await cdp.send('Page.startScreencast', {'format': 'jpeg', 'quality': 70, **_max(VIEWPORT)})
        async for msg in sock:
            if msg.type != WSMsgType.TEXT:
                break
            event = json.loads(msg.data)
            if event['type'] == 'done':
                break
            await self._input(event)
        await cdp.send('Page.stopScreencast')
        await self.save()
        await sock.close()
        self.done.set()
        return sock

    async def _input(self, event: dict) -> None:
        if event['type'] == 'click':
            await self.page.mouse.click(event['x'], event['y'])
        elif event['type'] == 'wheel':
            await self.page.mouse.wheel(0, event['dy'])
        elif event['type'] == 'key':
            key = event['key']
            if key in SPECIAL_KEYS:
                await self.page.keyboard.press(key)
            elif len(key) == 1:
                await self.page.keyboard.insert_text(key)

    async def save(self) -> None:
        self.identity_path.parent.mkdir(exist_ok=True)
        state = await self.context.storage_state(indexed_db=True)
        self.identity_path.write_text(json.dumps(state, indent=2))


def _max(viewport: dict) -> dict:
    return {'maxWidth': viewport['width'], 'maxHeight': viewport['height']}


async def run(url: str, identity: str, port: int, ready: asyncio.Future | None = None) -> Path:
    identity_path = IDENTITIES / f'{identity}.json'
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport=VIEWPORT, storage_state=str(identity_path) if identity_path.exists() else None
        )
        page = await context.new_page()
        await page.goto(url)
        takeover = Takeover(context, page, identity_path, secrets.token_urlsafe(16))
        app = web.Application()
        app.router.add_get('/', takeover.index)
        app.router.add_get('/ws', takeover.ws)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, '127.0.0.1', port).start()
        link = f'http://127.0.0.1:{port}/?token={takeover.token}'
        print(f'Take over: {link}', flush=True)
        if ready is not None:
            ready.set_result(link)
        await takeover.done.wait()
        await runner.cleanup()
        await browser.close()
    return identity_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('url')
    parser.add_argument('--identity', required=True)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    path = asyncio.run(run(args.url, args.identity, args.port))
    state = json.loads(path.read_text())
    print(f'Saved {path}: {len(state["cookies"])} cookies, {len(state["origins"])} origins with storage')


if __name__ == '__main__':
    main()
