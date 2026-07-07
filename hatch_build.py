"""Hatch build hook: compile atelier with mypyc before wheel assembly.

What it does
------------
1. Finds all .py files under src/atelier/ that are safe for mypyc:
   - No pydantic BaseModel/RootModel subclasses (C-level incompatibility)
   - No bare __import__() calls
   - Not __main__, _vendor, or bench
2. Runs mypyc (cwd=src/) so compiled .so files land in-place next to .py files.
3. The mypyc support module (hash-named .so at src/) is added to force_include
   so it lands at site-packages root (importable on sys.path).
4. Deletes the .py source for every successfully-compiled module so only
   the .so is packaged (no readable source shipped).
5. In finalize(), restores all deleted .py files and cleans up .so artifacts.

Set ATELIER_SKIP_MYPYC=1 to skip compilation (dev builds, CI unit tests).
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


def _mypyc_importable() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("mypyc") is not None
    except Exception:  # noqa: BLE001
        return False


_PYDANTIC_RE = re.compile(r"class\s+\w+\s*\(.*?(?:BaseModel|RootModel)")
_DYNAMIC_RE = re.compile(r"__import__\s*\(")
# mypyc: "Inheriting from most builtin types is unimplemented"
_BUILTIN_INHERIT_RE = re.compile(
    r"class\s+\w+\s*\((?:dict|list|set|tuple|str|bytes|int|float|Exception|BaseException|ValueError|TypeError|RuntimeError|KeyError|OSError)[^)]*\)"
)
# mypyc: AssertionError on try/except redef pattern (optional-dep fallbacks)
_NO_REDEF_RE = re.compile(r"# type: ignore\[no-redef\]")
# mypyc: Protocol subclasses lose Protocol metaclass, breaking @runtime_checkable
_RUNTIME_CHECKABLE_RE = re.compile(r"@runtime_checkable")
# mypyc: Click decorators add __dict__ attrs to functions; C extension functions have no __dict__
_CLICK_RE = re.compile(r"@(?:click|_click)\.")
_SKIP_DIRS = {"__pycache__", "_vendor", "bench"}
# Files that cause mypyc cross-module Any errors when pydantic files are excluded
_SKIP_FILES = {
    "engine.py",
    "__init__.py",
}  # thin orchestration/facade, not core IP; __init__.py: mypyc module-level __getattr__ segfaults

# Files with mypyc-incompatible patterns found via batch testing:
#   AssertionError (defaultdict in dataclass), async generators, continue-in-try/finally,
#   Unsupported default attribute value, generator-as-list, cross-module issues
_SKIP_PATHS = {
    "atelier/core/capabilities/savings_summary.py",
    "atelier/core/capabilities/web_fetch.py",
    "atelier/core/capabilities/workspace_host_overrides.py",
    "atelier/core/domains/loader.py",
    "atelier/core/domains/manager.py",
    "atelier/core/foundation/store.py",
    "atelier/core/foundation/watchdogs.py",
    "atelier/core/service/telemetry/exporters/otel.py",
    "atelier/gateway/cli/commands/project.py",
    "atelier/gateway/openai_gateway/adapter.py",
    "atelier/gateway/openai_gateway/app.py",
    "atelier/gateway/cli/runtime.py",
    "atelier/infra/code_intel/zoekt/server.py",  # clang 21 ICE on mypyc-generated C
    # mypyc strips function annotations, but the @mcp_tool framework introspects
    # them (inspect.signature / get_type_hints) to build pydantic ArgsModels and
    # coerce stringified client args. Compiling this module erases those types, so
    # every tool rejects stringified scalar (int/bool) args at the call boundary.
    "atelier/gateway/adapters/mcp_server.py",
    # mypyc does not support async generators (async def with yield).
    "atelier/gateway/adapters/mcp_http.py",
}


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if os.environ.get("ATELIER_SKIP_MYPYC") == "1":
            return

        # Editable installs (uv run / pip install -e) must never compile.
        # Shipping .so files breaks live source editing and forces a full
        # ~296-module mypyc recompile on every `uv run` sync. Only real wheel
        # builds (uv build --wheel, version="standard") compile.
        if version == "editable":
            return

        # mypyc is shipped with mypy; skip gracefully if not installed
        # (e.g. bare `uv build --wheel` without dev deps — use ATELIER_SKIP_MYPYC=1
        # to suppress this warning).
        if not _mypyc_importable():
            print(
                "[hatch-mypyc] mypyc not importable — skipping mypyc compilation."
                " Set ATELIER_SKIP_MYPYC=1 if intentional.",
                flush=True,
            )
            return

        repo = pathlib.Path(self.root)
        src_dir = repo / "src"
        atelier_src = src_dir / "atelier"

        # 1. Clean stale artifacts from previous failed runs to prevent conflicts.
        build_dir = src_dir / "build"
        if build_dir.exists():
            shutil.rmtree(build_dir)
        for so in list(src_dir.rglob("*.so")):
            so.unlink(missing_ok=True)
        mypy_cache = repo / ".mypy_cache"
        if mypy_cache.exists():
            shutil.rmtree(mypy_cache)

        compilable = _find_compilable(atelier_src, src_dir)
        if not compilable:
            return

        build_data["infer_tag"] = True
        print(f"[hatch-mypyc] compiling {len(compilable)} modules …", flush=True)
        _run_mypyc(compilable, src_dir)

        # 2. Collect generated .so files
        all_sos = list(src_dir.rglob("*.so"))
        support_sos = [s for s in all_sos if s.parent == src_dir]
        module_sos = [s for s in all_sos if s.parent != src_dir and "atelier" in str(s) and "build" not in s.parts]

        # Put mypyc support module at wheel root → installs to site-packages/
        for so in support_sos:
            build_data.setdefault("force_include", {})[str(so)] = so.name

        # Explicitly include per-module .so files (hatch only auto-includes .py)
        for so in module_sos:
            build_data.setdefault("force_include", {})[str(so)] = str(so.relative_to(src_dir))

        # Delete .py for compiled modules; restore in finalize()
        self._deleted_py: dict[pathlib.Path, str] = {}
        for so in module_sos:
            stem = so.name.split(".")[0]
            py = so.parent / f"{stem}.py"
            if py.exists():
                self._deleted_py[py] = py.read_text(encoding="utf-8")
                py.unlink()

        print(f"[hatch-mypyc] {len(self._deleted_py)} .py sources replaced by .so in wheel", flush=True)

    def finalize(self, version: str, build_data: dict[str, Any], artifact_path: str) -> None:
        repo = pathlib.Path(self.root)
        src_dir = repo / "src"

        # Restore .py source files
        for py, content in getattr(self, "_deleted_py", {}).items():
            py.write_text(content, encoding="utf-8")

        # Clean up build artifacts
        for so in list(src_dir.rglob("*.so")):
            so.unlink(missing_ok=True)
        build_dir = src_dir / "build"
        if build_dir.exists():
            shutil.rmtree(build_dir, ignore_errors=True)
        mypy_cache = repo / ".mypy_cache"
        if mypy_cache.exists():
            shutil.rmtree(mypy_cache, ignore_errors=True)

        print("[hatch-mypyc] source restored, artifacts cleaned", flush=True)


def _find_compilable(atelier_src: pathlib.Path, src_dir: pathlib.Path) -> list[str]:
    result = []
    for py in sorted(atelier_src.rglob("*.py")):
        if any(p in py.parts for p in _SKIP_DIRS):
            continue
        if py.name == "__main__.py":
            continue
        if py.name in _SKIP_FILES:
            continue
        rel = str(py.relative_to(src_dir))
        if rel in _SKIP_PATHS:
            continue
        text = py.read_text(errors="replace")
        if (
            _PYDANTIC_RE.search(text)
            or _DYNAMIC_RE.search(text)
            or _BUILTIN_INHERIT_RE.search(text)
            or _NO_REDEF_RE.search(text)
            or _RUNTIME_CHECKABLE_RE.search(text)
            or _CLICK_RE.search(text)
        ):
            continue
        result.append(rel)
    return result


def _run_mypyc(files: list[str], cwd: pathlib.Path) -> None:
    # Disable parallel compilation (NPROC/MAX_JOBS) to prevent race conditions on macOS
    # during intermediate directory creation and file renaming.
    env = os.environ.copy()
    env["NPROC"] = "1"
    env["MAX_JOBS"] = "1"
    # Pre-create the temp build directory so gcc can write __native_*.o there.
    # mypyc places build/__native_*.c at the build/ root (not in a subdir), and
    # setuptools may not create build/temp.{plat}-cpython-{ver}/build/ in time.
    import sysconfig

    _plat = sysconfig.get_platform()
    _ver = f"{sys.version_info.major}{sys.version_info.minor}"
    _temp_build = cwd / "build" / f"temp.{_plat}-cpython-{_ver}" / "build"
    _temp_build.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [sys.executable, "-m", "mypyc", "--ignore-missing-imports", "--allow-untyped-decorators", *files],
        cwd=str(cwd),
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"mypyc compilation failed (exit {result.returncode})")
