"""Measure one browser engine for the Sammy engine spike.

usage (from spikes/engines, with scripts/server.py running on 127.0.0.1:8766):

  PLAYWRIGHT_BROWSERS_PATH=$PWD/bin/ms-playwright \
    uv run --no-project --with playwright==1.63.0 --with psutil \
    python scripts/measure.py --engine chromium-headless-shell --phase runs --sites example,github,login,walmart --runs 3
  ... --phase checks

Camoufox needs its own env (it pins playwright 1.62) and a HOME pointing at bin/camoufox-home; see README.

Phase `runs`: for each site and run, a fresh browser process is launched, one context and one page are opened, the
page is loaded with wait_until="load", the page is left to settle for 2 s, then title/URL/challenge markers are read
and a screenshot is taken. A background thread samples RSS of the whole browser process tree every 50 ms.
One JSON line per run goes to out/results.jsonl.

Phase `checks`: one browser process; accessibility snapshot, typing, storage_state round trip, screencast + input
injection, and multi-context isolation. Writes out/checks-<engine>.json plus snapshot text files.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import socket
import subprocess
import threading
import time
import traceback
import urllib.request

import psutil

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / 'out'
BIN = ROOT / 'bin'
LOCAL = 'http://127.0.0.1:8766'
SITES = {
    'example': 'https://example.com/',
    'github': 'https://github.com/login',
    'walmart': 'https://www.walmart.com/',
    'login': f'{LOCAL}/login.html',
    'walmart_search': 'https://www.walmart.com/search?q=milk',
}
VIEWPORT = {'width': 1280, 'height': 800}

# Directory each engine's binaries live in. Any process whose executable lives under it and that started after the
# launch counts towards the tree (this catches macOS XPC helpers that are not children of the browser process).
INSTALL_ROOTS = {
    'chromium-headless-shell': BIN / 'ms-playwright' / 'chromium_headless_shell-1243',
    'chromium-new-headless': BIN / 'ms-playwright' / 'chromium-1243',
    'firefox': BIN / 'ms-playwright' / 'firefox-1543',
    'webkit': BIN / 'ms-playwright' / 'webkit-2359',
    'lightpanda': BIN / 'lightpanda',
    'camoufox': BIN / 'camoufox-home',
}
CDP_ENGINES = {'chromium-headless-shell', 'chromium-new-headless', 'lightpanda'}


def err(e: BaseException) -> str:
    return f'{type(e).__name__}: {str(e).strip()[:600]}'


class Sampler(threading.Thread):
    """Sum RSS over the browser process tree every 50 ms; track the peak."""

    def __init__(self, install_root: pathlib.Path, baseline: set[int]):
        super().__init__(daemon=True)
        self.install_root = str(install_root.resolve())
        self.baseline = baseline
        self.t_start = time.time()
        self.pids: set[int] = set()
        self.peak = 0
        self.peak_procs = 0
        self.last = 0
        self.names: dict[int, str] = {}
        self._stop = threading.Event()

    def discover(self):
        me = psutil.Process()
        for c in me.children(recursive=True):
            try:
                # skip the macOS `footprint` tool this script itself spawns
                if c.pid not in self.baseline and c.name() != 'footprint':
                    self.pids.add(c.pid)
            except psutil.Error:
                pass
        for pr in psutil.process_iter(['pid', 'exe', 'create_time']):
            exe = pr.info.get('exe') or ''
            if exe.startswith(self.install_root) and (pr.info.get('create_time') or 0) >= self.t_start - 1:
                self.pids.add(pr.info['pid'])

    def sample(self) -> int:
        total = 0
        n = 0
        for pid in list(self.pids):
            try:
                p = psutil.Process(pid)
                total += p.memory_info().rss
                n += 1
                if pid not in self.names:
                    self.names[pid] = p.name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self.pids.discard(pid)
        self.last = total
        if total > self.peak:
            self.peak, self.peak_procs = total, n
        return total

    def run(self):
        i = 0
        while not self._stop.is_set():
            if i % 5 == 0:
                self.discover()
            self.sample()
            i += 1
            time.sleep(0.05)

    def current(self) -> int:
        self.discover()
        return self.sample()

    def stop(self):
        self._stop.set()
        self.join(timeout=2)


_UNITS = {'B': 1 / 2**20, 'KB': 1 / 1024, 'MB': 1, 'GB': 1024}


def footprint(pids) -> dict:
    """macOS `footprint` over a set of pids: dirty memory with shared pages counted once ("Summary Footprint"),
    plus the sum of each process's phys_footprint_peak (an upper bound on the simultaneous peak)."""
    import re

    pids = [str(x) for x in pids if psutil.pid_exists(x)]
    if not pids:
        return {}
    out = subprocess.run(['footprint', *pids], capture_output=True, text=True, timeout=60).stdout
    m = re.search(r'Summary Footprint: ([\d.]+) (B|KB|MB|GB)', out) or re.search(r' Footprint: ([\d.]+) (B|KB|MB|GB)', out)
    peaks = re.findall(r'phys_footprint_peak: ([\d.]+) (B|KB|MB|GB)', out)
    return {
        'footprint_mib': round(float(m.group(1)) * _UNITS[m.group(2)], 1) if m else None,
        'sum_phys_footprint_peak_mib': round(sum(float(v) * _UNITS[u] for v, u in peaks), 1) if peaks else None,
    }


def free_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def launch(p, engine: str):
    """Return (browser, cleanup, notes). Time from call to return is the launch time."""
    if engine == 'chromium-headless-shell':
        return p.chromium.launch(headless=True), lambda: None, {}
    if engine == 'chromium-new-headless':
        # Playwright docs ("Browsers" > "Chromium: new headless mode"): channel="chromium" opts into the full
        # Chromium build running Chrome's new headless mode instead of chromium-headless-shell.
        return p.chromium.launch(channel='chromium', headless=True), lambda: None, {}
    if engine == 'firefox':
        return p.firefox.launch(headless=True), lambda: None, {}
    if engine == 'webkit':
        return p.webkit.launch(headless=True), lambda: None, {}
    if engine == 'camoufox':
        from camoufox.sync_api import NewBrowser

        return NewBrowser(p, headless=True), lambda: None, {}
    if engine == 'lightpanda':
        port = free_port()
        env = dict(os.environ, LIGHTPANDA_DISABLE_TELEMETRY='true')
        log = open(ROOT / 'logs' / f'lightpanda-{port}.log', 'w')
        proc = subprocess.Popen(
            [str(BIN / 'lightpanda' / 'lightpanda-aarch64-macos'), 'serve', '--host', '127.0.0.1', '--port', str(port)],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 15
        while True:
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=0.5).read()
                break
            except Exception:
                if time.monotonic() > deadline or proc.poll() is not None:
                    raise RuntimeError(f'lightpanda did not come up (rc={proc.poll()})')
                time.sleep(0.01)
        t_ready = time.perf_counter()
        browser = p.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')

        def cleanup():
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()

        return browser, cleanup, {'cdp_ready_perf': t_ready, 'port': port}
    raise SystemExit(f'unknown engine {engine}')


def new_context(browser, engine, **kw):
    if engine == 'lightpanda' or engine == 'camoufox':
        return browser.new_context(**kw)
    return browser.new_context(viewport=VIEWPORT, **kw)


def baseline_pids() -> set[int]:
    return {c.pid for c in psutil.Process().children(recursive=True)}


def is_challenge(title: str, html: str, url: str) -> list[str]:
    hits = []
    if 'robot or human' in title.lower() or 'robot or human' in html.lower():
        hits.append('robot-or-human')
    if 'px-captcha' in html:
        hits.append('px-captcha')
    if '/blocked' in url:
        hits.append('blocked-url')
    return hits


def do_run(p, engine, site, idx, base):
    url = SITES[site]
    rec = {'engine': engine, 'site': site, 'run': idx, 'ts': time.strftime('%H:%M:%S')}
    sampler = Sampler(INSTALL_ROOTS[engine], base)
    sampler.start()
    cleanup = lambda: None
    browser = None
    try:
        t0 = time.perf_counter()
        browser, cleanup, notes = launch(p, engine)
        t_launch = time.perf_counter()
        rec['launch_s'] = round(t_launch - t0, 3)
        if 'cdp_ready_perf' in notes:
            rec['process_ready_s'] = round(notes['cdp_ready_perf'] - t0, 3)
        rec['browser_version'] = browser.version
        ctx = new_context(browser, engine)
        page = ctx.new_page()
        t1 = time.perf_counter()
        resp = page.goto(url, wait_until='load', timeout=60000)
        t2 = time.perf_counter()
        rec['load_s'] = round(t2 - t1, 3)
        rec['spawn_to_load_s'] = round(t2 - t0, 3)
        rec['status'] = resp.status if resp else None
        page.wait_for_timeout(2000)
        rec['title'] = page.title()
        rec['final_url'] = page.url
        html = page.content()
        rec['html_bytes'] = len(html)
        rec['challenge'] = is_challenge(rec['title'], html, page.url)
        try:
            rec['user_agent'] = page.evaluate('navigator.userAgent')
            rec['webdriver'] = page.evaluate('navigator.webdriver')
        except Exception as e:
            rec['user_agent_error'] = err(e)
        sampler.current()
        rec.update(footprint(sampler.pids))
        shot = OUT / f'{engine}-{site}-{idx}.png'
        try:
            page.screenshot(path=str(shot), timeout=15000)
            rec['screenshot'] = shot.name
        except Exception as e:
            rec['screenshot_error'] = err(e)
    except Exception as e:
        rec['error'] = err(e)
    finally:
        sampler.current()
        sampler.stop()
        rec['peak_rss_mib'] = round(sampler.peak / 2**20, 1)
        rec['peak_procs'] = sampler.peak_procs
        rec['proc_names'] = sorted(set(sampler.names.values()))
        try:
            if browser:
                browser.close()
        except Exception as e:
            rec['close_error'] = err(e)
        cleanup()
    print(json.dumps(rec), flush=True)
    with open(OUT / 'results.jsonl', 'a') as f:
        f.write(json.dumps(rec) + '\n')


def step(res, name, fn):
    try:
        res[name] = fn()
    except Exception as e:
        res[name] = {'error': err(e)}
        traceback.print_exc()


def ax_summary(cdp):
    tree = cdp.send('Accessibility.getFullAXTree')
    out = []
    for n in tree.get('nodes', []):
        role = (n.get('role') or {}).get('value')
        if role in ('textbox', 'button', 'link', 'heading'):
            out.append(
                {
                    'role': role,
                    'name': (n.get('name') or {}).get('value'),
                    'value': (n.get('value') or {}).get('value'),
                }
            )
    return {'nodes_total': len(tree.get('nodes', [])), 'interesting': out}


def checks(p, engine, base):
    res: dict = {'engine': engine}
    sampler = Sampler(INSTALL_ROOTS[engine], base)
    sampler.start()
    browser, cleanup, _ = launch(p, engine)
    res['browser_version'] = browser.version
    try:
        ctx = new_context(browser, engine)
        page = ctx.new_page()
        page.goto(SITES['login'], wait_until='load')

        # 1. Accessibility snapshot + typing
        def a11y():
            r = {}
            snap = page.locator('body').aria_snapshot()
            (OUT / f'{engine}-aria-before.txt').write_text(snap)
            page.locator('#user').click()
            page.keyboard.type('alice')
            snap2 = page.locator('body').aria_snapshot()
            (OUT / f'{engine}-aria-after.txt').write_text(snap2)
            r['textbox_named'] = 'textbox "Username"' in snap
            r['button_named'] = 'button "Log in"' in snap
            r['value_after_typing_in_snapshot'] = 'alice' in snap2
            r['dom_value'] = page.evaluate("document.getElementById('user').value")
            try:
                ai = page.aria_snapshot(mode='ai')
                (OUT / f'{engine}-aria-ai.txt').write_text(ai)
                r['ai_mode_has_refs'] = '[ref=' in ai
            except Exception as e:
                r['ai_mode_error'] = err(e)
            if engine in CDP_ENGINES:
                try:
                    cdp = ctx.new_cdp_session(page)
                    r['cdp_ax_tree'] = ax_summary(cdp)
                except Exception as e:
                    r['cdp_ax_tree'] = {'error': err(e)}
            return r

        step(res, 'a11y', a11y)

        # 2. storage_state round trip
        def storage():
            r = {}
            page.goto(f'{LOCAL}/set', wait_until='load')
            page.evaluate("localStorage.setItem('spike', 'ls-value')")
            r['document_cookie_sees_httponly'] = 'sid=' in page.evaluate('document.cookie')
            if engine in CDP_ENGINES:
                cdp = ctx.new_cdp_session(page)
                for method in ('Storage.getCookies', 'Network.getAllCookies', 'Network.getCookies'):
                    try:
                        cs = cdp.send(method)
                        r[method] = [
                            {k: c.get(k) for k in ('name', 'domain', 'httpOnly')} for c in cs.get('cookies', [])
                        ]
                    except Exception as e:
                        r[method] = {'error': err(e)}
            state = ctx.storage_state()
            (OUT / f'{engine}-storage-state.json').write_text(json.dumps(state, indent=1))
            ck = [c for c in state.get('cookies', []) if c['name'] == 'sid']
            r['exported_cookie'] = ck[0] if ck else None
            r['exported_origins'] = [o['origin'] for o in state.get('origins', [])]
            r['exported_localstorage'] = state.get('origins', [])
            ctx2 = new_context(browser, engine, storage_state=state)
            p2 = ctx2.new_page()
            p2.goto(f'{LOCAL}/check', wait_until='load')
            r['imported_cookie_sent_to_server'] = 'sid=spike-httponly' in p2.content()
            p2.goto(SITES['login'], wait_until='load')
            r['imported_localstorage'] = p2.evaluate("localStorage.getItem('spike')")
            ctx2.close()
            return r

        step(res, 'storage', storage)

        # 3a. Playwright screencast API (Playwright >= 1.59, cross-engine) + input injection via page.mouse/keyboard
        def pw_screencast():
            r = {}
            page.goto(SITES['login'], wait_until='load')
            frames = []
            t0 = time.perf_counter()

            def on_frame(f):
                frames.append((time.perf_counter() - t0, len(f['data'])))
                if len(frames) == 1:
                    (OUT / f'{engine}-screencast-frame.jpg').write_bytes(f['data'])

            page.screencast.start(on_frame=on_frame, size={'width': 800, 'height': 500})
            box = page.locator('#user').bounding_box()
            page.mouse.click(box['x'] + 5, box['y'] + 5)
            for ch in 'takeover-typed':
                page.keyboard.type(ch)
                page.wait_for_timeout(150)
            btn = page.locator('#go').bounding_box()
            page.mouse.click(btn['x'] + 5, btn['y'] + 5)
            page.wait_for_timeout(500)
            page.screencast.stop()
            dur = time.perf_counter() - t0
            r['frames'] = len(frames)
            r['duration_s'] = round(dur, 2)
            r['first_frame_s'] = round(frames[0][0], 3) if frames else None
            r['avg_frame_kb'] = round(sum(b for _, b in frames) / len(frames) / 1024, 1) if frames else None
            r['typed_value'] = page.evaluate("document.getElementById('user').value")
            r['status_after_click'] = page.evaluate("document.getElementById('status').textContent")
            return r

        step(res, 'pw_screencast', pw_screencast)

        # 3b. Raw CDP screencast + CDP input injection (what a takeover relay would speak directly)
        if engine in CDP_ENGINES:

            def cdp_screencast():
                r = {}
                page.goto(SITES['login'], wait_until='load')
                cdp = ctx.new_cdp_session(page)
                frames, pending = [], []
                t0 = time.perf_counter()

                def on_frame(params):
                    frames.append((time.perf_counter() - t0, len(params['data'])))
                    pending.append(params['sessionId'])
                    if len(frames) == 1:
                        (OUT / f'{engine}-cdp-frame.jpg').write_bytes(base64.b64decode(params['data']))

                cdp.on('Page.screencastFrame', on_frame)
                cdp.send('Page.enable')
                cdp.send('Page.startScreencast', {'format': 'jpeg', 'quality': 60, 'maxWidth': 800, 'maxHeight': 500})
                box = page.locator('#user').bounding_box()
                x, y = box['x'] + 5, box['y'] + 5
                for t in ('mousePressed', 'mouseReleased'):
                    cdp.send('Input.dispatchMouseEvent', {'type': t, 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})
                for ch in 'cdp-typed':
                    cdp.send('Input.dispatchKeyEvent', {'type': 'keyDown', 'text': ch, 'key': ch})
                    cdp.send('Input.dispatchKeyEvent', {'type': 'keyUp', 'key': ch})
                    page.wait_for_timeout(100)
                    while pending:
                        cdp.send('Page.screencastFrameAck', {'sessionId': pending.pop()})
                for _ in range(10):
                    page.wait_for_timeout(100)
                    while pending:
                        cdp.send('Page.screencastFrameAck', {'sessionId': pending.pop()})
                cdp.send('Page.stopScreencast')
                r['frames'] = len(frames)
                r['first_frame_s'] = round(frames[0][0], 3) if frames else None
                r['typed_value'] = page.evaluate("document.getElementById('user').value")
                return r

            step(res, 'cdp_screencast', cdp_screencast)

        # 4. Several isolated contexts in one process
        def multi():
            r = {}
            rss0 = sampler.current()
            fp0 = footprint(sampler.pids).get('footprint_mib')
            ctxs = []
            for i in range(5):
                c = new_context(browser, engine)
                pg = c.new_page()
                pg.goto(SITES['login'], wait_until='load')
                ctxs.append((c, pg))
            ctxs[0][1].goto(f'{LOCAL}/set', wait_until='load')
            ctxs[1][1].goto(f'{LOCAL}/check', wait_until='load')
            leaked = 'sid=' in ctxs[1][1].content()
            time.sleep(0.5)
            rss5 = sampler.current()
            fp5 = footprint(sampler.pids).get('footprint_mib')
            r['footprint_before_mib'] = fp0
            r['footprint_after_5_contexts_mib'] = fp5
            if fp0 is not None and fp5 is not None:
                r['per_context_footprint_mib'] = round((fp5 - fp0) / 5, 1)
            r['contexts_opened'] = len(ctxs)
            r['cookie_leaked_between_contexts'] = leaked
            r['rss_before_mib'] = round(rss0 / 2**20, 1)
            r['rss_after_5_contexts_mib'] = round(rss5 / 2**20, 1)
            r['per_context_mib'] = round((rss5 - rss0) / 5 / 2**20, 1)
            for c, _ in ctxs:
                c.close()
            return r

        step(res, 'multi_context', multi)
    finally:
        sampler.stop()
        res['peak_rss_mib_during_checks'] = round(sampler.peak / 2**20, 1)
        try:
            browser.close()
        except Exception as e:
            res['close_error'] = err(e)
        cleanup()
    (OUT / f'checks-{engine}.json').write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res, indent=1, default=str))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--engine', required=True)
    ap.add_argument('--phase', choices=['runs', 'checks'], required=True)
    ap.add_argument('--sites', default='example,github,login,walmart')
    ap.add_argument('--runs', type=int, default=3)
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    (ROOT / 'logs').mkdir(exist_ok=True)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        base = baseline_pids()
        if a.phase == 'runs':
            # warm-up launch, not recorded: the first launch after install pays macOS first-run checks
            b, c, _ = launch(p, a.engine)
            b.close()
            c()
            for site in a.sites.split(','):
                for i in range(1, a.runs + 1):
                    do_run(p, a.engine, site, i, base)
        else:
            checks(p, a.engine, base)


if __name__ == '__main__':
    main()
