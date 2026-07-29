#!/usr/bin/env bash
# =============================================================================
# End-to-end acceptance test. Run it after changing anything — especially after
# adding a datastore, editing a dataset, or touching the policies.
#
#     ./scripts/acceptance.sh        (or: make test)
#
# It uses a throwaway subject so it never disturbs demo@example.com, and it
# checks the one thing that matters: a single privacy request reaches EVERY
# datastore, and an erasure actually erases.
#
# Requires: python3 (for JSON parsing), a running stack.
# Exit code 0 = pass.
# =============================================================================
set -uo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
set -a; source .env; set +a

GW="http://localhost:${GATEWAY_PORT:-8000}"
SUBJECT="acceptance-$(date +%s)@example.com"
FAILURES=0

pass() { printf "  \033[32m✓\033[0m %s\n" "$1"; }
fail() { printf "  \033[31m✗ %s\033[0m\n" "$1"; FAILURES=$((FAILURES + 1)); }
step() { printf "\n\033[1m%s\033[0m\n" "$1"; }

# jq is optional; python3 does the parsing.
get() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)"; }

# --------------------------------------------------------------------------
step "1. Services are healthy"
health="$(curl -s "$GW/health")" || health=""
if [ -z "$health" ]; then
  fail "gateway is not answering at $GW — is the stack up?"
  echo; echo "Run ./scripts/bootstrap.sh first."; exit 1
fi
[ "$(echo "$health" | get 'd["gateway"]')" = "ok" ] && pass "gateway" || fail "gateway"
[ "$(echo "$health" | get 'd["fides"]["webserver"]')" = "healthy" ] && pass "Fides" || fail "Fides"
[ "$(echo "$health" | get 'd["app_databases"]["app_postgres"]')" = "ok" ] && pass "db1 app-postgres" || fail "db1 app-postgres"
[ "$(echo "$health" | get 'd["app_databases"]["app_mongo"]')" = "ok" ] && pass "db2 app-mongo" || fail "db2 app-mongo"

# --------------------------------------------------------------------------
step "2. Write a subject into every datastore"
written="$(curl -s -X POST "$GW/data/subject" -H 'Content-Type: application/json' -d "{
  \"email\": \"$SUBJECT\",
  \"full_name\": \"Acceptance Test\",
  \"phone\": \"+1-555-0000\",
  \"orders\": [{\"amount\": \"10.00\", \"item\": \"Test widget\"}],
  \"events\": [{\"event_type\": \"login\", \"ip_address\": \"203.0.113.1\",
                \"user_agent\": \"acceptance\", \"session_id\": \"sess_acc\"}]
}")"
echo "$written" | get 'd["written_to"]' | sed 's/^/  /'
[ "$(echo "$written" | get 'd["db1_app_postgres"]["orders"]["inserted"]')" = "1" ] && pass "db1 wrote an order" || fail "db1 write"
[ "$(echo "$written" | get 'd["db2_app_mongo"]["events"]["inserted"]')" = "1" ] && pass "db2 wrote an event" || fail "db2 write"

# --------------------------------------------------------------------------
step "3. The lookup finds them in both databases"
found="$(curl -s "$GW/data/subject/$SUBJECT")"
total="$(echo "$found" | get 'd["total_records"]')"
[ "$total" = "3" ] && pass "3 records (1 user + 1 order + 1 event)" || fail "expected 3 records, got $total"

# --------------------------------------------------------------------------
# Poll a privacy request to completion. Fides queues it to its Celery worker,
# so the first read is normally still in_processing.
run_dsar() {
  local action="$1" rid status body
  rid="$(curl -s -X POST "$GW/dsar" -H 'Content-Type: application/json' \
        -d "{\"email\":\"$SUBJECT\",\"action\":\"$action\"}" | get 'd["request_id"]')"
  for _ in $(seq 1 40); do
    body="$(curl -s "$GW/dsar/$rid")"
    status="$(echo "$body" | get 'd["status"]')"
    case "$status" in complete|error|canceled|denied) break ;; esac
    sleep 2
  done
  echo "$body"
}

step "4. Access request returns data from every datastore"
access="$(run_dsar access)"
[ "$(echo "$access" | get 'd["status"]')" = "complete" ] && pass "completed" || fail "status $(echo "$access" | get 'd["status"]')"
collections="$(echo "$access" | get 'len(d["collections_touched"])')"
[ "$collections" -ge 3 ] && pass "$collections collections touched" || fail "expected >=3 collections, got $collections"
keys="$(echo "$access" | get 'len(d["data"] or {})')"
[ "$keys" -ge 3 ] && pass "$keys collections returned data" || fail "expected data from >=3 collections, got $keys"
# The cross-database claim, checked explicitly rather than assumed.
echo "$access" | get '"|".join(sorted(d["data"] or {}))' | grep -q "app_mongo" && pass "db2 mongo data present" || fail "no db2 mongo data in the access package"
echo "$access" | get '"|".join(sorted(d["data"] or {}))' | grep -q "app_postgres" && pass "db1 postgres data present" || fail "no db1 postgres data in the access package"

step "5. Erasure request masks them in every datastore"
erasure="$(run_dsar erasure)"
[ "$(echo "$erasure" | get 'd["status"]')" = "complete" ] && pass "completed" || fail "status $(echo "$erasure" | get 'd["status"]')"
# Count DISTINCT collections: Fides logs more than one entry per collection
# (one per request task plus consolidation), so summing entries overstates it.
masked="$(echo "$erasure" | get 'len({e["dataset"]+":"+e["collection"] for e in d["execution_log"] if e["action_type"]=="erasure" and e["status"]=="complete" and e["collection"]})')"
[ "$masked" -ge 3 ] && pass "$masked collections masked" || fail "expected >=3 masked collections, got $masked"

step "6. The data is gone"
after="$(curl -s "$GW/data/subject/$SUBJECT")"
[ "$(echo "$after" | get 'd["total_records"]')" = "0" ] && pass "0 records match the identity" || fail "still $(echo "$after" | get 'd["total_records"]') record(s)"
[ "$(echo "$after" | get 'd["found"]')" = "False" ] && pass "found=false" || fail "found is still true"

step "7. A follow-up access request comes back empty"
again="$(run_dsar access)"
[ "$(echo "$again" | get 'len(d["data"] or {})')" = "0" ] && pass "access package is empty" || fail "access still returned data"

step "8. Other subjects are untouched"
control="$(curl -s "$GW/data/subject/control@example.com")"
[ "$(echo "$control" | get 'd["total_records"]')" -gt 0 ] && pass "control@example.com intact ($(echo "$control" | get 'd["total_records"]') records)" || fail "control subject was affected — the erasure was not targeted"

# --------------------------------------------------------------------------
echo
if [ "$FAILURES" -eq 0 ]; then
  printf "\033[32m\033[1mPASS\033[0m — one request reached every datastore, and the erasure held.\n"
  echo "       test subject was $SUBJECT (its masked rows remain, by design)"
  exit 0
fi
printf "\033[31m\033[1mFAIL\033[0m — %d check(s) failed. Try:\n" "$FAILURES"
echo "  docker compose logs --tail 40 fides-worker | grep -v poll_reply_mailbox"
echo "  docker compose logs --tail 40 fides-provisioner"
exit 1
