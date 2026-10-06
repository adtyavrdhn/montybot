"""Minimal W3C WebDriver client for servoshell, plus launch and memory helpers.

Plain HTTP (urllib) on purpose, so every request in the README maps 1:1 to a WebDriver endpoint.
"""

from __future__ import annotations

import base64
import json
import pathlib
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any

SPIKE = pathlib.Path(__file__).resolve().parents[1]
BUILDS = {
    'nightly': SPIKE / 'bin/nightly/Servo.app/Contents/MacOS/servoshell',
    'v070': SPIKE / 'bin/v070/Servo.app/Contents/MacOS/servoshell',
}
ELEMENT = 'element-6066-11e4-a52e-4f735466cecf'

UA = {
    'servo': None,  # servoshell default
    'firefox': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:143.0) Gecko/20100101 Firefox/143.0',
    'chrome': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36',
}

# Prefs real sites need that are off by default in Servo 0.7.0 (names from servo-config 0.7.0 prefs.rs).
SITE_PREFS = [
    'dom_crypto_subtle_enabled',
    'dom_intersection_observer_enabled',
    'dom_resize_observer_enabled',
    'dom_adoptedstylesheet_enabled',
    'dom_fontface_enabled',
    'dom_indexeddb_enabled',
    'dom_web_animations_enabled',
    'dom_permissions_enabled',
    'dom_storage_manager_api_enabled',
    'dom_cookiestore_enabled',
]


class WebDriverError(Exception):
    def __init__(self, status: int, value: dict[str, Any]):
        self.status = status
        self.value = value
        super().__init__(f'{status} {value.get("error")}: {value.get("message")}')


def free_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(method: str, url: str, body: Any = None, timeout: float = 120) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b'{}').get('value')
    except urllib.error.HTTPError as e:
        payload = json.loads(e.read() or b'{}').get('value') or {}
        raise WebDriverError(e.code, payload) from None


class Servo:
    """One servoshell process with its WebDriver server."""

    def __init__(
        self,
        *,
        build: str = 'nightly',
        ua: str = 'chrome',
        prefs: bool = True,
        headless: bool = True,
        size: str = '1280x800',
        extra_args: list[str] | None = None,
        log: pathlib.Path | None = None,
    ):
        self.port = free_port()
        args = [str(BUILDS[build]), f'--webdriver={self.port}', '--temporary-storage', f'--window-size={size}']
        if headless:
            args.append('--headless')
        if prefs:
            args.append('--enable-experimental-web-platform-features')
            args += [f'--pref={p}' for p in SITE_PREFS]
        if UA[ua]:
            args.append(f'--user-agent={UA[ua]}')
        args += extra_args or []
        args.append('about:blank')
        self.args = args
        self.t_spawn = time.perf_counter()
        self.log = open(log, 'w') if log else subprocess.DEVNULL
        self.proc = subprocess.Popen(args, stdout=self.log, stderr=subprocess.STDOUT)
        self.base = f'http://127.0.0.1:{self.port}'
        for _ in range(400):
            try:
                if http('GET', f'{self.base}/status', timeout=1):
                    break
            except Exception:
                pass
            if self.proc.poll() is not None:
                raise RuntimeError(f'servoshell exited {self.proc.returncode}')
            time.sleep(0.025)
        self.t_ready = time.perf_counter()

    def session(self) -> Session:
        return Session(self.base)

    def close(self) -> None:
        # Headless servoshell keeps running after Delete Session and ignores SIGTERM and SIGINT (measured), so kill it.
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


class Session:
    def __init__(self, base: str, caps: dict[str, Any] | None = None):
        self.base = base
        v = http('POST', f'{base}/session', {'capabilities': {'alwaysMatch': caps or {}}})
        self.id = v['sessionId']
        self.caps = v['capabilities']
        self.url = f'{base}/session/{self.id}'

    def cmd(self, method: str, path: str, body: Any = None, timeout: float = 120) -> Any:
        if method == 'POST' and body is None:
            body = {}
        return http(method, f'{self.url}{path}', body, timeout)

    # navigation
    def get(self, url: str, timeout: float = 90) -> None:
        self.cmd('POST', '/url', {'url': url}, timeout=timeout)

    def current_url(self) -> str:
        return self.cmd('GET', '/url')

    def title(self) -> str:
        return self.cmd('GET', '/title')

    def source(self) -> str:
        return self.cmd('GET', '/source')

    def set_timeouts(self, **kw: int) -> None:
        self.cmd('POST', '/timeouts', kw)

    # script
    def js(self, script: str, *args: Any, timeout: float = 60) -> Any:
        return self.cmd('POST', '/execute/sync', {'script': script, 'args': list(args)}, timeout=timeout)

    def js_async(self, script: str, *args: Any) -> Any:
        return self.cmd('POST', '/execute/async', {'script': script, 'args': list(args)})

    # elements
    def find(self, css: str) -> str:
        return self.cmd('POST', '/element', {'using': 'css selector', 'value': css})[ELEMENT]

    def click(self, el: str) -> None:
        self.cmd('POST', f'/element/{el}/click')

    def send_keys(self, el: str, text: str) -> None:
        self.cmd('POST', f'/element/{el}/value', {'text': text})

    def clear(self, el: str) -> None:
        self.cmd('POST', f'/element/{el}/clear')

    def prop(self, el: str, name: str) -> Any:
        return self.cmd('GET', f'/element/{el}/property/{name}')

    def rect(self, el: str) -> dict[str, float]:
        return self.cmd('GET', f'/element/{el}/rect')

    def computed_role(self, el: str) -> Any:
        return self.cmd('GET', f'/element/{el}/computedrole')

    def computed_label(self, el: str) -> Any:
        return self.cmd('GET', f'/element/{el}/computedlabel')

    def actions(self, sources: list[dict[str, Any]]) -> None:
        self.cmd('POST', '/actions', {'actions': sources})

    def release_actions(self) -> None:
        self.cmd('DELETE', '/actions')

    # cookies
    def cookies(self) -> list[dict[str, Any]]:
        return self.cmd('GET', '/cookie')

    def add_cookie(self, cookie: dict[str, Any]) -> None:
        self.cmd('POST', '/cookie', {'cookie': cookie})

    def delete_cookies(self) -> None:
        self.cmd('DELETE', '/cookie')

    # windows
    def new_window(self, kind: str = 'tab') -> str:
        return self.cmd('POST', '/window/new', {'type': kind})['handle']

    def switch_window(self, handle: str) -> None:
        self.cmd('POST', '/window', {'handle': handle})

    def window_handles(self) -> list[str]:
        return self.cmd('GET', '/window/handles')

    # screenshot
    def screenshot_png(self) -> bytes:
        return base64.b64decode(self.cmd('GET', '/screenshot'))

    def quit(self) -> None:
        try:
            self.cmd('DELETE', '', timeout=10)
        except Exception:
            pass


# ---------------------------------------------------------------- memory

_UNITS = {'B': 1 / 1048576, 'KB': 1 / 1024, 'MB': 1, 'GB': 1024}


def tree(pid: int) -> list[int]:
    import psutil

    try:
        p = psutil.Process(pid)
        return [pid] + [c.pid for c in p.children(recursive=True)]
    except psutil.NoSuchProcess:
        return []


def footprint(pids: list[int]) -> dict[str, Any]:
    """Same method as ../engines/scripts/measure.py: macOS `footprint`, summing phys_footprint_peak per process."""
    pids_s = [str(p) for p in pids]
    if not pids_s:
        return {}
    out = subprocess.run(['footprint', *pids_s], capture_output=True, text=True, timeout=60).stdout
    m = re.search(r'Summary Footprint: ([\d.]+) (B|KB|MB|GB)', out) or re.search(r' Footprint: ([\d.]+) (B|KB|MB|GB)', out)
    peaks = re.findall(r'phys_footprint_peak: ([\d.]+) (B|KB|MB|GB)', out)
    return {
        'footprint_mib': round(float(m.group(1)) * _UNITS[m.group(2)], 1) if m else None,
        'fp_peak_mib': round(sum(float(v) * _UNITS[u] for v, u in peaks), 1) if peaks else None,
    }


class RssSampler:
    """Samples the RSS sum of a process tree every 50 ms (upper bound; same as the engines spike's RSS column)."""

    def __init__(self, pid: int):
        self.pid = pid
        self.peak = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self) -> None:
        import psutil

        while not self._stop.is_set():
            total = 0
            for p in tree(self.pid):
                try:
                    total += psutil.Process(p).memory_info().rss
                except psutil.NoSuchProcess:
                    pass
            self.peak = max(self.peak, total)
            time.sleep(0.05)

    def stop(self) -> float:
        self._stop.set()
        self._t.join()
        return round(self.peak / 1048576, 1)


def write_png(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
