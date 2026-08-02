-- =============================================================================
-- Role bootstrap for AZURE DATABASE FOR POSTGRESQL — FLEXIBLE SERVER
--
-- Run ONCE per server, connected as your Flexible Server admin user, against the
-- `datashield` database:
--
--   psql "host=<srv>.postgres.database.azure.com port=5432 dbname=datashield \
--         user=<admin> sslmode=verify-full" -f scripts_bootstrap_azure.sql
--
-- WHY A SEPARATE FILE FROM scripts_bootstrap.sql
-- On Azure the admin is a member of `azure_pg_admin` and is NOT a superuser, as
-- is normal for managed Postgres. Three things therefore differ from local:
--
--   1. Ownership can only be transferred to a role you are a member of, so the
--      script GRANTs the new role to the current user first.
--   2. `public` schema privileges are managed through azure_pg_admin, not by a
--      superuser, so the REVOKE is wrapped and tolerated if it is refused.
--   3. It asserts the server is PostgreSQL 16+. On 15 and earlier, Azure's lack
--      of superuser causes real limitations around BYPASSRLS management; on 16+
--      standard PostgreSQL behaviour applies and NOBYPASSRLS is the default,
--      which is precisely what the application role needs.
--
-- CHANGE THE TWO PASSWORDS BELOW before running. They are placeholders.
-- =============================================================================

\set ON_ERROR_STOP on

-- ── 0. Preconditions ────────────────────────────────────────────────────────
DO $$
BEGIN
  IF current_setting('server_version_num')::int < 160000 THEN
    RAISE EXCEPTION
      'PostgreSQL 16+ required (found %). On 15 and earlier, Azure Flexible '
      'Server cannot manage BYPASSRLS the way this schema depends on.',
      current_setting('server_version');
  END IF;

  IF NOT pg_has_role(current_user, 'azure_pg_admin', 'member') THEN
    RAISE WARNING
      'current_user (%) is not a member of azure_pg_admin — role creation below '
      'may be refused.', current_user;
  END IF;
END $$;

-- ── 1. The two roles ────────────────────────────────────────────────────────
-- datashield_owner  owns the tables, runs migrations. RLS does not constrain a
--                   table owner, which is exactly why the app must not use it.
-- datashield_app    what the application connects as. NOBYPASSRLS, not the
--                   owner, so every isolation policy actually applies.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'datashield_owner') THEN
    CREATE ROLE datashield_owner LOGIN PASSWORD 'CHANGE_ME_owner' NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'datashield_app') THEN
    CREATE ROLE datashield_app LOGIN PASSWORD 'CHANGE_ME_app' NOBYPASSRLS;
  END IF;
END $$;

-- Idempotent and load-bearing: BYPASSRLS on either role silently disables every
-- tenant-isolation policy in the schema. The migration refuses to run if this is
-- not true, and scripts/verify_database.py re-checks it after every deploy.
ALTER ROLE datashield_owner NOBYPASSRLS;
ALTER ROLE datashield_app   NOBYPASSRLS;

-- ── 2. Database ownership ───────────────────────────────────────────────────
-- You can only hand ownership to a role you belong to, so join it first. This is
-- the step that fails on Azure if you copy the local bootstrap verbatim.
GRANT datashield_owner TO CURRENT_USER;
ALTER DATABASE datashield OWNER TO datashield_owner;

-- ── 3. Schema privileges ────────────────────────────────────────────────────
-- The app never creates objects; it only uses what migrations made. Tolerated if
-- Azure refuses, because it is defence in depth rather than a requirement.
DO $$
BEGIN
  EXECUTE 'REVOKE CREATE ON SCHEMA public FROM PUBLIC';
EXCEPTION WHEN insufficient_privilege THEN
  RAISE NOTICE 'could not REVOKE CREATE on public (managed by Azure) — continuing';
END $$;

GRANT USAGE ON SCHEMA public TO datashield_app;
GRANT USAGE, CREATE ON SCHEMA public TO datashield_owner;

-- ── 4. Report ───────────────────────────────────────────────────────────────
\echo ''
\echo 'Roles created. Verify before migrating — rolbypassrls MUST be false:'
SELECT rolname, rolcanlogin, rolbypassrls, rolsuper
  FROM pg_roles
 WHERE rolname IN ('datashield_owner', 'datashield_app')
 ORDER BY rolname;

\echo ''
\echo 'Next:'
\echo '  1. alembic upgrade head            (as datashield_owner)'
\echo '  2. python scripts/verify_database.py   <- the deployment gate'
\echo '  3. pytest -q                       (isolation tests, against Azure)'
