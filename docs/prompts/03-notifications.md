# Build brief — `notifications` (email/SMS delivery)

> Paste this whole file as the opening prompt. Fill the `[DECIDE]` block first.

**Size: ~1 week**, plus a provider decision. **Build this before grievances (04)
and retention (05)** — both are specified in terms of sending someone a message,
and building them against a notifications module that does not exist means
building them twice.

---

## 1. What exists

`/admin/notifications` (`NotificationCenter.jsx`) renders mock templates in 23
languages with a locked "Save template" button. `Capability.NOTIFICATION_MANAGE`
already exists. Nothing sends anything.

The rest of the product has been written with clean seams where a notification
belongs — DSAR status changes, consent withdrawal confirmations, grievance
escalation, pre-purge warnings.

## 2. `[DECIDE]` — genuinely blocking

- `[DECIDE]` **Email provider.** Options: **AWS SES** (cheap, needs domain
  verification and a sandbox exit), **SendGrid** / **Resend** (fastest to
  integrate), **Azure Communication Services** (keeps everything in one cloud and
  one bill, which matters because the rest of the platform is Azure).
  **Recommendation: Azure Communication Services**, for that last reason alone.

- `[DECIDE]` **SMS at all in v1?** India SMS needs DLT registration with TRAI —
  template pre-registration, sender-id approval, weeks of lead time.
  **Recommendation: email only in v1**, SMS behind a feature flag with the DLT
  work tracked separately. Say so on screen rather than showing a dead channel.

- `[DECIDE]` **Sending domain.** Needs SPF, DKIM and DMARC on a domain you
  control, or everything lands in spam and a "we notified you" claim becomes
  indefensible. Ties to the outstanding domain decision in the deployment plan.

- `[DECIDE]` **Which languages are actually translated?** The UI offers all 23
  Eighth Schedule languages and honestly labels the untranslated ones as English
  fallback. Notifications should do the same rather than silently sending English
  under a Hindi label.

## 3. Data model (migration `0005`+)

```
notification_templates
  tenant_id, key            e.g. dsar.received, grievance.escalated
  channel                   email | sms
  language
  subject, body             body is a template with named placeholders
  is_active
  UNIQUE (tenant_id, key, channel, language)
```

```
notifications                 the delivery log — append-and-read
  tenant_id, template_key, channel, language
  to_address                  see §6 on storing this
  subject_rendered
  status                      queued | sending | delivered | failed | suppressed
  provider_message_id
  attempts, last_error
  entity_type, entity_id      what it was about (a DSAR, a grievance…)
  principal_id                nullable
  queued_at, sent_at, delivered_at, failed_at     all timestamptz
```

- `notifications` is **append-and-read** (`GRANT SELECT, INSERT`) except for the
  status transitions the sender itself performs — if you need updates, grant
  `UPDATE (status, provider_message_id, attempts, last_error, sent_at,
  delivered_at, failed_at)` on those columns only and say why in a comment. A
  freely mutable delivery log is not evidence that anything was sent.
- Fall back to English when a template is missing for a language, and **record
  the language actually sent**, not the one requested. An invisible fallback is a
  compliance problem: "we notified them in their language" must be checkable.

## 4. Sending

A **Postgres-backed job table**, not a broker. ARCHITECTURE.md already says a
broker is deliberately deferred until it is needed, and this is not the thing
that needs it.

- `POST` a notification → row at `queued` → a worker claims it with
  `SELECT … FOR UPDATE SKIP LOCKED` → sends → records the outcome.
- Retries with backoff, capped. A permanently failing address must end at
  `failed` with the reason, not retry forever.
- **Idempotency**: `(tenant_id, template_key, entity_type, entity_id)` should not
  produce two identical sends. A DPO refreshing a queue must not re-notify a data
  principal.
- Provider calls go behind one interface (`NotificationProvider`) with a
  `ConsoleProvider` for local development that writes to the log instead of
  sending. **Local development must never be able to email a real person.**

## 5. Template rendering

- Named placeholders (`{{reference}}`, `{{deadline}}`), rendered server-side.
- **Escape everything.** A grievance description written by a member of the
  public goes into an email body; unescaped, that is an injection into whatever
  renders it.
- Reject a template that references an unknown placeholder **at save time**, not
  at send time. Discovering a typo when a statutory notification fails is too
  late.
- Provide a preview endpoint that renders with sample data.

## 6. Privacy of the log itself

The delivery log accumulates email addresses and message subjects for every data
principal — a second copy of personal data with none of the consent machinery
around it.

- Store the address, because "we notified you at X" is the claim being made, but
  **do not store rendered bodies**.
- Give the table a retention period and make it configurable. This is our own
  data-minimisation obligation, and a product that sells retention policy while
  keeping its own logs forever will be asked about it.

## 7. Wire the seams

Send on: DSAR received / completed / rejected, consent withdrawn confirmation,
grievance received / escalated / resolved, pre-purge warning (retention).

Each call site should be **one line calling the service**. If a call site needs
more than that, the interface is wrong.

## 8. Non-goals

- No marketing/campaign sending. This is transactional compliance messaging.
- No in-app notification centre with read/unread state beyond what exists.
- No SMS unless the `[DECIDE]` says yes.

## 9. Tests

- a queued notification is claimed once under concurrency (`SKIP LOCKED`)
- retry backoff caps, and a permanent failure lands at `failed` with a reason
- the same (template, entity) does not send twice
- a missing-language template falls back to English **and records English**
- an unknown placeholder is rejected at save time
- template bodies are escaped — a description containing markup is inert
- the log stores no rendered body
- cross-tenant isolation on both tables
- `ConsoleProvider` is the default outside prod, and a test asserts prod config
  cannot silently use it

## 10. Definition of done

- [ ] A real email arrives in a test inbox from a real provider
- [ ] Every seam in §7 wired
- [ ] `notifications` flipped to `"live"` in `src/config/modules.js`
- [ ] If SMS is deferred, the channel is visibly labelled, not hidden
- [ ] If most languages are English-fallback, the UI says so per template
- [ ] `./scripts/acceptance.sh http://localhost:8090` still passes

## 11. House rules

RLS + `TENANT_SCOPED_TABLES` + policy test. Timezone-aware timestamps. Audit
every state change. Add new `AuditAction` constants rather than reusing
approximate ones. Full list: [README.md](README.md).
