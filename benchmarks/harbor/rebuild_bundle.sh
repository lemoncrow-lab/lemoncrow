#!/usr/bin/env bash
# Refresh the portable LemonCrow bundle's *lemoncrow* package from current source
# WITHOUT recompiling native deps. lemoncrow is pure Python, so we reinstall just
# it (--no-deps) into the existing bundle venv; tree-sitter / pygit2 / etc. stay
# as compiled. Runs in debian:bullseye-slim so the /opt paths + glibc match the
# runtime image. For a from-scratch native rebuild (changed deps) this is NOT
# enough -- do a full bullseye build instead.
#
#   docker run --rm \
#     -v <repo>:/lemoncrow:ro \
#     -v /tmp/avbuild:/out \
#     debian:bullseye-slim bash /lemoncrow/benchmarks/harbor/rebuild_bundle.sh
#
# Reads  /out/lemoncrow-bundle.tar.gz  (existing bundle)
# Writes /out/lemoncrow-bundle-new.tar.gz  (refreshed; caller verifies + swaps)
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

# Restore the existing bundle into /opt, as the runtime does.
echo "==> restoring existing bundle into /opt"
tar -C /opt -xzf /out/lemoncrow-bundle.tar.gz
test -x /opt/lemoncrow-venv/bin/python
export UV_PYTHON_INSTALL_DIR=/opt/uvpy
if [ ! -x /opt/headroom-venv/bin/python ]; then
  echo "==> adding isolated Headroom benchmark venv"
  uv venv --python /opt/lemoncrow-venv/bin/python /opt/headroom-venv
fi
HEADROOM_VERSION="${LEMONCROW_BENCH_HEADROOM_VERSION:-0.37.0}"
uv pip install --python /opt/headroom-venv/bin/python "headroom-ai[proxy]==${HEADROOM_VERSION}"

# The original bundle is pure-Python lemoncrow (no mypyc .so), so keep the mypyc
# build hook off to match it -- also avoids needing a C toolchain here, and avoids
# the hook's mypy run writing .mypy_cache into the (read-only) source.
export LEMONCROW_ENABLE_MYPYC=0

# Build from a WRITABLE copy (repo is mounted read-only). Copy only what the
# wheel build needs: packages=src/lemoncrow + force-include integrations + the
# custom hook + project metadata.
echo "==> copying source for wheel build"
mkdir -p /tmp/src/benchmarks
cp -a /lemoncrow/pyproject.toml /lemoncrow/hatch_build.py /lemoncrow/README.md /lemoncrow/LICENSE /tmp/src/
cp -a /lemoncrow/src /lemoncrow/integrations /lemoncrow/vendor /lemoncrow/client /tmp/src/
cp -a /lemoncrow/benchmarks/pyproject.toml /tmp/src/benchmarks/

# Reinstall ONLY lemoncrow from current source. --no-deps keeps the compiled native
# deps; --reinstall-package forces it despite an unchanged version string.
echo "==> reinstalling lemoncrow package from current source (uv pip install)"
VIRTUAL_ENV=/opt/lemoncrow-venv uv pip install \
  --python /opt/lemoncrow-venv/bin/python \
  --no-deps --reinstall-package lemoncrow \
  /tmp/src
VIRTUAL_ENV=/opt/lemoncrow-venv uv pip install \
  --python /opt/lemoncrow-venv/bin/python \
  --no-deps --reinstall-package lemoncrow-client \
  /tmp/src/client
rm -rf /tmp/src

# Verify the refreshed surface is actually present before we trust the bundle.
echo "==> verifying refreshed bundle surface"
/opt/lemoncrow-venv/bin/python - <<'PY'
import inspect
import lemoncrow
import lemoncrow_client
from lemoncrow.pro.capabilities.code_context import engine
assert hasattr(engine, "IndexLockTimeout"), "IndexLockTimeout missing"
assert hasattr(engine, "_index_lock_timeout_s"), "_index_lock_timeout_s missing"
src = inspect.getsource(engine.CodeContextEngine.index_repo)
assert "require_lock" in src, "require_lock missing from index_repo"
import pygit2  # native dep must still load
print(
    "VERIFY_OK lemoncrow=%s client=%s pygit2=%s"
    % (getattr(lemoncrow, "__version__", "?"), lemoncrow_client.__file__, pygit2.__version__)
)
PY
/opt/headroom-venv/bin/python -c 'import headroom; print("HEADROOM_VERIFY_OK", headroom.__file__)'
/opt/headroom-venv/bin/headroom --version

# Re-pack both isolated venvs with the shared portable Python.
echo "==> repacking bundle"
tar -C /opt -czf /out/lemoncrow-bundle-new.tar.gz lemoncrow-venv headroom-venv uvpy
echo "REBUILD_OK bytes=$(stat -c%s /out/lemoncrow-bundle-new.tar.gz)"
