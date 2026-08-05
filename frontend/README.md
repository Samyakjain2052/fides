# DataShield CMS — DPDP Act, 2023 Consent Management System

The user (Data Principal) and admin (DPO / Auditor / Grievance Officer) UI built
to [CMS_Lovable_Prompt_Complete.md](../CMS_Lovable_Prompt_Complete.md).

React + Vite + Tailwind. No Redux — `useState` + one context. All API calls go
through [src/api/index.js](src/api/index.js), on mock data, ready to be swapped
for real `fetch()` calls one function at a time.

```bash
npm install
npm run dev        # http://localhost:5173     hot reload
npm run build      # production bundle in dist/ (runs the preview-lock check first)
npm run check:preview   # the honesty check on its own
```

In Docker, `cms-frontend` is now the **production** image: a multi-stage build
served by nginx on `http://localhost:8090`, which also reverse-proxies `/api`
and `/gateway`. It is the only service meant to be publicly exposed. For hot
reload in Docker instead: `docker compose --profile dev up cms-frontend-dev`.

## Authentication is real

**The four demo accounts and "any password is accepted" are gone.** Sign-in and
sign-up now hit the backend at `http://localhost:8100/v1/auth/*` and create real
rows in PostgreSQL. Start the API first (`make api` from the repo root), then:

1. Go to **`/signup`**, create an organisation — you become its first Admin/DPO.
2. Sign in at **`/login`** with your workspace id, email and password.

Two things changed that are worth understanding, not just noting:

- **The role selector is gone.** A client-side role picker is not authentication —
  it let anyone choose to be an admin. The role now comes from the database and is
  re-checked server-side on every request.
- **The user is no longer in `localStorage`.** It used to be, which meant you could
  write `{"role":"admin"}` into devtools and reload into the admin console. The
  session is now an HttpOnly refresh cookie the page cannot read, exchanged for a
  short-lived access token held only in memory. A page reload restores the session
  through `/auth/refresh`; there is nothing on disk for an XSS payload to steal.

| Role | Reaches |
| --- | --- |
| Admin / DPO | everything (what signup creates) |
| Data Principal | their own consents, requests, complaints |
| Auditor | audit logs + reports, read-only |
| Grievance Officer | the grievance queue only |

Additional users in other roles are created from **Admin → Users & Roles**.

## What is live, and how the app says so

Five of the seven module groups render sample data. Rather than let a buyer find
that out by clicking, [src/config/modules.js](src/config/modules.js) is the single
source of truth and the UI derives everything from it:

- **A preview banner** on every affected screen, rendered once by the layout from
  the route (not pasted into each page, which is 18 chances to forget one).
- **A badge** on the nav item.
- **Every mutating control disabled**, with a tooltip saying why — not a silent
  no-op, which reads as a bug rather than a deliberate placeholder.
- **[/roadmap](src/pages/Roadmap.jsx)**, public, listing what is live and what each
  preview module still needs.

`npm run build` runs `scripts/check-preview-locks.mjs`, which fails the build if a
preview module gains a mutating control that is not locked. Unknown module keys
resolve to `preview`, so a typo fails toward telling the truth.

Three modules are live: `auth`, `dsar` (submit and track — the admin triage
queue is a separate key, `dsar_workflow`, still preview), and `consent`.

**Consent is real as of Phase 3.** `/user/preferences` and `/user/consent-history`
read and write PostgreSQL through [src/api/consent.js](src/api/consent.js):
purposes, published notice versions, data principals and the consent lifecycle.
Every change writes to the tamper-evident audit chain, and the history screen
shows each entry's chain hash — the same evidence the integrity check verifies.

The public consent and cookie banners are a separate key, `consent_surfaces`, and
remain preview: they are unauthenticated screens and need the public API (Phase 4)
before an anonymous visitor's consent can be recorded for real.

Live modules can still carry a caveat: DSAR correction and the OTP/DigiLocker
identity check are simulated, and the app says so on the screen.

## Screens

**Auth** — `/login`, `/forgot-password`

**Consent surfaces** (no sign-in required — a first-time visitor meets these
before they have an account)

| Route | Screen |
| --- | --- |
| `/consent-banner` | Consent Banner + Guardian Consent flow (under-18 age gate) |
| `/cookie-consent` | Cookie banner: 4 categories, Essential locked on |

**User** — `/user/dashboard`, `/user/preferences`, `/user/consent-history`,
`/user/dsar`, `/user/dsar/status`, `/user/grievance`, `/user/grievance/status`

**Admin** — `/admin/dashboard`, `/admin/dsar`, `/admin/consent-validation`,
`/admin/grievances`, `/admin/breaches`, `/admin/audit`, `/admin/roles`,
`/admin/retention`, `/admin/notifications`, `/admin/reports`

## The rules the brief makes non-negotiable, and where they live

| Rule | Enforced in |
| --- | --- |
| No pre-checked consent toggles, ever | `ConsentBanner.jsx` initialises every optional purpose to `false`; `CookieConsent.jsx` the same. There is no code path that defaults one on. |
| Mandatory consents shown with a locked toggle + "Required by law", never hidden | `ConsentCard.jsx` |
| Every destructive action behind a confirmation | `ConfirmModal.jsx`, used by withdraw consent, initiate erasure, escalate, resolve, revoke access, run purge, report a breach |
| Status = coloured dot **and** text label | `StatusBadge.jsx` — colour is never the only signal |
| Audit entry after every state change | every mutation in `api/index.js` calls `appendAuditLog()`; the screens surface the entry |
| Language switcher on every user-facing screen | in `UserLayout` header **and** on the standalone consent surfaces |
| DSAR deadline = submitted + 30 days, shown prominently | `deadlineFor()` in the API; `SLACountdown.jsx` renders it green / amber / red / OVERDUE |
| Guardian consent when under 18 | the age gate in `ConsentBanner.jsx` routes to `GuardianConsentFlow` |
| Audit logs have no edit or delete control | `AuditLogs.jsx` — the absence is the feature |
| Grievance escalation after N days | `GRIEVANCE_ESCALATION_DAYS` in the API, surfaced on the admin dashboard and the user's status screen |
| Retention exemptions configurable | `DataRetentionPolicy.jsx` |

## Not built yet (per the brief)

Real OTP delivery, real DigiLocker, real Bhashini translation, real email/SMS
delivery, billing. Each has its UI in place with a placeholder response, and each
is labelled as such on screen rather than pretending to work.

**On translation specifically:** the switcher offers all 23 Eighth Schedule
languages and persists the choice. English is complete and Hindi is a worked
sample so the mechanism is visible; every other language falls back to English
and the UI says so with an "English fallback" tag. That is the honest state until
a translation service is wired in.

## What is real and what is still mock

| Area | State |
| --- | --- |
| **Auth** — signup, login, refresh, logout, roles | **real**, against the backend and PostgreSQL |
| Consent, DSAR, grievances, retention, reports | mock, in `src/api/index.js`, until backend Phase 3 |

`src/api/auth.js` is the real one; `src/api/index.js` is the mock one. They are
separate files so it is obvious which is which.

## Wiring it to a real backend

`src/api/index.js` is the only file that talks to the outside. Replace one
function at a time — the screens don't care.

This repo already contains a working DSAR backend (Fides + Postgres + Mongo). To
point the CMS's DSAR functions at it:

1. start the stack from the repo root (`make up`),
2. set `USE_REAL_DSAR_BACKEND = true` at the top of `src/api/index.js`.

Access and erasure requests then create real Fides privacy requests, and
`/user/dsar/status` shows the live per-collection execution log. Vite proxies
`/gateway` → the FastAPI gateway ([vite.config.js](vite.config.js)), so there is
no CORS to configure. Correction requests stay on mock data — Fides has no
correction action.

## Notes for whoever picks this up

- **Design tokens, not hex.** The brief's palette is in
  [tailwind.config.js](tailwind.config.js) as `navy`, `teal`, `success`,
  `warning`, `danger`, `info`, `canvas`, `surface`, `line`, `ink`, `muted`. Use
  the token names so a palette change is one edit.
- **Charts are hand-rolled SVG** in `components/common/Charts.jsx` — no chart
  dependency. Every series is direct-labelled with its value and has a text
  legend, so the charts stay readable without relying on hue.
- **`BreachManagement.jsx` had no screen spec** in the brief (it is listed in the
  file structure only). It is built to the DPDP breach-notification duty —
  notify the Board and every affected Data Principal — following the same
  conventions as the specified screens. Revisit when it is specified.
- **`ConsentQueue.jsx`** is the file name from the brief's structure; the brief's
  screen list calls the same screen "Consent Validation Queue" at
  `/admin/consent-validation`. Both are honoured: that file, that route.
