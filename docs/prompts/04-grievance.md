# Build brief — `grievance` (redressal & the statutory escalation clock)

> **STATUS: DONE.** Built and live. How the three `[DECIDE]` items were settled,
> and where the build differs from this brief:
>
> * **Non-account-holders can file** — `POST /public/v1/grievance`, no credential.
>   NOT via a publishable key: those are capped at `consent:collect` by a CHECK
>   constraint, and widening that would trade a strong, testable property for
>   convenience. So the endpoint is unauthenticated, addressed by workspace slug
>   (already public), with an email round trip.
> * **Confirmation gates escalation, not filing.** A barrier in front of a
>   statutory right is a barrier, so the complaint is recorded and the deadline
>   starts running immediately. What confirmation unlocks is the ability to page a
>   Grievance Officer — waking one over an address nobody has proven they own turns
>   the statutory alarm into noise.
> * **Two throttles instead of stored client IPs**: one unconfirmed complaint per
>   address at a time, and a per-workspace hourly cap. Logging the IP of everyone
>   who files a privacy complaint, to protect the privacy complaint system, would
>   be a poor trade.
> * **Escalation notifies the officer and raises the item.** It does not contact
>   the Data Protection Board — unattended regulator contact stays a human
>   decision, and the flag is what prompts it.
> * **Officer name/email needed no answer.** Both `registration_service` and
>   `tenant_service` now default them to the first admin, so no workspace is ever
>   non-compliant by omission. `PUT /v1/grievances/officer` changes them, and a
>   cleared contact reports `published: false` so the screens can say so instead of
>   rendering a blank line where a statutory contact belongs.
>
> Not built, and visible in the module's caveat: **no attachments**, so a person
> cannot submit supporting documents; and **no scheduler**, so escalation is
> evaluated whenever the queue is read rather than overnight.
>
> One state the brief did not anticipate: the confirmation window (7 days) is
> shorter than the default escalation threshold (10 days), so an anonymous
> complaint that is never confirmed becomes permanently unconfirmable *and*
> unescalatable. It is counted separately as `confirmation_expired` rather than
> left in a growing pile a DPO believes is still in flight.

> Paste this whole file as the opening prompt. Fill the `[DECIDE]` block first.

**Size: ~1 week.** Depends on `03-notifications.md` — the escalation clock is
defined in terms of notifying someone.

---

## 1. Why this module is not optional

DPDP §13 gives every Data Principal the right to a grievance redressal mechanism,
and requires the Data Fiduciary to publish a **Grievance Officer**. A person must
exhaust this before approaching the Data Protection Board. A compliance product
without a working grievance queue is missing a statutory obligation, not a
feature.

## 2. What exists

- `/user/grievance` (`GrievanceForm.jsx`), `/user/grievance/status`, and
  `/admin/grievances` (`GrievanceQueue.jsx`) — all on mock data, controls locked.
- The tenant already carries `grievance_officer_name`, `grievance_officer_email`,
  `grievance_sla_days` (15) and `grievance_escalation_days` (10).
- Capabilities exist: `GRIEVANCE_READ`, `GRIEVANCE_PROCESS`,
  `GRIEVANCE_ESCALATE`, `SELF_GRIEVANCE_WRITE`. The `grievance_officer` **role**
  exists and its nav is already restricted to this queue.
- No `AuditAction` constants yet — add them (`grievance.submitted`,
  `grievance.assigned`, `grievance.escalated`, `grievance.resolved`,
  `grievance.reopened`).

## 3. `[DECIDE]`

- `[DECIDE]` **Can a non-account-holder file a grievance?** Someone whose data
  you hold may have no login. Making an account a precondition arguably defeats
  §13.
  **Recommendation: yes, via the public API with an email round trip to verify
  the address** — otherwise the queue fills with unverifiable complaints.

- `[DECIDE]` **What happens at escalation?** Options: notify the Grievance
  Officer, notify a named escalation contact, or flag for Board reporting.
  **Recommendation: notify the officer AND raise queue priority**, with Board
  reporting as a manual action a human takes — automatic regulator contact is not
  something software should do unattended.

- `[DECIDE]` **Officer name/email** — the four `[DECIDE]` items from the
  deployment brief include this. It is published to data principals, so it has to
  be a real monitored address.

## 4. Data model (next free migration number)

```
grievances
  tenant_id, principal_id (nullable — see the non-account-holder decision)
  reference               GRV-2026-0007, unique per tenant
  category                consent_violation | data_breach | dsar_delay
                          | inaccurate_data | other
  description             free text from a member of the public — see §7
  contact_email           for people with no account
  status                  open | acknowledged | in_progress | resolved
                          | rejected | reopened
  assigned_to             -> users(id), nullable
  related_dsar_id         -> dsar_requests(id), nullable
  submitted_at, acknowledged_at, resolved_at, deadline_at, escalated_at
  escalated               bool
  resolution_notes
  satisfaction_rating     1..5, nullable — the person rates the outcome
```

```
grievance_events          append-only timeline, same shape as dsar_events
```

Rules:

- `deadline_at = submitted_at + tenants.grievance_sla_days`, **server-computed**.
- Escalation threshold is `tenants.grievance_escalation_days`, per tenant, not a
  constant.
- CHECK: `resolved` implies `resolution_notes IS NOT NULL`. A resolved grievance
  with no record of the resolution is not a redressal mechanism.
- CHECK: a principal_id **or** a contact_email — a grievance nobody can be
  answered at is not actionable.
- `grievance_events` is append-and-read.

## 5. The escalation clock

This is the part that must be real, because it is the statutory bit.

- A scheduled job — reuse the same Postgres job-table pattern as notifications —
  finds grievances past `escalation_days` and not resolved, sets `escalated`,
  stamps `escalated_at`, notifies, and writes to the audit chain.
- **Evaluate against the clock at read time as well.** A nightly job leaves a
  window where an overdue grievance still displays as fine, and that window is
  exactly when a DPO is looking. The row is the record; the display must not lag
  it.
- Escalation is **idempotent** — running the job twice must not double-notify.

## 6. API

```
POST   /v1/grievances                     file one (SELF_GRIEVANCE_WRITE)
GET    /v1/grievances                     the queue (GRIEVANCE_READ)
GET    /v1/grievances/mine                the caller's own (SELF_READ)
GET    /v1/grievances/{id}                one + timeline
PATCH  /v1/grievances/{id}                assign, acknowledge, resolve, reject
POST   /v1/grievances/{id}/escalate       manual escalation (GRIEVANCE_ESCALATE)
POST   /v1/grievances/{id}/feedback       satisfaction rating (the filer only)
```

Plus, if the `[DECIDE]` allows public filing:
```
POST   /public/v1/grievance               publishable key or unauthenticated + verify
```

**A Grievance Officer must not be able to read anything else.** The role's nav is
already restricted; the routes must enforce it independently, because nav is
presentation.

## 7. Free text from the public is hostile input

`description` is written by anyone.

- Store it raw, render it escaped, everywhere: the queue, the email
  notification, the exported report, the PDF.
- Cap the length at the API, not just in the form.
- It will contain personal data about third parties ("your agent Ravi told
  me…"). That is unavoidable and worth a comment — it affects retention and any
  future export.

## 8. Frontend

- `/user/grievance`: real submission, with the reference shown on success.
- `/user/grievance/status`: real status, the deadline, and the satisfaction
  prompt once resolved.
- `/admin/grievances`: real queue. Unlock triage; resolution requires notes.
  Escalated items visibly distinct — this is the statutory risk indicator.
- The Grievance Officer's restricted view must be exercised by a real login in
  the browser check, not assumed.

## 9. Non-goals

- No SLA analytics dashboard (that is `06-reports.md`).
- No automatic Board reporting.
- No live chat or ticketing integration.

## 10. Tests

- deadline and escalation threshold both come from the tenant, not a constant
- resolving without notes fails at the service **and** the DB
- a grievance with neither principal nor contact email is rejected
- the escalation job is idempotent — running twice notifies once
- an overdue grievance reads as overdue **before** the job has run
- the timeline is append-only
- a `grievance_officer` role can reach the queue and gets 403 on consent, DSAR,
  audit and users
- a principal sees only their own grievances
- description containing markup is inert in every rendering path
- cross-tenant isolation

## 11. Definition of done

- [ ] File → acknowledge → assign → resolve → rate, end to end
- [ ] Escalation fires on a real clock and notifies
- [ ] `grievance` flipped to `"live"` in `src/config/modules.js`
- [ ] Grievance Officer restriction verified by a real login
- [ ] `./scripts/acceptance.sh http://localhost:8090` still passes

## 12. House rules

RLS + `TENANT_SCOPED_TABLES` + policy test. Timezone-aware timestamps. Audit
every state change. Full list: [README.md](README.md).
