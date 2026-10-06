"""U1 read the web: a flights search with results spread over two pages, so the agent has to read both and work out the
answer ("the three cheapest flights to Lisbon next Friday")."""

from __future__ import annotations

from dataclasses import dataclass

from sites.base import Request, Response, Site, esc, page

FLIGHTS = [
    ('TP 1351', '07:05', 212),
    ('BA 500', '08:40', 189),
    ('FR 8341', '10:15', 64),
    ('U2 8169', '12:30', 97),
    ('TP 1357', '15:50', 240),
    ('FR 8345', '18:25', 71),
    ('BA 504', '20:10', 156),
    ('U2 8173', '21:45', 118),
]
PER_PAGE = 4


def cheapest(n: int) -> list[tuple[str, str, int]]:
    return sorted(FLIGHTS, key=lambda f: f[2])[:n]


@dataclass
class Flights(Site):
    def __post_init__(self) -> None:
        super().__init__()
        self.route('GET', '/')(self.search)
        self.route('GET', '/results')(self.results)

    def search(self, request: Request) -> Response:
        return page(
            'Flights',
            '<h1>Find a flight</h1><form method="get" action="/results">'
            '<p><input id="to" name="to" placeholder="To" value="Lisbon"></p>'
            '<p><input id="date" name="date" placeholder="Date" value="next Friday"></p>'
            '<p><button id="search" type="submit">Search</button></p></form>',
        )

    def results(self, request: Request) -> Response:
        number = int(request.query.get('page', '1'))
        to = request.query.get('to', 'Lisbon')
        shown = FLIGHTS[(number - 1) * PER_PAGE : number * PER_PAGE]
        rows = ''.join(f'<tr><td>{f}</td><td>{t}</td><td>€{p}</td></tr>' for f, t, p in shown)
        more = (
            f'<p><a id="next" href="/results?to={esc(to)}&page={number + 1}">Next page</a></p>'
            if number * PER_PAGE < len(FLIGHTS)
            else '<p>No more results.</p>'
        )
        return page(
            f'Flights to {to}',
            f'<h1>Flights to {esc(to)}, page {number}</h1>'
            f'<table><tr><th>Flight</th><th>Departs</th><th>Price</th></tr>{rows}</table>{more}',
        )
