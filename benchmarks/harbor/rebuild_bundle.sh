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

echo "==> apt-get update/install (curl, ca-certificates, git)"
apt-get update -qq
apt-get install -y -qq curl ca-certificates git

export HOME=/root
echo "==> installing uv"
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="/root/.local/bin:$PATH"
uv --version

# Restore the existing bundle into /opt (lemoncrow-venv + uvpy), as the runtime does.
echo "==> restoring existing bundle into /opt"
tar -C /opt -xzf /out/lemoncrow-bundle.tar.gz
test -x /opt/lemoncrow-venv/bin/python
export UV_PYTHON_INSTALL_DIR=/opt/uvpy

# The original bundle is pure-Python lemoncrow (no mypyc .so), so skip the mypyc
# build hook to match it -- also avoids needing a C toolchain here, and avoids
# the hook's mypy run writing .mypy_cache into the (read-only) source.
export LEMONCROW_SKIP_MYPYC=1

# Build from a WRITABLE copy (repo is mounted read-only). Copy only what the
# wheel build needs: packages=src/lemoncrow + force-include integrations + the
# custom hook + project metadata.
echo "==> copying source for wheel build"
mkdir -p /tmp/src
cp -a /lemoncrow/pyproject.toml /lemoncrow/hatch_build.py /lemoncrow/README.md /lemoncrow/LICENSE /tmp/src/
cp -a /lemoncrow/src /lemoncrow/integrations /tmp/src/

# Reinstall ONLY lemoncrow from current source. --no-deps keeps the compiled native
# deps; --reinstall-package forces it despite an unchanged version string.
echo "==> reinstalling lemoncrow package from current source (uv pip install)"
VIRTUAL_ENV=/opt/lemoncrow-venv uv pip install \
  --python /opt/lemoncrow-venv/bin/python \
  --no-deps --reinstall-package lemoncrow \
  /tmp/src

# Verify the refreshed surface is actually present before we trust the bundle.
echo "==> verifying refreshed bundle surface"
/opt/lemoncrow-venv/bin/python - <<'PY'
import inspect
import lemoncrow
from lemoncrow.pro.capabilities.code_context import engine
assert hasattr(engine, "IndexLockTimeout"), "IndexLockTimeout missing"
assert hasattr(engine, "_index_lock_timeout_s"), "_index_lock_timeout_s missing"
src = inspect.getsource(engine.CodeContextEngine.index_repo)
assert "require_lock" in src, "require_lock missing from index_repo"
import pygit2  # native dep must still load
print("VERIFY_OK lemoncrow=%s pygit2=%s" % (getattr(lemoncrow, "__version__", "?"), pygit2.__version__))
PY

# Re-pack with the same tar layout (lemoncrow-venv/ + uvpy/ at root).
echo "==> repacking bundle"
tar -C /opt -czf /out/lemoncrow-bundle-new.tar.gz lemoncrow-venv uvpy
echo "REBUILD_OK bytes=$(stat -c%s /out/lemoncrow-bundle-new.tar.gz)"
