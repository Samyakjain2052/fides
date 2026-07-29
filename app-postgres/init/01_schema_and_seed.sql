-- =============================================================================
-- app-postgres: demo application database
--
-- Run automatically by the postgres image's docker-entrypoint-initdb.d hook the
-- first time the volume is created. To re-seed from scratch:
--     docker compose down -v && docker compose up
--
-- The schema here is mirrored 1:1 by fides-config/resources/app_postgres_dataset.yml.
-- If you add a column here, add it there too or Fides will not know about it.
-- =============================================================================

CREATE TABLE IF NOT EXISTS users (
    id          SERIAL PRIMARY KEY,
    email       VARCHAR(255),
    full_name   VARCHAR(255),
    phone       VARCHAR(64),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS orders (
    id          SERIAL PRIMARY KEY,
    user_email  VARCHAR(255),
    amount      NUMERIC(10, 2),
    item        VARCHAR(255),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_email       ON users (email);
CREATE INDEX IF NOT EXISTS idx_orders_user_email ON orders (user_email);

-- NOTE: `email` / `user_email` are deliberately NULLABLE. The erasure policy
-- uses the `null_rewrite` masking strategy, which issues
-- `UPDATE users SET email = NULL, full_name = NULL, phone = NULL WHERE id = ...`.
-- A NOT NULL constraint on a PII column would make the erasure fail.

-- -----------------------------------------------------------------------------
-- The data subject under test. The SAME email is seeded into app-mongo so a
-- single DSAR fans out across both databases.
-- -----------------------------------------------------------------------------
INSERT INTO users (email, full_name, phone, created_at) VALUES
    ('demo@example.com', 'Demo Person',  '+1-555-0100', '2025-01-15T09:30:00Z');

INSERT INTO orders (user_email, amount, item, created_at) VALUES
    ('demo@example.com',  49.99, 'Noise-cancelling headphones', '2025-02-02T14:05:00Z'),
    ('demo@example.com', 129.50, 'Standing desk converter',     '2025-03-11T10:22:00Z'),
    ('demo@example.com',   8.75, 'USB-C cable',                 '2025-04-27T18:44:00Z');

-- -----------------------------------------------------------------------------
-- A control subject. Nothing belonging to this person should ever be touched by
-- a DSAR for demo@example.com — this is what proves the erasure was targeted
-- and not a table-wide UPDATE.
-- -----------------------------------------------------------------------------
INSERT INTO users (email, full_name, phone, created_at) VALUES
    ('control@example.com', 'Control Person', '+1-555-0199', '2025-01-16T11:00:00Z');

INSERT INTO orders (user_email, amount, item, created_at) VALUES
    ('control@example.com', 22.00, 'Desk lamp', '2025-02-14T08:00:00Z');
