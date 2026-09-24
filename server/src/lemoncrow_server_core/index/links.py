"""Layer 3: the per-view link index, and the incremental update that maintains it.

This is the only path-sensitive layer. Layer 1 knows that a blob contains the
string ``pkg.alpha`` and defines ``alpha``; only a view knows which file
``pkg.alpha`` *is*, so import, reference and call edges are resolved here and
nowhere else.

The module is deliberately pure. It takes facts and returns a graph; it opens
no database, holds no lock and knows nothing about organizations. Both the
in-process store and the SQLite store call the same two functions, which is
what makes one differential oracle cover both.

**The incremental contract.** :func:`update_graph` must produce exactly what
:func:`build_graph` would produce for the same file set. Everything below is in
service of that one equality:

* the *changed* set is the paths whose digest differs, plus additions and
  removals;
* a changed path can only affect another path through a **symbol** whose set of
  definers changed, or through a **module** whose set of claimants changed --
  so those two symmetric differences are the entire blast radius;
* the reverse indexes ``referrers`` (symbol -> paths that reference it) and
  ``candidate_importers`` (module name -> paths that *looked that name up*,
  whether or not it resolved) turn that blast radius into a dirty set;
* every edge whose source is dirty is dropped and re-resolved; every other edge
  is provably unchanged, because edge resolution reads only the source's own
  facts plus the definer and module tables.

``candidate_importers`` indexes *attempted* module names rather than resolved
ones on purpose. A file that imports a module which does not exist yet has no
resolved dependency to invalidate, so indexing only successful resolutions
would miss the moment that module appears -- a stale *missing* edge, which is
fail-safe, but also the moment it is renamed onto an existing name, which is
not.

Ambiguity is always resolved fail-safe: when a symbol has more than one
definer and no import binding picks one, no edge is emitted. A missing edge
costs recall; a wrong edge produces a false answer, and the plan makes a wrong
edge release-blocking.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from .contracts import (
    AnalysisArtifact,
    Edge,
    EdgeKind,
    ImportRef,
    LinkBuildMode,
    LinkBuildReport,
    SymbolDef,
    SymbolRef,
)

__all__ = [
    "DEFAULT_FULL_REBUILD_RATIO",
    "Divergence",
    "FileFacts",
    "LinkGraph",
    "build_graph",
    "changed_paths",
    "classify_divergence",
    "facts_from",
    "import_candidates",
    "module_names",
    "update_graph",
]

#: Past this fraction of the view changing, an incremental update stops paying
#: for itself and a clean rebuild is cheaper as well as simpler.
DEFAULT_FULL_REBUILD_RATIO: Final[float] = 0.35

_EXTENSIONS: Final[Mapping[str, tuple[str, ...]]] = {
    "python": (".py", ".pyi"),
    "typescript": (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
    "go": (".go",),
    "rust": (".rs",),
}


@dataclass(frozen=True, slots=True)
class FileFacts:
    """One view member: its path plus the Layer-1 facts of its content.

    The pairing is the whole of Layer 3's input. ``digest`` is carried so a
    change can be detected without re-reading the facts.
    """

    path: str
    digest: str
    language: str
    definitions: tuple[SymbolDef, ...]
    exports: tuple[str, ...]
    imports: tuple[ImportRef, ...]
    references: tuple[SymbolRef, ...]


def _strip_extension(path: str, language: str) -> str:
    for extension in _EXTENSIONS.get(language, ()):
        if path.endswith(extension):
            return path[: -len(extension)]
    dot = path.rfind(".")
    return path[:dot] if dot > path.rfind("/") else path


def module_names(path: str, language: str) -> tuple[str, ...]:
    """Every module identifier under which ``path`` can be imported.

    Path-sensitive by definition, which is exactly why it lives in Layer 3 and
    not in the immutable content artifact.
    """
    stem = _strip_extension(path, language)
    if language == "python":
        if stem == "__init__":
            return ()
        if stem.endswith("/__init__"):
            stem = stem[: -len("/__init__")]
        return (stem.replace("/", "."),) if stem else ()
    if language == "typescript":
        names = [stem]
        if stem.endswith("/index"):
            names.append(stem[: -len("/index")])
        elif stem == "index":
            names.append(".")
        return tuple(names)
    if language == "go":
        directory = path.rsplit("/", 1)[0] if "/" in path else "."
        return (directory,)
    if language == "rust":
        if stem.startswith("src/"):
            stem = stem[len("src/") :]
        if stem in {"lib", "main"}:
            return ("crate",)
        if stem.endswith("/mod"):
            stem = stem[: -len("/mod")]
        return (stem.replace("/", "::"),) if stem else ()
    return (stem,) if stem else ()


def _python_package(path: str) -> str:
    directory = path.rsplit("/", 1)[0] if "/" in path else ""
    return directory.replace("/", ".")


def _normalize_relative(base_directory: str, spec: str) -> str:
    parts = [piece for piece in base_directory.split("/") if piece]
    for segment in spec.split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            if parts:
                parts.pop()
            continue
        parts.append(segment)
    return "/".join(parts)


def import_candidates(facts: FileFacts, ref: ImportRef) -> tuple[str, ...]:
    """Module names an import *looks up*, best candidate first.

    Recorded whether or not any of them resolve: the incremental update indexes
    attempted names so that a module appearing later invalidates the files that
    were already asking for it.
    """
    language = facts.language
    module = ref.module
    if language == "python":
        if ref.level > 0:
            package = _python_package(facts.path)
            parts = [piece for piece in package.split(".") if piece]
            for _ in range(ref.level - 1):
                if parts:
                    parts.pop()
            base = ".".join(parts)
            if module:
                base = f"{base}.{module}" if base else module
            if not base:
                return ()
            return _dotted_prefixes(base)
        return _dotted_prefixes(module) if module else ()
    if language == "typescript":
        if not module:
            return ()
        spec = _strip_extension(module, "typescript")
        if spec.startswith("."):
            directory = facts.path.rsplit("/", 1)[0] if "/" in facts.path else ""
            resolved = _normalize_relative(directory, spec)
            return (resolved, f"{resolved}/index") if resolved else (".",)
        return (spec, f"{spec}/index")
    if language == "go":
        if not module:
            return ()
        pieces = module.split("/")
        return tuple("/".join(pieces[index:]) for index in range(len(pieces)))
    if language == "rust":
        if not module:
            return ()
        body = module
        for prefix in ("crate::", "self::"):
            if body.startswith(prefix):
                body = body[len(prefix) :]
        return tuple(_colon_prefixes(body))
    return ()


def _dotted_prefixes(module: str) -> tuple[str, ...]:
    parts = module.split(".")
    return tuple(".".join(parts[: index + 1]) for index in range(len(parts) - 1, -1, -1))


def _colon_prefixes(module: str) -> tuple[str, ...]:
    parts = module.split("::")
    return tuple("::".join(parts[: index + 1]) for index in range(len(parts) - 1, -1, -1))


@dataclass(frozen=True, slots=True)
class _Resolved:
    edges: tuple[Edge, ...]
    candidates: frozenset[str]


def _resolve_one(
    facts: FileFacts,
    *,
    module_index: Mapping[str, str],
    definers: Mapping[str, frozenset[str]],
) -> _Resolved:
    """Every edge sourced at ``facts.path``, plus the module names it tried.

    Pure: the same arguments always produce the same edges. That is what lets
    an incremental update re-resolve a handful of files and still be equal to a
    clean rebuild of all of them.
    """
    edges: list[Edge] = []
    candidates: set[str] = set()
    bindings: dict[str, tuple[str, str]] = {}

    for ref in facts.imports:
        attempted = import_candidates(facts, ref)
        candidates.update(attempted)
        target: str | None = None
        matched = ""
        for candidate in attempted:
            found = module_index.get(candidate)
            if found is not None:
                target = found
                matched = candidate
                break
        if target is None or target == facts.path:
            continue
        edges.append(
            Edge(
                kind=EdgeKind.IMPORT,
                src_path=facts.path,
                dst_path=target,
                symbol=matched,
                src_line=ref.line,
            )
        )
        if ref.wildcard:
            # A star import binds an unknown set of names. Binding nothing is
            # the fail-safe reading: the global fallback may still resolve a
            # reference, and it will do so identically in a clean rebuild.
            continue
        for original, local in ref.bindings:
            if local and local not in bindings:
                bindings[local] = (target, original)

    for use in facts.references:
        resolved: str | None = None
        symbol = use.name
        bound = bindings.get(use.name)
        if bound is not None:
            candidate_path, original = bound
            # An import binding only picks a target if the target really
            # defines the name. A re-export chain therefore produces no edge --
            # fail-safe -- rather than an edge to a file that does not have it.
            if candidate_path in definers.get(original, frozenset()):
                resolved = candidate_path
                symbol = original
        if resolved is None:
            owners = definers.get(use.name, frozenset())
            if len(owners) == 1:
                only = next(iter(owners))
                if only != facts.path:
                    resolved = only
        if resolved is None or resolved == facts.path:
            continue
        edges.append(
            Edge(
                kind=EdgeKind.CALL if use.is_call else EdgeKind.REFERENCE,
                src_path=facts.path,
                dst_path=resolved,
                symbol=symbol,
                src_line=use.line,
            )
        )

    return _Resolved(edges=tuple(edges), candidates=frozenset(candidates))


@dataclass(frozen=True, slots=True)
class LinkGraph:
    """The resolved edge set for one view, plus the indexes that maintain it.

    ``snapshot`` is the ``path -> digest`` map the edges were resolved from. It
    is what makes the next update incremental: the change set is a diff against
    it rather than a guess about what the client edited.
    """

    at_view_revision: int
    edges: frozenset[Edge]
    snapshot: Mapping[str, str]
    path_modules: Mapping[str, frozenset[str]]
    path_defines: Mapping[str, frozenset[str]]
    path_refs: Mapping[str, frozenset[str]]
    path_candidates: Mapping[str, frozenset[str]]

    @property
    def covered_paths(self) -> int:
        return len(self.snapshot)

    def module_claims(self) -> dict[str, set[str]]:
        claims: dict[str, set[str]] = {}
        for path, modules in self.path_modules.items():
            for module in modules:
                claims.setdefault(module, set()).add(path)
        return claims

    def definers(self) -> dict[str, set[str]]:
        table: dict[str, set[str]] = {}
        for path, names in self.path_defines.items():
            for name in names:
                table.setdefault(name, set()).add(path)
        return table

    def referrers(self) -> dict[str, set[str]]:
        table: dict[str, set[str]] = {}
        for path, names in self.path_refs.items():
            for name in names:
                table.setdefault(name, set()).add(path)
        return table

    def candidate_importers(self) -> dict[str, set[str]]:
        table: dict[str, set[str]] = {}
        for path, names in self.path_candidates.items():
            for name in names:
                table.setdefault(name, set()).add(path)
        return table

    def edges_from(self, path: str) -> tuple[Edge, ...]:
        return tuple(sorted(edge for edge in self.edges if edge.src_path == path))

    def edges_to(self, path: str) -> tuple[Edge, ...]:
        return tuple(sorted(edge for edge in self.edges if edge.dst_path == path))

    def to_json(self) -> dict[str, Any]:
        return {
            "at_view_revision": self.at_view_revision,
            "snapshot": dict(self.snapshot),
            "path_modules": {path: sorted(values) for path, values in self.path_modules.items()},
            "path_defines": {path: sorted(values) for path, values in self.path_defines.items()},
            "path_refs": {path: sorted(values) for path, values in self.path_refs.items()},
            "path_candidates": {path: sorted(values) for path, values in self.path_candidates.items()},
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any], edges: frozenset[Edge]) -> LinkGraph:
        def _sets(key: str) -> dict[str, frozenset[str]]:
            source = raw.get(key, {})
            return {str(path): frozenset(str(value) for value in values) for path, values in source.items()}

        return cls(
            at_view_revision=int(raw.get("at_view_revision", 0)),
            edges=edges,
            snapshot={str(path): str(digest) for path, digest in raw.get("snapshot", {}).items()},
            path_modules=_sets("path_modules"),
            path_defines=_sets("path_defines"),
            path_refs=_sets("path_refs"),
            path_candidates=_sets("path_candidates"),
        )


def _index_tables(
    facts: Mapping[str, FileFacts],
) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    path_modules: dict[str, frozenset[str]] = {}
    path_defines: dict[str, frozenset[str]] = {}
    path_refs: dict[str, frozenset[str]] = {}
    for path, item in facts.items():
        path_modules[path] = frozenset(module_names(path, item.language))
        path_defines[path] = frozenset(definition.name for definition in item.definitions)
        path_refs[path] = frozenset(reference.name for reference in item.references)
    return path_modules, path_defines, path_refs


def _unambiguous_modules(path_modules: Mapping[str, frozenset[str]]) -> dict[str, str]:
    claims: dict[str, list[str]] = {}
    for path, modules in path_modules.items():
        for module in modules:
            claims.setdefault(module, []).append(path)
    # A module claimed by two files cannot be resolved without guessing, and a
    # guess here is exactly the wrong-edge failure the plan blocks releases on.
    return {module: paths[0] for module, paths in claims.items() if len(paths) == 1}


def _definer_table(path_defines: Mapping[str, frozenset[str]]) -> dict[str, frozenset[str]]:
    table: dict[str, set[str]] = {}
    for path, names in path_defines.items():
        for name in names:
            table.setdefault(name, set()).add(path)
    return {name: frozenset(paths) for name, paths in table.items()}


def build_graph(facts: Mapping[str, FileFacts], *, at_view_revision: int) -> LinkGraph:
    """Resolve every file in the view from scratch. The oracle's reference."""
    path_modules, path_defines, path_refs = _index_tables(facts)
    module_index = _unambiguous_modules(path_modules)
    definers = _definer_table(path_defines)

    edges: set[Edge] = set()
    path_candidates: dict[str, frozenset[str]] = {}
    for path, item in facts.items():
        resolved = _resolve_one(item, module_index=module_index, definers=definers)
        edges.update(resolved.edges)
        path_candidates[path] = resolved.candidates

    return LinkGraph(
        at_view_revision=at_view_revision,
        edges=frozenset(edges),
        snapshot={path: item.digest for path, item in facts.items()},
        path_modules=path_modules,
        path_defines=path_defines,
        path_refs=path_refs,
        path_candidates=path_candidates,
    )


def changed_paths(previous: Mapping[str, str], current: Mapping[str, str]) -> frozenset[str]:
    """Paths added, removed, or whose content digest moved."""
    changed = {path for path, digest in current.items() if previous.get(path) != digest}
    changed.update(path for path in previous if path not in current)
    return frozenset(changed)


def update_graph(
    previous: LinkGraph,
    *,
    snapshot: Mapping[str, str],
    load_facts: Callable[[str], FileFacts | None],
    at_view_revision: int,
    full_rebuild_ratio: float = DEFAULT_FULL_REBUILD_RATIO,
) -> tuple[LinkGraph | None, LinkBuildReport]:
    """Advance ``previous`` to the file set ``snapshot`` describes.

    ``snapshot`` is ``path -> content digest``, which a view store can produce
    from its membership rows without touching Layer 1. ``load_facts`` is called
    **only for paths in the dirty closure**: that is what makes this cheaper
    than a rebuild, because loading a file's facts is the expensive part, not
    diffing a path list.

    Returns ``(None, report)`` when the change set is past the distance
    threshold. The caller then calls :func:`build_graph` itself, so the
    decision to pay for *every* file's facts is always made where the cost is
    visible rather than hidden inside this function.
    """
    current_snapshot = dict(snapshot)
    changed = changed_paths(previous.snapshot, current_snapshot)
    view_paths = len(current_snapshot)

    if not changed:
        return (
            LinkGraph(
                at_view_revision=at_view_revision,
                edges=previous.edges,
                snapshot=previous.snapshot,
                path_modules=previous.path_modules,
                path_defines=previous.path_defines,
                path_refs=previous.path_refs,
                path_candidates=previous.path_candidates,
            ),
            LinkBuildReport(
                mode=LinkBuildMode.UNCHANGED,
                at_view_revision=at_view_revision,
                view_paths=view_paths,
                changed_paths=0,
                dirty_paths=0,
                resolved_paths=0,
                edges_before=len(previous.edges),
                edges_after=len(previous.edges),
            ),
        )

    if view_paths == 0 or len(changed) > max(1, int(full_rebuild_ratio * view_paths)):
        return (
            None,
            LinkBuildReport(
                mode=LinkBuildMode.FULL,
                at_view_revision=at_view_revision,
                view_paths=view_paths,
                changed_paths=len(changed),
                dirty_paths=view_paths,
                resolved_paths=view_paths,
                edges_before=len(previous.edges),
                edges_after=0,
                reason="change_ratio_past_threshold",
            ),
        )

    # -- maintain the index tables ------------------------------------- #
    path_modules = dict(previous.path_modules)
    path_defines = dict(previous.path_defines)
    path_refs = dict(previous.path_refs)
    path_candidates = dict(previous.path_candidates)

    facts: dict[str, FileFacts] = {}
    affected_symbols: set[str] = set()
    affected_modules: set[str] = set()
    for path in changed:
        before_defines = path_defines.pop(path, frozenset())
        before_modules = path_modules.pop(path, frozenset())
        path_refs.pop(path, None)
        path_candidates.pop(path, None)
        item = load_facts(path) if path in current_snapshot else None
        if item is None:
            # Removed, or its content is not available to this view. Either way
            # it stops contributing, and everything that named it is affected.
            current_snapshot.pop(path, None)
            affected_symbols |= before_defines
            affected_modules |= before_modules
            continue
        facts[path] = item
        after_defines = frozenset(definition.name for definition in item.definitions)
        after_modules = frozenset(module_names(path, item.language))
        path_defines[path] = after_defines
        path_modules[path] = after_modules
        path_refs[path] = frozenset(reference.name for reference in item.references)
        affected_symbols |= before_defines ^ after_defines
        affected_modules |= before_modules ^ after_modules

    # -- dirty closure through the two reverse indexes ------------------ #
    referrers = previous.referrers()
    candidate_importers = previous.candidate_importers()
    dirty: set[str] = {path for path in changed if path in current_snapshot}
    for symbol in affected_symbols:
        dirty.update(referrers.get(symbol, ()))
    for module in affected_modules:
        dirty.update(candidate_importers.get(module, ()))
    dirty &= set(current_snapshot)

    module_index = _unambiguous_modules(path_modules)
    definers = _definer_table(path_defines)

    edges = {edge for edge in previous.edges if edge.src_path in current_snapshot and edge.src_path not in dirty}
    for path in sorted(dirty):
        item = facts.get(path)
        if item is None:
            item = load_facts(path)
            if item is None:
                # A clean path whose content vanished under us. Drop it from the
                # view rather than resolving against facts we do not have.
                current_snapshot.pop(path, None)
                path_modules.pop(path, None)
                path_defines.pop(path, None)
                path_refs.pop(path, None)
                path_candidates.pop(path, None)
                continue
        resolved = _resolve_one(item, module_index=module_index, definers=definers)
        edges.update(resolved.edges)
        path_candidates[path] = resolved.candidates

    graph = LinkGraph(
        at_view_revision=at_view_revision,
        edges=frozenset(edges),
        snapshot=current_snapshot,
        path_modules=path_modules,
        path_defines=path_defines,
        path_refs=path_refs,
        path_candidates=path_candidates,
    )
    report = LinkBuildReport(
        mode=LinkBuildMode.INCREMENTAL,
        at_view_revision=at_view_revision,
        view_paths=view_paths,
        changed_paths=len(changed),
        dirty_paths=len(dirty),
        resolved_paths=len(dirty),
        edges_before=len(previous.edges),
        edges_after=len(graph.edges),
    )
    return graph, report


# --------------------------------------------------------------------------- #
# The differential oracle's vocabulary                                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Divergence:
    """How an incrementally-maintained edge set differs from a clean rebuild.

    The distinction is the whole point. A **missing** edge degrades recall: a
    query returns less than it could, and the agent can tell. An **extra** edge
    is a *false answer*: the index asserts a relationship that does not exist,
    and nothing downstream can detect it. The plan makes the second
    release-blocking, so it gets its own field rather than a severity number.
    """

    missing: tuple[Edge, ...]
    extra: tuple[Edge, ...]

    @property
    def fail_safe(self) -> tuple[Edge, ...]:
        """Edges a clean rebuild has that the incremental index lost."""
        return self.missing

    @property
    def fail_open(self) -> tuple[Edge, ...]:
        """Edges the incremental index invented. Always release-blocking."""
        return self.extra

    @property
    def clean(self) -> bool:
        return not self.missing and not self.extra

    def describe(self, limit: int = 5) -> str:
        parts: list[str] = []
        if self.extra:
            parts.append(f"FAIL-OPEN: {len(self.extra)} invented edge(s): {list(self.extra[:limit])}")
        if self.missing:
            parts.append(f"fail-safe: {len(self.missing)} lost edge(s): {list(self.missing[:limit])}")
        return " | ".join(parts) or "no divergence"


def classify_divergence(incremental: frozenset[Edge], rebuilt: frozenset[Edge]) -> Divergence:
    """Compare an incremental edge set against a clean rebuild of the same view."""
    return Divergence(
        missing=tuple(sorted(rebuilt - incremental)),
        extra=tuple(sorted(incremental - rebuilt)),
    )


def facts_from(path: str, digest: str, artifact: AnalysisArtifact) -> FileFacts:
    """Adapt a Layer-1 :class:`AnalysisArtifact` to Layer-3 input."""
    return FileFacts(
        path=path,
        digest=digest,
        language=artifact.language,
        definitions=artifact.definitions,
        exports=artifact.exports,
        imports=artifact.imports,
        references=artifact.references,
    )
