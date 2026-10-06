"""U4 watching: a delivery-slot page that says "no slots" until the test opens one."""

from __future__ import annotations

from dataclasses import dataclass

from sites.base import Request, Response, Site, page


@dataclass
class Slots(Site):
    open_slot: str | None = None
    """Set by the test: the slot that has opened, such as `Tuesday 18:00-19:00`."""
    views: int = 0

    def __post_init__(self) -> None:
        super().__init__()
        self.route('GET', '/')(self.slots)

    def slots(self, request: Request) -> Response:
        self.views += 1
        body = (
            f'<p id="slot">Available: {self.open_slot}</p>'
            if self.open_slot
            else '<p id="slot">No slots available.</p>'
        )
        return page('Delivery slots', f'<h1>Delivery slots</h1>{body}')
