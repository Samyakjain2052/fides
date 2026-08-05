"""
Credential primitives: password hashing, JWTs, API keys, audit HMAC.

One rule runs through all of it — **nothing reversible is ever stored**. Passwords,
refresh tokens and API keys are all one-way hashed, so a database dump does not
hand over a single working credential.
"""

from __future__ import annotations

import base64
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
def generate_api_key(
    environment: str = "live", tenant_id: uuid.UUID | None = None
) -> tuple[str, str, str]:
    """Returns (full_key, prefix, key_hash).

        ds_live_<tenant-hex>.<43 urlsafe chars>

    **The tenant travels in the key**, for the same reason it travels in a refresh
    token: `api_keys` is under row-level security, so a lookup made before any
    tenant context exists matches nothing. Without the tenant in the key, API-key
    authentication cannot work at all — the query silently returns zero rows and
    every call 401s.

    Carrying it in the clear is safe. The secret half is what authenticates, and
    the table is still RLS-protected: claiming someone else's tenant just means
    the lookup hash matches no row there.

    The prefix is stored so the console can show which key is which, and so a
    leaked key is greppable in logs and public repos. The secret is shown exactly
    once, at creation, and only its hash is kept.
    """
    if environment not in ("live", "test"):
        raise ValueError("environment must be 'live' or 'test'")
    secret = secrets.token_urlsafe(32)
    prefix = f"{API_KEY_PREFIX}_{environment}"
    body = f"{tenant_id.hex}.{secret}" if tenant_id else secret
    full = f"{prefix}_{body}"
    return full, prefix, _hasher.hash(full)


def parse_api_key(full_key: str) -> tuple[uuid.UUID | None, str]:
    """Pull the tenant out of a key without trusting it.

    Returns (tenant_id or None, full_key). A malformed or legacy key yields None
    rather than raising: the caller then fails on the lookup, which is the same
    outcome by a less surprising route.
    """
    try:
        _, _, body = full_key.partition(f"{API_KEY_PREFIX}_")
        env_and_rest = body.split("_", 1)
        candidate = env_and_rest[1] if len(env_and_rest) == 2 else ""
        tenant_hex, sep, _secret = candidate.partition(".")
        if not sep:
            return None, full_key
        return uuid.UUID(hex=tenant_hex), full_key
    except (ValueError, IndexError):
        return None, full_key


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


# --------------------------------------------------------------------------
# Publishable keys
#
# Separate helpers from the secret-key ones above, so the two can never be
# confused at a call site. Different prefix, different verification story.
# --------------------------------------------------------------------------
PUBLISHABLE_KEY_PREFIX = "pk"


def generate_publishable_key(
    environment: str = "live", *, tenant_id: uuid.UUID
) -> tuple[str, str, str]:
    """Returns (full_key, prefix, lookup_hash).

        pk_live_<tenant-hex>.<32 urlsafe chars>

    The tenant travels in the key for the same reason it does in a refresh token
    and a secret API key: the lookup happens before any tenant context exists,
    and `publishable_keys` is under RLS, so a query with no context matches
    nothing. This project has now hit that same bug twice; the pattern is
    deliberate rather than incidental.

    No Argon2 hash is returned, unlike `generate_api_key`. This key is published
    in a browser bundle — hashing it would protect nothing and would stop the
    console from showing it again, which customers need in order to install it.
    """
    if environment not in ("live", "test"):
        raise ValueError("environment must be 'live' or 'test'")
    secret = secrets.token_urlsafe(24)
    prefix = f"{PUBLISHABLE_KEY_PREFIX}_{environment}"
    full = f"{prefix}_{tenant_id.hex}.{secret}"
    return full, prefix, publishable_lookup_hash(full)


def publishable_lookup_hash(full_key: str) -> str:
    """Keyed digest for an indexed lookup, mirroring `api_key_lookup_hash`.

    Not a security control here — the key is public — but keeping the lookup the
    same shape as the secret-key path means one mental model for both.
    """
    return hmac.new(
        _settings.jwt_secret.encode(), full_key.encode(), hashlib.sha256
    ).hexdigest()


def parse_publishable_key(full_key: str) -> uuid.UUID | None:
    """Tenant out of the key, or None. Reading it is not trusting it — the row
    still has to exist in that tenant."""
    try:
        if not full_key.startswith(f"{PUBLISHABLE_KEY_PREFIX}_"):
            return None
        _, _, rest = full_key.partition(f"{PUBLISHABLE_KEY_PREFIX}_")
        _env, _, candidate = rest.partition("_")
        tenant_hex, sep, _secret = candidate.partition(".")
        if not sep:
            return None
        return uuid.UUID(hex=tenant_hex)
    except (ValueError, IndexError):
        return None


def hash_ip(ip: str | None) -> str | None:
    """Keyed HMAC over a client IP.

    Raw IPs are not stored: an IP is personal data, and a consent-collection log
    is the wrong place to build a second identifier for everyone who ever saw a
    banner. Keyed rather than a bare SHA-256 because the IPv4 space is small
    enough to enumerate — an unkeyed digest is reversible in practice.

    Correlation still works (the same IP hashes the same way), which is what
    abuse investigation actually needs.
    """
    if not ip:
        return None
    return hmac.new(_settings.jwt_secret.encode(), ip.encode(), hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------
# Signed consent tokens (the step-up path)
#
# For sensitive-category consent, an asserted principal_ref is not good enough:
# the page could claim to be anyone. The integrator's own server — which has
# actually authenticated the person — mints a short-lived token binding the
# principal_ref, and the banner submits it alongside the consent.
#
# Format: <base64url(payload_json)>.<base64url(hmac_sha256)>
#
# Deliberately not a JWT. A JWT drags in algorithm negotiation, and `alg: none`
# plus a permissive library is a well-worn way to forge one. One algorithm, one
# secret, no negotiation.
# --------------------------------------------------------------------------
CONSENT_TOKEN_TTL_SECONDS = 300


class ConsentTokenError(Exception):
    """Invalid, expired, or not for this principal."""


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64u_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def mint_consent_token(
    *, secret: str, principal_ref: str, ttl_seconds: int = CONSENT_TOKEN_TTL_SECONDS
) -> str:
    """What an integrator's server calls. Also used by the tests.

    TODO(rotation): one secret per tenant, with no versioning. Rotating it
    invalidates every token in flight — acceptable at a 5-minute TTL, but a
    `kid` in the payload and two live secrets would make rotation seamless.
    Deferred rather than half-built.
    """
    payload = {
        "principal_ref": principal_ref,
        "exp": int(datetime.now(UTC).timestamp()) + ttl_seconds,
        # Not for replay prevention — the TTL and idempotency key handle that —
        # but so two tokens for the same person in the same second differ.
        "nonce": secrets.token_urlsafe(8),
    }
    body = _b64u(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64u(sig)}"


def verify_consent_token(*, secret: str, token: str) -> str:
    """Returns the bound principal_ref, or raises ConsentTokenError.

    Signature is checked BEFORE the payload is trusted for anything, and with a
    constant-time compare: a timing-leaky comparison on a signature is how
    forgery becomes practical.
    """
    try:
        body, _, provided = token.partition(".")
        if not body or not provided:
            raise ConsentTokenError("Malformed consent token.")

        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64u_decode(provided), expected):
            raise ConsentTokenError("Consent token signature does not verify.")

        payload = json.loads(_b64u_decode(body))
    except ConsentTokenError:
        raise
    except Exception as exc:  # noqa: BLE001 - any decode failure is a bad token
        raise ConsentTokenError("Malformed consent token.") from exc

    if int(payload.get("exp", 0)) <= int(datetime.now(UTC).timestamp()):
        raise ConsentTokenError("Consent token has expired.")

    principal_ref = payload.get("principal_ref")
    if not principal_ref:
        raise ConsentTokenError("Consent token does not bind a principal.")
    return str(principal_ref)


def generate_signing_secret() -> str:
    return secrets.token_urlsafe(32)
