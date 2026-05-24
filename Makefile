.DEFAULT_GOAL := help

PY_PATHS := src
ATELIER_STORE ?= $(HOME)/.atelier
ATELIER_CMD ?= uv run atelier
TEST_PRINT_TIME ?= 0
FORCE_ARG := $(if $(f),--force,)
EXTERNAL_PERIODS ?= today week month

.PHONY: help install uninstall status start restart build-host-skills sync-agent-context \
	check-agent-context docs-check worktree-env runtime-evidence \
	test test-fast test-cov security-test lint format-check format typecheck launch-gate verify pre-commit \
	benchmark bench-savings bench-savings-honest proof-cost-quality demo import clean \
	_ensure_hooks

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
	@# This target calls scripts/install.sh
	bash scripts/install.sh --local

uninstall: ## Remove all Atelier agent-host integrations, hooks, and bin wrappers
	@bash scripts/uninstall.sh $${ARGS:-}

status: ## Show Atelier installation status
	@bash scripts/status.sh

start: ## Start the service and frontend natively
	@if [ -f .env.worktree ]; then set -a; . ./.env.worktree; set +a; fi; \
	$(ATELIER_CMD) --root "$${ATELIER_STACK_ROOT:-$(ATELIER_STORE)}" stack start
	@if [ -f .env.worktree ]; then set -a; . ./.env.worktree; set +a; fi; \
	$(ATELIER_CMD) --root "$${ATELIER_STACK_ROOT:-$(ATELIER_STORE)}" stack logs -f
restart: ## Restart the service and frontend natively
	@if [ -f .env.worktree ]; then set -a; . ./.env.worktree; set +a; fi; \
	$(ATELIER_CMD) --root "$${ATELIER_STACK_ROOT:-$(ATELIER_STORE)}" stack stop --force || true
	@if [ -f .env.worktree ]; then set -a; . ./.env.worktree; set +a; fi; \
	$(ATELIER_CMD) --root "$${ATELIER_STACK_ROOT:-$(ATELIER_STORE)}" stack start
	@if [ -f .env.worktree ]; then set -a; . ./.env.worktree; set +a; fi; \
	$(ATELIER_CMD) --root "$${ATELIER_STACK_ROOT:-$(ATELIER_STORE)}" stack logs -f

# --------------------------------------------------------------------------- #
# Development                                                                 #
# --------------------------------------------------------------------------- #

build-host-skills: ## Generate Codex/Gemini skill bundles from integrations/skills (set ATELIER_DEV_MODE=1 to include dev-only skills)
	@bash scripts/build_host_skills.sh --host all $$( [ "$${ATELIER_DEV_MODE:-0}" = "1" ] && echo --include-dev )

sync-agent-context: ## Regenerate host instruction surfaces from docs/agent-os
	uv run python scripts/sync_agent_context.py

check-agent-context: ## Verify generated host instruction surfaces are up to date
	uv run python scripts/sync_agent_context.py --check

docs-check: check-agent-context ## Run docs and repo-governance checks
	uv run pytest tests/gateway/test_docs.py tests/gateway/test_generated_agent_contexts.py -q

worktree-env: ## Write a per-worktree .env file for local stack bootstraps
	uv run python scripts/worktree_env.py --env-file .env.worktree --json

runtime-evidence: ## Capture runtime evidence from a local Atelier stack
	uv run python scripts/runtime_evidence.py

# Auto-configure git hooks path so .githooks/pre-commit runs on every commit.
# Developers never need to run `git config core.hooksPath .githooks` by hand.
_ensure_hooks:
	@current=$$(git config core.hooksPath 2>/dev/null || echo ""); \
	if [ "$$current" != ".githooks" ]; then \
		git config core.hooksPath .githooks; \
		echo "  → Configured git hooks path → .githooks"; \
	fi

test: | _ensure_hooks ## Run all tests
ifeq ($(TEST_PRINT_TIME),1)
	@time bash -lc 'if uv run python -c "import xdist" >/dev/null 2>&1; then uv run pytest -q -ra --durations=0 -n auto --dist=loadfile; else uv run pytest -q -ra --durations=0; fi'
else
	@bash -lc 'if uv run python -c "import xdist" >/dev/null 2>&1; then uv run pytest -q -ra --durations=0 -n auto --dist=loadfile; else uv run pytest -q -ra --durations=0; fi'
endif

test-fast: | _ensure_hooks ## Run fast tests: stop on first failure, skip slow/Postgres-gated tests
	uv run pytest -q -x --ignore=tests/test_postgres_store.py --ignore=tests/test_worker_jobs.py -m "not slow"

test-cov: ## Run tests with terminal and HTML coverage reports
	uv run pytest --cov=atelier --cov-report=term-missing --cov-report=html

security-test: ## Run security-focused test cases
	uv run pytest tests/gateway/test_security.py -v

lint: | _ensure_hooks ## Run ruff lint checks
	uv run ruff check $(PY_PATHS)

format-check: ## Check Python formatting without rewriting files
	uv run black --check src tests

format: | _ensure_hooks ## Format all code: Python (ruff+black) and frontend (prettier if available)
	uv run ruff check --fix $(PY_PATHS)
	uv run black src tests
	@if [ -d "frontend" ]; then \
		if [ -f "frontend/package.json" ] && grep -q "prettier" frontend/package.json 2>/dev/null; then \
			cd frontend && npx prettier --write "src/**/*.{ts,tsx,js,jsx,json,css,md}" 2>/dev/null || true; \
		fi; \
	fi

typecheck: | _ensure_hooks ## Run mypy strict type-checking
	uv run mypy --strict $(PY_PATHS)

launch-gate: ## Run pre-launch policy gate (set mode with LAUNCH_GATE_MODE=shadow|suggest|enforce)
	bash scripts/launch_gate.sh --mode $${LAUNCH_GATE_MODE:-enforce}

verify: | _ensure_hooks lint format-check typecheck docs-check test ## Verify code, docs, runtime smoke tests, and agent integrations
	bash scripts/verify_atelier_service.sh
	bash scripts/verify_atelier_postgres.sh
	bash scripts/verify_agent_clis.sh

pre-commit: | _ensure_hooks format lint typecheck docs-check test ## Format, lint, typecheck, docs, and test

# --------------------------------------------------------------------------- #
# Benchmarks and demos                                                        #
# --------------------------------------------------------------------------- #

benchmark: ## Run the full benchmark suite
	LOCAL=1 atelier benchmark full --json

# `bench-savings` (V2) was removed — it was deprecated for measurement and
	# its percentage claims were not honored. Use `bench-ab` for real A/B numbers
	# or `bench-savings-honest` for the V3 synthetic replay.

	bench-ab: ## Real A/B benchmarks: run Atelier tool + native equivalent on the same input, persist deltas to ~/.atelier/savings_calibration.jsonl
	LOCAL=1 uv run pytest tests/benchmarks/ -v -m ab

	bench-savings-honest: ## V3 honest replay (synthetic corpus, 50 prompts)
	rm -rf /tmp/atelier-v3-savings-replay
	ATELIER_ROOT=/tmp/atelier-v3-savings-replay LOCAL=1 uv run python -m benchmarks.swe.savings_replay --csv docs/benchmarks/v3-honest-savings-results.csv

proof-cost-quality: ## Run cost-quality proof gate tests and write proof-report.json
	LOCAL=1 uv run pytest tests/core/test_cost_quality_proof_gate.py tests/gateway/test_cli_proof_gate.py -v
	LOCAL=1 atelier proof run --session-id wp32-proof --json
	@test -s $(ATELIER_STORE)/proof/proof-report.json

# --------------------------------------------------------------------------- #
# Utilities                                                                   #
# --------------------------------------------------------------------------- #

import: ## Import sessions and external tool snapshots: make import [f=1]
	LOCAL=1 $(ATELIER_CMD) --root "$(ATELIER_STORE)" import $(FORCE_ARG)
	@for period in $(EXTERNAL_PERIODS); do \
		LOCAL=1 $(ATELIER_CMD) --root "$(ATELIER_STORE)" external-report --tool all --period "$$period" --persist || true; \
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
