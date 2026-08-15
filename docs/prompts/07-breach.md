# Build brief — `breach` (breach register & the DPDP notification duty)

> Paste this whole file as the opening prompt. Fill the `[DECIDE]` block first.

**Size: ~1 week.** Depends on `03-notifications.md`.

---

## 1. What the law actually requires

DPDP §8(6): on becoming aware of a personal data breach, a Data Fiduciary must
notify **both** the Data Protection Board **and every affected Data Principal**.
The DPDP Rules prescribe the form and timing — *without delay* to the Board on
becoming aware, with fuller particulars to follow.

Two things follow for the design:

1. **"Aware" is a timestamp someone will litigate.** The clock does not start when
   the breach happened, or when it was contained — it starts when the fiduciary
   became aware. Record it as its own field, editable only with a reason, and put
   every change in the audit chain.
2. **Notifying only the Board is not compliance.** The affected individuals are a
   separate, mandatory obligation, and the product must not let someone mark a
   breach "reported" having done only half of it.

## 2. What exists

`/admin/breaches` (`BreachManagement.jsx`) on mock data, controls locked.
`Capability.BREACH_MANAGE` exists. No `AuditAction` constants yet — add
`breach.recorded`, `breach.updated`, `breach.board_notified`,
`breach.principals_notified`, `breach.closed`.

The screen was built without a spec (the original brief listed it in the file
structure only), so treat its current shape as a sketch, not a requirement.

## 3. `[DECIDE]`

- `[DECIDE]` **How are affected principals identified?** By explicit list, by
  data category, by a query over consents, or by upload?
  **Recommendation: a saved query plus a reviewable list.** A DPO must be able to
  see and correct exactly who is about to be notified before anything is sent —
  notifying the wrong people about a breach is itself an incident.

- `[DECIDE]` **Does the product actually submit to the Board?** There is no
  general API for this today; it is a portal/form process.
  **Recommendation: no.** Generate the notification content and record that a
  human submitted it, with a reference. Automatically contacting a regulator
  unattended is not something this software should do.

- `[DECIDE]` **Can a breach ever be deleted?** A mistaken entry is possible.
  **Recommendation: no deletion — only `void` with a reason**, preserved. A
  register whose entries can vanish is not a register.

## 4. Data model (next free migration number)

```
breaches
  tenant_id, reference              BRE-2026-0003, unique per tenant
  title
  description
  severity                          low | medium | high | critical
  status                            draft | investigating | contained
                                    | notified | closed | void
  occurred_at                       nullable — often unknown
  discovered_at                     when we became aware  ← the statutory clock
  contained_at, closed_at
  categories_affected               text[] — matches the notice category vocabulary
  estimated_affected_count
  root_cause, remediation
  board_notified_at, board_reference
  principals_notified_at
  void_reason
```

```
breach_affected_principals         who was affected, and whether they were told
  breach_id, principal_id
  notification_id                  -> notifications(id), nullable
  notified_at
  UNIQUE (breach_id, principal_id)
```

```
breach_events                      append-only timeline
```

Rules:

- `discovered_at` is **NOT NULL** from the moment a breach leaves `draft`. It is
  the field the whole obligation hangs on.
- CHECK: `status='notified'` requires **both** `board_notified_at` **and**
  `principals_notified_at`. Half a notification is not a notification, and the
  schema should not let the UI pretend otherwise.
- CHECK: `status='void'` requires `void_reason`.
- CHECK: `status='closed'` requires `root_cause` and `remediation`. A closed
  breach with no cause recorded teaches nobody anything.
- `breach_events` is append-and-read.

## 5. The notification duty, done properly

- **Two distinct actions**, tracked separately: notify the Board (generate
  content, human submits, record the reference) and notify the principals (real
  sends via the notifications module, one row per person in
  `breach_affected_principals`).
- The principal notification is a **bulk send that must be resumable**. Ten
  thousand people, a provider rate limit, and a half-finished run is the normal
  case — `breach_affected_principals.notified_at` is what makes the second
  attempt safe.
- **Show progress honestly**: "4,812 of 10,000 notified" is the truth; a green
  tick at 48% is not.
- A breach cannot reach `closed` while affected principals remain un-notified,
  unless a documented exemption is recorded.

## 6. Time pressure is the point

The interface should make lateness impossible to miss:

- Time since `discovered_at`, prominently, on the list and the detail.
- An un-notified breach past the threshold is the loudest thing on the admin
  dashboard — louder than an overdue DSAR.
- Reuse the `SLACountdown` behaviour (green / amber / red / OVERDUE) rather than
  inventing a second visual language for lateness.

## 7. API

```
GET    /v1/breaches
POST   /v1/breaches                       record one (starts draft)
GET    /v1/breaches/{id}
PATCH  /v1/breaches/{id}                  update; changing discovered_at needs a reason
POST   /v1/breaches/{id}/affected         attach principals (query result or list)
POST   /v1/breaches/{id}/notify-board     records the human submission + reference
POST   /v1/breaches/{id}/notify-principals   starts/resumes the bulk send
POST   /v1/breaches/{id}/close
POST   /v1/breaches/{id}/void             requires a reason
```

All under `BREACH_MANAGE`. Breach detail contains the most sensitive combination
in the product — who was affected by what — so **no wider read scope**, and
consider whether even an Auditor should see the affected list or only the counts.

## 8. Non-goals

- No automated Board submission.
- No breach *detection*. This is a register, not a SIEM.
- No forensic tooling.

## 9. Tests

- `status='notified'` is rejected with only one of the two notifications
- `closed` requires root cause and remediation; `void` requires a reason
- changing `discovered_at` records the old value and a reason in the chain
- a bulk notify run is resumable and never notifies the same principal twice
- progress reporting matches `breach_affected_principals` exactly
- a breach cannot close with un-notified principals and no exemption
- `breach_events` cannot be UPDATEd or DELETEd
- cross-tenant isolation on all three tables
- a role without `BREACH_MANAGE` gets 403 on every route

## 10. Definition of done

- [ ] Record → investigate → attach affected → notify Board → notify principals
      → close, end to end
- [ ] Bulk notification resumable and honestly reported
- [ ] `breach` flipped to `"live"` in `src/config/modules.js`
- [ ] Board submission clearly labelled as human-performed, not automated
- [ ] `./scripts/acceptance.sh http://localhost:8090` still passes

## 11. House rules

RLS + `TENANT_SCOPED_TABLES` + policy test. Timezone-aware timestamps. Audit
every state change. Full list: [README.md](README.md).
