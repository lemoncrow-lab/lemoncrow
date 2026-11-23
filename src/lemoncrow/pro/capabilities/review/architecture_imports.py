"""What a source file's import statements can reach, for the Review's file-reference graph.

A bare name does not say which file it means: ``root``, ``entry`` and ``run`` are defined at class or file level in
hundreds of files, and a local of the same name in another file is not a use of any of them. For the languages read
here a use of another file's name has to come through an import, so that is what ties two files together.

Imports are read with line patterns, not parsed: a pattern can only over-read text that looks like an import (a
docstring, say), and the name it brings in must still be used and defined at the other end.
"""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# lc-debt: Python and JavaScript/TypeScript only, and tsconfig `paths` beyond the @/ ~/ #/ src/ prefixes are not read;
# other languages keep bare-name matching. Upgrade path: per-language import tags in infra.tree_sitter.tags.
_PYTHON_SUFFIXES = (".py", ".pyi")
_SCRIPT_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_SCRIPT_ALIASES = ("@/", "~/", "#/", "src/")
#: How many re-exporting files a name is followed through (a package ``__init__``, a barrel ``index.ts``).
_MAX_REEXPORT_HOPS = 4

_PY_FROM = re.compile(r"^[ \t]*from[ \t]+(\.*[\w.]*)[ \t]+import[ \t]+(\([^)]*\)|(?:[^\n#\\]|\\\n)*)", re.MULTILINE)
_PY_IMPORT = re.compile(r"^[ \t]*import[ \t]+([^\n#;]+)", re.MULTILINE)
_PY_COMMENT = re.compile(r"#[^\n]*")
_JS_FROM = re.compile(
    r"^[ \t]*(?:import|export)[ \t]+(?:type[ \t]+)?"
    r"((?:[\w$]+[ \t]*,?[ \t]*)?(?:\*(?:[ \t]*as[ \t]+[\w$]+)?|\{[^}]*\})?)\s*from\s*['\"]([^'\"\n]+)['\"]",
    re.MULTILINE,
)
_JS_REQUIRE = re.compile(r"\b(?:const|let|var)\s+(\{[^}]*\}|[\w$]+)\s*=\s*require\(\s*['\"]([^'\"\n]+)['\"]\s*\)")
_JS_DYNAMIC = re.compile(r"\bimport\(\s*['\"]([^'\"\n]+)['\"]\s*\)")


@dataclass(slots=True)
class _FileImports:
    #: Local name -> (module spec, name in that module). An empty name is the module itself, or its default export.
    bindings: dict[str, list[tuple[str, str]]] = field(default_factory=lambda: defaultdict(list))
    #: Modules whose members this file can reach by attribute (``import a.b``, ``import * as a``).
    namespaces: list[str] = field(default_factory=list)
    #: Modules everything of which this file passes on (``from a import *``, ``export * from``).
    star: list[str] = field(default_factory=list)


def _python_imports(text: str) -> _FileImports:
    found = _FileImports()
    for match in _PY_IMPORT.finditer(text):
        for clause in match.group(1).split(","):
            words = clause.split()
            if not words or not re.fullmatch(r"[\w.]+", words[0]):
                continue
            spec = words[0]
            local = words[2] if len(words) == 3 and words[1] == "as" else spec.rsplit(".", 1)[-1]
            found.bindings[local].append((spec, ""))
            found.namespaces.append(spec)
    for match in _PY_FROM.finditer(text):
        spec = match.group(1)
        names = _PY_COMMENT.sub("", match.group(2)).strip("() \t\n").replace("\\\n", " ")
        for clause in names.split(","):
            words = clause.split()
            if not words:
                continue
            if words[0] == "*":
                found.star.append(spec)
                found.namespaces.append(spec)
                continue
            local = words[2] if len(words) == 3 and words[1] == "as" else words[0]
            found.bindings[local].append((spec, words[0]))
    return found


def _script_named(found: _FileImports, spec: str, clause: str) -> None:
    for item in clause.strip("{} \t\n").split(","):
        words = item.replace(":", " as ").split()
        if words and words[0] == "type":
            words = words[1:]
        if not words:
            continue
        local = words[2] if len(words) == 3 and words[1] == "as" else words[0]
        found.bindings[local].append((spec, "" if words[0] == "default" else words[0]))


def _script_imports(text: str) -> _FileImports:
    found = _FileImports()
    for match in _JS_FROM.finditer(text):
        clause, spec = match.group(1).strip(), match.group(2)
        braces = clause.find("{")
        if braces >= 0:
            _script_named(found, spec, clause[braces:])
            clause = clause[:braces]
        clause = clause.strip(" \t\n,")
        if clause.startswith("*") or ", *" in clause or ",*" in clause:
            head, _, star = clause.rpartition("*")
            words = star.split()
            if len(words) == 2:
                found.bindings[words[1]].append((spec, ""))
                found.namespaces.append(spec)
            else:
                found.star.append(spec)
            clause = head.strip(" \t\n,")
        if clause:
            found.bindings[clause].append((spec, ""))
    for match in _JS_REQUIRE.finditer(text):
        target, spec = match.group(1), match.group(2)
        if target.startswith("{"):
            _script_named(found, spec, target)
        else:
            found.bindings[target].append((spec, ""))
            found.namespaces.append(spec)
    found.namespaces.extend(match.group(1) for match in _JS_DYNAMIC.finditer(text))
    return found


def _shared_prefix(left: str, right: str) -> int:
    count = 0
    for one, other in zip(left.split("/"), right.split("/"), strict=False):
        if one != other:
            break
        count += 1
    return count


def _nearest(source: str, candidates: Iterable[str]) -> tuple[str, ...]:
    """The candidates closest to *source* in the tree: the same module path can exist in several packages."""

    rows = sorted(set(candidates))
    if len(rows) < 2:
        return tuple(rows)
    best = max(_shared_prefix(source, row) for row in rows)
    return tuple(row for row in rows if _shared_prefix(source, row) == best)


def _python_module(path: str) -> tuple[str, ...]:
    parts = PurePosixPath(path).with_suffix("").parts
    return parts[:-1] if parts[-1] == "__init__" else parts


class ImportResolver:
    """Which files a use of a name in one file can mean, going by that file's imports."""

    def __init__(
        self,
        root: Path,
        paths: Collection[str],
        definitions: Mapping[str, Collection[str]],
        *,
        read: Callable[[str], str] | None = None,
    ) -> None:
        self._root = root
        self._read_text = read
        self._paths = frozenset(paths)
        self._definitions = definitions
        self._imports: dict[str, _FileImports] = {}
        self._namespaces: dict[str, frozenset[str]] = {}
        self._resolved: dict[tuple[str, str], tuple[str, ...]] = {}
        self._python_by_tail: dict[str, list[str]] = defaultdict(list)
        self._script_by_stem: dict[str, list[str]] = defaultdict(list)
        for path in self._paths:
            if path.endswith(_PYTHON_SUFFIXES):
                module = _python_module(path)
                if module:
                    self._python_by_tail[module[-1]].append(path)
            elif path.endswith(_SCRIPT_SUFFIXES):
                self._script_by_stem[PurePosixPath(path).stem].append(path)

    def targets(self, source: str, name: str) -> frozenset[str] | None:
        """The files *name* in *source* can refer to; ``None`` for a language whose imports are not read."""

        python = source.endswith(_PYTHON_SUFFIXES)
        if not python and not source.endswith(_SCRIPT_SUFFIXES):
            return None
        found: set[str] = set()
        imports = self._read(source)
        bound = imports.bindings.get(name, ())
        for spec, imported in bound:
            for path in self._resolve(source, spec):
                if imported:
                    found.update(self._defining(path, imported, _MAX_REEXPORT_HOPS))
                else:
                    found.add(path)
            if python and imported:
                found.update(self._resolve(source, _submodule(spec, imported)))
        if not bound:
            found.update(path for path in self._namespace_files(source) if path in self._definitions.get(name, ()))
        found.discard(source)
        return frozenset(found)

    def binding_unresolved(self, source: str, name: str) -> bool:
        """Whether a script imports *name* from a module path not read here (a workspace package, a tsconfig path)."""

        if source.endswith(_PYTHON_SUFFIXES):
            return False  # a Python module is matched by its dotted tail: not found means another package
        return any(not self._resolve(source, spec) for spec, _imported in self._read(source).bindings.get(name, ()))

    def reaches(self, source: str, target: str) -> bool:
        """Whether *source* imports *target*, directly or through package ``__init__`` / ``index`` files."""

        seen: set[str] = set()
        frontier = self._imported_files(source)
        for _hop in range(_MAX_REEXPORT_HOPS + 1):
            if target in frontier:
                return True
            passing_on = {path for path in frontier - seen if PurePosixPath(path).stem in {"__init__", "index"}}
            seen |= frontier
            frontier = {found for path in passing_on for found in self._imported_files(path)}
        return False

    def _imported_files(self, source: str) -> set[str]:
        imports = self._read(source)
        files = set(self._namespace_files(source))
        files.update(path for spec in imports.star for path in self._resolve(source, spec))
        for rows in imports.bindings.values():
            for spec, _imported in rows:
                files.update(self._resolve(source, spec))
        return files

    def _namespace_files(self, source: str) -> frozenset[str]:
        known = self._namespaces.get(source)
        if known is not None:
            return known
        imports = self._read(source)
        files = {path for spec in imports.namespaces for path in self._resolve(source, spec)}
        if source.endswith(_PYTHON_SUFFIXES):
            for rows in imports.bindings.values():
                for spec, imported in rows:
                    if imported:  # ``from package import module``
                        files.update(self._resolve(source, _submodule(spec, imported)))
        result = frozenset(files)
        self._namespaces[source] = result
        return result

    def _defining(self, path: str, name: str, hops: int) -> set[str]:
        """*path* when it defines *name*, else the files it passes the name on from."""

        if path in self._definitions.get(name, ()):
            return {path}
        if hops <= 0:
            return set()
        found: set[str] = set()
        imports = self._read(path)
        for spec, imported in imports.bindings.get(name, ()):
            for next_path in self._resolve(path, spec):
                if imported:
                    found.update(self._defining(next_path, imported, hops - 1))
                else:
                    found.add(next_path)
        for spec in imports.star:
            for next_path in self._resolve(path, spec):
                found.update(self._defining(next_path, name, hops - 1))
        return found

    def _read(self, path: str) -> _FileImports:
        known = self._imports.get(path)
        if known is not None:
            return known
        if self._read_text is not None:
            text = self._read_text(path)
        else:
            try:
                text = (self._root / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
        parsed = _python_imports(text) if path.endswith(_PYTHON_SUFFIXES) else _script_imports(text)
        self._imports[path] = parsed
        return parsed

    def _resolve(self, source: str, spec: str) -> tuple[str, ...]:
        key = (source, spec)
        known = self._resolved.get(key)
        if known is None:
            known = (
                self._resolve_python(source, spec)
                if source.endswith(_PYTHON_SUFFIXES)
                else self._resolve_script(source, spec)
            )
            self._resolved[key] = known
        return known

    def _resolve_python(self, source: str, spec: str) -> tuple[str, ...]:
        bare = spec.lstrip(".")
        dots = len(spec) - len(bare)
        parts = tuple(part for part in bare.split(".") if part)
        if dots:
            base = PurePosixPath(source).parent
            for _ in range(dots - 1):
                base = base.parent
            stem = base.joinpath(*parts)
            options = (f"{stem}.py", f"{stem}.pyi", f"{stem}/__init__.py", f"{stem}/__init__.pyi")
            return tuple(path.removeprefix("./") for path in options if path.removeprefix("./") in self._paths)[:1]
        if not parts:
            return ()
        return _nearest(
            source,
            (path for path in self._python_by_tail.get(parts[-1], ()) if _python_module(path)[-len(parts) :] == parts),
        )

    def _resolve_script(self, source: str, spec: str) -> tuple[str, ...]:
        if spec.startswith("."):
            base = posixpath.normpath(posixpath.join(posixpath.dirname(source), spec))
            return self._script_file(base)[:1]
        for prefix in _SCRIPT_ALIASES:
            if spec.startswith(prefix):
                rest = spec[len(prefix) :].rstrip("/")
                stem = PurePosixPath(rest).stem or rest
                rows = [*self._script_by_stem.get(stem, ()), *self._script_by_stem.get("index", ())]
                return _nearest(source, (path for path in rows if _script_matches(path, rest)))
        return ()

    def _script_file(self, base: str) -> tuple[str, ...]:
        stems = [base]
        for suffix in (".js", ".jsx", ".mjs", ".cjs"):
            if base.endswith(suffix):  # TypeScript imports its own files by the emitted name
                stems.append(base[: -len(suffix)])
        options = [base]
        for stem in stems:
            options.extend(f"{stem}{suffix}" for suffix in _SCRIPT_SUFFIXES)
            options.extend(f"{stem}/index{suffix}" for suffix in _SCRIPT_SUFFIXES)
        return tuple(path for path in options if path in self._paths)


def _submodule(spec: str, name: str) -> str:
    return f"{spec}{name}" if spec.endswith(".") else f"{spec}.{name}"


def _script_matches(path: str, rest: str) -> bool:
    bare = path
    for suffix in _SCRIPT_SUFFIXES:
        if bare.endswith(suffix):
            bare = bare[: -len(suffix)]
            break
    return bare == rest or bare.endswith(f"/{rest}") or bare.endswith(f"/{rest}/index") or bare == f"{rest}/index"


__all__ = ["ImportResolver"]
