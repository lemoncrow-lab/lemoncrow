"""Test-impact selection: which tests a Change can affect (Phase 9R R6, item 19).

The static index is an :class:`ImpactMap`: an import graph over the trusted
base at one base commit -- Python through ``ast`` (absolute, relative and
literal ``import_module``/``__import__`` imports, package ``__init__`` and
``conftest.py`` loading) and JavaScript/TypeScript relative specifiers
(``import``/``export ... from``/``require``/``import()``). It is built once per
base commit and kept as a shared base artifact (``base_results``); each
Snapshot re-parses only its changed files on top of it, so the index is
updated incrementally per Snapshot instead of rebuilt.

:func:`select_recipe_tests` maps one Snapshot's changed paths to the test
files of one trusted-base test recipe whose import closure reaches a change.
It never degrades to "run nothing". Everything it cannot decide runs the
recipe in full, with the reason:

* no map, a map for another base commit or builder version (stale), or a map
  past its bounds (truncated);
* a changed dependency manifest, lockfile, test-runner configuration, recipe
  input or recipe definition;
* a changed file outside the graph (data, fixtures, other languages) that a
  test may read at runtime;
* test files in a language the graph does not parse, or none recognized;
* more changed paths than :data:`MAX_CHANGED`.

``none`` (skip the recipe) is a positive finding only: every change is inside
the graph (or documentation no test references) and no test reaches it.
Files whose imports the graph cannot resolve (dynamic imports, aliases,
unparsable or oversized files) count as affected whenever any source changed.

The selection also yields the recipe's input digest (item 22): the changed
files inside the closure of the tests the run executes, with their head
content digests, on top of the base commit. Two Snapshots with the same
digest run the same tests over the same bytes.

Coverage maps are not collected yet: selection uses the import graph only
(lc-debt below). Pure: no store, no I/O beyond the ``read`` callables.
"""

from __future__ import annotations

import ast
import hashlib
import json
import posixpath
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import islice
from pathlib import PurePosixPath
from typing import Any, Final, Literal, cast

from lemoncrow.pro.capabilities.review.recipes import CheckRecipe

#: v2 (RC12-B): a file naming more documentation than the bound records
#: :data:`DOC_REFS_UNKNOWN` instead of a silently cut list.
IMPACT_MAP_VERSION: Final[int] = 2
IMPACT_MAP_KIND: Final[str] = "test_impact_map"
#: RC12-B: building the map is metered compute (``ComputeLedger``) under this
#: entry and stage, on the Snapshot whose plan built it, executed where the
#: Snapshot is held. Loading a stored map costs nothing and records nothing.
IMPACT_MAP_ENTRY: Final[str] = "review.test_impact_map"
IMPACT_MAP_STAGE: Final[str] = "impact_map_build"
IMPACT_MAP_EXECUTOR: Final[str] = "review_host"

# lc-debt: the import graph is the only impact signal (no base coverage map) and it
# is capped at these bounds; past them selection runs the full suite. Upgrade:
# record per-test coverage during base runs and stream the graph from the code index.
MAX_GRAPH_FILES: Final[int] = 20_000
MAX_FILE_BYTES: Final[int] = 512 * 1024
MAX_TOTAL_BYTES: Final[int] = 128 * 1024 * 1024
MAX_LISTED_PATHS: Final[int] = 400_000
MAX_CHANGED: Final[int] = 2_000
MAX_UNRESOLVED: Final[int] = 50_000
MAX_FOREIGN_TESTS: Final[int] = 10_000
_MAX_DOC_REFS: Final[int] = 64
#: Recorded for a file naming more than :data:`_MAX_DOC_REFS` documentation files:
#: which ones it reads is unknown (coverage unknown), so it counts as reading every one.
DOC_REFS_UNKNOWN: Final[str] = "*"

SelectionMode = Literal["impact", "all", "none"]
#: Whether the selection knows which tests a change reaches (``unknown``: it broadened to the
#: full suite because the graph cannot tell, e.g. no or stale map, truncation, a runtime read).
Coverage = Literal["known", "unknown"]

PY_SUFFIXES: Final[frozenset[str]] = frozenset({".py"})
JS_SUFFIXES: Final[tuple[str, ...]] = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
SOURCE_SUFFIXES: Final[frozenset[str]] = PY_SUFFIXES | frozenset(JS_SUFFIXES)
DOC_SUFFIXES: Final[frozenset[str]] = frozenset({".md", ".rst", ".adoc"})
#: Test files in these languages are outside the graph: their recipe runs in full.
UNSUPPORTED_TEST_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".go", ".rs", ".rb", ".java", ".kt", ".cs", ".php", ".swift", ".scala", ".ex", ".exs", ".c", ".cc", ".cpp"}
)
#: Dependency manifests, lockfiles and test-runner configuration: any change runs everything.
_GLOBAL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "tox.ini",
        "pytest.ini",
        "noxfile.py",
        "pipfile",
        "pipfile.lock",
        "poetry.lock",
        "uv.lock",
        "pdm.lock",
        "sitecustomize.py",
        ".coveragerc",
        ".python-version",
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "bun.lockb",
        "jsconfig.json",
        ".babelrc",
        ".swcrc",
        ".nvmrc",
        ".node-version",
    }
)
_GLOBAL_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^requirements[^/]*\.(txt|in)$"),
    re.compile(r"^tsconfig[^/]*\.json$"),
    re.compile(r"^(babel|jest|vitest|vite|karma|mocha|ava)\.config\.[cm]?[jt]s(on)?$"),
    re.compile(r"^\.mocharc\.[a-z]+$"),
)
_DOCTEST_CONFIG: Final[tuple[str, ...]] = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")

_PY_TEST_ENTRY = re.compile(r"(^|/)(test_[^/]*|[^/]*_test)\.py$")
_JS_TEST_ENTRY = re.compile(r"(\.(test|spec)\.[cm]?[jt]sx?$)|(^|/)__tests__/[^/]+\.[cm]?[jt]sx?$")
_TEST_DIR = re.compile(r"(^|/)(tests?|__tests__|spec|specs)/")
_JS_STATIC = re.compile(r"""(?:\bfrom\s*|\bimport\s*|\brequire\s*\(\s*|\bimport\s*\(\s*)(['"])([^'"\n\r]{1,400})\1""")
_JS_DYNAMIC = re.compile(r"""\b(?:require|import)\s*\(\s*(?!['"])[^)\s]""")
_JS_DOC_LITERAL = re.compile(r"""(['"`])([^'"`\n\r]{1,200}\.(?:md|rst|adoc))\1""")
_DYNAMIC_PY_CALLS: Final[frozenset[str]] = frozenset({"import_module", "__import__"})
_LOADER_PY_CALLS: Final[frozenset[str]] = frozenset(
    {"spec_from_file_location", "load_source", "run_path", "run_module", "load_module", "exec_module"}
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def content_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _suffix(path: str) -> str:
    return PurePosixPath(path).suffix.lower()


def is_source(path: str) -> bool:
    return _suffix(path) in SOURCE_SUFFIXES


def is_test_entry(path: str) -> bool:
    """A file a test runner collects (not a helper or ``conftest.py``)."""

    return bool(_PY_TEST_ENTRY.search(path) or _JS_TEST_ENTRY.search(path))


_FOREIGN_TEST_NAME = re.compile(r"(^|/)([^/]*(_test|_spec|Test|Tests|Spec)\.[A-Za-z]+|test_[^/]+)$")


def is_foreign_test(path: str) -> bool:
    """A test file in a language the import graph does not parse."""

    return _suffix(path) in UNSUPPORTED_TEST_SUFFIXES and bool(
        _TEST_DIR.search(path) or _FOREIGN_TEST_NAME.search(path)
    )


def is_global_input(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return name in _GLOBAL_NAMES or any(pattern.match(name) for pattern in _GLOBAL_PATTERNS)


def _under(path: str, cwd: str) -> bool:
    return cwd in {"", "."} or path == cwd or path.startswith(cwd.rstrip("/") + "/")


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Parsed:
    #: Python: (module, level, imported names); JS: (specifier, -1, ()).
    imports: tuple[tuple[str, int, tuple[str, ...]], ...] = ()
    dynamic: bool = False
    doc_refs: tuple[str, ...] = ()


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _doc_basename(value: str) -> str:
    text = value.strip()
    if not text or len(text) > 200 or "\n" in text:
        return ""
    name = PurePosixPath(text).name
    return name.lower() if _suffix(name) in DOC_SUFFIXES else ""


def _bounded_docs(docs: set[str]) -> tuple[str, ...]:
    """The documentation basenames a file names, or the unknown marker past the bound."""

    return (DOC_REFS_UNKNOWN,) if len(docs) > _MAX_DOC_REFS else tuple(sorted(docs))


def _reads_doc(refs: Iterable[str], basename: str) -> bool:
    return any(ref == basename or ref == DOC_REFS_UNKNOWN for ref in refs)


def _parse_python(text: str) -> _Parsed:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return _Parsed(dynamic=True)
    imports: list[tuple[str, int, tuple[str, ...]]] = []
    dynamic = False
    docs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((alias.name, 0, ()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append((node.module or "", int(node.level or 0), tuple(alias.name for alias in node.names)))
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name in _DYNAMIC_PY_CALLS:
                first = node.args[0] if node.args else None
                if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value:
                    imports.append((first.value, 0, ()))
                else:
                    dynamic = True
            elif name in _LOADER_PY_CALLS:
                dynamic = True
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytest_plugins" for target in node.targets
        ):
            values = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
            for value in values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    imports.append((value.value, 0, ()))
                else:
                    dynamic = True
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            base = _doc_basename(node.value)
            if base:
                docs.add(base)
    return _Parsed(tuple(imports), dynamic, _bounded_docs(docs))


def _parse_js(text: str) -> _Parsed:
    specifiers = [match.group(2) for match in _JS_STATIC.finditer(text)]
    docs = {_doc_basename(match.group(2)) for match in _JS_DOC_LITERAL.finditer(text)} - {""}
    return _Parsed(
        tuple((specifier, -1, ()) for specifier in dict.fromkeys(specifiers)),
        bool(_JS_DYNAMIC.search(text)),
        _bounded_docs(docs),
    )


def _parse(path: str, text: str) -> _Parsed:
    return _parse_python(text) if _suffix(path) in PY_SUFFIXES else _parse_js(text)


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Resolver:
    present: set[str]
    package_dirs: set[str]
    conftests: tuple[str, ...]
    modules: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def of(cls, files: Iterable[str]) -> _Resolver:
        present = set(files)
        package_dirs = {posixpath.dirname(path) for path in present if PurePosixPath(path).name == "__init__.py"}
        conftests = tuple(sorted(path for path in present if PurePosixPath(path).name == "conftest.py"))
        resolver = cls(present, package_dirs, conftests)
        for path in present:
            if _suffix(path) in PY_SUFFIXES:
                for name in resolver.module_names(path):
                    resolver.modules.setdefault(name, set()).add(path)
        return resolver

    def module_names(self, path: str) -> set[str]:
        """Dotted names *path* is importable as: repo-root, package-root and ``src``-relative."""

        is_init = PurePosixPath(path).name == "__init__.py"
        parts = list(PurePosixPath(path).with_suffix("").parts)
        if is_init:
            parts = parts[:-1]
        if not parts:
            return set()
        names = {".".join(parts)}
        if parts[0] == "src" and len(parts) > 1:
            names.add(".".join(parts[1:]))
        # Walk up while the directory is a regular package: the module is named
        # relative to the first ancestor that is not one (pytest rootdir-style).
        directory = posixpath.dirname(path)
        depth = 0
        while directory and directory in self.package_dirs:
            directory = posixpath.dirname(directory)
            depth += 1
        start = len(parts) - depth - (0 if is_init else 1)
        if 0 <= start < len(parts):
            names.add(".".join(parts[start:]))
        return {name for name in names if name}

    def package_inits(self, path: str) -> list[str]:
        out: list[str] = []
        directory = posixpath.dirname(path)
        while directory and directory in self.package_dirs:
            init = f"{directory}/__init__.py"
            if init != path:
                out.append(init)
            directory = posixpath.dirname(directory)
        return out

    def ancestor_conftests(self, path: str) -> list[str]:
        directory = posixpath.dirname(path)
        out: list[str] = []
        for conftest in self.conftests:
            parent = posixpath.dirname(conftest)
            if conftest != path and (not parent or directory == parent or directory.startswith(parent + "/")):
                out.append(conftest)
        return out

    def python_targets(self, path: str, module: str, level: int, names: Sequence[str]) -> tuple[list[str], list[str]]:
        """(resolved repo files, unresolved absolute names) of one Python import."""

        resolved: list[str] = []
        unresolved: list[str] = []
        if level > 0:
            base = posixpath.dirname(path)
            for _ in range(level - 1):
                base = posixpath.dirname(base)
            stems = [posixpath.join(base, *module.split(".")) if module else base]
            stems.extend(posixpath.join(stems[0], name) for name in names if name != "*")
            for stem in stems:
                for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
                    candidate = posixpath.normpath(candidate)
                    if candidate in self.present:
                        resolved.append(candidate)
            for init in [*self.package_inits(f"{stems[0]}/__init__.py"), posixpath.join(stems[0], "__init__.py")]:
                if init in self.present:
                    resolved.append(init)
            return resolved, unresolved
        if not module:
            return resolved, unresolved
        candidates = [module, *(f"{module}.{name}" for name in names if name != "*")]
        pieces = module.split(".")
        # Importing a.b.c runs the a and a.b package initializers.
        prefixes = [".".join(pieces[:index]) for index in range(1, len(pieces))]
        found_module = False
        for candidate in candidates:
            files = self.modules.get(candidate)
            if files:
                resolved.extend(sorted(files))
                found_module = found_module or candidate == module
        for prefix in prefixes:
            resolved.extend(sorted(item for item in self.modules.get(prefix, ()) if item.endswith("__init__.py")))
        if not found_module and not any(self.modules.get(item) for item in candidates[1:]):
            unresolved.append(module)
        return resolved, unresolved

    def js_targets(self, path: str, specifier: str) -> tuple[list[str], list[str], bool]:
        """(resolved repo files, unresolved stems, alias) of one JS/TS specifier."""

        spec = specifier.split("?", 1)[0].split("#", 1)[0].strip()
        if spec.startswith(("./", "../")) or spec in {".", ".."}:
            stem = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
        elif spec.startswith(("@/", "~/", "#", "/")):
            return [], [], True
        else:
            return [], [], False  # a package: lockfile/manifest changes cover it
        candidates = [stem, *(stem + suffix for suffix in JS_SUFFIXES)]
        candidates.extend(f"{stem}/index{suffix}" for suffix in JS_SUFFIXES)
        if PurePosixPath(stem).suffix in {".js", ".mjs", ".cjs", ".jsx"}:
            # TypeScript resolves ``./x.js`` to ``./x.ts``.
            bare = str(PurePosixPath(stem).with_suffix(""))
            candidates.extend(bare + suffix for suffix in JS_SUFFIXES)
        hits = [candidate for candidate in candidates if candidate in self.present]
        return hits[:1] if hits else [], ([] if hits else [f"js:{stem}"]), False

    def edges(self, path: str, parsed: _Parsed) -> tuple[tuple[str, ...], bool, list[str]]:
        """Resolved dependencies, whether any import is unknowable, and unresolved names."""

        out: list[str] = []
        unresolved: list[str] = []
        dynamic = parsed.dynamic
        if _suffix(path) in PY_SUFFIXES:
            for module, level, names in parsed.imports:
                resolved, missing = self.python_targets(path, module, level, names)
                out.extend(resolved)
                unresolved.extend(missing)
            out.extend(self.package_inits(path))
            if is_test_entry(path) or _TEST_DIR.search(path):
                out.extend(self.ancestor_conftests(path))
        else:
            for specifier, _level, _names in parsed.imports:
                resolved, missing, alias = self.js_targets(path, specifier)
                out.extend(resolved)
                unresolved.extend(missing)
                dynamic = dynamic or alias
        return tuple(dict.fromkeys(item for item in out if item != path)), dynamic, unresolved


# --------------------------------------------------------------------------- #
# the static index
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ImpactMap:
    """The trusted base's import graph at one base commit (see module docstring)."""

    base_sha: str
    files: tuple[str, ...] = ()
    edges: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    dynamic: frozenset[str] = frozenset()
    doc_refs: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    unresolved: frozenset[str] = frozenset()
    unresolved_overflow: bool = False
    doc_tests: bool = False
    #: Test files in languages the graph does not parse (their recipe runs in full).
    foreign_tests: tuple[str, ...] = ()
    foreign_overflow: bool = False
    truncated: str = ""
    version: int = IMPACT_MAP_VERSION

    @property
    def usable(self) -> bool:
        return not self.truncated and self.version == IMPACT_MAP_VERSION

    @property
    def digest(self) -> str:
        return "impact:sha256:" + _digest(self.to_wire())

    def to_wire(self) -> dict[str, Any]:
        index = {path: position for position, path in enumerate(self.files)}
        return {
            "version": self.version,
            "base_sha": self.base_sha,
            "files": list(self.files),
            "edges": [[index[item] for item in self.edges.get(path, ()) if item in index] for path in self.files],
            "dynamic": sorted(index[item] for item in self.dynamic if item in index),
            "doc_refs": {str(index[path]): list(refs) for path, refs in sorted(self.doc_refs.items()) if path in index},
            "unresolved": sorted(self.unresolved),
            "unresolved_overflow": self.unresolved_overflow,
            "doc_tests": self.doc_tests,
            "foreign_tests": list(self.foreign_tests),
            "foreign_overflow": self.foreign_overflow,
            "truncated": self.truncated,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> ImpactMap:
        files = tuple(str(item) for item in raw.get("files") or ())
        rows = raw.get("edges") or ()
        if len(rows) != len(files):
            raise ValueError("impact map edges do not match its files")

        def at(position: Any) -> str:
            if not isinstance(position, int) or not 0 <= position < len(files):
                raise ValueError("impact map refers to an unknown file")
            return files[position]

        refs = raw.get("doc_refs") or {}
        return cls(
            base_sha=str(raw.get("base_sha") or ""),
            files=files,
            edges={path: tuple(at(item) for item in row) for path, row in zip(files, rows, strict=True) if row},
            dynamic=frozenset(at(item) for item in raw.get("dynamic") or ()),
            doc_refs={at(int(key)): tuple(str(item) for item in value) for key, value in dict(refs).items()},
            unresolved=frozenset(str(item) for item in raw.get("unresolved") or ()),
            unresolved_overflow=raw.get("unresolved_overflow") is True,
            doc_tests=raw.get("doc_tests") is True,
            foreign_tests=tuple(str(item) for item in raw.get("foreign_tests") or ()),
            foreign_overflow=raw.get("foreign_overflow") is True,
            truncated=str(raw.get("truncated") or ""),
            version=int(raw.get("version") or 0),
        )


def build_impact_map(
    base_sha: str,
    paths: Iterable[str] | None,
    read: Callable[[str], str | None],
    *,
    max_files: int = MAX_GRAPH_FILES,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> ImpactMap:
    """Build the base commit's import graph; past any bound the map is ``truncated`` (never used)."""

    if paths is None:
        return ImpactMap(base_sha, truncated="the trusted base tree is not readable")
    listed = list(islice(paths, MAX_LISTED_PATHS + 1))
    if len(listed) > MAX_LISTED_PATHS:
        return ImpactMap(base_sha, truncated=f"the trusted base lists more than {MAX_LISTED_PATHS} paths")
    sources = sorted({path for path in listed if is_source(path)})
    if len(sources) > max_files:
        return ImpactMap(base_sha, truncated=f"the trusted base has more than {max_files} source files")
    names = {PurePosixPath(path).name.lower(): path for path in listed}
    foreign = sorted(path for path in listed if is_foreign_test(path))
    doc_tests = False
    for config in _DOCTEST_CONFIG:
        if config in names:
            text = read(names[config])
            if text and "doctest" in text:
                doc_tests = True
    resolver = _Resolver.of(sources)
    parsed: dict[str, _Parsed] = {}
    dynamic: set[str] = set()
    total = 0
    for path in sources:
        text = read(path)
        if text is None:
            dynamic.add(path)
            continue
        size = len(text.encode("utf-8", "surrogatepass"))
        total += size
        if total > max_total_bytes:
            return ImpactMap(base_sha, truncated=f"the trusted base has more than {max_total_bytes} bytes of source")
        if size > max_file_bytes:
            dynamic.add(path)
            continue
        parsed[path] = _parse(path, text)
        if "doctest" in text and PurePosixPath(path).name == "conftest.py":
            doc_tests = True
    edges: dict[str, tuple[str, ...]] = {}
    unresolved: set[str] = set()
    docs: dict[str, tuple[str, ...]] = {}
    for path, row in parsed.items():
        resolved, unknowable, missing = resolver.edges(path, row)
        if resolved:
            edges[path] = resolved
        if unknowable:
            dynamic.add(path)
        unresolved.update(missing)
        if row.doc_refs:
            docs[path] = row.doc_refs
    for path in dynamic - set(parsed):
        # Unparsed files still load their package initializers.
        inits = resolver.package_inits(path)
        if inits:
            edges[path] = tuple(inits)
    overflow = len(unresolved) > MAX_UNRESOLVED
    return ImpactMap(
        base_sha=base_sha,
        files=tuple(sources),
        edges=edges,
        dynamic=frozenset(dynamic),
        doc_refs=docs,
        unresolved=frozenset() if overflow else frozenset(unresolved),
        unresolved_overflow=overflow,
        doc_tests=doc_tests,
        foreign_tests=tuple(foreign[:MAX_FOREIGN_TESTS]),
        foreign_overflow=len(foreign) > MAX_FOREIGN_TESTS,
    )


# --------------------------------------------------------------------------- #
# per Snapshot: head overlay + selection
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class HeadChange:
    """One changed path of a Snapshot: ``text`` is ``None`` when deleted or unreadable."""

    path: str
    deleted: bool = False
    text: str | None = None
    digest: str = ""

    @classmethod
    def of(cls, path: str, text: str | None, *, deleted: bool = False) -> HeadChange:
        digest = "deleted" if deleted else (content_digest(text) if text is not None else "unreadable")
        return cls(path=path, deleted=deleted, text=None if deleted else text, digest=digest)


@dataclass(frozen=True, slots=True)
class Overlay:
    """The base graph with one Snapshot's changed files re-parsed on the head side."""

    edges: Mapping[str, tuple[str, ...]]
    dynamic: frozenset[str]
    doc_refs: Mapping[str, tuple[str, ...]]
    present: frozenset[str]
    added: frozenset[str]
    shadowing: tuple[str, ...]


def overlay(impact: ImpactMap, changes: Sequence[HeadChange]) -> Overlay:
    """Re-parse only the changed source files on top of the base graph (incremental per Snapshot)."""

    base_files = set(impact.files)
    deleted = {row.path for row in changes if row.deleted and is_source(row.path)}
    added = {row.path for row in changes if not row.deleted and is_source(row.path) and row.path not in base_files}
    present = (base_files - deleted) | added
    resolver = _Resolver.of(present)
    edges = dict(impact.edges)
    dynamic = set(impact.dynamic)
    docs = dict(impact.doc_refs)
    shadowing: list[str] = []
    for row in changes:
        if not is_source(row.path):
            continue
        edges.pop(row.path, None)
        docs.pop(row.path, None)
        dynamic.discard(row.path)
        if row.deleted:
            continue
        if row.text is None or len(row.text.encode("utf-8", "surrogatepass")) > MAX_FILE_BYTES:
            dynamic.add(row.path)
            continue
        parsed = _parse(row.path, row.text)
        resolved, unknowable, _missing = resolver.edges(row.path, parsed)
        if resolved:
            edges[row.path] = resolved
        if unknowable:
            dynamic.add(row.path)
        if parsed.doc_refs:
            docs[row.path] = parsed.doc_refs
    # Adding a pytest conftest or Python package initializer changes how *unchanged*
    # test/modules load. The trusted-base graph cannot have edges to files that did
    # not exist at the base, so augment those edges without re-reading base source.
    # Otherwise a new fixture or package side effect yields an unsafe `none`.
    added_conftests = {path for path in added if PurePosixPath(path).name == "conftest.py"}
    added_inits = {path for path in added if PurePosixPath(path).name == "__init__.py"}
    if added_conftests or added_inits:
        for path in present:
            if _suffix(path) not in PY_SUFFIXES:
                continue
            loaded = [item for item in resolver.package_inits(path) if item in added_inits]
            if added_conftests and (is_test_entry(path) or _TEST_DIR.search(path)):
                loaded.extend(item for item in resolver.ancestor_conftests(path) if item in added_conftests)
            if loaded:
                edges[path] = tuple(dict.fromkeys((*edges.get(path, ()), *loaded)))
    for path in sorted(added):
        # A new module may satisfy an import the base could not resolve.
        if impact.unresolved_overflow:
            shadowing.append(path)
            continue
        names = resolver.module_names(path) if _suffix(path) in PY_SUFFIXES else set()
        stem = str(PurePosixPath(path).with_suffix(""))
        keys = names | {f"js:{stem}", f"js:{path}"}
        if stem.endswith("/index"):
            keys.add(f"js:{stem[: -len('/index')]}")
        if keys & impact.unresolved:
            shadowing.append(path)
    # The base resolution of unchanged files is kept; a file imported through a
    # package that gained an __init__ is still reached through that package.
    return Overlay(
        edges=edges,
        dynamic=frozenset(dynamic),
        doc_refs=docs,
        present=frozenset(present),
        added=frozenset(added),
        shadowing=tuple(shadowing),
    )


@dataclass(frozen=True, slots=True)
class RecipeSelection:
    """What one test recipe runs on one Snapshot, and why."""

    recipe_id: str
    mode: SelectionMode
    reason: str
    tests: tuple[str, ...] = ()
    total: int = 0
    #: Inputs of the run this selection executes (``""``: unknown, never reused).
    input_digest: str = ""
    #: Inputs of a full run of the recipe, for when depth or policy forces one.
    full_input_digest: str = ""
    #: Display only (never digested): ``unknown`` when the full suite runs because the
    #: graph cannot tell which tests the change reaches (Trust T3 item 9).
    coverage: Coverage = "known"

    def execution_document(self) -> dict[str, Any]:
        """What the executor consumes; part of the plan's digested execution content."""

        document: dict[str, Any] = {"mode": self.mode, "total": self.total, "input": self.input_digest}
        if self.mode == "impact":
            document["tests"] = list(self.tests)
        return document

    def to_wire(self) -> dict[str, Any]:
        unknown = {"coverage": "unknown"} if self.coverage == "unknown" else {}
        return {**self.execution_document(), "recipe_id": self.recipe_id, "reason": self.reason, **unknown}

    @classmethod
    def from_wire(cls, recipe_id: str, raw: Mapping[str, Any]) -> RecipeSelection:
        mode = str(raw.get("mode") or "all")
        chosen = cast(SelectionMode, mode if mode in {"impact", "all", "none"} else "all")
        return cls(
            recipe_id=recipe_id,
            mode=chosen,
            reason=str(raw.get("reason") or ""),
            tests=tuple(str(item) for item in raw.get("tests") or ()) if chosen == "impact" else (),
            total=int(raw.get("total") or 0),
            input_digest=str(raw.get("input") or ""),
            coverage="unknown" if raw.get("coverage") == "unknown" else "known",
        )

    def as_full(self, reason: str) -> RecipeSelection:
        """The same recipe run in full (Deep, policy-required or requested work)."""

        return RecipeSelection(
            self.recipe_id,
            "all",
            reason,
            total=self.total,
            input_digest=self.full_input_digest,
            full_input_digest=self.full_input_digest,
        )

    def cwd_relative(self, cwd: str) -> tuple[str, ...]:
        """Selected test files as the recipe's ``{tests}`` argument (relative to its ``cwd``)."""

        if cwd in {"", "."}:
            return self.tests
        prefix = cwd.rstrip("/") + "/"
        return tuple(path[len(prefix) :] if path.startswith(prefix) else path for path in self.tests)


def _whole_digest(recipe: CheckRecipe, base_sha: str, changes: Sequence[HeadChange], mode: str) -> str:
    return "inputs:sha256:" + _digest(
        {
            "v": IMPACT_MAP_VERSION,
            "scope": "whole_tree",
            "base": base_sha,
            "recipe": recipe.digest,
            "mode": mode,
            "changes": sorted([row.path, row.digest] for row in changes),
        }
    )


def whole_tree_digest(recipe: CheckRecipe, base_sha: str, changes: Sequence[HeadChange]) -> str:
    """Input digest when nothing narrower is known: the whole head tree over the base commit."""

    return _whole_digest(recipe, base_sha, changes, "all")


def _full(
    recipe: CheckRecipe,
    reason: str,
    base_sha: str,
    changes: Sequence[HeadChange],
    total: int = 0,
    *,
    known: bool = True,
    coverage: Coverage = "known",
) -> RecipeSelection:
    """Run the recipe in full; its inputs are the whole changed tree (unknown when *known* is false)."""

    digest = (
        _whole_digest(recipe, base_sha, changes, "all")
        if known and all(row.digest != "unreadable" for row in changes)
        else ""
    )
    return RecipeSelection(
        recipe.id, "all", reason, total=total, input_digest=digest, full_input_digest=digest, coverage=coverage
    )


def _closure(edges: Mapping[str, Sequence[str]], roots: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    pending = list(roots)
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(edges.get(current, ()))
    return seen


def select_recipe_tests(
    impact: ImpactMap | None,
    recipe: CheckRecipe,
    changes: Sequence[HeadChange],
    *,
    base_sha: str,
    unavailable: str = "",
    complete: bool = True,
) -> RecipeSelection:
    """The test files of *recipe* this Snapshot's changes can affect (module docstring)."""

    short = base_sha[:12]
    known = complete and len(changes) <= MAX_CHANGED and bool(base_sha)

    def full(reason: str, total: int = 0, *, unknown: bool = True) -> RecipeSelection:
        # Unknown unless the change is known to reach every test (fail toward unknown).
        return _full(recipe, reason, base_sha, changes, total, known=known, coverage="unknown" if unknown else "known")

    if recipe.kind != "test":
        return full("impact selection applies to test recipes only", unknown=False)
    if impact is None:
        return full(f"full suite: no test-impact map for base {short} ({unavailable or 'not built'})")
    if impact.base_sha != base_sha:
        return full(
            f"full suite: the test-impact map is stale (built for base {impact.base_sha[:12]}, "
            f"this Snapshot's base is {short})"
        )
    if impact.version != IMPACT_MAP_VERSION:
        return full(
            f"full suite: the test-impact map is stale (builder v{impact.version}, current v{IMPACT_MAP_VERSION})"
        )
    if impact.truncated:
        return full(f"full suite: test-impact map incomplete ({impact.truncated})")
    if not complete:
        return full("full suite: this Snapshot's changed-file list is incomplete")
    if len(changes) > MAX_CHANGED:
        return full(f"full suite: more than {MAX_CHANGED} changed paths")

    graph = overlay(impact, changes)
    cwd = recipe.cwd
    unsupported = sorted(
        path
        for path in (
            *impact.foreign_tests,
            *(row.path for row in changes if not row.deleted and is_foreign_test(row.path)),
        )
        if _under(path, cwd)
    )
    if impact.foreign_overflow:
        unsupported.append("(more test files in other languages than the map records)")
    tests = sorted(path for path in graph.present if _under(path, cwd) and is_test_entry(path))
    if unsupported:
        return full(f"full suite: tests in {_suffix(unsupported[0])} are outside the import graph")
    if not tests:
        where = "the repository" if cwd in {"", "."} else cwd
        return full(f"full suite: no Python or JS/TS test files recognized under {where}")
    declared = set(recipe.inputs) | ({recipe.definition} if recipe.definition else set())
    seeds: set[str] = set()
    ignored_docs: list[str] = []
    for row in changes:
        path = row.path
        if path in declared:
            return full(f"full suite: {path} is an input of this recipe", len(tests), unknown=False)
        if is_global_input(path):
            return full(
                f"full suite: {path} is a dependency or test-runner configuration input", len(tests), unknown=False
            )
        if is_source(path):
            seeds.add(path)
            continue
        if _suffix(path) in DOC_SUFFIXES:
            if impact.doc_tests:
                return full(f"full suite: {path} may be collected as a doctest", len(tests))
            basename = PurePosixPath(path).name.lower()
            readers = [source for source, refs in graph.doc_refs.items() if _reads_doc(refs, basename)]
            if readers:
                seeds.update(readers)
            else:
                ignored_docs.append(path)
            continue
        return full(f"full suite: {path} is outside the import graph and may be read at runtime", len(tests))
    if graph.shadowing:
        return full(
            f"full suite: added {graph.shadowing[0]} may satisfy an import the base could not resolve", len(tests)
        )
    # Files whose imports are unknowable may load any changed module.
    if any(is_source(row.path) for row in changes):
        seeds |= set(graph.dynamic)
    reverse: dict[str, list[str]] = {}
    for source, targets in graph.edges.items():
        for target in targets:
            reverse.setdefault(target, []).append(source)
    affected = _closure(reverse, seeds) if seeds else set()

    def inputs_of(executed: Sequence[str], mode: SelectionMode) -> str:
        closure = _closure(graph.edges, executed)
        if graph.dynamic & closure:
            # A test reaches a file whose imports are unknowable: every change counts.
            relevant = list(changes)
        else:
            read_docs = {basename for path in closure for basename in graph.doc_refs.get(path, ())}
            relevant = [
                row
                for row in changes
                if row.path in closure
                or (row.deleted and row.path in affected)
                or (_suffix(row.path) in DOC_SUFFIXES and _reads_doc(read_docs, PurePosixPath(row.path).name.lower()))
            ]
        if any(row.digest == "unreadable" for row in relevant):
            return ""
        return "inputs:sha256:" + _digest(
            {
                "v": IMPACT_MAP_VERSION,
                "scope": "closure",
                "base": base_sha,
                "recipe": recipe.digest,
                "mode": mode,
                "tests": list(executed) if mode == "impact" else [],
                "changes": sorted([row.path, row.digest] for row in relevant),
            }
        )

    full_digest = inputs_of(tests, "all")
    if not seeds:
        return RecipeSelection(
            recipe.id,
            "none",
            f"test impact: only documentation no test reads changed ({', '.join(ignored_docs[:3])})",
            total=len(tests),
            full_input_digest=full_digest,
        )
    selected = [path for path in tests if path in affected]
    changed_tests = sum(1 for path in selected if path in {row.path for row in changes})
    if not selected:
        return RecipeSelection(
            recipe.id,
            "none",
            f"test impact: no test under {cwd if cwd not in {'', '.'} else 'the repository'} reaches a changed "
            f"file (import graph at base {short})",
            total=len(tests),
            full_input_digest=full_digest,
        )
    detail = f"{len(selected)} of {len(tests)} test files reach a changed file" + (
        f" ({changed_tests} changed)" if changed_tests else ""
    )
    if len(selected) < len(tests) and recipe.selectable and len(selected) <= _max_args():
        return RecipeSelection(
            recipe.id,
            "impact",
            f"test impact: {detail}",
            tuple(selected),
            len(tests),
            inputs_of(selected, "impact"),
            full_digest,
        )
    if len(selected) == len(tests):
        why = f"every test file reaches a changed file ({len(tests)})"
    elif not recipe.selectable:
        why = f"{detail}; the recipe takes no {{tests}} argument, so it runs whole"
    else:
        why = f"{detail}; more than {_max_args()} files, so it runs whole"
    return RecipeSelection(
        recipe.id,
        "all",
        f"full suite: {why}",
        total=len(tests),
        input_digest=full_digest,
        full_input_digest=full_digest,
    )


def _max_args() -> int:
    from lemoncrow.pro.capabilities.review.recipes import MAX_SELECTED_TEST_ARGS

    return MAX_SELECTED_TEST_ARGS


# --------------------------------------------------------------------------- #
# plan entries
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EntryImpact:
    """Test-impact selections for the recipes one run-plan entry executes."""

    entry_id: str
    recipes: tuple[RecipeSelection, ...]
    map_digest: str = ""

    def digest_document(self) -> dict[str, Any]:
        return {"entry": self.entry_id, **selection_execution_document(self.selection_document())}

    def selection_document(self) -> dict[str, Any]:
        """The plan entry's ``selection``: per recipe mode, tests, inputs and the reason."""

        return {"map": self.map_digest, "recipes": {row.recipe_id: row.to_wire() for row in self.recipes}}

    def full(self, reason: str) -> EntryImpact:
        return EntryImpact(self.entry_id, tuple(row.as_full(reason) for row in self.recipes), self.map_digest)

    def summary(self) -> str:
        parts: list[str] = []
        for row in self.recipes:
            parts.append(row.reason if len(self.recipes) == 1 else f"{row.recipe_id}: {row.reason}")
        return "; ".join(dict.fromkeys(parts))


def selection_execution_document(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """The digested part of a plan entry's ``selection`` (reasons are display only)."""

    selections = selections_from_document(raw)
    return {
        "map": str(raw.get("map") or "") if isinstance(raw, Mapping) else "",
        "recipes": {recipe_id: row.execution_document() for recipe_id, row in sorted(selections.items())},
    }


def selections_from_document(raw: Mapping[str, Any] | None) -> dict[str, RecipeSelection]:
    """Recipe id -> selection from a plan entry's ``selection`` document (empty: run every recipe in full)."""

    if not isinstance(raw, Mapping):
        return {}
    recipes = raw.get("recipes")
    if not isinstance(recipes, Mapping):
        return {}
    return {
        str(recipe_id): RecipeSelection.from_wire(str(recipe_id), row)
        for recipe_id, row in recipes.items()
        if isinstance(row, Mapping)
    }


def apply_impact(drafts: Mapping[str, Any], impact: Mapping[str, EntryImpact]) -> None:
    """Narrow running check entries to the tests a change can affect (planner registration point).

    Sets each running entry's ``selection`` and states why in its reason. Deep
    runs the full suite; policy-required and requested entries (and what they
    depend on) run in full, because selection never narrows required work. An
    entry whose recipes all reach nothing is skipped with the reason, and so is
    a non-required dependent of it.
    """

    forcing: dict[str, str] = {}
    for draft in drafts.values():
        if draft.status != "run":
            continue
        why = _forced(draft)
        if why:
            forcing.setdefault(draft.catalog.id, why)
            for dep_id in draft.catalog.dependencies:
                forcing.setdefault(dep_id, f"{why} ({draft.catalog.id} depends on it)")
    skipped: dict[str, str] = {}
    for entry_id, draft in drafts.items():
        row = impact.get(entry_id)
        if row is None or draft.status != "run" or not row.recipes:
            continue
        forced = forcing.get(entry_id, "")
        if forced:
            draft.selection = row.full(forced).selection_document()
            draft.reason = _joined(draft.reason, forced)
            continue
        summary = row.summary()
        draft.selection = row.selection_document()
        if {selection.mode for selection in row.recipes} == {"none"}:
            draft.status = "skipped"
            draft.reason = summary
            skipped[entry_id] = summary
            continue
        draft.reason = _joined(draft.reason, summary)
    if not skipped:
        return
    for draft in drafts.values():
        if draft.status != "run" or draft.required:
            continue
        for dep_id in draft.catalog.dependencies:
            if dep_id in skipped:
                draft.status = "skipped"
                draft.reason = f"depends on {dep_id} (skipped: {skipped[dep_id]})"
                break


def _forced(draft: Any) -> str:
    if draft.depth == "deep":
        return "full suite: Deep runs every test"
    if draft.required:
        return "full suite: selection never narrows policy-required work"
    if draft.manual:
        return "full suite: requested for this Review"
    return ""


def _joined(first: str, second: str) -> str:
    return f"{first}; {second}" if first and second else first or second


__all__ = [
    "IMPACT_MAP_ENTRY",
    "IMPACT_MAP_EXECUTOR",
    "IMPACT_MAP_KIND",
    "IMPACT_MAP_STAGE",
    "IMPACT_MAP_VERSION",
    "MAX_CHANGED",
    "MAX_FILE_BYTES",
    "MAX_GRAPH_FILES",
    "MAX_TOTAL_BYTES",
    "EntryImpact",
    "HeadChange",
    "ImpactMap",
    "Overlay",
    "RecipeSelection",
    "apply_impact",
    "build_impact_map",
    "content_digest",
    "is_global_input",
    "is_source",
    "is_test_entry",
    "overlay",
    "select_recipe_tests",
    "selections_from_document",
    "whole_tree_digest",
]
