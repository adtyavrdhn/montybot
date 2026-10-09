"""The chat platforms this server talks to.

A platform registers itself with one line in `BUILT_IN`: the `module:function` of a factory that takes the settings
and returns its `Channel`, or None while any of its credentials is unset. `CHANNEL_BACKENDS` adds more factories the
same way, which is how tests add a fake platform.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import cast

from sammy.channels.base import Channel
from sammy.imports import import_object
from sammy.settings import Settings

Factory = Callable[[Settings], Channel | None]

BUILT_IN: tuple[str, ...] = ('sammy.channels.whatsapp:new_channel',)
"""The platforms that ship with Sammy, by factory."""


class Channels:
    """The enabled platforms, by name."""

    def __init__(self, channels: Iterable[Channel] = ()) -> None:
        self._by_name: dict[str, Channel] = {}
        for channel in channels:
            if channel.name in self._by_name:
                raise ValueError(f'two chat platforms are called {channel.name!r}')
            self._by_name[channel.name] = channel

    @classmethod
    def from_settings(cls, settings: Settings, built_in: Iterable[str] = BUILT_IN) -> Channels:
        made = (cast(Factory, import_object(path))(settings) for path in (*built_in, *settings.channel_backends))
        return cls(channel for channel in made if channel is not None)

    def get(self, name: str) -> Channel | None:
        return self._by_name.get(name)

    @property
    def names(self) -> list[str]:
        return sorted(self._by_name)

    async def aclose(self) -> None:
        for channel in self._by_name.values():
            await channel.aclose()
