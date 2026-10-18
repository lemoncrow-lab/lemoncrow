#!/usr/bin/env bash
# Build the local Review-only engine from an explicit source allowlist.
set -euo pipefail
# Smoke imports must resolve the installed wheel, never a checkout on PYTHONPATH.
unset PYTHONPATH

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
cd "$ROOT"
source "$ROOT/scripts/lib/terminal.sh"
OUT_DIR="${LEMONCROW_REVIEW_ENGINE_DIST_DIR:-$ROOT/dist}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/lemoncrow-review-engine-build.XXXXXX")"
trap 'rm -rf "${WORK:-}"' EXIT
PROJECT="$WORK/project"
mkdir -p "$PROJECT/src" "$OUT_DIR" "$WORK/vendor"

command -v uv >/dev/null 2>&1 || { lc_log error "build_review_engine.sh: uv is required" >&2; exit 1; }

version="$(python3 - <<'PY'
import tomllib
from pathlib import Path
with Path('pyproject.toml').open('rb') as fh:
    print(tomllib.load(fh)['project']['version'])
PY
)"

while IFS= read -r source; do
    [[ -n "$source" && "$source" != \#* ]] || continue
    [[ -f "$source" ]] || { echo "build_review_engine.sh: allowlisted source missing: $source" >&2; exit 1; }
    if grep -qxF -- "$source" server/review-engine-optional.txt; then
        echo "build_review_engine.sh: $source is listed as left out (server/review-engine-optional.txt)" >&2
        exit 1
    fi
    case "$source" in
        src/*) relative="${source#src/}" ;;
        client/src/*) relative="${source#client/src/}" ;;
        server/src/*) relative="${source#server/src/}" ;;
        *) echo "build_review_engine.sh: unsupported allowlist root: $source" >&2; exit 1 ;;
    esac
    target="$PROJECT/src/$relative"
    mkdir -p "$(dirname "$target")"
    cp "$source" "$target"
done < server/review-engine-files.txt

# Local Review also needs the shared product-aware frontend shell. It contains no
# Python Runtime implementation; server discovery keeps Runtime routes/features
# unavailable when the Runtime product is absent. Build it on demand so the
# standalone Review artifact does not depend on the legacy full bundle.
# LEMONCROW_REVIEW_ENGINE_FRONTEND_DIR packages an already built bundle instead
# (the engine smoke test passes a stub, so it never runs npm in the checkout).
FRONTEND_DIST="${LEMONCROW_REVIEW_ENGINE_FRONTEND_DIR:-frontend/dist}"
if [[ -z "${LEMONCROW_REVIEW_ENGINE_FRONTEND_DIR:-}" ]]; then
    frontend_stale=1
    if [[ -f frontend/dist/index.html ]]; then
        frontend_changed="$(find frontend \
            -type d \( -name node_modules -o -name dist -o -name .git \) -prune -o \
            -type f ! -name '*.tsbuildinfo' -newer frontend/dist/index.html -print -quit)"
        [[ -n "$frontend_changed" ]] || frontend_stale=0
    fi
    if [[ "$frontend_stale" == 1 ]]; then
        command -v npm >/dev/null 2>&1 || { echo "build_review_engine.sh: npm is required to build the frontend" >&2; exit 1; }
        lc_log step "Rebuilding changed Review frontend"
        (cd frontend && { [[ -d node_modules ]] || npm ci --silent; } && npm run build)
    fi
fi
[[ -f "$FRONTEND_DIST/index.html" ]] || { echo "build_review_engine.sh: no frontend bundle at $FRONTEND_DIST" >&2; exit 1; }
rm -rf "$WORK/frontend"
mkdir -p "$WORK/frontend"
cp -R "$FRONTEND_DIST"/. "$WORK/frontend/"
# The engine records which frontend it packaged, machine-readably, in its wheel:
# lemoncrow_server_core/review_engine_build.json says whether frontend/ came from
# this checkout's frontend/dist or from the override, and the digest of the
# index.html that ships beside it. Release qualification must refuse an override.
if [[ -n "${LEMONCROW_REVIEW_ENGINE_FRONTEND_DIR:-}" ]]; then
    frontend_source="LEMONCROW_REVIEW_ENGINE_FRONTEND_DIR"
    lc_log warn "Packaging an override frontend (recorded in review_engine_build.json; not releasable)" >&2
else
    frontend_source="frontend/dist"
fi
python3 - "$PROJECT/src/lemoncrow_server_core/review_engine_build.json" "$WORK/frontend/index.html" "$frontend_source" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

target, index, source = sys.argv[1:4]
record = {
    "schema": 1,
    "frontend": {
        "source": source,
        "override": source != "frontend/dist",
        "index_sha256": hashlib.sha256(Path(index).read_bytes()).hexdigest(),
    },
}
Path(target).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

# The engine is Apache-2.0 like the rest of the public product: its license files ship in the
# wheel (PEP 639) and at the artifact root (audit_release_boundary.py review-engine requires both).
LICENSE_FILES=(LICENSE LICENSE-APACHE NOTICE)
for name in "${LICENSE_FILES[@]}"; do
    [[ -s "$name" ]] || { echo "build_review_engine.sh: license file missing: $name" >&2; exit 1; }
    cp "$name" "$PROJECT/$name"
    cp "$name" "$WORK/$name"
done

cat > "$PROJECT/pyproject.toml" <<EOF_PROJECT
[project]
name = "lemoncrow-review-engine"
version = "$version"
description = "Local Review-only LemonCrow engine and loopback server"
requires-python = ">=3.12,<3.14"
license = "Apache-2.0"
license-files = ["LICENSE", "LICENSE-APACHE", "NOTICE"]
authors = [{ name = "LemonCrow" }]
classifiers = [
  "Programming Language :: Python :: 3",
  "Programming Language :: Python :: 3.12",
  "Programming Language :: Python :: 3.13",
]
dependencies = [
  "aiohttp>=3.14.1",
  "fastapi>=0.139.2",
  "pydantic>=2.6",
  "pygit2==1.19.2",
  "pyyaml>=6.0",
  "regex>=2026.5.9",
  "watchfiles>=1.1.1",
]

[project.scripts]
lemoncrow-review-server = "lemoncrow_server_core.cli:review_main"

[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/lemoncrow", "src/lemoncrow_client", "src/lemoncrow_server_core"]
EOF_PROJECT

lc_verbose step "Building Review-only local engine wheel..."
lc_run uv build --wheel "$PROJECT" --out-dir "$WORK/vendor"
find "$WORK/vendor" -maxdepth 1 -type f ! -name '*.whl' -delete
wheel="$(find "$WORK/vendor" -maxdepth 1 -type f -name 'lemoncrow_review_engine-*-py3-none-any.whl' -print -quit)"
[[ -n "$wheel" ]] || { echo "build_review_engine.sh: expected a py3-none-any Review engine wheel" >&2; exit 1; }

# Byte-level boundary audit before execution. Parent packages may be absent in
# the clean venv, so inspect archive names rather than relying only on find_spec.
python3 - "$wheel" <<'PY'
import sys, zipfile
wheel = sys.argv[1]
forbidden = (
    "lemoncrow/gateway/",
    "lemoncrow/pro/capabilities/code_context/",
    "lemoncrow/pro/capabilities/model_routing/",
    "lemoncrow/pro/capabilities/memory/",
    "lemoncrow/pro/capabilities/swarm/",
    "lemoncrow_client/mcpserver.py",
    "lemoncrow_client/dispatcher.py",
    "lemoncrow_client/localtools/",
    "lemoncrow_client/session.py",
    "lemoncrow_client/routing.py",
    "lemoncrow_server_core/registry_dispatch.py",
    "lemoncrow_server_core/product_telemetry.py",
    "lemoncrow_server_core/tracing.py",
    "lemoncrow_server_core/dashboard_service.py",
)
with zipfile.ZipFile(wheel) as zf:
    names = set(zf.namelist())
    leaks = sorted(name for name in names if any(name.startswith(prefix) for prefix in forbidden))
    required = {
        "lemoncrow/pro/capabilities/review/models.py",
        "lemoncrow/pro/capabilities/review/gate_h_contract.yaml",
        "lemoncrow_server_core/bootstrap.py",
        "lemoncrow_server_core/local_review.py",
        "lemoncrow_client/kit/redaction.py",
        "lemoncrow/infra/storage/migrations/v3_001_review_annotations.sql",
        "lemoncrow_server_core/review_engine_build.json",
    }
    missing = sorted(required - names)
if leaks:
    raise SystemExit("Review engine contains Runtime implementation: " + ", ".join(leaks[:20]))
if missing:
    raise SystemExit("Review engine is missing required files: " + ", ".join(missing))
PY

lc_verbose step "Smoke-testing Review engine in a clean venv..."
SMOKE="$WORK/smoke"
lc_run uv venv --python "${LEMONCROW_BUILD_PYTHON:-3.13}" "$SMOKE"
lc_run uv pip install --python "$SMOKE/bin/python" "$wheel"
HOME="$WORK/home" "$SMOKE/bin/python" -m lemoncrow_server_core up \
    --directory "$WORK/state" --local-no-auth --product review --ephemeral --check-only
LEMONCROW_REVIEW_SMOKE_STATE="$WORK/state-capabilities" "$SMOKE/bin/python" - <<'PY'
import importlib.util
import os
from pathlib import Path

from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig

for forbidden in (
    "lemoncrow.gateway",
    "lemoncrow_client.mcpserver",
    "lemoncrow_server_core.registry_dispatch",
    "lemoncrow_server_core.dashboard_service",
    "lemoncrow_server_core.runtime_product_surface",
):
    try:
        found = importlib.util.find_spec(forbidden)
    except ModuleNotFoundError:
        found = None
    assert found is None, forbidden

state_root = Path(os.environ["LEMONCROW_REVIEW_SMOKE_STATE"]).resolve()
config = LocalServerConfig(
    state_root=state_root,
    local_no_auth=True,
    ephemeral=True,
    products=frozenset({"review"}),
)
server, app = build_local_server(config)
identity = app._server_identity()
assert identity["products"]["review"]["available"] is True
assert identity["products"]["runtime"]["available"] is False
assert identity["product_capabilities"]["review"]["ask"] is True
assert app._advertised_mcp_tools() == []
# A Review store can be created: every migration it declares ships and applies.
import sqlite3
from contextlib import closing

from lemoncrow.pro.capabilities.review.store import ReviewStore

review_store = ReviewStore(state_root / "review-store")
assert review_store.get_session("absent") is None
with closing(sqlite3.connect(review_store.db_path)) as conn:
    applied = {row[0] for row in conn.execute("SELECT name FROM _schema_migrations")}
assert applied == set(ReviewStore.MIGRATIONS), sorted(set(ReviewStore.MIGRATIONS) - applied)
# Readiness evaluates the packaged Gate H contract, not a source-checkout file: it must resolve here.
from lemoncrow.pro.capabilities.review.evidence_qualification import runtime_gate_h_config
gate_h = runtime_gate_h_config()
assert not gate_h.source.startswith("built-in defaults"), gate_h.source
assert gate_h.mode == "report" and gate_h.thresholds.approved, (gate_h.mode, gate_h.thresholds.approval_problem())
from lemoncrow.pro.capabilities.review import ask, intelligence_drivers, tool_server
from lemoncrow.pro.capabilities.review.state_events import state_events
assert ask is not None and intelligence_drivers is not None and tool_server.SERVER_NAME == "lemoncrow-review"
assert callable(state_events), "Review event streams must import with bundled dependencies"
os.environ["LEMONCROW_MCP_TOOL_PROFILE"] = "review_autonomous"
listed = tool_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
assert listed is not None
assert {item["name"] for item in listed["result"]["tools"]} == {"code_search", "grep", "read"}
if server.state.workspace is not None:
    server.state.workspace.close()
server.state.backend.close_backend()
PY

archive="$OUT_DIR/lemoncrow-review-engine.tar.gz"
rm -f "$archive" "$archive.sha256"
COPYFILE_DISABLE=1 tar -czf "$archive" -C "$WORK" vendor frontend "${LICENSE_FILES[@]}"
# Content-level boundary audit of the shipped artifact (no semantic ANN, model
# libraries, private imports or learned assets), shared with the source audit.
lc_run uv run python scripts/audit_release_boundary.py review-engine "$archive"
if command -v sha256sum >/dev/null 2>&1; then
    digest="$(sha256sum "$archive" | awk '{print $1}')"
elif command -v shasum >/dev/null 2>&1; then
    digest="$(shasum -a 256 "$archive" | awk '{print $1}')"
else
    echo "build_review_engine.sh: sha256sum or shasum is required" >&2
    exit 1
fi
printf '%s\n' "$digest" >"$archive.sha256"
lc_log success "Review engine built and verified"
lc_verbose info "Artifact: $archive"
