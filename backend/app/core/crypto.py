"""Reversible encryption, for the one thing in this product that needs it.

Everything else here HASHES its secrets. Passwords, refresh tokens, API keys and
publishable keys are all Argon2 or keyed-SHA256, because the only question ever
asked of them is "does the value the caller presented match?" — and a hash
answers that without the plaintext ever being recoverable, which is why a stolen
database does not yield anybody's password.

Connector credentials break that pattern, and it is worth being explicit about
why rather than quietly reaching for a cipher. To call Razorpay on a customer's
behalf we must send Razorpay *their actual key*. There is no hash-shaped version
of that. So these values are encrypted rather than hashed, and every consequence
follows from that single fact:

  * The key becomes the whole security boundary. A leaked database plus a leaked
    key is a full compromise of every customer's payment gateway and cloud
    account. The key therefore lives in Key Vault and reaches the process as an
    environment variable, never in the database that holds the ciphertext.
  * Plaintext must never leave the server. No endpoint returns a decrypted
    credential, not even to the admin who typed it. They get a last-4 hint and
    nothing more — see `mask`.
  * Decryption is a privileged, audited act, performed only when a connector is
    about to use the value.

AES-256-GCM, which authenticates as well as encrypts: a tampered ciphertext
fails to open rather than decrypting to garbage that some driver then tries to
use as a password.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

# Bumped if the algorithm or key derivation ever changes. Stored alongside every
# ciphertext so old records stay readable through a migration rather than
# becoming undecryptable the day the scheme is improved.
SCHEME = "aesgcm-v1"

_NONCE_BYTES = 12  # 96 bits, the size AES-GCM is specified for


class CredentialSealError(RuntimeError):
    """Encryption or decryption failed. Never carries the plaintext."""


def _key() -> bytes:
    """The 32-byte key, from settings.

    Accepts base64 or raw text so an operator can paste either without a silent
    downgrade in strength; either way the material must be at least 32 bytes.
    A short key is refused rather than padded, because padding a weak key
    produces something that looks like AES-256 and is not.
    """
    raw = get_settings().credential_encryption_key
    if not raw:
        raise CredentialSealError(
            "credential_encryption_key is not configured; connector credentials "
            "cannot be stored without it"
        )
    try:
        material = base64.b64decode(raw, validate=True)
    except Exception:
        material = raw.encode("utf-8")

    if len(material) < 32:
        raise CredentialSealError(
            "credential_encryption_key must be at least 32 bytes of material"
        )
    return material[:32]


def seal(payload: dict[str, Any]) -> str:
    """Encrypt a credential dict into one opaque, storable string.

    The whole dict is sealed as a unit rather than field by field. Per-field
    ciphertexts would leak the shape of the credential — how many fields, which
    ones are present, how long each is — and length alone distinguishes an AWS
    key id from a Razorpay secret.
    """
    plaintext = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    nonce = os.urandom(_NONCE_BYTES)
    try:
        blob = AESGCM(_key()).encrypt(nonce, plaintext, SCHEME.encode())
    except CredentialSealError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CredentialSealError(f"could not encrypt: {type(exc).__name__}") from exc

    # `SCHEME` is also the AAD, so a record cannot be replayed under a future
    # scheme that happens to share a key.
    return f"{SCHEME}:{base64.b64encode(nonce + blob).decode()}"


def open_sealed(sealed: str) -> dict[str, Any]:
    """Decrypt. Raises rather than returning partial data.

    Errors deliberately say nothing about the contents — a decryption error
    message is a lovely oracle if it distinguishes "wrong key" from "corrupt
    nonce" from "not JSON".
    """
    try:
        scheme, encoded = sealed.split(":", 1)
    except ValueError as exc:
        raise CredentialSealError("stored credential is not in a known format") from exc

    if scheme != SCHEME:
        raise CredentialSealError(f"unknown credential scheme {scheme!r}")

    try:
        raw = base64.b64decode(encoded, validate=True)
        nonce, blob = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        plaintext = AESGCM(_key()).decrypt(nonce, blob, SCHEME.encode())
        loaded = json.loads(plaintext)
    except InvalidTag as exc:
        # Authentication failed: wrong key, or the ciphertext was altered.
        raise CredentialSealError(
            "stored credential failed authentication — the encryption key may "
            "have changed, or the record was tampered with"
        ) from exc
    except CredentialSealError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CredentialSealError(f"could not decrypt: {type(exc).__name__}") from exc

    if not isinstance(loaded, dict):
        raise CredentialSealError("stored credential did not decrypt to an object")
    return loaded


def mask(value: str) -> str:
    """What an admin is allowed to see back.

    Enough to recognise which key they pasted, not enough to use it. Short
    values are replaced entirely rather than partially revealed — the last four
    characters of a six-character secret is not a hint, it is most of the secret.
    """
    text = (value or "").strip()
    if len(text) <= 8:
        return "•" * len(text)
    return f"{'•' * 4}{text[-4:]}"
