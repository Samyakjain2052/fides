"""Consent renewal — BRD §4.1.4.

The property worth defending here is a negative one: none of this expires
anything. Expiry in this product is computed against the clock on every read,
and a renewal sweep that started writing `status` would make a consent's
validity depend on whether a worker had run. Several tests below exist purely to
catch that drift.

The rest is about not being annoying, which is a correctness concern rather than
a polish one: a reminder that fires every day for a month, or that arrives in
the same minute the consent was granted, teaches people to ignore the ones that
matter.
"""

from __future__ import annotations

import base64
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.session import set_tenant_context
from app.models.audit import AuditAction, AuditEvent
from app.models.consent import Consent, DataPrincipal
from app.models.notification import Notification
from app.models.webhook import WebhookDelivery
from app.services import consent_service, notice_service, webhook_service
from app.services.audit_service import Actor

TEST_KEY = base64.b64encode(b"r" * 32).decode()


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    """`announce_expiries` emits webhooks, and an endpoint's secret is sealed."""
    from app.core.config import get_settings

    monkeypatch.setenv("DS_CREDENTIAL_ENCRYPTION_KEY", TEST_KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _actor(tenant: dict) -> Actor:
    return Actor(type="user", id=tenant["admin_id"], label="dpo@test")


@asynccontextmanager
async def scoped(factory, tenant_id):
    async with factory() as session:
        await session.begin()
        await set_tenant_context(session, tenant_id)
        try:
            yield session
        finally:
            if session.in_transaction():
                await session.rollback()


async def _consent(session, tenant, *, retention_days: int, key: str = "marketing"):
    """A live consent with a real expiry, through the ordinary code path."""
    from app.services import notification_service

    purpose = await notice_service.create_purpose(
        session, tenant_id=tenant["id"], actor=_actor(tenant),
        key=key, name=key.title(), category="Contact Data",
        legal_basis="consent", retention_days=retention_days,
    )
    notice = await notice_service.draft_notice(
        session, tenant_id=tenant["id"], actor=_actor(tenant),
        purpose_id=purpose.id, language="English",
        content="We use your email to send offers.",
        data_collected="Email address",
        user_rights="You may withdraw at any time.",
        withdrawal_policy="Marketing stops within 24 hours.",
    )
    await notice_service.publish_notice(
        session, tenant_id=tenant["id"], actor=_actor(tenant), notice_id=notice.id
    )
    principal = DataPrincipal(
        tenant_id=tenant["id"], external_id=f"user:{key}",
        email=f"{key}@example.com",
    )
    session.add(principal)
    await session.flush()
    # Templates, or every enqueue suppresses and the reminder tests assert
    # nothing.
    await notification_service.seed_default_templates(session, tenant_id=tenant["id"])

    consent = await consent_service.grant(
        session, tenant_id=tenant["id"], actor=_actor(tenant),
        principal_id=principal.id, purpose_id=purpose.id,
    )
    return consent, purpose, principal


async def _age(session, consent, *, expires_in_days: int, lived_days: int = 365):
    """Make a consent look like one granted a while ago and lapsing soon.

    Both ends move, and that matters. Rewriting only `expires_at` produces a
    consent whose whole recorded life is a fortnight — which the reminder
    deliberately skips, because a "lapsing soon" email seconds after somebody
    said yes is noise. Waiting a year is not an option, so the test ages the row
    the way the clock would have.
    """
    now = datetime.now(UTC)
    consent.expires_at = now + timedelta(days=expires_in_days)
    consent.given_at = consent.expires_at - timedelta(days=lived_days)
    await session.flush()


# --------------------------------------------------------------------------- #
# The reminder
# --------------------------------------------------------------------------- #

async def test_a_consent_lapsing_inside_the_window_is_reminded_about(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        consent, _, _ = await _consent(session, tenant_a, retention_days=365)
        await _age(session, consent, expires_in_days=14)

        assert await consent_service.remind_before_expiry(
            session, tenant_id=tenant_a["id"]
        ) == 1

        queued = await session.execute(
            select(Notification).where(
                Notification.template_key == "consent.expiring"
            )
        )
        rows = list(queued.scalars().all())
        assert len(rows) == 1
        assert rows[0].status != "suppressed"


async def test_a_consent_outside_the_window_is_left_alone(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        consent, _, _ = await _consent(session, tenant_a, retention_days=365)
        consent.expires_at = datetime.now(UTC) + timedelta(days=200)
        await session.flush()

        assert await consent_service.remind_before_expiry(
            session, tenant_id=tenant_a["id"]
        ) == 0
        assert consent.renewal_notified_at is None


async def test_the_reminder_is_sent_once_not_once_a_day(
    app_session_factory, tenant_a
):
    """The job runs daily and the window is thirty days wide. Without the stamp
    this is thirty emails about one expiry."""
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        consent, _, _ = await _consent(session, tenant_a, retention_days=365)
        await _age(session, consent, expires_in_days=14)

        assert await consent_service.remind_before_expiry(
            session, tenant_id=tenant_a["id"]
        ) == 1
        assert await consent_service.remind_before_expiry(
            session, tenant_id=tenant_a["id"]
        ) == 0
        assert consent.renewal_notified_at is not None


async def test_a_short_lived_consent_is_not_reminded_about_immediately(
    app_session_factory, tenant_a
):
    """A seven-day consent sits inside a thirty-day window from the moment it is
    granted. "Your consent expires soon", arriving in the same minute somebody
    gave it, reads as a bug and teaches them to ignore the real ones."""
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        consent, _, _ = await _consent(session, tenant_a, retention_days=7)

        assert await consent_service.remind_before_expiry(
            session, tenant_id=tenant_a["id"]
        ) == 0
        # Stamped anyway: considered once, then left alone rather than
        # re-examined every day forever.
        assert consent.renewal_notified_at is not None


async def test_a_consent_with_no_expiry_is_never_reminded_about(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        consent, _, _ = await _consent(session, tenant_a, retention_days=0)
        assert consent.expires_at is None

        assert await consent_service.remind_before_expiry(
            session, tenant_id=tenant_a["id"]
        ) == 0


async def test_a_withdrawn_consent_is_never_reminded_about(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        consent, purpose, principal = await _consent(
            session, tenant_a, retention_days=365
        )
        consent.expires_at = datetime.now(UTC) + timedelta(days=14)
        await consent_service.withdraw(
            session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            principal_id=principal.id, purpose_id=purpose.id,
        )

        assert await consent_service.remind_before_expiry(
            session, tenant_id=tenant_a["id"]
        ) == 0


# --------------------------------------------------------------------------- #
# The announcement
# --------------------------------------------------------------------------- #

async def test_an_expired_consent_is_announced_to_subscribers(
    app_session_factory, tenant_a
):
    """An expiry is a stop, exactly like a withdrawal. A processor told only
    about explicit withdrawals runs on consent that quietly ran out."""
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        await webhook_service.create_endpoint(
            session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            url="https://example.com/hook", label="Processor",
            events=["consent.expired"],
        )
        consent, _, _ = await _consent(session, tenant_a, retention_days=365)
        consent.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.flush()

        assert await consent_service.announce_expiries(
            session, tenant_id=tenant_a["id"]
        ) == 1

        rows = await session.execute(
            select(WebhookDelivery).where(
                WebhookDelivery.event == "consent.expired"
            )
        )
        payloads = [d.payload for d in rows.scalars().all()]
        assert len(payloads) == 1
        assert payloads[0]["data"]["purpose"] == "marketing"


async def test_an_expiry_is_announced_once(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        consent, _, _ = await _consent(session, tenant_a, retention_days=365)
        consent.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.flush()

        assert await consent_service.announce_expiries(
            session, tenant_id=tenant_a["id"]
        ) == 1
        assert await consent_service.announce_expiries(
            session, tenant_id=tenant_a["id"]
        ) == 0


async def test_announcing_writes_an_audit_entry(app_session_factory, tenant_a):
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        consent, _, _ = await _consent(session, tenant_a, retention_days=365)
        consent.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.flush()
        await consent_service.announce_expiries(session, tenant_id=tenant_a["id"])
        await session.flush()

        rows = await session.execute(
            select(AuditEvent).where(
                AuditEvent.action == AuditAction.CONSENT_EXPIRED
            )
        )
        assert len(list(rows.scalars().all())) == 1


async def test_a_consent_that_has_not_expired_yet_is_not_announced(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        consent, _, _ = await _consent(session, tenant_a, retention_days=365)
        consent.expires_at = datetime.now(UTC) + timedelta(days=1)
        await session.flush()

        assert await consent_service.announce_expiries(
            session, tenant_id=tenant_a["id"]
        ) == 0


# --------------------------------------------------------------------------- #
# What renewal must NOT do
# --------------------------------------------------------------------------- #

async def test_neither_sweep_ever_writes_a_consent_status(
    app_session_factory, tenant_a
):
    """The design property this whole module is at risk of eroding.

    Expiry is computed against the clock on every read. A sweep that wrote
    `status` would make validity depend on whether a worker ran — so an expired
    consent must still read `active` in the row and `expired` to `check`.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        consent, purpose, principal = await _consent(
            session, tenant_a, retention_days=365
        )
        consent.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.flush()

        await consent_service.remind_before_expiry(session, tenant_id=tenant_a["id"])
        await consent_service.announce_expiries(session, tenant_id=tenant_a["id"])

        assert consent.status == "active", "a sweep rewrote the stored status"

        # ...and the computed answer is still the honest one.
        decision = await consent_service.check(
            session, tenant_id=tenant_a["id"],
            principal_id=principal.id, purpose_key=purpose.key,
        )
        assert decision["allowed"] is False
        assert decision["status"] == "expired"


# --------------------------------------------------------------------------- #
# Renewing
# --------------------------------------------------------------------------- #

async def test_renewing_pushes_the_expiry_out_and_clears_the_stamps(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        consent, purpose, principal = await _consent(
            session, tenant_a, retention_days=365
        )
        consent.expires_at = datetime.now(UTC) + timedelta(days=5)
        await consent_service.remind_before_expiry(session, tenant_id=tenant_a["id"])
        assert consent.renewal_notified_at is not None

        renewed = await consent_service.renew(
            session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            principal_id=principal.id, purpose_id=purpose.id,
        )

        assert renewed.expires_at > datetime.now(UTC) + timedelta(days=300)
        # Cleared, so the next cycle reminds again.
        assert renewed.renewal_notified_at is None
        assert renewed.expiry_announced_at is None


async def test_renewing_repoints_at_the_current_notice_version(
    app_session_factory, tenant_a
):
    """The reason renewal is a re-grant rather than a date bump.

    Extending `expires_at` on a consent given against text that has since been
    revised would manufacture agreement to wording nobody read.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        consent, purpose, principal = await _consent(
            session, tenant_a, retention_days=365
        )
        original_notice = consent.notice_id

        v2 = await notice_service.revise_notice(
            session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            notice_id=original_notice,
            content="We now also share your email with partners.",
        )
        await notice_service.publish_notice(
            session, tenant_id=tenant_a["id"], actor=_actor(tenant_a), notice_id=v2.id
        )

        renewed = await consent_service.renew(
            session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            principal_id=principal.id, purpose_id=purpose.id,
        )
        assert renewed.notice_id == v2.id
        assert renewed.notice_id != original_notice


async def test_renewing_restamps_when_consent_was_given(
    app_session_factory, tenant_a
):
    """A renewal is a fresh act of consent, so its date is the date of that act
    — not the date of the original one it replaces."""
    async with scoped(app_session_factory, tenant_a["id"]) as session:
        consent, purpose, principal = await _consent(
            session, tenant_a, retention_days=365
        )
        consent.given_at = datetime.now(UTC) - timedelta(days=300)
        await session.flush()
        before = consent.given_at

        renewed = await consent_service.renew(
            session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
            principal_id=principal.id, purpose_id=purpose.id,
        )
        assert renewed.given_at > before
