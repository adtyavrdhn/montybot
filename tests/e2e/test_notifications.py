"""#8: when the bot needs the user, it says so by email and by web push, without saying what is on the page."""

from __future__ import annotations

import base64
import os
import socketserver
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
import pytest
from conftest import Client
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from helpers import eventually
from sites.shop import Shop

from montybot.notifications import new_vapid_keys

VAPID_PRIVATE, VAPID_PUBLIC = new_vapid_keys()


class Mailbox(socketserver.ThreadingTCPServer):
    """Just enough SMTP to receive a message."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self) -> None:
        self.messages: list[str] = []
        super().__init__(('127.0.0.1', 0), _Smtp)


class _Smtp(socketserver.StreamRequestHandler):
    server: Mailbox  # pyright: ignore[reportIncompatibleVariableOverride]

    def handle(self) -> None:
        self.wfile.write(b'220 mailbox\r\n')
        while line := self.rfile.readline():
            command = line.decode().strip().upper()
            if command.startswith('DATA'):
                self.wfile.write(b'354 go\r\n')
                body = []
                while (data := self.rfile.readline()) not in (b'.\r\n', b''):
                    body.append(data.decode())
                self.server.messages.append(''.join(body))
                self.wfile.write(b'250 ok\r\n')
            elif command.startswith('QUIT'):
                self.wfile.write(b'221 bye\r\n')
                return
            elif command.startswith('EHLO'):
                self.wfile.write(b'250 mailbox\r\n')
            else:
                self.wfile.write(b'250 ok\r\n')


class PushService(ThreadingHTTPServer):
    """Stands in for a browser vendor's push service: records what it is sent."""

    def __init__(self) -> None:
        self.pushes: list[tuple[dict[str, str], bytes]] = []
        super().__init__(('127.0.0.1', 0), _Push)


class _Push(BaseHTTPRequestHandler):
    server: PushService  # pyright: ignore[reportIncompatibleVariableOverride]

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        self.server.pushes.append(({k.lower(): v for k, v in self.headers.items()}, body))
        self.send_response(201)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def mailbox() -> Iterator[Mailbox]:
    server = Mailbox()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


@pytest.fixture
def push_service() -> Iterator[PushService]:
    server = PushService()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


@pytest.fixture
def app_env(mailbox: Mailbox) -> dict[str, str]:
    return {
        'SMTP_URL': f'smtp://127.0.0.1:{mailbox.server_address[1]}',
        'VAPID_PRIVATE_KEY': VAPID_PRIVATE,
        'VAPID_PUBLIC_KEY': VAPID_PUBLIC,
    }


@pytest.fixture
def shop() -> Iterator[Shop]:
    site = Shop()
    site.start()
    yield site
    site.stop()


def subscription_keys() -> dict[str, str]:
    """What a browser's PushManager hands over: its public key and an auth secret."""
    key = ec.generate_private_key(ec.SECP256R1())
    point = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    b64 = lambda data: base64.urlsafe_b64encode(data).decode().rstrip('=')
    return {'p256dh': b64(point), 'auth': b64(os.urandom(16))}


@pytest.mark.u2
def test_the_user_hears_that_the_bot_needs_them(
    client: Client, shop: Shop, mailbox: Mailbox, push_service: PushService, database_url: str
) -> None:
    user = client.sign_up()
    assert client.http.get('/api/push/key').json() == {'public_key': VAPID_PUBLIC}
    # The app takes https push endpoints only; this one is registered as a browser vendor's would be, then pointed at
    # the stand-in.
    keys = subscription_keys()
    response = client.http.post(
        '/api/push/subscriptions', json={'endpoint': 'https://push.example.test/x', 'keys': keys}
    )
    assert response.status_code == 201
    assert client.http.post('/api/push/subscriptions', json={'endpoint': 'http://x', 'keys': keys}).status_code == 422
    with psycopg.connect(database_url) as connection:
        connection.execute(
            'UPDATE montybot.push_subscriptions SET endpoint = %s',
            (f'http://127.0.0.1:{push_service.server_address[1]}/push',),
        )

    thread = client.ask(f'Order eggs from {shop.url}')
    client.wait_for_ask(thread, 'handoff')

    mail = eventually(lambda: mailbox.messages or None, what='an email')
    assert f'To: {user["email"]}' in mail[0]
    assert 'take over its browser' in mail[0] and f'/#/t/{thread}' in mail[0]
    assert '/live/' not in mail[0] and 'sign in' not in mail[0].lower()  # no hand-off link, no prompt

    pushes = eventually(lambda: push_service.pushes or None, what='a push')
    headers, body = pushes[0]
    assert headers['content-encoding'] == 'aes128gcm' and headers['authorization'].startswith('vapid ')
    assert b'take over' not in body  # encrypted for the subscriber's browser only
