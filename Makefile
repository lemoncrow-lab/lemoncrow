.DEFAULT_GOAL := help
MAKEFLAGS += --no-print-directory

PY_PATHS := src benchmarks tests scripts integrations
MYPY_PATHS := src/lemoncrow
LEMONCROW_STORE ?= $(HOME)/.lemoncrow
LEMONCROW_CMD ?= uv run lemoncrow
TEST_PRINT_TIME ?= 0
# Coverage floor for the full slow-inclusive suite (make test-full / nightly-coverage.yml).
# Conservative provisional floor pending first-CI calibration (see 22-01-SUMMARY.md):
# local measurement could not complete the full suite (slow-service + xdist tree-sitter
# limitations); a partial subset run measured 68% (a strict lower bound). Calibrate to
# ~2 points below the first nightly run's reported total.
COV_FAIL_UNDER ?= 66
FORCE_ARG := $(if $(f),--force,)
# Messages choose color at recipe execution time, so redirected logs stay plain.
_log = sh "$(CURDIR)/scripts/lib/terminal.sh" $(1) "$(2)"
.PHONY: help uninstall build review-client-build review-engine-build runtime-client-build build-lemoncode-host build-host-skills sync-agent-context mirror release release-preflight release-prepare release-prepared release-version-check release-qualify release-stage \
	oss-dev oss-prod oss-up oss-down oss-status oss-reset oss-ui oss-ui-build oss-test \
	hosted-dev hosted-prod hosted-up hosted-down hosted-status hosted-reset hosted-ui hosted-ui-build hosted-test review-hosted-test-full \
	_rehearse-install _verify-hosted-artifact _verify-hosted-e2e \
	review-demo review-demo-diff review-demo-plugin review-e2e perf-slo perf-slo-product perf-product \
	docs-check status-index \
	test test-fast test-cov test-full gate-g gate-k release-gate lint format-check format typecheck verify pre-commit \
	proof-cost-quality import clean \
	_ensure_hooks

# --------------------------------------------------------------------------- #
# Lifecycle: oss-* uses only the Apache mirror, hosted-* needs enterprise/     #
# --------------------------------------------------------------------------- #
# Both sides have the same verbs. Only -prod touches the live home; every other
# verb stays in that side's sandbox, and the two sandboxes run side by side.
#
#   verb       oss-*                                    hosted-*
#   -dev       staged install.sh --local                hosted-up, then staged install.sh --cloud
#              into ~/.lemoncrow-oss (server on 7428)   into ~/.lemoncrow-hosted (client of hosted-up)
#   -up        start the sandbox Review server          start enterprise API 7432 + UI 3130 + Authward
#   -down      stop it                                  stop it (data kept)
#   -status    servers and the sandbox client           servers and the sandbox client
#   -reset     -down, then delete the sandbox home      -down, then delete the sandbox home
#   -ui        Vite HMR against API_PORT (7428)         disposable preview with sample data (3131)
#   -ui-build  frontend/dist                            enterprise/frontend/dist
#   -test      install both products in a temp home     qualify the Review client on a real server
#   -prod      full rebuild, live ~/.lemoncrow (7420)    -test, then live ~/.lemoncrow (HOSTED_URL)
#
# The live home holds one deployment, so oss-prod and hosted-prod replace each
# other. Sandboxes never configure agent hosts; -dev and -status print the
# environment that points lc at a sandbox.

OSS_HOME ?= $(HOME)/.lemoncrow-oss
OSS_PORT ?= 7428
OSS_TEST_PORT ?= 7429
API_PORT ?= $(OSS_PORT)
HOSTED_HOME ?= $(HOME)/.lemoncrow-hosted
HOSTED_DEV_URL ?= http://127.0.0.1:3130
HOSTED_UI_PORT ?= 3131
HOSTED_UI_ROLE ?= admin
LIVE_PORT ?= 7420
RELEASE_STAGE ?= dist/release

_require_private = test -f enterprise/server/pyproject.toml || { $(call _log,error,make $(1) requires the private checkout (enterprise/)) >&2; exit 1; }
LC_DEV = PYTHONPATH="$(CURDIR)/client/src" uv run --project enterprise/server --locked python -m lemoncrow_client dev --checkout "$(CURDIR)"
# $(1)=sandbox home: the environment that points lc, lazy fetches and lc update at it.
_sandbox_hint = $(call _log,hint,Use this sandbox: export LEMONCROW_HOME=$(1) LEMONCROW_BIN_DIR=$(1)/bin LEMONCROW_RELEASE_BASE_URL=file://$(abspath $(RELEASE_STAGE)) PATH=$(1)/bin:\$$PATH; unset LEMONCROW_URL PYTHONPATH)
# $(1)=sandbox home: sandbox verbs refuse the live home before doing anything.
_guard_sandbox = home="$(1)"; home="$${home%/}"; \
	case "$$home" in ""|"$$HOME"|"$$HOME/.lemoncrow") $(call _log,error,refusing: $$home is not a sandbox (only -prod touches the live home)) >&2; exit 1;; esac
_oss_server = $(call _guard_sandbox,$(OSS_HOME)); LEMONCROW_HOME="$(OSS_HOME)" LEMONCROW_LOCAL_SERVER_PORT="$(OSS_PORT)" bash scripts/local_server.sh $(1)
# $(1)=sandbox home: the sandbox client's own status.
_sandbox_status = if [ -x "$(1)/bin/lc" ]; then LEMONCROW_HOME="$(1)" LEMONCROW_NO_LAZY_INSTALL=1 "$(1)/bin/lc" status; else $(call _log,warn,sandbox client: not installed ($(1))); fi

oss-dev: ## Real install.sh --local on staged assets into the OSS sandbox (OSS_HOME, port 7428); reuses fresh artifacts
	@$(MAKE) --no-print-directory _rehearse-install FAST=1 REHEARSE_HOME="$(OSS_HOME)" REHEARSE_PORT="$(OSS_PORT)" REHEARSE_ARGS="--product review --local"

oss-prod: ## Full rebuild, then real install.sh --local into the live home (port 7420)
	@$(call _log,step,OSS production installation)
	@$(call _log,info,Installation home: $$HOME/.lemoncrow)
	@$(MAKE) --no-print-directory _rehearse-install FAST= REHEARSE_HOME="$$HOME/.lemoncrow" REHEARSE_BIN="$$HOME/.lemoncrow/bin" REHEARSE_PORT="$(LIVE_PORT)" REHEARSE_ARGS="--product review --local"

oss-up: ## Start the OSS sandbox Review server (port 7428)
	@$(call _log,step,Start OSS sandbox server)
	@$(call _oss_server,start)
	@$(call _sandbox_hint,$(OSS_HOME))

oss-down: ## Stop the OSS sandbox Review server; the live server is untouched
	@$(call _log,step,Stop OSS sandbox server)
	@$(call _oss_server,stop)

oss-status: ## Show the OSS sandbox and live Review servers and the sandbox client
	@$(call _log,step,OSS sandbox and live server status)
	@printf 'sandbox server (%s): ' "$(OSS_PORT)"; $(call _oss_server,status) || true
	@printf 'live server    (%s): ' "$(LIVE_PORT)"; LEMONCROW_HOME="$$HOME/.lemoncrow" LEMONCROW_LOCAL_SERVER_PORT="$(LIVE_PORT)" bash scripts/local_server.sh status || true
	@$(call _sandbox_status,$(OSS_HOME))
	@$(call _sandbox_hint,$(OSS_HOME))

oss-reset: ## Stop the OSS sandbox server, then delete OSS_HOME
	@$(call _guard_sandbox,$(OSS_HOME))
	@$(call _log,warn,Reset OSS sandbox: $(OSS_HOME))
	@$(MAKE) --no-print-directory oss-down
	rm -rf "$(OSS_HOME)"

oss-ui: ## Vite HMR for the local web UI against API_PORT (default the OSS sandbox, 7428)
	@$(call _log,step,OSS frontend — Vite HMR)
	@$(call _log,hint,API: http://127.0.0.1:$(API_PORT))
	VITE_API_URL=$${VITE_API_URL:-http://127.0.0.1:$(API_PORT)} npm --prefix frontend run dev

oss-ui-build: ## Build the local web UI bundle (frontend/dist)
	@$(call _log,step,Build OSS frontend)
	@if command -v npm >/dev/null 2>&1; then \
		if [ ! -d frontend/node_modules ]; then npm --prefix frontend ci; fi; \
		npm --prefix frontend run build; \
	else \
		echo "npm is required to build the LemonCrow frontend" >&2; exit 1; \
	fi

hosted-dev: hosted-up ## Real install.sh --cloud on staged assets into the hosted sandbox (HOSTED_HOME), against hosted-up
	@$(MAKE) --no-print-directory _rehearse-install FAST=1 REHEARSE_HOME="$(HOSTED_HOME)" REHEARSE_PORT= REHEARSE_ARGS="--product review --cloud --url $(HOSTED_DEV_URL)"

hosted-prod: hosted-test ## Qualify, then real install.sh --cloud into the live home (HOSTED_URL, default https://api.lemoncrow.com)
	@$(call _log,step,Hosted production installation)
	@$(call _log,info,Installation home: $$HOME/.lemoncrow)
	@$(MAKE) --no-print-directory _rehearse-install FAST=1 REHEARSE_HOME="$$HOME/.lemoncrow" REHEARSE_BIN="$$HOME/.lemoncrow/bin" REHEARSE_PORT= REHEARSE_ARGS="--product review --cloud$(if $(HOSTED_URL), --url $(HOSTED_URL))"

hosted-up: ## Start the hosted stack: enterprise API 7432, UI 3130, Authward; keeps the active lc dev profile
	@$(call _require_private,hosted-up)
	@$(call _log,step,Start hosted development stack)
	@$(LC_DEV) start hosted
	@$(call _sandbox_hint,$(HOSTED_HOME))

hosted-down: ## Stop the hosted stack; its data is kept
	@$(call _require_private,hosted-down)
	@$(call _log,step,Stop hosted development stack)
	@$(LC_DEV) stop hosted

hosted-status: ## Show the hosted stack and the hosted sandbox client
	@$(call _require_private,hosted-status)
	@$(call _log,step,Hosted stack and sandbox status)
	@$(LC_DEV) status
	@$(call _sandbox_status,$(HOSTED_HOME))
	@$(call _sandbox_hint,$(HOSTED_HOME))

hosted-reset: ## Stop the hosted stack, then delete HOSTED_HOME (the stack keeps its own data)
	@$(call _guard_sandbox,$(HOSTED_HOME))
	@$(call _log,warn,Reset hosted sandbox: $(HOSTED_HOME))
	@$(MAKE) --no-print-directory hosted-down
	rm -rf "$(HOSTED_HOME)"

hosted-ui: ## Disposable hosted UI preview with sample data and Vite HMR (port 3131; VITE_API_URL selects another API)
	@$(call _log,step,Hosted frontend — Vite HMR)
	@$(call _log,hint,Preview: http://127.0.0.1:$(HOSTED_UI_PORT))
	@$(call _require_private,hosted-ui)
	@if [ ! -d enterprise/frontend/node_modules ]; then npm --prefix enterprise/frontend ci; fi
	@if [ -n "$$VITE_API_URL" ]; then \
		LEMONCROW_HOSTED_PREVIEW= LEMONCROW_HOSTED_PREVIEW_TOKEN= npm --prefix enterprise/frontend run dev -- --host 127.0.0.1 --port "$(HOSTED_UI_PORT)" --strictPort; \
	else \
		cd enterprise/server && uv run python -m lemoncrow_server.hosted_dev --port "$(HOSTED_UI_PORT)" --role "$(HOSTED_UI_ROLE)"; \
	fi

hosted-ui-build: ## Build the private hosted web UI (enterprise/frontend/dist)
	@$(call _log,step,Build hosted frontend)
	@$(call _require_private,hosted-ui-build)
	@if [ ! -d enterprise/frontend/node_modules ]; then npm --prefix enterprise/frontend ci; fi
	npm --prefix enterprise/frontend run build

build: ## Build and package for production distribution
	bash scripts/build.sh

review-client-build: ## Build the isolated Review client artifact
	@bash scripts/build_review_client.sh

review-engine-build: ## Build the isolated local Review engine artifact
	@bash scripts/build_review_engine.sh

runtime-client-build: ## Build the isolated Runtime client artifact
	@bash scripts/build_runtime_client.sh

mirror: ## Incremental mirror bench → public repo (history-preserving): make mirror [f=1] [ARGS="--resync"]
	LEMONCROW_MIRROR_RUNNING=1 uv run python -m scripts.mirror $(FORCE_ARG) $(ARGS)

release-version-check: ## Verify root/public/private LemonCrow package versions are synchronized
	uv run python scripts/release_version.py check --require-private

release-preflight: ## Read-only release safety checks: clean/synced main, reproducible submodules, changelog
	@TAG=$${tag:-}; \
	[ -n "$$TAG" ] || { echo "Usage: make release-preflight tag=vX.Y.Z"; exit 1; }; \
	uv run python scripts/release_preflight.py --tag "$$TAG" $(FORCE_ARG)

release-prepared: ## Read-only checks of the prepared release commit: its version and changelog, clean, directly on origin/main
	@TAG=$${tag:-}; \
	[ -n "$$TAG" ] || { echo "Usage: make release-prepared tag=vX.Y.Z"; exit 1; }; \
	uv run python scripts/release_preflight.py --tag "$$TAG" --prepared $(FORCE_ARG)

release-prepare: release-preflight ## Version and commit the release, not pushed: make release-prepare tag=vX.Y.Z; then release-qualify and release use that exact commit
	@set -e; \
	TAG=$${tag:-}; \
	VER=$${TAG#v}; \
	uv run python scripts/release_version.py set "$$VER"; \
	uv run python scripts/release_changelog.py "$$VER"; \
	uv lock; \
	uv lock --project server; \
	uv lock --project enterprise/server; \
	uv run python scripts/release_version.py check "$$VER" --require-private; \
	uv run python scripts/release_notes.py "$$VER" >/dev/null; \
	git add pyproject.toml uv.lock server/pyproject.toml server/uv.lock \
	  client/pyproject.toml client/src/lemoncrow_client/__init__.py \
	  enterprise/server/pyproject.toml enterprise/server/uv.lock CHANGELOG.md; \
	git diff --cached --check; \
	git commit -m "chore(release): $$TAG"; \
	echo "✓ Prepared $$TAG at $$(git rev-parse --short HEAD) (not pushed); next: make release-qualify tag=$$TAG"

release-qualify: release-prepared release-version-check ## Qualify the prepared release commit for tag=vX.Y.Z: agent CLIs, the full release gate (clean checkout, fresh services, versioned candidate) and a stamped release record
	@# Catch missing prerequisites before spending hours on the release gate.
	bash scripts/release_canary_env.sh uv run --locked --extra postgres python -m scripts.release_gate --preflight --tag "$(tag)"
	@# Real agent CLIs and accounts live on the releasing machine, outside the gate's clean checkout.
	bash scripts/verify_agent_clis.sh
	@# Rehearse the OSS installer here; the release gate also runs the hosted-test suite
	@# through its stable hosted-test-full evidence component, without executing it twice.
	@$(MAKE) --no-print-directory oss-test
	@# G11-RELEASE: suites, gates, drills and the versioned candidate upgrade all run inside the gate;
	@# the release record below is stamped only from its releasable report for this commit and tag.
	@# The whole root suite runs only with the person's opt-in on this command: LEMONCROW_FULL_SUITE=1.
	@bash scripts/release_canary_env.sh $(MAKE) --no-print-directory release-gate RELEASE_GATE_ARGS="--tag $(tag) $(RELEASE_GATE_ARGS)"
	uv run python -m scripts.release_record stamp --report "$(RELEASE_REPORT)" --tag "$(tag)"

release: release-prepared ## Push, mirror and tag the exact commit release-qualify qualified: make release tag=vX.Y.Z [f=1] [SKIP_MIRROR=1]
	@# Only a releasable record stamped for this exact HEAD and tag, whose kept candidate archive still
	@# hashes to the stamped sha256, lets a tag be made; the tag is that commit, never a new one.
	@set -e; \
	TAG=$${tag:-}; \
	FORCE=$${f:-0}; \
	SKIP_MIRROR=$${SKIP_MIRROR:-0}; \
	QUALIFIED=$$(git rev-parse HEAD); \
	MESSAGE="$$(git rev-parse --absolute-git-dir)/lemoncrow-release-tag-message"; \
	uv run python -m scripts.release_record verify --tag "$$TAG" --tag-message "$$MESSAGE"; \
	git push -u origin HEAD; \
	PUSH_FLAG=; \
	[ "$$FORCE" = "1" ] && PUSH_FLAG=--force; \
	if [ "$$SKIP_MIRROR" = "1" ]; then \
	  if [ "$$FORCE" = "1" ]; then \
	    git tag -d "$$TAG" 2>/dev/null || true; \
	    git push --delete origin "$$TAG" 2>/dev/null || true; \
	  fi; \
	  git tag -a "$$TAG" -F "$$MESSAGE" "$$QUALIFIED"; \
	  git push $$PUSH_FLAG origin "$$TAG"; \
	  echo "✓ Tagged $$TAG (private only; public mirror/release skipped)"; \
	  exit 0; \
	fi; \
	if [ "$$(uname -s)" = "Darwin" ] && command -v gh >/dev/null 2>&1; then \
	  gh auth setup-git >/dev/null 2>&1 || true; \
	fi; \
	echo "Mirroring release commit to public repo..."; \
	MIRROR_FORCE_ARG=; \
	[ "$$FORCE" = "1" ] && MIRROR_FORCE_ARG=--force; \
	LEMONCROW_MIRROR_RUNNING=1 uv run python -m scripts.mirror --resync $$MIRROR_FORCE_ARG; \
	PUB_SHA=$$(git rev-parse refs/mirror/last-pub); \
	if [ "$$FORCE" = "1" ]; then \
	  git tag -d "$$TAG" 2>/dev/null || true; \
	  git push --delete origin "$$TAG" 2>/dev/null || true; \
	  git push --delete https://github.com/lemoncrow-lab/lemoncrow.git "$$TAG" 2>/dev/null || true; \
	fi; \
	git tag -a "$$TAG" -F "$$MESSAGE" "$$QUALIFIED"; \
	git push $$PUSH_FLAG origin "$$TAG"; \
	git push $$PUSH_FLAG https://github.com/lemoncrow-lab/lemoncrow.git "$$PUB_SHA:refs/tags/$$TAG"; \
	echo "✓ Released $$TAG (private + public tag; public GitHub Actions owns artifact publication)"

# FAST=1 rebuilds an artifact only when its sources are newer than it.
# lc-debt: source dirs per artifact are approximate (whole trees); narrow per artifact if rebuilds are too eager.
_stale = $(if $(FAST),$(shell [ -f $(1) ] && [ -z "$$(find $(2) scripts/build_*.sh server/review-engine-files.txt -type f -newer $(1) -not -path '*/node_modules/*' -not -path '*/__pycache__/*' -print -quit 2>/dev/null)" ] && echo fresh),)

release-stage: ## Stage release assets (scripts/stage_release.sh) in RELEASE_STAGE; FAST=1 reuses fresh artifacts
	@$(if $(call _stale,dist/lemoncrow-review-engine.tar.gz,src client/src server/src frontend/src),$(call _log,info,fast: review engine fresh),$(MAKE) --no-print-directory review-engine-build)
	@$(if $(call _stale,dist/lemoncrow-review-client.tar.gz,src client/src),$(call _log,info,fast: review client fresh),$(MAKE) --no-print-directory review-client-build)
	@$(if $(call _stale,dist/lemoncrow-runtime-client.tar.gz,src client/src),$(call _log,info,fast: runtime client fresh),$(MAKE) --no-print-directory runtime-client-build)
	@bash scripts/stage_release.sh "$(RELEASE_STAGE)"

# Install verification and its final summary are owned by scripts/install.sh.
# Run the exact public install.sh against staged assets (file:// release base).
# REHEARSE_ARGS picks the deployment; a REHEARSE_PORT means a local Review server.
_rehearse-install:
	@$(call _log,step,Build and stage release assets)
	@$(MAKE) --no-print-directory release-stage
	@LEMONCROW_RELEASE_BASE_URL="file://$(abspath $(RELEASE_STAGE))" LEMONCROW_HOME="$(REHEARSE_HOME)" LEMONCROW_BIN_DIR="$(or $(REHEARSE_BIN),$(REHEARSE_HOME)/bin)" $(if $(REHEARSE_PORT),LEMONCROW_LOCAL_SERVER_PORT="$(REHEARSE_PORT)") \
		bash "$(RELEASE_STAGE)/install.sh" $(REHEARSE_ARGS)

oss-test: ## Install both products from freshly staged assets into a temp home (port 7429), smoke them, clean up
	@$(call _log,step,Qualify OSS artifacts in a temporary installation)
	@$(MAKE) --no-print-directory release-stage
	@set -eu; \
		tmp="$$(mktemp -d "$${TMPDIR:-/tmp}/lemoncrow-oss-test.XXXXXX")"; \
		test_state="$$tmp/state"; \
		export HOME="$$tmp/user"; \
		export XDG_CONFIG_HOME="$$tmp/xdg/config" XDG_CACHE_HOME="$$tmp/xdg/cache"; \
		export XDG_DATA_HOME="$$tmp/xdg/data" XDG_STATE_HOME="$$tmp/xdg/state"; \
		export CLAUDE_CONFIG_DIR="$$HOME/.claude" CODEX_HOME="$$HOME/.codex" PI_CODING_AGENT_DIR="$$HOME/.pi/agent"; \
		unset PYTHONPATH PYTHONHOME LEMONCROW_RUNTIME_INSTALL_ROOT LEMONCROW_REVIEW_INSTALL_ROOT LEMONCROW_HOSTED_INSTALL_ROOT LEMONCROW_REVIEW_ENGINE_ROOT; \
		mkdir -p "$$HOME"; \
		cleanup() { LEMONCROW_HOME="$$test_state" LEMONCROW_LOCAL_SERVER_PORT="$(OSS_TEST_PORT)" bash "$$test_state/install/scripts/local_server.sh" stop >/dev/null 2>&1 || true; rm -rf "$$tmp"; }; \
		trap cleanup EXIT INT TERM; \
		LEMONCROW_RELEASE_BASE_URL="file://$(abspath $(RELEASE_STAGE))" LEMONCROW_HOME="$$test_state" LEMONCROW_BIN_DIR="$$test_state/bin" LEMONCROW_LOCAL_SERVER_PORT="$(OSS_TEST_PORT)" \
			bash "$(RELEASE_STAGE)/install.sh" --product both --local; \
		echo "✓ OSS install test passed (product both, port $(OSS_TEST_PORT))"

# Full hosted qualification is also runnable directly from the developer checkout.
# Full hosted qualification is part of `make hosted-prod`, but remains directly runnable.
hosted-test: review-client-build ## Qualify the hosted Review client artifact: Review/Core tests plus real-server Review E2E
	@$(call _log,step,Qualify hosted client artifact)
	@$(MAKE) _verify-hosted-artifact
	PYTHONPATH=client/src uv run python -m pytest -q \
		$${LEMONCROW_TEST_JUNIT_DIR:+-o junit_family=xunit1 "--junitxml=$$LEMONCROW_TEST_JUNIT_DIR/hosted-client.xml"} \
		client/tests/test_configuration.py \
		client/tests/test_auth_cli.py \
		client/tests/test_managed_credentials.py \
		client/tests/test_review_cli.py \
		client/tests/test_review_session.py \
		client/tests/test_transport.py \
		tests/test_release_boundary_audit.py
	@$(call _log,step,Verify hosted Review end to end)
	@$(MAKE) _verify-hosted-e2e

# Backward-compatible release gate name; now the standard hosted smoke already includes all former full tests.
review-hosted-test-full: hosted-test ## Complete Review client and real-server qualification

_verify-hosted-artifact:
	@set -eu; \
		tmp="$$(mktemp -d "$${TMPDIR:-/tmp}/lemoncrow-hosted-smoke.XXXXXX")"; \
		trap 'rm -rf "$$tmp"' EXIT INT TERM; \
		export HOME="$$tmp/home" LEMONCROW_HOME="$$tmp/state"; \
		export XDG_CONFIG_HOME="$$tmp/xdg/config" XDG_CACHE_HOME="$$tmp/xdg/cache"; \
		export XDG_DATA_HOME="$$tmp/xdg/data" XDG_STATE_HOME="$$tmp/xdg/state"; \
		LEMONCROW_BIN_DIR="$$tmp/bin" \
		LEMONCROW_REVIEW_INSTALL_ROOT="$$tmp/review-client" \
		LEMONCROW_HOSTED_URL=http://127.0.0.1:7420 \
			bash scripts/hosted.sh --from-build dist/lemoncrow-review-client.tar.gz; \
		test -x "$$tmp/bin/lc"; \
		test -x "$$tmp/bin/lcr"; \
		test -x "$$tmp/bin/lemoncrow-review-client"; \
		"$$tmp/bin/lc" --version >/dev/null; \
		"$$tmp/bin/lcr" --help >/dev/null; \
		"$$tmp/bin/lc" audit >/dev/null; \
		set +e; runtime_out="$$(LEMONCROW_NO_LAZY_INSTALL=1 "$$tmp/bin/lc" runtime status 2>&1)"; runtime_status=$$?; set -e; \
		test "$$runtime_status" -eq 2; \
		printf '%s' "$$runtime_out" | grep -q 'Runtime is not installed'

_verify-hosted-e2e:
	@set -eu; \
		tmp="$$(mktemp -d "$${TMPDIR:-/tmp}/lemoncrow-review-e2e.XXXXXX")"; \
		trap 'rm -rf "$$tmp"' EXIT INT TERM; \
		tar -xzf dist/lemoncrow-review-client.tar.gz -C "$$tmp"; \
		wheel="$$(find "$$tmp/vendor" -maxdepth 1 -name 'lemoncrow_review_client-*-py3-none-any.whl' -print -quit)"; \
		test -n "$$wheel"; \
		uv venv --python 3.13 "$$tmp/review-venv" >/dev/null; \
		uv pip install --python "$$tmp/review-venv/bin/python" --no-index --no-deps "$$wheel" >/dev/null; \
		cd enterprise/server; \
		LEMONCROW_REVIEW_E2E_PYTHON="$$tmp/review-venv/bin/python" \
			uv run --extra dev python -m pytest \
			$${LEMONCROW_TEST_JUNIT_DIR:+-o junit_family=xunit1 "--junitxml=$$LEMONCROW_TEST_JUNIT_DIR/hosted-e2e.xml"} \
			../../client/tests/test_end_to_end_server.py::test_thin_hosted_review_derives_packet_on_the_real_server -q

build-lemoncode-host: ## Provision the LemonCode host binary: builds from the vendored lemoncode/ submodule if bun is available, else downloads a release. Skips if already installed; force rebuild with 'make build-lemoncode-host f=1'
	@if [ -z "$(f)" ] && [ -x "$(LEMONCROW_STORE)/bin/lemoncode-host" ]; then \
		:; \
	elif command -v bun >/dev/null 2>&1 && git submodule update --init lemoncode >/dev/null 2>&1 && [ -f lemoncode/package.json ]; then \
		echo "Building LemonCode host from source (lemoncode/ submodule)..."; \
		$(LEMONCROW_CMD) code host build --source lemoncode; \
	else \
		echo "bun/lemoncode submodule unavailable -- downloading prebuilt LemonCode host release..."; \
		$(LEMONCROW_CMD) code host install; \
	fi

uninstall: ## Remove the LIVE LemonCrow install: all agent-host integrations, hooks, and bin wrappers (ARGS="..." forwards flags)
	@bash scripts/uninstall.sh $${ARGS:-}
# --------------------------------------------------------------------------- #
# Review demo                                                                 #
# --------------------------------------------------------------------------- #

DEMO_DIR ?= /tmp/lemoncrow-review-demo

review-demo: ## Create (or reset) the Review demo repo and open it in the Reader: make review-demo [DEMO_DIR=/path]
	./demo/review-surfaces/bootstrap.sh "$(DEMO_DIR)"
	uv run lc review --repo-root "$(DEMO_DIR)" --open

review-demo-diff: ## Show the demo's prepared change (after make review-demo): make review-demo-diff [DEMO_DIR=/path]
	git -C "$(DEMO_DIR)" diff --stat
	git -C "$(DEMO_DIR)" diff

review-e2e: ## Real-browser Review harness (B1-B5, Gate 1 E1-E8; Playwright + real server + built bundle): make review-e2e [E2E_ARGS=...]
	cd frontend && npm run test:e2e -- $(E2E_ARGS)

PERF_SLO_REPORT ?= $(CURDIR)/frontend/test-results/perf-slo
PERF_PRODUCT_REPORT ?= $(CURDIR)/frontend/test-results/perf-slo-product
PERF_PRODUCT_TARGETS ?= $(CURDIR)/evals/performance/large_change_product.toml
REFERENCE_RUNNER_ID ?=
PERF_PRODUCT_MAX_LOAD_FRACTION ?= 0.75

perf-slo: ## G9-SLO speed gate: Python targets, then Chromium (reader open, 10k-file change); reports in PERF_SLO_REPORT. NO_RERUN=1 measures once (release-gate keeps the rerun)
	@mkdir -p "$(PERF_SLO_REPORT)" && rm -f "$(PERF_SLO_REPORT)"/*.json
	@status=0; \
	uv run python -m benchmarks.review_slo run --report "$(PERF_SLO_REPORT)/python.json" $(if $(NO_RERUN),--no-rerun,) || status=1; \
	(cd frontend && REVIEW_SLO_REPORT_DIR="$(PERF_SLO_REPORT)" $(if $(NO_RERUN),REVIEW_SLO_NO_RERUN=1,) npm run test:perf) || status=1; \
	uv run python -m benchmarks.review_slo verify --report-dir "$(PERF_SLO_REPORT)" || status=1; \
	exit $$status

perf-slo-product: ## RC2 dedicated quiet-runner 10k product target. Set REFERENCE_RUNNER_ID; DIAGNOSTIC=1 permits a non-qualifying shared-machine run.
	@mkdir -p "$(PERF_PRODUCT_REPORT)" && rm -f "$(PERF_PRODUCT_REPORT)"/*.json
	@status=0; \
	uv run python -m benchmarks.review_slo quiet-check --reference-id "$(REFERENCE_RUNNER_ID)" --max-load-fraction "$(PERF_PRODUCT_MAX_LOAD_FRACTION)" $(if $(DIAGNOSTIC),--diagnostic,) > "$(PERF_PRODUCT_REPORT)/qualification.json" || status=1; \
	if [ $$status -eq 0 ]; then \
		(cd frontend && REVIEW_SLO_TARGETS="$(PERF_PRODUCT_TARGETS)" REVIEW_SLO_REPORT_DIR="$(PERF_PRODUCT_REPORT)" npm run test:perf:product) || status=1; \
		uv run python -m benchmarks.review_slo --targets "$(PERF_PRODUCT_TARGETS)" verify --report-dir "$(PERF_PRODUCT_REPORT)" --only change_10k_open --only change_10k_navigate --only change_10k_heap || status=1; \
	fi; \
	exit $$status

perf-product: ## RC2 10k product profile on the designated quiet runner; DIAGNOSTIC=1 is shared-machine evidence only
	uv run python -m benchmarks.review_slo --profile product reference $(if $(DIAGNOSTIC),--diagnostic,)
	cd frontend && REVIEW_SLO_PROFILE=product REVIEW_SLO_NO_RERUN=1 $(if $(DIAGNOSTIC),REVIEW_SLO_DIAGNOSTIC=1,) npm run test:perf -- --grep '10k-file'

review-demo-plugin: ## Install the example CSV preview plugin used by the demo's data/plans.csv
	uv pip install -e ./demo/review-preview-plugin
	@tool_python="$${LEMONCROW_SERVER_PYTHON:-$${LEMONCROW_TOOL_DIR:-$$HOME/.lemoncrow/uv-tools}/lemoncrow/bin/python}"; \
		if [ -x "$$tool_python" ]; then \
			uv pip install --python "$$tool_python" -e ./demo/review-preview-plugin; \
		else \
			echo "LemonCrow tool Python not found at $$tool_python; run make oss-prod before testing Reader plugins" >&2; \
			exit 1; \
		fi
	bash scripts/local_server.sh restart
# --------------------------------------------------------------------------- #
# Development                                                                 #
# --------------------------------------------------------------------------- #

build-host-skills: ## Generate Codex/Gemini skill bundles from integrations/skills (set LEMONCROW_DEV_MODE=1 to include dev-only skills)
	@bash scripts/build_host_skills.sh --host all $$( [ "$${LEMONCROW_DEV_MODE:-0}" = "1" ] && echo --include-dev )

sync-agent-context: ## Regenerate host instruction surfaces from integrations/agents/shared/
	uv run python scripts/sync_agent_context.py

docs-check: ## Run docs and repo-governance checks
	uv run pytest tests/gateway/test_docs.py tests/gateway/test_generated_agent_contexts.py $(wildcard tests/gateway/test_status_index.py) -q

status-index: ## Render docs/status/README.md from docs/status/capabilities.toml (private checkout only)
	uv run python scripts/render_status_index.py

# Auto-configure git hooks path so .githooks/pre-commit runs on every commit.
# Developers never need to run `git config core.hooksPath .githooks` by hand.
_ensure_hooks:
	@current=$$(git config core.hooksPath 2>/dev/null || echo ""); \
	if [ "$$current" != ".githooks" ]; then \
		git config core.hooksPath .githooks; \
		echo "  → Configured git hooks path → .githooks"; \
	fi

test: | _ensure_hooks ## Run tests with bounded xdist workers and isolated browser/import-heavy shards
ifeq ($(TEST_PRINT_TIME),1)
	@time LEMONCROW_TEST_DURATIONS=0 bash scripts/run_test_suite.sh all
else
	@bash scripts/run_test_suite.sh all
endif

test-fast: | _ensure_hooks ## Run fast tests; stop on the first failing test shard
	@bash scripts/run_test_suite.sh fast

test-cov: ## Run tests with terminal and HTML coverage reports
	PYTHONPATH="$(CURDIR)/enterprise/runtime/src$${PYTHONPATH:+:$$PYTHONPATH}" uv run pytest --cov=lemoncrow --cov-report=term-missing --cov-report=html

test-full: ## Run the FULL suite (incl. slow) with measured coverage floor
	PYTHONPATH="$(CURDIR)/enterprise/runtime/src$${PYTHONPATH:+:$$PYTHONPATH}" uv run pytest -m "" --timeout=300 --cov=lemoncrow --cov-report=term-missing --cov-fail-under=$(COV_FAIL_UNDER)

RELEASE_REPORT ?= $(CURDIR)/reports/release/release-report.json

release-gate: ## G11-RELEASE: suites, Gates F/G/H/I, G9 perf, G2 engine, G8 canary, G1 index, ops drills -> one report (RELEASE_REPORT=path; ONLY=ids is never releasable; RELEASE_GATE_ARGS="--tag vX.Y.Z --jobs N")
	uv run --locked --extra postgres python -m scripts.release_gate --report "$(RELEASE_REPORT)" $(if $(ONLY),--only "$(ONLY)",) $(RELEASE_GATE_ARGS)

gate-g: ## Replay the Gate G qualification matrix (LEMONCROW_TEST_POSTGRES_DSN for Postgres rows; STRICT=1 fails on skips; GATE_G_REPORT_DIR keeps the JUnit reports)
	uv run python scripts/gate_g_matrix.py $(if $(STRICT),--strict,) $(if $(GATE_G_REPORT_DIR),--report-dir "$(GATE_G_REPORT_DIR)",)

gate-k: ## Replay the Gate K (Project Knowledge) qualification matrix (LEMONCROW_TEST_POSTGRES_DSN for Postgres rows; STRICT=1 fails on skips; GATE_K_REPORT_DIR keeps the reports)
	uv run python scripts/gate_g_matrix.py --manifest scripts/gate_k_matrix.toml $(if $(STRICT),--strict,) $(if $(GATE_K_REPORT_DIR),--report-dir "$(GATE_K_REPORT_DIR)",)

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
	uv run mypy --explicit-package-bases $(MYPY_PATHS)

pre-commit: | _ensure_hooks ## Full pre-commit gate without re-running the docs pytest subset
	@$(MAKE) --no-print-directory format
	@$(MAKE) --no-print-directory typecheck
	@$(MAKE) --no-print-directory test

verify: | _ensure_hooks ## Verify code, docs, tests, runtime smoke tests, and agent integrations
	@$(MAKE) --no-print-directory -j3 lint format-check typecheck
	@$(MAKE) --no-print-directory test
	bash scripts/verify_lemoncrow_service.sh
	bash scripts/verify_lemoncrow_postgres.sh
	bash scripts/verify_agent_clis.sh

proof-cost-quality: ## Run the deterministic cost-quality proof gate tests
	LOCAL=1 uv run pytest tests/core/test_cost_quality_proof_gate.py -v

# --------------------------------------------------------------------------- #
# Utilities                                                                   #
# --------------------------------------------------------------------------- #

import: ## Import new agent sessions (root `lc import` is retired)
	LOCAL=1 uv run python -m lemoncrow.core.service.session_import --root "$(LEMONCROW_STORE)"

flow-dump: ## Extract chat from a .flow file or directory: make flow-dump path=/path/to/file_or_dir
	@if [ -z "$(path)" ]; then \
		echo "Error: 'path' argument is required. Usage: make flow-dump path=/path/to/file_or_dir"; \
		exit 1; \
	fi
	uv run --project benchmarks python -m benchmarks.flowlib.dump $(path)

clean: ## Remove build artifacts, caches, and coverage data
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

help: ## Show this help message
	@$(call _log,step,LemonCrow - AI reasoning/procedure/runtime layer)
	@echo ""
	@echo "Usage: make <target>"
	@echo ""
	@printf "%-20s %s\n" "Target" "Description"
	@printf "%-20s %s\n" "------" "-----------"
	@grep -E '^[a-zA-Z0-9_.-]+:.*##' $(MAKEFILE_LIST) | \
		sed 's/:.*## /\t/' | \
		awk -F'\t' '{ printf "  %-18s %s\n", $$1, $$2 }'
