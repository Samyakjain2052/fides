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

`cms-db` creates the two database roles on first boot; `cms-backend` runs
`alembic upgrade head` before starting uvicorn, so a deploy can never serve an
un-migrated schema.

### Tests

```bash
docker compose run --rm --no-deps \
  -e DS_DATABASE_URL="postgresql+asyncpg://datashield_app:apppassword@cms-db:5432/datashield" \
  -e DS_DATABASE_OWNER_URL="postgresql+asyncpg://datashield_owner:ownerpassword@cms-db:5432/datashield" \
  cms-backend pytest -q
```

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

## Next: Phase 3

Purposes, **versioned notices** (consent is bound to a notice version — see
ARCHITECTURE.md N4), data principals, and the consent lifecycle. Then Phase 4, the
public API with rate limits and idempotency, which is the part customers actually
integrate against.

When adding a table: give it `tenant_id`, add it to `TENANT_SCOPED_TABLES` in
`app/models/__init__.py`, and add its RLS policy in the migration. A table holding
customer data with no policy is the one mistake this codebase is arranged to make
hard.
