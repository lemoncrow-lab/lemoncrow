#!/usr/bin/env bash
# build.sh — Build a production-ready LemonCrow distribution archive.
#
# This is the main entrypoint for CI and local release builds.
set -euo pipefail

# 1. Clean ONLY local build/dist/bundle artifacts so the wheel and release
#    archive are rebuilt fresh. The uv cache at ~/.cache/uv is deliberately left
#    untouched — we want a rebuild that REUSES the cache, never a cold
#    re-download. Do not add --no-cache/--refresh/--upgrade to any uv command
#    below.
echo "◆ Cleaning local build artifacts (uv cache preserved)..."
rm -rf build/ dist/ bundle/
mkdir -p build/ dist/ bundle/bin bundle/frontend bundle/scripts

# Prime the uv cache from the committed lockfile before building. `uv build`
# resolves its own isolated build environment; priming with --frozen pulls the
# locked wheels into ~/.cache/uv first so the build resolves from cache instead
# of stalling on a PyPI "resolving packages" round-trip.
if [ -f "uv.lock" ]; then
    echo "◆ Priming uv cache from uv.lock (uv sync --frozen)..."
    uv sync --frozen || echo "  (uv sync --frozen skipped; continuing)"
fi

# 2. Build Frontend
echo "◆ Building Frontend..."
if [ -d "frontend" ]; then
    cd frontend && npm ci --silent && npm run build && cd ..
    rm -rf bundle/frontend/*
    cp -r frontend/dist/* bundle/frontend/
fi

# 3. Build mypyc-compiled wheel
# hatch_build.py hook compiles ~440 modules with mypyc (skip with LEMONCROW_SKIP_MYPYC=1),
# strips .py source for compiled modules, and packages a platform-specific wheel.
# Refresh the model pricing snapshot from the litellm version pinned in uv.lock.
# This runs before the wheel build so the wheel ships the freshest data available
# without requiring litellm at runtime.
echo "◆ Refreshing model prices from litellm..."
uv run --with "litellm>=1.83.14" python scripts/refresh_model_prices.py || \
    echo "  (refresh skipped; bundled snapshot will be used)"

echo "◆ Building mypyc wheel (this takes a few minutes)..."
rm -rf dist/
uv build --wheel
WHEEL_PATH="$(ls dist/lemoncrow-*.whl | head -1)"
if [[ -z "$WHEEL_PATH" ]]; then
    echo "ERROR: wheel not found in dist/" >&2
    exit 1
fi
echo "  $(du -sh "$WHEEL_PATH" | awk '{print $1}')  $WHEEL_PATH"

# 4. Place wheel in bundle/bin/ (install.sh picks it up by glob)
echo "◆ Staging wheel..."
rm -rf bundle/bin/
mkdir -p bundle/bin
cp "$WHEEL_PATH" bundle/bin/

# 5. Include distribution scripts
echo "◆ Including distribution scripts..."
cp -f scripts/install.sh bundle/scripts/install.sh
cp -f scripts/sessions.sh bundle/scripts/sessions.sh
cp -f scripts/bundle.sh bundle/scripts/bundle.sh

# Export the locked dependency set as a constraints file. The bundle ships a
# prebuilt wheel (no uv.lock / source), so on a cold end-user machine
# `uv tool install` would otherwise resolve ~293 unbounded `>=` deps from PyPI
# — the "stuck resolving packages" hang. bundle.sh passes this file via
# `uv tool install -c` to pin every transitive dep to its locked version, so
# resolution is deterministic with no version search. The markers uv emits make
# a single file valid across every release platform.
if [ -f "uv.lock" ]; then
    echo "◆ Exporting dependency constraints (uv export)..."
    uv export --frozen --no-emit-project --no-hashes \
        --extra mcp \
        --extra memory \
        --extra smart \
        --extra cloud \
        --extra postgres \
        --extra vector \
        --extra parsers \
        --extra rename \
        --extra ortools \
        --extra litellm \
        -o bundle/constraints.txt \
        >/dev/null \
        || echo "  (constraints export skipped; install will resolve from PyPI)"

    # uv export emits local-path deps (the babel stub) as a bare, unnamed,
    # build-machine-relative path -- `uv tool install -c` rejects unnamed
    # constraint entries outright. Ship the wheel alongside the bundle;
    # bundle.sh rewrites the constraint line to a named, absolute file:// URL
    # pointing at it at install time (the path isn't known until then).
    if [ -f "bundle/constraints.txt" ] && grep -q "vendor/babel-" bundle/constraints.txt; then
        mkdir -p bundle/vendor
        cp vendor/babel-*.whl bundle/vendor/
    fi
fi

# Bundle all host integration scripts so install.sh can run them after binary install.
echo "◆ Bundling host integration scripts..."
for s in scripts/install_hosts.sh scripts/install_agents.sh \
          scripts/install_antigravity.sh scripts/install_claude.sh \
          scripts/install_codex.sh scripts/install_copilot.sh \
          scripts/install_cursor.sh scripts/install_hermes.sh \
          scripts/install_opencode.sh \
          scripts/build_host_skills.sh scripts/sync_agent_context.py; do
    [[ -f "$s" ]] && cp -f "$s" "bundle/scripts/$(basename "$s")"
done
# Bundle lib/ (shared installer functions + managed context helpers).
mkdir -p bundle/scripts/lib
cp -f scripts/lib/common.sh bundle/scripts/lib/common.sh
cp -f scripts/lib/managed_context.sh bundle/scripts/lib/managed_context.sh
cp -f scripts/lib/versions.sh bundle/scripts/lib/versions.sh

# Bundle integration files (pre-generated .md/.json/.sh per-host configs).
echo "◆ Bundling host integration configs..."
mkdir -p bundle/integrations
for host in agents antigravity claude codex copilot copilot-cli cursor hermes opencode shared skills; do
    [[ -d "integrations/$host" ]] && cp -r "integrations/$host" "bundle/integrations/$host"
done
# Top-level files (e.g. AGENTS.lemoncrow.md) used by install_codex.sh and install_agents.sh
[[ -f "integrations/AGENTS.lemoncrow.md" ]] && cp -f "integrations/AGENTS.lemoncrow.md" "bundle/integrations/AGENTS.lemoncrow.md"

# Pre-generate host context files in the staged bundle so install scripts work
# without uv/Python, without rewriting generated files in the source checkout.
echo "◆ Pre-generating bundled host context files..."
uv run python3 bundle/scripts/sync_agent_context.py >/dev/null 2>&1 || true

chmod +x bundle/scripts/*.sh 2>/dev/null || true

# 6. Create Archive
echo "◆ Creating Archive..."
mkdir -p dist
OS_NAME="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
# Normalize arch identically to install.sh/sessions.sh so the produced asset
# name always matches what the downloader looks for (e.g. amd64 -> x86_64).
case "$ARCH" in
    amd64) ARCH="x86_64" ;;
    arm64) ARCH="arm64" ;;
    aarch64) ARCH="aarch64" ;;
esac
ARCHIVE_NAME="dist/lemoncrow-distribution-${OS_NAME}-${ARCH}.tar.gz"

rm -f "$ARCHIVE_NAME"
tar -czf "$ARCHIVE_NAME" -C bundle .
echo "✓ Production bundle complete: $ARCHIVE_NAME"
echo "  $(du -sh "$ARCHIVE_NAME" | awk '{print $1}')  (wheel + scripts)"
