#!/bin/bash
# =============================================================================
# app-mongo: create the least-privilege user that Fides connects as.
#
# Runs before 01_seed.js (the entrypoint executes /docker-entrypoint-initdb.d
# in lexical order), once, on first volume creation.
#
# WHY THIS EXISTS: the image's MONGO_INITDB_ROOT_USERNAME user is created in
# the `admin` database. Fides' mongodb connector authenticates against the
# database named in its `defaultauthdb` secret (`appdb` here) rather than
# against `admin`, so a user that only exists in `admin` cannot be used — the
# DSAR would fail with an authentication error. This creates `$APP_MONGO_USER`
# *inside* appdb with readWrite, which is exactly what erasure needs.
#
# Shell rather than .js so the credentials can come from the environment;
# mongosh's `process.env` support is not something to rely on.
# =============================================================================
set -euo pipefail

echo "[app-mongo init] creating user '${APP_MONGO_USER}' in db '${MONGO_INITDB_DATABASE}'"

mongosh --quiet \
  --host 127.0.0.1 \
  --username "${MONGO_INITDB_ROOT_USERNAME}" \
  --password "${MONGO_INITDB_ROOT_PASSWORD}" \
  --authenticationDatabase admin \
  "${MONGO_INITDB_DATABASE}" \
  --eval "db.createUser({
            user: '${APP_MONGO_USER}',
            pwd:  '${APP_MONGO_PASSWORD}',
            roles: [
              { role: 'readWrite', db: '${MONGO_INITDB_DATABASE}' },
              { role: 'dbAdmin',   db: '${MONGO_INITDB_DATABASE}' }
            ]
          })"

echo "[app-mongo init] user created"
