"""Metadata for every uploaded or generated object.

The bytes live in an object store (app/storage); this table is the index, and
the only thing that knows what a blob *is*. A key on its own is meaningless,
which is deliberate: enumerating the container yields encrypted objects with
opaque names and no indication of whose they are.

FOUR THINGS THIS ROW EXISTS TO ENFORCE

1. **Purpose.** A file uploaded to prove somebody's identity must not be
   readable through the endpoint that serves grievance attachments. `purpose` is
   checked on every download, so a leaked file id from one flow cannot be
   redeemed in another.

2. **Provenance.** Who uploaded it, and against which request. An identity
   document that cannot be tied to the request it was submitted for is a loose
   photograph of a stranger's passport.

3. **Integrity.** `sha256` of the PLAINTEXT, recorded at upload. AES-GCM
   already detects tampering with the ciphertext; this catches the different
   problem of an object correctly encrypted from the wrong source bytes, and
   lets a delivered package be proven identical to what was assembled.

4. **An expiry.** `delete_after` is set at upload for anything whose purpose is
   exhausted quickly — identity documents above all. Retention sweeps it.
   Nothing here is kept because deleting it was never scheduled.

WHY `content_type` IS NOT WHAT THE BROWSER SAID

It is the type we *detected*, from magic bytes. A caller who declares
`image/png` and uploads HTML is not making a mistake, and echoing their claim
back on download would turn this table into stored XSS with our own domain
around it. Downloads are additionally served as attachments with `nosniff` —
belt and braces, because the cost of being wrong here is somebody's session.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDMixin

#: What a file is for. Checked on download — see the module docstring.
#:
#: Each value implies an access rule, enforced in `file_service.readable_by`:
#:
#:   dsar_id_document      identity proof. Staff with dsar:review, and nobody
#:                         else — not even the person who uploaded it, who has
#:                         the original on their phone anyway.
#:   dsar_evidence         an internal working file attached to an action item.
#:                         Staff only.
#:   dsar_package          the assembled response. The data principal it belongs
#:                         to, and staff.
#:   dsar_message          an attachment on a message in the request thread.
#:                         Both sides of that thread.
#:   grievance_attachment  supporting material on a §13 complaint.
#:   assessment_evidence   a document answering an assessment question.
FILE_PURPOSES = (
    "dsar_id_document",
    "dsar_evidence",
    "dsar_package",
    "dsar_message",
    "grievance_attachment",
    "assessment_evidence",
)

#: Types we will accept and store. Anything else is refused at upload.
#:
#: No SVG. An SVG is a script container, and one served back to a browser from
#: our origin — even as an attachment — is a needless risk when a PNG or JPEG
#: does the same job for every purpose here.
ALLOWED_CONTENT_TYPES = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/heic",
    "application/pdf",
    "text/csv",
    "text/plain",
    "application/json",
    "application/zip",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)


class StoredFile(UUIDMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "stored_files"
    __table_args__ = (
        Index("ix_stored_files_tenant_purpose", "tenant_id", "purpose"),
        Index("ix_stored_files_entity", "tenant_id", "entity_type", "entity_id"),
        # Retention sweeps this. Partial, because most rows have no expiry and
        # the sweep only ever asks for the ones that do.
        Index(
            "ix_stored_files_delete_after",
            "delete_after",
            postgresql_where="delete_after IS NOT NULL AND deleted_at IS NULL",
        ),
        CheckConstraint(
            "purpose IN ('dsar_id_document','dsar_evidence','dsar_package',"
            "'dsar_message','grievance_attachment','assessment_evidence')",
            name="purpose",
        ),
        CheckConstraint("byte_size > 0", name="not_empty"),
        # A deleted row keeps its metadata and loses its bytes. The audit trail
        # should still be able to say "an identity document existed here and was
        # destroyed on this date", which is the opposite of a hard delete.
        CheckConstraint(
            "deleted_at IS NULL OR storage_key IS NULL",
            name="deleted_has_no_key",
        ),
    )

    #: What this is for, and therefore who may read it.
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)

    #: What it hangs off — 'dsar_request', 'grievance', 'assessment_response'.
    #: Loose rather than a foreign key per kind: one files table serving six
    #: flows would otherwise need six nullable FK columns and a CHECK asserting
    #: exactly one is set.
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)

    #: As the uploader named it, for display only. Never used to build a path —
    #: see `storage_key`, which is generated.
    filename: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Detected, not declared. See the module docstring.
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)

    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)

    #: Of the plaintext, hex. Integrity and de-duplication evidence.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Where the ciphertext sits in the object store. NULL once purged, which is
    #: how a row survives its bytes.
    storage_key: Mapped[str | None] = mapped_column(String(512))

    #: NULL for anything the system generated (a package, a report).
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: Set instead when the uploader was a data principal rather than staff.
    uploaded_by_principal: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("data_principals.id", ondelete="SET NULL")
    )

    #: When the bytes should stop existing. See config.id_document_retention_days.
    delete_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Why it went — 'retention', 'erasure', 'admin'. An object that vanished
    #: for no recorded reason is indistinguishable from one that was lost.
    deleted_reason: Mapped[str | None] = mapped_column(Text)

    @property
    def is_available(self) -> bool:
        return self.deleted_at is None and self.storage_key is not None
