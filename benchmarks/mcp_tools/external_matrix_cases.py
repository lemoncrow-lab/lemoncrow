from __future__ import annotations

import ast
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

DEFAULT_CASE_QUOTAS: dict[str, int] = {
    "exact_symbol": 100,
    "exact_search": 100,
    "substring_search": 100,
    "file_outline": 100,
    "references": 100,
    "callers": 100,
    "callees": 100,
    "fuzzy_symbol": 100,
    "structural_search": 100,
    "nohit_search": 100,
    "explore": 100,
    "explore_skeleton": 100,
}

# Families whose corpus is opportunistic: generate up to the quota, but do not
# fail if the repository yields fewer well-posed cases.
_BEST_EFFORT_FAMILIES = frozenset({"nohit_search", "structural_search", "explore", "explore_skeleton"})

_SIBLING_KINDS = frozenset({"class", "struct", "interface", "trait", "protocol", "enum", "method", "function"})
_SIBLING_STOPWORDS = frozenset(
    {
        "make",
        "handle",
        "data",
        "base",
        "util",
        "utils",
        "test",
        "tests",
        "impl",
        "main",
        "value",
        "values",
        "name",
        "names",
        "type",
        "types",
        "node",
        "item",
        "items",
        "list",
        "dict",
        "async",
        "await",
        "none",
        "true",
        "false",
        "self",
        "func",
        "call",
        "args",
        "kwargs",
        "init",
        "build",
        "create",
        "update",
        "delete",
        "result",
        "config",
        "client",
        "server",
        "model",
        "models",
        "error",
        "errors",
    }
)
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _affix_tokens(name: str) -> list[str]:
    """CamelCase/snake_case affix tokens (>=4 chars, non-generic) of a symbol name."""
    base = (name or "").split(".")[-1]
    raw: list[str] = []
    for snake in base.split("_"):
        if snake:
            raw.extend(_CAMEL_SPLIT_RE.split(snake))
    tokens = [token.lower() for token in raw if token]
    tokens = [token for token in tokens if len(token) >= 4 and token not in _SIBLING_STOPWORDS]
    if not tokens:
        return []
    affixes: list[str] = []
    if tokens[-1] not in affixes:
        affixes.append(tokens[-1])
    if tokens[0] not in affixes:
        affixes.append(tokens[0])
    return affixes


def _sibling_family_facts(symbols: Iterable[SymbolFact]) -> list[tuple[str, tuple[str, ...]]]:
    """Mine >=3-member same-kind/shared-affix symbol families (skeletonization targets)."""
    groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    for symbol in symbols:
        kind = (symbol.kind or "").lower()
        if kind not in _SIBLING_KINDS:
            continue
        for affix in _affix_tokens(symbol.name):
            groups[(kind, affix)].add(symbol.path)
    out: list[tuple[str, tuple[str, ...]]] = []
    seen: set[str] = set()
    for (_kind, affix), paths in sorted(groups.items()):
        if len(paths) >= 3 and affix not in seen:
            seen.add(affix)
            out.append((affix, tuple(sorted(paths))))
    return out


@dataclass(frozen=True)
class SymbolFact:
    name: str
    qualified_name: str
    path: str
    line: int
    kind: str


@dataclass(frozen=True)
class FileOutlineFact:
    path: str
    symbols: tuple[str, ...]


@dataclass(frozen=True)
class ExternalBenchCase:
    case_id: str
    family: str
    query: str
    path: str | None = None
    symbol_name: str | None = None
    expected_paths: tuple[str, ...] = ()
    expected_names: tuple[str, ...] = ()
    metadata: dict[str, str] | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["expected_paths"] = list(self.expected_paths)
        payload["expected_names"] = list(self.expected_names)
        return payload


class _SymbolCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.class_stack: list[str] = []
        self.symbols: list[tuple[str, int, str, str]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified_name = ".".join((*self.class_stack, node.name))
        self.symbols.append((node.name, node.lineno, "class", qualified_name))
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        kind = "method" if self.class_stack else "function"
        qualified_name = ".".join((*self.class_stack, node.name))
        self.symbols.append((node.name, node.lineno, kind, qualified_name))
        # Do NOT descend into the function body: function-local closures (e.g.
        # a `consider_rows` defined inside another function) are not navigable
        # symbols and code-intel providers do not reliably index them, so they
        # make unfair retrieval ground truth. Methods are still captured because
        # visit_ClassDef descends into class bodies.

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        kind = "method" if self.class_stack else "function"
        qualified_name = ".".join((*self.class_stack, node.name))
        self.symbols.append((node.name, node.lineno, kind, qualified_name))


def _repo_python_files(repo_root: Path) -> list[Path]:
    src_root = repo_root / "src" / "lemoncrow"
    roots = [src_root] if src_root.exists() else [repo_root]
    files: list[Path] = []
    for root in roots:
        files.extend(sorted(path for path in root.rglob("*.py") if path.is_file()))
    return files


def _collect_symbol_facts(repo_root: Path) -> tuple[list[SymbolFact], list[FileOutlineFact]]:
    symbol_facts: list[SymbolFact] = []
    outline_facts: list[FileOutlineFact] = []
    for path in _repo_python_files(repo_root):
        relative_path = path.relative_to(repo_root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        collector = _SymbolCollector()
        collector.visit(tree)
        ordered_symbols = [
            SymbolFact(
                name=name,
                qualified_name=qualified_name,
                path=relative_path,
                line=line,
                kind=kind,
            )
            for name, line, kind, qualified_name in collector.symbols
        ]
        symbol_facts.extend(ordered_symbols)
        if ordered_symbols:
            outline_facts.append(
                FileOutlineFact(
                    path=relative_path,
                    symbols=tuple(symbol.name for symbol in ordered_symbols[:6]),
                )
            )
    symbol_facts.sort(key=lambda item: (item.path, item.line, item.qualified_name))
    outline_facts.sort(key=lambda item: (item.path, tuple(item.symbols)))
    return symbol_facts, outline_facts


def _unique_symbol_facts(symbol_facts: Iterable[SymbolFact]) -> list[SymbolFact]:
    counts = Counter(symbol.name for symbol in symbol_facts)
    return [symbol for symbol in symbol_facts if counts[symbol.name] == 1]


def _unique_substring_queries(
    symbol_facts: Iterable[SymbolFact],
    *,
    all_symbol_names: Iterable[str],
) -> list[tuple[str, SymbolFact]]:
    """(token, symbol) pairs where *token* (>=5 chars) identifies exactly one symbol.

    Uniqueness is enforced under true substring matching -- the semantics a
    substring search actually uses -- not merely underscore-token splitting. A
    token like ``adapter`` splits out of only ``_deleted_history_adapter`` yet is a
    substring of ~19 camelCase ``*Adapter`` names; querying it would match all of
    them, so the expected symbol cannot be uniquely retrieved and the token is
    rejected. This keeps every substring case well-posed: the query maps to a
    single expected symbol, so any competent provider can score it.
    """
    names = [name.lower() for name in all_symbol_names]
    token_to_symbols: dict[str, list[SymbolFact]] = defaultdict(list)
    for symbol in symbol_facts:
        tokens = [part for part in symbol.name.split("_") if len(part) >= 5]
        for token in tokens:
            token_to_symbols[token.lower()].append(symbol)
    pairs: list[tuple[str, SymbolFact]] = []
    for token, symbols in sorted(token_to_symbols.items()):
        if len(symbols) != 1:
            continue
        symbol = symbols[0]
        if token == symbol.name.lower():
            continue
        # Reject tokens that are a substring of any OTHER symbol name: the query
        # would match multiple symbols and the expected answer is not unique.
        if sum(1 for name in names if token in name) != 1:
            continue
        pairs.append((token, symbol))
    return pairs


def _collect_reference_facts(
    repo_root: Path,
    unique_symbols: list[SymbolFact],
) -> list[tuple[SymbolFact, tuple[str, ...]]]:
    """For each uniquely-named symbol, the other files that reference its name.

    Ground truth is built by neutral AST identifier analysis (every ``Name``/
    ``Attribute`` occurrence), NOT any provider's index -- otherwise the
    provider under test would define its own answer key. Because the symbols are
    globally name-unique, an identifier occurrence is a sound proxy for a
    reference. Symbols with no external references are skipped (nothing to find).
    """
    name_to_files: dict[str, set[str]] = defaultdict(set)
    for path in _repo_python_files(repo_root):
        relative_path = path.relative_to(repo_root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        for name in names:
            name_to_files[name].add(relative_path)
    # Cap: if a symbol's name appears in too many files it is a common
    # English/Python word (e.g. "available", "run", "result") that happens to
    # be defined exactly once.  Including it makes reference cases unfair
    # (providers must do an expensive cross-repo disambiguation scan) and
    # produces inflated ground-truth sets.  8 is a generous upper bound that
    # keeps well-scoped symbols while filtering out noise.
    _MAX_REFERENCE_FILES = 8
    facts: list[tuple[SymbolFact, tuple[str, ...]]] = []
    for symbol in unique_symbols:
        referencing = tuple(sorted(name_to_files.get(symbol.name, set()) - {symbol.path}))
        if referencing and len(referencing) <= _MAX_REFERENCE_FILES:
            facts.append((symbol, referencing))
    return facts


class _CallEdgeCollector(ast.NodeVisitor):
    """Collect (enclosing-function-qualname, callee-name) edges from one module."""

    def __init__(self) -> None:
        self.name_stack: list[str] = []
        self.func_qual_stack: list[str] = []
        self.edges: list[tuple[str, str]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.name_stack.append(node.name)
        self.generic_visit(node)
        self.name_stack.pop()

    def _enter_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qualified_name = ".".join((*self.name_stack, node.name))
        self.name_stack.append(node.name)
        self.func_qual_stack.append(qualified_name)
        self.generic_visit(node)
        self.func_qual_stack.pop()
        self.name_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter_func(node)

    def visit_Call(self, node: ast.Call) -> None:
        target = node.func
        callee = (
            target.id if isinstance(target, ast.Name) else target.attr if isinstance(target, ast.Attribute) else None
        )
        if callee and self.func_qual_stack:
            self.edges.append((self.func_qual_stack[-1], callee))
        self.generic_visit(node)


def _collect_call_facts(
    repo_root: Path,
    unique_symbols: list[SymbolFact],
) -> tuple[list[tuple[SymbolFact, tuple[str, ...]]], list[tuple[SymbolFact, tuple[str, ...], tuple[str, ...]]]]:
    """Neutral AST call graph: returns (callers_facts, callees_facts).

    callers_facts: (symbol, files that call it) for uniquely-named functions/methods
    with at least one external caller. callees_facts: (symbol, callee def files,
    callee names) for functions that call other uniquely-named repo symbols.
    Built from ``ast.Call`` edges, NOT a provider index, so it is a fair answer key.
    """
    name_to_symbol = {symbol.name: symbol for symbol in unique_symbols}
    caller_files_by_callee: dict[str, set[str]] = defaultdict(set)
    callee_names_by_caller: dict[str, set[str]] = defaultdict(set)
    for path in _repo_python_files(repo_root):
        relative_path = path.relative_to(repo_root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        collector = _CallEdgeCollector()
        collector.visit(tree)
        for caller_qualified_name, callee_name in collector.edges:
            caller_files_by_callee[callee_name].add(relative_path)
            callee_names_by_caller[caller_qualified_name].add(callee_name)
    callable_kinds = {"function", "method"}
    # Names that appear as callees in more than this many files are builtins or
    # stdlib methods (e.g. 'append', 'get', 'read_text'). Their expected_paths
    # become unreliable because AST-only matching can't distinguish
    # list.append() from MemoryAuditLog.append() -- any code-intel tool that
    # correctly resolves to the specific method will return different callers.
    _MAX_CALLEE_POPULARITY = 50
    callers_facts: list[tuple[SymbolFact, tuple[str, ...]]] = []
    callees_facts: list[tuple[SymbolFact, tuple[str, ...], tuple[str, ...]]] = []
    for symbol in unique_symbols:
        if symbol.kind not in callable_kinds:
            continue
        all_caller_files = caller_files_by_callee.get(symbol.name, set())
        if len(all_caller_files) > _MAX_CALLEE_POPULARITY:
            # Too common a name -- ground truth is unreliable; skip this case.
            continue
        caller_files = tuple(sorted(all_caller_files - {symbol.path}))
        if caller_files:
            callers_facts.append((symbol, caller_files))
        callee_names = tuple(
            sorted(
                name
                for name in callee_names_by_caller.get(symbol.qualified_name, set())
                if name in name_to_symbol and name != symbol.name
            )
        )
        if callee_names:
            callee_files = tuple(dict.fromkeys(name_to_symbol[name].path for name in callee_names))
            callees_facts.append((symbol, callee_files, callee_names))
    return callers_facts, callees_facts


def _fuzzy_symbol_queries(
    unique_symbols: list[SymbolFact],
    *,
    all_symbol_names: Iterable[str],
) -> list[tuple[str, SymbolFact]]:
    """(typo, symbol) pairs: a single adjacent-character transposition of the name.

    The typo must not collide with any real symbol name, so it stays a well-posed
    fuzzy query (one intended target). Tests typo-tolerant symbol lookup.
    """
    existing = {name.lower() for name in all_symbol_names}
    pairs: list[tuple[str, SymbolFact]] = []
    for symbol in unique_symbols:
        name = symbol.name
        if len(name) < 6:
            continue
        pivot = len(name) // 2 - 1
        if name[pivot] == name[pivot + 1]:
            continue
        typo = name[:pivot] + name[pivot + 1] + name[pivot] + name[pivot + 2 :]
        lowered = typo.lower()
        if lowered == name.lower() or lowered in existing:
            continue
        pairs.append((typo, symbol))
    return pairs


def _decorator_name(node: ast.expr) -> str | None:
    """Bare decorator name for ``@foo`` / ``@foo(...)``; skip attribute decorators."""
    target = node.func if isinstance(node, ast.Call) else node
    return target.id if isinstance(target, ast.Name) else None


def _collect_structural_facts(repo_root: Path) -> list[tuple[str, tuple[str, ...]]]:
    """(pattern, files matching it) for structural-search cases.

    Covers two pattern families, both valid in LemonCrow's native pattern engine
    and in ast-grep:

    1. Decorator patterns — ``@foo`` (bare) or ``@foo($$$)`` (called with args).
       Ground truth: files containing any definition decorated with ``foo``.
       Decorators used in too many files are dropped to keep the answer key
       checkable (max 6 files).

    2. Function-definition patterns — ``def func_name($$$):``.
       Ground truth: files containing a public function/method with that name.
       Functions defined in too many files are also dropped (max 6 files).

    Decorator facts come first so the original 15 cases stay stable; function
    facts fill the remaining quota slots up to 100.

    """
    # --- decorator patterns ---
    decorator_to_files: dict[str, set[str]] = defaultdict(set)
    decorator_has_args: dict[str, bool] = {}
    for path in _repo_python_files(repo_root):
        relative_path = path.relative_to(repo_root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                for decorator in node.decorator_list:
                    name = _decorator_name(decorator)
                    if name and len(name) >= 4:
                        decorator_to_files[name].add(relative_path)
                        # Track whether this decorator is always-called vs bare.
                        # Once seen as a call, mark it; never reset to False.
                        if name not in decorator_has_args or not decorator_has_args[name]:
                            decorator_has_args[name] = isinstance(decorator, ast.Call)
    facts: list[tuple[str, tuple[str, ...]]] = []
    for name, files in sorted(decorator_to_files.items()):
        if 1 <= len(files) <= 6:
            # Use @name($$$) for decorators always invoked with arguments so
            # that ast-grep's structural matcher finds them too (bare @name only
            # matches the no-parens form in ast-grep).
            pattern = f"@{name}($$$)" if decorator_has_args.get(name) else f"@{name}"
            facts.append((pattern, tuple(sorted(files))))

    # --- function-definition patterns ---
    # ``def func_name($$$):`` works in both LemonCrow (mode='def') and ast-grep.
    # Only public names (no leading underscore) of length >= 6 that appear in
    # exactly 1-6 files are included, sorted by (file_count, name) for
    # stability and diversity.
    func_to_files: dict[str, set[str]] = defaultdict(set)
    for path in _repo_python_files(repo_root):
        relative_path = path.relative_to(repo_root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                n = node.name
                if not n.startswith("_") and len(n) >= 6:
                    func_to_files[n].add(relative_path)
    func_facts: list[tuple[str, tuple[str, ...]]] = []
    for name, files in func_to_files.items():
        if 1 <= len(files) <= 6:
            func_facts.append((f"def {name}($$$):", tuple(sorted(files))))
    # Sort by file count ascending then name for determinism.
    func_facts.sort(key=lambda t: (len(t[1]), t[0]))
    facts.extend(func_facts)
    return facts


def _make_nohit_query(index: int) -> str:
    return f"lemoncrow_missing_symbol_{index:04d}_never_exists"


def generate_case_manifest(
    repo_root: Path,
    *,
    case_quotas: Mapping[str, int] = DEFAULT_CASE_QUOTAS,
) -> list[ExternalBenchCase]:
    symbol_facts, outline_facts = _collect_symbol_facts(repo_root)
    unique_symbols = _unique_symbol_facts(symbol_facts)
    substring_pairs = _unique_substring_queries(
        unique_symbols,
        all_symbol_names=[symbol.name for symbol in symbol_facts],
    )
    reference_facts = _collect_reference_facts(repo_root, unique_symbols)
    callers_facts, callees_facts = _collect_call_facts(repo_root, unique_symbols)
    fuzzy_pairs = _fuzzy_symbol_queries(unique_symbols, all_symbol_names=[symbol.name for symbol in symbol_facts])
    structural_facts = _collect_structural_facts(repo_root)

    required = {
        "exact_symbol": len(unique_symbols),
        "exact_search": len(unique_symbols),
        "substring_search": len(substring_pairs),
        "file_outline": len(outline_facts),
        "references": len(reference_facts),
        "callers": len(callers_facts),
        "callees": len(callees_facts),
        "fuzzy_symbol": len(fuzzy_pairs),
    }
    for family, quota in case_quotas.items():
        if family in _BEST_EFFORT_FAMILIES:
            continue
        if required.get(family, quota) < quota:
            raise ValueError(
                f"not enough repository facts to satisfy {family}: need {quota}, have {required.get(family, 0)}"
            )

    cases: list[ExternalBenchCase] = []

    for index, symbol in enumerate(unique_symbols[: case_quotas["exact_symbol"]], start=1):
        cases.append(
            ExternalBenchCase(
                case_id=f"exact-symbol-{index:04d}",
                family="exact_symbol",
                query=symbol.name,
                path=symbol.path,
                symbol_name=symbol.name,
                expected_paths=(symbol.path,),
                expected_names=(symbol.name,),
                metadata={"qualified_name": symbol.qualified_name, "kind": symbol.kind},
            )
        )

    len(cases)
    for index, symbol in enumerate(unique_symbols[: case_quotas["exact_search"]], start=1):
        cases.append(
            ExternalBenchCase(
                case_id=f"exact-search-{index:04d}",
                family="exact_search",
                query=symbol.name,
                path=symbol.path,
                symbol_name=symbol.name,
                expected_paths=(symbol.path,),
                expected_names=(symbol.name,),
                metadata={"qualified_name": symbol.qualified_name, "kind": symbol.kind},
            )
        )

    for index, (query, symbol) in enumerate(
        substring_pairs[: case_quotas["substring_search"]],
        start=1,
    ):
        cases.append(
            ExternalBenchCase(
                case_id=f"substring-search-{index:04d}",
                family="substring_search",
                query=query,
                path=symbol.path,
                symbol_name=symbol.name,
                expected_paths=(symbol.path,),
                expected_names=(symbol.name,),
                metadata={"qualified_name": symbol.qualified_name, "kind": symbol.kind},
            )
        )

    outline_candidates = [fact for fact in outline_facts if len(fact.symbols) >= 3]
    for index, outline in enumerate(outline_candidates[: case_quotas["file_outline"]], start=1):
        cases.append(
            ExternalBenchCase(
                case_id=f"file-outline-{index:04d}",
                family="file_outline",
                query=outline.path,
                path=outline.path,
                expected_paths=(outline.path,),
                expected_names=outline.symbols[:3],
            )
        )

    for index, (symbol, referencing) in enumerate(
        reference_facts[: case_quotas.get("references", 0)],
        start=1,
    ):
        cases.append(
            ExternalBenchCase(
                case_id=f"references-{index:04d}",
                family="references",
                query=symbol.name,
                path=symbol.path,
                symbol_name=symbol.name,
                expected_paths=referencing,
                expected_names=(symbol.name,),
                metadata={
                    "qualified_name": symbol.qualified_name,
                    "kind": symbol.kind,
                    "def_line": str(symbol.line),
                },
            )
        )

    for index, (symbol, caller_files) in enumerate(
        callers_facts[: case_quotas.get("callers", 0)],
        start=1,
    ):
        cases.append(
            ExternalBenchCase(
                case_id=f"callers-{index:04d}",
                family="callers",
                query=symbol.name,
                path=symbol.path,
                symbol_name=symbol.name,
                expected_paths=caller_files,
                expected_names=(symbol.name,),
                metadata={"qualified_name": symbol.qualified_name, "kind": symbol.kind},
            )
        )

    for index, (symbol, callee_files, callee_names) in enumerate(
        callees_facts[: case_quotas.get("callees", 0)],
        start=1,
    ):
        cases.append(
            ExternalBenchCase(
                case_id=f"callees-{index:04d}",
                family="callees",
                query=symbol.name,
                path=symbol.path,
                symbol_name=symbol.name,
                expected_paths=callee_files,
                expected_names=callee_names,
                metadata={"qualified_name": symbol.qualified_name, "kind": symbol.kind},
            )
        )

    for index, (typo, symbol) in enumerate(
        fuzzy_pairs[: case_quotas.get("fuzzy_symbol", 0)],
        start=1,
    ):
        cases.append(
            ExternalBenchCase(
                case_id=f"fuzzy-symbol-{index:04d}",
                family="fuzzy_symbol",
                query=typo,
                path=symbol.path,
                symbol_name=symbol.name,
                expected_paths=(symbol.path,),
                expected_names=(symbol.name,),
                metadata={"qualified_name": symbol.qualified_name, "kind": symbol.kind},
            )
        )

    for index, (pattern, matching_files) in enumerate(
        structural_facts[: case_quotas.get("structural_search", 0)],
        start=1,
    ):
        cases.append(
            ExternalBenchCase(
                case_id=f"structural-search-{index:04d}",
                family="structural_search",
                query=pattern,
                expected_paths=matching_files,
                expected_names=(pattern,),
            )
        )

    # Both explore families share the SAME sibling-family queries so the rows form a
    # controlled A/B: explore renders full bodies, explore_skeleton collapses
    # redundant siblings to signatures at equal coverage.
    sibling_facts = _sibling_family_facts(unique_symbols)
    explore_appended = 0
    for index, (affix, member_files) in enumerate(sibling_facts[: case_quotas.get("explore", 0)], start=1):
        cases.append(
            ExternalBenchCase(
                case_id=f"explore-{index:04d}",
                family="explore",
                query=affix,
                expected_paths=member_files,
                expected_names=(affix,),
                metadata={"family_size": str(len(member_files))},
            )
        )
        explore_appended += 1

    skeleton_appended = 0
    for index, (affix, member_files) in enumerate(sibling_facts[: case_quotas.get("explore_skeleton", 0)], start=1):
        cases.append(
            ExternalBenchCase(
                case_id=f"explore-skeleton-{index:04d}",
                family="explore_skeleton",
                query=affix,
                expected_paths=member_files,
                expected_names=(affix,),
                metadata={"family_size": str(len(member_files))},
            )
        )
        skeleton_appended += 1

    for index in range(1, case_quotas["nohit_search"] + 1):
        query = _make_nohit_query(index)
        cases.append(
            ExternalBenchCase(
                case_id=f"nohit-search-{index:04d}",
                family="nohit_search",
                query=query,
                expected_paths=(),
                expected_names=(),
            )
        )

    # Strict families must hit their quota exactly; best-effort families
    # (no-hit/structural) contribute however many well-posed cases the
    # repository yields, up to the quota.
    available = {
        "structural_search": len(structural_facts),
        "explore": explore_appended,
        "explore_skeleton": skeleton_appended,
    }
    expected_total = sum(
        min(quota, available[family]) if family in available else quota for family, quota in case_quotas.items()
    )
    if len(cases) != expected_total:
        raise AssertionError(f"expected {expected_total} cases, got {len(cases)}")
    return cases


def write_case_manifest(path: Path, repo_root: Path) -> list[ExternalBenchCase]:
    cases = generate_case_manifest(repo_root)
    payload = {
        "repo_root": str(repo_root.resolve()),
        "case_quotas": DEFAULT_CASE_QUOTAS,
        "cases": [case.to_dict() for case in cases],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return cases


def load_case_manifest(path: Path) -> list[ExternalBenchCase]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload["cases"]
    assert isinstance(cases, list)
    return [
        ExternalBenchCase(
            case_id=item["case_id"],
            family=item["family"],
            query=item["query"],
            path=item.get("path"),
            symbol_name=item.get("symbol_name"),
            expected_paths=tuple(item.get("expected_paths", [])),
            expected_names=tuple(item.get("expected_names", [])),
            metadata=item.get("metadata"),
        )
        for item in cases
    ]
