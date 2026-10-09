"""`http_request`: HTTPS calls from the agent's code, to public addresses only, with the user's secrets put in at the
boundary (`sammy.vault`).

```
run_code: await http_request(url, method, headers, body, json)    a host function, inside the code.run step
  placeholders {{secret:NAME}} in the URL, headers or body: opened (vault.reveal), each to its own host only
  egress.public_client: public addresses only, no redirects followed, no HTTP client span
  the response, with every value sent scrubbed back to its placeholder (vault.scrub) -> Monty
```

The span (`code.http`) records the host only, as browser steps do. A failure raises `RuntimeError` in the sandbox,
scrubbed the same way, and chained to nothing, so no traceback or message can carry a value.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import httpx2
from opentelemetry import trace

from sammy import vault
from sammy.integrations import egress
from sammy.observability import timed
from sammy.resources import Resources

MAX_BODY = 1_000_000
"""Bytes of a response the code gets; the rest is cut."""
METHODS = frozenset({'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'})
Json = str | int | float | bool | None | list['Json'] | dict[str, 'Json']
Response = dict[str, int | str | dict[str, str]]

INSTRUCTIONS = """\
- `await http_request(url, method='GET', headers=None, body=None, json=None) -> dict`: call an HTTPS API on a public
  address; returns `{'status': int, 'headers': dict, 'body': str}`. Redirects are not followed. Write
  `{{secret:NAME}}` where one of the user's secrets goes (see `request_secret`)."""


def filled_json(value: Json, revealed: dict[str, vault.Revealed]) -> Json:
    if isinstance(value, str):
        return vault.fill(value, revealed)
    if isinstance(value, list):
        return [filled_json(item, revealed) for item in value]
    if isinstance(value, dict):
        return {key: filled_json(item, revealed) for key, item in value.items()}
    return value


def strings_in(value: Json) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in strings_in(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in strings_in(item)]
    return []


def http_functions(resources: Resources, user_id: str) -> dict[str, Callable[..., Awaitable[Response]]]:
    """`http_request`, bound to the run's user: the only secrets it can open are theirs."""
    allow_private = resources.settings.allow_private_networks

    @timed('code.http')
    async def http_request(
        url: str,
        method: str = 'GET',
        headers: dict[str, str] | None = None,
        body: str | None = None,
        json: Json = None,
    ) -> Response:
        method = str(method).upper()
        if method not in METHODS:
            raise RuntimeError(f'method must be one of {", ".join(sorted(METHODS))}')
        headers = {str(name): str(value) for name, value in (headers or {}).items()}
        names = vault.names_in([str(url), *headers.values(), str(body or ''), *strings_in(json)])
        try:
            revealed = await vault.reveal(resources, user_id, names) if names else {}
        except vault.SecretError as error:
            raise RuntimeError(str(error)) from None
        sent = revealed.values()
        real_url = vault.fill(str(url), revealed)
        try:
            egress.check_url(real_url, allow_private=allow_private)
        except ValueError as error:
            raise RuntimeError(vault.scrub(str(error), sent)) from None
        host = (urlsplit(real_url).hostname or '').lower()
        for secret in sent:
            if secret.host != host:
                raise RuntimeError(
                    f'The secret {secret.name!r} may only be sent to {secret.host}. Ask the user with '
                    '`request_secret` for one for this site.'
                )
        trace.get_current_span().set_attribute('http.host', host)  # the host only, never the URL
        try:
            async with (
                egress.public_client(allow_private=allow_private) as http,
                http.stream(
                    method,
                    real_url,
                    headers={name: vault.fill(value, revealed) for name, value in headers.items()},
                    content=None if body is None else vault.fill(str(body), revealed),
                    json=None if json is None else filled_json(json, revealed),
                ) as response,
            ):
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_BODY:
                        break
        except (httpx2.HTTPError, ValueError, TypeError) as error:
            raise RuntimeError(vault.scrub(f'the request failed: {error}', sent)) from None
        text = bytes(data[:MAX_BODY]).decode(response.encoding or 'utf-8', errors='replace')
        if len(data) > MAX_BODY:
            text += f'\n[cut at {MAX_BODY} bytes]'
        return {
            'status': response.status_code,
            'headers': {name: vault.scrub(value, sent) for name, value in response.headers.items()},
            'body': vault.scrub(text, sent),
        }

    return {'http_request': http_request}
