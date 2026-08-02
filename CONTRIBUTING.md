# Working on this repo

Read [README.md](README.md) first for what the demo *is*. This file is about
changing it — in particular **adding another tool to the DSAR fan-out**, which is
what most work here will be.

---

## First run

```bash
git clone <this repo>
cd fides-dsar-demo
./scripts/bootstrap.sh        # or: make up
```

That creates `.env`, **moves any host port already taken on your machine**,
builds, starts everything, waits for Fides to be provisioned, and prints the
URLs. Re-running it is safe.

Then confirm the whole thing works end to end:

```bash
make test                     # ./scripts/acceptance.sh
```

You should see `PASS — one request reached every datastore, and the erasure held.`
**Run this before and after every change.** It is the contract of the repo: if it
passes, a single privacy request still reaches every datastore and erasure still
erases.

You need Docker (with Compose v2), `python3`, and `curl`. `jq` is optional.

---

## The mental model

```
your API + UI          the privacy engine            the data
fastapi-gateway  ──▶   fides ──▶ redis ──▶ worker ──▶ app-postgres
                       fides-db (its own records)  ──▶ app-mongo
```

Fides never writes application data — it reads it and masks it. Three ideas do
all the work:

| Idea | Where it lives | What it decides |
| --- | --- | --- |
| **Dataset** | `fides-config/resources/*_dataset.yml` | which fields exist, what category each one is, how to *find* a person (`identity`), how to *update* a row (`primary_key`) |
| **Connection** | `fides-config/connections/connections.yml` | how Fides physically reaches the datastore, with secrets as `$ENV_VARS` |
| **DSR policy** | `fides-config/policies/dsr_policies.yml` | what an access/erasure request *does*, targeted at **data categories** — never at named tables |

That last row is the whole trick: because the policy targets `user.contact`,
adding a datastore that declares `user.contact.email` on a field means the
existing erasure policy reaches it **with no policy change at all**.

---

## Adding another tool to the DSAR

This is the main workflow. Say you want to add a MySQL database with a
`support_tickets` table.

### 0. Is there a Fides connector for it?

```bash
TOKEN=$(curl -s -X POST localhost:$FIDES_PORT/api/v1/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"root_user","password":"Testpassword1!"}' \
  | jq -r .token_data.access_token)

curl -s "localhost:$FIDES_PORT/api/v1/connection_type?size=100" \
  -H "Authorization: Bearer $TOKEN" | jq -r '.items[] | "\(.type)\t\(.identifier)"' | sort
```

The pinned image (`ethyca/fides:2.86.2`) has **17 database** types —
`postgres, mysql, mariadb, mssql, mongodb, dynamodb, scylla, timescale,
bigquery, snowflake, redshift, s3, rds_postgres, rds_mysql,
google_cloud_sql_postgres, google_cloud_sql_mysql` — plus **SaaS** templates
(`hubspot, mailchimp, stripe`), **manual** tasks (`manual_task, jira_ticket`),
`website`, and erasure-by-email types. Use that `identifier` as your
`connection_type`.

If there is no connector, you have two honest options: a **manual task**
(Fides pauses the request and asks a human to act, which still ends up in the
execution log), or a **SaaS connector template** you write yourself.

### 1. Add the container

`docker-compose.yml` — a service plus an init script that seeds the *same*
`demo@example.com`, so one DSAR is visibly cross-system:

```yaml
  app-mysql:
    image: mysql:8.0
    environment:
      MYSQL_ROOT_PASSWORD: ${APP_MYSQL_ROOT_PASSWORD}
      MYSQL_DATABASE: ${APP_MYSQL_DB}
      MYSQL_USER: ${APP_MYSQL_USER}
      MYSQL_PASSWORD: ${APP_MYSQL_PASSWORD}
    volumes:
      - app-mysql-data:/var/lib/mysql
      - ./app-mysql/init:/docker-entrypoint-initdb.d:ro
    ports: ["${APP_MYSQL_HOST_PORT:-6306}:3306"]
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "127.0.0.1"]
      interval: 5s
      timeout: 5s
      retries: 20
```

Add the volume under `volumes:`, add the credentials to **`.env.example`**
(and your `.env`), and add the service to `fides-provisioner`'s `depends_on`
with `condition: service_healthy` — the provisioner verifies every connection
for real, so it must not start before the database is up.

**Make PII columns nullable.** The erasure strategy is `null_rewrite`; a
`NOT NULL` column makes the masking `UPDATE` fail.

### 2. Describe it to Fides — the dataset

`fides-config/resources/app_mysql_dataset.yml`:

```yaml
dataset:
  - fides_key: app_mysql_dataset
    name: App MySQL Dataset
    collections:
      - name: support_tickets
        fields:
          - name: id
            data_categories: [system.operations]
            fides_meta:
              primary_key: True          # ← without this, erasure silently no-ops
              data_type: integer
          - name: customer_email
            data_categories: [user.contact.email]
            fides_meta:
              identity: email            # ← without this, the collection is unreachable
              data_type: string
          - name: subject
            data_categories: [user.content]
            fides_meta:
              data_type: string
```

Three rules, each of which fails **silently** if you get it wrong:

- **`primary_key: True`** on exactly one field per collection, or Fides builds no
  masking `UPDATE` and the erasure does nothing while reporting success.
- **`identity: email`** on the field holding the subject's email — or a
  `references:` edge to another collection Fides can already reach. Without a
  path, the collection is unreachable and returns nothing.
- **`data_categories`** decide what the policies hit. `user.*` categories get
  returned by access; the four the erasure policy targets get nulled. Check a
  category exists before using it:
  `curl -s localhost:$FIDES_PORT/api/v1/data_category -H "Authorization: Bearer $TOKEN" | jq -r '.items[].fides_key'`

**MongoDB only:** the dataset's `fides_key` *is* the database name. See the note
at the top of [app_mongo_dataset.yml](fides-config/resources/app_mongo_dataset.yml).
SQL connectors use `dbname` from the connection secrets instead, so their
`fides_key` is free-form.

### 3. Add it to the data map — the system

`fides-config/resources/systems.yml`: another `- fides_key:` entry with a
`privacy_declarations` block listing the categories it holds and a
`dataset_references: [app_mysql_dataset]`. This is what makes it appear in the
Fides data map. `data_use` must be a real taxonomy key — check with
`GET /api/v1/data_use`.

### 4. Tell Fides how to connect

`fides-config/connections/connections.yml`:

```yaml
  - key: app_mysql_connection
    name: App MySQL Connection
    connection_type: mysql
    access: write                  # ← `read` makes erasure silently skip everything
    dataset: app_mysql_dataset
    system_key: app_mysql_system
    secrets:
      host: "$APP_MYSQL_HOST"
      port: "$APP_MYSQL_PORT"
      dbname: "$APP_MYSQL_DB"
      username: "$APP_MYSQL_USER"
      password: "$APP_MYSQL_PASSWORD"
```

**Never put a literal secret here** — only `$VAR` references, expanded by the
provisioner from its environment. Pass those vars to the `fides-provisioner`
service in `docker-compose.yml`.

Secret field names differ per connector. Get the exact schema:

```bash
curl -s "localhost:$FIDES_PORT/api/v1/connection_type/secrets/mysql" \
  -H "Authorization: Bearer $TOKEN" | jq
```

### 5. Load it

```bash
make provision        # docker compose up -d fides-provisioner
```

Idempotent. It pushes the dataset + system, upserts the connection, writes the
secrets **with `?verify=true`** (so a bad password fails here, not mid-DSAR), and
runs a **reachability check** on every dataset. If your collection has no
identity path, provisioning fails loudly right here instead of returning nothing
later.

### 6. Prove it

```bash
make test
```

The acceptance test asserts `>= 3` collections, so it will still pass — read its
output and confirm your new collection is in the list. Then look at the console:
your datastore should appear as a new card.

### 7. Two places that need a manual follow-up

- **`fastapi-gateway/app/db.py` + the console** only know about the two seeded
  databases (they power `POST /data/subject` and the "where is my data" view).
  A new datastore shows up in DSAR results automatically, but if you want it in
  the lookup view, add read/write methods there.
- **`fastapi-gateway/app/static/app.js` → `CATEGORIES`** mirrors the dataset
  YAML so the console can annotate columns with their data category. Add your
  collection's fields, or they render without annotations.

### The whole checklist

```
[ ] docker-compose.yml       service + volume + healthcheck
[ ] docker-compose.yml       secrets passed to fides-provisioner
[ ] .env.example (+ .env)    credentials and host port
[ ] app-<x>/init/            seed script using demo@example.com
[ ] resources/*_dataset.yml  primary_key, identity, data_categories, nullable PII
[ ] resources/systems.yml    system + privacy_declarations + dataset_references
[ ] connections.yml          connection_type, access: write, $VAR secrets only
[ ] make provision           loads it; fails loudly if unreachable
[ ] make test                proves the fan-out still holds
[ ] app.js CATEGORIES        (optional) console annotations
[ ] README.md                mention the new datastore
```

---

## The CMS frontend

[frontend/](frontend/) is a separate React + Vite + Tailwind app — the DPDP
Consent Management System UI, built to
[CMS_Lovable_Prompt_Complete.md](CMS_Lovable_Prompt_Complete.md). It is
independent of the DSAR engine and of the vanilla console at `:8000/ui`.

```bash
make cms          # npm install + vite dev on :5173, hot reload
make cms-build    # production bundle into frontend/dist
make cms-docker   # run it as a compose service instead
```

Everything it knows about the outside world is
[frontend/src/api/index.js](frontend/src/api/index.js) — mock data plus one
function per operation. Replace functions there to wire a real backend; the
screens don't change. Setting `USE_REAL_DSAR_BACKEND = true` in that file points
its access and erasure requests at this repo's Fides gateway through the Vite
`/gateway` proxy.

See [frontend/README.md](frontend/README.md) for the screen list, the compliance
rules the code enforces and where, and the two places where the brief was
ambiguous.

---

## Other common changes

**Change what an erasure nulls** — `fides-config/policies/dsr_policies.yml`, the
`targets` of `demo_erasure_rule`. Targets are *categories* and are hierarchical:
`user.contact` covers `user.contact.email` and `user.contact.phone_number`. Then
`make provision`.

**Change the masking strategy** — same file, `masking_strategy.strategy`.
Alternatives to `null_rewrite`: `string_rewrite`, `random_string_rewrite`,
`hash`, `hmac`, `aes_encrypt`. Some take `configuration`. Note `masking_strict`
in `fides.toml` forbids Fides from falling back to row deletion — that is what
makes the demo's erasure provable per column.

**Add a gateway endpoint** — `fastapi-gateway/app/main.py`, then
`make build`. Fides calls go through `app/fides_client.py`; direct database work
goes in `app/db.py`.

**Change the console** — `fastapi-gateway/app/static/{index.html,styles.css,app.js}`.
Vanilla, no build step. The files are baked into the image, so `make build` after
editing. Colours: the two databases use the validated categorical blue/orange,
statuses use the reserved status palette and always carry a written label, and
the erasure marker is a glyph as well as a colour — please keep that.

---

## Dev loop

```bash
make up            # start (safe to re-run)
make logs          # follow worker + gateway, minus known noise
make build         # after editing fastapi-gateway/
make provision     # after editing fides-config/
make test          # end-to-end proof
make data          # raw rows from both databases
make reset         # wipe volumes and re-seed
make dsar EMAIL=someone@example.com
```

Rebuild rules that trip people up:

| You edited | Do this |
| --- | --- |
| `frontend/**` | nothing — Vite hot-reloads (`make cms`) |
| `fides-config/**` | `make provision` |
| `fastapi-gateway/**` (incl. static) | `make build` |
| `docker-compose.yml`, `.env` | `docker compose up -d` |
| `fides.toml` | `docker compose up -d --force-recreate fides fides-worker` |
| an `init/` seed script | `make reset` (they only run on a fresh volume) |

---

## House rules

1. **Pin versions.** `ethyca/fides` is pinned via `FIDES_IMAGE_TAG`; database
   images are pinned too. A demo that drifts is a demo that breaks.
2. **Secrets only in `.env`.** `.env` is gitignored; `.env.example` holds the
   local demo values and is committed. YAML gets `$VAR` references, never
   literals.
3. **Comment anything version-sensitive**, with the Fides docs link or source
   path. The existing config is full of these — they are the expensive knowledge
   in this repo.
4. **`make test` must pass** before you push.
5. **Never weaken a check to make something pass.** The provisioner's
   `verify=true` and reachability checks exist because every one of those
   failures is otherwise silent.
6. **Don't commit `fides_uploads/`.** Those are real access packages containing
   personal data. Already gitignored.

---

## Gotchas that have already cost time

Each of these fails *silently* or with a misleading error. All are commented at
the relevant place in the code.

| Symptom | Cause |
| --- | --- |
| `fides push` → `Not Authorized for this action` | The CLI reads a token from `~/.fides_credentials`; you must `fides user login` first. It does **not** use `[user]` in `fides.toml`. |
| Mongo DSAR → `not authorized on <key> to execute command` | For MongoDB the dataset `fides_key` **is** the database name; `defaultauthdb` only authenticates. |
| `Invalid privacy declaration referencing unknown DataUse` | The `data_use` isn't in the taxonomy. Check `GET /api/v1/data_use`. |
| Policy push → `UniqueViolation on ix_policy_drp_action` | `drp_action` is globally unique and Fides' built-in default policies already hold `access` and `deletion`. Omit it. |
| Erasure says complete, nothing changed | Missing `primary_key: True`, or the connection is `access: read`. |
| A collection returns nothing, ever | Unreachable: no `identity` and no `references` path. The provisioner's reachability check catches this. |
| Access request returns no data | `filtered-results` is test-request-only; `access-results` needs `security.subject_request_download_ui_enabled = true` and only returns the storage location. The gateway reads the package off disk. |
| Request stuck `in_processing` forever | `fides-worker` is down. `docker compose logs fides-worker`. |
| `KeyError: '…poll_reply_mailbox'` in the worker log | Harmless. Fides schedules a mailbox poll for a feature this deployment doesn't run. Filter it. |
| `ports are not available … address already in use` | Something owns that host port. `./scripts/bootstrap.sh` moves it for you. |
