"""Uploads and downloads, with the checks that make them safe to have at all.

This is new capability, and it is the riskiest thing in the product: before it,
DataShield accepted no files, so no file could be stored, mistyped, served back
as script, or left lying around after its purpose expired. Every rule below is
here because it is the specific way that goes wrong.

THE SIX RULES

1. **The declared content type is ignored.** We detect from magic bytes. A
   caller who uploads HTML labelled `image/png` is not confused, they are
   probing, and echoing their label back on download is stored XSS on our own
   origin. Detection also has to *agree* with the extension-free allowlist, so
   an unrecognised sniff is a refusal rather than a fallback to
   `application/octet-stream`.

2. **The stored name is generated.** The uploader's filename is display text
   and nothing else — never a path component. `../../etc/passwd` as a filename
   is a boring attack that works depressingly often.

3. **Size is bounded before the bytes are read into memory.** Enforced twice:
   once against the declared `content-length`, and again while streaming, since
   a lying header is free.

4. **Everything is encrypted at rest**, with the file's own id as additional
   authenticated data. An attacker with write access to the object store cannot
   swap one person's identity document for another's — the AAD would not match
   and decryption fails closed.

5. **Reads are authorised by purpose**, not by possession of an id. A file id
   leaked from a grievance cannot be redeemed at the identity-document endpoint.
   See `assert_readable`.

6. **Identity documents get an expiry at upload.** Not a cleanup job somebody
   remembers to write later: `delete_after` is set in the same INSERT, so the
   retention sweep will destroy them even if nobody ever thinks about it again.
   §8(7) requires erasure once the purpose is served, and an ID photograph's
   purpose is served the moment identity is confirmed.

WHAT THIS DOES NOT DO

No malware scanning. Files here are served only back to staff and to the person
who uploaded them, always as attachments, never executed — but a customer
uploading a document that infects the DPO's laptop is a real risk and this does
not address it. If we integrate a scanner it belongs in `store`, between
detection and encryption, and it needs to be able to quarantine rather than
merely refuse.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.config import get_settings
from app.core.crypto import open_sealed_bytes, seal_bytes
from app.core.errors import Conflict, NotFound, ValidationProblem
from app.models.stored_file import ALLOWED_CONTENT_TYPES, StoredFile
from app.storage import ObjectNotFound, get_storage

logger = logging.getLogger("app.files")

#: Magic-byte signatures, checked in order. Longest / most specific first so
#: a container format is not mistaken for the thing it contains.
#:
#: Office documents and .zip share a signature, because .docx IS a zip. They are
#: therefore indistinguishable here without parsing the archive, and we do not
#: parse untrusted archives to decide what to call them — a zip is reported as a
#: zip, and the caller's declared type is allowed to narrow it to an Office type
#: only if it claims one. That is the single place a declared type influences
#: anything, and it can only ever move between two already-allowed values.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
    (b"PK\x05\x06", "application/zip"),  # empty archive
)

_ZIP_NARROWABLE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

#: Purposes whose bytes are destroyed on a short clock. See rule 6.
_EPHEMERAL_PURPOSES = ("dsar_id_document",)


def _detect(data: bytes, declared: str | None) -> str:
    """What this actually is.

    RIFF/WEBP and HEIC are checked separately from the simple prefix table
    because both put their identifying bytes after a length field rather than at
    offset zero.
    """
    for signature, kind in _SIGNATURES:
        if data.startswith(signature):
            if kind == "application/zip" and (declared or "") in _ZIP_NARROWABLE:
                return declared  # type: ignore[return-value]
            return kind

    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"mif1"):
        return "image/heic"

    # Text-ish payloads have no signature. Accept them only if they decode as
    # UTF-8 and the caller claimed a text type — that keeps CSV evidence working
    # without turning "no signature matched" into a way to store anything.
    if (declared or "") in ("text/csv", "text/plain", "application/json"):
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValidationProblem(
                "That file was sent as text but is not valid UTF-8. If it is a "
                "spreadsheet or a document, upload it in its own format."
            ) from None
        return declared  # type: ignore[return-value]

    raise ValidationProblem(
        "That file type is not accepted. Images (PNG, JPEG, WebP, HEIC), PDF, "
        "CSV, plain text, Word and Excel documents are."
    )


async def store(
    session,
    *,
    tenant_id: uuid.UUID,
    purpose: str,
    entity_type: str,
    entity_id: uuid.UUID,
    filename: str,
    data: bytes,
    declared_content_type: str | None = None,
    uploaded_by: uuid.UUID | None = None,
    uploaded_by_principal: uuid.UUID | None = None,
) -> StoredFile:
    """Validate, encrypt, put, and record. In that order.

    The row is flushed before the bytes are written so the file's id exists to
    use as AAD. If the object write then fails the transaction rolls back and
    the row goes with it, which is the right way round: a row with no object is
    a broken download, an object with no row is an orphan nobody will ever find
    or delete.
    """
    settings = get_settings()

    if not data:
        raise ValidationProblem("That file is empty.")
    if len(data) > settings.max_upload_bytes:
        mb = settings.max_upload_bytes // (1024 * 1024)
        raise ValidationProblem(f"Files must be {mb} MB or smaller.")

    content_type = _detect(data, declared_content_type)
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ValidationProblem("That file type is not accepted.")

    # Display text only. Strip directory separators and control characters so a
    # name cannot travel anywhere or corrupt a header on the way out.
    safe_name = (filename or "upload").replace("\\", "/").rsplit("/", 1)[-1]
    safe_name = "".join(c for c in safe_name if c.isprintable() and c != '"')[:255]

    delete_after = None
    if purpose in _EPHEMERAL_PURPOSES:
        delete_after = datetime.now(UTC) + timedelta(
            days=settings.id_document_retention_days
        )

    row = StoredFile(
        tenant_id=tenant_id,
        purpose=purpose,
        entity_type=entity_type,
        entity_id=entity_id,
        filename=safe_name or "upload",
        content_type=content_type,
        byte_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        uploaded_by=uploaded_by,
        uploaded_by_principal=uploaded_by_principal,
        delete_after=delete_after,
    )
    session.add(row)
    await session.flush()

    # Tenant in the key, so an object found loose is at least attributable, and
    # a misconfigured prefix listing cannot mix customers together.
    row.storage_key = f"{tenant_id.hex}/{purpose}/{row.id.hex}"
    await get_storage().put(row.storage_key, seal_bytes(data, aad=str(row.id)))

    logger.info(
        "stored file id=%s purpose=%s bytes=%d type=%s",
        row.id, purpose, row.byte_size, content_type,
    )
    return row


async def fetch(session, *, file_id: uuid.UUID) -> tuple[StoredFile, bytes]:
    """The row and its plaintext.

    Verifies the hash after decrypting. AES-GCM already proves the ciphertext
    was not altered; this catches the different failure of a correctly encrypted
    object built from the wrong bytes, and it is cheap next to the decryption.
    """
    row = await session.scalar(select(StoredFile).where(StoredFile.id == file_id))
    if row is None:
        raise NotFound("No such file.")
    if not row.is_available:
        raise Conflict(
            f"That file was deleted on "
            f"{row.deleted_at:%d %b %Y} ({row.deleted_reason or 'no reason recorded'}) "
            "and cannot be retrieved."
        )

    try:
        sealed = await get_storage().get(row.storage_key or "")
    except ObjectNotFound:
        # The row says it exists and the store disagrees. Worth shouting about:
        # it means something deleted objects without going through `purge`.
        logger.error("stored file %s has no object at %s", row.id, row.storage_key)
        raise Conflict(
            "That file's contents are missing from storage. This is a fault on "
            "our side and has been logged."
        ) from None

    data = open_sealed_bytes(sealed, aad=str(row.id))
    if hashlib.sha256(data).hexdigest() != row.sha256:
        raise Conflict(
            "That file failed its integrity check and was not returned."
        )
    return row, data


async def purge(
    session, *, file_id: uuid.UUID, reason: str
) -> StoredFile:
    """Destroy the bytes, keep the record.

    The row survives with `storage_key` cleared, so the audit trail can still
    say an identity document existed here and was destroyed on this date. A hard
    delete would leave no evidence that we ever held it — which sounds tidier and
    is worse, because "we deleted it" is the thing we may have to prove.
    """
    row = await session.scalar(select(StoredFile).where(StoredFile.id == file_id))
    if row is None:
        raise NotFound("No such file.")
    if row.deleted_at is not None:
        return row

    if row.storage_key:
        await get_storage().delete(row.storage_key)
    row.storage_key = None
    row.deleted_at = datetime.now(UTC)
    row.deleted_reason = reason
    return row


async def purge_expired(session, *, tenant_id: uuid.UUID, now: datetime | None = None) -> int:
    """Destroy everything past its `delete_after`. Called by the retention sweep."""
    moment = now or datetime.now(UTC)
    rows = (
        await session.execute(
            select(StoredFile).where(
                StoredFile.delete_after.is_not(None),
                StoredFile.delete_after <= moment,
                StoredFile.deleted_at.is_(None),
            )
        )
    ).scalars().all()

    for row in rows:
        if row.storage_key:
            await get_storage().delete(row.storage_key)
        row.storage_key = None
        row.deleted_at = moment
        row.deleted_reason = "retention"

    if rows:
        logger.info(
            "purged %d expired file(s) for tenant %s", len(rows), tenant_id
        )
    return len(rows)


#: Which capability lets staff read each purpose.
#:
#: Identity documents are the outlier: `dsar:process` rather than `dsar:read`.
#: An auditor with read access over the rights queue has a legitimate need to
#: see that identity was verified, and no need whatever to look at the
#: photograph — so the person who has to make the verification decision can open
#: it and nobody else can.
_STAFF_CAPABILITY = {
    "dsar_id_document": "dsar:process",
    "dsar_evidence": "dsar:read",
    "dsar_package": "dsar:read",
    "dsar_message": "dsar:read",
    "grievance_attachment": "grievance:read",
    "assessment_evidence": "assessment:read",
}

#: Purposes the subject of the file may also read. Identity documents and
#: internal evidence are deliberately absent: the first they already have, and
#: the second is our working note about their request, not a disclosure to them.
_SUBJECT_READABLE = ("dsar_package", "dsar_message", "grievance_attachment")


async def assert_readable(
    session,
    row: StoredFile,
    *,
    capabilities: set[str],
    principal_ids: set[uuid.UUID],
) -> None:
    """Refuse unless this caller may read this file, for this purpose.

    Authorisation is by purpose and not by possession of an id. That distinction
    is the whole point: file ids travel — in a message thread, a log line, a
    support ticket — and a leaked id from a grievance attachment must not be
    redeemable at the identity-document endpoint just because both are files.

    `principal_ids` is the set of Data Principal records the caller *is*. Staff
    pass their own; a data principal passes theirs. Empty for a machine caller.
    """
    needed = _STAFF_CAPABILITY.get(row.purpose)
    if needed and needed in capabilities:
        return

    if row.purpose in _SUBJECT_READABLE and principal_ids:
        if await _belongs_to(session, row, principal_ids):
            return

    raise NotFound("No such file.")


async def _belongs_to(session, row: StoredFile, principal_ids: set[uuid.UUID]) -> bool:
    """Is this file about one of these people?

    Resolved through the owning entity rather than stored on the file, so the
    answer cannot drift from the request it hangs off.
    """
    if row.entity_type == "dsar_request":
        from app.models.dsar import DsarRequest

        owner = await session.scalar(
            select(DsarRequest.principal_id).where(DsarRequest.id == row.entity_id)
        )
        return owner in principal_ids

    if row.entity_type == "grievance":
        from app.models.grievance import Grievance

        owner = await session.scalar(
            select(Grievance.principal_id).where(Grievance.id == row.entity_id)
        )
        return owner is not None and owner in principal_ids

    # Unknown entity kind: refuse. A new flow that forgets to extend this gets
    # "no such file" rather than an open door.
    return False


async def for_entity(
    session, *, entity_type: str, entity_id: uuid.UUID, purpose: str | None = None
) -> list[StoredFile]:
    """Files attached to one thing, newest last."""
    query = select(StoredFile).where(
        StoredFile.entity_type == entity_type,
        StoredFile.entity_id == entity_id,
    )
    if purpose is not None:
        query = query.where(StoredFile.purpose == purpose)
    result = await session.execute(query.order_by(StoredFile.created_at))
    return list(result.scalars().all())


def as_dict(row: StoredFile) -> dict:
    """What an API returns about a file. Never the bytes, never the storage key.

    The key is withheld deliberately: it is not a secret, but publishing it
    invites somebody to build a client that fetches from the object store
    directly and bypasses every access check in this module.
    """
    return {
        "id": str(row.id),
        "filename": row.filename,
        "content_type": row.content_type,
        "byte_size": row.byte_size,
        "sha256": row.sha256,
        "purpose": row.purpose,
        "uploaded_at": row.created_at,
        "available": row.is_available,
        "deleted_at": row.deleted_at,
        "deleted_reason": row.deleted_reason,
    }
