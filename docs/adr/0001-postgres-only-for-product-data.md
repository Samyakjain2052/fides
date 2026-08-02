# ADR 0001 — PostgreSQL only for product data; customer databases stay containerised

- **Status:** accepted
- **Date:** 2026-08-02
- **Decides:** which database technology holds DataShield's own data, and what we
  do about the demo datastores

## Context

The system touches four data stores, and they have different owners — which is the
thing that makes "Postgres or Mongo?" sound like one question when it is really
three:

| Store | Contents | Owner |
| --- | --- | --- |
| `cms-db` | tenants, users, api_keys, **audit_events** | **us — the product** |
| `fides-db` | the engine's config, requests, execution logs | us, but dictated |
| `app-postgres` | `users`, `orders` | stand-in for **the customer's** database |
| `app-mongo` | `events` | stand-in for **the customer's** database |

## Decision

1. **All product data is PostgreSQL.** `cms-db` is PostgreSQL 16 and will not be
   anything else. `fides-db` is PostgreSQL because Fides requires it — not a choice.
2. **The demo datastores stay containerised.** They are props representing a
   customer's existing systems. They are not converted to managed Azure database
   services, because managed HA, PITR and geo-redundancy on seed data is money
   spent on nothing.
3. **We still support MongoDB** — as a *target*, through Fides' connector. Not
   supporting Mongo is not on the table; customers have Mongo.

## Why PostgreSQL for product data

Four reasons, each of which we actively depend on today:

1. **Row-Level Security is the tenant-isolation guarantee.** MongoDB has no
   equivalent; neither does Cosmos DB. Without it we fall back to "the application
   remembers to filter by tenant", and one forgotten `WHERE tenant_id = ?` is a
   cross-customer breach of personal data. RLS turns that from a convention into a
   constraint the database enforces.
2. **The audit chain needs transactions and advisory locks.**
   `pg_advisory_xact_lock` stops two concurrent events forking the chain, and
   `UNIQUE (tenant_id, seq)` backstops it. Append-only is enforced by
   `REVOKE UPDATE, DELETE` plus a trigger. None of this has a Mongo equivalent.
3. **Constraints are guarantees, not suggestions.** Foreign keys,
   `CHECK (role IN …)`, `UNIQUE (tenant_id, email)`. Move those into application
   code and they become optional, and eventually violated.
4. **Alembic makes schema change a reviewed artifact.** For compliance data,
   "schemaless" means the schema is whatever some code wrote last year.

The product's core claim is that the database *is* the evidence. Trading away
constraints and row-level security is the wrong trade for that claim.

## Consequences

**Good**
- One database technology to operate, tune, back up and hire for.
- The isolation and audit guarantees we already built and tested carry over
  unchanged.
- On Azure: **PostgreSQL 16** on Flexible Server follows standard PostgreSQL role
  behaviour, and `NOBYPASSRLS` is the default — which is exactly what the
  application role needs. (On PG 15 and earlier, Azure's lack of superuser caused
  real limitations here. Pin 16 deliberately.)
- Built-in PgBouncer in *transaction* pooling mode is safe for us because tenant
  context uses `SET LOCAL`, which is transaction-scoped. A session-level `SET`
  would have leaked tenant context between requests sharing a pooled connection.

**Costs accepted**
- No document flexibility for product data. Acceptable: consent records are
  strongly relational and their shape is a compliance concern, not a convenience.
- Fides' database and ours have different load profiles (bursty DSAR execution vs
  steady API traffic). Start on one server with two databases; split when the
  profiles actually diverge, not before.

**Risk to test, not assume**
- If a customer's Mongo is **Cosmos DB for MongoDB** rather than real MongoDB,
  Fides talks pymongo (`find()`, `update_one()`) and Cosmos's Mongo API has
  wire-protocol gaps. Prefer **vCore** over the RU-based API, and verify before
  promising a customer we can erase their Cosmos data.

## Alternatives rejected

- **MongoDB / Cosmos DB for product data** — no RLS, no advisory locks, no
  declarative constraints. Directly undermines the two guarantees we sell.
- **Managed Azure databases for the demo stores** — paying for HA and PITR on
  seed data.
- **Two Flexible Servers from day one** — premature; one server, two databases,
  split later.
