# Azure go-live runbook

> **DEPLOYED.** The product is live at
> `https://cms-frontend.salmonriver-3b8dcd32.centralindia.azurecontainerapps.io`
>
> What is actually running, and what the plan below got wrong, is recorded in
> **[the deployment record](#deployment-record)** at the end. Read that before
> following the steps — three of them changed on contact with Azure.

The concrete plan for getting **the DataShield product** onto a URL, for the shape
that was actually chosen. [AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md) is the
architecture and the reasoning behind the service mapping; this is the ordered
list of things to do, and the things that will stop you if you do them out of
order.

[DEPLOYMENT_PLAN.md](DEPLOYMENT_PLAN.md) predates the modules being built. Its §0
describes consent, DSAR, grievances, retention and reports as frontend mock data
with only signup and login real. **That is no longer true** — all twelve modules
are live against Postgres, the mocks are deleted, and there are 436 backend tests.
Read its §1 for the pre-flight fixes, which are still accurate and still done, and
ignore its framing of what the product is.

---

## Decisions taken

| | Chosen | Consequence |
| --- | --- | --- |
| **Scope** | Product only — no Fides engine | All twelve modules work. A DSAR is filed, tracked, deadline-enforced and worked by staff; it is not dispatched to an engine that erases across four datastores. See "What a DSAR does without the engine" below. |
| **Hostnames** | Azure defaults, Azure-managed TLS | No Front Door, no WAF, no DNS work. |
| **Access** | Open registration | Anyone with the URL can create a workspace. Risks named below — read them, they are not theoretical. |

---

## What is already in Azure

Verified against the live subscription, not from memory:

| Resource | State | Verdict |
| --- | --- | --- |
| PostgreSQL Flexible Server | **v16**, Burstable B1ms, 32 GB, HA disabled, 7-day backup, public access on | Right version — v16 is the one that makes `NOBYPASSRLS` the default, which the whole isolation model rests on. Correct dev/demo SKU. |
| `datashield` database | exists | Migration state unverified — could not connect, see the firewall row |
| Firewall | one rule, pinned to a single IP | **Stale.** That address is not the current egress IP. This is why nothing local can reach it. |
| Azure Communication Services | Email service + **AzureManagedDomain: Verified, SPF Verified, DKIM Verified**, data location India | Better than expected. Real email is reachable without any DNS work. |
| Log Analytics workspace | exists | Reuse |
| Application Insights + action group | exists | Reuse |
| `marketing-site` App Service | Node 22, running | **A different project.** Not part of this. Do not touch it. |

Not provisioned, and needed: **Container Registry**, **Container Apps
environment**, **Key Vault**. That is the whole gap for this scope.

---

## Three things that must be fixed before any deploy

These are not cleanup. Each one either prevents boot or breaks sign-in.

### 1. `DS_ENV=prod` will refuse to boot — by design

`_prod_guardrails` in `app/core/config.py` raises if `notification_provider` is
`console`:

> In prod, "logged instead of sent" means a statutory notification silently did
> not happen. Better to refuse to boot than to appear to be notifying people.

That guardrail is right, and it puts a real decision in front of the deploy rather
than after it. But note what it actually checks: **the provider's name, not whether
the provider can send.** Setting `azure_acs` satisfies it — and the ACS provider is
unfinished:

```python
# TODO(acs): sign with the shared key — this raises a clear, non-retryable
# error until that is done, instead of silently appearing to send.
```

So `DS_ENV=prod` + `azure_acs` boots cleanly and then fails every notification
permanently. The delivery log would show them all failed, which is at least
honest, but nothing would reach anybody.

**Do this:** implement ACS request signing. It is a contained piece of work —
HMAC-SHA256 over a canonical string with `hmac`, `hashlib` and `base64` from the
standard library, roughly thirty lines, no new dependency. The endpoint and access
key go in Key Vault as `DS_ACS_ENDPOINT` and `DS_ACS_ACCESS_KEY`; the sender is the
verified `AzureManagedDomain` address.

This closes the `TODO(acs)` caveat and turns the notifications module from
"logged" into "sent", which is the difference between demonstrating a statutory
obligation and describing one.

*Fallback if you want a URL today:* deploy with `DS_ENV=staging` and the console
provider. Notifications land in the log, the guardrail stays out of the way, and
the notifications screen keeps telling the truth via `sends_real_messages: false`.
Do not call that configuration production.

### 2. The frontend must be the nginx container, not Static Web Apps

The refresh cookie is `HttpOnly; Secure; SameSite=Strict`, and `samesite="strict"`
is hardcoded. Static Web Apps would put the frontend on
`*.azurestaticapps.net` and the backend on `*.azurecontainerapps.io` — two
different sites, so the browser would never send the cookie on a refresh.

That exact bug has already been paid for once here. From DEPLOYMENT_PLAN.md's
record: the cookie was scoped to `path=/v1/auth` while the browser requested
`/api/v1/auth/refresh`, sign-in succeeded, the first reload signed the user out,
**and nothing appeared in any log.** It was found by hard-reloading in a real
browser, not by reading code.

`frontend/nginx.conf.template` already solves this properly: it serves the bundle
and reverse-proxies `/api/` to the backend from one origin. Deploy that image to
Container Apps and there is one hostname, one site, and the cookie works.
`AZURE_DEPLOYMENT.md` offers "Static Web Apps (or Container Apps)" — the
parenthetical is the correct branch, and this is why.

### 3. `DNS_RESOLVER` has no valid default in Container Apps

The template proxies through a variable so nginx re-resolves the backend per
request instead of caching its IP at startup — a fix made after recreating the
backend 502'd the whole site. A variable `proxy_pass` needs an explicit
`resolver`, and the default is `127.0.0.11`, which is **Docker's** embedded DNS.
It does not exist in Container Apps. nginx would fail to resolve anything.

**Do this:** have the entrypoint read the first nameserver out of
`/etc/resolv.conf` and use that, falling back to the current default. Reading it
at runtime is correct in every environment; hardcoding a guess at the Container
Apps resolver IP is not, because it is not a documented stable value.

Also set `GATEWAY_URL` to something harmless. Without the engine, `/gateway/`
has no upstream. Because the template uses a variable `proxy_pass`, nginx still
starts and only that path 502s — acceptable, but decide it deliberately rather
than discovering it.

---

## Build order

Each step ends in something you can check. Do not proceed past a failed check.

### Step 0 — Reachability

```bash
az postgres flexible-server firewall-rule create \
  -g rg-datashield -s <server> -n dev-$(date +%Y%m%d) \
  --start-ip-address "$(curl -s https://api.ipify.org)" \
  --end-ip-address   "$(curl -s https://api.ipify.org)"
```

That home IP is dynamic; it has already moved once. It is a stopgap for
administering the server from a laptop, not part of the running system — the
application will reach Postgres from inside the Container Apps environment.

**Check:** `psql` connects and `SELECT version()` reports 16.

### Step 1 — Confirm the database is actually ready

The gate the architecture doc calls the gate, and it is the right one:

```bash
cd backend && set -a && . ./.env.azure && set +a
python3 scripts/verify_database.py
alembic upgrade head
```

`make azure-verify-db` is currently broken — it sources `backend/.env`, which does
not exist; the Azure values live in `backend/.env.azure`. Fix the target or run it
by hand.

**Check:** two roles present, `datashield_app` is `NOBYPASSRLS` and not the table
owner, RLS is forced, the audit trigger rejects an UPDATE, and `alembic_version`
is at head.

Then the part that matters most: **run the isolation tests against Azure.** That
suite is the only evidence tenant isolation survived the move to a managed server
where nobody has superuser. `test_isolation.py` and `test_self_service.py`
together cover cross-tenant reads and the own-principal scoping.

### Step 2 — Registry, Key Vault, images

```bash
az acr create -g rg-datashield -n <acr> --sku Basic --admin-enabled false
az keyvault create -g rg-datashield -n <kv> --enable-rbac-authorization true
```

Into Key Vault: `DS_JWT_SECRET`, `DS_AUDIT_HMAC_KEY`, the two database URLs, and
the ACS endpoint and key. The audit HMAC key especially — it is the thing that
stops somebody with database write access forging history, so it must not sit in
a container's environment block in the portal.

Build both images for **linux/amd64** (a laptop on Apple silicon defaults to arm64
and Container Apps will not run it):

```bash
az acr build -r <acr> --platform linux/amd64 -t datashield-backend:$(git rev-parse --short HEAD) ./backend
az acr build -r <acr> --platform linux/amd64 -t datashield-frontend:$(git rev-parse --short HEAD) \
  --build-arg VITE_BUILD_SHA=$(git rev-parse --short HEAD) ./frontend
```

Tag by commit, not `latest`. `latest` makes "what is running" unanswerable, and
the frontend stamps its own commit into the bundle so the two should agree.

**Check:** both images pull and `docker run` locally against the Azure database.

### Step 3 — Migrations as their own step

Alembic runs as a **pre-deploy job**, not in the app's startup path. Two replicas
racing `upgrade head` is a corrupted migration state, and a migration that fails
should stop the deploy rather than crash-loop a container that is already serving.

```bash
az containerapp job create -g rg-datashield -n migrate \
  --environment <env> --trigger-type Manual --replica-timeout 600 \
  --image <acr>.azurecr.io/datashield-backend:<sha> \
  --command alembic upgrade head
```

**Check:** the job exits 0, and `alembic_version` matches the revision in the
image.

### Step 4 — Backend

Container Apps, **internal ingress only**. Nothing but the frontend needs to reach
it, and internal ingress means the API is not independently addressable — which
also removes any temptation to point a browser at it directly and reintroduce the
two-hostname cookie problem.

Environment: `DS_ENV`, `DS_DEBUG=false`, `DS_DB_SSL_MODE=require`,
`DS_COOKIE_SECURE=true`, `DS_EXTERNAL_PATH_PREFIX=/api`, `DS_CORS_ORIGINS` set to
the frontend's exact origin (never `*` — the prod guardrail rejects it), secrets
as Key Vault references via managed identity.

`DS_EXTERNAL_PATH_PREFIX=/api` is not optional. It is what makes the refresh
cookie's path match what the browser actually requests through nginx.

**Check:** from a shell in the environment, `/health` returns 200 and
`/v1/auth/workspace-available?workspace=x` returns JSON.

### Step 5 — Scheduler

A second container app from the same image running `python -m app.worker`, with
**min-replicas 1**. Three jobs live here: `notifications.drain` every 60s,
`grievance.escalate` every 900s, `retention.prepurge_warn` daily.

Scale-to-zero here is a correctness bug, not a saving. Nothing would drain the
notification queue, and nothing would escalate an overdue complaint — the product
would silently stop meeting the deadlines it displays.

Use `healthcheck_worker.py`, not an HTTP probe. The worker serves no HTTP and an
HTTP probe reports it permanently unhealthy; that is already a known false alarm
locally.

**Check:** `GET /v1/admin/jobs` shows three jobs with recent heartbeats and no
stale flag.

### Step 6 — Frontend, and the only public ingress

External ingress, `targetPort 8080`, `BACKEND_URL` set to the backend's internal
FQDN, `DNS_RESOLVER` from step 3 of the fixes above.

**Check, in a real browser, not with curl:**

1. Register a workspace. 2. **Hard-reload three times** — you must stay signed in.
   That is the cookie test, and it is the one that has failed before with nothing
   in any log. 3. Sign in as a data principal and confirm the Preference Centre
   and Consent History load. Those two were 403 for that role until recently, so
   they are worth re-checking on a new deployment. 4. Confirm the footer shows the
   real Grievance Officer and the header the real organisation name.

### Step 7 — Prove it works, with data

```bash
./scripts/seed_demo.py --base-url https://<frontend-fqdn>/api --register "Demo Company"
```

The seeder drives the same HTTP API a browser does, so it doubles as an
end-to-end test of the deployment: twelve principals, a consent ledger with
withdrawals, eight rights requests, five complaints in five states with one
overdue and escalated, four retention policies, two breaches.

The backdate pass will be skipped — it shells out to `docker compose exec`, which
is not how you reach Azure Postgres. Everything will be dated today, so nothing
will be overdue. Either accept that, or point the pass at the Azure server.

**Check:** `POST /v1/audit/verify` returns `ok: true`, then send yourself a real
notification and confirm it arrives. That last one is the ACS work paying off, and
it is the only proof that matters for it.

### Step 8 — Alerts

App Insights is already provisioned and the logs are already structured JSON with
a request id, so they land queryable with no code change.

The alerts worth having are about obligations, not CPU: **a DSAR approaching its
30-day statutory deadline**, a grievance past its 15-day one, notification
failures above a threshold, and the scheduler heartbeat going stale.

---

## What a DSAR does without the engine

Worth being precise, because "DSAR" now means something narrower.

**Works:** a person raises a request; it gets a reference and a statutory deadline;
staff move it through `verifying → in_progress → completed`; every transition is
audited; the deadline is enforced and shown; an overdue request is visible; a
rejection demands a written reason.

**Does not:** automatically collect or erase that person's data across Postgres,
Mongo, MySQL and Zoho. The backend posts to `gateway_url` and, with nothing there,
records the failure on the request's timeline and leaves it at `received` for
retry. It degrades honestly — but a request sitting at `received` with a dispatch
error is what a demo viewer would see, so either work them by hand or say plainly
that execution is the customer's own integration.

Adding the engine later is additive: stand up the gateway with internal ingress
and point `gateway_url` at it. Requests already in `received` can be retried,
because that is exactly what the retry path is for.

---

## Risks of open registration, stated plainly

You chose open registration, which is what the signup flow is built for. Three
things follow, and none of them is a reason to change the decision — they are
things to know:

1. **Rate limiting is weaker than it looks.** `app/core/throttle.py` says so
   itself: it is an in-process sliding window and *not a security boundary*. On
   Container Apps it is per-replica, so it gets weaker as you scale out. With no
   WAF in this shape, there is nothing in front of it. Set a modest max-replica
   count so the ceiling is known.
2. **Your ACS quota is spendable by strangers.** Every workspace someone creates
   can send notifications on your subscription's allowance.
3. **You become a data fiduciary for whatever they put in.** Someone will type
   real personal data into a demo. The database has 7-day backups, no HA, and no
   field-level encryption. That is a fine posture for a demo and not one for
   somebody's actual customer list.

If any of that stops being acceptable, the change is one command — Container Apps
ingress supports IP restrictions, and it is reversible.

---

## Cost

| | Monthly, roughly |
| --- | --- |
| PostgreSQL B1ms, 32 GB, no HA | already running |
| Container Apps — backend, scheduler (min 1), frontend | low tens |
| Container Registry Basic | ~$5 |
| Key Vault | negligible |
| Log Analytics / App Insights | already running; watch ingestion volume |
| ACS email | per-message, negligible at demo volume |

The scheduler at min-replicas 1 is the one thing that cannot scale to zero, so it
is the floor. Verify in the pricing calculator for Central India — India region
pricing differs from US, and SKU prices move.

---

## Explicitly not in this deployment

Front Door, WAF, custom domain, zone-redundant HA, read replicas, Redis, Blob
Storage, private endpoints, the Fides engine, the three prop datastores, Presidio,
and staff SSO. Each is a deliberate omission for a demo on default hostnames, and
each is in [AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md) for when it is not a demo.

Still open regardless of deployment: `consent_guardian` remains the one preview
module — verifiable parental consent under §9 needs a real identity check, and a
publishable key in a browser cannot perform one. No PDF export, no grievance
attachments, SMS unimplemented, and the audit chain is verifiable but not
externally anchored.


---

# Deployment record

What was built, and the four things the plan above did not anticipate.

## Running

| | |
| --- | --- |
| **URL** | `https://cms-frontend.salmonriver-3b8dcd32.centralindia.azurecontainerapps.io` |
| `cms-frontend` | nginx, external ingress, the only public surface |
| `cms-backend` | internal ingress only, min 1 / max 3 |
| `cms-scheduler` | min-replicas 1, `python -m app.worker`, three jobs succeeding |
| `datashield-pg-5b8c16` | PostgreSQL 16, **public access Disabled**, reached over a private endpoint |
| `cae-datashield` | Container Apps environment, VNet-joined |
| `kv-ds-c20b5d2c` | 5 secrets, read via user-assigned identity |
| `acrdatashieldc20b5d2c` | both images, tagged by commit |
| ACS `dpdpa` | email domain linked and verified; provider reports `sends_real_messages: true` |

Verified: 34 isolation, self-service, audit-chain and auth tests against Azure
Postgres; all 25 tenant-scoped tables RLS-forced; a real email delivered; a
workspace seeded through the public URL; and the refresh cookie surviving three
consecutive `/auth/refresh` calls with `path=/api/v1/auth`, `HttpOnly`, `Secure`,
`SameSite=Strict`.

## What the plan got wrong

**1. Container Apps could not reach Postgres, and allow-listing IPs did not fix
it.** The plan assumed the firewall was a one-line step. The environment's
reported `outboundIpAddresses` were added and connections still timed out —
consumption-environment egress comes from a pool that is not fully enumerated by
that field. Chasing individual addresses would have produced something that broke
silently later. The environment was rebuilt VNet-joined with a private endpoint
and private DNS zone for Postgres, and public access is now off entirely. A
Container Apps environment cannot be VNet-joined after creation, so this meant
deleting and recreating it and both apps.

**Consequence worth knowing:** no laptop can reach that database any more,
including for `make azure-verify-db` and `alembic upgrade head`. Migrations and
verification now need to run *inside* the VNet — as a Container Apps job, which is
the pattern the plan already specified for migrations and should now be treated as
the only way in.

**2. nginx sent the wrong `Host` header.** Container Apps routes internal ingress
by Host, so `proxy_set_header Host $host` handed the backend's endpoint the
*frontend's* hostname; the platform found no app by that name and returned its own
"Container App - Unavailable" page. Every `/api/` call 404'd with HTML. Invisible
locally, because uvicorn does not care what Host says. Fixed to `$proxy_host` in
all three proxy blocks, with the original preserved in `X-Forwarded-Host`.

**3. Proxying to internal https needed SNI.** Internal ingress redirects http to
https and puts the internal FQDN in `Location`, which a browser cannot resolve. So
the frontend proxies to https — and that returned 502, because nginx does not send
SNI by default and the platform routes on it. `proxy_ssl_server_name on` fixed it.

**4. `DNS_RESOLVER` was solved by the base image.** The plan proposed reading
`/etc/resolv.conf` in a custom entrypoint. Unnecessary: nginx already ships
`15-local-resolvers.envsh`, which does exactly that and is sourced before template
substitution. It only needed `NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1`.

## Still true, and still open

`consent_guardian` is the one preview module. No PDF export, no grievance
attachments, SMS unimplemented, and the audit chain is verifiable but not
externally anchored. There is no CI pipeline — both images were built with
`az acr build` by hand, and migrations were applied from a laptop while public
access was still on. The DSAR engine is not deployed, so a rights request is
filed, tracked and worked by staff but not executed across datastores.

Registration is open, so anyone with the URL can create a workspace. The rate
limiter is per-replica and says of itself that it is not a security boundary, and
there is no WAF in this shape.


## A deploy verification trap, learned the hard way

`az containerapp update` reports **Succeeded** when the control-plane operation
succeeds — not when the new revision is serving. In single-revision mode, if the
new revision never becomes ready, Container Apps keeps the previous one serving
traffic and the app stays up on the old code.

That happened here twice, and hid two things at once. Setting
`DS_CORS_ORIGINS=https://...` as a bare string crash-looped every new revision,
because the field is `list[str]` and pydantic-settings needs JSON
(`["https://..."]`). "Succeeded" was printed, the app was probed and answered
200 — from the old revision — so the misconfiguration went unnoticed, and the
next image rollout appeared to do nothing because its revision could not start
either.

**So a deploy is not verified by the app answering.** After every update, check
that exactly one revision is active, that its image is the tag you just pushed,
and that its health is `Healthy`:

```bash
az containerapp revision list -g rg-datashield -n <app> \
  --query "[?properties.active].{rev:name, image:properties.template.containers[0].image, health:properties.healthState}" -o table
```

Two active revisions, or an active one on the old tag, means the rollout did not
take — whatever the update command said.


---

# CI/CD

`.github/workflows/ci.yml` on every push and pull request;
`.github/workflows/deploy.yml` on pushes to `main` and on demand.

## No secret in the repo

The repo is public, so nothing that grants access can live in it. Azure trusts a
short-lived OIDC token GitHub mints for this repository, exchanged through a
federated credential on the `id-github-deploy` identity. There is no client
secret. A fork cannot obtain the token: the credential pins the subject, and the
workflow's `environment: production` is what makes GitHub mint one matching it.

`AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and `AZURE_SUBSCRIPTION_ID` are stored as
repository secrets. They are identifiers, not credentials — they grant nothing
without the federated trust; they are secrets only to keep the subscription id
out of public logs.

**The subject has numeric ids in it, and that is correct:**

```
repo:Samyakjain2052@114948120/fides@1316194005:environment:production
```

`GET /repos/.../actions/oidc/customization/sub` reports `use_default: true` with
an id-qualified `sub_claim_prefix`. The first attempt used the documented
name-based subject and failed with `AADSTS700213: No matching federated identity
record`. Those ids survive a rename, so matching them is more durable than
matching the names — but a credential written from the documentation will not
work here. If login ever fails with AADSTS700213, read the subject the workflow
log prints and match it exactly.

## What the deploy identity can do

Contributor on the registry, the three container apps, and the migration job.
**Not on the resource group.** It cannot read Key Vault, reach Postgres, or
change the network. A compromised workflow can deploy a bad image; it cannot
exfiltrate the database.

Adding a new deployable resource means adding a scope. The first run failed with
`the containerapps job 'cms-migrate' does not exist` — which is how an
unauthorised resource looks to a scoped identity, not a missing resource.

## Order, and why migrations come first

Migrations run as a Container Apps job before any app is updated. Not a
preference: Postgres has public access disabled and is reachable only over a
private endpoint, so **no GitHub runner can reach it**, and inside the VNet is
the only way in. A failed migration also stops the deploy rather than
crash-looping something already serving.

## The step that earns its keep

`Verify the rollout actually took` asserts exactly one active revision, on the
tag just pushed, reporting `Healthy` — because `az containerapp update` reports
success when the control-plane call succeeds, not when the new revision serves.
See the trap section above for how that hid a broken config while the site
answered 200 from the previous revision. The smoke test then hits `/api/health`,
which exercises nginx, internal ingress, the backend and the private endpoint in
one request; `/healthz` alone would only prove nginx serves files.

## Two things that only fail in CI

**`docker compose run <service> <cmd>` replaces the service's command.** CI first
ran `docker compose run --rm cms-test pytest -q`, which dropped the service's
own `alembic upgrade head &&` prefix, and all 448 tests failed with
`relation "audit_events" does not exist`. It passes on a laptop, where the test
database is already at head from an earlier run. CI passes no command.

**`scripts_bootstrap_test_db.sql` was never mounted.** Its header always said it
runs from `docker-entrypoint-initdb.d`, but the volume line did not exist, so
`datashield_test` only existed where somebody had run it by hand. A fresh clone
could not run the suite at all. Now mounted as `01_test_db.sql`, after
`00_roles.sql`, whose roles it grants to.
