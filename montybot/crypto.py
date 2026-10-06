"""Envelope encryption for each user's saved sign-ins.

```
ENCRYPTION_KEY (the deployment's key, from the environment; a KMS key later)
  └─ wraps each user's data key           montybot.users.data_key
       └─ encrypts that user's sign-ins   montybot.sign_ins.state
```

AES-256-GCM throughout. The associated data (`label`) names the user and what the ciphertext is (`<user>:data_key`,
`<user>:sign_ins:<version>`), so a ciphertext copied to another user's row, or an older version put back, does not
decrypt. Keys never leave this module in a log or an error message.
"""

from __future__ import annotations

import base64
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = b'\x01'
NONCE = 12


class BadKey(Exception):
    """The deployment's key is missing or malformed."""


def deployment_key(encoded: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
    except ValueError as error:
        raise BadKey('ENCRYPTION_KEY must be 32 bytes, base64url-encoded') from error
    if len(key) != 32:
        raise BadKey('ENCRYPTION_KEY must be 32 bytes, base64url-encoded')
    return key


def new_key() -> str:
    """A fresh value for ENCRYPTION_KEY."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def seal(key: bytes, plaintext: bytes, *, label: str) -> bytes:
    nonce = os.urandom(NONCE)
    return VERSION + nonce + AESGCM(key).encrypt(nonce, plaintext, label.encode())


def open_sealed(key: bytes, sealed: bytes, *, label: str) -> bytes:
    if sealed[:1] != VERSION:
        raise ValueError('unknown ciphertext version')
    nonce, body = sealed[1 : 1 + NONCE], sealed[1 + NONCE :]
    return AESGCM(key).decrypt(nonce, body, label.encode())
