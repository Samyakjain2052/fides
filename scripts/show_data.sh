#!/usr/bin/env bash
# =============================================================================
# Print the demo subject's data as it exists RIGHT NOW in both databases.
#
#   ./scripts/show_data.sh                    # demo@example.com
#   ./scripts/show_data.sh control@example.com
#
# Run it before and after an erasure DSAR: that side-by-side is the ground
# truth, independent of anything Fides reports.
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
set -a; source .env; set +a

EMAIL="${1:-${DEMO_EMAIL:-demo@example.com}}"

echo "==============================================================="
echo " Subject: ${EMAIL}"
echo "==============================================================="
echo
echo "--- app-postgres : users ---------------------------------------"
# Rows are matched by id, not by email, so a row whose email has been NULLed by
# an erasure still shows up here — otherwise "gone" and "masked" look identical.
docker compose exec -T app-postgres psql -U "${APP_POSTGRES_USER}" -d "${APP_POSTGRES_DB}" -c "
  SELECT id, email, full_name, phone, created_at
  FROM users
  WHERE email = '${EMAIL}'
     OR id IN (SELECT id FROM users WHERE email IS NULL)
  ORDER BY id;"

echo "--- app-postgres : orders --------------------------------------"
docker compose exec -T app-postgres psql -U "${APP_POSTGRES_USER}" -d "${APP_POSTGRES_DB}" -c "
  SELECT id, user_email, amount, item, created_at
  FROM orders
  WHERE user_email = '${EMAIL}'
     OR user_email IS NULL
  ORDER BY id;"

echo "--- app-mongo : events -----------------------------------------"
docker compose exec -T app-mongo mongosh --quiet \
  --username "${APP_MONGO_USER}" --password "${APP_MONGO_PASSWORD}" \
  --authenticationDatabase "${APP_MONGO_DB}" "${APP_MONGO_DB}" \
  --eval "db.events.find(
            { \$or: [ { email: '${EMAIL}' }, { email: null } ] },
            { _id: 0 }
          ).pretty()"

echo
echo "Counts still matching the email exactly (0 across the board after erasure):"
docker compose exec -T app-postgres psql -U "${APP_POSTGRES_USER}" -d "${APP_POSTGRES_DB}" -tAc "
  SELECT 'postgres.users  = ' || count(*) FROM users  WHERE email      = '${EMAIL}'
  UNION ALL
  SELECT 'postgres.orders = ' || count(*) FROM orders WHERE user_email = '${EMAIL}';"
docker compose exec -T app-mongo mongosh --quiet \
  --username "${APP_MONGO_USER}" --password "${APP_MONGO_PASSWORD}" \
  --authenticationDatabase "${APP_MONGO_DB}" "${APP_MONGO_DB}" \
  --eval "print('mongo.events    = ' + db.events.countDocuments({ email: '${EMAIL}' }))"
