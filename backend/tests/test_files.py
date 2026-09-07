"""The file substrate: detection, encryption, isolation, expiry, authorisation.

Before this existed the product accepted no uploads, so none of these failure
modes were reachable. Each test below is one of the ways adding uploads could
have gone wrong.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.core.crypto import open_sealed_bytes
from app.core.errors import Conflict, NotFound, ValidationProblem
from app.db.session import set_tenant_context
from app.models.stored_file import StoredFile
from app.services import file_service
from app.storage import get_storage, reset_storage

pytestmark = pytest.mark.asyncio

# A key for the suite. Real deployments read this from Key Vault.
TEST_KEY = base64.b64encode(b"k" * 32).decode()


@pytest.fixture(autouse=True)
def _storage(monkeypatch, tmp_path):
    """A key, and a storage root of this test's own.

    The root is per-test so one test's objects cannot satisfy another's
    assertions, and so nothing accumulates in the container between runs. The
    settings cache is cleared on both sides — a stale `Settings` would inherit
    whatever an earlier import happened to see.
    """
    from app.core.config import get_settings

    monkeypatch.setenv("DS_CREDENTIAL_ENCRYPTION_KEY", TEST_KEY)
    monkeypatch.setenv("DS_STORAGE_BACKEND", "local")
    monkeypatch.setenv("DS_STORAGE_LOCAL_ROOT", str(tmp_path / "objects"))
    get_settings.cache_clear()
    reset_storage()
    yield
    get_settings.cache_clear()
    reset_storage()

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"\x00" * 64


async def _store(factory, tenant, **kw):
    async with factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant["id"])
            row = await file_service.store(
                session,
                tenant_id=tenant["id"],
                purpose=kw.pop("purpose", "dsar_evidence"),
                entity_type=kw.pop("entity_type", "dsar_request"),
                entity_id=kw.pop("entity_id", uuid.uuid4()),
                filename=kw.pop("filename", "proof.png"),
                data=kw.pop("data", PNG),
                **kw,
            )
            return row.id, row.storage_key, row.sha256


# --------------------------------------------------------------------------- #
# Type detection
# --------------------------------------------------------------------------- #

async def test_the_declared_content_type_does_not_decide_what_is_stored(
    app_session_factory, tenant_a
):
    """HTML labelled as an image is refused, not stored and echoed back.

    This is the stored-XSS case. A file served from our own origin with a
    content type the uploader chose is a scripting primitive, so detection wins
    and an unrecognised payload is a refusal.
    """
    with pytest.raises(ValidationProblem):
        await _store(
            app_session_factory,
            tenant_a,
            data=b"<html><script>alert(1)</script></html>",
            declared_content_type="image/png",
            filename="totally-a-png.png",
        )


async def test_a_real_png_is_detected_regardless_of_what_the_caller_claimed(
    app_session_factory, tenant_a
):
    file_id, _, _ = await _store(
        app_session_factory, tenant_a, data=PNG,
        declared_content_type="application/pdf",
    )
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await session.scalar(select(StoredFile).where(StoredFile.id == file_id))
    assert row.content_type == "image/png"


async def test_svg_is_not_accepted_at_all(app_session_factory, tenant_a):
    """An SVG is a script container. No purpose here needs one."""
    with pytest.raises(ValidationProblem):
        await _store(
            app_session_factory, tenant_a,
            data=b'<svg xmlns="http://www.w3.org/2000/svg"><script>x</script></svg>',
            declared_content_type="image/svg+xml",
            filename="logo.svg",
        )


async def test_text_must_actually_decode_as_utf8(app_session_factory, tenant_a):
    with pytest.raises(ValidationProblem):
        await _store(
            app_session_factory, tenant_a,
            data=b"\xff\xfe\x00binary-not-text",
            declared_content_type="text/csv",
            filename="data.csv",
        )


async def test_csv_evidence_is_accepted(app_session_factory, tenant_a):
    file_id, _, _ = await _store(
        app_session_factory, tenant_a,
        data=b"email,city\na@b.com,Pune\n",
        declared_content_type="text/csv", filename="rows.csv",
    )
    assert file_id is not None


async def test_an_empty_file_is_refused(app_session_factory, tenant_a):
    with pytest.raises(ValidationProblem):
        await _store(app_session_factory, tenant_a, data=b"")


async def test_a_file_over_the_ceiling_is_refused(app_session_factory, tenant_a):
    from app.core.config import get_settings

    oversize = PNG + b"\x00" * get_settings().max_upload_bytes
    with pytest.raises(ValidationProblem) as err:
        await _store(app_session_factory, tenant_a, data=oversize)
    assert "MB or smaller" in str(err.value)


# --------------------------------------------------------------------------- #
# Names are display text, never paths
# --------------------------------------------------------------------------- #

async def test_a_traversing_filename_cannot_reach_the_filesystem(
    app_session_factory, tenant_a
):
    """`../../etc/passwd` is stored as a name, and the key is generated."""
    file_id, key, _ = await _store(
        app_session_factory, tenant_a, filename="../../../../etc/passwd"
    )
    assert ".." not in key
    assert str(file_id.hex) in key

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await session.scalar(select(StoredFile).where(StoredFile.id == file_id))
    # The directory parts are stripped; what remains is inert display text.
    assert "/" not in row.filename
    assert row.filename == "passwd"


async def test_a_quote_in_a_filename_cannot_break_the_download_header(
    app_session_factory, tenant_a
):
    file_id, _, _ = await _store(
        app_session_factory, tenant_a,
        filename='evil".png',
    )
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await session.scalar(select(StoredFile).where(StoredFile.id == file_id))
    assert '"' not in row.filename


# --------------------------------------------------------------------------- #
# Encryption at rest
# --------------------------------------------------------------------------- #

async def test_the_object_store_never_holds_plaintext(app_session_factory, tenant_a):
    secret = PDF + b"AADHAAR 1234 5678 9012"
    file_id, key, _ = await _store(
        app_session_factory, tenant_a, data=secret,
        declared_content_type="application/pdf", filename="id.pdf",
    )
    raw = await get_storage().get(key)
    assert b"AADHAAR" not in raw
    assert raw != secret
    # And it opens with the file's own id as AAD.
    assert open_sealed_bytes(raw, aad=str(file_id)) == secret


async def test_an_object_moved_to_another_row_fails_to_open(
    app_session_factory, tenant_a
):
    """The AAD binding. Swapping one person's document for another's is caught.

    Without the file id as additional authenticated data, an attacker with write
    access to the blob container could replace the ID document attached to one
    request with the one from another and the swap would decrypt cleanly.
    """
    first, first_key, _ = await _store(app_session_factory, tenant_a, data=PNG)
    second, second_key, _ = await _store(app_session_factory, tenant_a, data=JPEG)

    # Move the second object over the first's key.
    await get_storage().put(first_key, await get_storage().get(second_key))

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        from app.core.crypto import CredentialSealError

        with pytest.raises(CredentialSealError):
            await file_service.fetch(session, file_id=first)


async def test_a_corrupted_object_is_not_returned(app_session_factory, tenant_a):
    file_id, key, _ = await _store(app_session_factory, tenant_a)
    sealed = bytearray(await get_storage().get(key))
    sealed[-1] ^= 0xFF
    await get_storage().put(key, bytes(sealed))

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        from app.core.crypto import CredentialSealError

        with pytest.raises(CredentialSealError):
            await file_service.fetch(session, file_id=file_id)


async def test_a_stored_hash_is_of_the_plaintext(app_session_factory, tenant_a):
    _, _, digest = await _store(app_session_factory, tenant_a, data=PDF,
                                declared_content_type="application/pdf")
    assert digest == hashlib.sha256(PDF).hexdigest()


async def test_a_round_trip_returns_exactly_what_went_in(app_session_factory, tenant_a):
    payload = JPEG + b"some scanned contract bytes"
    file_id, _, _ = await _store(
        app_session_factory, tenant_a, data=payload,
        declared_content_type="image/jpeg", filename="contract.jpg",
    )
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row, data = await file_service.fetch(session, file_id=file_id)
    assert data == payload
    assert row.byte_size == len(payload)


# --------------------------------------------------------------------------- #
# Tenant isolation
# --------------------------------------------------------------------------- #

async def test_one_tenant_cannot_resolve_anothers_file_id(
    app_session_factory, tenant_a, tenant_b
):
    """RLS, not a WHERE clause. The id is a valid UUID and still finds nothing."""
    file_id, _, _ = await _store(app_session_factory, tenant_a)

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_b["id"])
        with pytest.raises(NotFound):
            await file_service.fetch(session, file_id=file_id)


async def test_stored_files_is_covered_by_row_level_security(app_session_factory):
    """The table is RLS-enabled and FORCEd, like every other tenant table."""
    async with app_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'stored_files'"
                )
            )
        ).one()
    assert row[0] is True, "RLS not enabled on stored_files"
    assert row[1] is True, "RLS not FORCEd on stored_files"


async def test_every_tenant_scoped_table_has_a_forced_rls_policy(app_session_factory):
    """The promise in models/__init__.py, actually enforced.

    That module says "a test asserts every tenant-scoped table appears in it —
    so adding a table without a policy fails the build rather than leaking
    quietly". No such test existed, and the list had already drifted:
    `password_resets` was created with a policy but never added to it.

    This checks the real thing rather than the list — every table carrying a
    `tenant_id` must have RLS enabled, FORCEd, and a policy attached. A new
    table with customer data in it now fails here instead of leaking.
    """
    async with app_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT c.relname,
                           c.relrowsecurity,
                           c.relforcerowsecurity,
                           (SELECT count(*) FROM pg_policies p
                              WHERE p.tablename = c.relname) AS policies
                      FROM pg_class c
                      JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = 'public'
                       AND c.relkind = 'r'
                       AND EXISTS (
                             SELECT 1 FROM pg_attribute a
                              WHERE a.attrelid = c.oid
                                AND a.attname = 'tenant_id'
                                AND NOT a.attisdropped
                           )
                     ORDER BY c.relname
                    """
                )
            )
        ).all()

    assert rows, "found no tenant-scoped tables at all — the query is wrong"
    unprotected = [
        r[0] for r in rows if not (r[1] and r[2] and r[3] > 0)
    ]
    assert not unprotected, (
        "these tables carry tenant_id but are not fully protected by RLS: "
        f"{unprotected}"
    )


# --------------------------------------------------------------------------- #
# Expiry — data minimisation for identity documents
# --------------------------------------------------------------------------- #

async def test_an_identity_document_gets_an_expiry_at_upload(
    app_session_factory, tenant_a
):
    """Not a cleanup job somebody remembers later. Same INSERT."""
    file_id, _, _ = await _store(
        app_session_factory, tenant_a, purpose="dsar_id_document",
    )
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await session.scalar(select(StoredFile).where(StoredFile.id == file_id))
    assert row.delete_after is not None
    assert row.delete_after > datetime.now(UTC)


async def test_ordinary_evidence_has_no_automatic_expiry(app_session_factory, tenant_a):
    """Evidence that a request was handled is part of the record and stays."""
    file_id, _, _ = await _store(
        app_session_factory, tenant_a, purpose="dsar_evidence",
    )
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await session.scalar(select(StoredFile).where(StoredFile.id == file_id))
    assert row.delete_after is None


async def test_purging_destroys_the_bytes_and_keeps_the_record(
    app_session_factory, tenant_a
):
    """"We held an ID document and destroyed it on this date" is the fact to keep."""
    file_id, key, _ = await _store(
        app_session_factory, tenant_a, purpose="dsar_id_document",
    )
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            await file_service.purge(session, file_id=file_id, reason="verified")

    from app.storage import ObjectNotFound

    with pytest.raises(ObjectNotFound):
        await get_storage().get(key)

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await session.scalar(select(StoredFile).where(StoredFile.id == file_id))
        assert row is not None, "the metadata row must survive its bytes"
        assert row.deleted_at is not None
        assert row.deleted_reason == "verified"
        assert row.storage_key is None
        with pytest.raises(Conflict) as err:
            await file_service.fetch(session, file_id=file_id)
    assert "deleted" in str(err.value)


async def test_the_retention_sweep_destroys_expired_documents(
    app_session_factory, tenant_a
):
    file_id, key, _ = await _store(
        app_session_factory, tenant_a, purpose="dsar_id_document",
    )
    # Backdate it past its expiry.
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await session.scalar(
                select(StoredFile).where(StoredFile.id == file_id)
            )
            row.delete_after = datetime.now(UTC) - timedelta(days=1)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            count = await file_service.purge_expired(session, tenant_id=tenant_a["id"])
    assert count == 1

    from app.storage import ObjectNotFound

    with pytest.raises(ObjectNotFound):
        await get_storage().get(key)


async def test_the_sweep_leaves_unexpired_documents_alone(app_session_factory, tenant_a):
    file_id, key, _ = await _store(
        app_session_factory, tenant_a, purpose="dsar_id_document",
    )
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            count = await file_service.purge_expired(session, tenant_id=tenant_a["id"])
    assert count == 0
    assert await get_storage().get(key)


# --------------------------------------------------------------------------- #
# Authorisation is by purpose, not by holding an id
# --------------------------------------------------------------------------- #

async def _assert_readable(factory, tenant, file_id, caps, principals=frozenset()):
    async with factory() as session:
        await set_tenant_context(session, tenant["id"])
        row = await session.scalar(select(StoredFile).where(StoredFile.id == file_id))
        await file_service.assert_readable(
            session, row, capabilities=set(caps), principal_ids=set(principals)
        )


async def test_an_auditor_with_dsar_read_cannot_open_an_identity_document(
    app_session_factory, tenant_a
):
    """`dsar:process`, not `dsar:read`.

    An auditor has a legitimate need to see THAT identity was verified and no
    need at all to look at the photograph.
    """
    file_id, _, _ = await _store(
        app_session_factory, tenant_a, purpose="dsar_id_document",
    )
    with pytest.raises(NotFound):
        await _assert_readable(app_session_factory, tenant_a, file_id, {"dsar:read"})


async def test_a_processor_can_open_an_identity_document(app_session_factory, tenant_a):
    file_id, _, _ = await _store(
        app_session_factory, tenant_a, purpose="dsar_id_document",
    )
    await _assert_readable(app_session_factory, tenant_a, file_id, {"dsar:process"})


async def test_a_grievance_capability_does_not_open_a_dsar_file(
    app_session_factory, tenant_a
):
    """A leaked id from one flow is not redeemable in another."""
    file_id, _, _ = await _store(
        app_session_factory, tenant_a, purpose="dsar_evidence",
    )
    with pytest.raises(NotFound):
        await _assert_readable(
            app_session_factory, tenant_a, file_id, {"grievance:read"}
        )


async def test_a_caller_with_no_capabilities_gets_not_found_not_forbidden(
    app_session_factory, tenant_a
):
    """404, never 403.

    "This exists but you may not have it" confirms a document about a named
    person exists to somebody who should not know that.
    """
    file_id, _, _ = await _store(app_session_factory, tenant_a)
    with pytest.raises(NotFound):
        await _assert_readable(app_session_factory, tenant_a, file_id, set())


async def test_a_file_for_an_unknown_entity_kind_is_refused(
    app_session_factory, tenant_a
):
    """A new flow that forgets to extend `_belongs_to` fails closed."""
    file_id, _, _ = await _store(
        app_session_factory, tenant_a,
        purpose="dsar_package", entity_type="something_new",
    )
    with pytest.raises(NotFound):
        await _assert_readable(
            app_session_factory, tenant_a, file_id, set(), {uuid.uuid4()}
        )
