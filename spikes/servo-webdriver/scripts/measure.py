"""Timed site runs for servoshell over WebDriver, mirroring ../engines/scripts/measure.py --phase runs.

Every run spawns a fresh servoshell (headless, --webdriver, --temporary-storage), creates a session, navigates with
WebDriver Navigate To (pageLoadStrategy normal = blocks until the load event, pageLoad timeout 60 s), waits 2 s, then
reads title/URL/source, runs the DOM snapshot, saves a screenshot and runs macOS `footprint` on the process tree.

usage: uv run --no-project --with psutil python scripts/measure.py SITE UA [RUN_LABEL] [--build nightly|v070] [--no-prefs]
  SITE in example | github | walmart | walmart-search ; UA in chrome | firefox | servo
Appends one JSON line per run to out/results.jsonl.
"""

from __future__ import annotations

import json
import sys
import time

from wd import SPIKE, RssSampler, Servo, footprint, tree, write_png

SITES = {
    'example': 'https://example.com/',
    'github': 'https://github.com/login',
    'walmart': 'https://www.walmart.com/',
    'walmart-search': 'https://www.walmart.com/search?q=milk',
}
SNAP = (SPIKE / 'scripts/snapshot.js').read_text()

FINGERPRINT_JS = """return {
  ua: navigator.userAgent, webdriver: navigator.webdriver, vendor: navigator.vendor, platform: navigator.platform,
  languages: navigator.languages, plugins: navigator.plugins ? navigator.plugins.length : null,
  userAgentData: typeof navigator.userAgentData, chrome: typeof window.chrome,
  subtle: typeof (window.crypto && crypto.subtle), io: typeof IntersectionObserver, ro: typeof ResizeObserver,
  webgl: (() => { try { return !!document.createElement('canvas').getContext('webgl'); } catch (e) { return 'err ' + e; } })(),
  hw: navigator.hardwareConcurrency, mem: navigator.deviceMemory, screen: [screen.width, screen.height, devicePixelRatio],
}"""


def is_challenge(title: str, html: str, url: str) -> list[str]:
    hits = []
    if 'robot or human' in title.lower() or 'robot or human' in html.lower():
        hits.append('robot-or-human')
    if 'px-captcha' in html:
        hits.append('px-captcha')
    if '/blocked' in url:
        hits.append('/blocked')
    return hits


def press_and_hold(d, name: str) -> dict:
    """On a PerimeterX challenge: WebDriver Actions pointerMove/pointerDown/pause/pointerUp on #px-captcha."""
    out: dict = {}
    rect = d.js("const e = document.querySelector('#px-captcha'); if (!e) return null; const r = e.getBoundingClientRect();"
                " return {x: r.x, y: r.y, w: r.width, h: r.height, iframes: e.querySelectorAll('iframe').length}")
    out['rect'] = rect
    if not rect or not rect['w']:
        return out
    cx, cy = int(rect['x'] + rect['w'] / 2), int(rect['y'] + rect['h'] / 2)
    t = time.perf_counter()
    try:
        d.actions([{'type': 'pointer', 'id': 'mouse', 'parameters': {'pointerType': 'mouse'}, 'actions': [
            {'type': 'pointerMove', 'x': cx - 40, 'y': cy + 30, 'duration': 0},
            {'type': 'pointerMove', 'x': cx, 'y': cy, 'duration': 400},
            {'type': 'pointerDown', 'button': 0},
            {'type': 'pause', 'duration': 12000},
            {'type': 'pointerUp', 'button': 0}]}], )
        out['action_s'] = round(time.perf_counter() - t, 2)
    except Exception as e:  # noqa: BLE001
        out['action_error'] = str(e)
    time.sleep(5)
    write_png(SPIKE / f'out/{name}-after-hold.png', d.screenshot_png())
    html = d.source()
    out['challenge_after'] = is_challenge(d.title(), html, d.current_url())
    out['url_after'] = d.current_url()
    return out


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    build = 'v070' if '--build=v070' in sys.argv else 'nightly'
    prefs = '--no-prefs' not in sys.argv
    site, ua = args[0], args[1]
    label = args[2] if len(args) > 2 else str(int(time.time()))
    name = f'{build}-{site}-{ua}-{label}'
    rec: dict = {'build': build, 'site': site, 'ua_kind': ua, 'label': label, 'prefs': prefs, 'url_requested': SITES[site]}

    servo = Servo(build=build, ua=ua, prefs=prefs, log=SPIKE / f'logs/run-{name}.log')
    sampler = RssSampler(servo.proc.pid)
    try:
        t0 = time.perf_counter()
        d = servo.session()
        rec['launch_s'] = round(time.perf_counter() - servo.t_spawn, 3)  # spawn -> WebDriver ready -> session created
        d.set_timeouts(pageLoad=60000, script=30000)
        t1 = time.perf_counter()
        try:
            d.get(SITES[site], timeout=75)
            rec['load_s'] = round(time.perf_counter() - t1, 3)
        except Exception as e:  # noqa: BLE001
            rec['load_s'] = None
            rec['load_error'] = str(e)
        time.sleep(2)
        for k, f in [('title', d.title), ('final_url', d.current_url), ('html', d.source)]:
            try:
                rec[k] = f()
            except Exception as e:  # noqa: BLE001
                rec[k] = ''
                rec[f'{k}_error'] = str(e)
        html = rec.pop('html')
        rec['html_len'] = len(html)
        rec['challenge'] = is_challenge(rec['title'] or '', html, rec['final_url'] or '')
        try:
            rec['fingerprint'] = d.js(FINGERPRINT_JS)
        except Exception as e:  # noqa: BLE001
            rec['fingerprint_error'] = str(e)
        try:
            snap = d.js(SNAP, 400)
            rec['snapshot_count'] = snap['count']
            rec['snapshot_ms'] = snap['ms']
            rec['snapshot_head'] = snap['lines'][:25]
            (SPIKE / f'out/{name}-snapshot.txt').write_text('\n'.join(snap['lines']))
        except Exception as e:  # noqa: BLE001
            rec['snapshot_error'] = str(e)
        try:
            write_png(SPIKE / f'out/{name}.png', d.screenshot_png())
        except Exception as e:  # noqa: BLE001
            rec['screenshot_error'] = str(e)
        if '--hold' in sys.argv and rec['challenge']:
            rec['hold'] = press_and_hold(d, name)
        pids = tree(servo.proc.pid)
        rec['pids'] = len(pids)
        rec.update(footprint(pids))
        rec['peak_rss_sum_mib'] = sampler.stop()
        rec['total_s'] = round(time.perf_counter() - t0, 3)
        d.quit()
    finally:
        sampler.stop() if sampler._t.is_alive() else None
        servo.close()
    log = (SPIKE / f'logs/run-{name}.log').read_text(errors='replace')
    rec['log_lines'] = len(log.splitlines())
    rec['log_errors'] = [ln[:300] for ln in log.splitlines() if 'error' in ln.lower() or 'panic' in ln.lower()][:15]
    with open(SPIKE / 'out/results.jsonl', 'a') as f:
        f.write(json.dumps(rec) + '\n')
    show = {k: rec.get(k) for k in ('build', 'site', 'ua_kind', 'launch_s', 'load_s', 'title', 'final_url', 'challenge',
                                    'snapshot_count', 'fp_peak_mib', 'footprint_mib', 'peak_rss_sum_mib', 'load_error')}
    print(json.dumps(show))


if __name__ == '__main__':
    main()
