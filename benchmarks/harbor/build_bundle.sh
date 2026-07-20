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

echo "==> apt-get update/install (curl, ca-certificates, git)"
apt-get update -qq
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

echo "==> creating venv"
uv venv --python 3.13 /opt/lemoncrow-venv

# Same as the incremental refresh: the runtime bundle ships pure-Python lemoncrow
# (no mypyc .so). ENABLE_MYPYC=0 avoids needing a C toolchain here and avoids the
# hook's mypy run writing .mypy_cache into the (read-only) source.
export LEMONCROW_ENABLE_MYPYC=0

echo "==> copying source for install"
mkdir -p /tmp/src
cp -a /lemoncrow/pyproject.toml /lemoncrow/hatch_build.py /lemoncrow/README.md /lemoncrow/LICENSE /tmp/src/
cp -a /lemoncrow/src /lemoncrow/integrations /lemoncrow/vendor /tmp/src/

# Full parity install (matches scripts/bundle.sh's production extras set) --
# every dep resolved fresh from current source + PyPI, not patched onto an
# old venv.
echo "==> installing lemoncrow[mcp,memory,smart,cloud,postgres,vector,parsers,rename] from current source"
uv pip install \
  --python /opt/lemoncrow-venv/bin/python \
  "/tmp/src[mcp,memory,smart,cloud,postgres,vector,parsers,rename]"

# Verify the built bundle surface before we trust it.
echo "==> verifying bundle surface"
/opt/lemoncrow-venv/bin/python - <<'PY'
import inspect
import lemoncrow
from lemoncrow.pro.capabilities.code_context import engine
assert hasattr(engine, "IndexLockTimeout"), "IndexLockTimeout missing"
assert hasattr(engine, "_index_lock_timeout_s"), "_index_lock_timeout_s missing"
src = inspect.getsource(engine.CodeContextEngine.index_repo)
assert "require_lock" in src, "require_lock missing from index_repo"
import pygit2  # native dep must load
print("VERIFY_OK lemoncrow=%s pygit2=%s" % (getattr(lemoncrow, "__version__", "?"), pygit2.__version__))
PY

# Pack with the same tar layout rebuild_bundle.sh / setup_preflight.sh expect
# (lemoncrow-venv/ + uvpy/ at root).
echo "==> packing bundle"
tar -C /opt -czf /out/lemoncrow-bundle-new.tar.gz lemoncrow-venv uvpy
echo "BUILD_OK bytes=$(stat -c%s /out/lemoncrow-bundle-new.tar.gz)"
