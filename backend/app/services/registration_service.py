"""
Self-serve registration: a company signs up and gets a tenant plus its first
Admin/DPO.

Until now tenants were provisioned by running a Python script. That is fine for a
demo and useless for a product, so this is the path a real customer takes.

Three things are deliberate:

* **One transaction.** Tenant, admin user and both audit entries commit together
  or not at all. A tenant with no admin is an unusable account that support has to
  clean up by hand.
* **Registration does not leak whether a company already exists.** A taken slug
  returns a generic "not available" rather than "Acme Ltd is already registered
  here" — that would turn signup into a customer-list oracle for a competitor.
* **The password policy is enforced server-side.** The frontend also checks it,
  for the user's benefit, but that check is a courtesy and this one is the rule.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, Conflict
from app.core.permissions import Role
from app.core.security import hash_password
from app.models.audit import AuditAction
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit_service
from app.services.audit_service import Actor

logger = logging.getLogger("app.registration")

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

# Slugs that must not become a tenant, because they would collide with routes or
# be used to impersonate us in a URL like datashield.in/admin.
RESERVED_SLUGS = {
    "admin", "api", "app", "www", "mail", "support", "help", "docs", "status",
    "billing", "auth", "login", "signup", "register", "dashboard", "static",
    "public", "internal", "system", "root", "datashield", "test", "demo",
}


class WeakPassword(AppError):
    error_type = "/errors/weak-password"
    title = "Password is not strong enough"


@dataclass
class Registration:
    tenant: Tenant
    admin: User


# Latin letters that NFKD does NOT decompose, because they are atomic code points
# rather than letter-plus-accent. Without this map they are silently DROPPED:
# "Çørp" becomes "crp", not "corp". Nordic, Polish and German company names hit
# this constantly, so it is a correctness issue rather than a nicety.
_TRANSLITERATE = str.maketrans({
    "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
    "ð": "d", "Ð": "D", "þ": "th", "Þ": "TH", "ß": "ss",
    "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ħ": "h", "Ħ": "H",
    "ı": "i", "İ": "I", "ŋ": "n", "Ŋ": "N", "ŧ": "t", "Ŧ": "T",
    "'": "", "’": "",   # O'Brien Ltd -> obrien-ltd, not o-brien-ltd
})


def slugify(name: str) -> str:
    """Suggest a URL-safe slug from a company name.

    Only a suggestion — the caller can override it, and it is validated either
    way. Transliterate first, then NFKD-fold the rest, so "Åcme Çørp" yields
    "acme-corp" rather than dropping the characters ASCII cannot represent.
    """
    name = name.translate(_TRANSLITERATE)
    ascii_name = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug[:63].rstrip("-")


def validate_password(password: str, *, email: str = "", name: str = "") -> None:
    """Reject the passwords that actually get compromised.

    Length first, because it matters more than character classes. The
    similarity checks catch the genuinely common cases — reusing your own email
    or company name — which no amount of "must contain a symbol" prevents.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(
            f"Use at least {MIN_PASSWORD_LENGTH} characters. A short passphrase of "
            f"a few unrelated words is stronger than a short complex string."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise WeakPassword(f"Maximum {MAX_PASSWORD_LENGTH} characters.")

    lowered = password.lower()
    email_lower = email.lower().strip()
    local_part = email_lower.split("@")[0] if email_lower else ""

    # Two separate checks, and both are needed.
    #
    # The full address catches "dpo@acme.example.com-x", a real pattern. The
    # local part catches "amitkumar99" — but only when it is at least 4
    # characters, because short role addresses (dpo@, hr@, it@) would otherwise
    # ban any password containing those three letters in sequence.
    if email_lower and email_lower in lowered:
        raise WeakPassword("Your password must not contain your email address.")
    if local_part and len(local_part) >= 4 and local_part in lowered:
        raise WeakPassword("Your password must not contain your email address.")
    if name and len(name) >= 4 and name.lower() in lowered:
        raise WeakPassword("Your password must not contain your name.")
    if len(set(password)) < 5:
        raise WeakPassword("Too repetitive — use a longer, more varied passphrase.")

    # A tiny denylist of the ones that show up first in any credential-stuffing
    # run. Real defence is length plus rate limiting, not a big dictionary.
    if lowered in {
        "password1234", "passw0rd1234", "administrator", "qwertyuiop12",
        "123456789012", "letmein12345", "welcome12345",
    }:
        raise WeakPassword("That password is too common.")


async def register_tenant(
    session: AsyncSession,
    *,
    company_name: str,
    slug: str | None,
    admin_name: str,
    admin_email: str,
    password: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> Registration:
    """Create a tenant and its first Admin/DPO in one transaction."""
    email = admin_email.strip().lower()
    company_name = company_name.strip()
    admin_name = admin_name.strip()

    chosen = (slug or slugify(company_name)).strip().lower()
    if not SLUG_RE.match(chosen):
        raise Conflict(
            "Workspace name must be 2–63 characters, lowercase letters, numbers "
            "and hyphens, starting with a letter or number."
        )
    if chosen in RESERVED_SLUGS:
        raise Conflict("That workspace name is not available.")

    validate_password(password, email=email, name=admin_name)

    # Case-insensitive, because slugs are lowercase by definition and a
    # case-sensitive check would let "Acme" and "acme" both exist.
    taken = (
        await session.execute(
            select(Tenant.id).where(func.lower(Tenant.slug) == chosen)
        )
    ).scalar_one_or_none()
    if taken:
        # Deliberately identical to the reserved-slug message: signup must not
        # confirm which companies are already customers.
        raise Conflict("That workspace name is not available.")

    tenant = Tenant(
        slug=chosen,
        name=company_name,
        legal_name=company_name,
        grievance_officer_name=admin_name,
        grievance_officer_email=email,
    )
    session.add(tenant)
    await session.flush()

    # Tenant context must be bound before writing anything tenant-scoped, or
    # RLS's WITH CHECK rejects the insert. That rejection would be the policy
    # working correctly, not a bug.
    from app.db.session import set_tenant_context

    await set_tenant_context(session, tenant.id)

    admin = User(
        tenant_id=tenant.id,
        email=email,
        password_hash=hash_password(password),
        full_name=admin_name,
        role=Role.ADMIN.value,
        password_changed_at=datetime.now(UTC),
    )
    session.add(admin)
    try:
        await session.flush()
    except IntegrityError as exc:  # pragma: no cover - unique slug already checked
        raise Conflict("That workspace name is not available.") from exc

    actor = Actor(type="user", id=admin.id, label=email, ip=ip, user_agent=user_agent)
    await audit_service.record(
        session,
        tenant_id=tenant.id,
        actor=actor,
        action=AuditAction.TENANT_CREATED,
        entity_type="tenant",
        entity_id=tenant.id,
        # The company name is business data, not personal data, so it is safe in
        # the trail. The password and its hash are not recorded anywhere.
        payload={"slug": chosen, "name": company_name, "self_serve": True},
    )
    await audit_service.record(
        session,
        tenant_id=tenant.id,
        actor=actor,
        action=AuditAction.USER_CREATED,
        entity_type="user",
        entity_id=admin.id,
        payload={"role": admin.role, "first_admin": True},
    )

    # Starter purposes and published notices, so the consent module can actually
    # be used the moment someone signs in. Configuration only — no consents are
    # created, because a consent has to be an act by a person.
    from app.services import notice_service

    await notice_service.seed_default_purposes(session, tenant_id=tenant.id, actor=actor)

    logger.info("tenant registered", extra={"context": {"slug": chosen}})
    return Registration(tenant=tenant, admin=admin)


async def slug_available(session: AsyncSession, slug: str) -> bool:
    """Availability check for the signup form's live feedback.

    This one DOES reveal whether a slug is taken, which the registration error
    deliberately does not. That is a considered trade: a signup form that cannot
    tell you your workspace name is unavailable until you submit is hostile, and
    the same information is obtainable by attempting to register anyway. It is
    rate-limited at the edge for that reason.
    """
    chosen = slug.strip().lower()
    if not SLUG_RE.match(chosen) or chosen in RESERVED_SLUGS:
        return False
    taken = (
        await session.execute(
            select(Tenant.id).where(func.lower(Tenant.slug) == chosen)
        )
    ).scalar_one_or_none()
    return taken is None
