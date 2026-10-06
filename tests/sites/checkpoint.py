"""U6 blocked by a bot check: a store behind a "press and hold" check, like Walmart's.

Every page sends a visitor without the `human` cookie to `/checkpoint`. There, the button must be held for
`HOLD_MS`; then the page posts to `/checkpoint/pass`, which sets the cookie and goes back. In a real browser the
page's script times the hold; `HtmlBrowser` times it the same way from the button's `data-hold-ms` and `data-post`.
The button covers the middle of the screen, so a person (or the scripted human) presses wherever they look.
"""

from __future__ import annotations

from dataclasses import dataclass

from sites.base import Request, Response, Site, esc, page, redirect

HOLD_MS = 1000

_HOLD_SCRIPT = """<script>
const button = document.getElementById('hold');
let since = null;
button.addEventListener('mousedown', () => { since = Date.now(); button.textContent = 'Keep holding...'; });
button.addEventListener('mouseup', () => {
  const held = since === null ? 0 : Date.now() - since;
  since = null;
  if (held >= Number(button.dataset.holdMs)) {
    const form = document.createElement('form');
    form.method = 'post';
    form.action = button.dataset.post;
    document.body.appendChild(form);
    form.submit();
  } else {
    button.textContent = 'Press and hold';
  }
});
</script>"""


@dataclass
class Checkpoint(Site):
    passes: int = 0

    def __post_init__(self) -> None:
        super().__init__()
        self.route('GET', '/')(self.home)
        self.route('GET', '/checkpoint')(self.check)
        self.route('POST', '/checkpoint/pass')(self.passed)

    def home(self, request: Request) -> Response:
        if request.cookies.get('human') != '1':
            return redirect('/checkpoint')
        return page('Guarded store', '<h1>Guarded store</h1><p>Today only: oat milk, 2 for €3.</p>')

    def check(self, request: Request) -> Response:
        return page(
            'Robot or human?',
            '<h1>Robot or human?</h1><p>Press and hold the button to show you are human.</p>'
            f'<button id="hold" data-hold-ms="{HOLD_MS}" data-post="{esc("/checkpoint/pass")}" '
            'style="position:fixed;left:10%;top:25%;width:80%;height:50%;font-size:24px">Press and hold</button>',
            script=_HOLD_SCRIPT,
        )

    def passed(self, request: Request) -> Response:
        self.passes += 1
        return redirect('/', cookie='human=1; HttpOnly; Path=/; SameSite=Lax; Max-Age=86400')
