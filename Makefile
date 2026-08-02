# =============================================================================
# Shortcuts. Everything here is a plain docker compose command underneath —
# `make` is a convenience, never a requirement.
#
#   make            list the targets
#   make up         first run / start everything (safe to re-run)
#   make logs       follow the interesting logs
#   make dsar       run an access DSAR for demo@example.com
# =============================================================================
.DEFAULT_GOAL := help
.PHONY: help up down stop reset build cms cms-build cms-docker api api-test \
        api-migrate api-revision api-logs api-verify-db azure-verify-db ps logs logs-all provision open dsar \
        dsar-erasure data health test

EMAIL ?= demo@example.com

help:  ## show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk -F':.*?## ' '{printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  Override the subject:  make dsar EMAIL=someone@example.com"

up:  ## start everything (creates .env, moves busy ports, waits for provisioning)
	./scripts/bootstrap.sh

down:  ## stop and remove containers (keeps the data volumes)
	docker compose down

stop:  ## stop containers without removing them
	docker compose stop

reset:  ## wipe ALL data and start clean (re-seeds both databases)
	docker compose down -v
	rm -f fides_uploads/*.json
	./scripts/bootstrap.sh

build:  ## rebuild the gateway image (after editing fastapi-gateway/)
	docker compose up -d --build fastapi-gateway

cms:  ## run the DataShield CMS frontend locally with hot reload (needs npm)
	cd frontend && npm install --no-audit --no-fund && npm run dev

cms-build:  ## production build of the CMS frontend into frontend/dist
	cd frontend && npm install --no-audit --no-fund && npm run build

cms-docker:  ## run the CMS frontend as a container instead
	docker compose up -d --build cms-frontend

api:  ## start the CMS backend (multi-tenant API) + its Postgres
	docker compose up -d --build cms-db cms-backend
	@echo "  API      http://localhost:$$(grep -E '^CMS_BACKEND_PORT=' .env | cut -d= -f2 || echo 8100)"
	@echo "  Swagger  http://localhost:$$(grep -E '^CMS_BACKEND_PORT=' .env | cut -d= -f2 || echo 8100)/docs"

api-test:  ## run the backend test suite (RLS isolation, auth, audit chain)
	docker compose run --rm --no-deps \
	  -e DS_DATABASE_URL="postgresql+asyncpg://datashield_app:apppassword@cms-db:5432/datashield" \
	  -e DS_DATABASE_OWNER_URL="postgresql+asyncpg://datashield_owner:ownerpassword@cms-db:5432/datashield" \
	  cms-backend pytest -q

api-migrate:  ## apply backend migrations
	docker compose run --rm --no-deps cms-backend alembic upgrade head

api-revision:  ## generate a migration: make api-revision M="add consents"
	docker compose run --rm --no-deps cms-backend alembic revision --autogenerate -m "$(M)"

api-logs:  ## follow backend logs
	docker compose logs -f --tail 40 cms-backend

api-verify-db:  ## GATE: assert tenant isolation + audit immutability on the target DB
	docker compose run --rm --no-deps \
	  -e DS_DATABASE_URL="postgresql+asyncpg://datashield_app:apppassword@cms-db:5432/datashield" \
	  -e DS_DATABASE_OWNER_URL="postgresql+asyncpg://datashield_owner:ownerpassword@cms-db:5432/datashield" \
	  cms-backend python scripts/verify_database.py

azure-verify-db:  ## same gate, against Azure. Reads DS_* from backend/.env
	cd backend && set -a && . ./.env && set +a && python3 scripts/verify_database.py

provision:  ## re-apply fides-config/ to a running Fides (idempotent)
	docker compose up -d fides-provisioner
	docker compose logs -f --tail 40 fides-provisioner

ps:  ## show container status
	docker compose ps

logs:  ## follow the DSAR-relevant logs (worker + gateway), minus known noise
	docker compose logs -f --tail 30 fides-worker fastapi-gateway \
	  | grep --line-buffered -v poll_reply_mailbox

logs-all:  ## follow every container
	docker compose logs -f

open:  ## open the CMS, the DSAR console and the Fides Admin UI in a browser
	@gateway=$$(grep -E '^GATEWAY_PORT=' .env | cut -d= -f2); \
	 fides=$$(grep -E '^FIDES_PORT=' .env | cut -d= -f2); \
	 cms=$$(grep -E '^CMS_PORT=' .env | cut -d= -f2); \
	 open "http://localhost:$${cms:-5173}" || xdg-open "http://localhost:$${cms:-5173}"; \
	 open "http://localhost:$$gateway" || xdg-open "http://localhost:$$gateway"; \
	 open "http://localhost:$$fides"   || xdg-open "http://localhost:$$fides"

dsar:  ## access DSAR for $(EMAIL)
	./scripts/dsar.sh access $(EMAIL)

dsar-erasure:  ## erasure DSAR for $(EMAIL) — masks their PII in both databases
	./scripts/dsar.sh erasure $(EMAIL)

data:  ## print $(EMAIL)'s rows from both databases
	./scripts/show_data.sh $(EMAIL)

health:  ## gateway + Fides + both application databases
	@gateway=$$(grep -E '^GATEWAY_PORT=' .env | cut -d= -f2); \
	 curl -s "localhost:$$gateway/health" | (jq . 2>/dev/null || cat)

test:  ## end-to-end check: seed -> access -> erasure -> confirm gone
	./scripts/acceptance.sh
