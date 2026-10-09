"""Shops that sell the same kettle at different prices, for comparing prices across sites (#132).

Each product page takes `delay` seconds to answer, as a busy shop's does, and carries what real product pages put
around the price (a description and reviews), so reading it fills an agent's context.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sites.base import Request, Response, Site, esc, page

REVIEWS = 30


@dataclass
class KettleShop(Site):
    name: str
    price: float
    delay: float = 2.0
    visits: int = 0

    def __post_init__(self) -> None:
        super().__init__()
        self.route('GET', '/')(self.product)

    def product(self, request: Request) -> Response:
        self.visits += 1
        time.sleep(self.delay)
        reviews = ''.join(
            f'<li>Review {number}: boils fast, quiet, the lid opens wide and the handle stays cool.</li>'
            for number in range(1, REVIEWS + 1)
        )
        return page(
            f'{self.name}: Acme kettle',
            f'<h1>Acme kettle</h1><p>Sold by {esc(self.name)}.</p><p>Price: ${self.price:.2f}</p>'
            '<p>1.7 litres, 3 kW, stainless steel, with a limescale filter and a two-year guarantee.</p>'
            f'<h2>Reviews</h2><ul>{reviews}</ul>',
        )
