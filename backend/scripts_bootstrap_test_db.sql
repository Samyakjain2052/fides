-- A separate database for the test suite.
--
-- Until this existed, `docker compose run --rm cms-test` truncated the same
-- database `cms-backend` serves — so running the tests silently destroyed
-- whatever workspace you had been demoing with. It is the sort of thing you
-- discover by losing an account you just created, which is exactly how it was
-- found.
--
-- Same roles, same RLS behaviour, different database. `cms-test` migrates it
-- itself before running, so it is always at head without anyone remembering to
-- do anything.
--
-- Runs from docker-entrypoint-initdb.d AFTER 00_roles.sql (files execute in
-- lexical order), so the roles it grants to already exist.

CREATE DATABASE datashield_test OWNER datashield_owner;

-- The grants below must be applied INSIDE the new database, not this one.
\connect datashield_test

GRANT USAGE ON SCHEMA public TO datashield_app;
GRANT CREATE ON SCHEMA public TO datashield_owner;
ALTER SCHEMA public OWNER TO datashield_owner;

-- Same non-negotiable as the main database: the application role must NOT be
-- able to bypass row-level security, or every isolation test would pass for the
-- wrong reason — which is worse than failing.
ALTER ROLE datashield_owner NOBYPASSRLS;
ALTER ROLE datashield_app NOBYPASSRLS;
