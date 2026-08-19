# Deployment plan

How this repo gets onto Azure. [AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md) is the
*target design* — what maps to what, and why. This is the *execution order*: what
to do, in what sequence, and what has to be fixed before any of it works.

---

## 0. Decide what you are deploying, because it is two things

This repo contains two products at very different maturities:

| | State | Deployable? |
| --- | --- | --- |
| **DSAR demo** — Fides across app-postgres, app-mongo, app-mysql, Zoho CRM, plus the proof console at `/ui` | **Genuinely works.** One request, four engines, verified end to end by `scripts/acceptance.sh` | **Yes, today** |
| **DataShield CMS** — the DPDP consent product | Backend Phases 0–2 only: tenancy, RLS, auth, audit chain. Consent, DSAR, grievances, retention and reports are **frontend mock data** (ARCHITECTURE.md §9) | Signup and login are real; nothing else is |

**So this plan deploys a demo/pilot environment, not a customer-facing SaaS.**
That is worth being explicit about, because the two need different things: a demo
needs to be reachable, cheap and honest; a production SaaS needs HA, PITR, a WAF,
a DPA and Phases 3–9 actually built. Everything below is sized for the first and
grows into the second without being rebuilt.

If the goal is "put it in front of a customer this month" — that is Track A, and
this plan is it. If the goal is "start billing" — Phases 3–5 of the backend come
first, and no amount of infrastructure substitutes for them.

---

## Status

**Phase 0 (truth in the UI) and Phase 1 (pre-flight repo fixes) are done and
verified locally.** Section 1 below is kept as the record of what was wrong and
why each fix was made. Phases 2 onward are blocked on the four decisions in
[the brief's §3](#4-assumptions-i-made-flag-any-that-are-wrong) — domain, access
model, mock-module policy (settled: Option A), and the grievance officer.

One blocker was found by testing rather than reading, and it is the reason the
hard-reload check exists: the refresh cookie is scoped to `path=/v1/auth`, but
behind nginx the browser requests `/api/v1/auth/refresh`. The paths do not match,
so the cookie is never sent. Sign-in succeeded and the first reload signed the
user out, with nothing in any log. Fixed with a new `DS_EXTERNAL_PATH_PREFIX`
setting; verified by three consecutive hard reloads in a real browser.

Local entry point is now **http://localhost:8090** (nginx). Not 8080 — Fides
already holds that port locally; override with `WEB_PORT`.

---

## 0b. FIXED — nginx cached the API's IP (kept as the record of why)

Worth fixing before Phase 5, because nginx is the single public ingress and this
breaks the whole product rather than one feature.

nginx resolves an upstream hostname **once, at startup**, and caches the address.
Recreating `cms-backend` gives it a new container IP, and nginx keeps proxying to
the old one — every request through `/api` returns **502 until nginx is
restarted**. The backend is healthy the whole time, which is what makes it
confusing: `curl` directly against the API works, through the proxy it does not.

Reproduced by accident: `docker compose up -d --force-recreate cms-backend` while
adding the scheduler service.

**Fixed.** `frontend/nginx.conf.template` now proxies through an nginx variable
with an explicit `resolver`, and `DNS_RESOLVER` is overridable for Container Apps
and Kubernetes. Proved by parking a throwaway container on the old address to
force a real IP move: the backend went .14 -> .15 with nginx untouched and every
request stayed 200.

The fix was a `resolver` directive plus a variable upstream, so the name is
re-resolved per request:

```nginx
resolver 127.0.0.11 valid=10s;      # Docker's embedded DNS
set $api http://cms-backend:8100;   # a variable forces re-resolution
proxy_pass $api;
```

On Azure Container Apps the platform's own ingress handles this and the concern
largely disappears — but the compose stack is what the demo runs on, and "restart
nginx after every backend deploy" is a footgun nobody will remember.

---

## 1. Pre-flight: eight things that will break a deploy

These are repo fixes, not Azure work. Each one is cheap now and expensive after
infrastructure exists.

### 1.1 `SameSite=Strict` + two hostnames = auth silently dead — **blocker**

`auth.py:35` hardcodes `samesite="strict"`. If the frontend lands on
`something.azurestaticapps.net` and the API on `something.azurecontainerapps.io`,
those are different registrable domains, so the browser **will not send the
refresh cookie**. Login appears to work, then every reload signs the user out.
Nothing in the logs looks wrong.

**Fix: one public hostname.** The frontend container becomes the only public
ingress and reverse-proxies to the rest over the Container Apps internal network:

```
https://demo.<yourdomain>/           → cms-frontend (static assets)
https://demo.<yourdomain>/api/*      → cms-backend      (internal ingress)
https://demo.<yourdomain>/gateway/*  → fastapi-gateway  (internal ingress)
https://demo.<yourdomain>/fides/*    → fides admin UI   (internal, IP-restricted)
```

This keeps `SameSite=Strict` — the strongest CSRF posture — instead of weakening
it to `None` and then owing a CSRF token everywhere. It also removes the need for
Front Door on day one (~$35/mo saved). Front Door goes in front later when a WAF
is wanted; the routing does not change.

### 1.2 The frontend image is a Vite dev server — **blocker**

`frontend/Dockerfile` runs `npm run dev`. That is a development server: unminified,
single-process, HMR websocket open, and explicitly not for production use.

**Fix:** multi-stage → `npm run build` → serve `dist/` from nginx. The nginx stage
is also where the reverse-proxy config from 1.1 lives, so these two are one job.

### 1.3 Three services share a filesystem — **blocker on Container Apps**

`fides`, `fides-worker` (rw) and `fastapi-gateway` (ro) all mount the same
`./fides_uploads/` directory. The worker writes the access package; the gateway
reads it back to serve `GET /dsar/{id}`. Separate Container Apps do **not** share
a local volume.

**Fix:** an **Azure Files** share mounted into all three. (Blob Storage is the
better long-term answer, per AZURE_DEPLOYMENT.md, but that is a Fides storage-
destination change and a code change in the gateway — not day-one work.)

### 1.4 Migrations live in the compose command, not the image

`docker-compose.yml` runs `alembic upgrade head && uvicorn ...`. The image alone
does not migrate, and with more than one replica the replicas race each other.

**Fix:** a separate **Container Apps job** running `alembic upgrade head` with the
*owner* URL, run to completion before the app revision goes live. The app keeps
the restricted URL. This split already exists in config — it just needs to become
two deployment units.

### 1.5 The backend image ships a compiler and the test suite

The Dockerfile comment says build deps are "removed in the same layer"; only the
apt *lists* are removed. `build-essential` stays, and `pip install -e ".[dev]"`
puts pytest and friends in the runtime image.

**Fix:** multi-stage — compile argon2-cffi in a builder, copy the venv into a slim
runtime, install without `[dev]`.

### 1.6 `presidio/dockerfile` is lowercase

Works on macOS (case-insensitive filesystem). Fails on any Linux CI runner or
`docker build` that expects `Dockerfile`. One `git mv`.

### 1.7 Nineteen secrets are in `.env`

`.env` is correctly gitignored and stays local — **it is not used on Azure at
all.** Every one of these goes to Key Vault and is referenced by the container
apps via managed identity:

```
FIDES_APP_ENCRYPTION_KEY   FIDES_OAUTH_ROOT_CLIENT_SECRET   FIDES_ROOT_PASSWORD
FIDES_DB_PASSWORD          FIDES_REDIS_PASSWORD             APP_POSTGRES_PASSWORD
APP_MONGO_ROOT_PASSWORD    APP_MONGO_PASSWORD               APP_MYSQL_ROOT_PASSWORD
APP_MYSQL_PASSWORD         CMS_DB_ROOT_PASSWORD             DS_JWT_SECRET
DS_AUDIT_HMAC_KEY          ZOHO_CRM_CLIENT_ID               ZOHO_CRM_CLIENT_SECRET
ZOHO_CRM_REFRESH_TOKEN     FIDES_OAUTH_ROOT_CLIENT_ID       ACCESS_POLICY_KEY
ERASURE_POLICY_KEY
```

**Generate new values for Azure.** Do not lift the local ones — a secret that has
lived in a developer `.env` is not a production secret. `DS_AUDIT_HMAC_KEY`
especially: it is what stops someone with database write access forging history,
and rotating it later invalidates the existing chain.

### 1.8 `DS_ENV=prod` turns on guardrails that will reject a sloppy config

By design, config refuses to boot on placeholder secrets, `debug`, insecure
cookies, a wildcard CORS origin, or `db_ssl_mode=disable`. Expect the first
deploy to fail here — that is the feature working. Set `DS_CORS_ORIGINS` to the
single real origin from 1.1.

---

## 2. Phases

Each phase is independently verifiable. Do not start the next until the check passes.

### Phase 1 — Pre-flight fixes (repo only, no Azure)
Everything in §1. **Check:** `docker compose up` still works locally, the
frontend nginx image serves the built app, and `scripts/acceptance.sh` still passes.

### Phase 2 — Infrastructure as code
One Bicep deployment into the existing `rg-datashield`:

| Resource | SKU | Why |
| --- | --- | --- |
| Container Registry | Basic | four of our own images |
| Key Vault | Standard | the 19 secrets, + RBAC for managed identities |
| Container Apps Environment | Consumption | the compute plane |
| Storage Account + File share | Standard LRS | the shared `fides_uploads` (§1.3) |
| Log Analytics workspace | Pay-as-you-go | logs are already structured JSON with a request id |
| Azure Cache for Redis | Basic C0 | holds in-flight DSAR jobs — losing it loses work |
| *(existing)* PostgreSQL Flexible Server 16 | Burstable B1ms | already provisioned; name in `backend/.env.azure` |

**Check:** `az deployment group what-if` is clean, then apply; every resource
exists and Key Vault RBAC resolves.

### Phase 3 — Images
Build and push four images to ACR: `cms-backend`, `cms-frontend`, `fastapi-gateway`,
`presidio`. Tag with the **git SHA**, not `latest` — a rollback needs a name to
roll back *to*. The three vendor images (`ethyca/fides`, postgres, mongo, mysql,
redis) are pulled, not built.

**Check:** `az acr repository show-tags` lists all four at the expected SHA.

### Phase 4 — Data layer
On the existing Postgres server: create the `fides` database alongside
`datashield`; run `scripts_bootstrap_azure.sql` for it. Then run the Alembic
migration job (§1.4). Then run `backend/scripts/verify_database.py` — the
14-check deployment gate.

**Check:** `verify_database.py` exits 0. It is the gate; if it fails, stop.

### Phase 5 — Deploy, in dependency order
1. `app-postgres`, `app-mongo`, `app-mysql` — internal ingress, seeded from their
   init scripts on boot
2. `fides-redis` → Azure Cache
3. `fides` (internal), then `fides-worker` with **`min-replicas: 1`** — scale-to-zero
   here is a correctness bug: a queued DSAR with no worker sits in `in_processing`
   forever
4. `fides-provisioner` as a run-once job — it already exits 0 cleanly
5. `cms-backend` (internal), `fastapi-gateway` (internal)
6. `cms-frontend` — **external ingress, the only public one**

**Check:** `GET /gateway/health` through the public hostname reports all four
datastores plus Zoho.

### Phase 6 — Domain, TLS, verification
Custom domain on the frontend app with a Container Apps managed certificate
(free, auto-renewing). Then the real test:

```bash
GATEWAY_URL=https://demo.<yourdomain>/gateway ./scripts/acceptance.sh
```

This needs a small change — the script currently hardcodes `localhost:$GATEWAY_PORT`.
Making it take a URL is worth doing regardless: **the deployment is then verified
by the same suite that verifies local**, including the named-datastore assertions
added in the last commit. A deploy that cannot run the acceptance suite is a
deploy nobody has actually checked.

**Check:** the suite passes against the deployed URL, and signup → login → reload
works in a real browser (this is what proves §1.1).

### Phase 7 — Operations
Log Analytics alerts on backend 5xx and on `fides-worker` replica count hitting
zero. Confirm the 7-day PITR window on Postgres. Restrict the Fides admin route
to office IPs. Record the monthly cost.

---

## 3. Cost, roughly

Central India, demo-sized, USD/month:

| | |
| --- | --- |
| PostgreSQL B1ms 32 GB (existing) | ~$14 |
| Container Apps (consumption, most scale-to-zero, worker pinned at 1) | ~$35–55 |
| Azure Cache for Redis Basic C0 | ~$16 |
| Container Registry Basic | ~$5 |
| Storage + Key Vault + Log Analytics | ~$8 |
| **Total** | **~$80–100** |

Front Door Standard would add ~$35. Deferred — it buys a WAF, not reachability,
and the single-ingress design in §1.1 does not change when it is added.

The two levers if this needs to be cheaper: drop Redis to a container (accepting
that a restart loses in-flight DSARs — acceptable for a demo), and let the demo
datastores scale to zero.

---

## 4. Assumptions I made, flag any that are wrong

- **A demo/pilot environment, not production.** Single region, no HA, no
  read replica. Justified in §0.
- **One environment to start.** Not dev + staging + prod — there is one team and
  no customers yet. The Bicep is parameterised so a second is a parameter file.
- **The demo datastores re-seed on boot** rather than persisting. For a demo this
  is a feature: every showing starts from a known state. It does mean data written
  through the console is lost on restart.
- **Zoho stays optional.** Without credentials it reports `not configured`, the
  console shows db4 amber, everything else works. Ship it that way and add
  credentials when there is a reason to.
- **Presidio is deployed but unwired**, at min-replicas 0 so it costs nothing.
  It is in the plan only so it does not get lost.
- **`.env` stays local and untouched.** Azure reads from Key Vault.

---

## 5. What would change for real production

Not now, but so the shape is known: Postgres to General Purpose with HA and a
35-day PITR window; Front Door + WAF; the demo datastores deleted entirely (a
customer's data lives in *their* systems, which is the whole premise); Blob
Storage with presigned URLs instead of the Azure Files share; field-level
encryption and WORM anchoring for the audit chain (Phase 10); and backend Phases
3–9 built, because that is the product.
