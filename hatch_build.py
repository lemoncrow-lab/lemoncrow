"""Hatch build hook: compile lemoncrow with mypyc before wheel assembly.

What it does
------------
1. Finds all .py files under src/lemoncrow/ that are safe for mypyc:
   - No pydantic BaseModel/RootModel subclasses (C-level incompatibility)
   - No bare __import__() calls
   - Not __main__, _vendor, or bench
2. Runs mypyc (cwd=src/) so compiled .so files land in-place next to .py files.
3. The mypyc support module (hash-named .so at src/) is added to force_include
   so it lands at site-packages root (importable on sys.path).
4. Deletes the .py source for every successfully-compiled module so only
   the .so is packaged (no readable source shipped).
5. In finalize(), restores all deleted .py files and cleans up .so artifacts.

Pure-Python is the DEFAULT, officially-supported distribution. The mypyc
compile is EXPERIMENTAL and opt-in: set LEMONCROW_ENABLE_MYPYC=1 to enable it
(and only publish a compiled build once CI verifies it on every supported
platform). scripts/build.sh forces it on for releases; there is no separate
skip flag -- unset (or LEMONCROW_ENABLE_MYPYC=0) is the pure-Python build.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from setuptools.command.build_ext import build_ext


class _ParallelSourceBuildExt(build_ext):
    """Compile a multi-file mypyc extension across all configured CPUs."""

    def build_extensions(self) -> None:
        compiler = self.compiler
        if compiler is None:
            super().build_extensions()
            return
        jobs = max(1, int(os.environ.get("LEMONCROW_BUILD_JOBS", "1")))
        original_compile = compiler.compile

        def parallel_compile(sources: list[str], *args: Any, **kwargs: Any) -> list[str]:
            if jobs == 1 or len(sources) < 2:
                return original_compile(sources, *args, **kwargs)
            output_dir = kwargs.get("output_dir")
            for obj in compiler.object_filenames(sources, output_dir=output_dir):
                pathlib.Path(obj).parent.mkdir(parents=True, exist_ok=True)
            with ThreadPoolExecutor(max_workers=min(jobs, len(sources))) as executor:
                futures = [executor.submit(original_compile, [source], *args, **kwargs) for source in sources]
                return [obj for future in futures for obj in future.result()]

        compiler.compile = parallel_compile  # type: ignore[method-assign]
        try:
            # Keep extension linking serial; the expensive shared extension's
            # source objects already consume the full worker budget above.
            self.parallel = None
            super().build_extensions()
        finally:
            compiler.compile = original_compile  # type: ignore[method-assign]


def _mypyc_importable() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("mypyc") is not None
    except Exception:
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
    "lemoncrow/core/capabilities/savings_summary.py",
    "lemoncrow/core/capabilities/web_fetch.py",
    "lemoncrow/core/capabilities/workspace_host_overrides.py",
    "lemoncrow/core/domains/loader.py",
    "lemoncrow/core/domains/manager.py",
    "lemoncrow/core/foundation/store.py",
    "lemoncrow/core/foundation/watchdogs.py",
    "lemoncrow/core/service/telemetry/exporters/otel.py",
    "lemoncrow/gateway/cli/commands/project.py",
    "lemoncrow/gateway/openai_gateway/adapter.py",
    "lemoncrow/gateway/openai_gateway/app.py",
    "lemoncrow/gateway/cli/runtime.py",
    "lemoncrow/infra/code_intel/zoekt/server.py",  # clang 21 ICE on mypyc-generated C
    # Must stay interpreted: mypyc-native classes have no __weakref__ slot, and
    # this holder exists precisely to be a weakref target for the native engine.
    "lemoncrow/core/foundation/weakref_token.py",
    # mypyc strips function annotations, but the @mcp_tool framework introspects
    # them (inspect.signature / get_type_hints) to build pydantic ArgsModels and
    # coerce stringified client args. Compiling this module erases those types, so
    # every tool rejects stringified scalar (int/bool) args at the call boundary.
    "lemoncrow/gateway/adapters/mcp_server.py",
    # mypyc does not support async generators (async def with yield).
    "lemoncrow/gateway/adapters/mcp_http.py",
    # FastAPI DI defaults (Header()/Depends()/Request) are sentinel objects that
    # violate the compiled parameter's type annotation, so mypyc raises
    # "str object expected; got fastapi.params.Header" the instant run_daemon
    # defines its route handlers. Ship interpreted like the FastAPI modules above.
    "lemoncrow/gateway/adapters/mcp_daemon.py",
}


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        # Pure-Python by default; the mypyc compile is experimental and opt-in.
        if os.environ.get("LEMONCROW_ENABLE_MYPYC") != "1":
            print(
                "[hatch-mypyc] pure-Python build (default, supported). "
                "Set LEMONCROW_ENABLE_MYPYC=1 for the experimental compiled build.",
                flush=True,
            )
            return
        # Editable installs (uv run / pip install -e) must never compile.
        # Shipping .so files breaks live source editing and forces a full
        # ~296-module mypyc recompile on every `uv run` sync. Only real wheel
        # builds (uv build --wheel, version="standard") compile.
        if version == "editable":
            return

        # mypyc ships with mypy; if it is missing we cannot produce a compiled
        # wheel. Fall through to pure-Python here -- the source-leak guard in
        # finalize() then FAILS the build when compilation was required
        # (LEMONCROW_ENABLE_MYPYC=1), so a source-shipping wheel never escapes.
        if not _mypyc_importable():
            print(
                "[hatch-mypyc] mypyc not importable — cannot compile. Install the dev"
                " deps (mypy) for a compiled wheel, or unset LEMONCROW_ENABLE_MYPYC"
                " for a pure-Python build.",
                flush=True,
            )
            return

        repo = pathlib.Path(self.root)
        src_dir = repo / "src"
        lemoncrow_src = src_dir / "lemoncrow"

        # 1. Clean stale artifacts from previous failed runs to prevent conflicts.
        build_dir = src_dir / "build"
        if build_dir.exists():
            shutil.rmtree(build_dir)
        for so in list(src_dir.rglob("*.so")):
            so.unlink(missing_ok=True)
        mypy_cache = repo / ".mypy_cache"
        if mypy_cache.exists():
            shutil.rmtree(mypy_cache)

        compilable = _find_compilable(lemoncrow_src, src_dir)
        if not compilable:
            return

        build_data["infer_tag"] = True
        print(f"[hatch-mypyc] compiling {len(compilable)} modules …", flush=True)
        _run_mypyc(compilable, src_dir)

        # 2. Collect generated .so files
        all_sos = list(src_dir.rglob("*.so"))
        support_sos = [s for s in all_sos if s.parent == src_dir]
        module_sos = [s for s in all_sos if s.parent != src_dir and "lemoncrow" in str(s) and "build" not in s.parts]

        # Put mypyc support module at wheel root → installs to site-packages/
        for so in support_sos:
            build_data.setdefault("force_include", {})[str(so)] = so.name

        # Explicitly include per-module .so files (hatch only auto-includes .py)
        for so in module_sos:
            build_data.setdefault("force_include", {})[str(so)] = str(so.relative_to(src_dir))

        # Compiled build: ship ONLY the .so for every module that produced one, so
        # source never lands in the wheel (IP protection) and the wheel stays lean --
        # the readable source lives in Git, not the package. Strip each compiled
        # module's .py from the build tree now; finalize() restores the working-tree
        # sources after the wheel is assembled. Modules with no .so (skip-listed
        # FastAPI/click/pydantic modules, __main__ shims, any uncompilable pro
        # module) necessarily still ship as .py.
        self._deleted_py: dict[pathlib.Path, str] = {}
        for rel in compilable:
            py_path = src_dir / rel
            try:
                self._deleted_py[py_path] = py_path.read_text(encoding="utf-8")
                py_path.unlink()
            except OSError:
                pass
        print(
            f"[hatch-mypyc] compiled {len(module_sos)} modules; "
            f"stripped {len(self._deleted_py)} .py from wheel (.so only)",
            flush=True,
        )

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

        # Source-leak guard: a compiled wheel must never ship the source it just
        # replaced, AND a build that ASKED to compile (LEMONCROW_ENABLE_MYPYC=1)
        # must not silently fall back to a pure-Python, source-shipping wheel.
        # Self-gating on sdists and on intentional pure-Python builds.
        mypyc_requested = os.environ.get("LEMONCROW_ENABLE_MYPYC") == "1" and version != "editable"
        _assert_no_source_leak(artifact_path, require_compiled=mypyc_requested)


def _assert_no_source_leak(artifact_path: str, require_compiled: bool = False) -> None:
    """Fail the build if the compiled wheel ships source it must not.

    Two invariants for a mypyc-compiled wheel:
      1. No module that compiled to a ``.so`` may ALSO ship its ``.py`` -- a
         stale or failed strip would leak the very source the ``.so`` replaces.
      2. No ``lemoncrow/pro/`` module may ship as ``.py`` at all: the pro tree is
         the closed engine and must be 100% compiled. An uncompilable pro module
         is a release blocker, never a silent source leak.

    Uncompilable OPEN modules (pydantic/click/FastAPI/hook scripts) have no
    ``.so`` and legitimately ship as ``.py`` -- those are allowed.

    When *require_compiled* is set (the caller expected a mypyc build, i.e.
    ``LEMONCROW_ENABLE_MYPYC=1``), a wheel with NO ``.so`` is itself a failure:
    compilation silently fell back to a pure-Python wheel that ships all source.
    This is the guard against an accidental uncompiled release.
    """
    import re
    import zipfile

    if not artifact_path.endswith(".whl"):
        return  # sdists ship pure source by design; the guard only covers wheels.

    with zipfile.ZipFile(artifact_path) as zf:
        names = set(zf.namelist())

    if not any(n.endswith(".so") for n in names):
        if require_compiled:
            raise RuntimeError(
                f"[hatch-mypyc] REFUSING to ship {os.path.basename(artifact_path)}: "
                "LEMONCROW_ENABLE_MYPYC=1 requested a compiled wheel but it contains no "
                ".so -- mypyc did not run (not importable?), so this wheel would ship ALL "
                "source, including the closed lemoncrow/pro engine."
            )
        return  # intentional pure-Python build: every .py legitimately ships.

    so_stems = {re.sub(r"\.cpython-.*\.so$", "", n) for n in names if n.endswith(".so")}
    twin_leaks = sorted(f"{stem}.py" for stem in so_stems if f"{stem}.py" in names)
    pro_leaks = sorted(n for n in names if n.startswith("lemoncrow/pro/") and n.endswith(".py"))

    problems = []
    if twin_leaks:
        problems.append(
            f"{len(twin_leaks)} compiled module(s) shipped BOTH .so and .py:\n    " + "\n    ".join(twin_leaks)
        )
    if pro_leaks:
        problems.append(
            f"{len(pro_leaks)} closed-engine lemoncrow/pro source file(s) shipped as .py:\n    "
            + "\n    ".join(pro_leaks)
        )
    if problems:
        raise RuntimeError(
            f"[hatch-mypyc] SOURCE LEAK in {os.path.basename(artifact_path)}:\n" + "\n".join(problems)
        )
    print(
        "[hatch-mypyc] source-leak check PASSED: no compiled .py twins, no lemoncrow/pro/*.py",
        flush=True,
    )


def _find_compilable(lemoncrow_src: pathlib.Path, src_dir: pathlib.Path) -> list[str]:
    result = []
    # lemoncrow/pro is the closed IP engine: EVERY module must compile to .so so
    # no readable source ever ships. The thin-orchestration / mypyc-quirk skip
    # lists below are allowances for the open tree only -- they do NOT apply to
    # pro/, and any pro module that matches a mypyc-incompatible pattern is a
    # release blocker (raised below) rather than a silent source leak.
    pro_uncompilable: list[tuple[str, str]] = []
    for py in sorted(lemoncrow_src.rglob("*.py")):
        if any(p in py.parts for p in _SKIP_DIRS):
            continue
        if py.name == "__main__.py":
            continue
        rel = str(py.relative_to(src_dir))
        is_pro = rel.startswith("lemoncrow/pro/")
        reason = ""
        if not is_pro and py.name in _SKIP_FILES:
            reason = f"SKIP_FILES({py.name})"
        elif not is_pro and rel in _SKIP_PATHS:
            reason = "SKIP_PATHS"
        else:
            text = py.read_text(errors="replace")
            if _PYDANTIC_RE.search(text):
                reason = "pydantic BaseModel/RootModel"
            elif _DYNAMIC_RE.search(text):
                reason = "__import__()"
            elif _BUILTIN_INHERIT_RE.search(text):
                reason = "builtin-type inheritance"
            elif _NO_REDEF_RE.search(text):
                reason = "type: ignore[no-redef]"
            elif _RUNTIME_CHECKABLE_RE.search(text):
                reason = "@runtime_checkable"
            elif _CLICK_RE.search(text):
                reason = "click decorator"
        if reason:
            if is_pro:
                pro_uncompilable.append((rel, reason))
            continue
        result.append(rel)
    if pro_uncompilable:
        details = "\n".join(f"  - {rel}  [{why}]" for rel, why in pro_uncompilable)
        # Open-source engine: an uncompilable pro module simply ships as (open) .py.
        # No IP-leak guard — there is no proprietary source to protect.
        print(
            "[hatch-mypyc] these lemoncrow/pro modules are not mypyc-compilable and "
            f"will ship as .py (open source):\n{details}",
            flush=True,
        )
    return result


def _run_mypyc(files: list[str], cwd: pathlib.Path) -> None:
    # Use all available cores for mypyc compilation.
    env = os.environ.copy()
    configured_jobs = env.get("LEMONCROW_BUILD_JOBS", "").strip()
    if configured_jobs:
        try:
            jobs = int(configured_jobs)
        except ValueError as exc:
            raise RuntimeError("LEMONCROW_BUILD_JOBS must be a positive integer") from exc
        if jobs < 1:
            raise RuntimeError("LEMONCROW_BUILD_JOBS must be a positive integer")
    else:
        jobs = os.process_cpu_count() or 1
    env["LEMONCROW_BUILD_JOBS"] = str(jobs)
    env["NPROC"] = str(jobs)
    env["MAX_JOBS"] = str(jobs)
    env["PYTHONPATH"] = str(cwd.parent) + os.pathsep + env.get("PYTHONPATH", "")

    # Pre-create directories so parallel build_ext workers never race while
    # creating mypyc's shared intermediate directory (including on macOS).
    import sysconfig

    _plat = sysconfig.get_platform()
    _ver = f"{sys.version_info.major}{sys.version_info.minor}"
    _build_root = cwd / "build"
    _temp_build = _build_root / f"temp.{_plat}-cpython-{_ver}" / "build"
    _build_root.mkdir(parents=True, exist_ok=True)
    _temp_build.mkdir(parents=True, exist_ok=True)

    # mypyc's CLI always invokes `build_ext` serially. Generate the equivalent
    # documented mypycify/setuptools setup and pass build_ext's real parallel
    # option so native extensions compile concurrently on every platform.
    mypyc_args = ["--ignore-missing-imports", "--allow-untyped-decorators", *files]
    opt_level = env.get("MYPYC_OPT_LEVEL", "3")
    debug_level = env.get("MYPYC_DEBUG_LEVEL", "1")
    strict_dunder_typing = bool(int(env.get("MYPYC_STRICT_DUNDER_TYPING", "0")))
    log_trace = bool(int(env.get("MYPYC_LOG_TRACE", "0")))
    setup_file = _build_root / "setup.py"
    setup_file.write_text(
        "from setuptools import setup\n"
        "from mypyc.build import mypycify\n"
        "from hatch_build import _ParallelSourceBuildExt\n"
        "setup(name='mypyc_output', ext_modules=mypycify("
        f"{mypyc_args!r}, opt_level={opt_level!r}, debug_level={debug_level!r}, "
        f"strict_dunder_typing={strict_dunder_typing!r}, log_trace={log_trace!r}, "
        f"multi_file={jobs > 1!r}), cmdclass={{'build_ext': _ParallelSourceBuildExt}})\n",
        encoding="utf-8",
    )

    print(f"[hatch-mypyc] cwd={cwd}", flush=True)
    print(f"[hatch-mypyc] parallel_jobs={jobs}", flush=True)
    print(f"[hatch-mypyc] build_root={_build_root} exists={_build_root.exists()}", flush=True)
    print(f"[hatch-mypyc] temp_build={_temp_build} exists={_temp_build.exists()}", flush=True)

    result = subprocess.run(
        [sys.executable, str(setup_file), "build_ext", "--inplace"],
        cwd=str(cwd),
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"mypyc compilation failed (exit {result.returncode})")
