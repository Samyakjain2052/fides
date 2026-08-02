"""
fastapi-gateway — a thin, opinionated DSAR API in front of Fides.

    GET  /health              gateway + Fides + app-database + Zoho CRM liveness
    POST /data/subject        write a person into db1 (postgres) AND db2 (mongo)
    GET  /data/subject/{email} where that person's data lives, across all three systems
    POST /dsar                {"email": ..., "action": "access"|"erasure"}
    GET  /dsar/{id}           status + per-collection execution log + data

The DSAR endpoints are pure wrappers: Fides does the work. Their only real jobs
are (1) hiding Fides' auth handshake, (2) mapping a friendly `action` onto the
right DSR policy key, and (3) flattening Fides' several status/log/result
endpoints into one response a human (or a regulator) can read.

`GET /data/subject/{email}` is the exception — it talks to app-postgres and
app-mongo (see db.py) AND Zoho CRM (see zoho_client.py) directly, with no Fides
in the path, because you need a raw view to check Fides' answers against.
`POST /data/subject` only writes to app-postgres/app-mongo — creating a Zoho
Contact is a manual step done directly against Zoho's own API/UI, so this
stays a two-database write.
"""

from __future__ import annotations

import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path as FilePath
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AfterValidator, BaseModel, Field

from .db import AppDatabases
from .fides_client import FidesClient, FidesError
from .zoho_client import ZohoClient, ZohoError

# --------------------------------------------------------------------------
# Config — all from the environment, nothing hardcoded.
# --------------------------------------------------------------------------
FIDES_URL = os.environ.get("FIDES_URL", "http://fides:8080")
FIDES_ROOT_USERNAME = os.environ.get("FIDES_ROOT_USERNAME", "root_user")
FIDES_ROOT_PASSWORD = os.environ.get("FIDES_ROOT_PASSWORD", "Testpassword1!")

# These must match the keys in fides-config/policies/dsr_policies.yml.
POLICY_KEYS = {
    "access": os.environ.get("ACCESS_POLICY_KEY", "demo_access_policy"),
    "erasure": os.environ.get("ERASURE_POLICY_KEY", "demo_erasure_policy"),
}

# Same secrets provision.py uses to configure Fides' own Zoho CRM connection
# (see fides-config/provision/provision.py's refresh_zoho_access_token). Used
# here so GET /data/subject/{email} can check Zoho directly, the same way it
# already checks app-postgres/app-mongo directly, with no Fides in the path.
ZOHO_CRM_ACCOUNTS_DOMAIN = os.environ.get("ZOHO_CRM_ACCOUNTS_DOMAIN")
ZOHO_CRM_DOMAIN = os.environ.get("ZOHO_CRM_DOMAIN")
ZOHO_CRM_CLIENT_ID = os.environ.get("ZOHO_CRM_CLIENT_ID")
ZOHO_CRM_CLIENT_SECRET = os.environ.get("ZOHO_CRM_CLIENT_SECRET")
ZOHO_CRM_REFRESH_TOKEN = os.environ.get("ZOHO_CRM_REFRESH_TOKEN")

# Where Fides' `local` storage destination drops access packages, bind-mounted
# read-only into this container (same host directory as ./fides_uploads). The
# provisioner configures that destination with `details.naming = request_id`, so
# each package is exactly `<request_id>.json`.
ACCESS_PACKAGE_DIR = FilePath(os.environ.get("ACCESS_PACKAGE_DIR", "/access-packages"))

# The Fides Admin UI as a BROWSER can reach it. Inside the Docker network Fides is
# `http://fides:8080`, which means nothing on the host, so the console links to the
# published host port instead. docker-compose.yml sets this from FIDES_PORT.
FIDES_ADMIN_URL = os.environ.get("FIDES_ADMIN_URL", "http://localhost:8080")

# Served at /ui by StaticFiles below. Baked into the image by the Dockerfile's
# `COPY app ./app`.
STATIC_DIR = FilePath(__file__).parent / "static"

logging.basicConfig(
    level=os.environ.get("GATEWAY_LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("gateway")


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_identity(value: str) -> str:
    """A permissive email-*shape* check (local@domain.tld) — deliberately
    looser than pydantic's `EmailStr`. This gateway only ever uses this value
    as a lookup/match key (against Postgres/Mongo columns and Zoho CRM's
    `email` field) and never sends mail with it, but `EmailStr`'s underlying
    `email_validator` library unconditionally rejects RFC 2606 reserved TLDs
    (`.invalid`, `.test`, `.localhost`, etc. — see `SPECIAL_USE_DOMAIN_NAMES`
    in email_validator/__init__.py; there is no keyword argument that allows
    them). Those are exactly the kind of deliberately-synthetic addresses a
    privacy demo (or a real system!) is likely to hold — e.g. a Zoho contact
    seeded with `*@noemail.invalid`. Rejecting the identity at the gateway
    would hide the very data a subject access request is supposed to find.
    """
    value = value.strip()
    if not _EMAIL_SHAPE.match(value):
        raise ValueError("must look like an email address (local@domain.tld)")
    return value


Identity = Annotated[str, AfterValidator(_validate_identity)]


class DSARAction(str, Enum):
    """Maps 1:1 onto a Fides DSR policy."""

    access = "access"
    erasure = "erasure"


class DSARRequest(BaseModel):
    email: Identity = Field(
        ...,
        description="The data subject's email. This is the identity Fides uses "
        "to locate the person — it matches every field marked "
        "`identity: email` in the datasets, in BOTH databases.",
        examples=["demo@example.com"],
    )
    action: DSARAction = Field(
        ...,
        description="`access` returns the person's data; `erasure` masks it.",
        examples=["access"],
    )


class DSARCreated(BaseModel):
    request_id: str = Field(..., description="Fides privacy request id. Poll GET /dsar/{request_id}.")
    action: DSARAction
    policy_key: str = Field(..., description="The Fides DSR policy that will execute.")
    email: Identity
    status: str | None = Field(None, description="Fides status at creation time, usually 'pending'.")
    created_at: str | None = None


class CollectionResult(BaseModel):
    """One normalised execution-log entry: what happened to one collection."""

    dataset: str | None = None
    collection: str | None = None
    action_type: str | None = None
    status: str | None = None
    message: str | None = None
    fields_affected: list[str] = Field(
        default_factory=list,
        description="Field paths Fides actually touched. For an erasure this is "
        "the proof of which columns were masked.",
    )
    updated_at: str | None = None


class OrderIn(BaseModel):
    """One row for app-postgres `orders`."""

    amount: Decimal = Field(
        ...,
        gt=0,
        max_digits=10,
        decimal_places=2,
        description="Matches NUMERIC(10,2). Categorised "
        "`user.behavior.purchase_history`, so an erasure leaves it intact.",
        examples=["49.99"],
    )
    item: str = Field(..., min_length=1, max_length=255, examples=["Noise-cancelling headphones"])


class EventIn(BaseModel):
    """One document for app-mongo `events`.

    Only fields declared in app_mongo_dataset.yml are accepted — anything else
    would be invisible to Fides and would survive an erasure, which is exactly
    the silent gap this demo is meant to disprove.
    """

    event_type: str = Field(..., min_length=1, examples=["login"])
    ip_address: str | None = Field(
        None, description="-> metadata.ip_address (user.device.ip_address)", examples=["203.0.113.42"]
    )
    user_agent: str | None = Field(
        None, description="-> metadata.user_agent (user.device)", examples=["Mozilla/5.0"]
    )
    session_id: str | None = Field(
        None,
        description="-> metadata.session_id (user.unique_id.pseudonymous)",
        examples=["sess_8fa31c"],
    )


class SupportTicketIn(BaseModel):
    """One MySQL `support_tickets` row."""

    subject: str = Field(..., min_length=1, max_length=255, examples=["Question about my order"])


class SubjectIn(BaseModel):
    """A person to write across both application databases."""

    email: Identity = Field(
        ...,
        description="The identity that ties everything together. Used as "
        "`users.email`, `orders.user_email` and `events.email` — which is what "
        "later lets one DSAR find all of it.",
        examples=["newperson@example.com"],
    )
    full_name: str | None = Field(None, max_length=255, examples=["New Person"])
    phone: str | None = Field(None, max_length=64, examples=["+1-555-0142"])
    orders: list[OrderIn] = Field(
        default_factory=list, description="Appended to app-postgres. May be empty."
    )
    events: list[EventIn] = Field(
        default_factory=list, description="Appended to app-mongo. May be empty."
    )
    support_tickets: list[SupportTicketIn] = Field(
        default_factory=list, description="Appended to app-mysql. May be empty."
    )

    model_config = {
        # Pre-fills Swagger's "Try it out" with a complete cross-database payload.
        "json_schema_extra": {
            "example": {
                "email": "newperson@example.com",
                "full_name": "New Person",
                "phone": "+1-555-0142",
                "orders": [
                    {"amount": "24.00", "item": "Mechanical keyboard"},
                    {"amount": "9.99", "item": "Mouse pad"},
                ],
                "events": [
                    {
                        "event_type": "login",
                        "ip_address": "203.0.113.99",
                        "user_agent": "Mozilla/5.0 (X11; Linux x86_64)",
                        "session_id": "sess_newp01",
                    },
                    {
                        "event_type": "checkout",
                        "ip_address": "203.0.113.99",
                        "user_agent": "Mozilla/5.0 (X11; Linux x86_64)",
                        "session_id": "sess_newp01",
                    },
                ],
                "support_tickets": [
                    {"subject": "Question about my order"},
                    {"subject": "Delivery status request"},
                ],
            }
        }
    }


class SubjectCreated(BaseModel):
    email: Identity
    written_to: list[str] = Field(
        ...,
        description="Which of the two databases this call actually wrote to.",
        examples=[["db1 app-postgres: users, orders", "db2 app-mongo: events"]],
    )
    db1_app_postgres: dict[str, Any] = Field(
        ..., description="What landed in db1 (PostgreSQL): the users row and order ids."
    )
    db2_app_mongo: dict[str, Any] = Field(
        ..., description="What landed in db2 (MongoDB): the events documents."
    )
    db3_app_mysql: dict[str, Any] = Field(
        ..., description="What landed in db3 (MySQL): the support-ticket rows."
    )
    next: str = Field(..., description="Suggested follow-up call.")


class CollectionData(BaseModel):
    """One table/collection's worth of a subject's data."""

    count: int = Field(..., description="Records matching this email.")
    rows: list[dict[str, Any]] = Field(
        default_factory=list,
        description="The records themselves. Empty when include_rows=false.",
    )


class DatabaseData(BaseModel):
    """Where one database sits, and what it holds for this subject."""

    label: str = Field(..., examples=["db1 — app-postgres (PostgreSQL)"])
    host: str = Field(..., description="Address as the gateway and Fides reach it.")
    database: str
    fides_dataset: str = Field(
        ..., description="The Fides dataset that maps this database."
    )
    total: int = Field(..., description="Records for this subject in this database.")
    collections: dict[str, CollectionData]


class SubjectLocation(BaseModel):
    """Answer to 'where is my data?', straight from the source systems."""

    email: Identity
    found: bool = Field(..., description="True if any record matches this email.")
    total_records: int
    found_in: list[str] = Field(
        ...,
        description="Human summary — which system and which collections hold "
        "records for this subject.",
        examples=[["db1 app-postgres: users(1), orders(3)", "db2 app-mongo: events(4)"]],
    )
    db1_app_postgres: DatabaseData
    db2_app_mongo: DatabaseData
    db3_zoho_crm: DatabaseData
    db3_app_mysql: DatabaseData
    masked_rows_remaining: dict[str, int] = Field(
        ...,
        description="Rows whose identifying email is NULL, i.e. erased by some "
        "earlier DSAR. They cannot be matched to any email — which is the point "
        "— so they are reported as a count only. Zoho CRM has no equivalent: an "
        "erasure there deletes the contact rather than masking a field.",
    )
    note: str | None = Field(
        None, description="Set when nothing was found, to explain what that means."
    )


class DSARStatus(BaseModel):
    request_id: str
    status: str | None = Field(
        None,
        description="pending | in_processing | complete | error | canceled | "
        "requires_input | paused | approved | denied | identity_unverified",
    )
    policy_key: str | None = None
    action: str | None = Field(None, description="Derived from the policy's rules.")
    created_at: str | None = None
    finished_processing_at: str | None = None
    execution_log: list[CollectionResult] = Field(
        default_factory=list,
        description="Per-collection audit trail across both databases. THIS is "
        "the artifact a regulator would ask for.",
    )
    collections_touched: list[str] = Field(
        default_factory=list,
        description="Convenience roll-up: every dataset:collection that reported "
        "a completed step.",
    )
    data: dict[str, Any] | None = Field(
        None,
        description="ACCESS only: the retrieved rows, keyed by "
        "`dataset:collection` — one key per collection per database. `{}` once "
        "the subject has been erased. Absent for erasure requests.",
    )
    access_package: dict[str, Any] | None = Field(
        None,
        description="ACCESS only: where Fides wrote the full package (the "
        "`local` storage destination, surfaced on the host at ./fides_uploads).",
    )
    execution_log_raw: list[dict[str, Any]] | None = Field(
        None, description="Unmodified Fides log entries. Set include_raw_log=true."
    )


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.fides = FidesClient(
        base_url=FIDES_URL,
        username=FIDES_ROOT_USERNAME,
        password=FIDES_ROOT_PASSWORD,
    )
    app.state.db = AppDatabases()
    app.state.zoho = ZohoClient(
        accounts_domain=ZOHO_CRM_ACCOUNTS_DOMAIN,
        api_domain=ZOHO_CRM_DOMAIN,
        client_id=ZOHO_CRM_CLIENT_ID,
        client_secret=ZOHO_CRM_CLIENT_SECRET,
        refresh_token=ZOHO_CRM_REFRESH_TOKEN,
    )
    logger.info("gateway up; Fides at %s; policies=%s", FIDES_URL, POLICY_KEYS)
    try:
        yield
    finally:
        await app.state.fides.aclose()
        await app.state.db.aclose()
        await app.state.zoho.aclose()


app = FastAPI(
    title="DSAR Gateway",
    version="1.0.0",
    description=(
        "Trigger and inspect Data Subject Access Requests. Fides fans each "
        "request out across **app-postgres** (users, orders) and **app-mongo** "
        "(events) using the subject's email as the identity.\n\n"
        "Try it: `POST /dsar` with `demo@example.com` + `access`, then "
        "`GET /dsar/{id}`."
    ),
    lifespan=lifespan,
    # Swagger UI is served here by default; spelled out because the README
    # points at it.
    docs_url="/docs",
    redoc_url="/redoc",
)


def _fides() -> FidesClient:
    return app.state.fides


def _db() -> AppDatabases:
    return app.state.db


def _zoho() -> ZohoClient:
    return app.state.zoho


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """Land on the console; Swagger stays at /docs."""
    return RedirectResponse(url="/ui/")


# ------------------------------------------------------------------ health --
@app.get(
    "/health",
    summary="Gateway + Fides liveness",
    tags=["health"],
)
async def health() -> dict:
    """Returns 200 only if Fides is also reachable — a green check here means
    the whole chain is up, not just this container. The two application
    databases and Zoho CRM are reported alongside but do not fail the check:
    the DSAR endpoints work without the gateway itself being able to reach
    them (Fides connects to them independently)."""
    try:
        fides_health = await _fides().health()
    except FidesError as exc:
        raise HTTPException(
            status_code=503,
            detail={"gateway": "ok", "fides": "unreachable", "error": str(exc)},
        )

    return {
        "gateway": "ok",
        "fides": fides_health,
        "fides_url": FIDES_URL,
        "policies": POLICY_KEYS,
        "app_databases": await _db().ping(),
        "zoho_crm": await _zoho().ping(),
        # Read by the console to link out to the Fides Admin UI.
        "fides_admin_url": FIDES_ADMIN_URL,
    }


# -------------------------------------------------------------------- data --
@app.post(
    "/data/subject",
    response_model=SubjectCreated,
    status_code=201,
    summary="Add a person's data to PostgreSQL, MongoDB and MySQL",
    tags=["data"],
)
async def create_subject(body: SubjectIn) -> SubjectCreated:
    """Writes one person across BOTH application databases in a single call, so
    you can create a subject and immediately DSAR them.

    One POST, two databases — the fan-out mirrors the datasets:

    | Body field   | Database                    | Rows                        |
    | ------------ | --------------------------- | --------------------------- |
    | `email`, `full_name`, `phone` | **db1** app-postgres `users`  | 1, upserted on email |
    | `orders[]`   | **db1** app-postgres `orders` | one per entry, appended    |
    | `events[]`   | **db2** app-mongo `events`    | one per entry, appended    |

    Send both `orders` and `events` to populate both databases at once; send only
    one of them to write to only that database. `written_to` in the response says
    which databases the call actually touched.

    `users` is **upserted**: a second call with the same email updates the name
    and phone instead of creating a duplicate person. `orders` and `events` always
    append, so repeated calls grow the person's history. `created_at` /
    `timestamp` are set by the databases to now.

    This endpoint bypasses Fides entirely — Fides is a privacy engine, it never
    writes application data. It uses the same credentials as the Fides
    ConnectionConfigs, so anything inserted here is reachable by a DSAR.
    """
    db = _db()
    email = str(body.email)
    logger.info(
        "seeding subject %s (%d orders, %d events, %d support tickets)",
        email, len(body.orders), len(body.events), len(body.support_tickets),
    )

    try:
        user_id, user_action = await db.upsert_user(email, body.full_name, body.phone)
        order_ids = await db.insert_orders(
            email, [(o.amount, o.item) for o in body.orders]
        )
        event_count = await db.insert_events(
            email,
            [
                {
                    "email": email,
                    "event_type": e.event_type,
                    # Nested exactly as app_mongo_dataset.yml declares it; the
                    # erasure rule masks these with dotted-path $set operations.
                    "metadata": {
                        "ip_address": e.ip_address,
                        "user_agent": e.user_agent,
                        "session_id": e.session_id,
                    },
                    "timestamp": _utcnow(),
                }
                for e in body.events
            ],
        )
        ticket_ids = await db.insert_support_tickets(
            email, body.full_name, body.phone, [ticket.subject for ticket in body.support_tickets]
        )
    except Exception as exc:  # noqa: BLE001 - surface the real cause to the caller
        logger.exception("failed to seed subject %s", email)
        raise HTTPException(
            status_code=502,
            detail=f"could not write to the application databases: "
            f"{type(exc).__name__}: {exc}",
        )

    # NOT transactional across engines: Postgres and Mongo are independent, so a
    # Mongo failure after a successful Postgres write leaves the person in one
    # database only. Fine for a demo — and re-POSTing is safe, since the user is
    # upserted (orders/events would duplicate).
    written_to = [
        f"db1 app-postgres: users({user_action})"
        + (f", orders(+{len(order_ids)})" if order_ids else "")
    ]
    if event_count:
        written_to.append(f"db2 app-mongo: events(+{event_count})")
    if ticket_ids:
        written_to.append(f"db3 app-mysql: support_tickets(+{len(ticket_ids)})")

    return SubjectCreated(
        email=body.email,
        written_to=written_to,
        db1_app_postgres={
            "host": db.pg_host,
            "database": db.pg_database,
            "users": {"id": user_id, "action": user_action},
            "orders": {"inserted": len(order_ids), "ids": order_ids},
        },
        db2_app_mongo={
            "host": db.mongo_host,
            "database": db.mongo_database,
            "events": {"inserted": event_count},
        },
        db3_app_mysql={
            "host": db.mysql_host,
            "database": db.mysql_database,
            "support_tickets": {"inserted": len(ticket_ids), "ids": ticket_ids},
        },
        next=f'GET /data/subject/{email}  then  POST /dsar {{"email": "{email}", "action": "access"}}',
    )


@app.get(
    "/data/subject/{email}",
    response_model=SubjectLocation,
    summary="Where is this person's data? (all three systems, no Fides involved)",
    summary="Where is this person's data? (all three databases, no Fides involved)",
    tags=["data"],
)
async def locate_subject(
    email: Identity = Path(
        ...,
        description="The data subject's email.",
        examples=["demo@example.com"],
    ),
    include_rows: bool = Query(
        True, description="Set false for counts only — useful for large subjects."
    ),
) -> SubjectLocation:
    """Reads app-postgres, app-mongo AND Zoho CRM directly and reports where
    the person's records live, collection by collection.

    This is the **raw** view: it queries all three systems itself, with no
    Fides in the path. Use it to check Fides' work — the collections listed
    here are exactly the ones an access DSAR should return, and after an
    erasure all three should agree that nothing matches the email any more.

    A Zoho CRM lookup failure (not configured, network error, expired
    credentials) does not fail the whole call — it is reported as its own
    `db3_zoho_crm` entry with `total: 0` and the reason folded into `note`, so
    a Zoho outage never hides what app-postgres/app-mongo found.

    `masked_rows_remaining` counts rows whose identifying email is `NULL`. Those
    are the leftovers of an earlier erasure: the row survives with its PII nulled
    (`masking_strict = true`), but it can no longer be attributed to anyone, so it
    can only be counted, never looked up. A lookup that finds nothing while this
    count is non-zero is the signature of a completed erasure — as opposed to a
    subject who was never here at all.
    """
    db = _db()
    zoho = _zoho()

    try:
        pg = await db.find_in_postgres(email)
        mongo = await db.find_in_mongo(email)
        mysql = await db.find_in_mysql(email)
        masked = await db.count_masked(email)
    except Exception as exc:  # noqa: BLE001 - surface the real cause
        logger.exception("failed to look up subject %s", email)
        raise HTTPException(
            status_code=502,
            detail=f"could not read the application databases: "
            f"{type(exc).__name__}: {exc}",
        )

    zoho_error: str | None = None
    try:
        contacts = await zoho.find_contacts_by_email(email)
    except ZohoError as exc:
        logger.warning("Zoho CRM lookup failed for %s: %s", email, exc)
        contacts = []
        zoho_error = str(exc)

    def wrap(rows_by_collection: dict[str, list[dict[str, Any]]]) -> dict[str, CollectionData]:
        return {
            name: CollectionData(
                count=len(rows), rows=rows if include_rows else []
            )
            for name, rows in rows_by_collection.items()
        }

    db1 = DatabaseData(
        label="db1 — app-postgres (PostgreSQL)",
        host=db.pg_host,
        database=db.pg_database,
        fides_dataset="app_postgres_dataset",
        total=sum(len(r) for r in pg.values()),
        collections=wrap(pg),
    )
    db2 = DatabaseData(
        label="db2 — app-mongo (MongoDB)",
        host=db.mongo_host,
        # Reminder: the Mongo database is named after the dataset's fides_key,
        # because that is the name Fides' mongodb connector queries.
        database=db.mongo_database,
        fides_dataset="app_mongo_dataset",
        total=sum(len(r) for r in mongo.values()),
        collections=wrap(mongo),
    )
    db3 = DatabaseData(
        label="db3 — Zoho CRM (SaaS)",
        host=zoho.api_domain or "not configured",
        database="Contacts module",
        fides_dataset="zoho_crm_instance",
        total=len(contacts),
        collections=wrap({"contacts": contacts}),
        label="db3 - app-mysql (MySQL)",
        host=db.mysql_host,
        database=db.mysql_database,
        fides_dataset="app_mysql_dataset",
        total=sum(len(r) for r in mysql.values()),
        collections=wrap(mysql),
    )

    found_in = [
        f"{label}: " + ", ".join(f"{n}({len(r)})" for n, r in rows.items() if r)
        for label, rows in (
            ("db1 app-postgres", pg),
            ("db2 app-mongo", mongo),
            ("db3 Zoho CRM", {"contacts": contacts}),
            ("db3 app-mysql", mysql),
        )
        if any(rows.values())
    ]
    total = db1.total + db2.total + db3.total

    note = None
    if total == 0:
        note = (
            f"No record in any system matches {email}. "
            + (
                f"There are {sum(masked.values())} masked row(s) with a NULL "
                "identifier, so this subject may have been erased by a previous "
                "DSAR — an erasure nulls the email, which is exactly why a "
                "lookup by email can no longer find them."
                if any(masked.values())
                else "There are no masked rows either, so this subject was never "
                "in the system."
            )
        )
    if zoho_error:
        note = (note + " " if note else "") + f"Zoho CRM lookup skipped: {zoho_error}"

    return SubjectLocation(
        email=email,
        found=total > 0,
        total_records=total,
        found_in=found_in,
        db1_app_postgres=db1,
        db2_app_mongo=db2,
        db3_zoho_crm=db3,
        db3_app_mysql=db3,
        masked_rows_remaining=masked,
        note=note,
    )


# -------------------------------------------------------------------- dsar --
@app.post(
    "/dsar",
    response_model=DSARCreated,
    status_code=202,
    summary="Create a privacy request (access or erasure)",
    tags=["dsar"],
)
async def create_dsar(body: DSARRequest) -> DSARCreated:
    """Creates a Fides privacy request and returns its id immediately.

    Execution is asynchronous — Fides queues the request to its worker, which
    walks the dataset graph across both databases. Poll `GET /dsar/{request_id}`
    for the outcome.

    Verification and manual approval are both disabled in this demo
    (`fides-config/fides.toml` -> `[execution]`), so the request starts running
    the moment it is created.
    """
    policy_key = POLICY_KEYS[body.action.value]
    logger.info("creating %s DSAR for %s (policy=%s)", body.action.value, body.email, policy_key)

    try:
        created = await _fides().create_privacy_request(str(body.email), policy_key)
    except FidesError as exc:
        # 400s are usually a bad/unknown policy_key; anything else is upstream.
        status = 400 if 400 <= exc.status_code < 500 else 502
        raise HTTPException(status_code=status, detail=str(exc))

    request_id = created.get("id")
    if not request_id:
        raise HTTPException(status_code=502, detail=f"Fides returned no request id: {created}")

    logger.info("created privacy request %s", request_id)
    return DSARCreated(
        request_id=request_id,
        action=body.action,
        policy_key=policy_key,
        email=body.email,
        status=created.get("status"),
        created_at=_iso(created.get("created_at")),
    )


@app.get(
    "/dsar/{request_id}",
    response_model=DSARStatus,
    summary="Status, execution log, and results for a privacy request",
    tags=["dsar"],
)
async def get_dsar(
    request_id: str = Path(..., description="Id returned by POST /dsar."),
    include_raw_log: bool = Query(
        False, description="Also return Fides' unmodified execution-log entries."
    ),
) -> DSARStatus:
    """The proof endpoint.

    Combines:
      * `POST /privacy-request/search`              -> status + policy
      * `GET  /privacy-request/{id}/log`            -> per-collection audit trail
      * `GET  /privacy-request/{id}/access-results` -> package location
      * the access package itself, read off disk    -> the data (access only)

    On a completed **access** request you should see entries for
    `app_postgres_dataset:users`, `app_postgres_dataset:orders` and
    `app_mongo_dataset:events`. On a completed **erasure**, the same three
    collections with `action_type: erasure` and the masked field paths listed
    under `fields_affected`.
    """
    fides = _fides()

    try:
        request = await fides.get_privacy_request(request_id)
    except FidesError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    if request is None:
        raise HTTPException(status_code=404, detail=f"No privacy request with id '{request_id}'")

    policy = request.get("policy") or {}
    policy_key = policy.get("key")
    action = _action_from_policy(policy)
    status = request.get("status")

    try:
        raw_logs = await fides.get_execution_logs(request_id)
    except FidesError as exc:
        logger.warning("could not read execution log for %s: %s", request_id, exc)
        raw_logs = []

    entries = [_normalise_log(e) for e in raw_logs]

    result = DSARStatus(
        request_id=request_id,
        status=status,
        policy_key=policy_key,
        action=action,
        created_at=_iso(request.get("created_at")),
        finished_processing_at=_iso(request.get("finished_processing_at")),
        execution_log=entries,
        collections_touched=sorted(
            {
                f"{e.dataset}:{e.collection}"
                for e in entries
                if e.collection and (e.status or "").lower() == "complete"
            }
        ),
        execution_log_raw=raw_logs if include_raw_log else None,
    )

    # Only an access rule produces data — an erasure writes no package, so skip
    # this entirely for erasure requests.
    if action == "access":
        package_path = ACCESS_PACKAGE_DIR / f"{request_id}.json"
        result.data = _read_access_package(package_path)
        result.access_package = {
            "local_path_in_container": str(package_path),
            "host_path": f"./fides_uploads/{request_id}.json",
            "found": result.data is not None,
        }
        try:
            result.access_package["fides"] = await fides.get_access_results(request_id)
        except FidesError as exc:
            logger.warning("access-results unavailable for %s: %s", request_id, exc)

    return result


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _action_from_policy(policy: dict) -> str | None:
    """Fides tracks the action on the policy's rules, not on the request.

    A policy could in principle carry both an access and an erasure rule; ours
    are deliberately split one-per-policy, so the first rule is definitive.
    """
    for rule in policy.get("rules") or []:
        action_type = rule.get("action_type")
        if action_type in ("access", "erasure"):
            return action_type
    # Fall back to the naming convention in dsr_policies.yml.
    key = policy.get("key") or ""
    for candidate in ("access", "erasure"):
        if candidate in key:
            return candidate
    return None


def _utcnow() -> datetime:
    """Timezone-aware now. Mongo stores it as a BSON date, matching the seed."""
    return datetime.now(timezone.utc)


def _read_access_package(path: FilePath) -> dict[str, Any] | None:
    """Read the access package Fides wrote for this request.

    Fides has no API that hands back the rows for a real (non-test) privacy
    request — `filtered-results` is test-only and `access-results` returns just
    the storage location. With the demo's `local` storage destination the
    package is a JSON file keyed by `dataset:collection`, so the gateway reads it
    directly. Swap this for an S3 GET against `access_result_urls` when you move
    to a real storage destination.

    Returns None while the request is still running (no file yet).
    """
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            package = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning("could not read access package %s: %s", path, exc)
        return None
    # Fides writes an object keyed by `dataset:collection`; anything else is
    # unexpected, so surface it rather than pretending it is the data.
    if not isinstance(package, dict):
        return {"unexpected_package_shape": package}
    return package


def _normalise_log(entry: dict) -> CollectionResult:
    """Flatten one Fides ExecutionLog record.

    Field names have moved around across Fides versions (`collection_name` vs
    `collection`), so accept either.
    """
    fields = []
    for field in entry.get("fields_affected") or []:
        if isinstance(field, dict):
            fields.append(field.get("path") or field.get("field_name") or str(field))
        else:
            fields.append(str(field))

    return CollectionResult(
        dataset=entry.get("dataset_name") or entry.get("dataset"),
        collection=entry.get("collection_name") or entry.get("collection"),
        action_type=entry.get("action_type"),
        status=entry.get("status"),
        message=entry.get("message"),
        fields_affected=fields,
        updated_at=_iso(entry.get("updated_at")),
    )


def _iso(value: Any) -> str | None:
    """Timestamps arrive as ISO strings over JSON; normalise anything else."""
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


# --------------------------------------------------------------------------
# The console — a dependency-free static page served by this same app, so it
# shares the API's origin and needs no CORS configuration.
#   http://localhost:8000/ui/
# Mounted last: a mount swallows every path beneath it, so it must not shadow
# the API routes above.
# --------------------------------------------------------------------------
app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
