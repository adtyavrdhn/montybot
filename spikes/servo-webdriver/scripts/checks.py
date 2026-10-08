"""Functional checks for servoshell over WebDriver. Needs `python3 scripts/server.py 8767` running.

usage: uv run --no-project --with psutil python scripts/checks.py [form|github|state|live|density ...]
Writes out/checks-<name>.json per section.
"""

from __future__ import annotations

import json
import statistics
import sys
import time

from servo_backend import BrowserState, apply_state, export_state
from wd import SPIKE, Servo, WebDriverError, footprint, tree, write_png

SNAP = (SPIKE / 'scripts/snapshot.js').read_text()
HOSTS = ['--host-file=' + str(SPIKE / 'scripts/hosts.txt')]
PORT = 8767
LOCAL = f'http://127.0.0.1:{PORT}'


def body(d) -> str:
    return d.js('return document.body.innerText').replace('\n', ' ')


def err(e: Exception) -> str:
    return str(e)


# ------------------------------------------------------------------ 2 + 3: agent reads and acts on forms
def check_form() -> dict:
    r: dict = {}
    s = Servo(log=SPIKE / 'logs/checks-form.log')
    d = s.session()
    try:
        d.get(f'{LOCAL}/login.html')
        snap = d.js(SNAP)
        r['snapshot_before'] = snap['lines']
        refs = {ln.split('"')[1]: ln.split('[ref=')[1].split(']')[0] for ln in snap['lines'] if '[ref=' in ln and '"' in ln}
        # act purely by ref, the way an agent would: Find Element by [data-mb-ref], Element Click, Element Send Keys
        user = d.find(f'[data-mb-ref="{refs["Username"]}"]')
        d.click(user)
        d.send_keys(user, 'alice')
        pw = d.find(f'[data-mb-ref="{refs["Password"]}"]')
        d.send_keys(pw, 'hunter2')
        r['snapshot_after_typing'] = d.js(SNAP)['lines']
        d.click(d.find(f'[data-mb-ref="{refs["Log in"]}"]'))
        time.sleep(0.3)
        r['status_after_submit'] = d.js("return document.getElementById('status').textContent")
        write_png(SPIKE / 'out/checks-login.png', d.screenshot_png())
        # Element Clear and keyboard via Actions API
        d.clear(user)
        d.click(user)
        d.actions([{'type': 'key', 'id': 'kb', 'actions': [a for ch in 'bob' for a in
                                                          ({'type': 'keyDown', 'value': ch}, {'type': 'keyUp', 'value': ch})]}])
        r['value_after_actions_typing'] = d.prop(user, 'value')
        # WebDriver's own accessibility endpoints
        for name, sel in [('user', '#user'), ('button', '#go')]:
            el = d.find(sel)
            for ep in ('computedrole', 'computedlabel'):
                try:
                    r[f'{ep}_{name}'] = d.cmd('GET', f'/element/{el}/{ep}')
                except WebDriverError as e:
                    r[f'{ep}_{name}'] = 'ERROR ' + err(e)
    finally:
        d.quit()
        s.close()
    return r


def check_github() -> dict:
    """Read and fill github.com/login (not submitted)."""
    r: dict = {}
    s = Servo(log=SPIKE / 'logs/checks-github.log')
    d = s.session()
    try:
        d.set_timeouts(pageLoad=60000)
        d.get('https://github.com/login')
        time.sleep(1)
        snap = d.js(SNAP)
        r['snapshot_before'] = snap['lines']
        refs = {ln.split('"')[1]: ln.split('[ref=')[1].split(']')[0] for ln in snap['lines'] if '[ref=' in ln and '"' in ln}
        u = d.find(f'[data-mb-ref="{refs["Username or email address"]}"]')
        d.click(u)
        d.send_keys(u, 'sammy-spike')
        p = d.find(f'[data-mb-ref="{refs["Password"]}"]')
        d.click(p)
        d.send_keys(p, 'not-a-real-password')
        r['snapshot_after_typing'] = d.js(SNAP)['lines'][:8]
        write_png(SPIKE / 'out/checks-github-filled.png', d.screenshot_png())
    finally:
        d.quit()
        s.close()
    return r


# ------------------------------------------------------------------ 6: BrowserState round trip
def check_state() -> dict:
    r: dict = {}
    www, api = f'http://www.shop.test:{PORT}', f'http://api.shop.test:{PORT}'
    port_map = lambda host, secure: f'http://{host}:{PORT}'  # noqa: E731

    # A: a "remote" browser builds up state
    a = Servo(log=SPIKE / 'logs/checks-state-a.log', extra_args=HOSTS)
    d = a.session()
    try:
        d.get(f'{www}/setall')  # sid (HttpOnly, host-only), pref (Domain=shop.test), plain (host-only)
        d.js("document.cookie = 'jsc=1; path=/'")
        d.js("localStorage.setItem('cart', JSON.stringify(['coffee'])); sessionStorage.setItem('views', '2')")
        d.get(f'{api}/robots.txt')
        d.js("localStorage.setItem('token-cache', 'abc')")
        d.get(f'{www}/check')
        r['A_server_sees_on_www'] = body(d)
        r['A_get_all_cookies_on_www'] = d.cookies()
        try:
            r['A_get_named_cookie_sid'] = d.cmd('GET', '/cookie/sid')
        except WebDriverError as e:
            r['A_get_named_cookie_sid'] = 'ERROR ' + err(e)
        state, rep = export_state(d, [www, api])
        r['A_export_report'] = rep
        r['A_exported_state'] = state.to_json()
        r['A_exported_summary'] = state.summary()
    finally:
        d.quit()
        a.close()

    # B: apply exactly what WebDriver could export
    def seed_and_check(st: BrowserState, tag: str) -> None:
        b = Servo(log=SPIKE / f'logs/checks-state-{tag}.log', extra_args=HOSTS)
        e = b.session()
        try:
            r[f'{tag}_apply_report'] = apply_state(e, st, origin_for_host=port_map)
            r[f'{tag}_landed_on'] = e.current_url()
            r[f'{tag}_session_storage_on_landing'] = e.js('return sessionStorage.getItem("views")')
            e.get(f'{www}/check')
            r[f'{tag}_server_sees_on_www'] = body(e)
            r[f'{tag}_local_storage_www'] = e.js('return localStorage.getItem("cart")')
            e.get(f'{api}/check')
            r[f'{tag}_server_sees_on_api'] = body(e)
            r[f'{tag}_local_storage_api'] = e.js('return localStorage.getItem("token-cache")')
            back, _ = export_state(e, [www, api])
            r[f'{tag}_re_exported_summary'] = back.summary()
        finally:
            e.quit()
            b.close()

    state.url = f'{www}/login.html'
    seed_and_check(state, 'B')
    # C: the same state plus the HttpOnly cookie, as it would arrive from an engine that can export it
    # (e.g. the Playwright side of the PoC). Shows Add Cookie can apply HttpOnly.
    st2 = BrowserState.from_json(state.to_json())
    st2.cookies.append(state_cookie('sid', 'srv-httponly', 'www.shop.test', http_only=True))
    seed_and_check(st2, 'C')
    # Add Cookie domain rules, measured directly
    s = Servo(log=SPIKE / 'logs/checks-state-domain.log', extra_args=HOSTS)
    d = s.session()
    try:
        d.get(f'{www}/robots.txt')
        for c in [{'name': 'dot', 'value': '1', 'domain': '.shop.test'}, {'name': 'parent', 'value': '1', 'domain': 'shop.test'},
                  {'name': 'exact', 'value': '1', 'domain': 'www.shop.test'}, {'name': 'hostonly', 'value': '1', 'httpOnly': True},
                  {'name': 'other', 'value': '1', 'domain': 'other.test'}]:
            try:
                d.add_cookie(c)
                r[f'add_cookie_from_www_{c["name"]}'] = 'ok'
            except WebDriverError as e:
                r[f'add_cookie_from_www_{c["name"]}'] = 'ERROR ' + err(e)
        d.get('about:blank')
        try:
            d.add_cookie({'name': 'blank', 'value': '1'})
            r['add_cookie_on_about_blank'] = 'ok'
        except WebDriverError as e:
            r['add_cookie_on_about_blank'] = 'ERROR ' + err(e)
    finally:
        d.quit()
        s.close()
    (SPIKE / 'out/servo-browser-state.json').write_text(json.dumps(state.to_json(), indent=1))
    return r


def state_cookie(name, value, domain, http_only=False):
    from servo_backend import Cookie

    return Cookie(name=name, value=value, domain=domain, http_only=http_only)


# ------------------------------------------------------------------ 7: live view + input replay
def check_live() -> dict:
    r: dict = {}
    s = Servo(log=SPIKE / 'logs/checks-live.log')
    d = s.session()
    try:
        for label, url, n in [('login', f'{LOCAL}/login.html', 60), ('github', 'https://github.com/login', 40)]:
            d.set_timeouts(pageLoad=60000)
            d.get(url)
            time.sleep(1)
            ts, sizes = [], []
            t0 = time.perf_counter()
            for _ in range(n):
                t = time.perf_counter()
                png = d.screenshot_png()
                ts.append(time.perf_counter() - t)
                sizes.append(len(png))
            wall = time.perf_counter() - t0
            r[f'shot_{label}'] = {'n': n, 'median_ms': round(statistics.median(ts) * 1000, 1), 'p90_ms': round(sorted(ts)[int(n * .9)] * 1000, 1),
                                  'fps_sequential': round(n / wall, 1), 'median_png_kb': round(statistics.median(sizes) / 1024)}
        # screenshot while the page changes: type in a loop and check frames differ
        d.get(f'{LOCAL}/login.html')
        u = d.find('#user')
        d.click(u)
        frames = []
        for ch in 'live':
            d.send_keys(u, ch)
            frames.append(d.screenshot_png())
        r['frames_differ_while_typing'] = len({f for f in frames}) == len(frames)
        # replay a "human" input recording (as a takeover client would send it) through the Actions API
        d.get(f'{LOCAL}/events')
        recording = [  # (dt_ms, kind, x, y)
            (0, 'move', 120, 120), (40, 'move', 150, 130), (40, 'down', 150, 130), (900, 'move', 155, 131), (600, 'up', 155, 131),
        ]
        acts = []
        for dt, kind, x, y in recording:
            if dt:
                acts.append({'type': 'pause', 'duration': dt})
            if kind == 'move':
                acts.append({'type': 'pointerMove', 'x': x, 'y': y, 'duration': 0})
            elif kind == 'down':
                acts.append({'type': 'pointerDown', 'button': 0})
            else:
                acts.append({'type': 'pointerUp', 'button': 0})
        t = time.perf_counter()
        d.actions([{'type': 'pointer', 'id': 'mouse', 'parameters': {'pointerType': 'mouse'}, 'actions': acts}])
        r['replay_batch_s'] = round(time.perf_counter() - t, 3)
        r['replay_events'] = [e for e in d.js('return log') if e['type'] != 'pointermove']
        # per-event latency, the way a live relay would send each human event as it happens
        d.js('log.length = 0')
        lat = []
        for x in range(110, 290, 15):
            t = time.perf_counter()
            d.actions([{'type': 'pointer', 'id': 'mouse', 'parameters': {'pointerType': 'mouse'},
                        'actions': [{'type': 'pointerMove', 'x': x, 'y': 140, 'duration': 0}]}])
            lat.append(time.perf_counter() - t)
        t = time.perf_counter()
        d.actions([{'type': 'pointer', 'id': 'mouse', 'parameters': {'pointerType': 'mouse'}, 'actions': [{'type': 'pointerDown', 'button': 0}]}])
        down_ms = (time.perf_counter() - t) * 1000
        time.sleep(1.0)
        d.actions([{'type': 'pointer', 'id': 'mouse', 'parameters': {'pointerType': 'mouse'}, 'actions': [{'type': 'pointerUp', 'button': 0}]}])
        r['single_move_action_median_ms'] = round(statistics.median(lat) * 1000, 1)
        r['single_down_action_ms'] = round(down_ms, 1)
        r['split_down_up_events'] = [e for e in d.js('return log') if e['type'] != 'pointermove']
        d.click(d.find('#t'))
        t = time.perf_counter()
        d.actions([{'type': 'key', 'id': 'kb', 'actions': [{'type': 'keyDown', 'value': 'x'}, {'type': 'keyUp', 'value': 'x'}]}])
        r['single_key_action_ms'] = round((time.perf_counter() - t) * 1000, 1)
    finally:
        d.quit()
        s.close()
    return r


# ------------------------------------------------------------------ 8: multiple users
def check_density() -> dict:
    r: dict = {}
    procs = [Servo(log=SPIKE / 'logs/checks-density-0.log', extra_args=HOSTS)]
    sessions = [procs[0].session()]
    try:
        try:
            procs[0].session()
            r['second_session_same_process'] = 'ok'
        except WebDriverError as e:
            r['second_session_same_process'] = 'ERROR ' + err(e)
        sessions[0].get(f'{LOCAL}/login.html')
        time.sleep(1)
        base = footprint(tree(procs[0].proc.pid))
        r['one_process_on_login'] = base
        # windows inside one session share the cookie jar (not isolation)
        d = sessions[0]
        d.get(f'http://www.shop.test:{PORT}/set')
        main = d.cmd('GET', '/window')
        w = d.new_window('window')
        d.switch_window(w)
        d.get(f'http://www.shop.test:{PORT}/check')
        r['second_window_same_session_sees_cookie'] = body(d)
        d.cmd('DELETE', '/window')
        d.switch_window(main)
        d.get(f'{LOCAL}/login.html')
        # 4 more users = 4 more processes
        for i in range(1, 5):
            p = Servo(log=SPIKE / f'logs/checks-density-{i}.log', extra_args=HOSTS)
            procs.append(p)
            s = p.session()
            sessions.append(s)
            s.get(f'{LOCAL}/login.html')
        time.sleep(1.5)
        per = [footprint(tree(p.proc.pid)) for p in procs]
        r['five_processes_on_login'] = per
        r['fp_peak_per_extra_process_mib'] = round(statistics.mean(x['fp_peak_mib'] for x in per[1:]), 1)
        r['footprint_now_per_extra_process_mib'] = round(statistics.mean(x['footprint_mib'] for x in per[1:]), 1)
        # isolation across processes: cookie set in user 0 is not seen by user 1
        sessions[1].get(f'http://www.shop.test:{PORT}/check')
        r['user1_sees_user0_cookie'] = body(sessions[1])
    finally:
        for s in sessions:
            s.quit()
        for p in procs:
            p.close()
    return r


if __name__ == '__main__':
    names = sys.argv[1:] or ['form', 'github', 'state', 'live', 'density']
    for name in names:
        out = globals()[f'check_{name}']()
        (SPIKE / f'out/checks-{name}.json').write_text(json.dumps(out, indent=1, default=str))
        print(f'== {name}')
        print(json.dumps(out, indent=1, default=str)[:6000])
