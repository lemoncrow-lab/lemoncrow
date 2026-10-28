"""Hatch build hook: compile lemoncrow with mypyc before wheel assembly.

What it does
------------
1. Finds all .py files under src/lemoncrow/ that are safe for mypyc.
2. Runs mypyc (cwd=src/) so compiled .so files land next to the staged .py files.
3. Adds generated .so files to the wheel and temporarily removes their staged
   .py twins so readable source is not packaged.
4. In finalize(), restores the staged .py files and cleans generated artifacts.

IMPORTANT: a compiled build mutates its build root while the wheel is assembled.
It must therefore run only in a disposable, non-Git staging tree. The hook
refuses LEMONCROW_ENABLE_MYPYC=1 when ``self.root`` is a Git checkout, and
``scripts/build.sh`` creates the isolated copy used by local and CI releases.
Interrupting or killing a build can consequently damage only that disposable
copy, never tracked source in the developer checkout.

Pure-Python is the default supported distribution. Set LEMONCROW_ENABLE_MYPYC=1
only through the isolated release build path; unset/0 produces a pure-Python
wheel.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
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


def _acquire_build_lock(repo: pathlib.Path) -> Any:
    """Block until this checkout's compiled build is exclusively ours.

    Returns the held file handle, or None where locking is unavailable
    (non-POSIX): the build then proceeds unserialized, as it always did.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        return None
    digest = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]
    lock_path = pathlib.Path(tempfile.gettempdir()) / f"lemoncrow-mypyc-{digest}.lock"
    handle = lock_path.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"[hatch-mypyc] waiting for the compiled build holding {lock_path} …", flush=True)
        fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def _release_build_lock(handle: Any) -> None:
    if handle is None:
        return
    try:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_UN)
    except (ImportError, OSError):  # pragma: no cover
        pass
    handle.close()


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
# mypyc checks the whole MRO, so a class deriving from such a builtin through an
# in-repo class (``class Refused(InternalLLMError)``, ``InternalLLMError(RuntimeError)``)
# hits the same error. The builtins mypyc can subclass natively, per
# ``mypyc.irbuild.prepare`` (BaseException and dict special-cased, then
# ``can_subclass_builtin``):
_MYPYC_SUBCLASSABLE = frozenset(
    {
        "object",
        "BaseException",
        "dict",
        "Exception",
        "LookupError",
        "IndexError",
        "Warning",
        "UserWarning",
        "ValueError",
    }
)
_UNSUBCLASSABLE_BUILTINS = frozenset(
    name for name, value in vars(builtins).items() if isinstance(value, type) and name not in _MYPYC_SUBCLASSABLE
)
# mypyc: AssertionError on try/except redef pattern (optional-dep fallbacks)
_NO_REDEF_RE = re.compile(r"# type: ignore\[no-redef\]")
# mypyc: Protocol subclasses lose Protocol metaclass, breaking @runtime_checkable
_RUNTIME_CHECKABLE_RE = re.compile(r"@runtime_checkable")
# mypyc: Click decorators add __dict__ attrs to functions; C extension functions have no __dict__
_CLICK_RE = re.compile(r"@(?:click|_click)\.")
# mypyc: FastAPI reads every endpoint's signature, and a mypyc 2.2 function has none
# ("no signature found for builtin", or an empty one that drops every parameter); its DI
# defaults (Header()/Depends()/...) also violate the compiled parameter's annotation on
# import. Any module importing fastapi stays interpreted.
_FASTAPI_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+fastapi\b", re.M)


def _contains_async_generator(text: str) -> bool:
    """Return whether a module contains an async generator unsupported by mypyc."""

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if any(isinstance(child, (ast.Yield, ast.YieldFrom)) for child in ast.walk(node)):
            return True
    return False


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
    "lemoncrow/gateway/openai_gateway/responses.py",
    "lemoncrow/pro/capabilities/review/state_events.py",
    # FastAPI DI defaults (Header()/Depends()/Request) are sentinel objects that
    # violate the compiled parameter's type annotation, so mypyc raises
    # "str object expected; got fastapi.params.Header" the instant run_daemon
    # defines its route handlers. Ship interpreted like the FastAPI modules above.
    "lemoncrow/gateway/adapters/mcp_daemon.py",
    # mypyc 2.2: "continue inside try/finally block is unimplemented" (the retry loop).
    "lemoncrow/infra/runtime/lite_semantic_helper.py",
    # mypyc 2.2: "Nested class definitions not supported" (a class inside a function).
    "lemoncrow/pro/capabilities/review/analysis_common.py",
    "lemoncrow/pro/capabilities/review/evaluation/pipeline.py",
    # mypyc 2.2: 'Cannot access instance attribute "__slots__" through class object'.
    "lemoncrow/pro/capabilities/review/outcomes/incidents.py",
}


def _assert_isolated_mypyc_build_root(repo: pathlib.Path) -> None:
    """Refuse a compiled build that could mutate a real Git working tree."""
    if (repo / ".git").exists():
        raise RuntimeError(
            "[hatch-mypyc] REFUSING in-place compiled build in a Git checkout. "
            "mypyc packaging temporarily strips compiled .py files from its build root; "
            "run `bash scripts/build.sh`, which builds from a disposable staging copy."
        )


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
        # wheel. Fall through to pure-Python here -- the wheel guard in finalize()
        # then FAILS the build when compilation was required
        # (LEMONCROW_ENABLE_MYPYC=1), so a silently-uncompiled wheel never escapes.
        if not _mypyc_importable():
            print(
                "[hatch-mypyc] mypyc not importable — cannot compile. Install the dev"
                " deps (mypy) for a compiled wheel, or unset LEMONCROW_ENABLE_MYPYC"
                " for a pure-Python build.",
                flush=True,
            )
            return

        repo = pathlib.Path(self.root)
        _assert_isolated_mypyc_build_root(repo)
        src_dir = repo / "src"
        lemoncrow_src = src_dir / "lemoncrow"

        # 0. Serialize compiled builds of this checkout. src/build and the
        # in-place .so tree are shared mutable state: a second compiled build
        # rmtree'ing them mid-compile makes the first one fail with
        # "can't create build/temp.../__native_*.o: No such file or directory".
        # Held until finalize().
        self._build_lock = _acquire_build_lock(repo)

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
        # the wheel stays lean and carries no ambiguous .py/.so twin -- the readable
        # source lives in Git and the sdist, not the wheel. Strip each compiled
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

        # Only the compiled build owns src/build and the src/**/*.so tree, so only
        # it may clean them. A pure-Python or editable build (any concurrent
        # `uv run` / `uv sync`, which rebuilds the editable install) used to wipe
        # both here -- deleting a parallel `uv build --wheel`'s object directory
        # mid-compile: "Fatal error: can't create build/temp.../__native_*.o: No
        # such file or directory". Such a build compiled nothing, so it has
        # nothing to restore or clean.
        if os.environ.get("LEMONCROW_ENABLE_MYPYC") == "1" and version != "editable":
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
            _release_build_lock(getattr(self, "_build_lock", None))
            self._build_lock = None

        # Source-leak guard: a compiled wheel must never ship the source it just
        # replaced, AND a build that ASKED to compile (LEMONCROW_ENABLE_MYPYC=1)
        # must not silently fall back to a pure-Python, source-shipping wheel.
        # Self-gating on sdists and on intentional pure-Python builds.
        mypyc_requested = os.environ.get("LEMONCROW_ENABLE_MYPYC") == "1" and version != "editable"
        _assert_no_duplicate_source(artifact_path, require_compiled=mypyc_requested)


def _assert_no_duplicate_source(artifact_path: str, require_compiled: bool = False) -> None:
    """Fail the build if the compiled wheel is internally inconsistent.

    One invariant for a mypyc-compiled wheel: no module that compiled to a
    ``.so`` may ALSO ship its ``.py`` -- a stale or failed strip leaves two
    import targets for the same module.

    Uncompilable modules (pydantic/click/FastAPI/hook scripts, and any
    ``lemoncrow/pro`` module mypyc cannot handle) have no ``.so`` and
    legitimately ship as ``.py``. The whole tree is Apache-2.0 published source,
    so a ``.py`` in the wheel is never a leak.

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
                ".so -- mypyc did not run (not importable?), so this wheel silently fell "
                "back to pure Python and compiled nothing."
            )
        return  # intentional pure-Python build: every .py legitimately ships.

    so_stems = {re.sub(r"\.cpython-.*\.so$", "", n) for n in names if n.endswith(".so")}
    twins = sorted(f"{stem}.py" for stem in so_stems if f"{stem}.py" in names)

    if twins:
        raise RuntimeError(
            f"[hatch-mypyc] DUPLICATE SOURCE in {os.path.basename(artifact_path)}: "
            f"{len(twins)} compiled module(s) shipped BOTH .so and .py:\n    " + "\n    ".join(twins)
        )
    print("[hatch-mypyc] wheel check PASSED: no compiled .py twins", flush=True)


def _subtree_has_yield(node: ast.AST) -> bool:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    if isinstance(node, (ast.Yield, ast.YieldFrom)):
        return True
    for child in ast.iter_child_nodes(node):
        if _subtree_has_yield(child):
            return True
    return False


def _has_starred_membership_literal(text: str) -> bool:
    """Whether a module tests ``x in (*xs, y)`` or ``x not in [*xs, y]``.

    mypyc's in-literal fast path compiles every item of the tuple or list,
    and a starred one is an internal error ("should have been handled in
    Tuple/List/Set/DictExpr or CallExpr"). A set literal takes another path.
    """

    if "*" not in text:
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    return any(
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], (ast.In, ast.NotIn))
        and isinstance(node.comparators[0], (ast.Tuple, ast.List))
        and any(isinstance(item, ast.Starred) for item in node.comparators[0].elts)
        for node in ast.walk(tree)
    )


def _has_module_getattr(text: str) -> bool:
    """Whether a module defines PEP 562 ``__getattr__`` at top level.

    A mypyc 2.2 module with one segfaults on import. ``_SKIP_FILES`` covers
    ``__init__.py`` by name, but not in pro/ and not any other module.
    """

    if "__getattr__" not in text:
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__getattr__" for node in tree.body
    )


def _has_dataclass_classvar(text: str) -> bool:
    """Whether a ``@dataclass`` declares a ``ClassVar``.

    mypyc 2.2 makes it an init field: ahead of a required field the class
    fails on import, after one ``fields()`` and ``__init__`` silently grow it.
    """

    if "ClassVar" not in text or "dataclass" not in text:
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    return any(
        isinstance(node, ast.ClassDef)
        and any(_base_name(decorator) == "dataclass" for decorator in node.decorator_list)
        and any(isinstance(stmt, ast.AnnAssign) and _base_name(stmt.annotation) == "ClassVar" for stmt in node.body)
        for node in ast.walk(tree)
    )


def _has_ellipsis_default(text: str) -> bool:
    """Whether a function defaults a parameter to ``...``.

    mypy accepts ``n: int = ...`` in a Protocol stub; mypyc checks the default
    against the annotation when the class is built, so the module fails to import.
    """

    if "..." not in text:
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(default, ast.Constant) and default.value is Ellipsis
            for default in (*node.args.defaults, *node.args.kw_defaults)
        )
        for node in ast.walk(tree)
    )


def _source_read_modules(texts: dict[pathlib.Path, str]) -> frozenset[str]:
    """Module files another module reads with ``inspect.getsource(module)``.

    A compiled module has no source, so the read raises OSError. Only an absolute
    import binds the name; an unresolved one leaves its target compiled.
    """

    read: set[str] = set()
    for text in texts.values():
        if "getsource" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        bound: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                bound.update({alias.asname or alias.name: f"{node.module}.{alias.name}" for alias in node.names})
            elif isinstance(node, ast.Import):
                bound.update({alias.asname: alias.name for alias in node.names if alias.asname})
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and _base_name(node.func) == "getsource"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in bound
            ):
                path = bound[node.args[0].id].replace(".", "/")
                read.update((f"{path}.py", f"{path}/__init__.py"))
    return frozenset(read)


#: Calls that read a function's parameters or annotations; a mypyc 2.2 function keeps only the
#: parameter names and defaults (``__text_signature__``), never the annotations.
_SIGNATURE_READS = frozenset({"signature", "get_type_hints"})


def _introspecting_decorators(texts: dict[pathlib.Path, str]) -> frozenset[str]:
    """Names of module-level functions that read another function's signature or type hints.

    ``@mcp_tool`` builds each tool's input schema and its argument coercion from the decorated
    function's annotations: compiled, every parameter is ``Any``, a stringified ``"20"`` is not
    coerced and the strict compiled handler raises ``TypeError``. A module applying one of these
    as a decorator stays interpreted. Matched by name, so a same-named decorator elsewhere also
    keeps its module interpreted (fail-safe).
    """

    names: set[str] = set()
    for text in texts.values():
        if "signature" not in text and "get_type_hints" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                isinstance(call, ast.Call) and _base_name(call.func) in _SIGNATURE_READS for call in ast.walk(node)
            ):
                names.add(node.name)
    return frozenset(names)


def _applies_decorator(text: str, decorators: frozenset[str]) -> bool:
    """Whether a module decorates a function with one of *decorators* (``@d``, ``@m.d``, ``@d(...)``)."""

    if not decorators or not any(name in text for name in decorators):
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    return any(
        _base_name(decorator) in decorators
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
    )


def _has_async_generator(text: str) -> bool:
    if "async def" not in text or "yield" not in text:
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            for child in ast.iter_child_nodes(node):
                if _subtree_has_yield(child):
                    return True
    return False


def _base_name(node: ast.expr) -> str:
    """The bare name a reference resolves to: ``a.b.Err`` -> ``Err``, ``dict[str, int]`` -> ``dict``, ``f(x)`` -> ``f``."""

    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _class_bases(text: str) -> list[tuple[str, frozenset[str]]]:
    """(name, base names) of every class a module defines."""

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    return [
        (node.name, frozenset(_base_name(base) for base in node.bases))
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    ]


def _builtin_derived(classes: list[tuple[str, frozenset[str]]]) -> frozenset[str]:
    """Names of the classes deriving, at any depth, from a builtin mypyc cannot subclass natively.

    Bases are matched by bare name, so a name collision only keeps one more
    module interpreted; it never compiles one mypyc would reject.
    """

    derived = set(_UNSUBCLASSABLE_BUILTINS)
    changed = True
    while changed:
        changed = False
        for name, bases in classes:
            if name not in derived and bases & derived:
                derived.add(name)
                changed = True
    return frozenset(derived)


def _find_compilable(lemoncrow_src: pathlib.Path, src_dir: pathlib.Path) -> list[str]:
    result = []
    # lemoncrow/pro is the performance-critical engine, so it is compiled as
    # aggressively as possible: the thin-orchestration / mypyc-quirk skip lists
    # below are allowances for the rest of the tree only and do NOT apply to
    # pro/. A pro module mypyc cannot handle simply ships as .py (reported
    # below) -- all source is published either way.
    pro_uncompilable: list[tuple[str, str]] = []
    sources = [
        py
        for py in sorted(lemoncrow_src.rglob("*.py"))
        if not any(p in py.parts for p in _SKIP_DIRS) and py.name != "__main__.py"
    ]
    texts = {py: py.read_text(errors="replace") for py in sources}
    classes = {py: _class_bases(text) for py, text in texts.items()}
    derived = _builtin_derived([entry for entries in classes.values() for entry in entries])
    source_read = _source_read_modules(texts)
    introspecting = _introspecting_decorators(texts)
    for py in sources:
        rel = str(py.relative_to(src_dir))
        is_pro = rel.startswith("lemoncrow/pro/")
        reason = ""
        if not is_pro and py.name in _SKIP_FILES:
            reason = f"SKIP_FILES({py.name})"
        elif rel in _SKIP_PATHS:
            reason = "SKIP_PATHS"
        else:
            text = texts[py]
            if _contains_async_generator(text):
                reason = "async generator"
            elif _PYDANTIC_RE.search(text):
                reason = "pydantic BaseModel/RootModel"
            elif _DYNAMIC_RE.search(text):
                reason = "__import__()"
            elif _BUILTIN_INHERIT_RE.search(text):
                reason = "builtin-type inheritance"
            elif any(bases & derived for _, bases in classes[py]):
                reason = "builtin-type inheritance through an in-repo base"
            elif _has_starred_membership_literal(text):
                reason = "starred item in an `in` literal"
            elif _has_module_getattr(text):
                reason = "module-level __getattr__"
            elif _has_dataclass_classvar(text):
                reason = "ClassVar in a dataclass"
            elif _has_ellipsis_default(text):
                reason = "`...` parameter default"
            elif rel in source_read:
                reason = "source read by inspect.getsource"
            elif _NO_REDEF_RE.search(text):
                reason = "type: ignore[no-redef]"
            elif _RUNTIME_CHECKABLE_RE.search(text):
                reason = "@runtime_checkable"
            elif _CLICK_RE.search(text):
                reason = "click decorator"
            elif _FASTAPI_IMPORT_RE.search(text):
                reason = "FastAPI endpoint signatures"
            elif _applies_decorator(text, introspecting):
                reason = "signature read by its decorator (@mcp_tool)"
            elif _has_async_generator(text):
                reason = "async generator"
        if reason:
            if is_pro:
                pro_uncompilable.append((rel, reason))
            continue
        result.append(rel)
    if pro_uncompilable:
        details = "\n".join(f"  - {rel}  [{why}]" for rel, why in pro_uncompilable)
        # Open-source engine: an uncompilable pro module simply ships as (open)
        # .py. Nothing is hidden, so this is a perf note, not a release blocker.
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
