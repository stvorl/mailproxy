-include .env
export

COMPOSE_FLAGS :=
ifeq ($(ENABLE_ROUNDCUBE),true)
  COMPOSE_FLAGS := --profile roundcube
endif

.PHONY: init build rebuild up down logs restart export import clean

init:
	sh ./scripts/init.sh

build:
	docker compose $(COMPOSE_FLAGS) build

rebuild:
	docker compose $(COMPOSE_FLAGS) build --no-cache

up: build
	docker compose $(COMPOSE_FLAGS) up -d --remove-orphans

down:
	docker compose $(COMPOSE_FLAGS) down

logs:
	@docker compose $(COMPOSE_FLAGS) logs -f --tail=200 >/dev/tty 2>/dev/tty

restart: down up

# make export FILE=backup.tar.gz
export:
ifndef FILE
	$(error FILE is not set. Usage: make export FILE=backup.tar.gz)
endif
	$(MAKE) down
	@CERTS=""; \
	for f in certs/mailproxy.pem certs/mailproxy.key; do \
		[ -r "$$f" ] && CERTS="$$CERTS $$f"; \
	done; \
	tar -czf $(FILE) logs/ maildata/ rcdata/ accounts.yml .env $$CERTS; \
	if [ -n "$$CERTS" ]; then \
		echo "Exported to $(FILE) (including cert files)"; \
	else \
		echo "Exported to $(FILE) (cert files not included — run with sudo to include)"; \
	fi
	@echo "Start servers again with: make up"

# make import FILE=backup.tar.gz
import:
ifndef FILE
	$(error FILE is not set. Usage: make import FILE=backup.tar.gz)
endif
	@echo "Clearing logs/ maildata/, rcdata/ and certs/ ..."
	rm -rf logs maildata rcdata certs
	tar -xzf $(FILE) --no-same-owner
	@echo "Imported from $(FILE)"
	@echo "Start servers with: make up"

# make clean — stop services and wipe all private data from this instance
clean:
	@echo "WARNING: This will permanently delete all mail, credentials and config."
	@echo "         logs/ maildata/, rcdata/, certs/, accounts.yml, .env will be removed."
	@printf 'Type YES (uppercase) to confirm: '; \
	read answer; \
	if [ "$$answer" != "YES" ]; then \
		echo "Aborted."; \
		exit 1; \
	fi
	-docker compose $(COMPOSE_FLAGS) down 2>/dev/null || true
	rm -rf logs maildata rcdata certs logs
	rm -f accounts.yml .env
	@echo "Done. Run 'make init' to start fresh."
