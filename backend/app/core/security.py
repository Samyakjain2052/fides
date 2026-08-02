"""
Credential primitives: password hashing, JWTs, API keys, audit HMAC.

One rule runs through all of it — **nothing reversible is ever stored**. Passwords,
refresh tokens and API keys are all one-way hashed, so a database dump does not
hand over a single working credential.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.core.config import get_settings

_settings = get_settings()

# Argon2id — the current recommendation over bcrypt/PBKDF2. Cost comes from
# config so tests can turn it down and production can turn it up.
_hasher = PasswordHasher(
    time_cost=_settings.argon2_time_cost,
    memory_cost=_settings.argon2_memory_cost,
    parallelism=_settings.argon2_parallelism,
)

API_KEY_PREFIX = "ds"


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash used weaker parameters than we now require, so
    a successful login can transparently upgrade it."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, ValueError):
        return True


# --------------------------------------------------------------------------
# Access tokens (JWT)
# --------------------------------------------------------------------------
def create_access_token(
    *, user_id: uuid.UUID, tenant_id: uuid.UUID, role: str, ttl_minutes: int | None = None
) -> tuple[str, datetime]:
    """Short-lived bearer token.

    `tenant_id` is inside the token, but it is NOT trusted as the only tenant
    signal — the request pipeline re-derives it and sets the database session
    variable from it, so RLS is the thing that actually enforces scope.
    """
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=ttl_minutes or _settings.access_token_ttl_minutes)
    payload = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "jti": secrets.token_urlsafe(16),
        "typ": "access",
    }
    token = jwt.encode(payload, _settings.jwt_secret, algorithm=_settings.jwt_algorithm)
    return token, expires


def decode_access_token(token: str) -> dict[str, Any]:
    """Raises jwt exceptions on expiry/signature failure — the caller maps those
    to a 401. `require` makes a token missing a claim invalid rather than
    silently defaulting."""
    return jwt.decode(
        token,
        _settings.jwt_secret,
        algorithms=[_settings.jwt_algorithm],
        options={"require": ["exp", "sub", "tenant_id", "typ"]},
    )


# --------------------------------------------------------------------------
# Refresh tokens
# --------------------------------------------------------------------------
def generate_refresh_token(tenant_id: uuid.UUID) -> tuple[str, str]:
    """Returns (plaintext, hash).

    Format: `<tenant-uuid-hex>.<48 random bytes>`

    The tenant id is carried in the clear, and it has to be: refresh happens
    before any tenant context exists, and `refresh_tokens` is under row-level
    security, so a lookup without a tenant returns zero rows — the flow could
    never find its own token. Parsing the tenant from the token lets us bind the
    session first, then look up inside it.

    This leaks nothing. The tenant id is already in every access token, it is not
    a credential, and claiming someone else's tenant just means the hash matches
    no row in it. The secret half is still what authenticates.
    """
    raw = f"{tenant_id.hex}.{secrets.token_urlsafe(48)}"
    return raw, _hasher.hash(raw)


def parse_refresh_token(raw: str) -> uuid.UUID | None:
    """Extract the tenant from a refresh token, or None if it is malformed."""
    tenant_part, _, rest = raw.partition(".")
    if not rest:
        return None
    try:
        return uuid.UUID(hex=tenant_part)
    except ValueError:
        return None


def verify_refresh_token(raw: str, token_hash: str) -> bool:
    try:
        return _hasher.verify(token_hash, raw)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


# --------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------
def generate_api_key(environment: str = "live") -> tuple[str, str, str]:
    """Returns (full_key, prefix, key_hash).

        ds_live_<43 urlsafe chars>

    The prefix is stored so the console can show which key is which, and so a
    leaked key is greppable in logs and public repos. The secret half is shown
    exactly once, at creation, and only its hash is kept.
    """
    if environment not in ("live", "test"):
        raise ValueError("environment must be 'live' or 'test'")
    secret = secrets.token_urlsafe(32)
    prefix = f"{API_KEY_PREFIX}_{environment}"
    full = f"{prefix}_{secret}"
    return full, prefix, _hasher.hash(full)


def verify_api_key(full_key: str, key_hash: str) -> bool:
    try:
        return _hasher.verify(key_hash, full_key)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def api_key_lookup_hash(full_key: str) -> str:
    """A deterministic index for finding the candidate row.

    Argon2 is salted, so you cannot look a key up by its Argon2 hash. A plain
    SHA-256 over the key, keyed with the JWT secret, gives a stable index; the
    Argon2 hash is still what actually authenticates. This is the standard
    two-hash pattern: fast lookup, slow verify.
    """
    return hmac.new(
        _settings.jwt_secret.encode(), full_key.encode(), hashlib.sha256
    ).hexdigest()


# --------------------------------------------------------------------------
# Audit chain
# --------------------------------------------------------------------------
def canonical_json(payload: Any) -> str:
    """Deterministic serialisation.

    The hash must be reproducible months later, so key order and separators are
    fixed. Two structurally identical payloads must hash identically or
    verification produces false alarms.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def audit_hash(*, tenant_id: str, seq: int, action: str, payload: Any, prev_hash: str) -> str:
    """HMAC-SHA256 over this entry plus the previous entry's hash.

    HMAC rather than a bare hash on purpose: with a plain SHA-256 chain, anyone
    who can write rows can also recompute every subsequent hash and erase the
    evidence of their edit. HMAC additionally requires the signing key, which
    lives in the secret manager and never in the database.
    """
    body = canonical_json(
        {"tenant_id": tenant_id, "seq": seq, "action": action, "payload": payload}
    )
    return hmac.new(
        _settings.audit_hmac_key.encode(),
        f"{body}|{prev_hash}".encode(),
        hashlib.sha256,
    ).hexdigest()


GENESIS_HASH = "0" * 64
