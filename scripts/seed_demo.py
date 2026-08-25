#!/usr/bin/env python3
# =============================================================================
# Fill a workspace with a realistic data set, so the product can be looked at.
#
#     ./scripts/seed_demo.py --register "Meridian Financial"
#     ./scripts/seed_demo.py --workspace sam --email you@x.com --password '…'
#
# A freshly registered workspace holds four purposes and nothing else. That is
# the correct state for a real signup and a useless state for a demo: every
# screen shows zero, so nothing can be evaluated and every workspace looks
# identical to every other one.
#
# Everything below is created through the same HTTP API a browser uses. No
# INSERTs, no fixtures, no direct writes. If the API cannot produce a piece of
# state, this script does not contain that state — which is the point, because a
# seeder that reaches into the database can show you a product that does not
# exist.
#
# THE ONE EXCEPTION, stated plainly
# ---------------------------------
# `submitted_at` and its deadlines are stamped by the server from the clock.
# Filed-today is therefore the only thing the API can create, and a data set
# where nothing is old cannot show an overdue grievance, an escalation, or a
# deadline about to pass — most of what this product is *for*.
#
# So the final pass shifts timestamps backwards with UPDATE, and only
# timestamps: no row is created this way and no value is invented. It moves
# events that really happened to dates on which they plausibly happened.
#
# The audit trail is deliberately NOT shifted. Its entries are HMAC-chained and
# rewriting them is exactly the tampering the chain exists to detect. The
# consequence is worth knowing before you demo it: the audit log will honestly
# say every one of these events was recorded during the seed run, while the
# business rows say the complaint was filed three weeks ago. Skip the pass with
# --no-backdate if that discrepancy matters more to you than the overdue states.
#
# Requires: python3 (stdlib only), and docker for the backdate pass.
# =============================================================================
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_BASE = "http://localhost:8100"
DEMO_PASSWORD = "DemoPeople!2026"   # the seeded data_principal logins

# Deterministic, so re-running names the same people rather than accumulating a
# crowd of strangers. Seeded from a constant, never from the clock.
RNG = random.Random(20260820)


def bold(s: str) -> None:
    print(f"\033[1m{s}\033[0m")


def ok(s: str) -> None:
    print(f"  \033[32m✓\033[0m {s}")


def warn(s: str) -> None:
    print(f"  \033[33m!\033[0m {s}")


def die(s: str) -> None:
    print(f"\033[31m✗ {s}\033[0m", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Api:
    """Thin JSON client. Raises on unexpected status so a silent partial seed
    cannot be mistaken for a complete one."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.token: str | None = None

    def call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        token: str | None = ...,          # type: ignore[assignment]
        expect: tuple[int, ...] = (200, 201),
        allow: tuple[int, ...] = (),
        quiet: tuple[int, ...] = (409,),
    ) -> tuple[int, dict | list | None]:
        tok = self.token if token is ... else token
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
        req.add_header("content-type", "application/json")
        if tok:
            req.add_header("authorization", f"Bearer {tok}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {"detail": raw.decode(errors="replace")[:400]}
            if e.code in allow or e.code in expect:
                # Never silently. On a create, a 409 means "already there" and is
                # genuinely uninteresting — hence the default. On a *state
                # transition* it means the API refused the move, which is a
                # failure wearing the same status code, so those call sites pass
                # `quiet=()`. Getting this wrong once already cost a silent
                # "0 rights requests", and again a resolved complaint that was
                # never resolved.
                if e.code not in quiet:
                    detail = payload.get("detail") if isinstance(payload, dict) else None
                    errs = payload.get("errors") if isinstance(payload, dict) else None
                    warn(f"{method} {path} → {e.code} "
                         f"{json.dumps(errs or detail)[:200]}")
                return e.code, payload
            detail = payload.get("detail") if isinstance(payload, dict) else payload
            die(f"{method} {path} → HTTP {e.code}\n    {json.dumps(detail)[:600]}")
        except urllib.error.URLError as e:
            die(f"cannot reach {self.base} ({e.reason}). Is the stack up?")
        raise AssertionError("unreachable")

    def get(self, path: str, **kw):
        return self.call("GET", path, **kw)[1]

    def post(self, path: str, body: dict | None = None, **kw):
        return self.call("POST", path, body, **kw)[1]

    def patch(self, path: str, body: dict, **kw):
        return self.call("PATCH", path, body, **kw)[1]


# --------------------------------------------------------------------------- #
# The cast
#
# Names and emails only. Everything else about these people — their consents,
# their complaints, their requests — is produced by the API below.
#
# Addresses are under `example.com`, which RFC 2606 reserves precisely so it
# cannot be delivered to. This used to be `example.in`, which is NOT reserved —
# only example.com/.net/.org and the .example TLD are — and that stopped being
# harmless the moment a deployed environment had a real email provider: seeding
# it sent nine actual messages to a domain somebody else may own.
# --------------------------------------------------------------------------- #

PEOPLE = [
    ("Ananya Iyer",        "ananya.iyer@example.com",      "+919812345001"),
    ("Rohit Deshpande",    "rohit.deshpande@example.com",  "+919812345002"),
    ("Fatima Sheikh",      "fatima.sheikh@example.com",    "+919812345003"),
    ("Karthik Nair",       "karthik.nair@example.com",     "+919812345004"),
    ("Meera Bhattacharya", "meera.b@example.com",          "+919812345005"),
    ("Devansh Patel",      "devansh.patel@example.com",    "+919812345006"),
    ("Sneha Raghavan",     "sneha.raghavan@example.com",   "+919812345007"),
    ("Imran Qureshi",      "imran.qureshi@example.com",    "+919812345008"),
    ("Tanvi Kulkarni",     "tanvi.kulkarni@example.com",   "+919812345009"),
    ("Joseph Mathew",      "joseph.mathew@example.com",    "+919812345010"),
    ("Priya Venkatesan",   "priya.v@example.com",          "+919812345011"),
    # A minor, with a guardian on record. §9 consent is not implemented — this
    # record exists so the flag is visible somewhere real, not to imply the
    # guardian was verified. He was not.
    ("Aarav Menon",        "aarav.menon@example.com",      "+919812345012"),
]

MINOR_INDEX = 11
GUARDIAN_EMAIL = "lakshmi.menon@example.com"


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #

def tenant_id_from_token(access_token: str) -> str | None:
    """Read `tenant_id` out of the access token's payload.

    Not verified, and it does not need to be: the server already verified this
    token when it issued it, and the only use here is telling the backdate pass
    which tenant to scope itself to. `/v1/auth/me` mints a fresh token rather
    than returning a profile, so there is no endpoint to ask instead.
    """
    import base64
    try:
        body = access_token.split(".")[1]
        body += "=" * (-len(body) % 4)          # JWT strips base64 padding
        return json.loads(base64.urlsafe_b64decode(body)).get("tenant_id")
    except Exception:
        return None


def refuse_if_already_seeded(api: Api, force: bool) -> None:
    """Stop before adding a second copy of everything.

    Consents and principals are idempotent — the API keys them on
    `external_id` and on (principal, purpose), so a re-run updates rather than
    duplicates. Requests, complaints and breaches are not, and cannot be:
    filing the same complaint twice is a real thing a person can do, so the API
    is right to allow it and wrong to guess.

    A second run therefore doubled the queue and, worse, put every row through
    the backdate pass again — which aged the oldest complaint by five months and
    reported seven of ten as overdue. A data set that reads as total collapse is
    no more useful than an empty one.
    """
    counts = {}
    for label, path in (("rights requests", "/v1/dsar"),
                        ("complaints", "/v1/grievances"),
                        ("breaches", "/v1/breaches")):
        rows = api.get(path)
        if isinstance(rows, dict):
            rows = rows.get("items", [])
        counts[label] = len(rows or [])

    total = sum(counts.values())
    if total == 0:
        return

    summary = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
    if force:
        warn(f"workspace already holds {summary} — --force given, adding more "
             f"on top (timestamps will be shifted again)")
        return
    die(f"this workspace already holds {summary}.\n"
        f"    Seeding again would duplicate all of it and re-age every date.\n"
        f"    Use --register NAME for a clean workspace, or --force if you "
        f"really want more on top.")


def login(api: Api, workspace: str, email: str, password: str) -> dict:
    _, payload = api.call(
        "POST", "/v1/auth/login",
        {"tenant_slug": workspace, "email": email, "password": password},
        token=None, allow=(401, 422),
    )
    if not isinstance(payload, dict) or "access_token" not in payload:
        die(f"login failed for {email} in workspace {workspace!r}: "
            f"{json.dumps(payload)[:300]}")
    return payload


def register(api: Api, company: str) -> tuple[str, str, str]:
    """Create a workspace and return (slug, admin_email, password)."""
    slug = "".join(c for c in company.lower().replace(" ", "-") if c.isalnum() or c == "-")[:24]
    email = f"dpo@{slug}.example.com"
    password = "Demo!Workspace2026"
    status, payload = api.call(
        "POST", "/v1/auth/register",
        {
            "company_name": company,
            "workspace": slug,
            "admin_name": "Demo DPO",
            "admin_email": email,
            "password": password,
        },
        token=None, allow=(409, 422),
    )
    if status in (409, 422):
        warn(f"workspace {slug!r} already exists — signing in instead")
        login(api, slug, email, password)
    else:
        ok(f"registered workspace {slug!r}")
    return slug, email, password


def publish_notices(api: Api, purposes: list[dict]) -> dict[str, str]:
    """A published notice per purpose, so consent references one.

    §5 requires the notice; consent recorded without one is the paperwork gap
    this product is supposed to close. Where a notice already exists the API
    tells us so and we reuse it.
    """
    existing = {n["purpose_id"]: n for n in (api.get("/v1/notices") or [])
                if n.get("is_published")}
    out: dict[str, str] = {}
    for p in purposes:
        if p["id"] in existing:
            out[p["id"]] = existing[p["id"]]["id"]
            continue
        notice = api.post("/v1/notices", {
            "purpose_id": p["id"],
            "language": "en",
            "content": (
                f"We process your data for {p['name'].lower()}. This notice explains "
                f"what we collect, why, how long we keep it, and how to say no."
            ),
            "data_collected": p.get("category") or "Personal data",
            "user_rights": (
                "You may ask for a copy of your data, ask us to correct it, ask us to "
                "erase it, and complain to our Grievance Officer. If we do not resolve "
                "your complaint you may approach the Data Protection Board of India."
            ),
            "withdrawal_policy": (
                "You can withdraw this consent at any time from your privacy dashboard. "
                "Withdrawal stops future processing; it does not undo processing that "
                "already happened lawfully."
            ),
        }, allow=(409,))
        if not isinstance(notice, dict) or "id" not in notice:
            warn(f"notice for {p['key']} not created ({json.dumps(notice)[:120]})")
            continue
        api.post(f"/v1/notices/{notice['id']}/publish", allow=(409, 422))
        out[p["id"]] = notice["id"]
    ok(f"{len(out)} published notices")
    return out


LOGIN_COUNT = 4


def create_logins(api: Api) -> dict[str, str]:
    """Real accounts for the first few of the cast. Returns email -> user id.

    Runs BEFORE the principals are created, and the order is the whole point.
    The app resolves a signed-in person's own principal record by
    `external_id = f"user:{user.id}"` — see `ensureSelfPrincipal` in
    src/api/consent.js. Creating principals first, keyed `demo-001`, produced two
    records for the same human: the seeded one holding all their consents, and
    the one their login actually resolves to, holding nothing.

    The effect was invisible from the admin side and total from theirs: sign in
    as Ananya and the Preference Centre showed no consents, the history was
    empty, and the dashboard said "Nothing recorded yet" — for a person with
    three consents and two complaints in the database.
    """
    out: dict[str, str] = {}
    for name, email, _phone in PEOPLE[:LOGIN_COUNT]:
        status, row = api.call("POST", "/v1/admin/users", {
            "email": email,
            "full_name": name,
            "role": "data_principal",
            "password": DEMO_PASSWORD,
        }, allow=(409, 422))
        if status in (200, 201) and isinstance(row, dict) and "id" in row:
            out[email] = row["id"]
        elif status == 409:
            # Already there from an earlier run; find the id so the principal
            # still gets keyed correctly.
            users = api.get("/v1/admin/users")
            items = users.get("items", []) if isinstance(users, dict) else (users or [])
            match = next((u for u in items if u.get("email") == email), None)
            if match:
                out[email] = match["id"]
    ok(f"{len(out)} data-principal logins (password: {DEMO_PASSWORD})")
    return out


def create_principals(api: Api, user_ids: dict[str, str]) -> list[dict]:
    """One principal record per person, keyed so their own login finds it.

    Anybody with a login gets `user:<id>`, which is what the app looks for.
    Everybody else gets `demo-NNN` — they are people the organisation holds data
    about who have never signed in, which is the normal case.
    """
    rows: list[dict] = []
    listing = api.get("/v1/principals")
    items = listing.get("items", []) if isinstance(listing, dict) else (listing or [])
    existing = {p["external_id"]: p for p in items
                if isinstance(p, dict) and p.get("external_id")}

    for i, (name, email, phone) in enumerate(PEOPLE):
        uid = user_ids.get(email)
        ext = f"user:{uid}" if uid else f"demo-{i + 1:03d}"

        if ext in existing:
            rows.append(dict(existing[ext], _name=name, _email=email))
            continue

        body = {"external_id": ext, "email": email, "phone": phone}
        if i == MINOR_INDEX:
            body |= {"is_minor": True, "guardian_email": GUARDIAN_EMAIL}
        row = api.post("/v1/principals", body, allow=(409,))
        if isinstance(row, dict) and "id" in row:
            row["_name"] = name
            row["_email"] = email
            row["_has_login"] = bool(uid)
            rows.append(row)
        else:
            warn(f"principal {ext} not created ({json.dumps(row)[:120]})")

    with_logins = sum(1 for r in rows if r.get("_has_login"))
    ok(f"{len(rows)} data principals ({with_logins} of them can sign in)")
    return rows


def record_consents(
    api: Api, principals: list[dict], purposes: list[dict], notices: dict[str, str]
) -> int:
    """A realistic spread, not everyone-said-yes-to-everything.

    Mandatory purposes are granted for everyone (that is what mandatory means).
    Optional ones get a mix, and a handful are then withdrawn — because a
    consent ledger with no withdrawals in it proves nothing about whether
    withdrawal works.
    """
    granted = withdrawn = 0
    methods = ["banner", "checkbox", "api", "import"]

    for idx, person in enumerate(principals):
        # Which optional purposes this person actually agreed to. Needed because
        # the withdrawal below has to name one of them: the first version picked a
        # purpose arithmetically and asked the API to withdraw consent that had
        # never been granted, which is a 404 and not a withdrawal.
        agreed_optional: list[dict] = []

        for p in purposes:
            mandatory = bool(p.get("is_mandatory"))
            # Mandatory: everyone. Optional: about two thirds, deterministically.
            if not mandatory and RNG.random() > 0.68:
                continue
            body = {
                "principal_id": person["id"],
                "purpose_id": p["id"],
                "language": "en",
                "method": "guardian" if idx == MINOR_INDEX else RNG.choice(methods),
                "source": "seed_demo.py",
            }
            if p["id"] in notices:
                body["notice_id"] = notices[p["id"]]
            status, _ = api.call("POST", "/v1/consents", body, allow=(409, 422))
            if status in (200, 201):
                granted += 1
                if not mandatory:
                    agreed_optional.append(p)

        # Withdrawals: a few people change their mind, and only about something
        # they actually said yes to.
        if agreed_optional and idx % 4 == 1:
            target = agreed_optional[idx % len(agreed_optional)]
            status, _ = api.call("POST", "/v1/consents/withdraw", {
                "principal_id": person["id"],
                "purpose_id": target["id"],
                "reason": "Too many emails.",
            }, allow=(404, 409, 422))
            if status in (200, 201):
                withdrawn += 1

    ok(f"{granted} consents granted, {withdrawn} later withdrawn")
    return granted


def create_dsars(api: Api, workspace: str, principals: list[dict],
                 with_logins: list[dict]) -> list[dict]:
    """Requests at every stage, filed by both routes.

    A queue where everything is 'received' does not show the workflow; a queue
    where everything is 'completed' does not show the deadline pressure. Both
    exist here.
    """
    made: list[dict] = []

    # Filed by the people themselves, through their own login.
    for person in with_logins[:3]:
        tok = login(api, workspace, person["_email"], DEMO_PASSWORD)["access_token"]
        kind = ["access", "erasure", "access"][len(made) % 3]
        row = api.post("/v1/dsar", {"type": kind, "verification_method": "otp"},
                       token=tok, allow=(409, 422))
        if isinstance(row, dict) and "id" in row:
            made.append(row)

    # Raised by staff on someone's behalf — a DPO taking a phone call. The API
    # records this as requested_by=staff, which is the whole reason it is a
    # separate field.
    for person in principals[4:9]:
        kind = RNG.choice(["access", "access", "erasure", "correction"])
        body = {"principal_id": person["id"], "type": kind,
                "verification_method": "staff_verified"}
        if kind == "correction":
            body["correction_payload"] = {
                "field": "phone", "current": person.get("phone") or "",
                "requested": "+919800000000",
                "reason": "Number changed; old one belongs to someone else now.",
            }
        row = api.post("/v1/dsar", body, allow=(409, 422))
        if isinstance(row, dict) and "id" in row:
            made.append(row)

    # Walk a subset forward toward a target state. The rest stay where they are.
    #
    # Written as "aim at a target from wherever the row actually is" rather than
    # as a fixed chain, because a request does not reliably start at `received`:
    # once it is dispatched to the engine the service moves it to `in_progress`
    # on its own. A hardcoded verifying-then-in_progress chain asked an
    # already-in_progress request to go backwards, and the API refused — correctly,
    # and three times per run.
    transitions = {
        "received":    {"verifying", "in_progress", "rejected", "cancelled"},
        "verifying":   {"in_progress", "rejected", "cancelled"},
        "in_progress": {"completed", "rejected", "cancelled"},
    }
    notes = {
        "verifying":   {"note": "Identity check sent to the requester."},
        "in_progress": {"note": "Verified; collection started across connected systems."},
        "completed":   {"note": "Package generated and delivered to the requester."},
        "rejected":    {"reason": "Retained under a statutory obligation "
                                  "(RBI record-keeping, 8 years)."},
        "cancelled":   {"reason": "Withdrawn by the requester over the phone."},
    }
    targets = ["completed", "in_progress", "verifying", "rejected"]

    advanced = 0
    for i, row in enumerate(made):
        target = targets[i % len(targets)]
        # A rejection only makes sense on something we could lawfully refuse.
        if target == "rejected" and row.get("type") != "erasure":
            continue

        current = row.get("status") or "received"
        moved = False
        # At most three hops gets from `received` to any terminal state.
        for _ in range(3):
            if current == target:
                break
            allowed = transitions.get(current, set())
            if not allowed:
                break
            step = target if target in allowed else next(
                (s for s in ("verifying", "in_progress") if s in allowed), None)
            if step is None:
                break
            status, payload = api.call(
                "PATCH", f"/v1/dsar/{row['id']}/status",
                {"to_status": step, **notes.get(step, {})},
                allow=(409, 422), quiet=())
            if status not in (200, 201):
                break
            current = (payload or {}).get("status", step) if isinstance(payload, dict) else step
            moved = True
        if moved:
            advanced += 1

    ok(f"{len(made)} rights requests ({advanced} moved past where they were filed)")
    return made


def create_grievances(api: Api, workspace: str, with_logins: list[dict],
                      dsars: list[dict]) -> list[dict]:
    """Complaints in each state the officer console has to handle."""
    scripts = [
        ("consent_violation",
         "I withdrew consent for marketing email on the 3rd and received two more "
         "campaigns after that. The preference centre shows the consent as withdrawn, "
         "so something downstream is still using the old list."),
        ("dsar_delay",
         "I asked for a copy of my data and have heard nothing. The acknowledgement "
         "said thirty days and that has now passed without any update."),
        ("inaccurate_data",
         "My date of birth is wrong in your records and it is causing my premium to be "
         "calculated incorrectly. I have corrected it twice through the app."),
        ("other",
         "I cannot find anywhere on your website to see what data you hold about me. "
         "The privacy policy links to a page that does not load."),
        ("data_breach",
         "I received a phishing message that quoted my policy number and the last four "
         "digits of my account. Nobody outside your organisation should have both."),
    ]

    made: list[dict] = []
    for i, (category, description) in enumerate(scripts):
        person = with_logins[i % len(with_logins)]
        tok = login(api, workspace, person["_email"], DEMO_PASSWORD)["access_token"]
        body = {"category": category, "description": description}
        # Tie the delay complaint to a real request, which is what makes it a
        # DSAR-delay complaint rather than a generic one.
        if category == "dsar_delay":
            mine = api.get("/v1/dsar/mine", token=tok) or []
            if mine:
                body["related_dsar_id"] = mine[0]["id"]
        row = api.post("/v1/grievances", body, token=tok, allow=(409, 422))
        if isinstance(row, dict) and "id" in row:
            made.append(row)

    # Officer work. Each of these is a *chain*, because the state machine does not
    # allow open -> resolved: a complaint nobody ever picked up cannot have been
    # resolved, which is the correct rule. The first version of this jumped
    # straight to `resolved`, was refused with a 409, and — because a 409 was
    # quiet at the time — reported five complaints in four states while the
    # database held three still sitting open.
    chains: list[tuple[int, list[tuple[str, dict]]]] = [
        (0, [("in_progress", {"note": "Reproduced against the campaign tool's "
                                      "suppression list."}),
             ("resolved", {"resolution_notes":
                           "The withdrawal had not propagated to the campaign tool. "
                           "Suppression list re-synced and a check added to the "
                           "nightly job. The two sends in the gap are logged."})]),
        (1, [("acknowledged", {"note": "Received and assigned; awaiting the "
                                       "fulfilment team's timeline."})]),
        (2, [("in_progress", {"note": "Reproduced. Correction queued with the "
                                      "policy team."})]),
        (3, [("rejected", {"rejection_reason":
                           "The privacy page was unreachable for four hours during a "
                           "deployment and is live again. No personal data was "
                           "affected, so there is nothing to remedy beyond the "
                           "outage itself."})]),
        # made[4] is left open on purpose, and the backdate pass makes it overdue.
    ]
    for index, chain in chains:
        if index >= len(made):
            continue
        for to_status, extra in chain:
            api.call("PATCH", f"/v1/grievances/{made[index]['id']}",
                     {"to_status": to_status, **extra},
                     allow=(409, 422), quiet=())

    ok(f"{len(made)} complaints (resolved / acknowledged / in progress / "
       f"rejected / open)")
    return made


def create_retention(api: Api) -> int:
    policies = [
        # Deliberately no auto_delete anywhere. A seeder that arms an automatic
        # purge in someone's demo workspace is a seeder that eventually deletes
        # something they wanted.
        {"name": "Marketing contact data", "data_category": "Contact Data",
         "retention_days": 730, "action": "mask", "auto_delete": False,
         "notify_days": 30, "exemption_code": "none"},
        {"name": "Product analytics events", "data_category": "Usage Data",
         "retention_days": 395, "action": "delete", "auto_delete": False,
         "notify_days": 14, "exemption_code": "none"},
        {"name": "KYC and account records", "data_category": "Identity Data",
         "retention_days": 2920, "action": "mask", "auto_delete": False,
         "notify_days": 60, "exemption_code": "statutory",
         "exemption_reference": "RBI Master Direction — KYC, records for 8 years"},
        {"name": "Support call recordings", "data_category": "Communications Data",
         "retention_days": 180, "action": "delete", "auto_delete": False,
         "notify_days": 7, "exemption_code": "none"},
    ]
    made = existed = 0
    for p in policies:
        status, _ = api.call("POST", "/v1/retention/policies", p, allow=(409, 422))
        if status in (200, 201):
            made += 1
        elif status == 409:
            existed += 1
    # "0 retention policies" was what a re-run printed once the API started
    # returning 409 for a duplicate name, and it reads like a failure rather than
    # like nothing needing doing.
    note = f", {existed} already present" if existed else ""
    ok(f"{made} retention policies created{note} (none set to auto-delete)")
    return made


def create_breaches(api: Api) -> list[dict]:
    """One incident worked all the way through, one still open.

    `discovered_at` is settable on this endpoint, so unlike everything else here
    the 72-hour board clock can be made to look real without touching the
    database.
    """
    now = datetime.now(timezone.utc)
    made: list[dict] = []

    # 1 — worked through to closure.
    b1 = api.post("/v1/breaches", {
        "title": "Misconfigured storage bucket exposed statement PDFs",
        "description":
            "A storage container holding generated account statements was set to "
            "public-read during a migration on the 4th. Access logs show 1,240 objects "
            "were fetched by three IPs outside our estate before the setting was "
            "reverted. Statements contain name, masked account number and balance.",
        "severity": "high",
        "discovered_at": (now - timedelta(days=9)).isoformat(),
        "occurred_at": (now - timedelta(days=12)).isoformat(),
        "categories_affected": ["Identity Data", "Financial Data"],
        "estimated_affected_count": 1240,
    }, allow=(409, 422))
    if isinstance(b1, dict) and "id" in b1:
        made.append(b1)
        bid = b1["id"]
        # draft -> investigating -> contained. `notified` and `closed` are not
        # reachable from here on purpose: the API only lets the notification
        # endpoints move a breach into `notified`, so the status cannot claim
        # people were told before they were.
        api.call("POST", f"/v1/breaches/{bid}/status",
                 {"to_status": "investigating", "note": "Incident channel opened; "
                  "storage team pulled access logs."}, allow=(409, 422), quiet=())
        api.call("POST", f"/v1/breaches/{bid}/status",
                 {"to_status": "contained", "note": "Container ACL reverted; "
                  "public-access block enforced at subscription level."},
                 allow=(409, 422), quiet=())
        api.call("POST", f"/v1/breaches/{bid}/affected",
                 {"categories": ["Identity Data", "Financial Data"]},
                 allow=(409, 422), quiet=())
        api.call("POST", f"/v1/breaches/{bid}/notify-board", {
            "submitted_by": "Demo DPO",
            "board_reference": "DPBI/2026/INC/00417",
        }, allow=(409, 422), quiet=())

        # Remediation has to be on the record BEFORE the affected people are
        # written to. The service refuses otherwise, and the refusal is right:
        # "we lost your statements, and here is nothing you can do" is not a
        # notice. So it is recorded here rather than only at close.
        root_cause = (
            "A Terraform module default flipped the container ACL to public during an "
            "unrelated migration. The plan output showed the change and it was not "
            "read."
        )
        remediation = (
            "ACL reverted within 40 minutes of discovery. Public-access block now "
            "enforced at the subscription level so an individual container cannot "
            "override it, and plan diffs touching ACLs fail CI without a second "
            "approver. Affected customers were issued new statement links."
        )
        api.call("PATCH", f"/v1/breaches/{bid}",
                 {"root_cause": root_cause, "remediation": remediation},
                 allow=(409, 422), quiet=())
        api.call("POST", f"/v1/breaches/{bid}/notify-principals", {},
                 allow=(409, 422), quiet=())
        api.call("POST", f"/v1/breaches/{bid}/close",
                 {"root_cause": root_cause, "remediation": remediation},
                 allow=(409, 422), quiet=())

    # 2 — open, inside the 72-hour window, board not yet notified.
    b2 = api.post("/v1/breaches", {
        "title": "Support agent account accessed from an unrecognised device",
        "description":
            "MFA-approved sign-in to a support console account from a device and "
            "geography that account has never used. The session viewed 34 customer "
            "records before it was terminated. Whether the access was malicious is not "
            "yet established.",
        "severity": "medium",
        "discovered_at": (now - timedelta(hours=19)).isoformat(),
        "categories_affected": ["Identity Data", "Contact Data"],
        "estimated_affected_count": 34,
    }, allow=(409, 422))
    if isinstance(b2, dict) and "id" in b2:
        made.append(b2)
        api.call("POST", f"/v1/breaches/{b2['id']}/status",
                 {"to_status": "investigating",
                  "note": "Account suspended, device fingerprint pulled, agent "
                          "interviewed. Awaiting SIEM correlation."},
                 allow=(409, 422), quiet=())

    ok(f"{len(made)} breaches (one closed, one open inside the 72-hour window)")
    return made


# --------------------------------------------------------------------------- #
# The backdate pass — read the header comment before changing anything here.
# --------------------------------------------------------------------------- #

BACKDATE_SQL = """
SET LOCAL app.tenant_id = '{tenant_id}';

-- Grievances. Spread by filing order, keeping every interval inside a row intact
-- so the CHECK constraints and the escalation arithmetic still hold.
--
-- The stride is four days, chosen against the defaults rather than picked for
-- looks: the grievance SLA is 15 days and escalation fires at 10, so five
-- complaints land at 4, 8, 12, 16 and 20 days old. That puts the one still-open
-- complaint past its deadline and the in-progress one inside the escalation
-- window, which is the picture worth showing. A nine-day stride — the first
-- attempt — made four of five overdue, and an organisation failing everything
-- demonstrates as little as one with no data at all.
--
-- Note the ordering: n=1 is the *earliest* submitted and gets the smallest
-- shift, so the complaint left open ends up the oldest.
WITH ordered AS (
  SELECT id, row_number() OVER (ORDER BY submitted_at) AS n FROM grievances
)
UPDATE grievances g SET
  submitted_at    = g.submitted_at    - make_interval(days => (o.n * 4)::int),
  deadline_at     = g.deadline_at     - make_interval(days => (o.n * 4)::int),
  escalate_at     = g.escalate_at     - make_interval(days => (o.n * 4)::int),
  acknowledged_at = g.acknowledged_at - make_interval(days => (o.n * 4)::int),
  resolved_at     = g.resolved_at     - make_interval(days => (o.n * 4)::int),
  escalated_at    = g.escalated_at    - make_interval(days => (o.n * 4)::int),
  created_at      = g.created_at      - make_interval(days => (o.n * 4)::int)
FROM ordered o WHERE o.id = g.id;

-- Rights requests. Same idea, tighter spacing: the statutory clock here is
-- 30 days, so a nine-day stride would push everything into breach and the queue
-- would read as total failure rather than a working queue under pressure.
WITH ordered AS (
  SELECT id, row_number() OVER (ORDER BY submitted_at) AS n FROM dsar_requests
)
UPDATE dsar_requests d SET
  submitted_at = d.submitted_at - make_interval(days => (o.n * 4)::int),
  deadline_at  = d.deadline_at  - make_interval(days => (o.n * 4)::int),
  resolved_at  = d.resolved_at  - make_interval(days => (o.n * 4)::int),
  verified_at  = d.verified_at  - make_interval(days => (o.n * 4)::int),
  created_at   = d.created_at   - make_interval(days => (o.n * 4)::int)
FROM ordered o WHERE o.id = d.id;

-- Consents. Ages the ledger so 'agreed last week' and 'agreed a year ago' are
-- distinguishable, and so anything carrying an expiry has a believable one.
--
-- `expires_at` moves with `given_at` rather than staying put, because the expiry
-- was derived from when consent was given. Shifting one without the other would
-- invent consents that expired before they were granted.
WITH ordered AS (
  SELECT id, row_number() OVER (ORDER BY created_at) AS n FROM consents
)
UPDATE consents c SET
  given_at     = c.given_at     - make_interval(days => ((o.n % 24) * 15)::int),
  withdrawn_at = c.withdrawn_at - make_interval(days => ((o.n % 24) * 15)::int),
  expires_at   = c.expires_at   - make_interval(days => ((o.n % 24) * 15)::int),
  created_at   = c.created_at   - make_interval(days => ((o.n % 24) * 15)::int)
FROM ordered o WHERE o.id = c.id;
"""


def is_local(base_url: str) -> bool:
    """Whether `--base-url` points at the local compose stack."""
    from urllib.parse import urlparse
    host = (urlparse(base_url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def backdate(tenant_id: str, base_url: str) -> bool:
    """Shift timestamps only. Never inserts, never invents a value.

    Runs through `docker compose exec` as the owner role, because the app role
    is intentionally not allowed to rewrite history — which is the correct
    setting and the reason this is a separate, opt-out pass rather than part of
    the seed.

    REFUSES TO RUN AGAINST A REMOTE TARGET, and that guard is not paranoia — it
    is a bug this had. `docker compose exec cms-db` always reaches the *local*
    database, whatever `--base-url` says. Seeding a deployed environment
    therefore sent these UPDATEs to the wrong server, matched nothing (the
    tenant id belongs to the remote one), and still reported
    "timestamps backdated" — so the summary claimed a data set with an overdue
    complaint in it while the target had everything dated today.
    """
    if not is_local(base_url):
        warn(f"backdate pass SKIPPED — {base_url} is not the local stack, and "
             f"this pass can only reach the local database")
        print("    Everything is dated today, so nothing is overdue and no")
        print("    deadline is close. To age a deployed environment, run the")
        print("    UPDATEs in BACKDATE_SQL against it directly as the owner role.")
        return False
    sql = BACKDATE_SQL.format(tenant_id=tenant_id)
    proc = subprocess.run(
        ["docker", "compose", "exec", "-T", "cms-db",
         "psql", "-U", "datashield_owner", "-d", "datashield", "-v", "ON_ERROR_STOP=1",
         "-c", f"BEGIN; {sql} COMMIT;"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        warn("backdate pass failed — the data is still valid, just all dated today")
        print("    " + (proc.stderr or proc.stdout).strip().replace("\n", "\n    ")[:600])
        return False
    ok("timestamps backdated (audit entries left alone — they are hash-chained)")
    return True


def nudge_scheduler(api: Api) -> None:
    """Run the background jobs now instead of waiting for their next tick.

    Escalation is decided by comparing `escalate_at` against the clock, and the
    backdate pass has just moved a complaint's `escalate_at` into the past. The
    scheduler would notice on its own — `grievance.escalate` runs every fifteen
    minutes — but a data set that only becomes correct a quarter of an hour after
    it is created is a trap for whoever demos it.
    """
    for job in ("notifications.drain", "grievance.escalate"):
        status, _ = api.call("POST", f"/v1/admin/jobs/{job}/run", {},
                             allow=(404, 409, 422, 423), quiet=())
        if status in (200, 201, 202):
            ok(f"ran {job}")


def consent_expiry_note(api: Api) -> None:
    overview = api.get("/v1/consents/overview")
    if isinstance(overview, dict):
        bits = ", ".join(f"{k}={v}" for k, v in overview.items()
                         if isinstance(v, int))
        if bits:
            print(f"    consent overview: {bits}")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Fill a DataShield workspace with a realistic data set.")
    ap.add_argument("--base-url", default=DEFAULT_BASE,
                    help=f"backend base URL (default {DEFAULT_BASE})")
    ap.add_argument("--register", metavar="COMPANY",
                    help="create a new workspace for COMPANY and seed that")
    ap.add_argument("--workspace", help="seed an existing workspace (slug)")
    ap.add_argument("--email", help="admin email for --workspace")
    ap.add_argument("--password", help="admin password for --workspace")
    ap.add_argument("--no-backdate", action="store_true",
                    help="leave every timestamp at today (see the header comment)")
    ap.add_argument("--force", action="store_true",
                    help="seed on top of a workspace that already has data")
    args = ap.parse_args()

    if not args.register and not args.workspace:
        ap.error("give either --register COMPANY or --workspace SLUG")
    if args.workspace and not (args.email and args.password):
        ap.error("--workspace needs --email and --password")

    api = Api(args.base_url)

    bold("DataShield — seed a demo workspace")

    if args.register:
        slug, email, password = register(api, args.register)
    else:
        slug, email, password = args.workspace, args.email, args.password

    session = login(api, slug, email, password)
    api.token = session["access_token"]
    tenant_id = tenant_id_from_token(api.token)
    if not tenant_id:
        warn("could not read tenant_id; backdate pass will be skipped")
    ok(f"signed in to {slug!r} as {email}")
    refuse_if_already_seeded(api, args.force)

    purposes = api.get("/v1/purposes") or []
    if isinstance(purposes, dict):
        purposes = purposes.get("items", [])
    if not purposes:
        die("this workspace has no purposes — registration should have seeded four")
    ok(f"{len(purposes)} purposes on record")

    notices = publish_notices(api, purposes)
    user_ids = create_logins(api)
    principals = create_principals(api, user_ids)
    if not principals:
        die("no data principals were created; nothing downstream can be seeded")
    record_consents(api, principals, purposes, notices)
    # The people who can sign in, so their own screens show their own data.
    with_logins = [p for p in principals if p.get("_has_login")]
    dsars = create_dsars(api, slug, principals, with_logins)
    create_grievances(api, slug, with_logins, dsars)
    create_retention(api)
    create_breaches(api)

    if args.no_backdate:
        warn("skipped the backdate pass — everything is dated today, so nothing "
             "is overdue and no deadline is close")
    elif tenant_id:
        backdate(str(tenant_id), args.base_url)

    # After the shift, not before: escalation is judged against the dates the
    # backdate pass has just written.
    nudge_scheduler(api)
    consent_expiry_note(api)

    print()
    bold("Sign in")
    # Strip a trailing /api: the browser entry point is the site root, while
    # --base-url points at the API mounted beneath it.
    browser = args.base_url.replace(":8100", ":8090")
    if browser.rstrip("/").endswith("/api"):
        browser = browser.rstrip("/")[: -len("/api")]
    print(f"  {browser.rstrip('/')}/login")
    print(f"  workspace : {slug}")
    print(f"  admin     : {email} / {password}")
    print(f"  a person  : {principals[0]['_email']} / {DEMO_PASSWORD}"
          if principals and principals[0].get("_email") else "")
    print()
    print("  Everything above went through the same API the browser uses. Timestamps")
    print("  were shifted afterwards; audit entries were not, so the audit trail")
    print("  honestly reports the seed run as the moment each event was recorded.")


if __name__ == "__main__":
    main()
