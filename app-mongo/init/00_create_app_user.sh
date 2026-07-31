#!/bin/sh
# =============================================================================
# app-mongo: create the least-privilege user that Fides connects as.
#
# Runs before 01_seed.js, once, on first volume creation.
# =============================================================================
set -eu

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
