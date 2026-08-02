# DataShield backend — architecture and delivery plan

The backend that turns [frontend/](../frontend/) from a mock-data prototype into a
product other companies can buy.

This document is the plan of record. It states the decisions, **why** each one was
made, and what gets built in what order. Read it before changing anything
structural.

---

## 1. What this has to be

A **multi-tenant SaaS backend** for DPDP Act consent management. Each customer is
a Data Fiduciary; their users are Data Principals. That framing produces five
non-negotiables, and every design decision below traces back to one of them:

| # | Non-negotiable | Why |
| --- | --- | --- |
| **N1** | **No tenant can ever see another tenant's data** | A leak here isn't "our customer's data" — it's *our customer's customers' personal data*. It is the company-ending failure mode. |
| **N2** | **The audit trail must be tamper-evident, not merely append-only** | We sell proof. If the trail can be silently edited — by an attacker, a rogue admin, or us — the product has no value. Append-only alone proves nothing about the past. |
| **N3** | **Every permission is enforced server-side** | The React role guards are UX. Anything enforced only in the client is not enforced. |
| **N4** | **Consent is bound to a notice version** | "She consented" is worthless without *to which version of which notice, in which language, how, and when*. Versioning is a data-model requirement, not a feature. |
| **N5** | **Machines are first-class callers** | The value is the customer's other systems asking "do I have consent right now?" before they process. The API is the product; the admin UI is the console. |

---

## 2. Stack, and why

| Choice | Why this one |
| --- | --- |
| **Python 3.12 + FastAPI** | The repo already runs FastAPI (the DSAR gateway) and Fides itself is Python. One language for the team, and we can call Fides internals directly if we ever need to. Async, typed, OpenAPI for free. |
| **PostgreSQL 16** | We need row-level security, `jsonb`, partitioning and real transactional guarantees. RLS is the reason — see §3. |
| **SQLAlchemy 2.0 async + `async_sessionmaker`** | Current best practice for new projects; typed ORM with an escape hatch to raw SQL for the joins that deserve it. |
| **Alembic from commit one** | Hand-altering a production database is how compliance data gets corrupted. Every schema change is a reviewed migration. |
| **Argon2id** | For passwords *and* refresh tokens *and* API keys. Current recommendation over bcrypt/PBKDF2. |
| **pyproject.toml** | Standard packaging; works with pip and uv. |
| **pytest + a real Postgres** | RLS and triggers cannot be tested against SQLite. Tests run against an actual Postgres or they test nothing. |

Deliberately **not** chosen yet: a message broker (Postgres-backed job table is
enough until it isn't), an ORM-level tenant plugin (explicit is better), GraphQL.

---

## 3. Multi-tenancy — shared schema + Postgres RLS

**Decision: one schema, `tenant_id` on every tenant-scoped table, and PostgreSQL
Row-Level Security as a second, independent enforcement layer.**

Three layers of defence, deliberately redundant:

```
1. Application  services always filter by the request's tenant
2. DATABASE     RLS policies append the tenant filter to every query,
                whatever the application forgot            ← the guarantee
3. Tests        a cross-tenant isolation test that must fail loudly
```

Why RLS and not just careful coding: application filtering is *a convention
developers must follow*. RLS is *a constraint the database enforces*. One
forgotten `WHERE tenant_id = ?` in one query is a cross-customer breach; with RLS
that query returns zero rows instead. Given N1, convention is not good enough.

Why not schema-per-tenant or database-per-tenant: both give stronger isolation
and much worse operations — migrations across thousands of schemas, connection
pool explosion, painful cross-tenant analytics. Shared schema + RLS is the
industry default for this stage, and the migration path to database-per-tenant
for a large enterprise customer stays open because `tenant_id` is already the
partition key everywhere.

### How it works mechanically

**Two database roles. This is the part people get wrong.**

| Role | Used for | RLS applies? |
| --- | --- | --- |
| `datashield_owner` | Alembic migrations. Owns the tables. | No — table owners bypass RLS |
| `datashield_app` | **The application connects as this.** Not the owner, no `BYPASSRLS`. | **Yes** |

RLS does not apply to superusers or table owners. If the app connects as the
owner (the default lazy setup), every policy you write is decorative. So the app
gets its own restricted role, and there is a test asserting it cannot bypass.

**Per-request tenant context** via a Postgres session variable:

```sql
-- set once per request, inside the transaction
SET LOCAL app.tenant_id = '<uuid>';

-- every tenant-scoped table carries
CREATE POLICY tenant_isolation ON consents
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
```

`SET LOCAL` scopes to the transaction, so a pooled connection cannot leak context
into the next request. If the variable is unset, `current_setting(..., true)`
returns NULL and the policy matches nothing — **failing closed**, which is the
correct direction to fail.

---

## 4. Authentication — two paths

### 4a. Humans (the admin console and user portal)

- **Passwords**: Argon2id, per-user salt, tuned cost. Never reversible.
- **Access token**: JWT, short-lived (15 min), carries `sub`, `tenant_id`, `role`,
  `jti`. Sent in the `Authorization` header.
- **Refresh token**: opaque random, **stored hashed** (Argon2id), delivered in an
  `HttpOnly; Secure; SameSite=Strict` cookie. Never in localStorage — anything
  JavaScript can read, an XSS payload can steal. (The prototype used
  localStorage; that was a demo shortcut and it is being removed.)
- **Rotation, single use**: every refresh mints a new token and consumes the old
  one. Each token carries a `family_id`; **presenting an already-used token
  revokes the entire family**, because reuse means the token leaked.
- **MFA**: TOTP, enforced per user and per tenant policy.
- **SSO**: OIDC, per tenant — deferred to Phase 10, but the user model reserves
  `external_idp_subject` so it doesn't need a migration later.

### 4b. Machines (the customer's own systems)

API keys, because that is what a backend service integration actually wants.

```
ds_live_7f3a2b...        ← shown ONCE at creation
└──┬──┘└─┬─┘└────┬─────┘
   │     │       └── 32 bytes urlsafe random — hashed with Argon2id, never stored
   │     └────────── environment: live / test
   └──────────────── fixed prefix, so a leaked key is greppable in logs and repos
```

- Only the **hash** and the **prefix** are stored. We cannot show a key twice.
- **Scopes** per key (`consent:read`, `consent:write`, `dsar:write`) — least
  privilege, so a key embedded in a marketing service can't erase anybody.
- Rotation and revocation are first-class; `last_used_at` surfaces dead keys.
- Rate limits per key.
- **Idempotency keys** on mutating public endpoints, so a customer's retry
  doesn't double-record a consent.

---

## 5. The audit trail — tamper-*evident*, not just append-only

This is the core asset (N2), so it gets the most engineering.

**Append-only is not tamper-evident.** An append-only table where someone with DB
access rewrites a row leaves no trace. So: an **HMAC-SHA256 hash chain**.

```
entry N:  hash = HMAC(key, canonical_json(entry_N) || hash_of_entry_N-1)
```

Four properties, each earned by a specific mechanism:

| Property | Mechanism |
| --- | --- |
| Order and completeness provable | Per-tenant monotonic `seq` + `prev_hash` linking. Remove or reorder an entry and every later hash fails. |
| Content changes detectable | The row's own fields are inside the HMAC. |
| **Forgery requires a secret** | **HMAC, not a bare SHA-256.** With a plain hash, anyone who can write rows can recompute the whole chain and cover their tracks. With HMAC they also need the signing key, which lives in the secret manager, not the database. |
| Deletion/mutation blocked at the DB | `REVOKE UPDATE, DELETE` from `datashield_app` **and** a trigger that raises. Belt and braces: grants stop the app, the trigger stops anyone using the app's connection. |

Plus:
- **Concurrency**: writes take a per-tenant Postgres advisory lock so two
  concurrent events can't claim the same `seq` or chain off the same `prev_hash`.
- **Verification endpoint** that walks the chain and reports the first break.
- **Periodic anchoring** (Phase 10): sign the head hash on a schedule and store it
  in WORM storage (S3 Object Lock), so even someone who obtains the HMAC key
  can't rewrite history that has already been anchored externally.
- Partitioned by month, because this table only grows.

**Everything that changes state writes here.** There is no code path that mutates
a consent, a request, a role or a policy without an audit entry — enforced by
putting the write inside the service layer transaction, not left to the caller.

---

## 6. Data model

Grouped by delivery phase. Every table below except `tenants` carries
`tenant_id`, an RLS policy, and `created_at`/`updated_at` in UTC.

**Phase 1 — identity**
```
tenants            slug, name, legal_name, grievance officer, default_language,
                   dsar_sla_days, grievance_sla_days, status
users              tenant_id, email, password_hash, full_name, role, mfa_*,
                   is_active, last_login_at        UNIQUE(tenant_id, email)
refresh_tokens     user_id, family_id, token_hash, expires_at, used_at,
                   revoked_at, ip, user_agent
api_keys           tenant_id, name, prefix, key_hash, scopes[], environment,
                   last_used_at, expires_at, revoked_at
```

**Phase 2 — evidence**
```
audit_events       tenant_id, seq, actor_type, actor_id, action, entity_type,
                   entity_id, payload jsonb, ip, user_agent,
                   prev_hash, hash          INSERT-ONLY, partitioned by month
```

**Phase 3 — consent (the domain core)**
```
purposes           key, name, category, is_mandatory, legal_basis,
                   retention_days, is_active
notices            purpose_id, version, language, content, data_collected,
                   user_rights, withdrawal_policy, published_at
                   UNIQUE(tenant_id, purpose_id, version, language)   ← N4
data_principals    external_id, email, phone, is_minor, guardian_email,
                   verified_at
consents           principal_id, purpose_id, notice_id, status, given_at,
                   withdrawn_at, expires_at, language, method, source
```
Consent *history* is not a table — it is a query over `audit_events`. One source
of truth; a history that can disagree with the audit trail is a liability.

**Phase 4 — public API**: `idempotency_keys`, `api_request_log`
**Phase 5 — DSAR**: `dsar_requests` (+ `engine_ref` linking to a Fides privacy request)
**Phase 6 — grievances**: `grievances`
**Phase 7 — retention**: `retention_policies`, `purge_runs`
**Phase 8 — notifications**: `notification_templates`, `notifications`
**Phase 9 — breach**: `breaches`

---

## 7. API surface

```
/health                     liveness + readiness (unauthenticated)
/v1/auth/*                  login, refresh, logout, me, mfa
/v1/admin/*                 the console: users, api-keys, purposes, notices,
                            dsar, grievances, retention, breaches, reports, audit
/v1/portal/*                the Data Principal's own view of their own data
/v1/public/*                API-key authenticated, for customer systems:
                            consent check / collect / withdraw, dsar submit
```

Conventions: `/v1` in the path from day one · cursor pagination · RFC 7807
problem+json errors · `X-Request-Id` on every response · idempotency on public
writes · 100% of routes carry an explicit permission dependency (N3).

---

## 8. Project layout

```
backend/
  pyproject.toml
  alembic.ini
  migrations/                 Alembic revisions (incl. RLS policies + grants)
  app/
    main.py                   app factory, middleware, router mounting
    core/
      config.py               pydantic-settings, 12-factor, fails fast
      security.py             argon2, JWT, API-key generation
      logging.py              structured JSON logs, request ids, PII-free
      errors.py               problem+json handlers
      permissions.py          the role → capability matrix (server-side)
    db/
      session.py              async engine, sessionmaker, per-request tenant ctx
      base.py                 declarative base, UUID/timestamp mixins
    models/                   SQLAlchemy models, one file per aggregate
    schemas/                  Pydantic request/response — never the ORM models
    services/                 business logic; the only place that writes audit
    api/
      deps.py                 auth, tenant, permission dependencies
      v1/                     routers, thin: parse → call service → serialise
  tests/
    conftest.py               real Postgres, per-test transaction rollback
    test_isolation.py         RLS cross-tenant — the most important test here
    test_auth.py              rotation, reuse detection, lockout
    test_audit_chain.py       verification + tamper detection + append-only
```

Layering rule: **routers do not touch the database and services do not touch
HTTP.** A router parses, calls a service, serialises. Business rules live in
services so they're testable without a client and reusable from a background job.

---

## 9. Delivery phases

| Phase | Content | State |
| --- | --- | --- |
| **0** | Skeleton: config, logging, errors, health, Docker, DB session, Alembic | **building now** |
| **1** | Tenancy + RLS + human auth + API keys | **building now** |
| **2** | Audit chain + verification + append-only enforcement | **building now** |
| 3 | Purposes, versioned notices, principals, consent lifecycle | next |
| 4 | Public API: consent check/collect, API-key auth, rate limits, idempotency | next |
| 5 | DSAR workflow + bridge to the Fides engine already in this repo | |
| 6 | Grievances + SLA escalation jobs | |
| 7 | Retention policies + purge execution | |
| 8 | Notifications (email/SMS providers, per-tenant templates) | |
| 9 | Breach register, reports, server-side PDF | |
| 10 | Hardening: OIDC SSO, WORM anchoring, field encryption, observability, load tests | |

Phases 0–2 first because nothing else is safe to build on top of a backend
without tenancy, auth and evidence. They are the spine.

---

## 10. Security posture (tracked, not aspirational)

- [x] Secrets from environment / secret manager, never in code
- [x] Argon2id for every credential at rest
- [x] App DB role cannot bypass RLS, cannot UPDATE/DELETE audit rows
- [x] Fail-closed tenant context
- [x] Server-side permission checks on every route
- [ ] TLS termination + HSTS (deployment)
- [ ] Rate limiting (Phase 4)
- [ ] Field-level encryption for principal PII (Phase 10)
- [ ] Dependency + container scanning in CI
- [ ] Pen test before first paying customer
- [ ] SOC 2 Type II — start early, it gates enterprise deals

## 11. Explicitly out of scope for now

Billing, self-serve signup, GraphQL, real-time websockets, on-prem deployment,
data residency beyond a single region. Each is a real requirement later; none
should shape the spine today.

---

## References

Grounding for the decisions above:

- [AWS: multi-tenant data isolation with PostgreSQL RLS](https://aws.amazon.com/blogs/database/multi-tenant-data-isolation-with-postgresql-row-level-security)
- [Shipping multi-tenant SaaS using Postgres RLS](https://www.thenile.dev/blog/multi-tenant-rls)
- [Mastering PostgreSQL RLS for multi-tenancy](https://ricofritzsche.me/mastering-postgresql-row-level-security-rls-for-rock-solid-multi-tenancy/)
- [fastapi-best-practices (zhanymkanov)](https://github.com/zhanymkanov/fastapi-best-practices)
- [Immutable audit log with HMAC hash chaining](https://tracehold.ai/blog/immutable-audit-log-hmac-hash-chain/)
- [Compliance by design: tamper-proof audit logs](https://mattermost.com/blog/compliance-by-design-18-tips-to-implement-tamper-proof-audit-logs/)
- [B2B SaaS authentication checklist 2026](https://securityboulevard.com/2026/05/user-authentication-best-practices-for-b2b-saas-in-2026-a-security-engineers-checklist/)
- [API key management best practices](https://oneuptime.com/blog/post/2026-02-20-api-key-management-best-practices/view)
