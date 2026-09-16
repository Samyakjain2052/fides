"""The second factor.

Weighted towards the ways a second factor quietly stops being one: a code that
works twice, a challenge token that doubles as a session, a wrong code that can
be guessed without limit, and a recovery code that survives being used.

The TOTP tests drive `at=` rather than sleeping. A suite that waits thirty
seconds to prove a code rolled over is a suite people stop running.
"""

from __future__ import annotations

import base64
import time
import uuid

import pytest
from sqlalchemy import select

from app.core import totp
from app.core.errors import AuthenticationError, Conflict
from app.db.session import set_tenant_context
from app.models.audit import AuditAction, AuditEvent
from app.models.user import User
from app.services import auth_service, mfa_service
from app.services.audit_service import Actor

TEST_KEY = base64.b64encode(b"m" * 32).decode()


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    """The TOTP secret is sealed, so the suite needs a key."""
    from app.core.config import get_settings

    monkeypatch.setenv("DS_CREDENTIAL_ENCRYPTION_KEY", TEST_KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _actor(tenant: dict) -> Actor:
    return Actor(type="user", id=tenant["admin_id"], label="dpo@test")


async def _admin(session, tenant) -> User:
    return await session.scalar(
        select(User).where(User.id == tenant["admin_id"])
    )


async def _enrolled(session, tenant) -> tuple[User, str, list[str]]:
    """An account with MFA actually on, and its secret and recovery codes."""
    user = await _admin(session, tenant)
    started = await mfa_service.begin_enrolment(
        session, tenant_id=tenant["id"], user=user
    )
    secret = started["secret"]
    codes = await mfa_service.confirm_enrolment(
        session,
        tenant_id=tenant["id"],
        actor=_actor(tenant),
        user=user,
        code=totp.current_code(secret),
    )
    return user, secret, codes


# --------------------------------------------------------------------------- #
# The algorithm
# --------------------------------------------------------------------------- #

def test_the_reference_vector_from_rfc_6238():
    """RFC 6238 Appendix B, the SHA-1 row for T=59.

    Asserted against the published vector rather than against our own output,
    which is the only way to know this interoperates with an authenticator app
    rather than merely being self-consistent.
    """
    # The RFC's ASCII seed "12345678901234567890", in base32.
    secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    assert totp.current_code(secret, at=59) == "287082"
    assert totp.current_code(secret, at=1111111109) == "081804"
    assert totp.current_code(secret, at=1111111111) == "050471"


def test_a_code_is_accepted_either_side_of_now():
    """Phone clocks drift. Rejecting a code typed at the rollover is the
    single most common false negative in any TOTP deployment."""
    secret = totp.generate_secret()
    now = time.time()
    for skew in (-totp.STEP_SECONDS, 0, totp.STEP_SECONDS):
        code = totp.current_code(secret, at=now + skew)
        assert totp.verify(secret, code, at=now) is not None


def test_a_code_two_steps_away_is_refused():
    secret = totp.generate_secret()
    now = time.time()
    stale = totp.current_code(secret, at=now - 2 * totp.STEP_SECONDS)
    assert totp.verify(secret, stale, at=now) is None


def test_a_used_counter_is_refused_even_though_the_code_is_valid():
    """The window would otherwise be a replay window: ninety seconds in which
    six digits somebody read over a shoulder still work."""
    secret = totp.generate_secret()
    now = time.time()
    code = totp.current_code(secret, at=now)

    counter = totp.verify(secret, code, at=now)
    assert counter is not None
    assert totp.verify(secret, code, last_counter=counter, at=now) is None


def test_a_code_from_a_different_secret_is_refused():
    a, b = totp.generate_secret(), totp.generate_secret()
    assert totp.verify(a, totp.current_code(b)) is None


def test_a_malformed_code_is_refused_not_crashed():
    secret = totp.generate_secret()
    for code in ("", "abcdef", "12345", "1234567", None):
        assert totp.verify(secret, code) is None


def test_the_provisioning_uri_names_the_organisation_twice():
    """Old apps read the label prefix, newer ones the issuer parameter. An
    account showing as a bare email next to eleven others is a support call."""
    uri = totp.provisioning_uri(
        secret="ABC", account="dpo@acme.example.com", issuer="Acme Fintech"
    )
    assert uri.startswith("otpauth://totp/")
    assert "Acme%20Fintech%3Adpo%40acme.example.com" in uri
    assert "issuer=Acme%20Fintech" in uri
    # Unpadded base32: several popular apps reject a trailing '='.
    assert "secret=ABC&" in uri


def test_a_generated_secret_carries_no_padding():
    assert "=" not in totp.generate_secret()


# --------------------------------------------------------------------------- #
# Enrolment
# --------------------------------------------------------------------------- #

async def test_starting_enrolment_does_not_turn_anything_on(
    app_session_factory, tenant_a
):
    """A user who scans the QR and closes the tab must not be locked out by a
    secret they never stored."""
    async with app_session_factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_a["id"])
        user = await _admin(session, tenant_a)

        started = await mfa_service.begin_enrolment(
            session, tenant_id=tenant_a["id"], user=user
        )
        assert started["enabled"] is False
        assert user.mfa_enabled is False
        await session.rollback()


async def test_a_wrong_code_does_not_complete_enrolment(
    app_session_factory, tenant_a
):
    async with app_session_factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_a["id"])
        user = await _admin(session, tenant_a)
        await mfa_service.begin_enrolment(
            session, tenant_id=tenant_a["id"], user=user
        )

        with pytest.raises(AuthenticationError):
            await mfa_service.confirm_enrolment(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                user=user, code="000000",
            )
        assert user.mfa_enabled is False
        await session.rollback()


async def test_confirming_enrolment_issues_recovery_codes(
    app_session_factory, tenant_a
):
    async with app_session_factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_a["id"])
        user, _, codes = await _enrolled(session, tenant_a)

        assert user.mfa_enabled is True
        assert len(codes) == 10
        # Hashed on the way in — a readable list of these is a readable list of
        # ways past MFA.
        assert all(c not in str(user.mfa_recovery_hashes) for c in codes)
        await session.rollback()


async def test_enrolling_again_while_enabled_is_refused(
    app_session_factory, tenant_a
):
    """Silently replacing the secret would lock out somebody whose authenticator
    still works and who called this by mistake."""
    async with app_session_factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_a["id"])
        user, _, _ = await _enrolled(session, tenant_a)

        with pytest.raises(Conflict):
            await mfa_service.begin_enrolment(
                session, tenant_id=tenant_a["id"], user=user
            )
        await session.rollback()


async def test_the_audit_entry_for_enrolment_carries_no_secret(
    app_session_factory, tenant_a
):
    async with app_session_factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_a["id"])
        _, secret, codes = await _enrolled(session, tenant_a)
        await session.flush()

        rows = await session.execute(
            select(AuditEvent).where(AuditEvent.action == AuditAction.MFA_ENABLED)
        )
        payloads = [str(e.payload) for e in rows.scalars().all()]

        assert payloads
        blob = " ".join(payloads)
        assert secret not in blob
        assert all(code not in blob for code in codes)
        await session.rollback()


# --------------------------------------------------------------------------- #
# The challenge
# --------------------------------------------------------------------------- #

async def test_a_password_alone_no_longer_returns_a_session(
    app_session_factory, tenant_a
):
    """The property the whole feature rests on."""
    async with app_session_factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_a["id"])
        await _enrolled(session, tenant_a)
        await session.commit()

    async with app_session_factory() as session:
        with pytest.raises(mfa_service.MfaRequired) as exc:
            await auth_service.authenticate(
                session,
                tenant_slug=tenant_a["slug"],
                email=tenant_a["admin_email"],
                password=tenant_a["password"],
            )
    assert exc.value.challenge_token
    # So a client can tell this apart from a wrong password without
    # string-matching the message. Both are 401s.
    assert exc.value.extra["mfa_required"] is True


async def test_a_challenge_token_is_not_an_access_token(
    app_session_factory, tenant_a
):
    """`api/deps` refuses any token whose `typ` is not `access`, so the
    half-authenticated state cannot call anything."""
    from app.core.security import decode_access_token

    async with app_session_factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_a["id"])
        user, _, _ = await _enrolled(session, tenant_a)
        token, _ = mfa_service.mint_challenge(user)
        await session.rollback()

    claims = decode_access_token(token)
    assert claims["typ"] == "mfa_challenge"
    assert "role" not in claims


async def test_an_access_token_cannot_be_used_as_a_challenge(
    app_session_factory, tenant_a
):
    """Otherwise a live session could satisfy a step-up challenge by presenting
    itself, which is the opposite of what stepping up means."""
    from app.core.security import create_access_token

    access, _ = create_access_token(
        user_id=uuid.uuid4(), tenant_id=tenant_a["id"], role="admin"
    )
    with pytest.raises(AuthenticationError):
        mfa_service.read_challenge(access)


async def test_a_correct_code_completes_the_login(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_a["id"])
        _, secret, _ = await _enrolled(session, tenant_a)
        await session.commit()

    async with app_session_factory() as session:
        with pytest.raises(mfa_service.MfaRequired) as exc:
            await auth_service.authenticate(
                session, tenant_slug=tenant_a["slug"],
                email=tenant_a["admin_email"], password=tenant_a["password"],
            )
        challenge = exc.value.challenge_token

    async with app_session_factory() as session:
        await session.begin()
        # The NEXT window's code, not this one's.
        #
        # Enrolment consumed the current counter, and the replay guard refuses
        # anything at or below it. So the first sign-in after turning MFA on
        # genuinely does require waiting for the code to roll over — correct
        # behaviour, and worth a test that says so rather than one that quietly
        # avoids it.
        pair = await auth_service.complete_mfa(
            session,
            challenge_token=challenge,
            code=totp.current_code(secret, at=time.time() + totp.STEP_SECONDS),
        )
        assert pair.access_token
        await session.commit()


async def test_the_same_code_cannot_complete_two_logins(
    app_session_factory, tenant_a
):
    """The replay property, end to end. Two challenges, one code."""
    async with app_session_factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_a["id"])
        user, secret, _ = await _enrolled(session, tenant_a)
        first, _ = mfa_service.mint_challenge(user)
        second, _ = mfa_service.mint_challenge(user)
        await session.commit()

    # The next window's, because enrolment already spent the current counter.
    code = totp.current_code(secret, at=time.time() + totp.STEP_SECONDS)

    async with app_session_factory() as session:
        await session.begin()
        await auth_service.complete_mfa(
            session, challenge_token=first, code=code
        )
        await session.commit()

    async with app_session_factory() as session:
        await session.begin()
        with pytest.raises(AuthenticationError):
            await auth_service.complete_mfa(
                session, challenge_token=second, code=code
            )
        await session.rollback()


async def test_a_wrong_code_counts_towards_the_lockout(
    app_session_factory, tenant_a
):
    """Otherwise the second factor is six digits an attacker holding the
    password may guess without limit, taking a fresh challenge each time."""
    async with app_session_factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_a["id"])
        user, _, _ = await _enrolled(session, tenant_a)
        challenge, _ = mfa_service.mint_challenge(user)
        await session.commit()

    async with app_session_factory() as session:
        await session.begin()
        with pytest.raises(AuthenticationError):
            await auth_service.complete_mfa(
                session, challenge_token=challenge, code="000000"
            )
        await session.rollback()

    async with app_session_factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_a["id"])
        user = await _admin(session, tenant_a)
        assert user.failed_login_count >= 1
        await session.rollback()


# --------------------------------------------------------------------------- #
# Recovery codes
# --------------------------------------------------------------------------- #

async def test_a_recovery_code_works_once_and_then_never_again(
    app_session_factory, tenant_a
):
    """Single use means removed, not merely accepted. A JSONB list mutated in
    place is not seen by SQLAlchemy — which would leave the code usable
    forever, and is exactly the bug this asserts against."""
    async with app_session_factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_a["id"])
        user, _, codes = await _enrolled(session, tenant_a)

        assert await mfa_service.verify_challenge(
            session, tenant_id=tenant_a["id"], user=user, code=codes[0]
        ) == "recovery"
        assert len(user.mfa_recovery_hashes) == 9

        with pytest.raises(AuthenticationError):
            await mfa_service.verify_challenge(
                session, tenant_id=tenant_a["id"], user=user, code=codes[0]
            )
        await session.rollback()


async def test_using_a_recovery_code_is_recorded_loudly(
    app_session_factory, tenant_a
):
    """Somebody got in without the enrolled device. That is either a lost phone
    or an attacker with a printed code, and the two look identical at the time."""
    async with app_session_factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_a["id"])
        user, _, codes = await _enrolled(session, tenant_a)
        await mfa_service.verify_challenge(
            session, tenant_id=tenant_a["id"], user=user, code=codes[0]
        )
        await session.flush()

        rows = await session.execute(
            select(AuditEvent).where(
                AuditEvent.action == AuditAction.MFA_RECOVERY_USED
            )
        )
        payloads = [e.payload for e in rows.scalars().all()]
        assert payloads
        assert payloads[0]["codes_remaining"] == 9
        await session.rollback()


async def test_regenerating_invalidates_every_previous_code(
    app_session_factory, tenant_a
):
    async with app_session_factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_a["id"])
        user, _, old = await _enrolled(session, tenant_a)

        fresh = await mfa_service.regenerate_recovery_codes(
            session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            user=user, password=tenant_a["password"],
        )
        assert set(fresh).isdisjoint(old)

        with pytest.raises(AuthenticationError):
            await mfa_service.verify_challenge(
                session, tenant_id=tenant_a["id"], user=user, code=old[0]
            )
        await session.rollback()


async def test_regenerating_needs_the_password(app_session_factory, tenant_a):
    async with app_session_factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_a["id"])
        user, _, _ = await _enrolled(session, tenant_a)

        with pytest.raises(AuthenticationError):
            await mfa_service.regenerate_recovery_codes(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                user=user, password="not-the-password",
            )
        await session.rollback()


# --------------------------------------------------------------------------- #
# Turning it off
# --------------------------------------------------------------------------- #

async def test_disabling_needs_the_password_not_just_a_session(
    app_session_factory, tenant_a
):
    """An unlocked laptop is what MFA exists to survive. A factor a borrowed
    session can remove is not a second factor."""
    async with app_session_factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_a["id"])
        user, _, _ = await _enrolled(session, tenant_a)

        with pytest.raises(AuthenticationError):
            await mfa_service.disable(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                user=user, password="wrong",
            )
        assert user.mfa_enabled is True
        await session.rollback()


async def test_disabling_clears_the_secret_and_the_recovery_codes(
    app_session_factory, tenant_a
):
    """Leaving either behind means re-enabling later silently restores codes
    that were printed, shared and forgotten about months ago."""
    async with app_session_factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_a["id"])
        user, _, _ = await _enrolled(session, tenant_a)

        await mfa_service.disable(
            session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            user=user, password=tenant_a["password"],
        )
        assert user.mfa_enabled is False
        assert user.mfa_secret is None
        assert user.mfa_recovery_hashes is None
        assert user.mfa_last_counter is None
        await session.rollback()


async def test_a_password_login_works_again_once_mfa_is_off(
    app_session_factory, tenant_a
):
    async with app_session_factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_a["id"])
        user, _, _ = await _enrolled(session, tenant_a)
        await mfa_service.disable(
            session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            user=user, password=tenant_a["password"],
        )
        await session.commit()

    async with app_session_factory() as session:
        pair = await auth_service.authenticate(
            session, tenant_slug=tenant_a["slug"],
            email=tenant_a["admin_email"], password=tenant_a["password"],
        )
        assert pair.access_token
