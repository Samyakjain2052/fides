"""
Self-serve registration tests.

The interesting cases are not "does it create a row" — they are the ones where
signup either leaks information or produces an unusable account.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.core.errors import Conflict
from app.db.session import set_tenant_context
from app.models.audit import AuditAction, AuditEvent
from app.models.tenant import Tenant
from app.models.user import User
from app.services import registration_service
from app.services.registration_service import WeakPassword, slugify

GOOD_PASSWORD = "correct-horse-battery-staple"


async def _register(factory, **over):
    args = dict(
        company_name="Acme Fintech Pvt. Ltd.",
        slug=None,
        admin_name="Amit Kumar",
        admin_email="dpo@acme.example.com",
        password=GOOD_PASSWORD,
    )
    args.update(over)
    async with factory() as session:
        async with session.begin():
            return await registration_service.register_tenant(session, **args)


async def test_registration_creates_tenant_and_admin_together(app_session_factory):
    reg = await _register(app_session_factory)

    assert reg.tenant.slug == "acme-fintech-pvt-ltd"
    assert reg.admin.role == "admin"
    assert reg.admin.email == "dpo@acme.example.com"
    # Never stored in the clear, and never equal to the input.
    assert reg.admin.password_hash and reg.admin.password_hash != GOOD_PASSWORD
    assert reg.admin.password_hash.startswith("$argon2")


async def test_registration_writes_both_audit_entries(app_session_factory):
    reg = await _register(app_session_factory)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, reg.tenant.id)
            actions = [
                r.action
                for r in (await session.execute(select(AuditEvent).order_by(AuditEvent.seq))).scalars()
            ]

    # The chain starts here, so the very first entry in a customer's trail is the
    # moment their account came into existence.
    assert actions[0] == AuditAction.TENANT_CREATED
    assert AuditAction.USER_CREATED in actions


async def test_password_is_never_in_the_audit_trail(app_session_factory):
    reg = await _register(app_session_factory, password="a-very-distinctive-passphrase-9")

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, reg.tenant.id)
            blob = str(
                [r.payload for r in (await session.execute(select(AuditEvent))).scalars()]
            )

    assert "distinctive" not in blob, "the password leaked into the audit trail"


async def test_duplicate_workspace_is_rejected_without_confirming_who_owns_it(
    app_session_factory,
):
    """A taken slug must not tell you WHICH company holds it.

    Otherwise signup becomes a way for a competitor to enumerate our customers.
    """
    await _register(app_session_factory)

    with pytest.raises(Conflict) as exc:
        await _register(app_session_factory, admin_email="someone@else.example.com")

    message = str(exc.value)
    assert "not available" in message
    assert "Acme" not in message, "the error revealed the existing company name"


async def test_reserved_workspace_names_are_refused(app_session_factory):
    for reserved in ("admin", "api", "datashield", "login"):
        with pytest.raises(Conflict):
            await _register(app_session_factory, slug=reserved)


async def test_reserved_and_taken_give_the_same_message(app_session_factory):
    """Same wording for both, so the error cannot be used to distinguish
    'this is ours' from 'this is another customer's'."""
    await _register(app_session_factory, slug="acme-one")

    with pytest.raises(Conflict) as taken:
        await _register(app_session_factory, slug="acme-one",
                        admin_email="b@example.com")
    with pytest.raises(Conflict) as reserved:
        await _register(app_session_factory, slug="admin",
                        admin_email="c@example.com")

    assert str(taken.value) == str(reserved.value)


@pytest.mark.parametrize(
    "password,why",
    [
        ("short1", "under the length floor"),
        ("aaaaaaaaaaaaaaa", "too repetitive"),
        ("dpo@acme.example.com-x", "contains the email"),
        ("Amit Kumar is here!!", "contains the name"),
        ("password1234", "common password"),
    ],
)
async def test_weak_passwords_are_refused(app_session_factory, password, why):
    with pytest.raises(WeakPassword):
        await _register(app_session_factory, password=password)


async def test_a_new_tenant_cannot_see_an_existing_tenants_data(app_session_factory):
    """The guarantee, applied to accounts that create themselves.

    Self-serve signup means strangers create tenants next to paying customers.
    Isolation has to hold for a tenant that arrived through the public form
    exactly as it does for one provisioned by hand.
    """
    first = await _register(app_session_factory, slug="tenant-one")
    second = await _register(
        app_session_factory, slug="tenant-two", admin_email="dpo@two.example.com"
    )

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, second.tenant.id)
            emails = set((await session.execute(select(User.email))).scalars())
            audit_count = (
                await session.execute(select(func.count()).select_from(AuditEvent))
            ).scalar_one()

    assert second.admin.email in emails
    assert first.admin.email not in emails, "a self-serve tenant can see another's users"
    # Its own two bootstrap entries, and nobody else's.
    assert audit_count == 2


async def test_each_new_tenant_starts_its_own_audit_chain(app_session_factory):
    a = await _register(app_session_factory, slug="chain-a")
    b = await _register(app_session_factory, slug="chain-b", admin_email="b@example.com")

    from app.services import audit_service

    async with app_session_factory() as session:
        for reg in (a, b):
            async with session.begin():
                await set_tenant_context(session, reg.tenant.id)
                status = await audit_service.verify_chain(session, tenant_id=reg.tenant.id)
                assert status.ok, status.problem
                assert status.head_seq == 2, "each tenant's chain starts at 1"


async def test_email_is_normalised_to_lowercase(app_session_factory):
    reg = await _register(app_session_factory, admin_email="DPO@Acme.Example.COM")
    assert reg.admin.email == "dpo@acme.example.com"


async def test_slug_is_derived_from_the_company_name_when_omitted(app_session_factory):
    reg = await _register(app_session_factory, company_name="Zeta Health & Care Ltd")
    assert reg.tenant.slug == "zeta-health-care-ltd"


def test_slugify_handles_unicode_and_punctuation():
    # Company names are not ASCII, and a slug that breaks in a URL is a bug.
    assert slugify("Åcme Pvt. Ltd.") == "acme-pvt-ltd"
    assert slugify("  Spaces   Everywhere  ") == "spaces-everywhere"
    assert slugify("Ünïcôdé Çørp") == "unicode-corp"


async def test_workspace_availability_check(app_session_factory):
    await _register(app_session_factory, slug="taken-one")

    async with app_session_factory() as session:
        async with session.begin():
            assert await registration_service.slug_available(session, "free-one") is True
            assert await registration_service.slug_available(session, "taken-one") is False
            assert await registration_service.slug_available(session, "admin") is False
            assert await registration_service.slug_available(session, "x") is False


async def test_registered_admin_can_immediately_sign_in(app_session_factory):
    """A tenant with no working login is an unusable account."""
    from app.services import auth_service

    reg = await _register(app_session_factory, slug="signin-test")

    async with app_session_factory() as session:
        async with session.begin():
            pair = await auth_service.authenticate(
                session,
                tenant_slug="signin-test",
                email=reg.admin.email,
                password=GOOD_PASSWORD,
            )
    assert pair.access_token
    assert pair.user.role == "admin"


async def test_tenant_row_carries_the_grievance_contact(app_session_factory):
    """The Act requires a published grievance contact, so signup seeds it with the
    first admin rather than leaving it null until someone notices."""
    reg = await _register(app_session_factory)

    async with app_session_factory() as session:
        async with session.begin():
            t = (
                await session.execute(select(Tenant).where(Tenant.id == reg.tenant.id))
            ).scalar_one()

    assert t.grievance_officer_email == "dpo@acme.example.com"
    assert t.grievance_officer_name == "Amit Kumar"
