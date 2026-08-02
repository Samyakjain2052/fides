-- =============================================================================
-- Roles the migration expects to exist.
--
-- Run once per database, as a superuser, before the first `alembic upgrade`.
-- docker-compose does this automatically via app-db-init.
--
-- TWO roles, and the separation is the whole point:
--   datashield_owner  owns the tables and runs migrations. Postgres exempts
--                     table owners from RLS, so this role can see everything —
--                     which is exactly why the application must not use it.
--   datashield_app    what the application connects as. NOBYPASSRLS, so every
--                     row-level security policy actually applies to it.
-- =============================================================================

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'datashield_owner') THEN
    CREATE ROLE datashield_owner LOGIN PASSWORD 'ownerpassword' NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'datashield_app') THEN
    CREATE ROLE datashield_app LOGIN PASSWORD 'apppassword' NOBYPASSRLS;
  END IF;
END $$;

-- Belt and braces: even if a role pre-existed with BYPASSRLS, strip it here.
-- The migration refuses to run if this is not true, because BYPASSRLS silently
-- disables every tenant-isolation policy.
ALTER ROLE datashield_owner NOBYPASSRLS;
ALTER ROLE datashield_app NOBYPASSRLS;

ALTER DATABASE datashield OWNER TO datashield_owner;

-- The app never creates objects; it only uses what migrations made.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO datashield_app;
