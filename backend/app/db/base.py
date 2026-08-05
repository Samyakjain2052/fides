"""
Declarative base and the mixins every table shares.

Two conventions worth stating:

* **UUID primary keys**, not sequential integers. Sequential ids in a
  multi-tenant product leak volume ("they have 412 customers") and invite
  enumeration. They are also awkward to merge if a large customer is ever moved
  to their own database.
* **Timezone-aware UTC timestamps**, always. A consent timestamp is legal
  evidence; a naive datetime whose zone depends on the server's locale is not
  evidence. Defaults are set by the database (`now()`), not Python, so a
  clock-skewed application server cannot backdate a record.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit naming so Alembic autogenerate produces stable, reviewable migrations
# instead of database-assigned names that churn between environments.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class TimestampMixin:
    """`DateTime(timezone=True)` is explicit for a reason.

    Every migration in this project creates these columns as `timestamptz`, and
    the module docstring above promises timezone-aware timestamps — but the
    mapped type was left to be inferred from the `datetime` annotation, which
    SQLAlchemy renders as a NAIVE `TIMESTAMP WITHOUT TIME ZONE`.

    The mismatch is invisible until something compares one of these columns to an
    aware datetime, at which point asyncpg refuses the parameter and the endpoint
    500s. The first such comparison was the public API's rate-limit window; the
    same trap was waiting for every retention and expiry query still to be built.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TenantMixin:
    """Marks a table as tenant-scoped.

    Presence of this column is what the migration looks for when enabling RLS —
    so a new table either carries a tenant_id and gets a policy, or it is
    deliberately global. There is no third option where a table holds customer
    data with no policy.
    """

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        nullable=False,
        index=True,
    )
