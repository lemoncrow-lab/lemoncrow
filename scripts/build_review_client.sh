#!/usr/bin/env bash
# Build the Review-only LemonCrow client artifact from an explicit module allowlist.
set -euo pipefail
# Smoke tests assert the absent-product messages; never lazy-fetch from the network.
export LEMONCROW_NO_LAZY_INSTALL=1
# Smoke imports must resolve the installed wheel, never a checkout on PYTHONPATH.
unset PYTHONPATH

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
cd "$ROOT"
source "$ROOT/scripts/lib/terminal.sh"
OUT_DIR="${LEMONCROW_REVIEW_DIST_DIR:-$ROOT/dist}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/lemoncrow-review-build.XXXXXX")"
trap 'rm -rf "${WORK:-}"' EXIT
PROJECT="$WORK/project"
PKG="$PROJECT/src/lemoncrow_client"
mkdir -p "$OUT_DIR" "$PKG" "$WORK/vendor"

command -v uv >/dev/null 2>&1 || { lc_log error "build_review_client.sh: uv is required" >&2; exit 1; }

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
    [[ -f "$source" ]] || { echo "build_review_client.sh: allowlisted module missing: $source" >&2; exit 1; }
    mkdir -p "$(dirname "$PKG/$relative")"
    cp "$source" "$PKG/$relative"
done < client/review-files.txt

cat > "$PROJECT/pyproject.toml" <<EOF_PROJECT
[project]
name = "lemoncrow-review-client"
version = "$version"
description = "Review-only LemonCrow client: Core/auth plus immutable Review capture"
requires-python = ">=3.12,<3.14"
dependencies = []

[project.scripts]
lemoncrow-review-client = "lemoncrow_client.cli:main"
lc = "lemoncrow_client.cli:main"
lcr = "lemoncrow_client.cli:review_entrypoint"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/lemoncrow_client"]
EOF_PROJECT

lc_verbose step "Building Review-only LemonCrow client wheel..."
lc_run uv build --wheel "$PROJECT" --out-dir "$WORK/vendor"
find "$WORK/vendor" -maxdepth 1 -type f ! -name '*.whl' -delete
wheel="$(find "$WORK/vendor" -maxdepth 1 -type f -name 'lemoncrow_review_client-*-py3-none-any.whl' -print -quit)"
[[ -n "$wheel" ]] || { echo "build_review_client.sh: expected a py3-none-any Review wheel" >&2; exit 1; }

lc_verbose step "Smoke-testing installed Review wheel..."
SMOKE="$WORK/smoke"
lc_run uv venv --python "${LEMONCROW_BUILD_PYTHON:-3.13}" "$SMOKE"
lc_run uv pip install --python "$SMOKE/bin/python" --no-index --no-deps "$wheel"
HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" "$SMOKE/bin/lcr" --help >/dev/null
HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" "$SMOKE/bin/lc" --version >/dev/null
"$SMOKE/bin/python" - <<'PY_SMOKE'
import importlib
from importlib.util import find_spec
from pathlib import Path

import lemoncrow_client.review
import lemoncrow_client.review_base
import lemoncrow_client.review_session

# Every module of the allowlist imports from the installed wheel. A module that names one the allowlist lacks
# (lemoncrow_client.session once did, and `lc review` crashed after capture) is found here, not by a user.
for line in Path("client/review-files.txt").read_text().splitlines():
    line = line.strip()
    if not line.endswith(".py") or line.endswith("__main__.py"):
        continue
    stem = line[: -len(".py")].replace("/", ".")
    importlib.import_module("lemoncrow_client" if stem == "__init__" else "lemoncrow_client." + stem.removesuffix(".__init__"))

for forbidden in (
    "lemoncrow_client.mcpserver",
    "lemoncrow_client.dispatcher",
    "lemoncrow_client.localtools",
    "lemoncrow_client.session",
    "lemoncrow_client.routing",
    "lemoncrow_client.autosync",
    "lemoncrow_client.trace",
    "lemoncrow_client.liveview",
    "lemoncrow_client.host_trial",
    "lemoncrow_client.lite",
):
    assert find_spec(forbidden) is None, forbidden
PY_SMOKE
set +e
review_runtime_message="$(HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" "$SMOKE/bin/lc" runtime status 2>&1)"
review_runtime_status=$?
set -e
[[ "$review_runtime_status" == "2" && "$review_runtime_message" == *"Runtime is not installed"* ]] \
    || { echo "build_review_client.sh: Review wheel did not refuse absent Runtime cleanly" >&2; exit 1; }
set +e
review_rt_message="$(HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" "$SMOKE/bin/lc" rt status 2>&1)"
review_rt_status=$?
set -e
[[ "$review_rt_status" == "2" && "$review_rt_message" == *"Runtime is not installed"* ]] \
    || { echo "build_review_client.sh: lc rt did not delegate to absent Runtime cleanly" >&2; exit 1; }
HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" "$SMOKE/bin/lc" status >/dev/null
for expected in -- --ai "hook install" init-action "review config"; do
    HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" "$SMOKE/bin/lc" review --help | grep -qF -e "$expected" \
        || { echo "build_review_client.sh: lc review --help does not mention: $expected" >&2; exit 1; }
done
set +e
review_ai_message="$(HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" "$SMOKE/bin/lc" review --ai 2>&1)"
review_ai_status=$?
set -e
[[ "$review_ai_status" == "2" && "$review_ai_message" == *"needs the local Review engine"* && "$review_ai_message" != *Traceback* ]] \
    || { echo "build_review_client.sh: lc review --ai did not report a missing engine cleanly (status $review_ai_status): $review_ai_message" >&2; exit 1; }
set +e
promoted_output="$(HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" "$SMOKE/bin/lc" code --help 2>&1)"
promoted_status=$?
set -e
[[ "$promoted_status" == "2" && "$promoted_output" == *"Runtime is not installed"* ]] \
    || { echo "build_review_client.sh: Review artifact did not delegate lc code to absent Runtime cleanly" >&2; exit 1; }
set +e
review_with_message="$(HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" "$SMOKE/bin/lc" with claude 2>&1)"
review_with_status=$?
set -e
[[ "$review_with_status" == "2" && "$review_with_message" == *"Runtime is not installed"* ]] \
    || { echo "build_review_client.sh: Review wheel did not refuse lc with without Runtime" >&2; exit 1; }
for removed_alias in mcp install routes; do
    set +e
    alias_output="$(HOME="$WORK/home" LEMONCROW_HOME="$WORK/state" "$SMOKE/bin/lc" "$removed_alias" 2>&1)"
    alias_status=$?
    set -e
    [[ "$alias_status" == "2" && "$alias_output" == *"nknown command"* ]] \
        || { echo "build_review_client.sh: removed top-level Runtime alias still resolves: $removed_alias (status $alias_status): $alias_output" >&2; exit 1; }
done

archive="$OUT_DIR/lemoncrow-review-client.tar.gz"
rm -f "$archive" "$archive.sha256"
COPYFILE_DISABLE=1 tar -czf "$archive" -C "$WORK" vendor
lc_run uv run python scripts/audit_release_boundary.py review "$archive"

if command -v sha256sum >/dev/null 2>&1; then
    digest="$(sha256sum "$archive" | awk '{print $1}')"
elif command -v shasum >/dev/null 2>&1; then
    digest="$(shasum -a 256 "$archive" | awk '{print $1}')"
else
    echo "build_review_client.sh: sha256sum or shasum is required" >&2
    exit 1
fi
printf '%s\n' "$digest" >"$archive.sha256"
lc_log success "Review client built and verified"
lc_verbose info "Artifact: $archive"
