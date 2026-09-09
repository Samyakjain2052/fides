"""Fulfilling a rights request: identity, correspondence, disclosure.

The product could previously track a request and not answer one. These are the
tests for answering it, and the ones that matter most are the negative
assertions — that an administrator cannot read what they should not, and that
"we prepared it" never reads as "they received it".
"""

from __future__ import annotations

import base64
import io
import uuid
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.errors import Conflict, NotFound, ValidationProblem
from app.db.session import set_tenant_context
from app.models.audit import AuditAction, AuditEvent
from app.models.consent import DataPrincipal
from app.models.dsar import DsarRequest
from app.models.dsar_message import DsarMessage
from app.models.stored_file import StoredFile
from app.services import disclosure, dsar_fulfilment_service, dsar_service, file_service
from app.services.audit_service import Actor
from app.services.dsar_service import DsarRefused
from app.storage import ObjectNotFound, get_storage, reset_storage

# No module-level asyncio mark: `asyncio_mode = "auto"` already applies
# one to every async test, and marking the synchronous tests here too
# only produces warnings.

TEST_KEY = base64.b64encode(b"k" * 32).decode()
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff" + b"\x00" * 64

#: What the engine would return. Chosen to exercise every classification:
#: ordinary values, sensitive ones, precise location, and credential material.
ENGINE_DATA = {
    "crm:contacts": [
        {
            "email": "asha@example.com",
            "city": "Pune",
            "country": "India",
            "street_address": "12 Laxmi Road",
            "postal_code": "411002",
            "pan": "ABCDE1234F",
            "password_hash": "$argon2id$v=19$m=65536",
        }
    ],
    "billing:invoices": [
        {"invoice_no": "INV-1", "amount": "4999.00", "item": "Annual plan"},
    ],
}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    from app.core.config import get_settings

    monkeypatch.setenv("DS_CREDENTIAL_ENCRYPTION_KEY", TEST_KEY)
    monkeypatch.setenv("DS_STORAGE_BACKEND", "local")
    monkeypatch.setenv("DS_STORAGE_LOCAL_ROOT", str(tmp_path / "objects"))
    get_settings.cache_clear()
    reset_storage()
    yield
    get_settings.cache_clear()
    reset_storage()


def _actor(tenant: dict) -> Actor:
    return Actor(type="user", id=tenant["admin_id"], label="dpo@test")


async def _request(factory, tenant, *, type_: str = "access") -> uuid.UUID:
    async with factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant["id"])
            principal = DataPrincipal(
                tenant_id=tenant["id"],
                external_id=f"cust-{uuid.uuid4().hex[:8]}",
                email="asha@example.com",
            )
            session.add(principal)
            await session.flush()
            row = await dsar_service.submit(
                session,
                tenant_id=tenant["id"],
                actor=_actor(tenant),
                principal_id=principal.id,
                type=type_,
            )
            return row.id


async def _fake_engine(monkeypatch, data=ENGINE_DATA):
    """Pretend the engine answered, without standing one up.

    Patches the one private helper that talks to it. The alternative is an
    httpx transport mock, which would test httpx.
    """
    async def _stub(request):
        return data

    monkeypatch.setattr(dsar_fulfilment_service, "_engine_data", _stub)


async def _with_engine_ref(factory, tenant, request_id):
    async with factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant["id"])
            row = await session.scalar(
                select(DsarRequest).where(DsarRequest.id == request_id)
            )
            row.engine_ref = "pr_" + uuid.uuid4().hex[:8]
            row.status = "in_progress"


# --------------------------------------------------------------------------- #
# Classification — what an admin may and may not read
# --------------------------------------------------------------------------- #

def test_government_id_and_financial_fields_are_sensitive():
    assert disclosure.classification("pan") == "sensitive"
    assert disclosure.classification("aadhaar_number") == "sensitive"
    assert disclosure.classification("card_number") == "sensitive"
    assert disclosure.classification("amount") == "sensitive"


def test_precise_location_is_sensitive_and_coarse_location_is_not():
    """The line is granularity. A city identifies nobody; a street does."""
    assert disclosure.classification("street_address") == "sensitive"
    assert disclosure.classification("postal_code") == "sensitive"
    assert disclosure.classification("latitude") == "sensitive"
    assert disclosure.classification("city") == "ordinary"
    assert disclosure.classification("country") == "ordinary"


def test_credential_material_is_never_disclosed_to_anyone():
    """Not even to the person. It is our credential about them, not their data.

    A password hash in a disclosure package is something an attacker who
    social-engineers a rights request can crack offline at leisure.
    """
    for field in ("password_hash", "api_key", "session_id", "reset_token"):
        assert disclosure.classification(field) == "never", field


def test_the_admin_preview_excludes_sensitive_values_and_keeps_the_field_names():
    result = disclosure.preview(ENGINE_DATA)
    by_field = {r["field"]: r["value"] for r in result["rows"]}

    # Shown: the admin needs these to confirm the right person.
    assert by_field["email"] == "asha@example.com"
    assert by_field["city"] == "Pune"
    # Excluded, not masked.
    assert by_field["pan"] == disclosure.EXCLUDED
    assert by_field["street_address"] == disclosure.EXCLUDED
    assert by_field["postal_code"] == disclosure.EXCLUDED
    # Withheld outright.
    assert by_field["password_hash"] == disclosure.WITHHELD
    # The field name is still there — the admin can see THAT we hold a PAN.
    assert "pan" in by_field


def test_the_preview_never_leaks_a_sensitive_value_anywhere_in_its_output():
    """Belt and braces: the raw value must not survive in any field of the row."""
    result = disclosure.preview(ENGINE_DATA)
    blob = repr(result)
    assert "ABCDE1234F" not in blob
    assert "12 Laxmi Road" not in blob
    assert "411002" not in blob
    assert "$argon2id" not in blob


# --------------------------------------------------------------------------- #
# The package itself
# --------------------------------------------------------------------------- #

def test_the_package_contains_everything_for_the_person():
    """It is their data. That is the whole right."""
    blob = disclosure.build_zip(reference="DSAR-1", data=ENGINE_DATA)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = set(archive.namelist())
        assert {"README.txt", "summary.csv", "data.json"} <= names
        csv_text = archive.read("summary.csv").decode("utf-8-sig")

    # The sensitive values ARE present for the subject.
    assert "ABCDE1234F" in csv_text
    assert "12 Laxmi Road" in csv_text
    # But credential material is not.
    assert "$argon2id" not in csv_text
    assert disclosure.WITHHELD in csv_text


def test_the_readme_is_plain_language_and_names_the_sources():
    blob = disclosure.build_zip(reference="DSAR-7", data=ENGINE_DATA)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        readme = archive.read("README.txt").decode()
    assert "DSAR-7" in readme
    assert "crm:contacts" in readme
    # A right to a summary is not satisfied by a summary nobody can read.
    assert "spreadsheet" in readme.lower()
    assert "Data Protection Board" in readme


def test_two_packages_from_the_same_data_hash_identically():
    """Fixed member timestamps, so sha256 is evidence of something.

    Zip stores mtimes. Letting them default to "now" would give a different
    digest every run for the same disclosure, which makes the hash useless as a
    record of what was sent.
    """
    import hashlib

    now = datetime(2026, 5, 1, tzinfo=UTC)
    first = disclosure.build_zip(reference="D", data=ENGINE_DATA, produced_at=now)
    second = disclosure.build_zip(reference="D", data=ENGINE_DATA, produced_at=now)
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()


def test_an_attachment_cannot_displace_the_real_summary():
    blob = disclosure.build_zip(
        reference="D", data=ENGINE_DATA,
        attachments=[("summary.csv", b"not the real one")],
    )
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert archive.read("summary.csv") != b"not the real one"
        assert archive.read("attachments/summary.csv") == b"not the real one"


def test_nested_engine_output_is_flattened_with_dotted_paths():
    rows = disclosure.flatten({"c": [{"a": {"b": "v"}}]})
    assert {"collection": "c", "record": "1", "field": "a.b", "value": "v"} in rows


# --------------------------------------------------------------------------- #
# Assembly and delivery
# --------------------------------------------------------------------------- #

async def test_assembling_requires_the_reference_typed_back(
    app_session_factory, tenant_a, monkeypatch
):
    await _fake_engine(monkeypatch)
    request_id = await _request(app_session_factory, tenant_a)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            with pytest.raises(DsarRefused) as err:
                await dsar_fulfilment_service.assemble(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    request=row, confirm_reference="nope",
                )
    assert "type the request's reference" in str(err.value)


async def test_assembling_an_erasure_request_is_refused(
    app_session_factory, tenant_a, monkeypatch
):
    """Answering a question nobody asked."""
    await _fake_engine(monkeypatch)
    request_id = await _request(app_session_factory, tenant_a, type_="erasure")

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            with pytest.raises(DsarRefused) as err:
                await dsar_fulfilment_service.assemble(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    request=row, confirm_reference=row.reference,
                )
    assert "not an access request" in str(err.value)


async def test_an_empty_result_refuses_rather_than_sending_an_empty_file(
    app_session_factory, tenant_a, monkeypatch
):
    """A nil return must be deliberate.

    "We hold nothing about you" is a legitimate answer, and it is also what an
    unreachable engine looks like. Sending an empty zip would make the two
    indistinguishable to the person receiving it.
    """
    await _fake_engine(monkeypatch, data={})
    request_id = await _request(app_session_factory, tenant_a)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            with pytest.raises(DsarRefused) as err:
                await dsar_fulfilment_service.assemble(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    request=row, confirm_reference=row.reference,
                )
    assert "nothing to package" in str(err.value)
    assert "do not send an empty file" in str(err.value)


async def test_assembling_stores_a_hashed_package_and_does_not_deliver_it(
    app_session_factory, tenant_a, monkeypatch
):
    """The distinction the whole module turns on."""
    await _fake_engine(monkeypatch)
    request_id = await _request(app_session_factory, tenant_a)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            stored, summary = await dsar_fulfilment_service.assemble(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, confirm_reference=row.reference,
            )
            assert stored.sha256
            assert summary["field_count"] > 0

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await session.scalar(
            select(DsarRequest).where(DsarRequest.id == request_id)
        )
        assert row.package_file_id is not None
        assert row.package_assembled_at is not None
        assert row.package_delivered_at is None, "assembling must not deliver"
        assert row.package_available_until > datetime.now(UTC)


async def test_delivering_puts_it_in_the_thread_and_stamps_delivery(
    app_session_factory, tenant_a, monkeypatch
):
    await _fake_engine(monkeypatch)
    request_id = await _request(app_session_factory, tenant_a)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            await dsar_fulfilment_service.assemble(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, confirm_reference=row.reference,
            )
            message = await dsar_fulfilment_service.deliver(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, author_user_id=tenant_a["admin_id"],
                author_label="DPO",
            )
            message_id = message.id

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await session.scalar(
            select(DsarRequest).where(DsarRequest.id == request_id)
        )
        assert row.package_delivered_at is not None
        # The package now hangs off the message, so it renders as its attachment.
        attachments = await file_service.for_entity(
            session, entity_type="dsar_message", entity_id=message_id
        )
        assert len(attachments) == 1
        assert attachments[0].purpose == "dsar_package"


async def test_delivering_without_assembling_is_refused(
    app_session_factory, tenant_a
):
    request_id = await _request(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            with pytest.raises(DsarRefused) as err:
                await dsar_fulfilment_service.deliver(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    request=row, author_user_id=tenant_a["admin_id"],
                    author_label="DPO",
                )
    assert "no assembled package" in str(err.value)


async def test_reassembling_purges_the_previous_package(
    app_session_factory, tenant_a, monkeypatch
):
    """Never more than one live copy of somebody's whole record."""
    await _fake_engine(monkeypatch)
    request_id = await _request(app_session_factory, tenant_a)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            first, _ = await dsar_fulfilment_service.assemble(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, confirm_reference=row.reference,
            )
            first_id, first_key = first.id, first.storage_key

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            second, _ = await dsar_fulfilment_service.assemble(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, confirm_reference=row.reference,
            )
            assert second.id != first_id

    with pytest.raises(ObjectNotFound):
        await get_storage().get(first_key)


async def test_a_staff_capability_cannot_download_the_package(
    app_session_factory, tenant_a, monkeypatch
):
    """The point of the redacted preview.

    If a DPO could open the package, excluding sensitive values from their
    screen would be theatre — they would simply download it instead.
    """
    await _fake_engine(monkeypatch)
    request_id = await _request(app_session_factory, tenant_a)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            stored, _ = await dsar_fulfilment_service.assemble(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, confirm_reference=row.reference,
            )
            file_id = stored.id

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await session.scalar(select(StoredFile).where(StoredFile.id == file_id))
        for capability in ("dsar:read", "dsar:process", "audit:read", "tenant:manage"):
            with pytest.raises(NotFound):
                await file_service.assert_readable(
                    session, row, capabilities={capability}, principal_ids=set()
                )


async def test_the_subject_can_download_their_own_package(
    app_session_factory, tenant_a, monkeypatch
):
    await _fake_engine(monkeypatch)
    request_id = await _request(app_session_factory, tenant_a)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            stored, _ = await dsar_fulfilment_service.assemble(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, confirm_reference=row.reference,
            )
            file_id, principal_id = stored.id, row.principal_id

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        stored_row = await session.scalar(
            select(StoredFile).where(StoredFile.id == file_id)
        )
        await file_service.assert_readable(
            session, stored_row, capabilities=set(), principal_ids={principal_id},
        )


async def test_someone_elses_package_is_not_readable(
    app_session_factory, tenant_a, monkeypatch
):
    await _fake_engine(monkeypatch)
    request_id = await _request(app_session_factory, tenant_a)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            stored, _ = await dsar_fulfilment_service.assemble(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, confirm_reference=row.reference,
            )
            file_id = stored.id

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        stored_row = await session.scalar(
            select(StoredFile).where(StoredFile.id == file_id)
        )
        with pytest.raises(NotFound):
            await file_service.assert_readable(
                session, stored_row, capabilities=set(),
                principal_ids={uuid.uuid4()},
            )


async def test_an_expired_package_says_expired_rather_than_missing(
    app_session_factory, tenant_a, monkeypatch
):
    """The person is entitled to know it existed and that the window closed."""
    await _fake_engine(monkeypatch)
    request_id = await _request(app_session_factory, tenant_a)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            await dsar_fulfilment_service.assemble(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, confirm_reference=row.reference,
            )
            row.package_available_until = datetime.now(UTC) - timedelta(minutes=1)

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await dsar_service.get(session, tenant_a["id"], request_id)
        with pytest.raises(Conflict) as err:
            await dsar_fulfilment_service.package_for_principal(session, request=row)
    assert "expired" in str(err.value)


async def test_assembly_is_audited_with_the_hash_and_not_the_data(
    app_session_factory, tenant_a, monkeypatch
):
    await _fake_engine(monkeypatch)
    request_id = await _request(app_session_factory, tenant_a)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            await dsar_fulfilment_service.assemble(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, confirm_reference=row.reference,
            )

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == AuditAction.DSAR_PACKAGE_ASSEMBLED
                )
            )
        ).scalars().all()
    assert len(events) == 1
    payload = events[0].payload
    assert payload["sha256"]
    # No disclosed values in the audit trail — auditors read this.
    assert "ABCDE1234F" not in repr(payload)
    assert "asha@example.com" not in repr(payload)


# --------------------------------------------------------------------------- #
# Identity verification
# --------------------------------------------------------------------------- #

async def test_accepting_identity_destroys_the_document(
    app_session_factory, tenant_a
):
    """§8(7). The purpose is served the moment identity is confirmed."""
    request_id = await _request(app_session_factory, tenant_a)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            stored = await dsar_fulfilment_service.submit_identity_document(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, filename="aadhaar.png", data=PNG,
                declared_content_type="image/png",
            )
            key = stored.storage_key

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            await dsar_fulfilment_service.review_identity(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, reviewer_id=tenant_a["admin_id"], accept=True,
            )

    with pytest.raises(ObjectNotFound):
        await get_storage().get(key)

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await session.scalar(
            select(DsarRequest).where(DsarRequest.id == request_id)
        )
        assert row.identity_verified
        assert row.verified_at is not None
        # The record of having held it survives.
        stored_row = await session.scalar(
            select(StoredFile).where(StoredFile.id == row.identity_document_id)
        )
        assert stored_row.deleted_at is not None
        assert "purpose served" in stored_row.deleted_reason


async def test_refusing_identity_requires_a_reason(app_session_factory, tenant_a):
    """The refusal a person is most likely to challenge."""
    request_id = await _request(app_session_factory, tenant_a)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            await dsar_fulfilment_service.submit_identity_document(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, filename="id.png", data=PNG,
                declared_content_type="image/png",
            )
            with pytest.raises(ValidationProblem) as err:
                await dsar_fulfilment_service.review_identity(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    request=row, reviewer_id=tenant_a["admin_id"], accept=False,
                    reason="   ",
                )
    assert "needs a reason" in str(err.value)


async def test_a_refusal_keeps_the_document_and_records_why(
    app_session_factory, tenant_a
):
    """Kept, because the decision may be challenged and reviewed."""
    request_id = await _request(app_session_factory, tenant_a)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            stored = await dsar_fulfilment_service.submit_identity_document(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, filename="id.png", data=PNG,
                declared_content_type="image/png",
            )
            key = stored.storage_key
            await dsar_fulfilment_service.review_identity(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, reviewer_id=tenant_a["admin_id"], accept=False,
                reason="The photograph is too blurred to read the name.",
            )

    assert await get_storage().get(key)

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await session.scalar(
            select(DsarRequest).where(DsarRequest.id == request_id)
        )
        assert not row.identity_verified
        assert "too blurred" in row.identity_rejection_reason
        assert row.verified_at is None


async def test_replacing_a_document_purges_the_old_one_and_reopens_the_decision(
    app_session_factory, tenant_a
):
    """Somebody who uploaded the wrong photograph should not leave it with us.

    And a prior acceptance must not stand over new evidence — otherwise the
    document could be swapped after approval.
    """
    request_id = await _request(app_session_factory, tenant_a)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            first = await dsar_fulfilment_service.submit_identity_document(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, filename="wrong.png", data=PNG,
                declared_content_type="image/png",
            )
            first_key = first.storage_key
            await dsar_fulfilment_service.review_identity(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, reviewer_id=tenant_a["admin_id"], accept=False,
                reason="wrong document",
            )

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            await dsar_fulfilment_service.submit_identity_document(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, filename="right.jpg", data=JPEG,
                declared_content_type="image/jpeg",
            )

    with pytest.raises(ObjectNotFound):
        await get_storage().get(first_key)

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        row = await session.scalar(
            select(DsarRequest).where(DsarRequest.id == request_id)
        )
        assert row.identity_reviewed_at is None, "a new document reopens the decision"
        assert row.identity_rejection_reason is None


async def test_reviewing_with_no_document_is_refused(app_session_factory, tenant_a):
    request_id = await _request(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            with pytest.raises(DsarRefused):
                await dsar_fulfilment_service.review_identity(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    request=row, reviewer_id=tenant_a["admin_id"], accept=True,
                )


async def test_the_identity_audit_payload_carries_no_filename(
    app_session_factory, tenant_a
):
    """A filename is frequently somebody's full name and passport number."""
    request_id = await _request(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            await dsar_fulfilment_service.submit_identity_document(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, filename="Asha-Patel-passport-Z1234567.png",
                data=PNG, declared_content_type="image/png",
            )

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == AuditAction.DSAR_IDENTITY_SUBMITTED
                )
            )
        ).scalars().one()
    assert "Z1234567" not in repr(event.payload)
    assert event.payload["sha256"]


# --------------------------------------------------------------------------- #
# The message thread
# --------------------------------------------------------------------------- #

async def test_a_message_needs_exactly_one_author(app_session_factory, tenant_a):
    """A database CHECK, not service politeness.

    An unattributed message in a statutory correspondence record is not evidence
    of anything.
    """
    from sqlalchemy.exc import IntegrityError

    request_id = await _request(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        session.add(
            DsarMessage(
                tenant_id=tenant_a["id"], dsar_request_id=request_id,
                direction="to_principal", author_label="nobody",
                body="who wrote this?",
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_a_blank_message_is_refused(app_session_factory, tenant_a):
    request_id = await _request(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            with pytest.raises(ValidationProblem):
                await dsar_fulfilment_service.post_message(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    request=row, body="   ", direction="to_principal",
                    author_user_id=tenant_a["admin_id"], author_label="DPO",
                )


async def test_the_notification_about_a_message_carries_no_content(
    app_session_factory, tenant_a
):
    """Email is not a safe place for correspondence about somebody's data."""
    from app.models.notification import Notification

    request_id = await _request(app_session_factory, tenant_a)
    secret = "Your account ending 4417 was closed on 3 March"

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            await dsar_fulfilment_service.post_message(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, body=secret, direction="to_principal",
                author_user_id=tenant_a["admin_id"], author_label="DPO",
            )

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        rows = (await session.execute(select(Notification))).scalars().all()
    assert rows, "a notification should have been queued"
    for note in rows:
        # `pending_body` is the rendered text actually sent. It is dropped once
        # the send reaches a terminal status, so it is the only place the words
        # exist and the right place to assert about them.
        rendered = f"{note.subject_rendered or ''}\n{note.pending_body or ''}"
        assert "4417" not in rendered
        assert secret not in rendered
        # And it does say which request, so the person knows what it is about.
        assert "DSAR" in rendered


async def test_each_message_notifies_separately(app_session_factory, tenant_a):
    """`enqueue` dedupes on (tenant, template, entity).

    Keying this to the request instead of the message would send the first
    message and silently swallow every reply after it — the trap that already
    ate the second password-reset email and the repeat connection alerts.
    """
    from app.models.notification import Notification

    request_id = await _request(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            for n in range(3):
                await dsar_fulfilment_service.post_message(
                    session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    request=row, body=f"message {n}", direction="to_principal",
                    author_user_id=tenant_a["admin_id"], author_label="DPO",
                )

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        rows = (
            await session.execute(
                select(Notification).where(Notification.template_key == "dsar.message")
            )
        ).scalars().all()
    assert len(rows) == 3, f"expected 3 notifications, got {len(rows)}"


async def test_reading_marks_the_other_sides_messages_only(
    app_session_factory, tenant_a
):
    """Marking your own messages read makes the receipt meaningless."""
    request_id = await _request(app_session_factory, tenant_a)

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            await dsar_fulfilment_service.post_message(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, body="from staff", direction="to_principal",
                author_user_id=tenant_a["admin_id"], author_label="DPO",
            )
            await dsar_fulfilment_service.post_message(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, body="from the person", direction="from_principal",
                author_principal_id=row.principal_id, author_label="asha",
                notify=False,
            )

    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            marked = await dsar_fulfilment_service.mark_read(
                session, request_id=request_id, reader_is_staff=True
            )
    assert marked == 1

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_a["id"])
        messages = await dsar_fulfilment_service.thread(
            session, request_id=request_id
        )
    by_direction = {m.direction: m for m in messages}
    assert by_direction["from_principal"].read_at is not None
    assert by_direction["to_principal"].read_at is None


async def test_the_thread_is_isolated_between_tenants(
    app_session_factory, tenant_a, tenant_b
):
    request_id = await _request(app_session_factory, tenant_a)
    async with app_session_factory() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_a["id"])
            row = await dsar_service.get(session, tenant_a["id"], request_id)
            await dsar_fulfilment_service.post_message(
                session, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                request=row, body="private", direction="to_principal",
                author_user_id=tenant_a["admin_id"], author_label="DPO",
            )

    async with app_session_factory() as session:
        await set_tenant_context(session, tenant_b["id"])
        assert await dsar_fulfilment_service.thread(
            session, request_id=request_id
        ) == []
