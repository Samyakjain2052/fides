# Build prompts — the eight remaining modules

One file per preview module. Each is **self-contained**: paste the whole file as
the opening prompt for a coding agent, fill the `[DECIDE]` blocks first, and it
has everything it needs.

They are numbered in the order I would build them. That order is not arbitrary —
see "Why this order" below.

| # | File | Module key | Rough size |
| --- | --- | --- | --- |
| 01 | [consent-surfaces.md](01-consent-surfaces.md) | `consent_surfaces` | **DONE** — live |
| 02 | [dsar-workflow.md](02-dsar-workflow.md) | `dsar_workflow` | **DONE** — live |
| 03 | [notifications.md](03-notifications.md) | `notifications` | **DONE** — live |
| 04 | [grievance.md](04-grievance.md) | `grievance` | **DONE** — live |
| 05 | [retention.md](05-retention.md) | `retention` | **DONE** — live |
| 06 | [reports.md](06-reports.md) | `reports` | **DONE** — live |
| 07 | [breach.md](07-breach.md) | `breach` | ~1 week |
| 08 | [users.md](08-users.md) | `users` | 2–3 days |

## Why this order

**01 is done** — the banners collect through the publishable-key API and the
module is live. Its brief is kept as the record of what was built and why.

**03 before 04 and 05.** Grievances have a statutory escalation clock and
retention has a "notify N days before purge" setting. Both are *specified* in
terms of sending someone a message. Building them against a notifications module
that does not exist means building them twice.

**06 and 07 late** because reports and breach notifications are only worth
anything once there is real consent, DSAR and grievance data to report on.

**08 whenever.** It has no dependencies and is the smallest.

---

## House rules (every prompt repeats these — they are not optional)

These are the patterns this codebase already uses. Do not build parallel versions.

### Tenancy and RLS
- Every tenant-scoped table gets `tenant_id`, a row-level security policy in the
  migration, `FORCE ROW LEVEL SECURITY`, and an entry in `TENANT_SCOPED_TABLES`
  in `app/models/__init__.py`. A test asserts the policy exists.
- **Any lookup that happens before tenant context exists will match nothing.**
  This codebase has hit that bug three times (refresh tokens, secret API keys,
  publishable keys). The fix each time was to carry the tenant in the credential
  and bind context *first*. If you write a pre-context lookup, expect it to
  return zero rows and design accordingly.

### Timestamps
`DateTime(timezone=True)`, always. `TimestampMixin` in `app/db/base.py` is
already correct — a naive column against a `timestamptz` comparison 500s, and it
is invisible until something compares against an aware datetime.

### The audit chain
- Every state change writes an entry via `audit_service.record(...)`.
- Add new action constants to `AuditAction` in `app/models/audit.py`. Several are
  already reserved (`DSAR_STATUS_CHANGED`, `CONSENT_EXPIRED`, …) — check before
  inventing one.
- New `actor_type` values need the CHECK constraint on `audit_events` widened in
  a migration. `user`, `api_key`, `publishable_key`, `system`, `data_principal`
  exist today.
- Evidence tables are **append-and-read**: `GRANT SELECT, INSERT` only, no
  `UPDATE`/`DELETE`. Retention trimming runs as the owner in a scheduled job.

### Layering
Routers parse and serialise. Services hold the rules. **Routers never touch the
database; services never touch HTTP.** No route writes a `tenant_id` filter —
RLS applies it, so a forgotten `WHERE` returns nothing rather than everything.

### Permissions
Capabilities already exist in `app/core/permissions.py` for every module below
(`GRIEVANCE_PROCESS`, `RETENTION_MANAGE`, `BREACH_MANAGE`, `REPORT_GENERATE`,
`NOTIFICATION_MANAGE`, `USER_MANAGE`, …). Use them; do not add near-duplicates.
Every route declares one:
```python
current: Annotated[CurrentUser, Depends(require(Capability.RETENTION_MANAGE))]
```

### Migrations
Sequential, reviewed, never hand-edited after being applied. The chain is at
`0004_publishable_keys`; claim the next free number. If two prompts run in
parallel, one rebases.

### Tests
Run against a **real PostgreSQL** in the container — `docker compose run --rm
cms-test`, against **datashield_test** — a different database from the one the
app serves, so a test run cannot destroy your demo data. RLS, triggers and
advisory locks do not exist outside Postgres, and a suite that stubs the database
passes while the product leaks. Currently **110 tests**; every prompt below adds
to that number and none may reduce it.

### Definition of done, for all eight
1. Backend built, migrated, tested.
2. The module's screens read and write the real API — no `src/api/index.js` mock.
3. Flip the module key to `"live"` in `frontend/src/config/modules.js`. The
   preview banner disappears on its own, because the UI derives it from that one
   file.
4. `npm run build` passes — it runs `scripts/check-preview-locks.mjs`, which
   fails the build if a preview module still has an unlocked mutating control.
5. `./scripts/acceptance.sh http://localhost:8090` still passes.
6. If the module is live but has an exception, add it to `MODULE_CAVEATS` rather
   than letting the green state overclaim.

### Honesty rule
This product is sold on the accuracy of its records. **Nothing on screen may
imply a capability that does not exist**, no number may be fabricated, and no
control may silently no-op. If part of a module cannot be finished, leave it
`preview` and say so in `MODULE_ROADMAP` — a smaller honest surface beats a
larger misleading one.
