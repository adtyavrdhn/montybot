"""The secret vault's boundary helpers (`sammy/vault.py`, `sammy/http_calls.py`): names, hosts, placeholders filled on
the way out and values scrubbed on the way back. The end-to-end path is `tests/e2e/test_secrets.py`."""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from sammy import vault
from sammy.http_calls import filled_json, strings_in

SECRET = vault.Revealed(name='notes_token', host='api.example.com', value='s3cr3t/+&"value')


@pytest.mark.parametrize(
    ('text', 'name'),
    [('notes_token', 'notes_token'), ('Todoist token', 'todoist_token'), ('a-b', 'a_b'), ('', None), ('{x}', None)],
)
def test_name_of(text: str, name: str | None) -> None:
    assert vault.name_of(text) == name


@pytest.mark.parametrize(
    ('text', 'host'),
    [
        ('api.example.com', 'api.example.com'),
        ('API.Example.com', 'api.example.com'),
        ('https://api.example.com/v1?x=1', 'api.example.com'),
        ('127.0.0.1:8080', '127.0.0.1'),
        ('', None),
        ('https://', None),
    ],
)
def test_host_of(text: str, host: str | None) -> None:
    assert vault.host_of(text) == host


def test_placeholders_are_filled_on_the_way_out() -> None:
    text = 'Bearer {{secret:notes_token}}'
    assert vault.names_in([text, 'none here', '{{secret:other}}']) == {'notes_token', 'other'}
    assert vault.fill(text, {'notes_token': SECRET}) == f'Bearer {SECRET.value}'
    payload = {'auth': [text, 3, None], 'n': {'deep': text}}
    assert strings_in(payload) == [text, text]
    assert filled_json(payload, {'notes_token': SECRET}) == {
        'auth': [f'Bearer {SECRET.value}', 3, None],
        'n': {'deep': f'Bearer {SECRET.value}'},
    }


def test_values_are_scrubbed_on_the_way_back_as_sent_or_quoted() -> None:
    echoed = json.dumps({'raw': SECRET.value}) + f' {SECRET.value} {quote(SECRET.value, safe="")}'
    scrubbed = vault.scrub(echoed, [SECRET])
    assert scrubbed.count('{{secret:notes_token}}') == 3
    assert 's3cr3t' not in scrubbed


def test_a_revealed_value_is_not_in_its_repr() -> None:
    assert SECRET.value not in repr(SECRET)
