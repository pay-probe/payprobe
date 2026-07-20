# PayProbe — one-command test + build entry points.
# `make test` runs the whole Python suite and exits non-zero on any failure.

.DEFAULT_GOAL := help
PYTEST ?= python -m pytest
PKGS := worker/tests orchestrator/tests scenario-service/tests mcp-server/tests payprobe-assistant/tests insight-service/tests ../scripts/test_showcase.py

HUB_COMPOSE := infra/docker/docker-compose.hub.yml

.PHONY: help test test-worker test-orchestrator test-scenario test-insight test-cov \
        install portal-build showcase showcase-teardown \
        nats-showcase nats-showcase-teardown clean \
        publish up-hub down-hub

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

test: ## Run the full Python suite (non-zero exit on any failure)
	cd packages && $(PYTEST) $(PKGS) -q

test-worker: ## Run the worker engine/adapter suite
	cd packages && $(PYTEST) worker/tests -q

test-orchestrator: ## Run the orchestrator suite
	cd packages && $(PYTEST) orchestrator/tests -q

test-scenario: ## Run the scenario-service suite
	cd packages && $(PYTEST) scenario-service/tests -q

test-insight: ## Run the insight-service suite (learned-layer tests skip without scikit-learn)
	cd packages && $(PYTEST) insight-service/tests -q

test-cov: ## Run the full suite with coverage
	cd packages && $(PYTEST) $(PKGS) --cov --cov-report=term-missing -q

install: ## Install worker runtime + dev test dependencies
	pip install -e "packages/worker[dev]" httpx fastapi structlog pyyaml

portal-build: ## Production build of the Angular portal
	cd packages/portal && npm run build

showcase: ## Build + start the demo acquiring network (needs the stack up)
	python scripts/showcase.py

showcase-teardown: ## Remove the demo network and its artifacts
	python scripts/showcase.py --teardown

nats-showcase: ## Build + start the NATS issuer, then load + storm + resilience cert (needs the NATS cluster up)
	python scripts/nats_showcase.py --certify

nats-showcase-teardown: ## Remove the NATS showcase artifacts
	python scripts/nats_showcase.py --teardown

publish: ## Build + push all service images to Docker Hub (NAMESPACE=datikos)
	scripts/publish-images.sh

up-hub: ## Run the whole platform from published Docker Hub images (no build)
	docker compose -f $(HUB_COMPOSE) up -d
	@echo "PayProbe is starting -> http://localhost:8080"

down-hub: ## Stop the Hub-image stack (keeps volumes)
	docker compose -f $(HUB_COMPOSE) down

clean: ## Remove local test artifacts (sqlite dbs, registry json)
	rm -f packages/scenario-service/*.json packages/scenario-service/scenarios.db \
	      packages/orchestrator/*.db 2>/dev/null || true
