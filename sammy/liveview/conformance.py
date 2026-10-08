"""The hand-off rules every `BrowserService` must keep, `live_view` included. Needs pytest and anyio; import it from
tests only.

While the user drives, the agent gets `HandoffActive` for every call that would show it the page, so no screenshot
or snapshot reaches the model or its history. Only calls naming the active hand-off, which the live view makes,
see the page. Ending the hand-off ends every live view of it.

    class TestMyService(HandoffRulesConformance):
        @asynccontextmanager
        async def service(self) -> AsyncGenerator[BrowserService]:
            async with my_service(new_backend=FakeBrowser) as service:
                yield service

The service's browsers must be able to take screenshots. Runs start from an empty saved state, on `about:blank`.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager

import pytest

from sammy.browser.contract import Press, Scroll
from sammy.browser.live import Frame, FrameSource
from sammy.browser.service import BrowserService, Handoff, HandoffActive, HandoffNotActive, UnknownRun

RUN = 'run-1'
USER = 'alice'
OTHER_USER = 'bob'


async def first_frame(source: FrameSource, *, timeout: float = 10) -> Frame:
    async def first() -> Frame:
        async for update in source.updates():
            if isinstance(update, Frame):
                return update
        raise AssertionError('the live view ended before its first frame')

    return await asyncio.wait_for(first(), timeout)


async def assert_ends(source: FrameSource, *, timeout: float = 10) -> None:
    """`source.updates()` ends within `timeout`."""

    async def drain() -> None:
        async for _ in source.updates():
            pass

    await asyncio.wait_for(drain(), timeout)


class HandoffRulesConformance(ABC):
    pytestmark = pytest.mark.anyio

    @pytest.fixture
    def anyio_backend(self) -> str:
        return 'asyncio'

    @abstractmethod
    def service(self) -> AbstractAsyncContextManager[BrowserService]:
        """A fresh service. Clean up its browsers on exit."""

    async def start_handoff(self, service: BrowserService) -> Handoff:
        await service.start(run_id=RUN, user_id=USER)
        return await service.start_handoff(run_id=RUN, user_id=USER, reason='Please sign in')

    async def test_the_agent_cannot_see_or_act_while_the_user_drives(self) -> None:
        async with self.service() as service:
            await self.start_handoff(service)
            with pytest.raises(HandoffActive):
                await service.screenshot(run_id=RUN, user_id=USER)
            with pytest.raises(HandoffActive):
                await service.snapshot(run_id=RUN, user_id=USER)
            with pytest.raises(HandoffActive):
                await service.act(run_id=RUN, user_id=USER, action=Press(key='Tab'))

    async def test_start_handoff_is_idempotent(self) -> None:
        async with self.service() as service:
            handoff = await self.start_handoff(service)
            again = await service.start_handoff(run_id=RUN, user_id=USER, reason='Something else')
            assert again.handoff_id == handoff.handoff_id

    async def test_live_view_needs_the_active_handoff(self) -> None:
        async with self.service() as service:
            await service.start(run_id=RUN, user_id=USER)
            with pytest.raises(HandoffNotActive):
                await service.live_view(run_id=RUN, user_id=USER, handoff_id='no-such-handoff')
            handoff = await service.start_handoff(run_id=RUN, user_id=USER, reason='Please sign in')
            with pytest.raises(HandoffNotActive):
                await service.live_view(run_id=RUN, user_id=USER, handoff_id='no-such-handoff')
            with pytest.raises(UnknownRun):
                await service.live_view(run_id=RUN, user_id=OTHER_USER, handoff_id=handoff.handoff_id)

    async def test_the_live_view_sees_and_drives_while_the_agent_waits(self) -> None:
        async with self.service() as service:
            handoff = await self.start_handoff(service)
            source = await service.live_view(run_id=RUN, user_id=USER, handoff_id=handoff.handoff_id)
            try:
                frame = await first_frame(source)
                assert frame.image and frame.width > 0 and frame.height > 0
                await source.send(Scroll(delta_y=10))
                ids = {'run_id': RUN, 'user_id': USER, 'handoff_id': handoff.handoff_id}
                await service.act(**ids, action=Scroll(delta_y=10))
                await service.screenshot(**ids)
                with pytest.raises(HandoffActive):
                    await service.screenshot(run_id=RUN, user_id=USER)
            finally:
                await source.close()

    async def test_ending_the_handoff_ends_the_live_view(self) -> None:
        async with self.service() as service:
            handoff = await self.start_handoff(service)
            ids = {'run_id': RUN, 'user_id': USER, 'handoff_id': handoff.handoff_id}
            source = await service.live_view(**ids)
            await first_frame(source)
            await service.end_handoff(**ids)
            await assert_ends(source)
            await source.close()
            with pytest.raises(HandoffNotActive):
                await service.live_view(**ids)
            with pytest.raises(HandoffNotActive):
                await service.screenshot(**ids)
            with pytest.raises(HandoffNotActive):
                await service.end_handoff(**ids)
            await service.screenshot(run_id=RUN, user_id=USER)  # the agent has the browser again

    async def test_closing_the_run_ends_the_live_view(self) -> None:
        async with self.service() as service:
            handoff = await self.start_handoff(service)
            ids = {'run_id': RUN, 'user_id': USER, 'handoff_id': handoff.handoff_id}
            source = await service.live_view(**ids)
            await service.close(run_id=RUN, user_id=USER)
            await assert_ends(source)
            await source.close()
            with pytest.raises(UnknownRun):
                await service.live_view(**ids)
