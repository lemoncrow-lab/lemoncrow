#!/usr/bin/env bash
# build_bundle.sh — Build a LemonCrow harbor bundle FROM SCRATCH, every time.
#
# Unlike rebuild_bundle.sh (which patches an *existing* bundle in place, and
# so leaks state across runs -- run N+1 can inherit whatever run N left in
# /opt), this script never reads a prior bundle. Every fresh `lc benchmark
# harbor` invocation gets a bundle built clean from current source, so
# back-to-back runs can't contaminate each other. Only `--resume` should
# reuse an existing bundle (the one already pinned to that job dir); a fresh
# run always gets a new one.
#
# Runs in debian:bullseye-slim so the /opt paths + glibc match the runtime
# image used by the harbor container.
#
#   docker run --rm \
#     -v <repo>:/lemoncrow:ro \
#     -v /tmp/avbuild:/out \
#     debian:bullseye-slim bash /lemoncrow/benchmarks/harbor/build_bundle.sh
#
# Writes /out/lemoncrow-bundle-new.tar.gz (caller verifies + swaps into place).
set -euo pipefail

echo "==> pinning Bullseye APT to final-LTS snapshots"
cat >/etc/apt/sources.list <<'EOF'
deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260831T235959Z/ bullseye main
deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/20260831T235959Z/ bullseye-security main
EOF
rm -rf /etc/apt/sources.list.d/* /var/lib/apt/lists/*
echo "==> apt-get update/install (curl, ca-certificates, git)"
apt-get -o Acquire::Check-Valid-Until=false update -qq
apt-get install -y -qq curl ca-certificates git build-essential

export HOME=/root
echo "==> installing uv"
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="/root/.local/bin:$PATH"
uv --version

# Fresh python + fresh venv every run -- /opt is scratch inside this
# throwaway container, so there is no prior state to inherit from.
export UV_PYTHON_INSTALL_DIR=/opt/uvpy
echo "==> installing python 3.13"
uv python install 3.13

echo "==> creating venvs"
uv venv --python 3.13 /opt/lemoncrow-venv
uv venv --python 3.13 /opt/headroom-venv

# Same as the incremental refresh: the runtime bundle ships pure-Python lemoncrow
# (no mypyc .so). ENABLE_MYPYC=0 avoids needing a C toolchain here and avoids the
# hook's mypy run writing .mypy_cache into the (read-only) source.
export LEMONCROW_ENABLE_MYPYC=0

echo "==> copying source for install"
mkdir -p /tmp/src/benchmarks
cp -a /lemoncrow/pyproject.toml /lemoncrow/hatch_build.py /lemoncrow/README.md /lemoncrow/LICENSE /tmp/src/
cp -a /lemoncrow/src /lemoncrow/integrations /lemoncrow/vendor /lemoncrow/client /lemoncrow/server /tmp/src/
# Root pyproject declares benchmarks as a workspace member. Only its metadata is
# needed to discover the workspace while installing the root package; copying
# the full benchmark tree would drag historical result artifacts into the build
# context for no runtime benefit.
cp -a /lemoncrow/benchmarks/pyproject.toml /tmp/src/benchmarks/

# Full parity install (matches scripts/bundle.sh's production extras set) --
# every dep resolved fresh from current source + PyPI, not patched onto an
# old venv.
echo "==> installing lemoncrow[mcp,memory,smart,cloud,postgres,vector,parsers,rename] from current source"
uv pip install \
  --python /opt/lemoncrow-venv/bin/python \
  "/tmp/src[mcp,memory,smart,cloud,postgres,vector,parsers,rename]"

# uv treats workspace dependencies as editable when resolving the root project.
# That is correct for development but fatal in a portable bundle: the generated
# .pth would point at /tmp/src/client/src, which disappears with this build
# container. Replace the workspace link with a normal wheel install, then remove
# the source tree before verification so no editable path can accidentally pass.
uv pip install \
  --python /opt/lemoncrow-venv/bin/python \
  --no-deps --reinstall-package lemoncrow-client \
  /tmp/src/client
# The thin client now requires the public loopback server in local mode. Harbor
# task containers are isolated, so ship the server package in the same portable
# venv and start one server per trial. --no-deps prevents server's local source
# reference from turning the already-portable root package editable again.
uv pip install \
  --python /opt/lemoncrow-venv/bin/python \
  --no-deps \
  /tmp/src/server
rm -rf /tmp/src

# Headroom stays isolated from LemonCrow's dependency graph. The Harbor bundle
# carries a separate venv so the raw/RTK/LC arms use the exact same LemonCrow
# environment while the Headroom arm can compress only fresh MCP tool results.
HEADROOM_VERSION="${LEMONCROW_BENCH_HEADROOM_VERSION:-0.37.0}"
echo "==> installing headroom-ai[proxy]==${HEADROOM_VERSION} into isolated benchmark venv"
uv pip install \
  --python /opt/headroom-venv/bin/python \
  "headroom-ai[proxy]==${HEADROOM_VERSION}"

# Verify the built bundle surface before we trust it.
echo "==> verifying bundle surface"
/opt/lemoncrow-venv/bin/python - <<'PY'
import inspect
import lemoncrow
import lemoncrow_client
import lemoncrow_server_core
from lemoncrow.pro.capabilities.code_context import engine
assert hasattr(engine, "IndexLockTimeout"), "IndexLockTimeout missing"
assert hasattr(engine, "_index_lock_timeout_s"), "_index_lock_timeout_s missing"
src = inspect.getsource(engine.CodeContextEngine.index_repo)
assert "require_lock" in src, "require_lock missing from index_repo"
import pygit2  # native dep must load
print(
    "VERIFY_OK lemoncrow=%s client=%s server=%s pygit2=%s"
    % (getattr(lemoncrow, "__version__", "?"), lemoncrow_client.__file__, lemoncrow_server_core.__file__, pygit2.__version__)
)
PY
/opt/lemoncrow-venv/bin/lemoncrow-server --help >/dev/null
/opt/headroom-venv/bin/python -c 'import headroom; print("HEADROOM_VERIFY_OK", headroom.__file__)'
/opt/headroom-venv/bin/headroom --version

# Pack isolated LemonCrow + Headroom venvs with the shared portable Python.
echo "==> packing bundle"
tar -C /opt -czf /out/lemoncrow-bundle-new.tar.gz lemoncrow-venv headroom-venv uvpy
echo "BUILD_OK bytes=$(stat -c%s /out/lemoncrow-bundle-new.tar.gz)"
