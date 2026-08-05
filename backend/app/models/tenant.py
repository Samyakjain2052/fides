"""A customer company — a Data Fiduciary under the DPDP Act."""

from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


class Tenant(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "tenants"
    __table_args__ = (
        # SLA windows are per tenant because the statutory floor is a floor, not
        # a target — a customer may contractually promise faster.
        CheckConstraint("dsar_sla_days BETWEEN 1 AND 90", name="dsar_sla_range"),
        CheckConstraint("grievance_sla_days BETWEEN 1 AND 90", name="grievance_sla_range"),
    )

    # URL-safe identifier used in the login flow and in per-tenant banner keys.
    slug: Mapped[str] = mapped_column(String(63), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(255))

    # The Act requires a published grievance contact.
    grievance_officer_name: Mapped[str | None] = mapped_column(String(255))
    grievance_officer_email: Mapped[str | None] = mapped_column(String(320))

    # Notices must be available in English or any Eighth Schedule language.
    default_language: Mapped[str] = mapped_column(String(32), default="English", nullable=False)

    dsar_sla_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    grievance_sla_days: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    grievance_escalation_days: Mapped[int] = mapped_column(Integer, default=10, nullable=False)

    # Tenant-wide security policy, enforced at login.
    require_mfa: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Secret for the signed-token step-up on public consent collection. Lives on
    # the tenant rather than on a publishable key so that rotating a key does not
    # invalidate tokens the integrator's server is already minting.
    consent_token_secret: Mapped[str | None] = mapped_column(String(128))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Tenant {self.slug}>"
