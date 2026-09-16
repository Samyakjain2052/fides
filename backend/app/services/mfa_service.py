"""Enrolling, challenging and disabling a second factor.

BRD §4.6.1 asks for MFA on admin accounts and §4.7 for it on audit-log access.
The columns for it have existed since the first migration and nothing ever wrote
to them, which is the most dangerous shape a security feature can take: a
`mfa_enabled` column that is always false reads, to anybody skimming, like a
control that is present and switched off rather than one that does not exist.

THE ORDER OF OPERATIONS IS THE SECURITY PROPERTY

Enrolment is two steps, and it has to be. Step one mints a secret and returns the
provisioning URI; step two takes a code computed from that secret and only then
sets `mfa_enabled`. Collapsing them — marking the account enabled when the QR is
displayed — locks a user out of their own account whenever they close the tab
before finishing, using a secret they never successfully stored.

Login is two steps for the mirror-image reason. The password is verified first
and no session is issued; a short-lived challenge token stands in until a code is
presented. It carries `typ: "mfa_challenge"`, and `api/deps.py` refuses any token
whose `typ` is not `access` — so the half-authenticated state cannot be used to
call anything.

RECOVERY CODES ARE NOT OPTIONAL
MFA with no recovery path produces permanently locked accounts whose only remedy
is a human at the vendor turning it off — and a support process that disables MFA
on request is a social-engineering target that undoes the control entirely. Ten
single-use codes, Argon2-hashed, consumed on use.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import totp
from app.core.config import get_settings
from app.core.crypto import open_sealed, seal
from app.core.errors import AuthenticationError, Conflict, NotFound
from app.core.security import hash_password, verify_password
from app.models.audit import AuditAction
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit_service
from app.services.audit_service import Actor

#: How long the half-authenticated state lasts. Long enough to open an
#: authenticator app and type six digits, short enough that a challenge token
#: captured from a log is useless by the time anybody reads it.
CHALLENGE_TTL = timedelta(minutes=5)

CHALLENGE_TYP = "mfa_challenge"


class MfaRequired(AuthenticationError):
    """The password was right and a second factor is still owed.

    A 401 carrying the challenge, rather than a 200 with a flag. The request did
    not produce a session, and a success status for "you are not signed in" is
    how a client ends up treating the response as one.

    `mfa_required` is in the body so a client can tell this apart from a wrong
    password without string-matching the message — both are 401s, and only one
    of them means "ask for a code".
    """

    error_type = "/errors/mfa-required"
    title = "Verification code required"

    def __init__(self, challenge_token: str, expires_at: datetime) -> None:
        super().__init__(
            "Enter the code from your authenticator app.",
            mfa_required=True,
            challenge_token=challenge_token,
            challenge_expires_at=expires_at.isoformat(),
        )
        self.challenge_token = challenge_token
        self.expires_at = expires_at


# --------------------------------------------------------------------------- #
# The challenge token
# --------------------------------------------------------------------------- #

def mint_challenge(user: User) -> tuple[str, datetime]:
    settings = get_settings()
    now = datetime.now(UTC)
    expires = now + CHALLENGE_TTL
    token = jwt.encode(
        {
            "sub": str(user.id),
            "tenant_id": str(user.tenant_id),
            # No `role`. This token authorises exactly one thing — presenting a
            # code — and a role claim on it would be an invitation for some
            # future code path to read it as an authorisation.
            "iat": int(now.timestamp()),
            "exp": int(expires.timestamp()),
            "typ": CHALLENGE_TYP,
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    return token, expires


def read_challenge(token: str) -> tuple[uuid.UUID, uuid.UUID]:
    """(user_id, tenant_id) from a challenge token, or raise."""
    settings = get_settings()
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub", "tenant_id", "typ"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthenticationError("That verification session has expired.") from exc

    # Checked explicitly. An access token would otherwise satisfy every other
    # requirement here, which would let a valid session skip the second factor
    # on a step-up challenge.
    if claims.get("typ") != CHALLENGE_TYP:
        raise AuthenticationError("That verification session is not valid.")

    return uuid.UUID(claims["sub"]), uuid.UUID(claims["tenant_id"])


# --------------------------------------------------------------------------- #
# Enrolment
# --------------------------------------------------------------------------- #

async def begin_enrolment(
    session: AsyncSession, *, tenant_id: uuid.UUID, user: User
) -> dict[str, Any]:
    """Mint a secret and return what a QR code needs. Does NOT enable anything.

    Re-enrolling while already enabled is refused rather than silently replacing
    the secret: somebody who still has a working authenticator and calls this by
    accident would otherwise be locked out by their own request.
    """
    if user.mfa_enabled:
        raise Conflict(
            "Two-factor authentication is already on for this account. Turn it "
            "off first if you need to move it to a new device."
        )

    tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
    secret = totp.generate_secret()
    user.mfa_secret = seal({"secret": secret})
    # Cleared, so a secret minted now cannot be verified by a counter recorded
    # against the secret it replaced.
    user.mfa_last_counter = None

    return {
        "secret": secret,
        "provisioning_uri": totp.provisioning_uri(
            secret=secret,
            account=user.email,
            issuer=tenant.name if tenant else "DataShield",
        ),
        # Said plainly, because the two-step flow is otherwise easy to misread
        # as "it is on now".
        "enabled": False,
        "next": "Enter a code from your authenticator to finish turning this on.",
    }


async def confirm_enrolment(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    user: User,
    code: str,
) -> list[str]:
    """Verify a code, switch MFA on, and return the recovery codes — once."""
    if user.mfa_enabled:
        raise Conflict("Two-factor authentication is already on for this account.")
    if not user.mfa_secret:
        raise Conflict("Start enrolment first — there is no secret to check against.")

    secret = open_sealed(user.mfa_secret)["secret"]
    counter = totp.verify(secret, code, last_counter=user.mfa_last_counter)
    if counter is None:
        raise AuthenticationError(
            "That code is not right. Check your device's clock is accurate, then "
            "try the next code."
        )

    codes = totp.generate_recovery_codes()
    user.mfa_enabled = True
    user.mfa_last_counter = counter
    user.mfa_recovery_hashes = [hash_password(c) for c in codes]
    user.mfa_enrolled_at = datetime.now(UTC)

    await audit_service.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action=AuditAction.MFA_ENABLED,
        entity_type="user",
        entity_id=user.id,
        # No secret, no codes. This entry is readable by an auditor.
        payload={"recovery_codes_issued": len(codes)},
    )
    return codes


async def disable(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    user: User,
    password: str,
) -> None:
    """Turn MFA off. Requires the account password, every time.

    Not the current TOTP code — the password. An unlocked session in front of an
    unattended laptop is exactly the situation MFA is meant to survive, and a
    second factor that a borrowed session can remove is not a second factor.
    """
    if not user.password_hash or not verify_password(password, user.password_hash):
        raise AuthenticationError("That password is not right.")

    user.mfa_enabled = False
    user.mfa_secret = None
    user.mfa_last_counter = None
    user.mfa_recovery_hashes = None
    user.mfa_enrolled_at = None

    await audit_service.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action=AuditAction.MFA_DISABLED,
        entity_type="user",
        entity_id=user.id,
        payload={"role": user.role},
    )


# --------------------------------------------------------------------------- #
# The challenge
# --------------------------------------------------------------------------- #

async def verify_challenge(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user: User,
    code: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> str:
    """Check a TOTP code or a recovery code. Returns which was used.

    TOTP first, so a six-digit recovery code — which the format deliberately
    avoids, but a future change might not — can never be burned by a request
    that a normal code would have satisfied.
    """
    if not user.mfa_enabled or not user.mfa_secret:
        raise Conflict("Two-factor authentication is not on for this account.")

    secret = open_sealed(user.mfa_secret)["secret"]
    counter = totp.verify(secret, code, last_counter=user.mfa_last_counter)
    if counter is not None:
        # Recorded BEFORE anything is issued. The window accepts a code for up
        # to ninety seconds; without this the same six digits work twice.
        user.mfa_last_counter = counter
        return "totp"

    used = await _consume_recovery_code(session, user=user, code=code)
    if used:
        await audit_service.record(
            session,
            tenant_id=tenant_id,
            actor=Actor(type="user", id=user.id, label=user.email, ip=ip,
                        user_agent=user_agent),
            action=AuditAction.MFA_RECOVERY_USED,
            entity_type="user",
            entity_id=user.id,
            # The count that remains, because running out is the thing somebody
            # needs to act on before it happens.
            payload={"codes_remaining": len(user.mfa_recovery_hashes or [])},
        )
        return "recovery"

    # Raised WITHOUT an audit write, deliberately, and this is not an omission.
    #
    # The caller records the failure in its own transaction, so that the lockout
    # counter survives this one being rolled back — the same reasoning
    # `authenticate` states for a wrong password. Writing an entry here first
    # would take the tenant's audit advisory lock on THIS transaction, and the
    # caller's separate transaction would then block on it forever waiting for a
    # lock held by the request that is waiting for it. That deadlock was real:
    # the suite hung rather than failed.
    #
    # The failure is on the record either way, with `reason: invalid_mfa_code`
    # distinguishing it from a wrong password.
    raise AuthenticationError("That code is not right.")


async def _consume_recovery_code(
    session: AsyncSession, *, user: User, code: str
) -> bool:
    """Single use: a matching code is removed, not merely accepted.

    Compared against every stored hash rather than stopping at the first match,
    because Argon2 is slow and an early return would make "this code was valid"
    measurably faster than "it was not" — for a credential that is typed by
    hand, from paper, on the worst day of somebody's month.
    """
    hashes = list(user.mfa_recovery_hashes or [])
    if not hashes:
        return False

    cleaned = (code or "").strip().lower()
    matched: int | None = None
    for index, stored in enumerate(hashes):
        if verify_password(cleaned, stored) and matched is None:
            matched = index

    if matched is None:
        return False

    hashes.pop(matched)
    # Reassigned rather than mutated in place: SQLAlchemy does not see a list
    # mutated under a JSONB column, so `hashes.pop(...)` alone would accept the
    # code and leave it usable forever.
    user.mfa_recovery_hashes = hashes
    return True


async def regenerate_recovery_codes(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor: Actor,
    user: User,
    password: str,
) -> list[str]:
    """Issue a fresh set, invalidating every previous one.

    Password-gated for the same reason `disable` is: printed codes get lost, and
    somebody who found a set should not be able to replace them with a set only
    they hold.
    """
    if not user.mfa_enabled:
        raise Conflict("Two-factor authentication is not on for this account.")
    if not user.password_hash or not verify_password(password, user.password_hash):
        raise AuthenticationError("That password is not right.")

    codes = totp.generate_recovery_codes()
    user.mfa_recovery_hashes = [hash_password(c) for c in codes]

    await audit_service.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action=AuditAction.MFA_RECOVERY_REGENERATED,
        entity_type="user",
        entity_id=user.id,
        payload={"recovery_codes_issued": len(codes)},
    )
    return codes


async def status(user: User) -> dict[str, Any]:
    return {
        "enabled": user.mfa_enabled,
        "enrolled_at": (
            user.mfa_enrolled_at.isoformat() if user.mfa_enrolled_at else None
        ),
        "recovery_codes_remaining": len(user.mfa_recovery_hashes or []),
    }


async def user_for_challenge(
    session: AsyncSession, *, user_id: uuid.UUID
) -> User:
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None or not user.is_active:
        raise NotFound("No such user.")
    return user
