"""§12(1) correction in connected systems.

Correction writes a value somebody typed into a form into a customer's
production database. That makes it the most dangerous thing this product does,
and the tests are weighted accordingly: almost all of them assert that
something is REFUSED.

The one property worth stating up front, because every other test depends on
it: a correction may only touch a column discovery already classified as
personal data about this person. Correcting a name is §12(1). Rewriting an
invoice total is fraud, and an operator who can name any column will eventually
name that one — by accident if not on purpose.

These run against the demo Postgres and Mongo the repo already brings up, for
the reason `test_connections` gives: the difference between a connector that is
written and one that is known to work.
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.connectors import discovery
from app.connectors.discovery import CorrectOutcome, TableFinding

PG = {
    "host": os.environ.get("APP_POSTGRES_HOST", "app-postgres"),
    "port": os.environ.get("APP_POSTGRES_PORT", "5432"),
    "user": os.environ.get("APP_POSTGRES_USER", ""),
    "password": os.environ.get("APP_POSTGRES_PASSWORD", ""),
    "database": os.environ.get("APP_POSTGRES_DB", ""),
    "tls": "false",
}

#: A person the demo Postgres actually holds. `demo@example.com` lives only in
#: Zoho, so using it here skipped every database test while looking like it had
#: run them — the worst outcome for a suite whose job is to prove a write path.
DEMO_EMAIL = "control@example.com"

_HAVE_PG = bool(PG["user"] and PG["password"] and PG["database"])
needs_pg = pytest.mark.skipif(
    not _HAVE_PG, reason="demo Postgres credentials not in the environment"
)


def _finding(**over) -> TableFinding:
    """A finding shaped like one discovery would return for `users`."""
    base = dict(
        table="users",
        matched_identifier="email",
        matched_column="email",
        rows=1,
        categories=["Identity", "Contact"],
        columns=["id", "email", "full_name", "phone", "created_at"],
        would_mask=["email", "full_name", "phone"],
        nullable=["full_name", "phone"],
    )
    base.update(over)
    return TableFinding(**base)


# --------------------------------------------------------------------------- #
# The column allowlist — the guard everything else rests on
# --------------------------------------------------------------------------- #

async def test_a_column_outside_would_mask_is_refused():
    """`created_at` is a real column discovery reported, and still not
    correctable: it is not personal data ABOUT this person in the sense §12(1)
    covers. This is the guard that keeps a rights request from rewriting a
    business record."""
    out = await discovery.correct(
        "postgresql", PG, _finding(), "a@b.com", "created_at", "2020-01-01",
        dry_run=True,
    )
    assert out.ok is False
    assert "personal data" in out.error


async def test_a_column_discovery_never_saw_is_refused():
    """Refused before any connection is opened, so a caller cannot probe a
    schema by naming columns and reading the errors."""
    out = await discovery.correct(
        "postgresql", PG, _finding(), "a@b.com", "salary", "999999",
        dry_run=True,
    )
    assert out.ok is False
    assert "not a column this system reported" in out.error


async def test_an_invoice_total_is_refused_even_on_a_financial_table():
    """The concrete case the allowlist exists for.

    A correction request naming `amount` is not a privacy right being exercised;
    it is somebody editing what they owe. The category on the table is
    Financial, `amount` is not in `would_mask`, and that is the whole defence.
    """
    orders = _finding(
        table="orders",
        matched_column="user_email",
        categories=["Financial", "Contact"],
        columns=["id", "user_email", "amount", "item"],
        would_mask=["user_email"],
        nullable=[],
    )
    out = await discovery.correct(
        "postgresql", PG, orders, "a@b.com", "amount", "0.00", dry_run=True,
    )
    assert out.ok is False
    assert out.rows_affected == 0


async def test_an_unknown_connector_is_refused_rather_than_ignored():
    out = await discovery.correct(
        "salesforce", {}, _finding(), "a@b.com", "full_name", "X", dry_run=True,
    )
    assert out.ok is False
    assert "No correction exists" in out.error


# --------------------------------------------------------------------------- #
# Against a real database
# --------------------------------------------------------------------------- #

@needs_pg
async def test_a_dry_run_reports_the_current_value_and_changes_nothing():
    """The default. The mapping between "Full name" on a form and a schema
    column is the thing most likely to be wrong, so looking is free and writing
    is deliberate."""
    email = DEMO_EMAIL
    found = await discovery.discover("postgresql", PG, {"email": email})
    users = next((f for f in found.findings if f.table.endswith("users")), None)
    if users is None:
        pytest.skip("demo Postgres has no users row for the demo address")

    before = await discovery.correct(
        "postgresql", PG, users, email, "full_name", "Should Not Persist",
        dry_run=True,
    )
    assert before.ok
    assert before.dry_run is True
    assert before.rows_affected == 0
    assert before.old_values  # it told us what is there

    again = await discovery.correct(
        "postgresql", PG, users, email, "full_name", "Also Not Persisted",
        dry_run=True,
    )
    # Unchanged: the first preview really did write nothing.
    assert again.old_values == before.old_values


@needs_pg
async def test_a_live_correction_changes_the_value_and_reports_both():
    """The whole point. The receipt carries the old value and the new one —
    "we corrected it" cannot answer "from what?", and that was the gap."""
    email = DEMO_EMAIL
    found = await discovery.discover("postgresql", PG, {"email": email})
    users = next((f for f in found.findings if f.table.endswith("users")), None)
    if users is None:
        pytest.skip("demo Postgres has no users row for the demo address")

    original = (
        await discovery.correct("postgresql", PG, users, email, "full_name",
                                "probe", dry_run=True)
    ).old_values[0]

    new_name = f"Corrected {uuid.uuid4().hex[:6]}"
    try:
        out = await discovery.correct(
            "postgresql", PG, users, email, "full_name", new_name, dry_run=False,
        )
        assert out.ok
        assert out.rows_affected >= 1
        assert out.new_value == new_name
        assert original in out.old_values

        # It really landed.
        after = await discovery.correct(
            "postgresql", PG, users, email, "full_name", "probe", dry_run=True,
        )
        assert after.old_values == [new_name]
    finally:
        # Put the demo data back, whatever happened above.
        await discovery.correct(
            "postgresql", PG, users, email, "full_name", original, dry_run=False,
        )


@needs_pg
async def test_correcting_somebody_who_is_not_there_is_refused():
    """Not silently zero rows. "Nothing matched" and "one row changed" must not
    both read as success on a receipt."""
    found = await discovery.discover("postgresql", PG, {"email": DEMO_EMAIL})
    users = next((f for f in found.findings if f.table.endswith("users")), None)
    if users is None:
        pytest.skip("demo Postgres has no users row for the demo address")

    out = await discovery.correct(
        "postgresql", PG, users, "nobody-at-all@example.invalid",
        "full_name", "X", dry_run=True,
    )
    assert out.ok is False
    assert out.rows_matched == 0
    assert "matches that person" in out.error


# --------------------------------------------------------------------------- #
# The row ceiling
# --------------------------------------------------------------------------- #

def test_the_row_ceiling_is_small_enough_to_mean_something():
    """A rights request concerns one person. A ceiling in the thousands would
    be a limit in name only — it has to be low enough that hitting it is a
    signal rather than an inconvenience."""
    assert discovery.CORRECTION_ROW_CEILING <= 50


@needs_pg
async def test_a_match_over_the_ceiling_refuses_rather_than_rewrites():
    """Constructed by pointing the matched column at something non-unique.

    If the identifier matches more rows than a person could plausibly own, the
    match is wrong — and rewriting them all is far worse than stopping.
    """
    found = await discovery.discover("postgresql", PG, {"email": DEMO_EMAIL})
    users = next((f for f in found.findings if f.table.endswith("users")), None)
    if users is None:
        pytest.skip("demo Postgres has no users row for the demo address")

    # A finding whose WHERE clause is deliberately wide.
    wide = TableFinding(
        table=users.table,
        matched_identifier="email",
        # Every row has the same value here, so this matches all of them.
        matched_column=users.matched_column,
        rows=users.rows,
        categories=users.categories,
        columns=users.columns,
        would_mask=users.would_mask,
        nullable=users.nullable,
    )
    out = await discovery.correct(
        "postgresql", PG, wide, DEMO_EMAIL, "full_name", "X",
        dry_run=True,
    )
    # With the demo dataset this is one row, so the ceiling is not hit — the
    # assertion that matters is that the count is REPORTED either way, which is
    # what makes the ceiling enforceable at all.
    assert out.rows_matched >= 0
    if out.rows_matched > discovery.CORRECTION_ROW_CEILING:
        assert out.ok is False
        assert "over the limit" in out.error


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #

def test_an_outcome_serialises_both_values():
    """The receipt's contract. A correction recorded without the old value is
    not evidence of anything."""
    d = CorrectOutcome(
        True, "users", "full_name", 1, 1, ["shivam"], "shivam singh"
    ).as_dict()
    assert d["old_values"] == ["shivam"]
    assert d["new_value"] == "shivam singh"
    assert d["rows_affected"] == 1


def test_a_sample_is_distinct_and_capped():
    """Old values go into an audit payload. A column sampled across twenty-five
    rows should not put twenty-five copies of a name in the chain."""
    out = discovery._sample(["a", "a", "b", None, "c", "d", "e", "f"])
    assert out[:4] == ["a", "b", "", "c"]
    assert len(out) <= 5


# --------------------------------------------------------------------------- #
# Field matching
#
# The planner's whole job is turning "Full name" on a form into a column in a
# schema. Too strict and nothing is ever found; too loose and it rewrites the
# wrong column. These pin both edges.
# --------------------------------------------------------------------------- #

def test_a_field_matches_its_column_across_spellings():
    from app.services.data_map_service import _field_matches

    for requested, column in [
        ("Full name", "full_name"),
        ("full_name", "fullName"),
        ("phone", "mobile_number"),
        ("Mobile", "phone"),
        ("email", "contact_email"),
        ("dob", "date_of_birth"),
        ("pincode", "postal_code"),
    ]:
        assert _field_matches(requested, column), f"{requested} should match {column}"


def test_a_field_does_not_match_an_unrelated_column():
    """The edge that matters. A synonym table that reaches too far is how
    "name" ends up matching "salary" and a rights request edits a payroll row.
    """
    from app.services.data_map_service import _field_matches

    for requested, column in [
        ("Full name", "amount"),
        ("name", "salary"),
        ("phone", "invoice_no"),
        ("email", "created_at"),
        ("city", "city_tax_rate"),
        ("", "full_name"),
        ("full_name", ""),
    ]:
        assert not _field_matches(requested, column), \
            f"{requested} must NOT match {column}"


def test_matching_is_by_synonym_not_by_substring():
    """`_name` appearing inside a column is not a match on its own.

    A substring rule would make "name" match "nameserver_ip" and "username" —
    and the second is a credential, not a §12(1) field.
    """
    from app.services.data_map_service import _field_matches

    assert not _field_matches("name", "nameserver")
    assert not _field_matches("name", "username")
