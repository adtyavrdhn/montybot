"""The shop (U2 sign in once, U3 act with approval, U4 the weekly cart), grown from `poc/montybot_poc/demo_site.py`.

- Sign-in sets an HttpOnly `sid` cookie, so only the browser engine carries it, not page scripts.
- The cart lives on the server, per account, as on real shops. Adding an item that is already in the cart does not
  add it twice, so the tests can see whether a step was repeated through the order, not the cart.
- Checkout places an order. `orders` records every order placed, for tests to count.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from sites.base import Request, Response, Site, esc, page, redirect

PASSWORD = 'hunter2'
PRICES = {'eggs': 3.20, 'milk': 1.10, 'bread': 2.50, 'coffee': 7.90}


@dataclass
class Order:
    number: int
    user: str
    items: list[str]
    total: float


@dataclass
class Shop(Site):
    sessions: dict[str, str] = field(default_factory=dict[str, str])
    carts: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    orders: list[Order] = field(default_factory=list[Order])
    sign_ins: int = 0

    def __post_init__(self) -> None:
        super().__init__()
        self.route('GET', '/')(self.home)
        self.route('GET', '/login')(self.login_form)
        self.route('POST', '/login')(self.login)
        self.route('POST', '/cart/add')(self.add)
        self.route('GET', '/cart')(self.cart)
        self.route('POST', '/checkout')(self.checkout)
        self.route('GET', '/orders')(self.order_list)

    def user(self, request: Request) -> str | None:
        return self.sessions.get(request.cookies.get('sid', ''))

    def home(self, request: Request) -> Response:
        user = self.user(request)
        who = (
            f'<p>Signed in as {esc(user)}. <a id="cart" href="/cart">Cart</a> <a id="orders" href="/orders">Your orders</a></p>'
            if user
            else '<p><a id="sign-in" href="/login">Sign in</a></p>'
        )
        rows = ''.join(
            f'<tr><td>{item}</td><td>${price:.2f}</td><td><form method="post" action="/cart/add">'
            f'<input type="hidden" name="item" value="{item}"><button id="add-{item}">Add {item}</button></form></td></tr>'
            for item, price in PRICES.items()
        )
        return page('Shop', f'<h1>Shop</h1>{who}<table>{rows}</table>')

    def login_form(self, request: Request, error: str = '') -> Response:
        next_path = esc(request.query.get('next') or request.form.get('next') or '/')
        return page(
            'Sign in',
            f'<h1>Sign in</h1>{error}<form method="post" action="/login">'
            f'<input type="hidden" name="next" value="{next_path}">'
            '<p><input id="username" name="username" placeholder="username" autofocus></p>'
            '<p><input id="password" name="password" type="password" placeholder="password"></p>'
            '<p><button id="submit" type="submit">Sign in</button></p></form>',
            status=401 if error else 200,
        )

    def login(self, request: Request) -> Response:
        if request.form.get('password') != PASSWORD or not request.form.get('username'):
            return self.login_form(request, '<p>Wrong username or password.</p>')
        sid = secrets.token_urlsafe(16)
        self.sessions[sid] = request.form['username']
        self.sign_ins += 1
        next_path = request.form.get('next') or '/'
        if not next_path.startswith('/'):
            next_path = '/'
        return redirect(next_path, cookie=f'sid={sid}; HttpOnly; Path=/; SameSite=Lax; Max-Age=2592000')

    def signed_in(self, request: Request) -> str | Response:
        user = self.user(request)
        if user is None:
            return redirect(f'/login?next={request.path if request.method == "GET" else "/"}')
        return user

    def add(self, request: Request) -> Response:
        user = self.signed_in(request)
        if isinstance(user, Response):
            return user
        item = request.form.get('item', '')
        cart = self.carts.setdefault(user, [])
        if item in PRICES and item not in cart:
            cart.append(item)
        return redirect('/cart')

    def cart(self, request: Request) -> Response:
        user = self.signed_in(request)
        if isinstance(user, Response):
            return user
        cart = self.carts.get(user, [])
        total = sum(PRICES[i] for i in cart)
        items = ', '.join(cart) or 'empty'
        button = (
            f'<form method="post" action="/checkout"><button id="place-order">Place order (${total:.2f})</button></form>'
            if cart
            else ''
        )
        return page(
            'Cart',
            f'<h1>Cart</h1><p>In cart: {items}</p><p>Total: ${total:.2f}</p>{button}<p><a id="shop" href="/">Shop</a></p>',
        )

    def checkout(self, request: Request) -> Response:
        user = self.signed_in(request)
        if isinstance(user, Response):
            return user
        cart = self.carts.pop(user, [])
        if not cart:
            return redirect('/cart')
        order = Order(len(self.orders) + 1, user, cart, round(sum(PRICES[i] for i in cart), 2))
        self.orders.append(order)
        return redirect('/orders')

    def order_list(self, request: Request) -> Response:
        user = self.signed_in(request)
        if isinstance(user, Response):
            return user
        mine = [o for o in self.orders if o.user == user]
        rows = ''.join(f'<li>Order #{o.number}: {", ".join(o.items)}, ${o.total:.2f}</li>' for o in reversed(mine))
        body = f'<ul>{rows}</ul>' if mine else '<p>No orders yet.</p>'
        return page('Your orders', f'<h1>Your orders</h1>{body}<p><a id="shop" href="/">Shop</a></p>')
