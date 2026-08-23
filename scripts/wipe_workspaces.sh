#!/usr/bin/env bash
# =============================================================================
# Delete every workspace and everything in it.
#
#     ./scripts/wipe_workspaces.sh            # lists what would go, then asks
#     ./scripts/wipe_workspaces.sh --yes      # no prompt (for scripts)
#
# This is the counterpart to seed_demo.py: it takes the database back to the
# state a fresh `alembic upgrade head` leaves it in. Schema and migration history
# stay; every tenant, user, consent, request, complaint, breach, policy and audit
# entry goes.
#
# WHY THIS USES TRUNCATE, WHICH IS THE INTERESTING PART
# ----------------------------------------------------
# `DELETE FROM tenants` does not work here, and the reason it does not work is a
# feature. Every table cascades from `tenants` except `audit_events`, which is
# ON DELETE RESTRICT and additionally carries
#
#     CREATE TRIGGER audit_events_no_update_delete
#       BEFORE DELETE OR UPDATE ON audit_events FOR EACH ROW ...
#
# so the evidence trail refuses to be removed or rewritten. That is the whole
# point of it. TRUNCATE gets past this because TRUNCATE does not fire row-level
# triggers — it is a different operation, and Postgres treats it as one.
#
# So this script deliberately defeats the append-only guarantee. Two things make
# that acceptable rather than alarming:
#
#   * It needs the OWNER role. `datashield_app` — the role the API runs as — holds
#     only SELECT and INSERT on audit_events: no DELETE, no TRUNCATE. Nothing
#     reachable from the application can do this, which is the property that
#     actually matters.
#   * It is all-or-nothing. It cannot remove *some* audit entries, which is what
#     tampering would look like. A missing chain is obvious; a doctored one is not.
#
# If you only want to drop some workspaces, do not reach for this. There is no
# safe partial version, because deleting one tenant's audit rows leaves a chain
# with a hole in it and `POST /v1/audit/verify` will — correctly — start failing.
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
die()  { printf "\033[31m✗ %s\033[0m\n" "$1" >&2; exit 1; }

ASSUME_YES=0
[[ "${1:-}" == "--yes" ]] && ASSUME_YES=1

psql_owner() {
  docker compose exec -T cms-db \
    psql -U datashield_owner -d datashield -v ON_ERROR_STOP=1 "$@"
}

docker compose ps --status running --format '{{.Service}}' 2>/dev/null \
  | grep -qx cms-db || die "cms-db is not running (try: make api)"

bold "DataShield — wipe every workspace"

COUNT=$(psql_owner -At -c "SELECT count(*) FROM tenants")
if [[ "$COUNT" == "0" ]]; then
  ok "no workspaces — nothing to do"
  exit 0
fi

echo
psql_owner -P pager=off -c \
  "SELECT slug, name, to_char(created_at,'MM-DD HH24:MI') AS created FROM tenants ORDER BY created_at"
echo
warn "$COUNT workspace(s) and every row belonging to them, including the audit trail."
warn "This cannot be undone."

if [[ "$ASSUME_YES" -eq 0 ]]; then
  # Asks for the count rather than "y". Typing a number you had to read means
  # you looked at the list; "y" is muscle memory.
  printf "  Type the number of workspaces to confirm (%s): " "$COUNT"
  read -r answer
  [[ "$answer" == "$COUNT" ]] || die "not confirmed — nothing was deleted"
fi

# One statement, one transaction. alembic_version is excluded on purpose: wiping
# it would make a migrated database look unmigrated and the next `upgrade head`
# would try to replay everything.
TABLES=$(psql_owner -At -c "
  SELECT string_agg(format('%I', tablename), ', ' ORDER BY tablename)
  FROM pg_tables
  WHERE schemaname = 'public' AND tablename <> 'alembic_version'")

psql_owner -q -c "TRUNCATE ${TABLES} CASCADE;"

REMAINING=$(psql_owner -At -c "SELECT count(*) FROM tenants")
AUDIT=$(psql_owner -At -c "SELECT count(*) FROM audit_events")
[[ "$REMAINING" == "0" && "$AUDIT" == "0" ]] \
  || die "wipe incomplete: ${REMAINING} tenant(s), ${AUDIT} audit row(s) left"

ok "all workspaces removed; schema and migration history intact"
echo
echo "  Register a new workspace at http://localhost:8090/register,"
echo "  or run: make seed NAME=\"Your Company\""
