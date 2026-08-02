#!/usr/bin/env python3
"""
Post-deploy database verification — the gate.

Run this against any database the application is about to use, local or Azure. It
asserts the properties the security model depends on, rather than assuming a
managed Postgres granted us the same things a local container did.

    python scripts/verify_database.py            # uses DS_DATABASE_URL / OWNER_URL
    python scripts/verify_database.py --app-only

Exits non-zero on any failure, so it works as a CI/deploy step.

Why it exists: moving to Azure changes who we are. The admin there is not a
superuser, `public` schema privileges are managed for us, and PgBouncer sits in
the path. Every one of those can quietly break tenant isolation while the app
still boots and serves traffic. A green /health means the process is up; this
means the guarantees are intact.
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

TENANT_TABLES = ["users", "refresh_tokens", "api_keys", "audit_events"]
REQUIRED_PG_MAJOR = 16

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

failures: list[str] = []
warnings: list[str] = []


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def bad(msg: str) -> None:
    print(f"  {RED}✗ {msg}{RESET}")
    failures.append(msg)


def warn(msg: str) -> None:
    print(f"  {YELLOW}!{RESET} {msg}")
    warnings.append(msg)


def section(msg: str) -> None:
    print(f"\n{msg}")


def to_asyncpg_dsn(url: str) -> str:
    """SQLAlchemy URLs carry a driver suffix asyncpg does not understand."""
    return url.replace("postgresql+asyncpg://", "postgresql://").split("?")[0]


async def connect(url: str) -> asyncpg.Connection:
    ssl_mode = os.environ.get("DS_DB_SSL_MODE", "disable")
    kwargs: dict = {}
    if ssl_mode != "disable":
        import ssl as ssl_lib

        ctx = ssl_lib.create_default_context(cafile=os.environ.get("DS_DB_SSL_ROOT_CERT"))
        if ssl_mode == "require":
            ctx.check_hostname = False
            ctx.verify_mode = ssl_lib.CERT_NONE
        kwargs["ssl"] = ctx
    return await asyncpg.connect(to_asyncpg_dsn(url), statement_cache_size=0, **kwargs)


async def main() -> int:
    app_url = os.environ.get("DS_DATABASE_URL")
    if not app_url:
        print(f"{RED}DS_DATABASE_URL is not set{RESET}")
        return 2

    conn = await connect(app_url)
    try:
        # ── server ──────────────────────────────────────────────────────────
        section("Server")
        version = await conn.fetchval("SHOW server_version")
        major = int(str(version).split(".")[0])
        if major >= REQUIRED_PG_MAJOR:
            ok(f"PostgreSQL {version}")
        else:
            bad(
                f"PostgreSQL {version} — {REQUIRED_PG_MAJOR}+ required. On 15 and "
                f"earlier, Azure cannot manage BYPASSRLS as this schema needs."
            )

        # Encryption in transit. Personal data on the wire in clear text is not a
        # style issue.
        ssl_row = await conn.fetchrow(
            "SELECT ssl, version FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
        )
        if ssl_row and ssl_row["ssl"]:
            ok(f"connection encrypted ({ssl_row['version']})")
        elif "localhost" in app_url or "@cms-db" in app_url or "127.0.0.1" in app_url:
            warn("connection NOT encrypted — acceptable for local Docker only")
        else:
            bad("connection is NOT encrypted against a remote host — set DS_DB_SSL_MODE=verify-full")

        # ── the application's own role ──────────────────────────────────────
        section("Application role (this is what makes RLS real)")
        role = await conn.fetchrow(
            "SELECT current_user AS name, "
            "  (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS super, "
            "  (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user) AS bypass"
        )
        print(f"  {DIM}connected as {role['name']}{RESET}")
        if role["super"]:
            bad("app role is a SUPERUSER — every RLS policy is bypassed")
        else:
            ok("not a superuser")
        if role["bypass"]:
            bad("app role has BYPASSRLS — every tenant-isolation policy is bypassed")
        else:
            ok("NOBYPASSRLS")

        # A table owner also escapes RLS unless FORCE is set; we check FORCE below,
        # but the app should not own these tables in the first place.
        owned = await conn.fetchval(
            "SELECT count(*) FROM pg_tables "
            "WHERE schemaname = 'public' AND tableowner = current_user "
            "  AND tablename = ANY($1::text[])",
            TENANT_TABLES,
        )
        if owned:
            warn(f"app role owns {owned} tenant table(s) — relies on FORCE RLS alone")
        else:
            ok("app role owns none of the tenant-scoped tables")

        # ── row level security ──────────────────────────────────────────────
        section("Row-level security")
        for table in TENANT_TABLES:
            row = await conn.fetchrow(
                "SELECT c.relrowsecurity AS enabled, c.relforcerowsecurity AS forced, "
                "  (SELECT count(*) FROM pg_policies p "
                "     WHERE p.tablename = $1 AND p.policyname = 'tenant_isolation') AS policies "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relname = $1 AND n.nspname = 'public'",
                table,
            )
            if row is None:
                bad(f"{table}: table is missing — has the migration run?")
                continue
            if row["enabled"] and row["forced"] and row["policies"] == 1:
                ok(f"{table}: RLS enabled + FORCED, tenant_isolation policy present")
            else:
                bad(
                    f"{table}: enabled={row['enabled']} forced={row['forced']} "
                    f"policies={row['policies']} — expected true/true/1"
                )

        # Fail-closed: with no tenant context, a tenant-scoped table must yield
        # nothing. The opposite failure mode — yielding everything — is a breach.
        section("Fail-closed behaviour")
        leaked = await conn.fetchval("SELECT count(*) FROM users")
        if leaked == 0:
            ok("no tenant context → zero rows (fails closed)")
        else:
            bad(f"no tenant context returned {leaked} row(s) — policies are not applying")

        # ── audit trail: append-only ────────────────────────────────────────
        section("Audit trail append-only enforcement")
        grants = await conn.fetch(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE table_name = 'audit_events' AND grantee = current_user"
        )
        held = {g["privilege_type"] for g in grants}
        forbidden = held & {"UPDATE", "DELETE", "TRUNCATE"}
        if forbidden:
            bad(f"app role holds {sorted(forbidden)} on audit_events — it must not")
        else:
            ok(f"grants are {sorted(held) or ['(none)']} — no UPDATE/DELETE")

        trig = await conn.fetchrow(
            "SELECT tgenabled FROM pg_trigger "
            "WHERE tgrelid = 'audit_events'::regclass "
            "  AND tgname = 'audit_events_no_update_delete' AND NOT tgisinternal"
        )
        if trig is None:
            bad("append-only trigger is missing on audit_events")
        elif trig["tgenabled"] == "D":
            bad("append-only trigger exists but is DISABLED")
        else:
            ok("append-only trigger present and enabled")

    finally:
        await conn.close()

    # ── owner-side checks ───────────────────────────────────────────────────
    # `alembic_version` is deliberately NOT readable by the app role — the
    # migration grants it only the domain tables. So the schema-state check runs
    # on the owner connection. (The first version of this script asked the app
    # role and got "permission denied", which was the grants working correctly.)
    owner_url = os.environ.get("DS_DATABASE_OWNER_URL")
    section("Migrations and owner role")
    if not owner_url:
        warn("DS_DATABASE_OWNER_URL not set — skipped schema-revision check")
    else:
        oconn = await connect(owner_url)
        try:
            rev = await oconn.fetchval("SELECT version_num FROM alembic_version LIMIT 1")
            if rev:
                ok(f"alembic at revision {rev}")
            else:
                bad("alembic_version is empty — migrations have not run")

            orole = await oconn.fetchrow(
                "SELECT current_user AS name, "
                "  (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user) AS bypass"
            )
            # The owner escapes RLS by virtue of owning the tables — that is why
            # every tenant table is FORCE RLS. It must still not hold BYPASSRLS,
            # which would defeat FORCE as well.
            if orole["bypass"]:
                bad(f"owner role {orole['name']} has BYPASSRLS — FORCE RLS is defeated too")
            else:
                ok(f"owner role {orole['name']} is NOBYPASSRLS")
        finally:
            await oconn.close()

    # ── summary ─────────────────────────────────────────────────────────────
    print()
    if failures:
        print(f"{RED}FAIL — {len(failures)} check(s) failed.{RESET}")
        for f in failures:
            print(f"  · {f}")
        print("\nDo not deploy on top of this. Tenant isolation is not intact.")
        return 1
    if warnings:
        print(f"{YELLOW}PASS with {len(warnings)} warning(s).{RESET}")
        for w in warnings:
            print(f"  · {w}")
        return 0
    print(f"{GREEN}PASS — tenant isolation and audit immutability are intact.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
