"""The agent may open only public http(s) addresses."""

from __future__ import annotations

import asyncio

import pytest

from montybot.browsing import refused_url


@pytest.mark.parametrize(
    'url',
    [
        'file:///etc/passwd',
        'javascript:alert(1)',
        'http://127.0.0.1:8000/api/threads',
        'http://localhost/',
        'http://169.254.169.254/latest/meta-data/',
        'http://10.1.2.3/',
        'http://[::1]/',
    ],
)
def test_refused(url: str) -> None:
    assert asyncio.run(refused_url(url, allow_private=False)) is not None


def test_public_and_allowed() -> None:
    assert asyncio.run(refused_url('https://93.184.215.14/', allow_private=False)) is None
    assert asyncio.run(refused_url('http://127.0.0.1:9/', allow_private=True)) is None
    assert asyncio.run(refused_url('file:///etc/passwd', allow_private=True)) is not None
