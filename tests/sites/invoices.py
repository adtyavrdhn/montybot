"""U5 files: an account page with invoices to download as CSV and PDF ("download my last three invoices and total
them")."""

from __future__ import annotations

from dataclasses import dataclass

from sites.base import Request, Response, Site, page

INVOICES = [
    ('2026-07', [('Coffee beans', 2, 7.90), ('Milk', 6, 1.10)]),
    ('2026-08', [('Coffee beans', 1, 7.90), ('Bread', 3, 2.50), ('Eggs', 2, 3.20)]),
    ('2026-09', [('Eggs', 4, 3.20), ('Milk', 2, 1.10)]),
    ('2026-10', [('Coffee beans', 3, 7.90)]),
]


def total(lines: list[tuple[str, int, float]]) -> float:
    return round(sum(quantity * price for _, quantity, price in lines), 2)


def last_three_total() -> float:
    return round(sum(total(lines) for _, lines in INVOICES[-3:]), 2)


def csv_of(lines: list[tuple[str, int, float]]) -> str:
    return 'item,quantity,unit_price\n' + ''.join(f'{i},{q},{p:.2f}\n' for i, q, p in lines)


def pdf_of(month: str, lines: list[tuple[str, int, float]]) -> bytes:
    """A minimal one-page PDF whose text is the invoice."""
    text = [f'Invoice {month}'] + [f'{i} x{q} {p:.2f}' for i, q, p in lines] + [f'Total {total(lines):.2f}']
    stream = 'BT /F1 12 Tf 72 720 Td ' + ' '.join(f'({t}) Tj 0 -16 Td' for t in text) + ' ET'
    objects = [
        '<< /Type /Catalog /Pages 2 0 R >>',
        '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        (
            '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R '
            '/Resources << /Font << /F1 5 0 R >> >> >>'
        ),
        f'<< /Length {len(stream)} >>\nstream\n{stream}\nendstream',
        '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    out = b'%PDF-1.4\n'
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f'{number} 0 obj\n{body}\nendobj\n'.encode()
    xref = len(out)
    out += f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode()
    out += ''.join(f'{o:010d} 00000 n \n' for o in offsets).encode()
    out += f'trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    return out


@dataclass
class Invoices(Site):
    downloads: int = 0

    def __post_init__(self) -> None:
        super().__init__()
        self.route('GET', '/')(self.index)
        self.route('GET', '/invoices/*')(self.download)

    def index(self, request: Request) -> Response:
        rows = ''.join(
            f'<tr><td>{month}</td><td>€{total(lines):.2f}</td>'
            f'<td><a id="csv-{month}" href="/invoices/{month}.csv">CSV</a> '
            f'<a id="pdf-{month}" href="/invoices/{month}.pdf">PDF</a></td></tr>'
            for month, lines in reversed(INVOICES)
        )
        return page('Your invoices', f'<h1>Your invoices</h1><table>{rows}</table>')

    def download(self, request: Request) -> Response:
        name = request.path.rsplit('/', 1)[-1]
        month, _, kind = name.partition('.')
        lines = dict(INVOICES).get(month)
        if lines is None or kind not in ('csv', 'pdf'):
            return page('Not found', '<h1>Not found</h1>', status=404)
        self.downloads += 1
        disposition = [('Content-Disposition', f'attachment; filename="invoice-{month}.{kind}"')]
        if kind == 'csv':
            return Response(csv_of(lines), content_type='text/csv', headers=disposition)
        return Response(data=pdf_of(month, lines), content_type='application/pdf', headers=disposition)
