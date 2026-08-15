# Build brief — `dsar_workflow` (DSAR persistence & triage queue)

> Paste this whole file as the opening prompt. Fill the `[DECIDE]` block first.

**Size: ~1 week.** Backend Phase 5.

---

## 1. What already exists, and what is wrong

**The DSAR engine genuinely works.** One privacy request fans out across
app-postgres, app-mongo, app-mysql and Zoho CRM, returns what it found, and on
erasure masks the identifying fields in all of them. `./scripts/acceptance.sh`
proves it end to end on every run.

What is missing is **the record**. Today:

- A submitted request is remembered in the **browser's `localStorage`**
  (`frontend/src/api/consent.js` → `persistDsar`). That was a deliberate stopgap,
  documented as such. It means a request is invisible to the DPO, invisible on
  another device, and gone if the user clears their browser.
- `/admin/dsar` (`DSARQueue.jsx`) reads mock rows. Its approve / reject /
  reassign / "prepare export" controls are locked by `previewLock`, because there
  is no backend behind them.
- The gateway at `:8000` has `POST /dsar` and `GET /dsar/{id}` but **no
  list-by-identity endpoint**, which is why the browser had to remember ids.

`AuditAction.DSAR_SUBMITTED`, `DSAR_STATUS_CHANGED` and `DSAR_COMPLETED` are
already reserved. Capabilities `DSAR_READ`, `DSAR_PROCESS` and `SELF_DSAR_WRITE`
already exist.

## 2. `[DECIDE]`

- `[DECIDE]` **Does the CMS backend call the Fides gateway, or does the frontend
  keep calling it directly?**
  **Recommendation: the backend calls it.** The frontend then talks to one API,
  the gateway can move behind internal-only ingress, and the request row and the
  engine call are written in the same transaction. It does mean the backend needs
  the gateway URL and network reach.

- `[DECIDE]` **Who may raise a DSAR for someone else?** A DPO acting on a phone
  request is a real workflow, but "staff can submit a request as any principal"
  is also how someone gets erased maliciously.
  **Recommendation: allow it under `DSAR_PROCESS`, and record the actor** — the
  audit entry must show it was staff-initiated, not principal-initiated.

- `[DECIDE]` **Correction requests.** The Fides engine has no correction action.
  Options: keep correction as a manual workflow (a task for the DPO with an
  evidence trail), or leave it `preview`.
  **Recommendation: manual workflow** — it is a real DPDP right and a queue item
  with an audit trail is honest; a hidden right is not.

## 3. Data model (migration `0005`)

```
dsar_requests
  tenant_id, principal_id -> data_principals(id)
  reference            human handle, e.g. DSAR-2026-0007, unique per tenant
  type                 access | erasure | correction
  status               received | verifying | in_progress | completed
                       | rejected | cancelled
  engine_ref           the Fides privacy_request id (pri_…), NULL for correction
  engine_status        last status polled from the engine
  submitted_at, deadline_at, resolved_at        all timestamptz
  verification_method  otp | digilocker | staff_verified | session
  verified_at
  requested_by_actor   principal | staff        ← who raised it
  rejection_reason
  correction_payload   jsonb, correction only
  package_available_until   access packages expire; see §5
```

```
dsar_events              append-only per-request timeline
  dsar_request_id, at, actor_type, actor_id, from_status, to_status, note
```

Rules:

- `deadline_at = submitted_at + tenants.dsar_sla_days` (already on the tenant,
  default 30). Compute it **server-side**; a client-supplied deadline is not a
  statutory deadline.
- `UNIQUE (tenant_id, reference)`.
- A **CHECK** that `status='completed'` implies `resolved_at IS NOT NULL`, and
  `status='rejected'` implies `rejection_reason IS NOT NULL`. A rejected request
  with no reason is not a record anyone can defend.
- `dsar_events` is **append-and-read** (`GRANT SELECT, INSERT` only), like the
  audit trail.

## 4. API

```
POST   /v1/dsar                     raise a request (SELF_DSAR_WRITE or DSAR_PROCESS)
GET    /v1/dsar                     the queue, filterable (DSAR_READ)
GET    /v1/dsar/mine                the caller's own requests (SELF_READ)
GET    /v1/dsar/{id}                one request + its timeline
PATCH  /v1/dsar/{id}/status         triage: advance, reject with a reason (DSAR_PROCESS)
POST   /v1/dsar/{id}/package        prepare/refresh the access package (DSAR_PROCESS)
GET    /v1/dsar/{id}/package        download it — see §5
```

Every status change writes `DSAR_STATUS_CHANGED` to the audit chain **and** a
`dsar_events` row. The two are not redundant: the chain is tamper-evident
evidence, the events table is the queryable timeline the UI renders.

## 5. The access package — treat it as the most sensitive object in the system

An access package is **one person's complete personal data in a single file**.

- Never serve it from a permanent public URL.
- Download requires an authenticated, capability-checked request, and **every
  download writes an audit entry** (who, when, which request).
- `package_available_until` defaults to 7 days; expired means gone, and the API
  says "expired" rather than 404, so the person knows it existed.
- Today it is a file on a shared volume read off disk by the gateway. Moving it
  to Blob with short-expiry SAS is the right end state and is out of scope here —
  but **do not widen the current exposure** while working on this.

## 6. Reconciling with the engine

- On `POST`, create the row, call the gateway, store `engine_ref`. If the gateway
  call fails, the row stays `received` with the failure in `dsar_events` — do not
  lose the request because a downstream was briefly down.
- Poll `GET /dsar/{engine_ref}` to refresh `engine_status`. A background job is
  better than polling on read, but **on-read refresh is acceptable for v1** as
  long as it is bounded and cannot hang the request.
- **Never let the engine's status silently overwrite a human decision.** If a DPO
  has rejected a request, an engine callback must not flip it back.

## 7. Frontend

- `/user/dsar` and `/user/dsar/status`: read from `/v1/dsar/mine`. **Delete the
  `localStorage` stopgap** in `src/api/consent.js` (`persistDsar`,
  `loadStoredDsar`, `dsarStoreKey`) and the comment explaining it — the reason it
  existed is now gone.
- `/admin/dsar`: the real queue. Unlock the triage controls. Rejection requires a
  reason (the DB enforces it; the form should too, with a better message).
- Keep the `SLACountdown` behaviour — green/amber/red/OVERDUE against
  `deadline_at`.
- The per-request timeline renders `dsar_events`.

## 8. Non-goals

- Do not rebuild the Fides engine or its connectors.
- Do not implement Blob storage migration (backlog).
- Do not build DSAR-related email — that is `03-notifications.md`. Leave a clean
  seam where a notification would be sent.

## 9. Tests

- deadline is `submitted_at + tenants.dsar_sla_days`, server-computed
- a client-supplied `deadline_at` is ignored
- rejecting without a reason fails (service *and* DB)
- completing sets `resolved_at`; the CHECK rejects the alternative
- every status change appears in both the audit chain and `dsar_events`
- `dsar_events` cannot be UPDATEd or DELETEd
- a package download writes an audit entry; an expired package says "expired"
- cross-tenant: tenant B cannot see, fetch or advance tenant A's requests
- a principal can see their own requests and not anyone else's
- an engine status update cannot overwrite a human rejection

## 10. Definition of done

- [ ] Requests survive a reload, a new device, and a cleared browser
- [ ] The DPO queue shows real requests with working triage
- [ ] `localStorage` stopgap deleted
- [ ] `dsar_workflow` flipped to `"live"` in `src/config/modules.js`
- [ ] If correction stays manual, say so in `MODULE_CAVEATS`
- [ ] `./scripts/acceptance.sh http://localhost:8090` still passes
- [ ] Browser run: submit → appears in the DPO queue → advance → complete →
      download the package → the whole path is in `/admin/audit`

## 11. House rules

RLS + `TENANT_SCOPED_TABLES` + policy test. Timezone-aware timestamps. Routers
thin, services hold rules. Audit every state change. Full list:
[README.md](README.md).
