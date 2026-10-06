from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from liveview_harness import StubBrowserService

from montybot.browser.fake import FakeBrowser
from montybot.browser.service import BrowserService
from montybot.liveview.conformance import HandoffRulesConformance


class TestStubBrowserService(HandoffRulesConformance):
    """The stand-in for #10 keeps the hand-off rules, so the app tests built on it mean something."""

    @asynccontextmanager
    async def service(self) -> AsyncGenerator[BrowserService]:
        service = StubBrowserService(FakeBrowser)
        try:
            yield service
        finally:
            await service.close_all()
