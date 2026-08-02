# Azure deployment architecture

How the 11 local services map onto Azure, following
[ADR 0001](adr/0001-postgres-only-for-product-data.md): **PostgreSQL for all
product data, the two demo databases stay containerised.**

Nothing here is provisioned yet. This is the design to build Bicep/Terraform from.

---

## The one-line rule

> **If it is evidence or customer account data → managed PostgreSQL.
> If it is a prop or a stateless process → a container.**

Everything below follows from that.

---

## Service mapping

| Local service | Azure | Why this, and not something else |
| --- | --- | --- |
| **cms-db** | **Azure Database for PostgreSQL Flexible Server 16** | Our data. Needs RLS, advisory locks, real constraints, PITR. **Pin 16** — see the version note below. |
| **fides-db** | *same server, second database* | Fides requires Postgres. Different load profile, so it may earn its own server later — not yet. |
| **cms-backend** | **Container Apps** | Stateless HTTP, scale-to-zero on dev, HTTP-driven autoscale in prod. |
| **cms-frontend** | **Static Web Apps** (or Container Apps) | It's a Vite build — static assets + a CDN is cheaper and faster than a container serving files. |
| **fastapi-gateway** | **Container Apps** | Stateless. Internal ingress only; not exposed publicly. |
| **fides** | **Container Apps** | The vendor image, unmodified. |
| **fides-worker** | **Container Apps job / min-replicas 1** | Must **not** scale to zero — a queued DSAR with no worker sits in `in_processing` forever. This is the one service where scale-to-zero is a correctness bug. |
| **fides-redis** | **Azure Cache for Redis (Basic C0)** | Real infrastructure: it holds in-flight DSAR jobs. Losing it loses work. Cheap enough that a container isn't worth the risk. |
| **fides-provisioner** | **Container Apps job**, run once on deploy | Already designed as one-shot, exits 0. Maps cleanly to a job with a completion policy. |
| **app-postgres** | **container** (Container Apps) — dev/demo only | A prop. No HA, no PITR, no managed anything. Per ADR 0001. |
| **app-mongo** | **container** (Container Apps) — dev/demo only | Same. **Not Cosmos DB** — see the risk note. |
| `./fides_uploads/` | **Blob Storage** + private container | Today it's a bind mount, which Fides itself documents as test-only. Blob gives presigned URLs and a lifecycle policy. |
| `.env` secrets | **Key Vault** + managed identity | `DS_JWT_SECRET` and `DS_AUDIT_HMAC_KEY` must live here. The audit key especially: it is what stops someone with database write access forging history. |
| — | **Front Door + WAF** | TLS termination, HSTS, rate limiting at the edge. |
| — | **Container Registry (Basic)** | Our three images. |
| — | **Log Analytics + App Insights** | Our logs are already structured JSON with a request id — they land queryable with no code change. |
| — | **Entra ID** | Staff SSO later (`external_idp` is already reserved on the user model), and Postgres auth now. |

---

## Topology

```
                        Internet
                           │
                  ┌────────▼────────┐
                  │  Front Door +   │  TLS · HSTS · WAF · rate limit
                  │      WAF        │
                  └────┬───────┬────┘
                       │       │
        ┌──────────────▼─┐   ┌─▼────────────────────────────┐
        │ Static Web App │   │  Container Apps environment  │
        │  cms-frontend  │   │  ┌────────────────────────┐  │
        └────────────────┘   │  │ cms-backend  (public)  │  │
                             │  │ fastapi-gateway (int.) │  │
                             │  │ fides           (int.) │  │
                             │  │ fides-worker  min 1 ✱  │  │
                             │  │ ─ dev/demo only ─      │  │
                             │  │ app-postgres, app-mongo│  │
                             │  └───────┬────────────────┘  │
                             └──────────┼───────────────────┘
                                        │  VNet, private endpoints
        ┌───────────────────────────────┼──────────────────────────┐
        │                               │                          │
┌───────▼─────────────┐    ┌────────────▼──────┐    ┌──────────────▼──┐
│ PostgreSQL Flexible │    │ Cache for Redis   │    │  Blob Storage   │
│ Server 16           │    │ Basic C0          │    │  DSAR packages  │
│  ├ datashield  ←ours│    │ DSR job queue     │    └─────────────────┘
│  └ fides            │    └───────────────────┘
│ zone-redundant HA   │              ┌──────────────────┐
│ PITR · Entra auth   │              │    Key Vault     │
└─────────────────────┘              │ JWT + audit HMAC │
                                     └──────────────────┘
   ✱ fides-worker must never scale to zero
```

---

## Two version details that decide whether this works

**1. PostgreSQL 16, not 15.** Our isolation model requires the app to connect as a
role that is `NOBYPASSRLS` and is *not* the table owner. Azure's admin has no
superuser, so this is not automatic:

- **PG 16+** — Azure follows standard PostgreSQL role behaviour, `azure_pg_admin`
  can manage roles, and **`NOBYPASSRLS` is the default**. Exactly what
  `datashield_app` needs.
- **PG 15 and earlier** — real limitations, because the admin role lacks superuser.

So pin 16 on purpose, not by accident.

**2. PgBouncer transaction pooling is safe for us — by luck of an earlier decision.**
Flexible Server ships PgBouncer. In *transaction* pooling mode, a session-level
`SET app.tenant_id` would leak one tenant's context onto the next request sharing
that connection — a cross-tenant breach *introduced by the pooler*. We used
`SET LOCAL`, which is transaction-scoped, so the design survives unchanged. Worth
knowing before someone "simplifies" it.

---

## Region and residency

**Central India** (primary), **South India** (paired, for geo-backup). Data
residency is a real DPDP consideration and will end up as a contract term with
enterprise customers, so pick the region deliberately and write it down.

---

## What changes in our code

Small, and mostly configuration — the application logic is untouched:

| Change | Where | Note |
| --- | --- | --- |
| `sslmode=require` | `DS_DATABASE_URL` | asyncpg needs an SSL context; Azure rejects plaintext |
| Bootstrap SQL runs as `azure_pg_admin`, not a superuser | `backend/scripts_bootstrap.sql` | **Smoke-test this first.** `CREATE ROLE` should work; verify `ALTER DATABASE … OWNER TO` does too |
| Alembic as a pre-deploy job | pipeline | Not in the app container. Migration must complete before the new revision serves traffic |
| Secrets from Key Vault | `app/core/config.py` | Env vars injected by Container Apps' Key Vault reference — config code unchanged |
| Fides storage → `azure` or `s3` | `fides-config/provision/provision.py` | Replaces the `local` destination, which Fides documents as test-only. The gateway's read-the-package-off-disk workaround then goes away |
| Optional: managed identity for Postgres | connection setup | Removes the DB password entirely. Note Entra-integrated roles are created without BYPASS RLS — which is what we want |

`FORCE ROW LEVEL SECURITY`, the audit trigger and the grants are all standard
PostgreSQL and carry over untouched.

---

## Rough cost shape

Order of magnitude only — verify in the Azure pricing calculator for your region,
because SKU pricing moves and India regions differ from US.

| Environment | Shape | Rough monthly |
| --- | --- | --- |
| **Dev / demo** | PG B1ms burstable · Redis C0 · Container Apps scale-to-zero · no HA | **low tens of dollars** |
| **Production v1** | PG D2ds_v5 + zone-redundant HA + 128 GB · Redis C0 · always-on containers · Front Door | **low hundreds** |

Biggest lever: **zone-redundant HA roughly doubles the database cost.** Worth it
before a paying customer; skip it while you have none. Second lever: Container Apps
scale-to-zero on dev — except `fides-worker`, which must stay at min 1.

---

## Build order

1. Resource group, VNet, Key Vault, Container Registry
2. **PostgreSQL Flexible Server 16** → run `scripts_bootstrap.sql` → `alembic upgrade head` → **confirm the RLS isolation tests still pass against Azure**
3. Redis, Blob Storage
4. Container Apps environment → push images → gateway, fides, worker, provisioner job
5. cms-backend → Static Web App for the frontend
6. Front Door + WAF, custom domain, TLS
7. Log Analytics dashboards + alerts (DSARs approaching their 30-day deadline is the alert that matters)

**Step 2's last clause is the gate.** The test suite is the only thing that proves
tenant isolation survived the move to a managed database where we do not have
superuser. Run `make api-test` against Azure before anything else goes on top.

---

## Deliberately not on Azure

`app-postgres` and `app-mongo` as **managed services**. They are stand-ins for a
customer's own databases. In a real deployment the customer already runs those, and
Fides reaches into them over a private endpoint or VPN — they were never ours to
host. Keeping them as containers for demos costs almost nothing; making them
managed databases would be paying for HA on seed data.

**Cosmos DB is not the default answer for Mongo.** If a customer's Mongo *is*
Cosmos DB, Fides talks to it with pymongo (`find()`, `update_one()`) and Cosmos's
Mongo API has wire-protocol gaps. Prefer **vCore** over the RU-based API, and test
before promising erasure on Cosmos data.
