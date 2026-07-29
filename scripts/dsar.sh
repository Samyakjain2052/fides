#!/usr/bin/env bash
# =============================================================================
# Fire a DSAR at the gateway and poll it to completion.
#
#   ./scripts/dsar.sh access
#   ./scripts/dsar.sh erasure
#   ./scripts/dsar.sh access someone@else.com
#
# Pure convenience — everything here is one POST and one GET against
# http://localhost:8000, which you can equally do from Swagger at /docs.
# Requires: curl, jq
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
set -a; source .env; set +a

ACTION="${1:-access}"
EMAIL="${2:-${DEMO_EMAIL:-demo@example.com}}"
GATEWAY="http://localhost:${GATEWAY_PORT:-8000}"

case "$ACTION" in
  access|erasure) ;;
  *) echo "usage: $0 [access|erasure] [email]" >&2; exit 2 ;;
esac

echo "POST ${GATEWAY}/dsar  {action: ${ACTION}, email: ${EMAIL}}"
REQUEST_ID=$(
  curl -sS -X POST "${GATEWAY}/dsar" \
    -H 'Content-Type: application/json' \
    -d "{\"email\": \"${EMAIL}\", \"action\": \"${ACTION}\"}" \
  | jq -er '.request_id'
)
echo "request_id = ${REQUEST_ID}"
echo

# Fides queues the request to its worker, so the first poll is almost always
# 'in_processing'. A cross-database DSAR on this data set finishes in seconds.
for attempt in $(seq 1 40); do
  BODY=$(curl -sS "${GATEWAY}/dsar/${REQUEST_ID}")
  STATUS=$(echo "$BODY" | jq -r '.status')
  printf '  poll %2d: %s\n' "$attempt" "$STATUS"
  case "$STATUS" in
    complete|error|canceled|denied) break ;;
  esac
  sleep 2
done

echo
echo "=== execution log (per collection, across BOTH databases) ==="
# Fides also logs two request-level entries with a null collection ("Request
# execution plan", "Dataset traversal"); they are in the API response but only
# noise here.
echo "$BODY" | jq '[.execution_log[] | select(.collection != null)]'
echo
echo "=== collections touched ==="
echo "$BODY" | jq '.collections_touched'

if [ "$ACTION" = "access" ]; then
  echo
  echo "=== returned data ==="
  echo "$BODY" | jq '.data'
fi

echo
echo "Full response:  curl -s ${GATEWAY}/dsar/${REQUEST_ID} | jq"
