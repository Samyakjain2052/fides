# DataShield backend

Multi-tenant DPDP Act consent-management API. FastAPI · PostgreSQL 16 ·
SQLAlchemy 2.0 async · Alembic.

**Read [ARCHITECTURE.md](ARCHITECTURE.md) first** — it is the plan of record and
explains *why* each decision was made. This file is how to run it.

Phases 0–2 (the spine) are built and tested. Phases 3–10 are planned and not
started; see the table in ARCHITECTURE.md §9.

---

## Run it

From the repository root:

```bash
docker compose up -d cms-db cms-backend
curl -s localhost:8100/health | jq
open http://localhost:8100/docs
```

`cms-db` creates the two database roles on first boot. Migrations run in their
own one-shot service, `cms-migrate`, which `cms-backend` waits on
(`condition: service_completed_successfully`) — so a deploy still cannot serve an
un-migrated schema, but two replicas cannot race each other to migrate, and the
app image needs no schema-altering rights.

### Tests

```bash
docker compose run --rm cms-test
```

It runs against **`datashield_test`, a different database** from the one
`cms-backend` serves. The suite truncates every table between tests; pointed at
the application's database it silently destroyed whatever workspace you had been
demoing with — which is exactly how the problem was found. `cms-test` migrates
its own database to head before running, so it is never stale.

`cms-test` builds the image's `dev` target — the runtime layers plus pytest. The
deployed image (`runtime`) has neither pytest nor a compiler, so the tests run
against the same layers that ship without those layers carrying test tooling.

Tests run **in the container, against a real PostgreSQL**, and that is not
negotiable: row-level security, the append-only trigger and advisory locks do not
exist outside Postgres. A suite that stubs the database would pass while the
product leaked.

Local `pip install` may fail on Python 3.13+ — pydantic-core has no wheel yet and
wants Rust. The image pins 3.12. Use the container.

---

## The three guarantees, and how each is enforced

Everything else in here is ordinary CRUD. These three are the product.

### 1. A tenant cannot see another tenant's data

Not by careful coding — by the database.

```
application  services filter by tenant                (convention)
DATABASE     RLS policies append the filter regardless (guarantee)
tests        cross-tenant isolation suite              (proof)
```

Two roles make it real:

| Role | Used by | RLS applies? |
| --- | --- | --- |
| `datashield_owner` | Alembic. Owns the tables. | No — owners bypass RLS |
| `datashield_app` | **the application** | **Yes** (`NOBYPASSRLS`, not the owner) |

Per request, inside the transaction:

```sql
SELECT set_config('app.tenant_id', '<uuid>', true);   -- true = SET LOCAL
```

`SET LOCAL` matters: connections are pooled, and a session-scoped variable would
leak one tenant's context into the next request on that connection. Policies
compare against `NULLIF(current_setting('app.tenant_id', true), '')::uuid`, so an
unset context matches **nothing** — it fails closed.

Verify on a running database:

```sql
SELECT tablename, rowsecurity, relforcerowsecurity FROM pg_tables t
  JOIN pg_class c ON c.relname = t.tablename WHERE schemaname = 'public';
SELECT rolname, rolbypassrls FROM pg_roles WHERE rolname LIKE 'datashield%';
```

`tenants` is deliberately **not** under RLS: login must find a tenant by slug
before any context exists, and it holds company configuration, not personal data.

### 2. The audit trail is tamper-evident

Append-only is not enough — an append-only table where someone rewrites a row
leaves no trace. So each entry is an **HMAC-SHA256 over its own contents plus the
previous entry's hash**:

| Attack | Detected by |
| --- | --- |
| Edit an entry | its hash no longer matches its contents |
| Delete an entry | the next entry's `prev_hash` no longer matches |
| Reorder entries | same, from the other direction |
| Forge a whole chain | needs the HMAC key, which is in the secret manager, not the database |
| App deletes a row | `REVOKE UPDATE, DELETE` **and** a trigger that raises |

`POST /v1/audit/verify` walks the chain and reports the first break. What it
cannot catch is truncation of the newest entries — that is what external anchoring
(Phase 10) is for.

Writes take a per-tenant advisory lock, and `UNIQUE (tenant_id, seq)` is the
backstop, because a forked chain is unrecoverable.

### 3. Permissions are enforced server-side

Every route declares a capability:

```python
current: Annotated[CurrentUser, Depends(require(Capability.USER_MANAGE))]
```

The frontend hiding a menu item is presentation. This is enforcement. Demonstrated:

```
auditor GET  /v1/audit          -> 200
auditor GET  /v1/admin/users    -> 403  {"required": ["user:manage"], "role": "auditor"}
auditor POST /v1/admin/api-keys -> 403
no token                        -> 401
```

The role is re-read from the database on every request, not taken from the JWT: a
role revoked two minutes ago must not keep working until the token expires.

`AUDIT_WRITE` and `AUDIT_DELETE` are not in the `Capability` enum at all, so no
role can be misconfigured into holding them.

---

## Authentication

**Self-serve signup.** `POST /v1/auth/register` creates a tenant and its first
Admin/DPO in one transaction, writes both audit entries, and signs them in. A
tenant with no admin is an unusable account, so it is all-or-nothing.

Registration deliberately does not confirm whether a company already exists — a
taken workspace and a reserved one return the identical "not available", because
otherwise signup becomes a way for a competitor to enumerate our customers.
`GET /v1/auth/workspace-available` *does* answer that question, which is a
considered trade: a form that hides it until submit is hostile, and the same fact
is obtainable by just attempting to register.

**Humans.** Argon2id passwords · 15-minute JWT access token · opaque refresh token
stored hashed and delivered as `HttpOnly; Secure; SameSite=Strict`. Rotation is
single-use, and **presenting a spent token revokes its whole family** — reuse means
it leaked, and we cannot tell which holder is legitimate, so neither is trusted.

Refresh tokens are `<tenant-hex>.<secret>`. The tenant travels in the clear
because refresh happens before any tenant context exists and `refresh_tokens` is
under RLS — a lookup without a tenant matches nothing. Claiming someone else's
tenant just means the hash matches no row there.

Login failures are deliberately indistinguishable — unknown tenant, unknown email
and wrong password give the same message and comparable timing, because "this
email has an account with this company" is itself personal data.

**Machines.** `ds_live_<random>` API keys: prefix stored (greppable when leaked),
secret Argon2-hashed, plaintext shown exactly once. Per-key **scopes**, so a key
in a marketing service can read consent and cannot erase anybody.

---

## Four bugs the tests caught

Recorded because they are all the kind that pass a code review and fail in
production:

1. **`refresh` could never find its own token.** The lookup ran before tenant
   context existed, so RLS returned zero rows. Refresh was completely broken.
   Fixed by carrying the tenant in the token.
2. **Failed-login bookkeeping was rolled back.** The counter increment and audit
   entry were written on the request transaction, then `raise` rolled them back —
   so lockout never triggered and no failed login was ever recorded. Brute-force
   protection was silently doing nothing. Now written in their own committed
   transaction.
3. **Family revocation on token reuse had the same defect** — the revocation
   rolled back with the 401, leaving the stolen token live.
4. **The engine was bound to the import-time event loop**, so any code path
   opening its own session died with "Event loop is closed" outside the main loop.
   Now cached per loop.

Also worth knowing: the first version of the tamper test *appeared* to pass while
tampering with nothing, because `FORCE ROW LEVEL SECURITY` blocks even the table
owner without tenant context. The test now sets it explicitly — which is RLS
proving itself in the course of testing something else.

---

## Layout

```
app/
  core/       config (fails fast) · security (argon2, JWT, HMAC) · logging
              (JSON, PII-scrubbed) · errors (problem+json) · permissions (matrix)
  db/         base + mixins · session (per-loop engine, tenant context)
  models/     tenant · user + refresh_token · api_key · audit
  schemas/    Pydantic in/out — never the ORM models, so password_hash cannot
              accidentally serialise
  services/   business logic; the ONLY place that writes to the audit trail
  api/        deps.py (auth + capability guards) · v1/ routers (thin)
migrations/   Alembic; the initial revision also creates RLS policies, grants
              and the append-only trigger
tests/        test_isolation.py is the most important file here
```

Layering rule: **routers never touch the database, services never touch HTTP.**

---

## Configuration

Copy `.env.example` to `.env`. Two things to notice:

- **Two database URLs.** `DS_DATABASE_URL` (app, restricted) and
  `DS_DATABASE_OWNER_URL` (migrations, owner). Running the app on the owner URL
  silently disables every isolation policy.
- **Two secrets.** `DS_JWT_SECRET` signs sessions; `DS_AUDIT_HMAC_KEY` signs the
  audit chain. Separate blast radius — leaking the session key must not let anyone
  forge history. Both must be 32+ chars, and the config refuses to start on a
  placeholder or with `debug`/insecure cookies in `prod`.

---

## The public API (Phase 4)

What customers' own systems call. Mounted at its own root — **not** under
`DS_API_PREFIX` — because these paths are a contract other companies deploy
against and must not move when the console's API version does.

```
GET  /public/v1/purposes        what you can ask about, and whether a notice is published
GET  /public/v1/consent/check   "do I have consent right now?"  ← belongs in your request path
POST /public/v1/consent         collect or withdraw. Send Idempotency-Key.
```

Auth is `X-API-Key: ds_live_…` (or `Authorization: Bearer ds_live_…`) with per-key
scopes: a key holding only `consent:read` gets a 403 naming the scope it lacks if
it tries to write.

**Collect and withdraw are separate scopes.** `consent:write` was one scope until
the publishable-key work forced the split: recording a consent that never happened
is bad, but destroying a real one is worse — it deletes genuine evidence and stops
the customer's downstream processing for someone who never asked. A credential has
to be trusted separately for the destructive half, so `granted: false` requires
`consent:withdraw` even on a secret key.

### Publishable keys — browser banners

```
GET  /public/v1/banner/purposes   what a banner may offer, with the notice wording
POST /public/v1/banner/consent    collect only. No withdraw path exists here.
```

Auth is `X-Publishable-Key: pk_live_…` — its own header, never `Authorization`,
so a publishable key cannot be confused with a secret one at any layer.

These keys ship inside a web page, so they are treated as public and extractable.
Safety comes from the key being **incapable of harm** (`consent:collect` only,
enforced by a constant, by the service, and by a CHECK constraint) plus
**server-observed provenance** on every record — not from secrecy. Origin pinning
is defence-in-depth, and an optional signed token from the integrator's own server
provides real principal binding for sensitive purposes.

Full reasoning, including what the model does *not* claim:
[docs/PUBLISHABLE_KEY_SECURITY.md](../docs/PUBLISHABLE_KEY_SECURITY.md).

- **Rate limit** — fixed window, counted in `api_request_log`, so it survives a
  restart and holds across replicas instead of living in one process's memory. The
  window's edges are honest: a caller can get 2N across a boundary, which for a
  consent check is not worth a second datastore to prevent.
  `X-RateLimit-Limit` / `X-RateLimit-Remaining` on every response.
- **Idempotency** — a repeat of the same key replays the first response and sets
  `Idempotent-Replay: true`. The same key with a *different* body is a 409, not a
  replay: that is a client bug, and hiding it would leave the customer missing a
  consent record with nothing to explain it.
- **Request log** — append-only, no UPDATE or DELETE grant. It holds no request
  bodies; a log of bodies would be a second copy of everyone's personal data with
  none of the consent machinery around it.

### Two bugs this phase surfaced

Recorded because both had been sitting unexercised and both were invisible:

1. **API-key auth could not work at all.** `api_keys` is under RLS, so the lookup —
   which happens before any tenant context exists — matched nothing and every
   valid key 401'd. Keys now carry their tenant (`ds_live_<tenant-hex>.<secret>`)
   so the context is bound first. Same fix as the refresh token, for the same
   reason. The tenant in the clear is not trusted: the secret authenticates and
   RLS means a forged tenant finds no row.
2. **`TimestampMixin` was not timezone-aware** while every migration creates
   `timestamptz`, so any comparison against an aware datetime raised. The
   rate-limit window was the first such query; retention and expiry would have
   been next.

---

## Rights requests (Phase 5)

```
POST   /v1/dsar                    raise one (own request, or someone else's with dsar:process)
GET    /v1/dsar                    the triage queue
GET    /v1/dsar/mine               the caller's own
PATCH  /v1/dsar/{id}/status        advance, reject with a reason, cancel
POST   /v1/dsar/{id}/retry         re-dispatch after a failed engine call
GET    /v1/dsar/{id}/package       the access package — audited, and it expires
```

The engine was never the missing piece; the **record** was. A submitted request
used to live in the browser's `localStorage`, so it was invisible to the DPO,
invisible on another device, and gone if the person cleared their browser —
while the erasure it triggered had genuinely happened. That stopgap is deleted.

Four things carry the compliance weight:

- **The deadline is computed server-side** from `tenants.dsar_sla_days`. There is
  no parameter through which a caller could influence it, because a deadline a
  caller can set is not a statutory deadline.
- **A rejection must say why and a completion must say when** — CHECK
  constraints, not service-level politeness.
- **The engine cannot overrule a human.** A DPO's rejection is a decision; a late
  callback saying "complete" must not resurrect it, and `refresh_from_engine`
  will not even consult the engine for a closed request.
- **A failed dispatch does not lose the request.** It stays at `received` with
  the reason on its timeline and can be retried — losing somebody's rights
  request because a downstream was briefly down would be the worst way to fail.

`dsar_events` is the queryable timeline; the audit chain is the tamper-evident
evidence. Both are written, neither is redundant, and a divergence is a bug.

Correction is a tracked **manual** workflow: the engine has no correction action,
and a CHECK constraint stops a correction from carrying an engine reference. A
right handled by hand beats a right quietly dropped.

---

## Retention and the purge executor (Phase 7)

```
GET  /v1/retention/policies
POST /v1/retention/policies
POST /v1/retention/policies/{id}/preview   dry run — reports, changes nothing
POST /v1/retention/policies/{id}/run       LIVE — needs the policy name back
GET  /v1/retention/runs                    history
GET  /v1/retention/runs/{id}/items         the receipt
```

**This is the only code in the product that destroys data**, and it is built
accordingly.

- **`select_candidates` is called by both the dry run and the live run.** Not two
  implementations that agree — one implementation. Two that can diverge is
  exactly how a preview reports four rows and the live run destroys four hundred.
  A CHECK constraint backs it up: a `dry_run` with `rows_affected > 0` cannot
  exist.
- **`auto_delete` defaults to false, `action` defaults to `mask`.** The safer
  option is what you get by not thinking about it.
- **A policy that destroys automatically must warn first** — a CHECK, so no route
  to saving one exists.
- **A live run needs the policy's name typed back**, and the UI never pre-fills
  it. Same reason `rm -rf` prompts.
- **Receipts are append-and-read**, and every skip records its reason: "not
  purged because they have an open rights request" is the answer to a question
  somebody will eventually ask.

What a purge does: **masks the identifiers, keeps the row** — mirroring the DSAR
erasure path, because two meanings of "erased" in one product is an audit
contradiction. Consent records are never destroyed; they are the evidence that
holding the data was lawful.

Three things always stop a purge, checked inside the transaction so a hold
created a moment ago is seen: a **legal hold** on the person, an **open rights
request**, and an **active consent**.

---

## Next: Phase 6

Purposes, **versioned notices** (consent is bound to a notice version — see
ARCHITECTURE.md N4), data principals, and the consent lifecycle. Then Phase 4, the
public API with rate limits and idempotency, which is the part customers actually
integrate against.

When adding a table: give it `tenant_id`, add it to `TENANT_SCOPED_TABLES` in
`app/models/__init__.py`, and add its RLS policy in the migration. A table holding
customer data with no policy is the one mistake this codebase is arranged to make
hard.
