"""Small helpers shared by the test suites."""

from __future__ import annotations

import socket
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar('T')


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def eventually(check: Callable[[], T | None], *, timeout: float = 60.0, what: str = 'it') -> T:
    """Wait until `check` returns something other than None. For waiting on what a user would see, never for timing."""
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result is not None:
            return result
        if time.monotonic() > deadline:
            raise AssertionError(f'gave up waiting for {what}')
        time.sleep(0.2)
