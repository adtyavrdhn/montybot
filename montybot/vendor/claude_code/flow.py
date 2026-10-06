"""Authorization-code PKCE flow for Claude Code credentials.

The browser flow hosts a short-lived localhost callback server, the same shape as
the Codex provider's `_REDIRECT_URI`. When the browser cannot reach it (SSH, a remote
box), the user can paste the address the browser ended on instead: see `read_pasteback`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import threading
import webbrowser
from collections.abc import Awaitable, Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode

import httpx2

from pydantic_ai.exceptions import UserError

from . import config
from .credentials import ClaudeCodeCredentials
from .storage import TokenStore, default_store


class ClaudeCodeOAuthFlow:
    """A side-effect-free authorization-code PKCE flow context."""

    def __init__(self, redirect_uri: str | None = None) -> None:
        self.redirect_uri = redirect_uri
        self.state = secrets.token_urlsafe(32)
        self.code_verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(self.code_verifier.encode()).digest()
        self.code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    def authorization_url(self) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": config.CLIENT_ID,
                "redirect_uri": self.redirect_uri or "",
                "scope": config.SCOPES,
                "state": self.state,
                "code": "true",
                "code_challenge": self.code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{config.AUTH_URL}?{query}"

    async def exchange_code(self, code: str, *, http_client: httpx2.AsyncClient | None = None) -> ClaudeCodeCredentials:
        """Exchange an authorization code for credentials."""
        return await exchange_code(
            code, self.code_verifier, self.redirect_uri, state=self.state, http_client=http_client
        )


async def exchange_code(
    code: str,
    code_verifier: str,
    redirect_uri: str | None,
    *,
    state: str | None = None,
    http_client: httpx2.AsyncClient | None = None,
) -> ClaudeCodeCredentials:
    """Exchange an authorization code for Claude Code credentials."""
    async with _client_context(http_client) as client:
        response = await client.post(
            config.TOKEN_URL,
            json={
                "grant_type": "authorization_code",
                "client_id": config.CLIENT_ID,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
                "state": state,
            },
            headers=_token_headers(),
        )
        _raise_for_token_error(response)
        return ClaudeCodeCredentials.from_token_response(response.json())


async def refresh_credentials(
    credentials: ClaudeCodeCredentials, *, http_client: httpx2.AsyncClient | None = None
) -> ClaudeCodeCredentials:
    """Refresh Claude Code credentials without persisting them."""
    async with _client_context(http_client) as client:
        response = await client.post(
            config.TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "client_id": config.CLIENT_ID,
                "refresh_token": credentials.refresh_token.get_secret_value(),
            },
            headers=_token_headers(),
        )
        _raise_for_token_error(response)
        return ClaudeCodeCredentials.from_token_response(response.json(), previous=credentials)


def _raise_for_token_error(response: httpx2.Response) -> None:
    """Raise a `UserError` that includes the token server's error body.

    The token endpoints return sparse HTTP codes, so the reason lives in the
    JSON body; without it a 400 tells us nothing.
    """
    if response.status_code < 400:
        return
    body = response.text[:500]
    raise UserError(f"Claude Code token endpoint returned {response.status_code}: {body}")


def _token_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "anthropic-beta": config.ANTHROPIC_BETA,
        "User-Agent": config.USER_AGENT,
    }


def parse_pasteback(raw: str) -> tuple[str, str] | None:
    """Parse what the user pasted into `(code, state)`; `None` when it holds no authorization code.

    Accepts the address the browser ended on (`http://localhost:PORT/callback?code=...&state=...`, or
    `claude://...`), just its query string, or the `code#state` form Anthropic's code page shows.
    """
    text = raw.strip()
    if "?" in text or text.startswith("code="):
        params = parse_qs(text.partition("?")[2] if "?" in text else text)
        code, state = params.get("code", [""])[0], params.get("state", [""])[0]
    elif "#" in text:
        code, _, state = text.partition("#")
    else:
        return None
    if not code or any(char.isspace() for char in code + state):
        return None
    return code, state


class _CallbackServer(ThreadingHTTPServer):
    """Localhost server that captures the OAuth redirect query string."""

    def __init__(self) -> None:
        self.query: str | None = None
        self.received = threading.Event()
        super().__init__(("127.0.0.1", 0), _CallbackHandler)
        self.daemon_threads = True


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackServer  # pyright: ignore[reportIncompatibleVariableOverride] - always our server

    def do_GET(self) -> None:  # noqa: N802
        if not self.server.received.is_set():
            self.server.query = self.path
            self.server.received.set()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h1>pydantic-ai-claude-code</h1><p>Auth successful. Return to your terminal.</p></body></html>"
        )

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass


def start_login_callback_server() -> _CallbackServer:
    """Start a callback server plus a daemon thread serving it."""
    server = _CallbackServer()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _print_url(url: str) -> None:
    print(f"Open this URL in your browser if it did not open automatically:\n{url}")


async def login(
    *,
    store: TokenStore | None = None,
    on_url: Callable[[str], None] = _print_url,
    read_pasteback: Callable[[], Awaitable[str | None]] | None = None,
) -> ClaudeCodeCredentials:
    """Run the full OAuth flow and persist the minted credentials.

    Waiting for the browser happens off the event loop, so a terminal UI keeps drawing meanwhile.

    Args:
        store: The token store to write to. Defaults to the standard store.
        on_url: Shows the authorization URL, in case the browser does not open. Prints it by default.
        read_pasteback: Reads the address the browser ended on, for a browser that cannot reach this
            machine's localhost callback (SSH, a remote box). Returns `None` to cancel. Runs alongside
            the callback, which cancels it when the browser gets through first. Without it the
            callback is the only way in, and it times out.

    Returns:
        The credentials that were persisted.
    """
    if store is None:
        store = default_store()
    server = start_login_callback_server()
    timeout = None if read_pasteback is not None else config.CALLBACK_TIMEOUT
    callback = asyncio.ensure_future(asyncio.to_thread(server.received.wait, timeout))
    paste = asyncio.ensure_future(read_pasteback()) if read_pasteback is not None else None
    try:
        redirect_uri = f"{config.REDIRECT_HOST}:{server.server_address[1]}/{config.REDIRECT_PATH}"
        flow = ClaudeCodeOAuthFlow(redirect_uri=redirect_uri)
        url = flow.authorization_url()
        on_url(url)
        # Launch the browser off the event path: on some macOS setups `webbrowser.open`
        # hangs until the UI settles, which would starve the callback wait below.
        threading.Thread(target=_open_browser, args=(url,), daemon=True).start()
        await asyncio.wait({callback} if paste is None else {callback, paste}, return_when=asyncio.FIRST_COMPLETED)
        if server.query is not None:
            code, state = _parse_callback_query(server.query)
        elif paste is not None and paste.done():
            code, state = _pasted(paste.result())
        else:
            raise UserError("Claude Code OAuth callback timed out.")
        if state and state != flow.state:
            raise UserError("Claude Code OAuth state mismatch; the redirect may be a replay.")
        if not code:
            raise UserError(f"Claude Code OAuth redirect did not include a `code` parameter: {server.query!r}")
        credentials = await flow.exchange_code(code)
    finally:
        server.shutdown()
        server.server_close()
        # Release the waiting thread at once when the login was cancelled or a paste won.
        server.received.set()
        if paste is not None and not paste.done():
            paste.cancel()
        await asyncio.gather(callback, *(() if paste is None else (paste,)), return_exceptions=True)
    store.save(credentials)
    return credentials


def _pasted(text: str | None) -> tuple[str, str]:
    if text is None:
        raise UserError("Claude Code sign-in cancelled.")
    parsed = parse_pasteback(text)
    if parsed is None:
        raise UserError("That paste has no authorization code; copy the whole address your browser ended on.")
    return parsed


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - best-effort: the URL is printed for manual use
        pass


def _parse_callback_query(raw: str | None) -> tuple[str, str]:
    """Extract `(code, state)` from a captured redirect path like `/callback?code=...`."""
    params = parse_qs((raw or "").split("?", 1)[-1])
    return params.get("code", [""])[0], params.get("state", [""])[0]


class _client_context:
    def __init__(self, client: httpx2.AsyncClient | None) -> None:
        self.client = client or httpx2.AsyncClient()
        self.owned = client is None

    async def __aenter__(self) -> httpx2.AsyncClient:
        return self.client

    async def __aexit__(self, *_: object) -> None:
        if self.owned:
            await self.client.aclose()
