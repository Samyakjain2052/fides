"""TOTP (RFC 6238), from the standard library.

No new dependency. The algorithm is thirty lines of HMAC and a truncation rule
that has not changed since 2011, and this codebase already states its position on
dependencies bought for nothing — a package here would be one more thing to audit
and pin for code we can read in one screen.

WHY SHA-1, WHICH LOOKS WRONG AND IS NOT
RFC 6238 permits SHA-256 and SHA-512, and every authenticator app in common use
implements SHA-1 only. Choosing a stronger digest here produces codes that Google
Authenticator, Authy and 1Password all compute differently, which presents to the
user as "my correct code is rejected". The collision resistance of the hash is
irrelevant to HMAC's security in any case; this is HMAC-SHA1, not SHA-1.

THE WINDOW, AND WHY REPLAY IS PREVENTED SEPARATELY
A code is accepted one step either side of now, because phone clocks drift and a
user who types a valid code at the moment it rolls over should not be told it is
wrong. That tolerance means a code stays valid for up to ninety seconds — long
enough for somebody who watched it being typed, or read it from a shoulder, to
use it again. So `verify` returns the counter it matched and the caller records
it: a counter at or below the last one accepted is refused even though the digest
is perfectly valid. Without that, the window is a replay window.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

#: Seconds per code. Thirty is the near-universal default and what every
#: authenticator app assumes when a provisioning URI omits the period.
STEP_SECONDS = 30

DIGITS = 6

#: How many steps either side of now are accepted. One — so ±30 seconds of clock
#: skew, and a code valid for at most 90 seconds in total. Larger windows are
#: sometimes suggested for "user friendliness"; they buy a few seconds of
#: convenience with a proportionally longer replay window.
WINDOW = 1


def generate_secret() -> str:
    """A fresh base32 secret, in the shape authenticator apps expect.

    160 bits, which is what RFC 4226 recommends for HMAC-SHA1. Unpadded: a
    trailing `=` is legal base32 and several popular apps reject it in a
    provisioning URI.
    """
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code_for(secret: str, counter: int) -> str:
    # Re-pad: we strip `=` for the URI, and b32decode insists on it.
    padded = secret + "=" * (-len(secret) % 8)
    key = base64.b32decode(padded, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    # Dynamic truncation, RFC 4226 §5.3. The low nibble of the last byte picks
    # the offset; the high bit of the selected word is masked off so the result
    # is the same on platforms that would otherwise read it as a sign bit.
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFF_FFFF
    return str(value % (10 ** DIGITS)).zfill(DIGITS)


def now_counter(at: float | None = None) -> int:
    return int((at if at is not None else time.time()) // STEP_SECONDS)


def current_code(secret: str, *, at: float | None = None) -> str:
    """The code an authenticator would be showing right now. For tests."""
    return _code_for(secret, now_counter(at))


def verify(
    secret: str,
    code: str,
    *,
    last_counter: int | None = None,
    at: float | None = None,
) -> int | None:
    """Return the counter this code matched, or None.

    The counter is the return value rather than a bare bool precisely so the
    caller cannot forget to record it. `last_counter` is the highest one already
    accepted for this user; anything at or below it is refused — see the module
    docstring on why the acceptance window would otherwise be a replay window.
    """
    cleaned = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(cleaned) != DIGITS:
        return None

    centre = now_counter(at)
    for offset in range(-WINDOW, WINDOW + 1):
        counter = centre + offset
        if counter < 0:
            continue
        if last_counter is not None and counter <= last_counter:
            continue
        # Timing-safe, because the comparison decides whether to admit somebody.
        if hmac.compare_digest(_code_for(secret, counter), cleaned):
            return counter
    return None


def provisioning_uri(*, secret: str, account: str, issuer: str) -> str:
    """The `otpauth://` URI a QR code encodes.

    The issuer appears twice — as a label prefix and as a parameter — because
    older apps read one and newer apps read the other, and an account that shows
    up in somebody's authenticator as a bare email address next to eleven others
    is a support call waiting to happen.
    """
    label = quote(f"{issuer}:{account}", safe="")
    return (
        f"otpauth://totp/{label}"
        f"?secret={secret}"
        f"&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    )


def generate_recovery_codes(count: int = 10) -> list[str]:
    """Single-use codes for the day the phone is lost.

    Not optional. MFA without a recovery path produces a permanently locked
    account whose only remedy is somebody at the vendor turning it off by hand —
    and a support process that disables MFA on request is a social-engineering
    target that undoes the control entirely.

    Grouped with a hyphen because these get written down and read back.
    """
    return [
        f"{secrets.token_hex(3)}-{secrets.token_hex(3)}"
        for _ in range(count)
    ]
