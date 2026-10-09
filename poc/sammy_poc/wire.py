"""Newline-delimited JSON over a TCP socket: the hand-off channel.

Messages:
    user's machine -> remote:  {"type": "hello", "token": ...}
    remote -> user's machine:  {"type": "handoff", "reason": ..., "state": BrowserState}
    user's machine -> remote:  {"type": "returned", "note": ..., "state": BrowserState}

The state carries live session cookies, so a real deployment needs TLS and a short-lived
signed token per hand-off. This proof of concept uses one shared token on localhost.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

# Browser state can be large (localStorage), and StreamReader's default line limit is 64 KiB.
LINE_LIMIT = 16 * 1024 * 1024
DEFAULT_PORT = 8799


async def send(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    writer.write(json.dumps(message).encode() + b'\n')
    await writer.drain()


async def recv(reader: asyncio.StreamReader) -> dict[str, Any]:
    line = await reader.readline()
    if not line:
        raise ConnectionError('the other side closed the hand-off socket')
    return json.loads(line)
