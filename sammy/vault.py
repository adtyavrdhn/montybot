"""Secrets the agent can use but never see: a personal API token, a webhook URL, anything that is not an integration.

```
agent: request_secret('todoist', why, 'api.todoist.com')       sammy.secret_tools
  ask 'secret' (sammy.approvals)                               the chat shows a password field
  ... POST /api/asks/<id> {secret}                             sammy.api: sealed and kept here (`keep`); the ask is
                                                               answered with {saved: true}, never the value
agent: run_code
  await http_request(url, headers={'Authorization': 'Bearer {{secret:todoist}}'})
                                                               sammy.http_calls, inside the code.run step: the value
                                                               goes in on the way out, to its own host only, and is
                                                               scrubbed from the response on the way back
```

The value is never a step's result, an ask's answer, a DBOS message, a span attribute, or anything Monty holds. It is
opened inside the host function that sends it (`reveal`), and taken out of what comes back before Monty sees it
(`scrub`), so it cannot reach the model, the history or Logfire. Sealed with the user's data key (`sammy.crypto`),
with a label naming the user, the secret and its host: a host changed in the database does not open.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from urllib.parse import quote, quote_plus, urlsplit

from cryptography.exceptions import InvalidTag

from sammy import crypto
from sammy.resources import Resources
from sammy.signins import user_key

NAME = re.compile(r'[a-z0-9][a-z0-9_]{0,63}')
PLACEHOLDER = re.compile(r'\{\{secret:([A-Za-z0-9_-]+)\}\}')
MIN_VALUE = 4
"""Shorter would scrub ordinary text from every response."""
MAX_VALUE = 4096


class SecretError(Exception):
    """A secret that cannot be used, in words safe to show the model."""


@dataclass(frozen=True, kw_only=True)
class Saved:
    """A secret as lists show it: never its value."""

    name: str
    host: str

    def json(self) -> dict[str, str]:
        return {'name': self.name, 'host': self.host}


@dataclass(frozen=True, kw_only=True)
class Revealed:
    name: str
    host: str
    value: str = field(repr=False)


def placeholder(name: str) -> str:
    return '{{secret:' + name + '}}'


def name_of(text: str) -> str | None:
    """The secret's name, as the model or the user wrote it (`Todoist token` is `todoist_token`); None if it is not
    one."""
    name = re.sub(r'[\s-]+', '_', text.strip().lower())
    return name if NAME.fullmatch(name) else None


def host_of(text: str) -> str | None:
    """The host a secret may be sent to, from a host (`api.todoist.com`) or a URL; None if there is none."""
    text = text.strip()
    try:
        host = urlsplit(text if '://' in text else f'//{text}').hostname
    except ValueError:
        return None
    return host.lower() if host else None


def label(user_id: str, name: str, host: str) -> str:
    return f'{user_id}:secret:{name}:{host}'


def _key(resources: Resources) -> bytes:
    return crypto.deployment_key(resources.settings.encryption_key.get_secret_value())


# --- storage ---


async def keep(resources: Resources, user_id: str, *, name: str, host: str, value: str) -> None:
    """Save the secret under `name`, for `host`, in place of one of that name."""
    async with resources.pool.connection() as connection, connection.transaction():
        sealed = crypto.seal(
            await user_key(connection, _key(resources), user_id), value.encode(), label=label(user_id, name, host)
        )
        await connection.execute(
            'INSERT INTO sammy.secrets (user_id, name, host, value) VALUES (%s, %s, %s, %s) '
            'ON CONFLICT (user_id, name) DO UPDATE SET host = EXCLUDED.host, value = EXCLUDED.value, '
            'updated_at = now()',
            (user_id, name, host, sealed),
        )


async def listing(resources: Resources, user_id: str) -> list[Saved]:
    async with resources.pool.connection() as connection:
        cursor = await connection.execute(
            'SELECT name, host FROM sammy.secrets WHERE user_id = %s ORDER BY name', (user_id,)
        )
        return [Saved(name=row['name'], host=row['host']) for row in await cursor.fetchall()]


async def find(resources: Resources, user_id: str, name: str) -> Saved | None:
    return next((saved for saved in await listing(resources, user_id) if saved.name == name), None)


async def forget(resources: Resources, user_id: str, name: str) -> bool:
    async with resources.pool.connection() as connection:
        cursor = await connection.execute('DELETE FROM sammy.secrets WHERE user_id = %s AND name = %s', (user_id, name))
        return cursor.rowcount == 1


async def reveal(resources: Resources, user_id: str, names: Collection[str]) -> dict[str, Revealed]:
    """The user's secrets of these names, opened. Raises `SecretError` for one they do not have, naming the ones
    they do. Only for the host function that sends them: nothing it returns may hold a value."""
    async with resources.pool.connection() as connection, connection.transaction():
        cursor = await connection.execute(
            'SELECT name, host, value FROM sammy.secrets WHERE user_id = %s AND name = ANY(%s)',
            (user_id, list(names)),
        )
        rows = await cursor.fetchall()
        key = await user_key(connection, _key(resources), user_id) if rows else b''
    found: dict[str, Revealed] = {}
    for row in rows:
        name, host = str(row['name']), str(row['host'])
        try:
            value = crypto.open_sealed(key, bytes(row['value']), label=label(user_id, name, host)).decode()
        except (InvalidTag, ValueError):
            raise SecretError(
                f'The secret {name!r} could not be read. Ask the user to forget it and give it again.'
            ) from None
        found[name] = Revealed(name=name, host=host, value=value)
    if missing := sorted(set(names) - set(found)):
        saved = ', '.join(sorted(s.name for s in await listing(resources, user_id))) or 'none'
        raise SecretError(f'No secret named {", ".join(missing)}. Saved secrets: {saved}. Use `request_secret`.')
    return found


# --- at the boundary ---


def names_in(texts: Iterable[str]) -> set[str]:
    return {match.group(1) for text in texts for match in PLACEHOLDER.finditer(text)}


def fill(text: str, revealed: dict[str, Revealed]) -> str:
    """`text` with each placeholder replaced by its secret's value. Every name in it must be in `revealed`."""
    return PLACEHOLDER.sub(lambda match: revealed[match.group(1)].value, text)


def scrub(text: str, revealed: Iterable[Revealed]) -> str:
    """`text` with each secret's value, as sent or as a response would quote it (JSON-escaped, URL-encoded), put back
    as its placeholder."""
    for secret in revealed:
        forms = {secret.value, json.dumps(secret.value)[1:-1], quote(secret.value, safe=''), quote_plus(secret.value)}
        for form in sorted(forms, key=len, reverse=True):
            text = text.replace(form, placeholder(secret.name))
    return text
