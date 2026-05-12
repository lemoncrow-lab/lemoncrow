.DEFAULT_GOAL := help

PY_PATHS := src tests
ATELIER_STORE ?= $(HOME)/.atelier
FORCE_ARG := $(if $(f),--force,)

.PHONY: help install uninstall status start restart \
	test test-fast test-cov security-test lint format-check format typecheck verify pre-commit \
	benchmark bench-savings bench-savings-honest proof-cost-quality demo import-sessions clean

# --------------------------------------------------------------------------- #
# Lifecycle                                                                   #
# --------------------------------------------------------------------------- #

#    * To do a clean global install (clones from GitHub):
#         make install
#    * To install from your current local folder:
#         make install ARGS="--local"
#    * To pass other flags (like skipping hosts or dry-run):

#         make install ARGS="--local --no-hosts --dry-run"
# install: ## Install Atelier (use ARGS="--local" to install from current dir)
install: ## Install Atelier (use ARGS="--local" to install from current dir)
	@# This target calls scripts/install.sh which eventually runs scripts/install_agent_clis.sh
	@INSTALL_ARGS="$(ARGS)"; \
	if [ -z "$$INSTALL_ARGS" ]; then INSTALL_ARGS="$(args)"; fi; \
	bash scripts/install.sh $$INSTALL_ARGS
	@echo "[atelier] Installation complete."

uninstall: ## Remove all Atelier agent-host integrations, hooks, and bin wrappers
	@bash scripts/uninstall.sh $${ARGS:-}

status: ## Show Atelier installation status
	@bash scripts/status.sh

start: ## Start the service and frontend with Docker Compose
	docker compose up --build -d
	docker compose logs -f
restart: ## Restart the service and frontend with Docker Compose
	docker compose down
	docker compose up --build -d
	docker compose logs -f

# --------------------------------------------------------------------------- #
# Development                                                                 #
# --------------------------------------------------------------------------- #

test: ## Run all tests
	uv run pytest -q

test-fast: ## Run fast tests: stop on first failure, skip slow/Postgres-gated tests
	uv run pytest -q -x --ignore=tests/test_postgres_store.py --ignore=tests/test_worker_jobs.py -m "not slow"

test-cov: ## Run tests with terminal and HTML coverage reports
	uv run pytest --cov=atelier --cov-report=term-missing --cov-report=html

security-test: ## Run security-focused test cases
	uv run pytest tests/gateway/test_security.py -v

lint: ## Run ruff lint checks
	uv run ruff check $(PY_PATHS)

format: ## Format all code: Python (ruff+black) and frontend (prettier if available)
	uv run ruff check --fix $(PY_PATHS)
	uv run black $(PY_PATHS)
	@if [ -d "frontend" ]; then \
		if [ -f "frontend/package.json" ] && grep -q "prettier" frontend/package.json 2>/dev/null; then \
			cd frontend && npx prettier --write "src/**/*.{ts,tsx,js,jsx,json,css,md}" 2>/dev/null || true; \
		fi; \
	fi

typecheck: ## Run mypy strict type-checking
	uv run mypy --strict $(PY_PATHS)

verify: lint format-check typecheck test ## Verify code, runtime smoke tests, and agent integrations
	bash scripts/verify_atelier_mcp_stdio.sh
	bash scripts/verify_atelier_service.sh
	bash scripts/verify_atelier_postgres.sh
	bash scripts/verify_agent_clis.sh

pre-commit: format lint typecheck test ## Format, lint, typecheck, and test

# --------------------------------------------------------------------------- #
# Benchmarks and demos                                                        #
# --------------------------------------------------------------------------- #

benchmark: ## Run the full benchmark suite
	LOCAL=1 uv run atelier benchmark-full --json

bench-savings: ## Run the context-savings benchmark
	LOCAL=1 uv run python -m benchmarks.swe.savings_bench --json

bench-savings-honest: ## Run the V3 honest replay context-savings benchmark
	rm -rf /tmp/atelier-v3-savings-replay
	ATELIER_ROOT=/tmp/atelier-v3-savings-replay LOCAL=1 uv run python -m benchmarks.swe.savings_replay --csv docs/benchmarks/v3-honest-savings-results.csv

proof-cost-quality: ## Run cost-quality proof gate tests and write proof-report.json
	LOCAL=1 uv run pytest tests/core/test_cost_quality_proof_gate.py tests/gateway/test_cli_proof_gate.py -v
	LOCAL=1 uv run atelier proof run --session-id wp32-proof --json
	@test -s $(ATELIER_STORE)/proof/proof-report.json

demo: ## Run a small blocked-plan demo in a temporary store
	@DEMO_ROOT=$$(mktemp -d); \
	uv run atelier --root "$$DEMO_ROOT" init --seed; \
	uv run atelier --root "$$DEMO_ROOT" check-plan \
		--task "Update Shopify product description" \
		--domain "beseam.shopify.publish" \
		--step "Parse product handle from the PDP URL" \
		--step "Look up product by handle" \
		--step "Update description" \
		--step "Publish" \
		--json; \
	EXIT=$$?; rm -rf "$$DEMO_ROOT"; \
	echo "exit code: $$EXIT (2=blocked, 0=pass)"; test $$EXIT -eq 2

# --------------------------------------------------------------------------- #
# Utilities                                                                   #
# --------------------------------------------------------------------------- #

import-sessions: ## Import sessions from all supported hosts: make import-sessions [f=1]
	@for host in copilot claude codex opencode; do \
		LOCAL=1 uv run atelier --root "$(ATELIER_STORE)" "$$host" import $(FORCE_ARG); \
	done

clean: ## Remove build artifacts, caches, and coverage data
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

help: ## Show this help message
	@echo "Atelier - AI reasoning/procedure/runtime layer"
	@echo ""
	@echo "Usage: make <target>"
	@echo ""
	@printf "%-20s %s\n" "Target" "Description"
	@printf "%-20s %s\n" "------" "-----------"
	@grep -E '^[a-zA-Z0-9_.-]+:.*##' $(MAKEFILE_LIST) | \
		sed 's/:.*## /\t/' | \
		awk -F'\t' '{ printf "  %-18s %s\n", $$1, $$2 }'
