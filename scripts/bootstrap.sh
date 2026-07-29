#!/usr/bin/env bash
# =============================================================================
# One command to get this running on a machine that has never seen it.
#
#     ./scripts/bootstrap.sh
#
# It will:
#   1. check Docker is running,
#   2. create .env from .env.example if you don't have one,
#   3. move any host port that is already taken on your machine (this is the
#      #1 first-run failure — 8080 in particular is a popular port),
#   4. build + start everything,
#   5. wait for the provisioner to finish loading /fides-config into Fides,
#   6. print the URLs.
#
# Safe to re-run: it never touches an already-running stack's ports, and every
# provisioning step is an upsert.
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
die()  { printf "\033[31m✗ %s\033[0m\n" "$1" >&2; exit 1; }

bold "fides-dsar-demo — bootstrap"

# --- 1. Docker ---------------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "Docker is not installed. See https://docs.docker.com/get-docker/"
docker info >/dev/null 2>&1 || die "Docker is installed but not running. Start Docker Desktop and re-run."
docker compose version >/dev/null 2>&1 || die "You need Docker Compose v2 (\`docker compose\`, not \`docker-compose\`)."
ok "Docker is running"

# --- 2. .env -----------------------------------------------------------------
if [ -f .env ]; then
  ok ".env already exists (leaving it alone)"
else
  cp .env.example .env
  ok "created .env from .env.example"
fi

# --- 3. Host ports -----------------------------------------------------------
# Only when nothing is up yet: reassigning a port under a running stack would
# just confuse everyone.
running="$(docker compose ps -q 2>/dev/null | wc -l | tr -d ' ')"

port_free() {
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$1" <<'PY'
import socket, sys
s = socket.socket()
s.settimeout(1)
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
  elif command -v lsof >/dev/null 2>&1; then
    ! lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
  else
    return 0   # can't tell; assume free and let Docker complain
  fi
}

set_env() {  # set_env KEY VALUE — in-place, portable across BSD/GNU sed
  local key="$1" value="$2"
  if grep -qE "^${key}=" .env; then
    sed "s|^${key}=.*|${key}=${value}|" .env > .env.tmp && mv .env.tmp .env
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}

if [ "$running" -gt 0 ]; then
  ok "stack already has containers; keeping the ports in .env as they are"
else
  for var in FIDES_PORT GATEWAY_PORT APP_POSTGRES_HOST_PORT APP_MONGO_HOST_PORT \
             FIDES_DB_HOST_PORT FIDES_REDIS_HOST_PORT; do
    want="$(grep -E "^${var}=" .env | head -1 | cut -d= -f2 | tr -d ' ')"
    [ -n "$want" ] || continue
    port="$want"
    tries=0
    while ! port_free "$port"; do
      port=$((port + 1))
      tries=$((tries + 1))
      [ "$tries" -lt 50 ] || die "could not find a free port near $want"
    done
    if [ "$port" != "$want" ]; then
      set_env "$var" "$port"
      warn "$var: $want is in use on this machine → using $port"
    fi
  done
  ok "host ports checked"
fi

# --- 4. Up -------------------------------------------------------------------
bold "Starting (first run pulls images and runs migrations — a few minutes)"
docker compose up -d --build

# --- 5. Wait for provisioning ------------------------------------------------
# The provisioner is the gate: when it exits 0, Fides has the datasets,
# connections and DSR policies loaded and the demo is ready.
bold "Waiting for Fides to be provisioned"
name="$(docker compose ps -a --format '{{.Name}}' fides-provisioner 2>/dev/null | head -1)"
deadline=$(( $(date +%s) + 600 ))
while :; do
  state="$(docker inspect -f '{{.State.Status}}:{{.State.ExitCode}}' "$name" 2>/dev/null || echo 'missing:-')"
  case "$state" in
    exited:0) ok "Fides is configured"; break ;;
    exited:*) docker compose logs --tail 40 fides-provisioner
              die "provisioning failed (${state}). See the log above." ;;
  esac
  [ "$(date +%s)" -lt "$deadline" ] || { docker compose logs --tail 40 fides-provisioner; die "timed out waiting for the provisioner"; }
  sleep 5
done

# --- 6. Where to go ----------------------------------------------------------
gateway="$(grep -E '^GATEWAY_PORT=' .env | cut -d= -f2 | tr -d ' ')"
fides="$(grep -E '^FIDES_PORT=' .env | cut -d= -f2 | tr -d ' ')"
user="$(grep -E '^FIDES_ROOT_USERNAME=' .env | cut -d= -f2 | tr -d ' ')"
pass="$(grep -E '^FIDES_ROOT_PASSWORD=' .env | cut -d= -f2 | tr -d ' ')"

echo
bold "Ready"
echo "  DSAR Console    http://localhost:${gateway}"
echo "  Swagger         http://localhost:${gateway}/docs"
echo "  Fides Admin UI  http://localhost:${fides}   (${user} / ${pass})"
echo
echo "  Try it:   ./scripts/dsar.sh access demo@example.com"
echo "  Logs:     docker compose logs -f fides-worker | grep -v poll_reply_mailbox"
echo "  Reset:    docker compose down -v && ./scripts/bootstrap.sh"
