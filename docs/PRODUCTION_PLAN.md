# From demo to production-grade

[DEPLOYMENT_PLAN.md](DEPLOYMENT_PLAN.md) covers getting the current thing onto
Azure as a *demo*. This covers the different and larger question: what has to be
true before a paying customer can run their DPDP compliance on this.

---

## 1. The gap, stated plainly

The demo is honest about what it does — the modules that render sample data say
so, in a banner derived from one file. That honesty is now the specification:
**every preview banner in the app is a line item in this plan.** When the last
one disappears, the product is feature-complete.

`frontend/src/config/modules.js` is the single source of truth for this table.
If the two ever disagree, the file is right and this document is stale.

| Module | Today | What is still missing |
| --- | --- | --- |
| Accounts & sign-in | **live** | — |
| Data requests | **live** | correction runs on sample data; identity check is simulated |
| DSAR triage queue | **live** | — |
| Consent management | **live** | — |
| Public consent/cookie banners | **live** | withdrawal from a banner (collect-only by design) |
| Retention & purge | **live** | reaches this product's tables, not connected systems; purges stay manual by design |
| Audit trail | **live** | cannot detect truncation of the newest entries (needs external anchoring) |
| Notifications | **live** | SMS modelled but unimplemented; console provider ships by default |
| Grievance redressal | **live** | no attachments; unconfirmed anonymous filings need picking up by hand |
| Reports | **live** | no PDF; nothing signed (chain-hash anchor only) |
| Breach management | **live** | no Board API (human submits); no breach detection |
| Users & roles | **live** | no SSO; no MFA enrolment flow |

Two things are worth separating, because they fail differently:

- **Product completeness** — the table above. Without it there is no product,
  only a login page and a DSAR engine.
- **Platform readiness** — deployment, HA, backups, observability, legal
  surface. Without it there is a product nobody can safely run.

Both are required. Product completeness comes first, because deploying an
incomplete product to a highly-available, WAF-protected, PITR-backed
environment does not make it more complete — it makes it expensive.

---

## 2. Order of work, and why this order

```
  3  Consent core        ← DONE. The product. Everything below assumes it.
  4  Public API          ← DONE. The thing customers integrate against (N5)
  5  DSAR workflow       ← DONE. Persist requests; bridge to the Fides engine
  7  Retention + purge   ← DONE. Built before notifications; see the note below
  8  Notifications       ← DONE. Moved up from 8; the pre-purge seam closed here
  6  Grievances          ← DONE. Public filing, escalation clock, officer contact
  9  Reports + breach    ← DONE. Both.
  S  Scheduler           ← DONE. Escalation, notification retries and pre-purge
                             warnings now run unattended. Purges deliberately
                             do not.
 10  Hardening           ← NEXT. SSO, field encryption, WORM anchoring, load tests
  8  Users & roles       ← DONE. Every module brief is now complete.
```

**The one reordering from ARCHITECTURE.md §9:** notifications moved from 8 to
just after 5. Grievances have a statutory escalation clock and retention has a
"notify N days before purge" setting — both are *specified* in terms of sending
someone a message, and building them against a notifications module that does
not exist means building them twice.

**What actually happened, and it is worth recording:** retention was built
*before* notifications anyway, and shipped with its notice period stored,
constrained (`auto_delete` is refused without one) and honoured by the UI — but
with nothing able to send it. That gap was visible in the module's own caveat for
two phases. Notifications then closed it with one function,
`retention_service.warn_upcoming`. So the reordering argument was right in
principle and the cost of ignoring it was one seam, not a rebuild — because the
seam was designed in rather than discovered.

### Phase 3 — Consent core (the domain)

Four tables, already specified in ARCHITECTURE.md §7:

```
purposes         key, name, category, is_mandatory, legal_basis, retention_days
notices          purpose_id, version, language, content, data_collected,
                 user_rights, withdrawal_policy, published_at
                 UNIQUE(tenant_id, purpose_id, version, language)
data_principals  external_id, email, phone, is_minor, guardian_email, verified_at
consents         principal_id, purpose_id, notice_id, status, given_at,
                 withdrawn_at, expires_at, language, method, source
```

The rules that make this a compliance product rather than a CRUD app:

1. **Consent binds to a notice version, not a purpose** (non-negotiable N4).
   "She consented" is worthless without *to which version of which text, in
   which language, how, and when*. `consents.notice_id` is not nullable.
2. **A published notice is immutable.** Editing the text people agreed to is
   the single most damaging thing this system could permit. Changing a
   published notice creates version N+1; existing consents keep pointing at
   the version actually shown. Enforced by a database trigger, not by a service
   method — the same reasoning as the append-only audit trail.
3. **Consent history is a query over `audit_events`**, not its own table. A
   history that can disagree with the audit trail is a liability.
4. **Withdrawal is as easy as granting** (DPDP §6(4)) — same number of steps,
   and the API makes it a single call.
5. **Nothing defaults to granted.** No code path creates a consent in `active`
   without an explicit act, and the tests assert it.

### Phase 4 — Public API — **DONE**

```
GET  /public/v1/purposes        discovery, so integrators do not hardcode keys
GET  /public/v1/consent/check   "do I have consent right now?"
POST /public/v1/consent         collect or withdraw, idempotent
```

Built with per-key scopes (a marketing key can read and cannot write), a fixed
rate-limit window counted in Postgres, `Idempotency-Key` replay, and an
append-only request log. Mounted at its own root, not under the admin API's
version prefix — customers deploy code against these paths and they must not
move when the console's API changes.

**Two real bugs surfaced while building it**, both of which had been sitting
there unexercised:

1. **API-key authentication could not work at all.** `api_keys` is under RLS, so
   the key lookup — which necessarily happens before any tenant context exists —
   matched zero rows and every valid key got a 401. Fixed the way the refresh
   token was: the tenant travels in the key (`ds_live_<tenant-hex>.<secret>`) so
   the context is bound before the lookup. Carrying it in the clear is safe — the
   secret authenticates, and RLS means a forged tenant finds no row.
2. **`TimestampMixin` declared `created_at` without `timezone=True`**, so the
   mapped type was a naive `TIMESTAMP` while every migration creates
   `timestamptz`. Invisible until something compares the column to an aware
   datetime — the rate-limit window was the first — and the same trap was waiting
   for every retention and expiry query still to be built.

Also fixed: nginx did not proxy `/public/`, so the SPA fallback answered machine
callers with a 200 carrying HTML.

13 new tests, 68 total.

### Publishable keys — DONE

`consent_surfaces` is unblocked. A browser banner can now collect consent with a
`pk_live_…` key that is **collect-only** — it cannot withdraw, read or erase
anything — and every record it creates carries server-observed provenance in the
tamper-evident chain. An optional signed token from the integrator's own server
gives real principal binding for sensitive purposes.

The load-bearing change was splitting `consent:write` into `consent:collect` and
`consent:withdraw`. Until that split, one scope covered both recording and
destroying consent, and a published credential was one field away from wiping a
real person's consent and tripping the customer's processing stops.

Security model, and its stated limits:
[PUBLISHABLE_KEY_SECURITY.md](PUBLISHABLE_KEY_SECURITY.md). 32 new tests,
**100 total**.

Still to do for the banners themselves: wire `ConsentBanner.jsx` and
`CookieConsent.jsx` to `/public/v1/banner/*` and flip `consent_surfaces` to live.
The backend they need now exists.

### Phase 5 — DSAR, properly

The engine already works and fans out across four datastores. What is missing
is the record: `dsar_requests` with an `engine_ref` to the Fides privacy
request, so a request survives a page reload on the server side rather than in
the browser's localStorage (where it lives today, as a stopgap), and the
fiduciary triage queue has something real to act on.

### Phases 6–9

Grievances with the escalation clock; retention policies with real purge
execution and exemption handling; breach register with the DPDP notification
duty; reports generated from data that now exists.

### Phase 10 — Hardening

OIDC SSO, field-level encryption for principal PII, WORM anchoring for the
audit chain, load tests. The audit chain currently cannot detect truncation of
its newest entries — external anchoring is the answer, and it belongs here.

---

## 3. Platform readiness (parallel track)

Not sequenced against the phases above; these gate *selling*, not *building*.

| | Why it gates a paying customer |
| --- | --- |
| Managed Postgres with HA + 35-day PITR | Currently Burstable B1ms, no HA, 7-day retention. Losing a consent record is losing the evidence the product exists to provide. |
| Backups **tested by restoring** | An untested backup is a belief, not a backup. |
| Secrets in Key Vault, rotated | 19 secrets; `DS_AUDIT_HMAC_KEY` especially — rotating it later invalidates the existing chain, so the rotation procedure has to be designed before it is needed. |
| Observability | Structured JSON logs already carry a request id. Needs shipping, retention, alerting on 5xx and on worker starvation. |
| The legal surface | Cookie notice, privacy notice, named grievance officer (DPDP §13), DPA, sub-processor list, retention schedule for our *own* data. We are a data fiduciary the moment a customer uploads a record. |
| Isolation proof | RLS is enforced and tested. A customer's security review will ask for the test output — it should be a document, not a grep. |
| Rate limiting + abuse | Login lockout exists. Signup, public API and uploads need caps. |
| Blob storage for DSAR packages | Currently a shared filesystem. An access package is one person's complete PII; it needs expiry, download audit, and not being streamed through the gateway. |

---

## 4. Honest effort

Ranges, not promises, and they assume the existing spine (auth, RLS, audit) is
not rebuilt.

| Phase | Rough size |
| --- | --- |
| 3 — Consent core | 1–2 weeks |
| 4 — Public API | 1 week |
| 5 — DSAR persistence + triage | 1 week |
| 8 — Notifications | 1 week (plus a provider decision) |
| 6 — Grievances | 1 week |
| 7 — Retention + purge | 1–2 weeks |
| 9 — Reports + breach | 1–2 weeks |
| 10 — Hardening | 2–3 weeks |
| Platform readiness | 1–2 weeks, overlapping |

**Roughly two to three months of focused work to a defensible v1.** Anyone
quoting less has not counted the purge executor or the notification provider
integration.

What is *not* in that number: multi-region, SOC 2, a billing system, or the
23-language translation the UI currently scaffolds and honestly labels as
English-fallback.

---

## 5. Phase 3 — DONE

Built, tested and wired to the UI. `consent` is now `live` in
`config/modules.js`, so its preview banner disappeared on its own.

Beyond the checklist below, what shipped:

- **A published notice is frozen by a database trigger**, not a service method.
  Editing one raises; revising creates version N+1; existing consents keep
  pointing at the version their signatory actually read (proven by test).
- **A mandatory purpose cannot rest on `consent`** as its legal basis. Consent
  that cannot be refused is not consent, so the shape is rejected at creation.
- **Expiry is evaluated at read time**, not by a nightly sweep — a sweep leaves
  a window in which an expired consent still reads as active, and processing in
  that window is unlawful.
- **Starter purposes and notices are seeded at registration** so the module is
  usable immediately. Configuration only: no consents are seeded, because a
  consent has to be an act by a person.
- **The public consent/cookie banners were split into `consent_surfaces`** and
  stay preview. They are unauthenticated screens and cannot use the
  authenticated API — they need the public API (Phase 4). Without the split,
  flipping `consent` to live would have silently re-enabled two screens still
  writing to in-memory mock state.

16 new tests, 55 total, all passing.

### Next: Phase 4 — the public API

`GET /public/v1/consent/check` is the surface customers integrate against, and
the consent core it needs now exists.

---

## 6. The original Phase 3 definition of done

Because it is the product, it unblocks 4, 6, 7 and 9, and it needs none of the
four outstanding `[DECIDE]` answers — which still block the Azure phases.

Definition of done for Phase 3:

- [ ] Four tables, RLS policies, added to `TENANT_SCOPED_TABLES`
- [ ] A published notice cannot be mutated — enforced by trigger, proven by test
- [ ] `consents.notice_id` NOT NULL; no path creates an `active` consent implicitly
- [ ] Withdrawal is one call and writes an audit entry
- [ ] Consent history reads from `audit_events`
- [ ] Cross-tenant isolation tests, as for every other table
- [ ] The frontend consent module reads and writes the real API
- [ ] `consent` flips to `live` in `config/modules.js` — and the preview banner
      disappears on its own, because the UI derives that from one file
