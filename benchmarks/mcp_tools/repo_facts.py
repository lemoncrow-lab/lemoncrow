from __future__ import annotations

import ast
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


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
class RepoFileFact:
    path: str
    line_count: int
    char_count: int
    symbols: tuple[str, ...]
    anchor_line: int
    anchor_text: str


@dataclass(frozen=True)
class CallRelationFact:
    caller: SymbolFact
    callee: SymbolFact


_GENERIC_SYMBOL_NAMES = {
    "get",
    "set",
    "ok",
    "stats",
    "parts",
    "values",
    "archive",
    "providers",
}
_IDENTIFIER_TOKEN_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z]|$)|[A-Z]?[a-z]+|\d+")


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
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        kind = "method" if self.class_stack else "function"
        qualified_name = ".".join((*self.class_stack, node.name))
        self.symbols.append((node.name, node.lineno, kind, qualified_name))
        self.generic_visit(node)


def benchmark_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def repo_python_files(repo_root: Path) -> list[Path]:
    src_root = repo_root / "src" / "atelier"
    roots = [src_root] if src_root.exists() else [repo_root]
    files: list[Path] = []
    for root in roots:
        files.extend(sorted(path for path in root.rglob("*.py") if path.is_file()))
    return files


def collect_symbol_facts(repo_root: Path) -> tuple[list[SymbolFact], list[FileOutlineFact]]:
    symbol_facts: list[SymbolFact] = []
    outline_facts: list[FileOutlineFact] = []
    for path in repo_python_files(repo_root):
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


def collect_repo_file_facts(repo_root: Path) -> list[RepoFileFact]:
    symbol_facts, _ = collect_symbol_facts(repo_root)
    symbols_by_path: dict[str, list[SymbolFact]] = defaultdict(list)
    for symbol in symbol_facts:
        symbols_by_path[symbol.path].append(symbol)

    file_facts: list[RepoFileFact] = []
    for path in repo_python_files(repo_root):
        relative_path = path.relative_to(repo_root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        lines = text.splitlines()
        if not lines:
            continue
        anchor_symbol = symbols_by_path.get(relative_path, [None])[0]
        anchor_line = anchor_symbol.line if anchor_symbol is not None else 1
        anchor_text = lines[max(anchor_line - 1, 0)].strip() if lines else ""
        file_facts.append(
            RepoFileFact(
                path=relative_path,
                line_count=len(lines),
                char_count=len(text),
                symbols=tuple(symbol.name for symbol in symbols_by_path.get(relative_path, [])),
                anchor_line=anchor_line,
                anchor_text=anchor_text,
            )
        )
    file_facts.sort(key=lambda item: item.path)
    return file_facts


def unique_symbol_facts(symbol_facts: Iterable[SymbolFact]) -> list[SymbolFact]:
    counts = Counter(symbol.name for symbol in symbol_facts)
    return [symbol for symbol in symbol_facts if counts[symbol.name] == 1]


def unique_substring_queries(
    repo_root: Path,
    symbol_facts: Iterable[SymbolFact],
    *,
    max_file_hits: int = 6,
) -> list[tuple[str, SymbolFact]]:
    token_to_symbols: dict[str, list[tuple[str, SymbolFact]]] = defaultdict(list)
    for symbol in symbol_facts:
        seen_norms: set[str] = set()
        tokens = _identifier_tokens(symbol.name)
        for token in tokens:
            token_norm = token.lower()
            if token_norm == symbol.name.lower() or token_norm in seen_norms:
                continue
            seen_norms.add(token_norm)
            token_to_symbols[token_norm].append((token, symbol))

    file_texts = {
        path.relative_to(repo_root).as_posix(): path.read_text(encoding="utf-8", errors="replace")
        for path in repo_python_files(repo_root)
    }
    pairs: list[tuple[str, SymbolFact]] = []
    for _token_norm, entries in sorted(token_to_symbols.items()):
        symbols = {symbol for _token, symbol in entries}
        if len(symbols) != 1:
            continue
        symbol = next(iter(symbols))
        token = max((candidate for candidate, _symbol in entries), key=len)
        file_hits = [relative_path for relative_path, text in file_texts.items() if token in text]
        if symbol.path not in file_hits or len(file_hits) == 0 or len(file_hits) > max_file_hits:
            continue
        pairs.append((token, symbol))
    pairs.sort(key=lambda item: (item[1].path, item[1].line, item[0].lower()))
    return pairs


def stable_symbol_facts(
    symbol_facts: Iterable[SymbolFact], *, require_dotted: bool = False, min_name_length: int = 5
) -> list[SymbolFact]:
    stable: list[SymbolFact] = []
    for symbol in symbol_facts:
        if len(symbol.name) < min_name_length:
            continue
        if symbol.name.lower() in _GENERIC_SYMBOL_NAMES:
            continue
        if require_dotted and "." not in symbol.qualified_name:
            continue
        stable.append(symbol)
    stable.sort(key=lambda item: (item.path, item.line, item.qualified_name))
    return stable


def benchmark_query_text(symbol: SymbolFact) -> str:
    return symbol.qualified_name if "." in symbol.qualified_name else symbol.name


def symbols_with_text_references(
    repo_root: Path,
    symbols: Iterable[SymbolFact],
    *,
    minimum_mentions: int = 2,
) -> list[SymbolFact]:
    files = [path.read_text(encoding="utf-8", errors="replace") for path in repo_python_files(repo_root)]
    referenced: list[SymbolFact] = []
    for symbol in symbols:
        mentions = sum(text.count(symbol.name) for text in files)
        if mentions >= minimum_mentions:
            referenced.append(symbol)
    referenced.sort(key=lambda item: (item.path, item.line, item.name))
    return referenced


def collect_call_relation_facts(repo_root: Path) -> list[CallRelationFact]:
    symbol_facts, _ = collect_symbol_facts(repo_root)
    unique_symbols = unique_symbol_facts(symbol_facts)
    symbols_by_name = {symbol.name: symbol for symbol in unique_symbols}
    relations: set[tuple[SymbolFact, SymbolFact]] = set()

    class _CallCollector(ast.NodeVisitor):
        def __init__(self, relative_path: str) -> None:
            self.relative_path = relative_path
            self.class_stack: list[str] = []
            self.function_stack: list[SymbolFact | None] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.class_stack.append(node.name)
            self.generic_visit(node)
            self.class_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_callable(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_callable(node)

        def _visit_callable(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            caller = symbols_by_name.get(node.name)
            if caller is not None and caller.path != self.relative_path:
                caller = None
            self.function_stack.append(caller)
            self.generic_visit(node)
            self.function_stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            caller = self.function_stack[-1] if self.function_stack else None
            if caller is not None:
                callee_name = _call_name(node.func)
                callee = symbols_by_name.get(callee_name) if callee_name else None
                if callee is not None:
                    relations.add((caller, callee))
            self.generic_visit(node)

    for path in repo_python_files(repo_root):
        relative_path = path.relative_to(repo_root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        _CallCollector(relative_path).visit(tree)

    facts = [CallRelationFact(caller=caller, callee=callee) for caller, callee in relations]
    facts.sort(
        key=lambda item: (
            item.caller.path,
            item.caller.line,
            item.caller.name,
            item.callee.path,
            item.callee.line,
            item.callee.name,
        )
    )
    return facts


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _identifier_tokens(name: str) -> list[str]:
    if "_" in name:
        return [part for part in name.split("_") if len(part) >= 5]
    return [match.group(0) for match in _IDENTIFIER_TOKEN_RE.finditer(name) if len(match.group(0)) >= 5]
