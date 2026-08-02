# fides-dsar-demo

An end-to-end **DSAR** (Data Subject Access Request) demo using [Ethyca's
Fides](https://ethyca.com/docs) as the privacy engine.

One API call fans a privacy request out across **two independent databases** —
PostgreSQL and MongoDB — and returns a per-collection execution log as proof of
what was read or erased.

- **Console UI at `/ui`** → see where a person's data lives and drive the whole flow
- `POST /data/subject` → add a person's data to **both** databases in one call
- `GET  /data/subject/{email}` → where that person's data lives, db1–db4
- `POST /dsar {action: "access"}` → returns the person's data from Postgres **and** Mongo
- `POST /dsar {action: "erasure"}` → Fides nulls their PII in **both** databases
- `GET /dsar/{id}` → status + the execution log a regulator would ask for

Everything runs locally with `docker compose up`. All Fides configuration is
version-controlled under [fides-config/](fides-config/) and loaded automatically
at startup, so a fresh `up` leaves Fides fully configured.

> **Also in this repo:**
> - **[DataShield backend](backend/)** — the multi-tenant API this product is being
>   built on: Postgres row-level security for tenant isolation, an HMAC hash-chained
>   audit trail, server-enforced permissions. `make api` → http://localhost:8100/docs
> - **[DataShield CMS](frontend/)** — a full Consent Management
> System UI for India's DPDP Act, 2023 (React + Vite + Tailwind, 21 screens, four
> roles), built to [CMS_Lovable_Prompt_Complete.md](CMS_Lovable_Prompt_Complete.md).
> It runs on mock data by default and can be pointed at the real DSAR engine
> below with a one-line flag. `make cms` → http://localhost:5173

---

## Architecture

```
                      ┌───────────────────────────────┐
   you ──────────────▶│  fastapi-gateway  :8000       │   Console at /
   browser / curl     │  ─────────────────────────    │   Swagger at /docs
                      │  GET  /ui  ← the console      │
                      │  GET  /health                 │
                      │  POST /data/subject ──────────┼──┐ writes app data
                      │  GET  /data/subject/{email}───┼──┤ reads app data
                      │  POST /dsar   {email, action} │  │ both direct,
                      │  GET  /dsar/{request_id}      │  │ not via Fides
                      └───────────────┬───────────────┘  │
                                      │  Fides REST API  │
                                      │  (login → token, │
                                      │   POST /api/v1/  │
                                      │   privacy-request)│
                                      ▼                  │
   ┌──────────────────────────────────────────────────────────────────┐  │
   │  fides  :8080          Fides webserver + Admin UI                │  │
   │  fides-worker          Celery worker — actually executes the DSR │  │
   │  ────────────────────────────────────────────────────────────    │  │
   │  fides-db   :7432      Fides' own Postgres (its app database)    │  │
   │  fides-redis:7379      Celery broker + DSR result cache          │  │
   └───────────┬──────────────────────────────────┬───────────────────┘  │
               │                                  │        ┌─────────────┘
   identity: email                    identity: email      │  read + write
               │      ┌───────────────────────────┼────┬───┘
               ▼      ▼                           ▼    ▼
   ┌───────────────────────────┐      ┌───────────────────────────────┐
   │  app-postgres  :6432      │      │  app-mongo  :37017            │
   │  ───────────────────────  │      │  ───────────────────────────  │
   │  users(id, email,         │      │  events {                     │
   │        full_name, phone,  │      │    email, event_type,         │
   │        created_at)        │      │    metadata{ip_address,       │
   │  orders(id, user_email,   │      │             user_agent,       │
   │         amount, item,     │      │             session_id},      │
   │         created_at)       │      │    timestamp }                │
   └───────────────────────────┘      └───────────────────────────────┘
   ┌───────────────────────────┐      ┌───────────────────────────────┐
   │  app-mysql  :6306         │      │  Zoho CRM      (SaaS, remote) │
   │  ───────────────────────  │      │  ───────────────────────────  │
   │  support_tickets(         │      │  Contacts module              │
   │    id, email, subject,    │      │    Email, First_Name,         │
   │    body, status,          │      │    Last_Name, Phone           │
   │    created_at)            │      │  optional — needs OAuth creds │
   └───────────────────────────┘      └───────────────────────────────┘

   ── one-shot at startup ────────────────────────────────────────────
   fides-provisioner   `fides push` + Fides API  ──▶  loads /fides-config
                       exits 0 when Fides is configured
```

The DSAR crosses the database boundary because **every** dataset declares
`fides_meta: {identity: email}` on its email field. No database is named in the
policy — Fides matches on **data categories**, so the same unchanged policy picks
up a new datastore the moment you annotate one. That is not a claim: MySQL and
Zoho CRM were both added later, and neither required a policy edit.

### Ports

| Service           | URL / port                                       | What it is                              |
| ----------------- | ------------------------------------------------ | --------------------------------------- |
| `fastapi-gateway` | **http://localhost:8000/** ← DSAR Console        | The UI you drive the demo from           |
| `fastapi-gateway` | **http://localhost:8000/docs** ← Swagger UI      | Same API, raw                            |
| `fides`           | **http://localhost:8080** ← Fides Admin UI       | Login `root_user` / `Testpassword1!`    |
| `app-postgres`    | `localhost:6432`                                 | Demo app data: `users`, `orders`        |
| `app-mongo`       | `localhost:37017`                                | Demo app data: `events`                 |
| `app-mysql`       | `localhost:6306`                                 | Demo app data: `support_tickets`        |
| `presidio`        | `localhost:8001`                                 | PII detection (`POST /detect`) — **no caller yet** |
| `fides-db`        | `localhost:7432`                                 | Fides' own database (debugging only)    |
| `fides-redis`     | `localhost:7379`                                 | Fides' Redis (debugging only)           |
| `cms-backend`     | **http://localhost:8100/docs** ← API Swagger      | Multi-tenant CMS backend (real Postgres) |
| `cms-db`          | `localhost:6543`                                 | The backend's own database                |
| `cms-frontend`    | **http://localhost:5173** ← DataShield CMS        | DPDP consent management (real auth)      |

Zoho CRM is a fourth datastore, reached over the internet through a Fides SaaS
connector rather than run as a container. It is optional: without OAuth
credentials in `.env` it reports `not configured`, the console shows db4 amber,
and every other leg of the demo still works.

**`presidio` is not wired into anything yet.** It builds, runs and answers
`POST /detect`, but no service calls it. It is here for the PII-discovery work
in progress, and it is listed as unwired rather than left to look load-bearing.

All host ports are overridable in `.env` if something is already bound.

### Layout

```
docker-compose.yml            every service, one file
.env.example                  all credentials + ports (copy to .env)
fides-config/
  fides.toml                  Fides server config (mounted into the container)
  resources/
    systems.yml               data-map entry per datastore
    app_postgres_dataset.yml  users + orders, PII-annotated, primary keys, identity
    app_mongo_dataset.yml     events (incl. nested metadata), PII-annotated
  connections/connections.yml ConnectionConfig per DB, secrets as $ENV_VARS
  policies/dsr_policies.yml   the access policy + the erasure policy
  provision/provision.py      loads all of the above into a running Fides
app-postgres/init/            schema + seed (auto-run by the postgres image)
app-mongo/init/               app user + seed (auto-run by the mongo image)
fastapi-gateway/              the FastAPI service
  app/main.py                 endpoints: /health, /data/subject (POST+GET), /dsar
  app/fides_client.py         async Fides API client (auth, requests, logs)
  app/db.py                   direct app-postgres + app-mongo reads/writes
  app/static/                 the console — index.html + styles.css + app.js
                              (vanilla, no build step, no CDN)
scripts/
  bootstrap.sh                one-command first run (ports, up, wait, URLs)
  acceptance.sh               end-to-end proof — run it after every change
  dsar.sh                     fire a DSAR and poll it to completion
  show_data.sh                print the subject's rows from both DBs
Makefile                      shortcuts for all of the above
CONTRIBUTING.md               how to add another tool to the DSAR fan-out
backend/                      DataShield API — multi-tenant, RLS-isolated
  ARCHITECTURE.md             the plan of record: decisions and why
  app/core/                   config, security, permissions, logging, errors
  app/db/                     session + tenant context (where RLS is wired)
  app/models|schemas|services|api
  migrations/                 Alembic, incl. RLS policies + append-only trigger
  tests/                      isolation, auth, audit-chain tamper detection
frontend/                     DataShield CMS — the DPDP consent-management UI
  src/api/index.js            all mock data + API functions (one file to replace)
  src/components/             layouts + 12 reusable components
  src/pages/{auth,user,admin} 21 screens
  README.md                   screen list, the rules it enforces, how to wire it
```

---

## How the Fides config gets loaded

`docker compose up` starts a one-shot **`fides-provisioner`** service. It uses the
same `ethyca/fides` image as the server (so the `fides` CLI is available), waits
for `/health`, then loads everything and exits 0.

| Resource                       | Loaded by                                        |
| ------------------------------ | ------------------------------------------------ |
| CLI credentials                | **`fides user login`** (CLI)                     |
| Systems, Datasets              | **`fides push /fides-config/resources`** (CLI)   |
| Local storage destination      | `PUT /api/v1/storage/default`                    |
| ConnectionConfigs              | `PATCH /api/v1/connection`                       |
| Connection secrets (from env)  | `PUT /api/v1/connection/{key}/secret?verify=true` |
| Dataset ↔ connection link      | `PATCH /api/v1/connection/{key}/datasetconfig`   |
| Connection ↔ system link       | `PATCH /api/v1/system/{key}/connection`          |
| DSR policies, rules, targets   | `PATCH /api/v1/dsr/policy[...]`                  |

The CLI is used where it exists; the rest goes through the API because
`fides push` only handles fideslang resources — **DSR policies and connections
are not pushable**, which is the single most common point of confusion in this
setup. Every call is an upsert, so re-running is safe:

`fides user login` comes first because the CLI does **not** authenticate from
`[user] username/password` in `fides.toml` — it reads a bearer token out of
`~/.fides_credentials`, which only that command writes. Skip it and `fides push`
creates the taxonomy and then fails every dataset with
`{'detail': 'Not Authorized for this action'}`.

```bash
docker compose up fides-provisioner   # re-apply after editing fides-config/
```

Two guardrails run at the end of provisioning, so a broken config fails the boot
rather than silently returning nothing at DSAR time:

- `?verify=true` opens each database connection for real (retried, since a
  container can pass its healthcheck a moment before it accepts connections).
- `GET /connection/{key}/dataset/{key}/reachability` confirms Fides can actually
  traverse to every collection. An unreachable collection — no `identity` and no
  `references` path — is the classic Fides gotcha: it loads fine and then quietly
  returns nothing.

---

## Quick start

```bash
./scripts/bootstrap.sh    # or: make up
```

One command on a machine that has never seen this: it creates `.env`, **moves any
host port that is already taken on your machine**, builds, starts everything,
waits for Fides to be provisioned, and prints the URLs. Safe to re-run.

Then prove it works end to end:

```bash
make test                 # ./scripts/acceptance.sh
```

`PASS — one request reached every datastore, and the erasure held.`

Requirements: Docker with Compose v2, `python3`, `curl`. (`jq` optional.)

<details>
<summary>Doing it by hand instead</summary>

```bash
cp .env.example .env      # all demo credentials; already gitignored
docker compose up         # first boot pulls images + runs migrations, ~2-3 min
```

If a host port is busy, change it in `.env` — only the host side moves, nothing
internal cares.
</details>

Wait for:

```
fides-provisioner  | [provision] Fides is configured and ready.
fides-provisioner exited with code 0
```

Then open the console — **http://localhost:8000** — and drive the whole demo from
there (see [The console](#the-console) below).

Or the same thing in three commands:

```bash
./scripts/show_data.sh          # data present in both DBs
./scripts/dsar.sh erasure       # erase across both, print the execution log
./scripts/show_data.sh          # PII is now NULL in both DBs
```

### Everyday commands

```bash
make up          # start (safe to re-run)        make provision  # after editing fides-config/
make logs        # follow the useful logs        make build      # after editing fastapi-gateway/
make test        # end-to-end proof              make reset      # wipe + re-seed
make data        # raw rows from every database   make open       # console + Fides UI
make dsar EMAIL=someone@example.com
make cms         # DataShield CMS with hot reload   make cms-build  # production bundle
make api         # DataShield backend + its Postgres  make api-test   # backend suite
```

`make` is only a shortcut — every target is a plain `docker compose` command, and
`make` with no arguments lists them.

**Adding another database, SaaS tool, or connector?** That is the main way to
extend this — see **[CONTRIBUTING.md](CONTRIBUTING.md)**, which has the
step-by-step, the checklist, and the silent-failure gotchas.

---

## The console

**http://localhost:8000** — a single page served by the gateway itself. No build
step, no npm, no CDN: three static files (`index.html`, `styles.css`, `app.js`)
mounted at `/ui`, talking to the same origin's API, so there is no CORS to set up
and nothing to compile when you edit it.

```
┌──────────────────────────────────────────────────────────────────────┐
│ DSAR Console            ● gateway ● Fides ● db1 ● db2      ◐ Theme   │
├──────────────────────────────────────────────────────────────────────┤
│ [ demo@example.com    ] [Where is my data?] [Access] [Erasure]       │
├──────────────────────────────────────────────────────────────────────┤
│ WHAT WE FOUND FOR DEMO@EXAMPLE.COM                                   │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌──────────────────┐            │
│  │    8    │ │ ▪ db1 4 │ │ ▪ db2 4 │ │ masked rows   9  │            │
│  └─────────┘ └─────────┘ └─────────┘ └──────────────────┘            │
├──────────────────────────────────────────────────────────────────────┤
│ WHERE THE DATA LIVES — DEMO@EXAMPLE.COM                              │
│  ▔▔ db1 — app-postgres    users(1) · orders(3)  + every row          │
│  ▔▔ db2 — app-mongo       events(4)             + every row          │
├──────────────────────────────────────────────────────────────────────┤
│ PRIVACY REQUEST            pri_…  ● complete                         │
│  EXECUTION LOG — THE PROOF        3 of 6 entries □ show every entry   │
│  DATA RETURNED TO THE SUBJECT                                        │
├──────────────────────────────────────────────────────────────────────┤
│ ADD DATA  ＋ Write a person into every database                       │
└──────────────────────────────────────────────────────────────────────┘
```

What each part is for:

- **Health pills** — gateway, Fides, and both application databases. All four
  green means the whole chain is up, not just the web server.
- **Where the data lives** — one card per database, *every* row rendered, with each
  column subtitled by its **Fides data category** and marked `⌫` when the erasure
  policy nulls it. This is the answer to "where is my data", and it is read
  straight from the databases with no Fides in the path — so it is the independent
  check on Fides' work.
- **KPI tiles** — records for this identity, split by database, plus **masked rows
  remaining**: the rows an earlier erasure left behind with NULL identifiers. After
  an erasure the first tile reads `0` while that one stays non-zero, which is the
  difference between "erased" and "never existed".
- **Privacy request** — status pill that live-polls until the request settles, then
  the **execution log**: one row per collection per action, which fields were
  touched, across every datastore. Collapsed to the outcome per collection by
  default; *show every entry* reveals Fides' full state-change trail.
- **Add data** — the `POST /data/subject` form, with *Fill with sample data* for a
  one-click subject, then it re-runs the lookup so the write is immediately visible.

Two details worth knowing:

- **Requests are linkable.** Creating one rewrites the URL to
  `/ui/?request=pri_…&subject=someone@example.com`. That link reopens the request,
  its execution log, and the databases as they stand for that person — paste it into
  a ticket as the evidence. The subject travels in the URL because Fides'
  privacy-request API deliberately returns `identity: null` (identities are
  encrypted at rest and never handed back), so the id alone cannot recover it.
- **`?theme=dark` / `?theme=light`** forces a mode; otherwise the ◐ toggle's choice
  is remembered, falling back to your OS setting.

On the visual design: the datastores are the only colour-coded entities (the
validated categorical blue/orange, checked against colour-vision-deficiency
separation in both light and dark), status colours come from the reserved status
palette and always ship with a written label rather than colour alone, and the
erasure marker is a glyph as well as a colour so it survives greyscale. There are
deliberately **no charts** — every number here is a single count, which a stat tile
states honestly and a bar chart would only decorate.

---

## Adding your own data

Two endpoints let you work with the application data directly, without SQL,
`mongosh`, `mysql`, or Fides in the path:

| Route | Does |
| --- | --- |
| `POST /data/subject` | writes a person into **db1** (app-postgres) *and* **db2** (app-mongo) |
| `GET /data/subject/{email}` | reports **where** that person's records live, per database, per collection |

Both live in [db.py](fastapi-gateway/app/db.py) and use the same credentials as
the Fides ConnectionConfigs, so whatever you write here is reachable by a DSAR,
and whatever the lookup shows is exactly what an access DSAR should return.

### `POST /data/subject` — write to every database

The seed scripts insert `demo@example.com` and `control@example.com` on first
boot. To create more subjects, POST to the gateway — one call, every database:

```bash
curl -s -X POST localhost:8000/data/subject \
  -H 'Content-Type: application/json' \
  -d '{
        "email": "newperson@example.com",
        "full_name": "New Person",
        "phone": "+1-555-0142",
        "orders": [
          {"amount": "24.00", "item": "Mechanical keyboard"},
          {"amount": "9.99",  "item": "Mouse pad"}
        ],
        "events": [
          {"event_type": "login",    "ip_address": "203.0.113.99",
           "user_agent": "Mozilla/5.0 (X11; Linux x86_64)", "session_id": "sess_newp01"},
          {"event_type": "checkout", "ip_address": "203.0.113.99",
           "user_agent": "Mozilla/5.0 (X11; Linux x86_64)", "session_id": "sess_newp01"}
        ]
      }' | jq
```

The response says exactly which database got what:

```json
{
  "email": "newperson@example.com",
  "written_to": ["db1 app-postgres: users(inserted), orders(+2)",
                 "db2 app-mongo: events(+2)",
                 "db3 app-mysql: support_tickets(+1)"],
  "db1_app_postgres": { "host": "app-postgres:5432", "database": "appdb",
                        "users": {"id": 4, "action": "inserted"},
                        "orders": {"inserted": 2, "ids": [8, 9]} },
  "db2_app_mongo":    { "host": "app-mongo:27017", "database": "app_mongo_dataset",
                        "events": {"inserted": 2} },
  "db3_app_mysql":    { "host": "app-mysql:3306", "database": "appmysql",
                        "support_tickets": {"inserted": 1, "ids": [5]} },
  "next": "GET /data/subject/newperson@example.com  then  POST /dsar {...}"
}
```

Then DSAR them exactly like the seeded subject:

```bash
./scripts/dsar.sh access  newperson@example.com
./scripts/dsar.sh erasure newperson@example.com
./scripts/show_data.sh    newperson@example.com
```

| Field | Goes to | Notes |
| --- | --- | --- |
| `email` | **db1** `users.email`, **db1** `orders.user_email`, **db2** `events.email`, **db3** `support_tickets.email` | Required. The identity that later makes one DSAR find all of it. |
| `full_name`, `phone` | **db1** `users` | Optional. Omitting them on a repeat call leaves the stored values alone (`COALESCE`). |
| `orders[]` | **db1** app-postgres `orders` | Optional, appended. `amount` is `NUMERIC(10,2)`, must be > 0. |
| `events[]` | **db2** app-mongo `events` | Optional, appended. `ip_address` / `user_agent` / `session_id` nest under `metadata`. |
| `support_tickets[]` | **db3** app-mysql `support_tickets` | Optional, appended. |

Zoho CRM (**db4**) is read-only here: the gateway looks contacts up during
`GET /data/subject` and Fides erases them during a DSAR, but `POST /data/subject`
does not create CRM contacts.

Send `orders`, `events` and `support_tickets` to populate all three writable
databases in one call; send only
one of them to write to only that database. Either way a `users` row is always
upserted in db1, since that is the person's identity record.

Behaviour worth knowing:

- **`users` is upserted on email; `orders` and `events` append.** A second call
  with the same email updates the person rather than duplicating them, but grows
  their order and event history.
- **`created_at` / `timestamp` are set to now** by the databases. Only the seed
  scripts carry historical dates.
- **Only the fields above are accepted.** They are exactly what the datasets in
  [fides-config/resources/](fides-config/resources/) declare — an undeclared
  field would be invisible to Fides and would survive an erasure, which is the
  silent gap this demo exists to disprove. Add a column here and you must add it
  to the dataset YAML too.
- **This endpoint bypasses Fides entirely** ([db.py](fastapi-gateway/app/db.py)).
  Fides is a privacy engine — it reads and masks, it never writes application
  data. The endpoint uses the same credentials as the Fides ConnectionConfigs, so
  anything it inserts is reachable by a DSAR.
- **Not transactional across engines.** Postgres and Mongo are independent, so a
  Mongo failure after a successful Postgres write leaves the person in one
  database only. Re-POSTing is safe for `users`; orders/events would duplicate.
- **Re-adding an erased subject does not undo the erasure.** Matching is on
  email, and an erased row's email is `NULL`, so a fresh `users` row is inserted
  and the anonymised one stays behind.

Everything is also clickable in Swagger at **http://localhost:8000/docs** — the
"Try it out" body comes pre-filled with the payload above.

### `GET /data/subject/{email}` — where is my data?

Reads every datastore directly — the three databases plus Zoho CRM — and
reports where the person's records live:

```bash
curl -s localhost:8000/data/subject/demo@example.com | jq
curl -s 'localhost:8000/data/subject/demo@example.com?include_rows=false' | jq   # counts only
```

```json
{
  "email": "demo@example.com",
  "found": true,
  "total_records": 8,
  "found_in": ["db1 app-postgres: users(1), orders(3)",
               "db2 app-mongo: events(4)",
               "db3 app-mysql: support_tickets(1)"],
  "db1_app_postgres": {
    "label": "db1 — app-postgres (PostgreSQL)",
    "host": "app-postgres:5432", "database": "appdb",
    "fides_dataset": "app_postgres_dataset",
    "total": 4,
    "collections": {
      "users":  {"count": 1, "rows": [{"id": 1, "email": "demo@example.com",
                                       "full_name": "Demo Person", "phone": "+1-555-0100",
                                       "created_at": "2025-01-15T09:30:00Z"}]},
      "orders": {"count": 3, "rows": [{"id": 1, "user_email": "demo@example.com",
                                       "amount": "49.99", "item": "Noise-cancelling headphones",
                                       "created_at": "2025-02-02T14:05:00Z"}, "..."]}
    }
  },
  "db2_app_mongo": {
    "label": "db2 — app-mongo (MongoDB)",
    "host": "app-mongo:27017", "database": "app_mongo_dataset",
    "fides_dataset": "app_mongo_dataset",
    "total": 4,
    "collections": {
      "events": {"count": 4, "rows": [{"_id": "6a64ae0e512eceda7a708edb",
                                       "email": "demo@example.com", "event_type": "login",
                                       "metadata": {"ip_address": "203.0.113.42",
                                                    "user_agent": "Mozilla/5.0 ...",
                                                    "session_id": "sess_8fa31c"},
                                       "timestamp": "2025-02-02T13:58:00"}, "..."]}
    }
  },
  "db3_app_mysql": {
    "label": "db3 — app-mysql (MySQL)",
    "host": "app-mysql:3306", "database": "appmysql",
    "fides_dataset": "app_mysql_dataset",
    "total": 1,
    "collections": {
      "support_tickets": {"count": 1, "rows": [{"id": 1, "email": "demo@example.com",
                                                "subject": "Refund request", "..." : "..."}]}
    }
  },
  "db4_zoho_crm": {
    "label": "db4 — Zoho CRM (SaaS)",
    "host": "www.zohoapis.in", "database": "Contacts module",
    "fides_dataset": "zoho_crm_instance",
    "total": 0,
    "collections": {"contacts": {"count": 0, "rows": []}}
  },
  "masked_rows_remaining": {"users": 1, "orders": 3, "events": 2},
  "note": null
}
```

This is the **raw** view — no Fides in the path — which makes it the independent
check on Fides' work:

- Before a DSAR, `found_in` is the list of collections an **access** request
  should return. Compare it with `collections_touched` from `GET /dsar/{id}`.
- After an **erasure**, `found: false` and `total_records: 0` — with a `note`
  explaining why, and `masked_rows_remaining` proving the rows are still there
  with their identifiers nulled:

```json
{ "email": "erased@example.com", "found": false, "total_records": 0,
  "found_in": [], "masked_rows_remaining": {"users": 1, "orders": 3, "events": 2},
  "note": "No record in any system matches erased@example.com. There are 6
           masked row(s) with a NULL identifier, so this subject may have been
           erased by a previous DSAR — an erasure nulls the email, which is
           exactly why a lookup by email can no longer find them." }
```

`masked_rows_remaining` is a **count only**, deliberately: an erased row can no
longer be attributed to anybody, so there is no honest way to list it under a
person's name. A lookup finding nothing while that count is non-zero is the
signature of a completed erasure, as opposed to someone who was never here.

Each collection is capped at 500 rows (`AppDatabases.MAX_ROWS`).

---

## Step-by-step manual test

> Every step below is also a click in the console at **http://localhost:8000** —
> the curl commands are the same calls, spelled out.

### 1. Confirm both connections + datasets loaded

Open the Fides Admin UI at **http://localhost:8080** and log in with
`root_user` / `Testpassword1!`.

- **Data map → Systems** — three systems: `Demo Application`,
  `App Postgres Database`, `App Mongo Database`.
- **Integrations** — `App Postgres Connection` (postgres) and
  `App Mongo Connection` (mongodb), both **Active**, each with its dataset
  attached. Hit **Test connection** on each.
- **Privacy requests → Configuration → Policies** — `Demo Access Policy` and
  `Demo Erasure Policy`.

Or from the terminal:

```bash
TOKEN=$(curl -s -X POST localhost:8080/api/v1/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"root_user","password":"Testpassword1!"}' | jq -r .token_data.access_token)

curl -s localhost:8080/api/v1/connection -H "Authorization: Bearer $TOKEN" \
  | jq '.items[] | {key, connection_type, access, disabled}'
curl -s localhost:8080/api/v1/dsr/policy -H "Authorization: Bearer $TOKEN" \
  | jq '.items[] | {key, rules: [.rules[] | {action_type, masking_strategy}]}'
```

### 2. Confirm seed data exists in both DBs

```bash
./scripts/show_data.sh                                   # straight from the DBs
curl -s localhost:8000/data/subject/demo@example.com | jq # same answer, via the API
```

Or in the console: type the email and hit **Where is my data?**

Expected: 1 user + 3 orders in Postgres, 4 events in Mongo — all for
`demo@example.com`. There is also a **`control@example.com`** subject in both
databases; nothing of theirs may ever be touched, which is what proves the
erasure was targeted rather than a table-wide `UPDATE`.

Check the gateway is healthy too:

```bash
curl -s localhost:8000/health | jq
```

### 3. Access DSAR → data from Postgres AND Mongo

```bash
curl -s -X POST localhost:8000/dsar \
  -H 'Content-Type: application/json' \
  -d '{"email":"demo@example.com","action":"access"}' | jq
```

```json
{
  "request_id": "pri_1a2b3c4d-...",
  "action": "access",
  "policy_key": "demo_access_policy",
  "email": "demo@example.com",
  "status": "in_processing"
}
```

Then read it back (`data` is keyed by `dataset:collection`):

```bash
curl -s localhost:8000/dsar/pri_1a2b3c4d-... | jq '.status, .data'
```

Five collections, four datastores, one request:

```json
{
  "app_postgres_dataset:users":  [ { "id": 1, "email": "demo@example.com", "full_name": "Demo Person", "phone": "+1-555-0100" } ],
  "app_postgres_dataset:orders": [ { "user_email": "demo@example.com", "amount": "49.99", "item": "Noise-cancelling headphones" }, ... ],
  "app_mongo_dataset:events":    [ { "email": "demo@example.com", "event_type": "login",
                                     "metadata": { "ip_address": "203.0.113.42", "user_agent": "...", "session_id": "sess_8fa31c" } }, ... ]
}
```

Where that `data` comes from: Fides writes the assembled package to its storage
destination — here the `local` one, i.e. `./fides_uploads/<request_id>.json` on
your host — and the gateway reads it back to serve inline. There is no Fides API
that returns the rows for a real privacy request:
`GET /privacy-request/{id}/filtered-results` is restricted to *test* requests
(403 *"Results can only be retrieved for test privacy requests"*), and
`GET /privacy-request/{id}/access-results` returns only the storage location —
literally `"your local fides_uploads folder"` for local storage, and presigned
URLs for S3. `access_package` in the response shows both paths. With a real S3
destination you would drop the bind mount and fetch the presigned URL instead.

### 4. Erasure DSAR → Fides erases across both

```bash
curl -s -X POST localhost:8000/dsar \
  -H 'Content-Type: application/json' \
  -d '{"email":"demo@example.com","action":"erasure"}' | jq -r .request_id
```

The erasure rule uses the **`null_rewrite`** masking strategy against the
data categories `user.contact`, `user.name`, `user.device` and
`user.unique_id.pseudonymous`. Fides translates one strategy into both dialects:

| Database     | What Fides issues                                                              |
| ------------ | ------------------------------------------------------------------------------ |
| app-postgres | `UPDATE users SET email=NULL, full_name=NULL, phone=NULL WHERE id = ...`       |
| app-postgres | `UPDATE orders SET user_email=NULL WHERE id = ...`                             |
| app-mongo    | `{$set: {email: null, "metadata.ip_address": null, "metadata.user_agent": null, "metadata.session_id": null}}` |

Deliberately **not** erased, so you can see erasure is surgical:
`orders.amount` / `orders.item` (`user.behavior.purchase_history`),
`events.event_type` (`user.behavior`), and every `created_at` / `timestamp`
(`system.operations`).

### 5. Poll for completed status + execution log

```bash
curl -s localhost:8000/dsar/pri_... | jq '{status, finished_processing_at, collections_touched, execution_log}'
```

```json
{
  "status": "complete",
  "collections_touched": [
    "app_mongo_dataset:events",
    "app_postgres_dataset:orders",
    "app_postgres_dataset:users"
  ],
  "execution_log": [
    {
      "dataset": "app_postgres_dataset",
      "collection": "users",
      "action_type": "erasure",
      "status": "complete",
      "fields_affected": ["users.email", "users.full_name", "users.phone"]
    },
    {
      "dataset": "app_mongo_dataset",
      "collection": "events",
      "action_type": "erasure",
      "status": "complete",
      "fields_affected": ["events.email", "events.metadata.ip_address", "events.metadata.session_id"]
    }
  ]
}
```

This is the regulator artifact: **per collection, per action, which fields were
touched, and when.** Fides also emits two request-level entries with a `null`
collection — `Request execution plan` and `Dataset traversal` — which confirm it
built and walked the graph successfully; `scripts/dsar.sh` filters them out for
readability. Add `?include_raw_log=true` for Fides' unmodified entries;
the same log is in the Admin UI under *Privacy requests → the request → Events
and logs*.

> An erasure runs its **access** phase first (Fides has to find the rows before
> it can mask them), so the log legitimately contains both `access` and
> `erasure` entries for each collection.

### 6. Re-run access → confirm the data is gone

```bash
./scripts/show_data.sh                                   # ground truth, via psql/mongosh
curl -s localhost:8000/data/subject/demo@example.com | jq # ground truth, via the API
./scripts/dsar.sh access                                 # and via Fides
```

Expected:

- **Postgres** — the `users` row still exists with `email`, `full_name`, `phone`
  all `NULL`; `orders` rows still exist with `user_email` `NULL` but `amount` and
  `item` intact.
- **Mongo** — the 4 `events` documents still exist with `email` and every
  `metadata.*` identifier `null`, `event_type` intact.
- **`control@example.com` — completely untouched.**
- The new access DSAR returns **empty** `data`: `email` was the traversal
  entrypoint, so with it nulled there is nothing left to join the person to.
- `GET /data/subject/demo@example.com` reports `found: false` with
  `masked_rows_remaining` non-zero — the rows survive, the identifiers do not.

Rows survive with their PII nulled because `fides.toml` sets
`execution.masking_strict = true`, which forbids Fides from falling back to row
deletion. That is what makes the erasure *provable per column*. Set it to `false`
if you want hard deletes instead.

Reset and start over at any time:

```bash
docker compose down -v && docker compose up
```

---

## Notes, gotchas, and version sensitivity

Pinned versions: **`ethyca/fides:2.86.2`** (via `FIDES_IMAGE_TAG` in `.env`),
`postgres:12` for Fides' own DB (matching Fides' sample deployment),
`postgres:16-alpine` + `mongo:7.0` for the app data.

- **For MongoDB, the dataset's `fides_key` IS the database name.** Fides does
  `client[node.address.dataset][collection]` in both `retrieve_data()` and
  `mask_data()`; the `defaultauthdb` secret is only used to authenticate. That is
  why app-mongo's database is called `app_mongo_dataset` (`APP_MONGO_DB` in
  `.env`) rather than `appdb`. Mismatch them and the connection test still
  passes, the dataset still loads, and every DSAR dies at execution time with
  `not authorized on <fides_key> to execute command { find: ... }`. The SQL
  connectors use `dbname` from the connection secrets instead, so
  `app_postgres_dataset`'s key is free-form. Source:
  `fides/api/service/connectors/mongodb_connector.py`.
- **The CLI needs `fides user login` before `fides push`** — it authenticates
  from `~/.fides_credentials`, not from `fides.toml`. See above.
- **`drp_action` is globally unique.** Fides' built-in `default_access_policy`
  and `default_erasure_policy` already hold `access` and `deletion`, so setting
  either on your own policy fails the push with a Postgres `UniqueViolation` on
  `ix_policy_drp_action`. Our policies omit it and are addressed by
  `policy_key`; see [dsr_policies.yml](fides-config/policies/dsr_policies.yml).
- **`security.subject_request_download_ui_enabled` must be `true`** for
  `GET /privacy-request/{id}/access-results` to answer at all. It defaults to
  `false`, which returns 403 *"Access results download is disabled."*
- **`primary_key: True` is mandatory for erasure.** Without it on
  `users.id` / `orders.id` / `events._id`, Fides generates no masking update and
  the erasure silently does nothing. See
  [app_postgres_dataset.yml](fides-config/resources/app_postgres_dataset.yml).
- **`access: write` on the ConnectionConfig is mandatory for erasure.** With
  `read`, access requests work and every masking update is skipped without error.
- **`fides_meta`, not `fidesops_meta`.** The old spelling is from standalone
  fidesops / Fides < 2.x. Both are still accepted; `fides_meta` is current.
- **DSR policies live at `/api/v1/dsr/policy`.** They are *not* the fideslang
  `policy:` resource that `fides push` handles — different concept, same word.
  See the header comment in
  [dsr_policies.yml](fides-config/policies/dsr_policies.yml).
- **The Mongo user must exist inside `appdb`**, not `admin`: Fides authenticates
  against the database given in `defaultauthdb`. That is why
  [00_create_app_user.sh](app-mongo/init/00_create_app_user.sh) exists alongside
  the image's root user.
- **A worker is required.** `FIDES__CELERY__TASK_ALWAYS_EAGER=False` plus the
  `fides-worker` service is the production shape and is what the DSR 3.0
  request-task graph expects. Without a running worker, requests sit in
  `in_processing` forever.
- **Verification and approval are disabled** in `fides.toml`
  (`subject_identity_verification_required`, `require_manual_request_approval`,
  `erasure_request_finalization_required` — all `false`) so the demo runs
  hands-free. A real deployment turns these on, and requests then wait in the
  Admin UI for a human.
- **`local` storage is test-only.** Access packages are written to the Fides
  container's filesystem (surfaced at `./fides_uploads/`, gitignored because it
  contains real personal data). Use `s3` or `gcs` for anything real.
- Set `logging.log_pii = false` in `fides.toml` before pointing this at anything
  that isn't seed data — it is `true` here so identities are visible while
  debugging.

Docs: [configuration](https://ethyca.com/docs/dev-docs/configuration/configuration)
· [datasets](https://ethyca.com/docs/dev-docs/configuration/datasets)
· [connections](https://ethyca.com/docs/dev-docs/configuration/connections)
· [policies](https://ethyca.com/docs/dev-docs/configuration/policies)

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `up` fails with `ports are not available: ... bind: address already in use` | Something else owns that host port. Change `FIDES_PORT` / `GATEWAY_PORT` in `.env` (only the host side moves; nothing internal cares). |
| `fides-provisioner` exits non-zero with a connectivity failure | Wrong credentials in `.env`, or the app DB never became healthy. Check `docker compose logs app-mongo app-postgres`. |
| Provisioner dies on `NOT reachable` | A dataset collection has no `identity` and no `references` path. Fix the dataset YAML. |
| DSAR stuck in `in_processing` | `fides-worker` is down. `docker compose logs fides-worker`. |
| Erasure completes but nothing changed | Missing `primary_key: True`, or the connection is `access: read`. |
| Access DSAR returns nothing at all | The identity email genuinely has no rows — or it was already erased. |
| Console loads but every pill is red | The gateway is up and Fides/the databases are not. `docker compose ps`. |
| Console edits don't show up | The static files are baked into the image: `docker compose up -d --build fastapi-gateway`. |
| `.env` edits appear to do nothing | `fides.toml` values are overridden by `FIDES__*` env vars in `docker-compose.yml`; the env var wins. |
