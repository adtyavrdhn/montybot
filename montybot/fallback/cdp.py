"""A small Chrome DevTools Protocol client that runs inside the desktop VM, next to Chrome.

`e2b_desktop.py` uploads this file into the VM and runs it with the VM's own python3, once per browser step:

    python3 cdp.py REQUEST.json        # the request file is deleted as soon as it is read
    python3 cdp.py --inline BASE64     # for small requests that hold no credentials

A request is `{"profile_dir": ..., "op": ..., "args": {...}}`. The reply is one line of JSON on stdout:
`{"ok": RESULT}` or `{"error": "not_found" | "failed", "message": ...}`.

Chrome is started with `--remote-debugging-port=0`, so it picks a free port on the VM's loopback and writes it to
`DevToolsActivePort` in its profile. Nothing outside the VM can reach it; only this script, run through the E2B
command API, talks to it.

Stdlib only, and no syntax newer than Python 3.10, the python3 on E2B's desktop template (Ubuntu 22.04). Error
messages never include cookie values, storage values or typed text.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

Json = dict[str, Any]

_SEED_PAGE = base64.b64encode(b'<!doctype html><title>montybot</title>').decode()
_DOCUMENTS = {'patterns': [{'urlPattern': '*', 'resourceType': 'Document', 'requestStage': 'Request'}]}


class CdpError(Exception):
    """Chrome refused a command, closed the connection, or did not answer in time."""


class NotFound(Exception):
    """No element matched a selector."""


# --- a WebSocket client: just enough of RFC 6455 for CDP ---


class WebSocket:
    def __init__(self, url: str, *, timeout: float) -> None:
        parts = urlsplit(url)
        self._sock = socket.create_connection((parts.hostname or '127.0.0.1', parts.port or 80), timeout=timeout)
        self._buffer = bytearray()
        key = base64.b64encode(os.urandom(16)).decode()
        self._sock.sendall(
            (
                f'GET {parts.path} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\n'
                f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n'
            ).encode()
        )
        while b'\r\n\r\n' not in self._buffer:
            self._fill()
        head, _, rest = bytes(self._buffer).partition(b'\r\n\r\n')
        status = head.split(b'\r\n')[0]
        if b' 101 ' not in status + b' ':
            raise CdpError(f'Chrome refused the WebSocket: {status.decode(errors="replace")}')
        self._buffer = bytearray(rest)

    def settimeout(self, seconds: float) -> None:
        self._sock.settimeout(max(seconds, 0.001))

    def send(self, text: str) -> None:
        self._frame(0x1, text.encode())

    def recv(self) -> str:
        message = bytearray()
        while True:
            first, second = self._read(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack('>H', self._read(2))[0]
            elif length == 127:
                length = struct.unpack('>Q', self._read(8))[0]
            mask = self._read(4) if second & 0x80 else b''
            payload = self._read(length)
            if mask:
                payload = _xor(payload, mask)
            if opcode == 0x8:
                raise CdpError('Chrome closed the connection')
            if opcode == 0x9:
                self._frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            message += payload
            if first & 0x80:
                return message.decode()

    def close(self) -> None:
        try:
            self._frame(0x8, b'')
        except OSError:
            pass
        self._sock.close()

    def _frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack('>H', length)
        else:
            header.append(0x80 | 127)
            header += struct.pack('>Q', length)
        mask = os.urandom(4)
        self._sock.sendall(bytes(header) + mask + _xor(payload, mask))

    def _fill(self) -> None:
        chunk = self._sock.recv(1 << 16)
        if not chunk:
            raise CdpError('Chrome closed the connection')
        self._buffer += chunk

    def _read(self, count: int) -> bytes:
        while len(self._buffer) < count:
            self._fill()
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data


def _xor(payload: bytes, mask: bytes) -> bytes:
    if not payload:
        return payload
    repeated = (mask * (len(payload) // 4 + 1))[: len(payload)]
    return (int.from_bytes(payload, 'big') ^ int.from_bytes(repeated, 'big')).to_bytes(len(payload), 'big')


# --- CDP over one browser connection, with tabs attached in flat mode ---


class Cdp:
    def __init__(self, socket: WebSocket) -> None:
        self._socket = socket
        self._last_id = 0
        self._events: list[Json] = []

    @classmethod
    def connect(cls, profile_dir: str, *, timeout: float = 30) -> Cdp:
        """Connect to the Chrome using `profile_dir`, waiting for it to start listening."""
        port_file = os.path.join(profile_dir, 'DevToolsActivePort')
        deadline = time.monotonic() + timeout
        while True:
            try:
                with open(port_file) as file:
                    port, path = file.read().split()[:2]
                return cls(WebSocket(f'ws://127.0.0.1:{port}{path}', timeout=timeout))
            except (OSError, ValueError, CdpError):
                if time.monotonic() > deadline:
                    raise CdpError('Chrome did not start listening for CDP') from None
                time.sleep(0.1)

    def send(self, method: str, params: Json | None = None, *, session: str | None = None) -> int:
        self._last_id += 1
        message: Json = {'id': self._last_id, 'method': method, 'params': params or {}}
        if session is not None:
            message['sessionId'] = session
        self._socket.send(json.dumps(message))
        return self._last_id

    def result(self, call_id: int, *, timeout: float = 30) -> Json:
        message = self._next(lambda m: m.get('id') == call_id, timeout, f'call {call_id}')
        if 'error' in message:
            raise CdpError(str(message['error'].get('message', 'unknown error')))
        return message.get('result', {})

    def call(self, method: str, params: Json | None = None, *, session: str | None = None, timeout: float = 30) -> Json:
        call_id = self.send(method, params, session=session)
        try:
            return self.result(call_id, timeout=timeout)
        except CdpError as error:
            raise CdpError(f'{method}: {error}') from None

    def event(self, method: str, *, session: str | None, timeout: float = 30) -> Json:
        def matches(message: Json) -> bool:
            return message.get('method') == method and message.get('sessionId') == session

        for index, message in enumerate(self._events):
            if matches(message):
                return self._events.pop(index)
        return self._next(matches, timeout, method)

    def close(self) -> None:
        self._socket.close()

    def _next(self, wanted: Callable[[Json], bool], timeout: float, what: str) -> Json:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise CdpError(f'no answer to {what} within {timeout:.0f}s')
            self._socket.settimeout(left)
            try:
                message: Json = json.loads(self._socket.recv())
            except TimeoutError:
                continue
            if wanted(message):
                return message
            if 'method' in message:
                self._events.append(message)


class Tab:
    """One attached page."""

    def __init__(self, cdp: Cdp, target_id: str) -> None:
        self.cdp = cdp
        self.target_id = target_id
        self.session: str = cdp.call('Target.attachToTarget', {'targetId': target_id, 'flatten': True})['sessionId']

    @classmethod
    def first(cls, cdp: Cdp) -> Tab:
        """The browser's first ordinary tab, the one the user sees. Opens one if there is none."""
        targets = cdp.call('Target.getTargets')['targetInfos']
        pages = [t for t in targets if t['type'] == 'page' and not str(t['url']).startswith('devtools://')]
        if pages:
            return cls(cdp, pages[0]['targetId'])
        return cls(cdp, cdp.call('Target.createTarget', {'url': 'about:blank'})['targetId'])

    def call(self, method: str, params: Json | None = None, *, timeout: float = 30) -> Json:
        return self.cdp.call(method, params, session=self.session, timeout=timeout)

    def evaluate(self, expression: str) -> Any:
        reply = self.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True, 'awaitPromise': True})
        if 'exceptionDetails' in reply:
            details = reply['exceptionDetails']
            raise CdpError(f'a script failed: {details.get("exception", {}).get("className") or details.get("text")}')
        return reply['result'].get('value')

    def wait_loaded(self, *, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self.evaluate('document.readyState') == 'complete':
                    return
            except CdpError:
                pass  # the old document went away mid-navigation
            if time.monotonic() > deadline:
                raise CdpError('the page did not finish loading')
            time.sleep(0.05)

    def navigate(self, url: str) -> None:
        reply = self.call('Page.navigate', {'url': url})
        if reply.get('errorText'):
            raise CdpError(f'could not load the page: {reply["errorText"]}')
        self.wait_loaded()

    def open_blank_page(self, origin: str) -> None:
        """Show an empty page on `origin` without asking its server: Chrome makes the request, and we answer it."""
        self.call('Fetch.enable', _DOCUMENTS)
        try:
            call_id = self.cdp.send('Page.navigate', {'url': origin + '/'}, session=self.session)
            paused = self.cdp.event('Fetch.requestPaused', session=self.session)
            self.call(
                'Fetch.fulfillRequest',
                {
                    'requestId': paused['params']['requestId'],
                    'responseCode': 200,
                    'responseHeaders': [{'name': 'Content-Type', 'value': 'text/html'}],
                    'body': _SEED_PAGE,
                },
            )
            self.cdp.result(call_id)
            self.wait_loaded()
        finally:
            self.call('Fetch.disable')

    def viewport(self) -> Json:
        """Where the viewport's top-left corner is on the screen, in CSS pixels, its size, and the device pixel
        ratio."""
        return self.evaluate(_VIEWPORT_JS)


_VIEWPORT_JS = (
    '({x: screenX + (outerWidth - innerWidth), y: screenY + (outerHeight - innerHeight), scale: devicePixelRatio,'
    ' width: innerWidth, height: innerHeight})'
)

_STORAGE_JS = 'Object.fromEntries(Object.keys({0}).map((k) => [k, {0}.getItem(k)]))'

_LOCATE_JS = """(() => {
  let el;
  try { el = document.querySelector(%(css)s); } catch (e) { return {invalid: true}; }
  if (!el) return null;
  el.scrollIntoView({block: 'center', inline: 'center'});
  const r = el.getBoundingClientRect();
  if (r.width === 0 && r.height === 0) return null;
  if (%(focus)s) {
    el.focus();
    if (typeof el.select === 'function') el.select();
    else if (el.isContentEditable) getSelection().selectAllChildren(el);
  }
  return {x: r.left + r.width / 2, y: r.top + r.height / 2};
})()"""


def _set_storage(tab: Tab, kind: str, items: dict[str, str]) -> None:
    tab.evaluate(
        f'(items => {{ for (const [k, v] of Object.entries(items)) {kind}.setItem(k, v); }})({json.dumps(items)})'
    )


# --- the operations e2b_desktop.py asks for ---


def op_ready(cdp: Cdp, tab: Tab, args: Json) -> Json:
    tab.call('Page.bringToFront')
    return {}


def op_seed(cdp: Cdp, tab: Tab, args: Json) -> Json:
    """Set cookies, then storage on an empty page per origin, then load `url`."""
    if args['cookies']:
        cdp.call('Storage.setCookies', {'cookies': args['cookies']})
    for origin, storage in args['storage'].items():
        tab.open_blank_page(origin)
        for kind in ('localStorage', 'sessionStorage'):
            if storage.get(kind):
                _set_storage(tab, kind, storage[kind])
    if args['url'] != 'about:blank' or args['storage']:
        tab.navigate(args['url'])
    return {}


def op_navigate(cdp: Cdp, tab: Tab, args: Json) -> Json:
    tab.navigate(args['url'])
    return {}


def op_settle(cdp: Cdp, tab: Tab, args: Json) -> Json:
    """Let an action's navigation start, then wait for the page to load."""
    time.sleep(0.3)
    tab.wait_loaded()
    return {}


def op_snapshot(cdp: Cdp, tab: Tab, args: Json) -> Json:
    return tab.evaluate(
        '({url: location.href, title: document.title, text: document.body ? document.body.innerText : ""})'
    )


def op_viewport(cdp: Cdp, tab: Tab, args: Json) -> Json:
    return tab.viewport()


def op_locate(cdp: Cdp, tab: Tab, args: Json) -> Json:
    """The centre of the element `css` matches, scrolled into view, and the viewport's place on the screen. With
    `focus`, the element is focused and its contents selected, so typing replaces them."""
    script = _LOCATE_JS % {'css': json.dumps(args['css']), 'focus': 'true' if args['focus'] else 'false'}
    deadline = time.monotonic() + args.get('timeout', 2)
    while True:
        found = tab.evaluate(script)
        if found and found.get('invalid'):
            raise NotFound('not a valid CSS selector')
        if found:
            return {'point': found, 'viewport': tab.viewport()}
        if time.monotonic() > deadline:
            raise NotFound('no element matched')
        time.sleep(0.1)


def op_scroll(cdp: Cdp, tab: Tab, args: Json) -> Json:
    viewport = tab.viewport()
    x = args['x'] if args['x'] is not None else viewport['width'] / 2
    y = args['y'] if args['y'] is not None else viewport['height'] / 2
    tab.call(
        'Input.dispatchMouseEvent',
        {'type': 'mouseWheel', 'x': x, 'y': y, 'deltaX': args['delta_x'], 'deltaY': args['delta_y']},
    )
    return {}


def op_screenshot(cdp: Cdp, tab: Tab, args: Json) -> Json:
    metrics = tab.call('Page.getLayoutMetrics')['cssVisualViewport']
    png = tab.call('Page.captureScreenshot', {'format': 'png'})['data']
    return {'png': png, 'width': round(metrics['clientWidth']), 'height': round(metrics['clientHeight'])}


def op_export(cdp: Cdp, tab: Tab, args: Json) -> Json:
    """The URL, every cookie, and storage: both kinds for the current origin, localStorage for `origins`.

    localStorage for other origins is read in a second, short-lived tab that shows an empty page on each origin, so the
    user's tab is left alone. localStorage is shared between tabs, so it reads the same.
    """
    local_js = _STORAGE_JS.format('localStorage')
    session_js = _STORAGE_JS.format('sessionStorage')
    page = tab.evaluate(
        "(() => { const web = ['http:', 'https:'].includes(location.protocol);"
        f' return {{url: location.href, origin: location.origin, local: web ? {local_js} : {{}},'
        f' session: web ? {session_js} : {{}}}}; }})()'
    )
    cookies = cdp.call('Storage.getCookies')['cookies']
    current = page['origin'] if page['url'].startswith(('http://', 'https://')) else None
    local: dict[str, dict[str, str]] = {}
    session: dict[str, dict[str, str]] = {}
    if current is not None:
        local[current] = page['local']
        session[current] = page['session']
    others = [origin for origin in args['origins'] if origin != current]
    if others:
        target = cdp.call('Target.createTarget', {'url': 'about:blank', 'background': True})['targetId']
        try:
            reader = Tab(cdp, target)
            for origin in others:
                reader.open_blank_page(origin)
                local[origin] = reader.evaluate(local_js)
        finally:
            cdp.call('Target.closeTarget', {'targetId': target})
    return {'url': page['url'], 'cookies': cookies, 'local_storage': local, 'session_storage': session}


OPS: dict[str, Callable[[Cdp, Tab, Json], Json]] = {
    'ready': op_ready,
    'seed': op_seed,
    'navigate': op_navigate,
    'settle': op_settle,
    'snapshot': op_snapshot,
    'viewport': op_viewport,
    'locate': op_locate,
    'scroll': op_scroll,
    'screenshot': op_screenshot,
    'export': op_export,
}


def handle(request: Json) -> Json:
    """Run one request and return the reply."""
    try:
        cdp = Cdp.connect(request['profile_dir'])
        try:
            return {'ok': OPS[request['op']](cdp, Tab.first(cdp), request['args'])}
        finally:
            cdp.close()
    except NotFound as error:
        return {'error': 'not_found', 'message': str(error)}
    except (CdpError, OSError) as error:
        return {'error': 'failed', 'message': str(error) or type(error).__name__}


def main(argv: list[str]) -> None:
    if argv[:1] == ['--inline']:
        request = json.loads(base64.b64decode(argv[1]))
    else:
        with open(argv[0]) as file:
            request = json.load(file)
        os.remove(argv[0])
    print(json.dumps(handle(request)))


if __name__ == '__main__':
    main(sys.argv[1:])
