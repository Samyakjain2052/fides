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
.PHONY: help up down stop reset build ps logs logs-all provision open \
        dsar dsar-erasure data health test

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

open:  ## open the console and the Fides Admin UI in a browser
	@gateway=$$(grep -E '^GATEWAY_PORT=' .env | cut -d= -f2); \
	 fides=$$(grep -E '^FIDES_PORT=' .env | cut -d= -f2); \
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
