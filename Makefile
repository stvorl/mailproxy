-include .env
export

COMPOSE_FLAGS :=
ifeq ($(ENABLE_ROUNDCUBE),true)
  COMPOSE_FLAGS := --profile roundcube
endif

.PHONY: init build up down logs restart exec-postfix postfix-reload

init:
	sh ./scripts/init.sh

build:
	docker compose --progress quiet build

up: build
	docker compose $(COMPOSE_FLAGS) up -d --remove-orphans

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
