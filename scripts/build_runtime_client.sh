#!/usr/bin/env bash
# Build the Runtime-only LemonCrow thin client artifact from an explicit module allowlist.
set -euo pipefail
# Smoke tests assert the absent-product messages; never lazy-fetch from the network.
export LEMONCROW_NO_LAZY_INSTALL=1
# Smoke imports must resolve the installed wheel, never a checkout on PYTHONPATH.
unset PYTHONPATH

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
cd "$ROOT"
source "$ROOT/scripts/lib/terminal.sh"
OUT_DIR="${LEMONCROW_RUNTIME_DIST_DIR:-$ROOT/dist}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/lemoncrow-runtime-build.XXXXXX")"
trap 'rm -rf "${WORK:-}"' EXIT
PROJECT="$WORK/project"
PKG="$PROJECT/src/lemoncrow_client"
mkdir -p "$OUT_DIR" "$PKG" "$WORK/vendor"

command -v uv >/dev/null 2>&1 || { lc_log error "build_runtime_client.sh: uv is required" >&2; exit 1; }

version="$(python3 - <<'PY'
import tomllib
from pathlib import Path
with Path('client/pyproject.toml').open('rb') as fh:
    print(tomllib.load(fh)['project']['version'])
PY
)"

while IFS= read -r relative; do
    [[ -n "$relative" && "$relative" != \#* ]] || continue
    source="client/src/lemoncrow_client/$relative"
    [[ -f "$source" ]] || { echo "build_runtime_client.sh: allowlisted module missing: $source" >&2; exit 1; }
    mkdir -p "$(dirname "$PKG/$relative")"
    cp "$source" "$PKG/$relative"
done < client/runtime-files.txt

cat > "$PROJECT/pyproject.toml" <<EOF_PROJECT
[project]
name = "lemoncrow-runtime-client"
version = "$version"
description = "Runtime-only LemonCrow thin client: MCP, code intelligence and agent integrations"
requires-python = ">=3.12,<3.14"
dependencies = []

[project.scripts]
lemoncrow-runtime-client = "lemoncrow_client.cli:main"
lemoncrow-client = "lemoncrow_client.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/lemoncrow_client"]
EOF_PROJECT

lc_verbose step "Building Runtime-only LemonCrow client wheel..."
lc_run uv build --wheel "$PROJECT" --out-dir "$WORK/vendor"
find "$WORK/vendor" -maxdepth 1 -type f ! -name '*.whl' -delete
wheel="$(find "$WORK/vendor" -maxdepth 1 -type f -name 'lemoncrow_runtime_client-*-py3-none-any.whl' -print -quit)"
[[ -n "$wheel" ]] || { echo "build_runtime_client.sh: expected a py3-none-any Runtime wheel" >&2; exit 1; }

lc_verbose step "Smoke-testing installed Runtime wheel..."
SMOKE="$WORK/smoke"
lc_run uv venv --python "${LEMONCROW_BUILD_PYTHON:-3.13}" "$SMOKE"
lc_run uv pip install --python "$SMOKE/bin/python" --no-index --no-deps "$wheel"
HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" "$SMOKE/bin/lemoncrow-runtime-client" --version >/dev/null
"$SMOKE/bin/python" - <<'PY_SMOKE'
from importlib.util import find_spec
import lemoncrow_client.mcpserver
import lemoncrow_client.dispatcher
import lemoncrow_client.host_trial

assert find_spec("lemoncrow_client.review") is None
assert find_spec("lemoncrow_client.review_session") is None
PY_SMOKE
set +e
runtime_review_message="$(HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" "$SMOKE/bin/lemoncrow-runtime-client" review --help 2>&1)"
runtime_review_status=$?
set -e
[[ "$runtime_review_status" == "2" && "$runtime_review_message" == *"Review is not installed"* ]] \
    || { echo "build_runtime_client.sh: Runtime wheel did not refuse absent Review cleanly" >&2; exit 1; }
HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" "$SMOKE/bin/lemoncrow-runtime-client" runtime routes >/dev/null
HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" "$SMOKE/bin/lemoncrow-runtime-client" rt routes >/dev/null
HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" "$SMOKE/bin/lemoncrow-runtime-client" runtime mcp --host smoke </dev/null >/dev/null
LITE_REPO="$WORK/lite-repo"
mkdir -p "$LITE_REPO"
git -C "$LITE_REPO" init -q
printf 'def runtime_artifact_lite_probe():\n    return 1\n' > "$LITE_REPO/probe.py"
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
    | HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" LEMONCROW_WORKSPACE_ROOT="$LITE_REPO" \
      "$SMOKE/bin/lemoncrow-runtime-client" runtime lite-mcp --host smoke >"$WORK/runtime-lite-smoke.jsonl"
grep -q '"name":"lemoncrow-runtime-lite"' "$WORK/runtime-lite-smoke.jsonl"
HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" "$SMOKE/bin/lemoncrow-runtime-client" status >/dev/null
code_help="$(HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" "$SMOKE/bin/lemoncrow-runtime-client" code --help)"
[[ "$code_help" == *"Hosted coding agent"* && "$code_help" == *"not the open-source local Runtime Lite launcher"* ]] \
    || { echo "build_runtime_client.sh: Runtime artifact does not expose Hosted lc code boundary" >&2; exit 1; }
[[ "$code_help" != *"--pro"* ]] \
    || { echo "build_runtime_client.sh: Runtime artifact still advertises removed --pro compatibility" >&2; exit 1; }
HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" \
    "$SMOKE/bin/lemoncrow-runtime-client" runtime index --repo-root "$LITE_REPO" --json \
    | grep -q 'runtime-lite-v3.sqlite3'
set +e
legacy_index_output="$(HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" "$SMOKE/bin/lemoncrow-runtime-client" code index 2>&1)"
legacy_index_status=$?
set -e
[[ "$legacy_index_status" == "2" && "$legacy_index_output" == *"unknown Hosted code option: index"* ]] \
    || { echo "build_runtime_client.sh: Runtime artifact still accepts legacy lc code index" >&2; exit 1; }
with_help="$(HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" "$SMOKE/bin/lemoncrow-runtime-client" with --help)"
[[ "$with_help" == *"pi"* ]] \
    || { echo "build_runtime_client.sh: Runtime artifact does not expose public Pi trial integration" >&2; exit 1; }
HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" \
    "$SMOKE/bin/lemoncrow-runtime-client" runtime host status --engine pi --json \
    | grep -q '"installed": false'
HOME="$WORK/home" PI_CODING_AGENT_DIR="$WORK/home/.pi/agent" "$SMOKE/bin/python" - <<'PY_PI_ASSET'
from pathlib import Path
import lemoncrow_client
from lemoncrow_client import pi_integration
asset = Path(lemoncrow_client.__file__).resolve().parent / "assets" / "pi_lite.mjs"
assert asset.is_file(), asset
text = asset.read_text(encoding="utf-8")
for tool in ("bash", "code_search", "edit", "read", "web_fetch"):
    assert f'"{tool}"' in text
settings = pi_integration.install()
assert settings.is_file()
assert pi_integration.enabled()
assert pi_integration.remove()
assert not pi_integration.enabled()
PY_PI_ASSET
for removed_alias in mcp install routes; do
    set +e
    alias_output="$(HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" "$SMOKE/bin/lemoncrow-runtime-client" "$removed_alias" 2>&1)"
    alias_status=$?
    set -e
    [[ "$alias_status" == "2" && "$alias_output" == *"runtime COMMAND"* ]] \
        || { echo "build_runtime_client.sh: removed top-level Runtime alias still resolves: $removed_alias" >&2; exit 1; }
done

archive="$OUT_DIR/lemoncrow-runtime-client.tar.gz"
rm -f "$archive" "$archive.sha256"
COPYFILE_DISABLE=1 tar -czf "$archive" -C "$WORK" vendor
lc_run uv run python scripts/audit_release_boundary.py runtime "$archive"

if command -v sha256sum >/dev/null 2>&1; then
    digest="$(sha256sum "$archive" | awk '{print $1}')"
elif command -v shasum >/dev/null 2>&1; then
    digest="$(shasum -a 256 "$archive" | awk '{print $1}')"
else
    echo "build_runtime_client.sh: sha256sum or shasum is required" >&2
    exit 1
fi
printf '%s\n' "$digest" >"$archive.sha256"
lc_log success "Runtime client built and verified"
lc_verbose info "Artifact: $archive"
