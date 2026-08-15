# Build brief — `retention` (policies and the purge executor)

> Paste this whole file as the opening prompt. Fill the `[DECIDE]` block first.

**Size: 1–2 weeks.** The largest remaining module, and **the most dangerous** —
it is the only one that deletes customer data. Depends on
`03-notifications.md` for the pre-purge warning.

---

## 1. Read this before writing any code

Every other module in this product **records** things. This one **destroys**
them, irreversibly, on a schedule, without a human in the loop.

A bug in the consent module produces a wrong record someone can correct. A bug
here deletes a customer's production data and there is nothing to correct it
from. Build it accordingly:

- **Dry run first, always.** Every policy must be runnable in a mode that reports
  exactly what it *would* delete without deleting it, and that must be the
  default in the UI.
- **Nothing purges without an explicit human action or an explicit per-policy
  opt-in to automation.** `auto_delete` defaults to `false`.
- **Every purge is preceded by a warning notification** at `notify_days`.
- **Every purge writes a receipt** — what was deleted, how many rows, under which
  policy, at whose instruction — into the audit chain. "We deleted it because the
  policy said so" is only defensible if the policy and the run are both on record.

## 2. What exists

`/admin/retention` (`DataRetentionPolicy.jsx`) renders mock policies with locked
"Save" and "Run purge" buttons. `Capability.RETENTION_MANAGE` exists.
`purposes.retention_days` already exists and already drives consent expiry.
No `AuditAction` constants yet — add `retention.policy_changed`,
`retention.purge_started`, `retention.purge_completed`, `retention.purge_failed`.

## 3. `[DECIDE]`

- `[DECIDE]` **What does "purge" mean for a given data category?** Hard delete,
  or anonymise in place? The DSAR erasure path already **masks** (nulls the
  identifiers, keeps the row) rather than deleting, so aggregate reporting
  survives.
  **Recommendation: mirror the DSAR behaviour — mask by default, hard delete only
  where a policy explicitly says so.** Two different meanings of "erased" in one
  product is a support nightmare and an audit contradiction.

- `[DECIDE]` **Which stores does a purge reach?** The CMS's own tables only, or
  also the demo datastores via the Fides engine?
  **Recommendation: CMS tables in v1**, with the engine path designed for but not
  built. Purging a customer's production systems on a timer is a much larger
  promise than this module can honestly make yet.

- `[DECIDE]` **Exemptions.** RBI/tax rules routinely require keeping records past
  a retention period. The mock already has an `exemption` field.
  **Recommendation: exemptions are structured, not free text** — a reason code
  plus a statute reference plus an expiry, so "why is this still here?" has an
  answer a regulator accepts.

## 4. Data model (next free migration number)

```
retention_policies
  tenant_id, name
  data_category            matches the purpose/notice category vocabulary
  retention_days
  action                   mask | delete          ← see the DECIDE above
  auto_delete              bool, DEFAULT FALSE
  notify_days              warn this many days before
  exemption_code           statutory | legal_hold | dispute | none
  exemption_reference      e.g. "RBI KYC Master Direction 2016 §12"
  exemption_expires_at
  is_active
  last_run_at
```

```
purge_runs                 append-and-read. The receipt.
  tenant_id, policy_id
  mode                     dry_run | live
  started_at, finished_at
  status                   running | completed | failed | cancelled
  initiated_by             -> users(id), NULL for scheduled
  candidates_found, rows_affected
  scope_summary            jsonb: per-table counts
  error
```

```
purge_run_items            what was actually touched, for the receipt
  purge_run_id, table_name, entity_id, action_taken
```

Rules:

- `purge_runs` and `purge_run_items` are **append-and-read**. A purge receipt the
  application can rewrite is worthless.
- CHECK: `auto_delete` true requires `notify_days >= 1`. Automatic destruction
  with no warning is not something the schema should permit.
- CHECK: an exemption code other than `none` requires a reference.

## 5. The executor

- Reuse the Postgres job-table pattern (`FOR UPDATE SKIP LOCKED`).
- **Batch, with a hard cap per run.** An unbounded `DELETE` over a large table
  holds locks and takes the product down.
- **Idempotent and resumable.** A run that dies halfway must be safe to re-run;
  `purge_run_items` is what makes that checkable.
- **Never purge a row under legal hold**, and never purge something with an
  active unresolved DSAR or grievance attached. Check that *inside* the
  transaction, not before it.
- A dry run and a live run must share **one** candidate-selection code path. Two
  implementations that can disagree is how a dry run reports 4 rows and the live
  run deletes 400.

## 6. API

```
GET    /v1/retention/policies
POST   /v1/retention/policies
PATCH  /v1/retention/policies/{id}
POST   /v1/retention/policies/{id}/preview     dry run, returns the candidate set
POST   /v1/retention/policies/{id}/run         live — requires explicit confirmation
GET    /v1/retention/runs                      history
GET    /v1/retention/runs/{id}                 the receipt, with items
```

All under `RETENTION_MANAGE`. The live run endpoint should require an explicit
confirmation field in the body (`"confirm": "<policy name>"`) — the same reason
`rm -rf` prompts.

## 7. Frontend

- Policies list with the exemption shown as structured data, not a text blob.
- **Preview is the primary action.** "Run purge" is secondary, destructive-styled,
  behind the existing `ConfirmModal` with the consequences spelled out and the
  candidate count from a fresh preview.
- Run history with receipts, downloadable.
- If a policy is `auto_delete`, say so prominently in the list. A policy that
  deletes on a timer should not look like one that does not.

## 8. Non-goals

- No purging of the demo datastores via Fides (per the `[DECIDE]`).
- No cross-tenant or platform-wide purge tooling.
- Do not touch the DSAR erasure path — it already works and is tested.

## 9. Tests

These matter more than usual. A false pass here loses data.

- a dry run deletes **nothing** — assert row counts before and after
- dry run and live run select an identical candidate set
- a row under legal hold is never purged, even when past retention
- a row with an open DSAR or grievance is never purged
- `auto_delete` with `notify_days = 0` is rejected by the DB
- an exemption code without a reference is rejected by the DB
- a run that fails halfway leaves a `failed` receipt and is safe to re-run
- `purge_runs` and `purge_run_items` cannot be UPDATEd or DELETEd
- the batch cap holds on a large candidate set
- concurrent runs of the same policy do not double-process
- cross-tenant: a policy in tenant A can never select a row in tenant B —
  **test this explicitly and hard**; it is the worst possible failure in this
  module

## 10. Definition of done

- [ ] Preview → warn → purge → receipt, end to end
- [ ] Receipts are append-only and show exactly what happened
- [ ] `retention` flipped to `"live"` in `src/config/modules.js`
- [ ] If the engine path is deferred, say so in `MODULE_CAVEATS` — a retention
      module that only reaches our own tables must not imply it reaches theirs
- [ ] `./scripts/acceptance.sh http://localhost:8090` still passes

## 11. House rules

RLS + `TENANT_SCOPED_TABLES` + policy test. Timezone-aware timestamps. Audit
every state change. Full list: [README.md](README.md).
