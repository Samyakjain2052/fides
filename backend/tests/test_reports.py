"""Reports — where a fabricated number would do the most damage.

This module's whole claim is that every figure came from a query. So the tests are
mostly *cross-checks*: build the report, then count the same thing independently,
and assert the two agree. A report test that only asserts "200 OK and some rows"
would have passed for both artifacts this codebase already had to delete — the
invented trend line and the `rowCount * 7919` "signature".

The other half is about honesty at the edges: an empty period, a truncated
export, a chain that was never verified, and a period nobody should be allowed to
ask for.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.core.errors import Conflict
from app.db.session import set_tenant_context
from app.models.audit import AuditEvent
from app.models.consent import Consent, DataPrincipal
from app.models.dsar import DsarRequest
from app.models.grievance import Grievance
from app.services import (
    audit_service,
    consent_service,
    dsar_service,
    grievance_service,
    notice_service,
    report_service,
)
from app.services.audit_service import Actor
from app.services.notification_providers import SendResult

CATEGORY = "Contact Data"


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


def _actor(tenant: dict) -> Actor:
    return Actor(type="user", id=tenant["admin_id"], label="dpo@test")


class _Silent:
    """Notifications are not what these tests are about."""

    name = "silent"

    async def send(self, *, to, subject, body, channel, html_body=None):
        return SendResult(ok=True, provider_message_id="x")


@pytest.fixture(autouse=True)
def provider(monkeypatch):
    monkeypatch.setattr(
        "app.services.notification_providers.get_provider", lambda: _Silent()
    )


@pytest.fixture
async def client():
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _period(days: int = 30) -> report_service.Period:
    now = datetime.now(UTC)
    return report_service.Period(start=now - timedelta(days=days), end=now + timedelta(minutes=1))


# --------------------------------------------------------------------------- #
# World building
# --------------------------------------------------------------------------- #

async def _purpose(session, tenant, key: str | None = None):
    purpose = await notice_service.create_purpose(
        session, tenant_id=tenant["id"], actor=_actor(tenant),
        key=key or f"p{uuid.uuid4().hex[:8]}", name="Marketing", category=CATEGORY,
    )
    notice = await notice_service.draft_notice(
        session, tenant_id=tenant["id"], actor=_actor(tenant), purpose_id=purpose.id,
        content="We use your email.", data_collected="Email",
        user_rights="Withdraw anytime.", withdrawal_policy="Stops in 24h.",
    )
    await notice_service.publish_notice(
        session, tenant_id=tenant["id"], actor=_actor(tenant), notice_id=notice.id
    )
    return purpose


async def _principal(session, tenant, email="person@example.com"):
    row = DataPrincipal(
        tenant_id=tenant["id"], external_id=f"cust-{uuid.uuid4().hex[:8]}", email=email
    )
    session.add(row)
    await session.flush()
    return row


async def _world(session, tenant, *, consents: int = 3, withdraw: int = 1):
    """N granted consents, some of them withdrawn, plus a DSAR and a grievance."""
    purpose = await _purpose(session, tenant)
    principals = []
    for i in range(consents):
        p = await _principal(session, tenant, email=f"p{i}@example.com")
        await consent_service.grant(
            session, tenant_id=tenant["id"], actor=_actor(tenant),
            principal_id=p.id, purpose_id=purpose.id,
        )
        principals.append(p)
    for p in principals[:withdraw]:
        await consent_service.withdraw(
            session, tenant_id=tenant["id"], actor=_actor(tenant),
            principal_id=p.id, purpose_id=purpose.id,
        )
    await dsar_service.submit(
        session, tenant_id=tenant["id"], actor=_actor(tenant),
        principal_id=principals[0].id, type="access",
    )
    await grievance_service.file(
        session, tenant_id=tenant["id"], actor=_actor(tenant),
        category="consent_violation",
        description="You kept emailing me after I withdrew consent.",
        principal_id=principals[0].id,
    )
    await session.flush()
    return purpose, principals


# --------------------------------------------------------------------------- #
# Every figure matches a direct query
# --------------------------------------------------------------------------- #

async def test_the_consent_register_row_count_matches_a_direct_count(
    app_session_factory, tenant_a
):
    """The cross-check this module exists to survive."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a, consents=4, withdraw=2)
        period = _period()

        counted = await report_service.count_rows(
            s, tenant_id=tenant_a["id"], key="consent_register", period=period
        )
        direct = await s.scalar(
            select(func.count()).select_from(Consent).where(
                Consent.tenant_id == tenant_a["id"],
                Consent.given_at >= period.start,
                Consent.given_at < period.end,
            )
        )
        assert counted == direct == 4


async def test_the_count_and_the_streamed_rows_agree(app_session_factory, tenant_a):
    """A count that can disagree with the rows is how a report says 4 and emits 400.

    Both come from the same statement — `count_rows` swaps the projection and
    keeps the FROM and WHERE — and this asserts the consequence rather than the
    mechanism.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a, consents=5, withdraw=2)
        period = _period()
        for key in report_service.REPORTS:
            total = await report_service.count_rows(
                s, tenant_id=tenant_a["id"], key=key, period=period
            )
            emitted = 0
            async for _ in report_service._iter_rows(  # noqa: SLF001
                s, tenant_id=tenant_a["id"], key=key, period=period
            ):
                emitted += 1
            assert emitted == total, f"{key}: counted {total}, emitted {emitted}"


async def test_consent_activity_counts_events_not_consents(
    app_session_factory, tenant_a
):
    """A consent granted and withdrawn in the same period is two events.

    That is the question the report answers, and collapsing it to one row would
    hide every withdrawal that happened in the window it was granted.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a, consents=3, withdraw=2)
        period = _period()

        total = await report_service.count_rows(
            s, tenant_id=tenant_a["id"], key="consent_activity", period=period
        )
        # 3 grants + 2 withdrawals.
        assert total == 5

        rows = []
        async for r in report_service._iter_rows(  # noqa: SLF001
            s, tenant_id=tenant_a["id"], key="consent_activity", period=period
        ):
            rows.append(r)
        events = sorted(r["event"] for r in rows)
        assert events == ["granted"] * 3 + ["withdrawn"] * 2


async def test_dsar_met_deadline_is_null_while_open_not_true(
    app_session_factory, tenant_a
):
    """An unanswered request has not met a deadline.

    Reporting an open request as a pass is how an SLA figure ends up flattering
    the fiduciary — the single most likely place for this module to lie.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        _purpose_, principals = await _world(s, tenant_a, consents=1, withdraw=0)
        period = _period()

        rows = []
        async for r in report_service._iter_rows(  # noqa: SLF001
            s, tenant_id=tenant_a["id"], key="dsar_register", period=period
        ):
            rows.append(r)
        assert rows, "the world builder raised a request"
        open_rows = [r for r in rows if r["resolved_at"] is None]
        assert open_rows
        for r in open_rows:
            assert r["met_deadline"] is None, "open must not report as met"
            assert r["days_taken"] is None


async def test_dsar_met_deadline_is_true_when_resolved_in_time(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        _p, principals = await _world(s, tenant_a, consents=1, withdraw=0)
        request = await s.scalar(select(DsarRequest))
        # Through the real state machine — `received` cannot jump to `completed`,
        # and a test that forced the column would be reporting on a row shape the
        # product cannot produce.
        for to_status in ("in_progress", "completed"):
            if request.status != to_status:
                await dsar_service.change_status(
                    s, tenant_id=tenant_a["id"], actor=_actor(tenant_a),
                    request=request, to_status=to_status,
                )
        rows = []
        async for r in report_service._iter_rows(  # noqa: SLF001
            s, tenant_id=tenant_a["id"], key="dsar_register", period=_period()
        ):
            rows.append(r)
        row = next(r for r in rows if r["reference"] == request.reference)
        assert row["met_deadline"] is True
        assert row["days_taken"] == 0


async def test_the_grievance_register_reports_verification_honestly(
    app_session_factory, tenant_a
):
    """`contact_verified` is on the register because it changes what the row means.

    An unconfirmed anonymous complaint is recorded and counted but never escalates
    on its own, and a register that omitted that would overstate how much of the
    queue is actually live.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await grievance_service.file(
            s, tenant_id=tenant_a["id"], actor=_actor(tenant_a), category="other",
            description="Filed anonymously, address not yet confirmed.",
            contact_email="anon@example.com", require_verification=True,
        )
        rows = []
        async for r in report_service._iter_rows(  # noqa: SLF001
            s, tenant_id=tenant_a["id"], key="grievance_register", period=_period()
        ):
            rows.append(r)
        row = next(r for r in rows if r["contact_email"] == "anon@example.com")
        assert row["contact_verified"] is False
        assert row["escalated"] is False


async def test_the_audit_extract_preserves_the_hash_links(app_session_factory, tenant_a):
    """The chain only reads as a chain in sequence order, with prev_hash intact.

    A reader must be able to walk the export and check that each entry's
    `prev_hash` is the previous entry's `hash`. Sorting by anything but `seq`
    would break that silently.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a, consents=2, withdraw=1)
        rows = []
        async for r in report_service._iter_rows(  # noqa: SLF001
            s, tenant_id=tenant_a["id"], key="audit_extract", period=_period()
        ):
            rows.append(r)

        assert len(rows) > 3
        assert [r["seq"] for r in rows] == sorted(r["seq"] for r in rows)
        for earlier, later in zip(rows, rows[1:], strict=False):
            assert later["prev_hash"] == earlier["hash"], (
                f"chain broken between seq {earlier['seq']} and {later['seq']}"
            )


async def test_the_audit_extract_omits_payloads(app_session_factory, tenant_a):
    """Audit payloads hold personal data.

    Exporting the whole chain body would be a second copy of it, outside every
    control that governs the first. The columns are the contract, so this asserts
    on the contract.
    """
    assert "payload" not in report_service.REPORTS["audit_extract"].columns
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a, consents=1, withdraw=0)
        async for row in report_service._iter_rows(  # noqa: SLF001
            s, tenant_id=tenant_a["id"], key="audit_extract", period=_period()
        ):
            assert "payload" not in row
            break


# --------------------------------------------------------------------------- #
# Empty is a real answer
# --------------------------------------------------------------------------- #

async def test_an_empty_period_produces_an_empty_report_not_zeros(
    app_session_factory, tenant_a
):
    """"No activity in this period" is a legitimate finding.

    What it must not become is a set of zeros presented as measurements, or a
    chart shape drawn over nothing.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a, consents=3, withdraw=1)
        # A window that closed before any of it happened.
        long_ago = report_service.Period(
            start=datetime.now(UTC) - timedelta(days=300),
            end=datetime.now(UTC) - timedelta(days=200),
        )
        for key in report_service.REPORTS:
            out = await report_service.preview(
                s, tenant_id=tenant_a["id"], key=key, period=long_ago,
                generated_by="dpo@test",
            )
            assert out["rows"] == [], key
            assert out["total"] == 0, key
            # And the provenance still says what it is, so an empty file is
            # attributable rather than anonymous.
            assert out["provenance"].rows_in_report == 0
            assert out["provenance"].truncated is False


async def test_an_empty_csv_export_still_carries_its_provenance(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        long_ago = report_service.Period(
            start=datetime.now(UTC) - timedelta(days=300),
            end=datetime.now(UTC) - timedelta(days=200),
        )
        chunks = []
        async for c in report_service.stream_csv(
            s, tenant_id=tenant_a["id"], key="consent_register", period=long_ago,
            generated_by="dpo@test",
        ):
            chunks.append(c)
        body = "".join(chunks)
        assert "Generated by        dpo@test" in body
        assert "END OF REPORT — 0 row(s) emitted" in body
        # The header row is present even with no data, so a parser does not have
        # to special-case an empty file.
        assert "principal_ref," in body


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #

async def test_the_provenance_block_appears_before_and_after_the_rows(
    app_session_factory, tenant_a
):
    """Both blocks, for a reason.

    A header cannot say how many rows were emitted; a footer is lost if the
    transfer dies. Together, a file whose header promises a total and whose footer
    is missing is visibly incomplete.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a, consents=2, withdraw=1)
        body = "".join([
            c async for c in report_service.stream_csv(
                s, tenant_id=tenant_a["id"], key="consent_register",
                period=_period(), generated_by="dpo@test",
            )
        ])
        assert body.count("Generated at ") == 2, "header and footer"
        header_end = body.index("principal_ref,")
        assert "Generated at " in body[:header_end]
        assert "Generated at " in body[header_end:]


async def test_nothing_is_labelled_signed(app_session_factory, tenant_a):
    """The rule the brief is emphatic about.

    The chain head hash is tamper evidence somebody can recompute. Calling it a
    signature would be a claim about authorship that nothing here supports.
    """
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a, consents=1, withdraw=0)
        body = "".join([
            c async for c in report_service.stream_csv(
                s, tenant_id=tenant_a["id"], key="audit_extract",
                period=_period(), generated_by="dpo@test",
            )
        ])
        assert "NOT digitally signed" in body
        lowered = body.lower()
        # Every mention of signing must be a denial of it.
        for fragment in ("digitally signed", "signature"):
            for line in lowered.splitlines():
                if fragment in line:
                    assert ("not" in line) or ("tamper" in line), line


async def test_the_chain_is_reported_as_not_checked_unless_it_was(
    app_session_factory, tenant_a
):
    """A cheap "OK" nobody earned is worse than an honest "not checked"."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a, consents=1, withdraw=0)

        unchecked = await report_service.preview(
            s, tenant_id=tenant_a["id"], key="audit_extract", period=_period(),
            generated_by="dpo@test", verify_chain=False,
        )
        assert unchecked["provenance"].chain_verified == "not_checked"
        # But the head is still there, so a reader can check it themselves.
        assert unchecked["provenance"].chain_head_hash

        checked = await report_service.preview(
            s, tenant_id=tenant_a["id"], key="audit_extract", period=_period(),
            generated_by="dpo@test", verify_chain=True,
        )
        assert checked["provenance"].chain_verified == "ok"


async def test_the_reported_chain_head_matches_verify_chain(
    app_session_factory, tenant_a
):
    """The provenance block points at POST /v1/audit/verify, so the two must agree."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a, consents=2, withdraw=1)
        status = await audit_service.verify_chain(s, tenant_id=tenant_a["id"])
        out = await report_service.preview(
            s, tenant_id=tenant_a["id"], key="audit_extract", period=_period(),
            generated_by="dpo@test",
        )
        assert out["provenance"].chain_head_hash == status.head_hash
        assert out["provenance"].chain_head_seq == status.head_seq


async def test_truncation_is_named_in_the_provenance(
    app_session_factory, tenant_a, monkeypatch
):
    """Silent truncation reads as completeness.

    The cap is lowered rather than seeding 50,000 rows: what is under test is that
    hitting a cap is *stated*, not the specific number.
    """
    monkeypatch.setattr(report_service, "MAX_ROWS", 2)
    monkeypatch.setattr(report_service, "STREAM_CHUNK", 1)
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a, consents=5, withdraw=0)
        body = "".join([
            c async for c in report_service.stream_csv(
                s, tenant_id=tenant_a["id"], key="consent_register",
                period=_period(), generated_by="dpo@test",
            )
        ])
        assert "TRUNCATED" in body
        assert "capped at 2 rows of 5" in body
        assert "END OF REPORT — 2 row(s) emitted" in body

        reader = csv.reader(io.StringIO(
            "\n".join(l for l in body.splitlines() if not l.startswith("#"))
        ))
        rows = [r for r in reader if r]
        assert len(rows) == 3, "header plus exactly the capped rows"


# --------------------------------------------------------------------------- #
# Period caps
# --------------------------------------------------------------------------- #

def test_an_over_long_period_is_refused():
    with pytest.raises(Conflict) as exc:
        report_service.resolve_period(date(2020, 1, 1), date(2026, 1, 1))
    assert "limited to" in str(exc.value)


def test_a_backwards_period_is_refused():
    with pytest.raises(Conflict):
        report_service.resolve_period(date(2026, 6, 1), date(2026, 5, 1))


def test_the_end_of_the_period_is_inclusive():
    """A DPO asking for 1–31 March means the whole of the 31st.

    An exclusive end silently drops a day of activity from a statutory report,
    which is the kind of off-by-one that only shows up in a dispute.
    """
    period = report_service.resolve_period(date(2026, 3, 1), date(2026, 3, 31))
    assert period.end == datetime(2026, 4, 1, tzinfo=UTC)


def test_the_default_period_is_the_last_thirty_days():
    period = report_service.resolve_period(None, None)
    assert 29 <= period.days <= 30


# --------------------------------------------------------------------------- #
# Streaming, not buffering
# --------------------------------------------------------------------------- #

async def test_rows_are_fetched_in_chunks_rather_than_all_at_once(
    app_session_factory, tenant_a, monkeypatch
):
    """Assert the chunking, not just that it succeeded.

    Reports run over the biggest tables in the system, so "it worked on six rows"
    proves nothing. This counts the round trips: with a chunk size of 2 over 5
    rows there must be more than one.
    """
    monkeypatch.setattr(report_service, "STREAM_CHUNK", 2)
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a, consents=5, withdraw=0)

        calls = 0
        original = s.execute

        async def counting(*args, **kwargs):
            nonlocal calls
            calls += 1
            return await original(*args, **kwargs)

        monkeypatch.setattr(s, "execute", counting)

        rows = [
            r async for r in report_service._iter_rows(  # noqa: SLF001
                s, tenant_id=tenant_a["id"], key="consent_register", period=_period()
            )
        ]
        assert len(rows) == 5
        # 5 rows at 2 per fetch = 3 round trips (2, 2, 1).
        assert calls == 3, f"expected chunked fetches, got {calls}"


async def test_the_json_export_is_a_single_parseable_document(
    app_session_factory, tenant_a
):
    """Streamed in pieces, but still valid JSON when reassembled."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a, consents=3, withdraw=1)
        body = "".join([
            c async for c in report_service.stream_json(
                s, tenant_id=tenant_a["id"], key="consent_register",
                period=_period(), generated_by="dpo@test",
            )
        ])
        parsed = json.loads(body)
        assert parsed["meta"]["report"] == "consent_register"
        assert parsed["meta"]["provenance"]["generated_by"] == "dpo@test"
        assert len(parsed["rows"]) == 3
        assert parsed["rows_in_report"] == 3
        # A parser that reaches this knows it has the whole thing.
        assert parsed["complete"] is True
        assert "not_a_signature" in parsed["meta"]


async def test_the_json_provenance_leads_so_it_survives_a_partial_transfer(
    app_session_factory, tenant_a
):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a, consents=2, withdraw=0)
        first = None
        async for chunk in report_service.stream_json(
            s, tenant_id=tenant_a["id"], key="consent_register", period=_period(),
            generated_by="dpo@test",
        ):
            first = chunk
            break
        assert "provenance" in first
        assert "generated_by" in first


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #

async def test_a_report_in_one_tenant_never_contains_another_tenants_rows(
    app_session_factory, tenant_a, tenant_b
):
    """Seeded on both sides, asserted on both sides.

    Checking only that A's report is non-empty and B's is absent from it would
    pass if B's data had failed to seed at all.
    """
    async with app_session_factory() as s:
        await s.begin()
        await set_tenant_context(s, tenant_b["id"])
        await _world(s, tenant_b, consents=4, withdraw=2)
        await s.commit()

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a, consents=2, withdraw=1)
        for key in report_service.REPORTS:
            total = await report_service.count_rows(
                s, tenant_id=tenant_a["id"], key=key, period=_period()
            )
            rows = [
                r async for r in report_service._iter_rows(  # noqa: SLF001
                    s, tenant_id=tenant_a["id"], key=key, period=_period()
                )
            ]
            assert len(rows) == total
            emails = {r.get("principal_email") for r in rows}
            assert not any(
                e and e.startswith("p") and "example.com" in e and len(rows) > 6
                for e in emails
            ), key

        # A's consent register has exactly A's two consents, not the six across both.
        assert await report_service.count_rows(
            s, tenant_id=tenant_a["id"], key="consent_register", period=_period()
        ) == 2

    async with scoped(app_session_factory, tenant_b["id"]) as s:
        assert await report_service.count_rows(
            s, tenant_id=tenant_b["id"], key="consent_register", period=_period()
        ) == 4, "tenant B's own data is really there"


async def test_the_provenance_names_the_workspace_it_came_from(
    app_session_factory, tenant_a
):
    """Two files on a desk have to be tellable apart."""
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        out = await report_service.preview(
            s, tenant_id=tenant_a["id"], key="consent_register", period=_period(),
            generated_by="dpo@test",
        )
        assert out["provenance"].workspace_slug == tenant_a["slug"]


# --------------------------------------------------------------------------- #
# Over HTTP, including the Auditor's restricted view
# --------------------------------------------------------------------------- #

async def _sign_in(client, email: str, password: str, workspace: str) -> str:
    r = await client.post(
        "/v1/auth/login",
        json={"tenant_slug": workspace, "email": email, "password": password},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture
async def admin_headers(app_session_factory, tenant_a, client):
    token = await _sign_in(
        client, tenant_a["admin_email"], tenant_a["password"], tenant_a["slug"]
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def auditor_headers(app_session_factory, tenant_a, client):
    from app.services import tenant_service

    email = "auditor@tenant-a.example.com"
    password = "correct-horse-battery-staple-audit"
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await tenant_service.create_user(
            s, tenant_id=tenant_a["id"], email=email, full_name="An Auditor",
            role="auditor", password=password, actor=_actor(tenant_a),
        )
        await s.commit()
    token = await _sign_in(client, email, password, tenant_a["slug"])
    return {"Authorization": f"Bearer {token}"}


async def test_the_catalogue_states_the_limits_and_the_signing_position(
    client, admin_headers
):
    r = await client.get("/v1/reports", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {d["key"] for d in body["reports"]} == set(report_service.REPORTS)
    # No PDF offered, because none can be produced. A dead option is worse than
    # an absent one.
    assert body["formats"] == ["csv", "json"]
    assert "NOT digitally signed" in body["signing"]
    assert body["max_period_days"] == report_service.MAX_PERIOD_DAYS


async def test_generate_streams_a_csv_attachment(
    app_session_factory, tenant_a, client, admin_headers
):
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        await _world(s, tenant_a, consents=3, withdraw=1)
        await s.commit()

    r = await client.post(
        "/v1/reports/consent_register/generate",
        headers=admin_headers, json={"format": "csv"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=" in r.headers["content-disposition"]
    # Streamed, so no Content-Length to be wrong about.
    assert "content-length" not in {k.lower() for k in r.headers}

    body = r.text
    assert "END OF REPORT — 3 row(s) emitted" in body
    assert f"({tenant_a['slug']})" in body


async def test_generating_a_report_is_itself_audited(
    app_session_factory, tenant_a, client, admin_headers
):
    """Reports are never stored, so the audit entry is the only trace.

    "Who extracted the consent register last quarter" is a reasonable question to
    ask about a file full of personal data.
    """
    before = None
    async with scoped(app_session_factory, tenant_a["id"]) as s:
        before = await s.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.action == "report.generated"
            )
        )

    r = await client.post(
        "/v1/reports/dsar_register/generate",
        headers=admin_headers, json={"format": "json"},
    )
    assert r.status_code == 200, r.text

    async with scoped(app_session_factory, tenant_a["id"]) as s:
        rows = (await s.execute(
            select(AuditEvent).where(AuditEvent.action == "report.generated")
        )).scalars().all()
        assert len(rows) == (before or 0) + 1
        assert rows[-1].payload["report"] == "dsar_register"
        assert rows[-1].payload["format"] == "json"


async def test_an_unknown_report_is_refused_by_name(client, admin_headers):
    r = await client.get("/v1/reports/not_a_report/preview", headers=admin_headers)
    assert r.status_code == 409, r.text
    assert "consent_register" in r.text, "the error names the alternatives"


async def test_an_over_long_period_is_refused_over_http(client, admin_headers):
    r = await client.get(
        "/v1/reports/audit_extract/preview",
        headers=admin_headers,
        params={"date_from": "2020-01-01", "date_to": "2026-01-01"},
    )
    assert r.status_code == 409, r.text


async def test_an_auditor_can_generate_reports(client, auditor_headers):
    """Read-only by construction, but reporting is a read."""
    r = await client.get("/v1/reports", headers=auditor_headers)
    assert r.status_code == 200, r.text
    p = await client.get(
        "/v1/reports/consent_register/preview", headers=auditor_headers
    )
    assert p.status_code == 200, p.text
    g = await client.post(
        "/v1/reports/consent_register/generate",
        headers=auditor_headers, json={"format": "csv"},
    )
    assert g.status_code == 200, g.text


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        # A real write, with a well-formed body — so the 403 comes from the
        # capability check and not from validation refusing it first.
        #
        # Deliberately NOT /v1/consents/withdraw: that is a *self* endpoint every
        # human role can reach for their own consent, on purpose, so that an
        # auditor can exercise their own rights over their own data. Testing it
        # here would assert the opposite of the intended design.
        (
            "post",
            "/v1/purposes",
            {"key": "audit_probe", "name": "Probe", "category": "Contact Data"},
        ),
        ("get", "/v1/dsar", None),
        ("get", "/v1/admin/users", None),
        ("get", "/v1/retention/policies", None),
        ("get", "/v1/grievances", None),
        ("get", "/v1/notifications/templates", None),
    ],
)
async def test_an_auditor_is_refused_everything_that_changes_or_processes(
    client, auditor_headers, method, path, body
):
    """An auditor who could change what they audit is not an auditor.

    Enforced by the routes, not by the nav hiding them.
    """
    call = getattr(client, method)
    r = await call(path, headers=auditor_headers, **({"json": body} if body else {}))
    assert r.status_code == 403, f"{path} returned {r.status_code}: {r.text[:200]}"
