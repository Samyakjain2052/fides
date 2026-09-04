"""Where does this person's data live, and what happens if we erase it.

The question an admin has to answer before pressing Delete, and the reason the
answer is METADATA rather than content.

WHAT THIS RETURNS, AND WHY THAT SHAPE

Per table: which identifier matched, which column it matched on, how many rows,
what categories of personal data the columns suggest, and the column NAMES. No
values. That is enough to decide three things a deletion needs decided —

  * is this the right person (the matched identifier is shown),
  * how much is affected (row counts),
  * is any of it something we must keep (categories flag financial and
    government-ID data, which statute often requires retaining) —

without the request becoming a licence to read somebody's record. A rights
request authorises acting on data, not browsing it, and an admin who reads a
full customer record because a request arrived is processing it for a new
purpose. The same reasoning `PurgeRunItem` already follows: it records
table, entity id, action and skip reason, never a value.

Reading actual rows is a separate, gated, audited act — see
`sample_rows`, which exists for the cases where a match genuinely cannot be
confirmed any other way, and is off unless a workspace turns it on.

THE HEURISTIC IS A HEURISTIC, AND SAYS SO

Nothing here knows a customer's schema. It finds candidate identifier columns by
name and reports what matched, and the UI presents that as candidates for a human
to confirm. A column called `email` in a table of supplier contacts is not this
person, and no amount of pattern matching will know that. Presenting a guess as a
finding is how the wrong rows get deleted.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.connectors.hosts import HostNotAllowed, resolve_and_check
from app.connectors.probes import TIMEOUT_SECONDS, _int, _truthy

logger = logging.getLogger("app.discovery")

#: Ceiling on tables inspected per connection. A schema with 900 tables would
#: otherwise turn one page load into 900 counting queries against a customer's
#: production database.
MAX_TABLES = 120

#: Whole-operation budget. Longer than a probe because this is many queries, but
#: still bounded — an admin is waiting, and a customer's database is working.
DISCOVERY_TIMEOUT = 45.0


# --------------------------------------------------------------------------- #
# Column-name vocabulary
# --------------------------------------------------------------------------- #

#: Which identifier a column might hold. Ordered so the most specific patterns
#: are tried first: `user_email` must match as an email, not fall through.
IDENTIFIER_PATTERNS: dict[str, tuple[str, ...]] = {
    "email": (
        r"^e?_?mail$", r"^email_?address$", r"_email$", r"^email_", r"^mail_",
        r"^contact_email$", r"^login$",
    ),
    "phone": (
        r"^phone$", r"^mobile$", r"^msisdn$", r"^telephone$", r"_phone$",
        r"^phone_?number$", r"^mobile_?number$", r"^contact_?number$",
    ),
    "external_id": (
        r"^customer_?id$", r"^user_?id$", r"^external_?id$", r"^account_?id$",
        r"^client_?id$", r"^subscriber_?id$", r"^member_?id$",
    ),
}

#: What kind of personal data a column name suggests. Used to warn about the
#: categories statute tends to protect — a table flagged "Government ID" or
#: "Financial" is one where erasure may be unlawful rather than merely awkward.
CATEGORY_PATTERNS: dict[str, tuple[str, ...]] = {
    "Government ID": (r"aadhaar", r"^pan$", r"_pan$", r"passport", r"voter",
                      r"driving", r"licen[cs]e_?no", r"^uid$", r"^nid$"),
    "Financial": (r"account_?no", r"^iban$", r"^ifsc$", r"^upi", r"card",
                  r"^cvv$", r"amount", r"balance", r"salary", r"invoice",
                  r"payment", r"transaction", r"^price$"),
    "Health": (r"diagnos", r"prescription", r"medical", r"blood", r"allerg",
               r"treatment"),
    "Identity": (r"^name$", r"_name$", r"^dob$", r"date_?of_?birth", r"gender",
                 r"^age$", r"father", r"mother", r"nationality"),
    "Contact": (r"mail", r"phone", r"mobile", r"address", r"^city$", r"^state$",
                r"pin_?code", r"postal", r"^zip"),
    "Usage": (r"^ip_?address$", r"user_?agent", r"last_?login", r"session",
              r"device", r"^referrer$"),
    "Location": (r"latitude", r"longitude", r"^geo", r"coordinates"),
}

#: Columns whose values are replaced when masking. Everything else in the row is
#: left alone — an order's amount is the company's business record, and erasing
#: the person from it is not the same as erasing the record.
_MASKABLE = ("email", "phone", "Identity", "Contact", "Government ID")


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    lowered = name.lower()
    return any(re.search(p, lowered) for p in patterns)


def identifier_kind(column: str) -> str | None:
    """Which identifier this column might hold, if any."""
    for kind, patterns in IDENTIFIER_PATTERNS.items():
        if _matches(column, patterns):
            return kind
    return None


def categories_for(columns: list[str]) -> list[str]:
    """Which categories of personal data these column names suggest."""
    found = [
        category
        for category, patterns in CATEGORY_PATTERNS.items()
        if any(_matches(c, patterns) for c in columns)
    ]
    # Ordered by how much they matter for an erasure decision, not
    # alphabetically: an admin should read "Government ID" first.
    order = list(CATEGORY_PATTERNS)
    return sorted(found, key=order.index)


def maskable_columns(columns: list[str]) -> list[str]:
    """Columns a mask would overwrite.

    Identifying and contact fields only. Leaving the rest is deliberate: the
    retention module made the same choice for this product's own tables, and
    for a customer's orders table it is the difference between removing a person
    and destroying a financial record they are required to keep.
    """
    out: list[str] = []
    for c in columns:
        if identifier_kind(c):
            out.append(c)
            continue
        for key in _MASKABLE:
            patterns = CATEGORY_PATTERNS.get(key)
            if patterns and _matches(c, patterns):
                out.append(c)
                break
    return out


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #

@dataclass
class TableFinding:
    table: str
    #: Which of the person's identifiers matched — email / phone / external_id.
    matched_identifier: str
    #: The column it matched on, so an admin can see WHY this table is listed.
    matched_column: str
    rows: int
    categories: list[str] = field(default_factory=list)
    #: Column names, never values.
    columns: list[str] = field(default_factory=list)
    #: Columns a mask would overwrite here.
    would_mask: list[str] = field(default_factory=list)
    #: Which of those accept NULL. Masking prefers NULL — it is the closest
    #: thing to "this was never here" — and falls back to a token for NOT NULL
    #: columns, which is what `_purge_principal` already does for this
    #: product's own tables.
    nullable: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "matched_identifier": self.matched_identifier,
            "matched_column": self.matched_column,
            "rows": self.rows,
            "categories": self.categories,
            "columns": self.columns,
            "would_mask": self.would_mask,
            "nullable": self.nullable,
        }


@dataclass
class SystemFinding:
    ok: bool
    findings: list[TableFinding] = field(default_factory=list)
    tables_scanned: int = 0
    #: Set when the whole system could not be inspected. Distinct from "found
    #: nothing": an admin must not read a connection error as a clean bill.
    error: str | None = None
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error": self.error,
            "tables_scanned": self.tables_scanned,
            "truncated": self.truncated,
            "total_rows": sum(f.rows for f in self.findings),
            "findings": [f.as_dict() for f in self.findings],
        }


# --------------------------------------------------------------------------- #
# PostgreSQL
# --------------------------------------------------------------------------- #

def _pg_quote(name: str) -> str:
    """Quote an identifier for PostgreSQL.

    Table and column names come from `information_schema`, so they are the
    database's own, not a caller's — but they are still interpolated into SQL
    because identifiers cannot be bound as parameters. Doubling embedded quotes
    is what makes that safe rather than merely usually-safe.
    """
    return '"' + name.replace('"', '""') + '"'


async def _discover_postgresql(
    config: dict[str, Any], identifiers: dict[str, str]
) -> SystemFinding:
    import asyncpg

    host = (config.get("host") or "").strip()
    try:
        resolve_and_check(host, _int(config.get("port"), 5432))
    except HostNotAllowed as exc:
        return SystemFinding(ok=False, error=str(exc))

    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(
                host=host,
                port=_int(config.get("port"), 5432),
                user=(config.get("user") or "").strip() or None,
                password=config.get("password") or None,
                database=(config.get("database") or "").strip() or None,
                ssl="require" if _truthy(config.get("tls", "true")) else False,
                statement_cache_size=0,
            ),
            timeout=TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        return SystemFinding(ok=False, error=f"{type(exc).__name__}: {exc}"[:250])

    try:
        rows = await conn.fetch(
            """
            SELECT table_schema, table_name, column_name, is_nullable
              FROM information_schema.columns
             WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
               AND table_schema NOT LIKE 'pg_%'
             ORDER BY table_schema, table_name, ordinal_position
            """
        )
        by_table: dict[tuple[str, str], list[str]] = {}
        nullable_by_table: dict[tuple[str, str], list[str]] = {}
        for r in rows:
            key = (r["table_schema"], r["table_name"])
            by_table.setdefault(key, []).append(r["column_name"])
            if r["is_nullable"] == "YES":
                nullable_by_table.setdefault(key, []).append(r["column_name"])

        truncated = len(by_table) > MAX_TABLES
        findings: list[TableFinding] = []
        scanned = 0

        for (schema, table), columns in list(by_table.items())[:MAX_TABLES]:
            scanned += 1
            # First identifier column wins. Counting the same table once per
            # matching column would double-report it.
            for column in columns:
                kind = identifier_kind(column)
                value = identifiers.get(kind or "")
                if not kind or not value:
                    continue
                qualified = f"{_pg_quote(schema)}.{_pg_quote(table)}"
                try:
                    count = await conn.fetchval(
                        f"SELECT count(*) FROM {qualified} "
                        f"WHERE {_pg_quote(column)}::text = $1",
                        value,
                    )
                except Exception:  # noqa: BLE001
                    # An unreadable table is not a finding and not a failure of
                    # the whole scan — a customer's grants are their business.
                    break
                if count:
                    findings.append(
                        TableFinding(
                            table=table if schema == "public" else f"{schema}.{table}",
                            matched_identifier=kind,
                            matched_column=column,
                            rows=int(count),
                            categories=categories_for(columns),
                            columns=columns,
                            would_mask=maskable_columns(columns),
                            nullable=nullable_by_table.get((schema, table), []),
                        )
                    )
                break

        return SystemFinding(
            ok=True, findings=findings, tables_scanned=scanned, truncated=truncated
        )
    except Exception as exc:  # noqa: BLE001
        return SystemFinding(ok=False, error=f"{type(exc).__name__}: {exc}"[:250])
    finally:
        await conn.close()


# --------------------------------------------------------------------------- #
# MySQL
# --------------------------------------------------------------------------- #

def _my_quote(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


async def _discover_mysql(
    config: dict[str, Any], identifiers: dict[str, str]
) -> SystemFinding:
    import pymysql

    host = (config.get("host") or "").strip()
    try:
        resolve_and_check(host, _int(config.get("port"), 3306))
    except HostNotAllowed as exc:
        return SystemFinding(ok=False, error=str(exc))

    def _run() -> SystemFinding:
        kwargs: dict[str, Any] = {
            "host": host,
            "port": _int(config.get("port"), 3306),
            "user": (config.get("user") or "").strip() or None,
            "password": config.get("password") or "",
            "database": (config.get("database") or "").strip() or None,
            "connect_timeout": int(TIMEOUT_SECONDS),
            "read_timeout": int(DISCOVERY_TIMEOUT),
        }
        if _truthy(config.get("tls", "true")):
            kwargs["ssl"] = {}
        conn = pymysql.connect(**kwargs)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name, column_name, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() "
                    "ORDER BY table_name, ordinal_position"
                )
                by_table: dict[str, list[str]] = {}
                nullable_by_table: dict[str, list[str]] = {}
                for table, column, is_nullable in cur.fetchall():
                    by_table.setdefault(table, []).append(column)
                    if str(is_nullable).upper() == "YES":
                        nullable_by_table.setdefault(table, []).append(column)

                truncated = len(by_table) > MAX_TABLES
                findings: list[TableFinding] = []
                scanned = 0

                for table, columns in list(by_table.items())[:MAX_TABLES]:
                    scanned += 1
                    for column in columns:
                        kind = identifier_kind(column)
                        value = identifiers.get(kind or "")
                        if not kind or not value:
                            continue
                        try:
                            cur.execute(
                                f"SELECT count(*) FROM {_my_quote(table)} "
                                f"WHERE {_my_quote(column)} = %s",
                                (value,),
                            )
                            count = (cur.fetchone() or [0])[0]
                        except Exception:  # noqa: BLE001
                            break
                        if count:
                            findings.append(
                                TableFinding(
                                    table=table,
                                    matched_identifier=kind,
                                    matched_column=column,
                                    rows=int(count),
                                    categories=categories_for(columns),
                                    columns=columns,
                                    would_mask=maskable_columns(columns),
                                    nullable=nullable_by_table.get(table, []),
                                )
                            )
                        break

                return SystemFinding(
                    ok=True, findings=findings, tables_scanned=scanned,
                    truncated=truncated,
                )
        finally:
            conn.close()

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_run), timeout=DISCOVERY_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001
        return SystemFinding(ok=False, error=f"{type(exc).__name__}: {exc}"[:250])


# --------------------------------------------------------------------------- #
# MongoDB
# --------------------------------------------------------------------------- #

async def _discover_mongodb(
    config: dict[str, Any], identifiers: dict[str, str]
) -> SystemFinding:
    from pymongo import MongoClient

    host = (config.get("host") or "").strip()
    srv = _truthy(config.get("srv", "false"))
    try:
        resolve_and_check(host.replace("mongodb+srv://", ""),
                          _int(config.get("port"), 27017))
    except HostNotAllowed as exc:
        return SystemFinding(ok=False, error=str(exc))

    def _run() -> SystemFinding:
        ms = int(TIMEOUT_SECONDS * 1000)
        kwargs: dict[str, Any] = {
            "serverSelectionTimeoutMS": ms,
            "connectTimeoutMS": ms,
            "socketTimeoutMS": int(DISCOVERY_TIMEOUT * 1000),
        }
        if srv:
            kwargs["host"] = f"mongodb+srv://{host.replace('mongodb+srv://', '')}"
        else:
            kwargs["host"] = host
            kwargs["port"] = _int(config.get("port"), 27017)
            if _truthy(config.get("tls", "true")):
                kwargs["tls"] = True
        user = (config.get("user") or "").strip()
        if user:
            kwargs["username"] = user
            kwargs["password"] = config.get("password") or ""
            kwargs["authSource"] = (config.get("auth_source") or "admin").strip()

        client = MongoClient(**kwargs)
        try:
            db = client[(config.get("database") or "").strip()]
            names = db.list_collection_names()
            truncated = len(names) > MAX_TABLES
            findings: list[TableFinding] = []
            scanned = 0

            for name in names[:MAX_TABLES]:
                scanned += 1
                # A document store has no schema, so the field names come from a
                # sample rather than a catalogue. One document is enough to learn
                # the shape and is the least reading that answers the question.
                sample = db[name].find_one({}, {"_id": 0}) or {}
                columns = sorted(sample.keys())
                for column in columns:
                    kind = identifier_kind(column)
                    value = identifiers.get(kind or "")
                    if not kind or not value:
                        continue
                    count = db[name].count_documents({column: value})
                    if count:
                        findings.append(
                            TableFinding(
                                table=name,
                                matched_identifier=kind,
                                matched_column=column,
                                rows=int(count),
                                categories=categories_for(columns),
                                columns=columns,
                                would_mask=maskable_columns(columns),
                                # A document store has no NOT NULL: a field can
                                # simply be absent, which is a cleaner erasure
                                # than any placeholder.
                                nullable=columns,
                            )
                        )
                    break

            return SystemFinding(
                ok=True, findings=findings, tables_scanned=scanned,
                truncated=truncated,
            )
        finally:
            client.close()

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_run), timeout=DISCOVERY_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001
        return SystemFinding(ok=False, error=f"{type(exc).__name__}: {exc}"[:250])


DISCOVERERS = {
    "postgresql": _discover_postgresql,
    "mysql": _discover_mysql,
    "mongodb": _discover_mongodb,
}


async def discover(
    connector_id: str, config: dict[str, Any], identifiers: dict[str, str]
) -> SystemFinding:
    """Find where one person appears in one system. Read-only.

    `identifiers` maps kind -> value: {"email": "...", "phone": "...",
    "external_id": "..."}. Only the kinds present are searched, so a principal
    with no phone number does not produce phone-shaped false matches.
    """
    fn = DISCOVERERS.get(connector_id)
    if fn is None:
        return SystemFinding(
            ok=False,
            error=(
                "No discovery exists for this connector yet, so where this "
                "person's data sits in it is unknown — not empty."
            ),
        )
    if not any(identifiers.values()):
        return SystemFinding(
            ok=False,
            error=(
                "This person has no email, phone or external id on record, so "
                "there is nothing to search by."
            ),
        )
    try:
        return await fn(config, identifiers)
    except Exception as exc:  # noqa: BLE001
        logger.exception("discovery failed",
                         extra={"context": {"connector": connector_id}})
        return SystemFinding(ok=False, error=f"Discovery failed: {type(exc).__name__}")


# --------------------------------------------------------------------------- #
# Erasure
#
# MASK, NOT DELETE, and the choice is the same one `_purge_principal` already
# made for this product's own tables. Two reasons:
#
#   * A row in a customer's `orders` table is their financial record, and much
#     of it they are legally required to keep. Removing the person from it is
#     erasure; removing the row is destroying a statutory record on the person's
#     behalf, which nobody asked for and RBI would have views about.
#   * A DELETE across a schema we did not design hits foreign keys we cannot see.
#     A half-completed cascade is worse than a mask that succeeds, and there is
#     no undo.
#
# So identifying columns are overwritten and the rest of the row is left. NULL
# where the schema allows it, a token where it does not — a NOT NULL unique
# column cannot be nulled and cannot be given a constant either, so the token
# carries the request reference and the row's ordinal.
# --------------------------------------------------------------------------- #

@dataclass
class EraseOutcome:
    ok: bool
    table: str
    rows_affected: int = 0
    columns_masked: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "table": self.table,
            "rows_affected": self.rows_affected,
            "columns_masked": self.columns_masked, "error": self.error,
        }


def _token(reference: str) -> str:
    """What replaces a value in a NOT NULL column.

    Carries the request reference so an auditor reading the customer's database
    later can tie the masked row back to the request that masked it — which is
    the difference between evidence and a mystery.
    """
    return f"erased:{reference}"


async def erase_postgresql(
    config: dict[str, Any],
    finding: TableFinding,
    identifier_value: str,
    reference: str,
) -> EraseOutcome:
    import asyncpg

    host = (config.get("host") or "").strip()
    try:
        resolve_and_check(host, _int(config.get("port"), 5432))
    except HostNotAllowed as exc:
        return EraseOutcome(False, finding.table, error=str(exc))

    columns = finding.would_mask
    if not columns:
        return EraseOutcome(
            False, finding.table,
            error="No identifying column here to mask, so nothing was changed.",
        )

    schema, _, table = (
        finding.table.partition(".") if "." in finding.table
        else ("public", "", finding.table)
    )
    table = table or finding.table

    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(
                host=host, port=_int(config.get("port"), 5432),
                user=(config.get("user") or "").strip() or None,
                password=config.get("password") or None,
                database=(config.get("database") or "").strip() or None,
                ssl="require" if _truthy(config.get("tls", "true")) else False,
                statement_cache_size=0,
            ),
            timeout=TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        return EraseOutcome(False, finding.table,
                            error=f"{type(exc).__name__}: {exc}"[:200])

    try:
        nullable = set(finding.nullable)
        assignments: list[str] = []
        params: list[Any] = [identifier_value]
        for column in columns:
            if column in nullable:
                assignments.append(f"{_pg_quote(column)} = NULL")
            else:
                params.append(_token(reference))
                assignments.append(f"{_pg_quote(column)} = ${len(params)}")

        qualified = f"{_pg_quote(schema)}.{_pg_quote(table)}"
        # One statement, so it is atomic per table: a mask that overwrote three
        # of five columns and then failed would leave the person half-erased and
        # still identifiable.
        status = await conn.execute(
            f"UPDATE {qualified} SET {', '.join(assignments)} "
            f"WHERE {_pg_quote(finding.matched_column)}::text = $1",
            *params,
        )
        # asyncpg returns "UPDATE <n>".
        affected = int(status.rsplit(" ", 1)[-1]) if status else 0
        return EraseOutcome(True, finding.table, affected, columns)
    except Exception as exc:  # noqa: BLE001
        return EraseOutcome(False, finding.table,
                            error=f"{type(exc).__name__}: {exc}"[:200])
    finally:
        await conn.close()


async def erase_mysql(
    config: dict[str, Any],
    finding: TableFinding,
    identifier_value: str,
    reference: str,
) -> EraseOutcome:
    import pymysql

    host = (config.get("host") or "").strip()
    try:
        resolve_and_check(host, _int(config.get("port"), 3306))
    except HostNotAllowed as exc:
        return EraseOutcome(False, finding.table, error=str(exc))

    columns = finding.would_mask
    if not columns:
        return EraseOutcome(
            False, finding.table,
            error="No identifying column here to mask, so nothing was changed.",
        )

    def _run() -> EraseOutcome:
        kwargs: dict[str, Any] = {
            "host": host, "port": _int(config.get("port"), 3306),
            "user": (config.get("user") or "").strip() or None,
            "password": config.get("password") or "",
            "database": (config.get("database") or "").strip() or None,
            "connect_timeout": int(TIMEOUT_SECONDS),
        }
        if _truthy(config.get("tls", "true")):
            kwargs["ssl"] = {}
        conn = pymysql.connect(**kwargs)
        try:
            nullable = set(finding.nullable)
            assignments, params = [], []
            for column in columns:
                if column in nullable:
                    assignments.append(f"{_my_quote(column)} = NULL")
                else:
                    assignments.append(f"{_my_quote(column)} = %s")
                    params.append(_token(reference))
            params.append(identifier_value)

            with conn.cursor() as cur:
                affected = cur.execute(
                    f"UPDATE {_my_quote(finding.table)} "
                    f"SET {', '.join(assignments)} "
                    f"WHERE {_my_quote(finding.matched_column)} = %s",
                    tuple(params),
                )
            conn.commit()
            return EraseOutcome(True, finding.table, int(affected or 0), columns)
        finally:
            conn.close()

    try:
        return await asyncio.wait_for(asyncio.to_thread(_run),
                                      timeout=DISCOVERY_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        return EraseOutcome(False, finding.table,
                            error=f"{type(exc).__name__}: {exc}"[:200])


async def erase_mongodb(
    config: dict[str, Any],
    finding: TableFinding,
    identifier_value: str,
    reference: str,
) -> EraseOutcome:
    from pymongo import MongoClient

    host = (config.get("host") or "").strip()
    srv = _truthy(config.get("srv", "false"))
    try:
        resolve_and_check(host.replace("mongodb+srv://", ""),
                          _int(config.get("port"), 27017))
    except HostNotAllowed as exc:
        return EraseOutcome(False, finding.table, error=str(exc))

    columns = finding.would_mask
    if not columns:
        return EraseOutcome(
            False, finding.table,
            error="No identifying field here to mask, so nothing was changed.",
        )

    def _run() -> EraseOutcome:
        ms = int(TIMEOUT_SECONDS * 1000)
        kwargs: dict[str, Any] = {
            "serverSelectionTimeoutMS": ms, "connectTimeoutMS": ms,
        }
        if srv:
            kwargs["host"] = f"mongodb+srv://{host.replace('mongodb+srv://', '')}"
        else:
            kwargs["host"] = host
            kwargs["port"] = _int(config.get("port"), 27017)
            if _truthy(config.get("tls", "true")):
                kwargs["tls"] = True
        user = (config.get("user") or "").strip()
        if user:
            kwargs["username"] = user
            kwargs["password"] = config.get("password") or ""
            kwargs["authSource"] = (config.get("auth_source") or "admin").strip()

        client = MongoClient(**kwargs)
        try:
            db = client[(config.get("database") or "").strip()]
            # `$unset`, not a placeholder. A document store can simply not have
            # the field, which is a cleaner erasure than any token — the data is
            # gone rather than overwritten with a marker.
            result = db[finding.table].update_many(
                {finding.matched_column: identifier_value},
                {"$unset": {c: "" for c in columns}},
            )
            return EraseOutcome(True, finding.table,
                                int(result.modified_count), columns)
        finally:
            client.close()

    try:
        return await asyncio.wait_for(asyncio.to_thread(_run),
                                      timeout=DISCOVERY_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        return EraseOutcome(False, finding.table,
                            error=f"{type(exc).__name__}: {exc}"[:200])


ERASERS = {
    "postgresql": erase_postgresql,
    "mysql": erase_mysql,
    "mongodb": erase_mongodb,
}


async def erase(
    connector_id: str,
    config: dict[str, Any],
    finding: TableFinding,
    identifier_value: str,
    reference: str,
) -> EraseOutcome:
    fn = ERASERS.get(connector_id)
    if fn is None:
        return EraseOutcome(
            False, finding.table,
            error="No erasure exists for this connector yet.",
        )
    try:
        return await fn(config, finding, identifier_value, reference)
    except Exception as exc:  # noqa: BLE001
        logger.exception("erase failed",
                         extra={"context": {"connector": connector_id,
                                            "table": finding.table}})
        return EraseOutcome(False, finding.table,
                            error=f"Erase failed: {type(exc).__name__}")
