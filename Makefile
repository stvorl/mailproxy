-include .env
export

COMPOSE_FLAGS :=
ifeq ($(ENABLE_ROUNDCUBE),true)
  COMPOSE_FLAGS := --profile roundcube
endif

.PHONY: init build up down logs restart exec-postfix postfix-reload export import

init:
	sh ./scripts/init.sh

build:
	docker compose --progress quiet build

up: build
	docker compose $(COMPOSE_FLAGS) up -d --remove-orphans
ifeq ($(ENABLE_ROUNDCUBE),true)
	docker exec mailproxy-roundcube-1 chown -R www-data:www-data /var/roundcube/db
endif

down:
	docker compose $(COMPOSE_FLAGS) down

logs:
	docker compose $(COMPOSE_FLAGS) logs -f --tail=200

restart: down up

exec-postfix:
	docker compose exec postfix sh

# Apply updated relay maps to a running Postfix (after editing accounts.yml)
postfix-reload:
	docker compose exec postfix sh -c '\
		for map in sasl_passwd sender_relay tls_policy; do \
			[ -f /var/credentials/$$map ] && cp /var/credentials/$$map /etc/postfix/$$map && postmap /etc/postfix/$$map && echo "reloaded $$map"; \
		done && \
		chmod 600 /etc/postfix/sasl_passwd /etc/postfix/sasl_passwd.db 2>/dev/null || true && \
		postfix reload'

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
	tar -czf $(FILE) maildata/ rcdata/ accounts.yml .env $$CERTS; \
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
	@echo "Clearing maildata/, rcdata/ and certs/ ..."
	rm -rf maildata rcdata certs
	tar -xzf $(FILE)
	@echo "Imported from $(FILE)"
	@echo "Start servers with: make up"
