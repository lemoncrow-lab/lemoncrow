"""Build a versioned, file-backed replacement for the mixed CodeBench gold.

The current five gold manifests contain cases whose recorded files are absent from
the repository revision pinned by the manifest. Those cases are impossible to
retrieve and therefore cap raw MRR below 1.0. This builder keeps every scorable
case, replaces each impossible case one-for-one, and writes a complete audit
receipt.

Important benchmark rules:

* Source manifests are read-only and remain the historical benchmark.
* Replacements are generated only from repository contents and symbol indexes.
* Retrieval output and model scores are never consulted while generating cases.
* Replacement counts preserve the evaluator's exact case identity
  ``(gold_kind, query, task_id, repository)``.
* Every emitted gold path is verified against the pinned workspace.
* Benchmark/training artefacts are excluded as replacement targets.

The output is a new benchmark version. It must not be used for training or model
selection after its first evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_MANIFESTS: tuple[tuple[str, str], ...] = (
    ("definition", "bench_pairs_def_gold.json"),
    ("content", "bench_pairs_content_gold.json"),
    ("semantic", "bench_pairs_semantic_gold.json"),
    ("swebench", "bench_pairs_swebench_gold.json"),
    ("sessions", "bench_pairs_sessions_gold.json"),
)

_OUTPUT_NAMES = {kind: source.replace("_gold.json", "_gold_filebacked_v2.json") for kind, source in _MANIFESTS}

_EXCLUDED_PATH_RE = re.compile(
    r"(?:^|/)(?:\.git|\.venv|node_modules|vendor|dist|build|coverage|__pycache__)(?:/|$)"
    r"|^benchmarks/"
    r"|^experiments/"
    r"|(?:\.min\.js|\.map|package-lock\.json|yarn\.lock)$",
    re.IGNORECASE,
)
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
_STRING_RE = re.compile(r"(?P<quote>['\"])(?P<value>[^'\"\n]{12,120})(?P=quote)")
_SPACE_RE = re.compile(r"\s+")
_COMMON_NAMES = {
    "self",
    "cls",
    "args",
    "kwargs",
    "value",
    "values",
    "result",
    "results",
    "data",
    "name",
    "path",
    "file",
    "text",
    "item",
    "items",
    "test",
    "main",
    "error",
    "index",
    "node",
    "config",
    "context",
}


@dataclass(frozen=True)
class GoldCase:
    kind: str
    query: str
    task_id: str
    repo: str
    gold_paths: tuple[str, ...]
    source_manifest: str

    @property
    def case_id(self) -> str:
        return f"{self.kind}:{self.repo}:{self.task_id}"


@dataclass(frozen=True)
class SymbolRow:
    symbol_id: str
    file_path: str
    symbol_name: str
    qualified_name: str
    kind: str
    signature: str
    doc_summary: str


@dataclass(frozen=True)
class Replacement:
    replaces_case_id: str
    kind: str
    repo: str
    query: str
    task_id: str
    gold_paths: tuple[str, ...]
    method: str
    source_file: str
    source_symbols: tuple[str, ...]


@dataclass
class RepositoryData:
    prefix: str
    workspace: Path
    db_path: Path
    symbols: list[SymbolRow]
    by_file: dict[str, list[SymbolRow]]
    name_files: dict[str, tuple[str, ...]]
    literals: list[tuple[str, tuple[str, ...], str]]


def _normalize(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def _stable_digest(*parts: object) -> str:
    payload = "\x00".join(str(part) for part in parts).encode("utf-8")
    return hashlib.blake2s(payload).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_allowed_path(path: str) -> bool:
    normalized = _normalize(path)
    return bool(normalized) and not _EXCLUDED_PATH_RE.search(normalized)


def _existing_gold_paths(workspace: Path, paths: Iterable[str]) -> tuple[str, ...]:
    """Return every recorded gold path that exists, including historical artefacts."""

    found: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        path = _normalize(str(raw))
        if path in seen:
            continue
        seen.add(path)
        if (workspace / path).is_file():
            found.append(path)
    return tuple(found)


def _existing_replacement_paths(workspace: Path, paths: Iterable[str]) -> tuple[str, ...]:
    """Return existing paths eligible to become newly generated benchmark gold."""

    return tuple(path for path in _existing_gold_paths(workspace, paths) if _is_allowed_path(path))


def _load_cases(source_dir: Path) -> tuple[list[GoldCase], dict[str, dict[str, Any]], dict[str, str]]:
    """Load cases using the same identity rule as the frozen evaluator."""

    cases: list[GoldCase] = []
    repositories: dict[str, dict[str, Any]] = {}
    source_hashes: dict[str, str] = {}
    seen: set[tuple[str, str, str, str]] = set()
    for expected_kind, filename in _MANIFESTS:
        path = source_dir / filename
        raw = json.loads(path.read_text(encoding="utf-8"))
        kind = str(raw.get("gold_kind") or expected_kind)
        if kind != expected_kind:
            raise ValueError(f"{path}: expected gold_kind={expected_kind!r}, got {kind!r}")
        source_hashes[filename] = _sha256(path)
        for prefix, metadata in (raw.get("repos") or {}).items():
            if isinstance(metadata, dict) and str(prefix) not in repositories:
                repositories[str(prefix)] = dict(metadata)
        true_map = raw.get("true_map") or {}
        for item in raw.get("pairs") or []:
            if not isinstance(item, list) or len(item) < 3:
                continue
            query, task_id, prefix = str(item[0]).strip(), str(item[1]), str(item[2])
            gold = true_map.get(task_id) or []
            if isinstance(gold, str):
                gold = [gold]
            gold_paths = tuple(_normalize(str(value)) for value in gold if str(value))
            key = (kind, query, task_id, prefix)
            if not query or not gold_paths or key in seen:
                continue
            seen.add(key)
            cases.append(
                GoldCase(
                    kind=kind,
                    query=query,
                    task_id=task_id,
                    repo=prefix,
                    gold_paths=gold_paths,
                    source_manifest=filename,
                )
            )
    return cases, repositories, source_hashes


def _load_repository(prefix: str, provision_root: Path) -> RepositoryData:
    root = provision_root / prefix
    workspace = root / "workspace"
    db_path = root / "index" / "code_context.sqlite"
    if not workspace.is_dir() or not db_path.is_file():
        raise FileNotFoundError(f"missing provisioned repository {prefix}: workspace={workspace} db={db_path}")

    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = connection.execute("""
        SELECT symbol_id, file_path, symbol_name, qualified_name, kind,
               COALESCE(signature, ''), COALESCE(doc_summary, '')
        FROM symbols
        WHERE file_path IS NOT NULL AND symbol_name IS NOT NULL
        """).fetchall()
    connection.close()

    symbols: list[SymbolRow] = []
    by_file: dict[str, list[SymbolRow]] = defaultdict(list)
    name_files_mut: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        path = _normalize(str(row[1]))
        name = str(row[2]).strip()
        if not name or len(name) < 4 or name.lower() in _COMMON_NAMES:
            continue
        if not _is_allowed_path(path) or not (workspace / path).is_file():
            continue
        symbol = SymbolRow(
            symbol_id=str(row[0]),
            file_path=path,
            symbol_name=name,
            qualified_name=str(row[3] or name),
            kind=str(row[4] or "symbol"),
            signature=str(row[5] or ""),
            doc_summary=str(row[6] or ""),
        )
        symbols.append(symbol)
        by_file[path].append(symbol)
        name_files_mut[name].add(path)

    symbols.sort(key=lambda item: _stable_digest("symbol", prefix, item.file_path, item.symbol_id))
    for file_symbols in by_file.values():
        file_symbols.sort(key=lambda item: _stable_digest("file-symbol", prefix, item.symbol_id))
    name_files = {name: tuple(sorted(paths)) for name, paths in name_files_mut.items()}
    literals = _mine_literals(workspace, by_file)
    return RepositoryData(
        prefix=prefix,
        workspace=workspace,
        db_path=db_path,
        symbols=symbols,
        by_file=dict(by_file),
        name_files=name_files,
        literals=literals,
    )


def _source_files(workspace: Path, paths: Iterable[str]) -> Iterator[tuple[str, str]]:
    for path in sorted(set(paths)):
        target = workspace / path
        try:
            if not target.is_file() or target.stat().st_size > 2_000_000:
                continue
            text = target.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        yield path, text


def _clean_literal(value: str) -> str:
    value = _SPACE_RE.sub(" ", value.strip())
    if "http://" in value or "https://" in value:
        return ""
    if len(value) < 12 or len(value) > 100:
        return ""
    if sum(character.isalpha() for character in value) < 6:
        return ""
    if value.count(" ") > 16:
        return ""
    return value


def _mine_literals(workspace: Path, by_file: dict[str, list[SymbolRow]]) -> list[tuple[str, tuple[str, ...], str]]:
    occurrences: dict[str, set[str]] = defaultdict(set)
    source_for: dict[str, str] = {}
    for path, text in _source_files(workspace, by_file):
        for match in _STRING_RE.finditer(text):
            literal = _clean_literal(match.group("value"))
            if not literal:
                continue
            occurrences[literal].add(path)
            source_for.setdefault(literal, path)
    output = [
        (literal, tuple(sorted(paths)), source_for[literal])
        for literal, paths in occurrences.items()
        if 1 <= len(paths) <= 10
    ]
    output.sort(key=lambda item: _stable_digest("literal", item[2], item[0]))
    return output


def _shape_count(query: str) -> int:
    return max(1, min(4, query.count("|") + 1))


def _semantic_query(symbol: SymbolRow) -> str:
    summary = _SPACE_RE.sub(" ", symbol.doc_summary.strip().rstrip("."))
    if len(summary) < 18:
        return ""
    banned = {
        token.lower()
        for token in _IDENTIFIER_RE.findall(f"{symbol.symbol_name} {symbol.qualified_name}")
        if len(token) >= 4
    }
    words = [word for word in summary.split() if word.strip("`'\".,:;()[]{}").lower() not in banned]
    summary = " ".join(words).strip()
    if len(summary) < 18 or len(summary.split()) < 4:
        return ""
    return f"find code that {summary[:120]}"


def _rg_files(pattern: str, repository: RepositoryData, *, fixed: bool = False, max_files: int = 10) -> tuple[str, ...]:
    command = ["rg", "-l", "--no-messages", "--max-filesize", "2M"]
    if fixed:
        command.append("-F")
    command.extend(["--", pattern, str(repository.workspace)])
    try:
        process = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return ()
    if process.returncode not in {0, 1}:
        return ()
    paths: list[str] = []
    for raw in process.stdout.splitlines():
        try:
            path = _normalize(str(Path(raw).resolve().relative_to(repository.workspace.resolve())))
        except ValueError:
            continue
        if not _is_allowed_path(path) or not (repository.workspace / path).is_file():
            continue
        paths.append(path)
        if len(paths) > max_files:
            return ()
    return tuple(sorted(set(paths)))


class ReplacementFactory:
    def __init__(
        self,
        repository: RepositoryData,
        *,
        seed: int,
        used_queries: set[tuple[str, str]],
    ) -> None:
        self.repository = repository
        self.seed = seed
        self.used_queries = used_queries
        self.cursor: Counter[str] = Counter()
        self.source_use: Counter[tuple[str, str]] = Counter()
        production = [
            symbol
            for symbol in repository.symbols
            if not re.search(r"(?:^|/)(?:tests?|docs?)(?:/|$)", symbol.file_path, re.IGNORECASE)
        ]
        self.production_symbols = production or repository.symbols

    def create(self, old: GoldCase) -> Replacement:
        method = {
            "definition": self._definition,
            "content": self._content,
            "semantic": self._semantic,
            "sessions": self._session,
            "swebench": self._swebench,
        }.get(old.kind)
        if method is None:
            raise ValueError(f"unsupported gold kind: {old.kind}")
        for attempt in range(20_000):
            result = method(old, attempt)
            if result is None:
                continue
            query, gold_paths, source_file, symbols, generation_method = result
            key = (old.repo, query)
            if key in self.used_queries:
                continue
            reuse_limit = 2 if old.kind in {"definition", "content", "semantic"} else 4
            source_key = (old.kind, source_file)
            if self.source_use[source_key] >= reuse_limit:
                continue
            self.used_queries.add(key)
            self.source_use[source_key] += 1
            task_id = f"fbv2-{old.kind}-{_stable_digest(self.seed, old.case_id, query)[:16]}"
            return Replacement(
                replaces_case_id=old.case_id,
                kind=old.kind,
                repo=old.repo,
                query=query,
                task_id=task_id,
                gold_paths=tuple(gold_paths),
                method=generation_method,
                source_file=source_file,
                source_symbols=tuple(symbols),
            )
        raise RuntimeError(f"unable to generate replacement for {old.case_id}")

    def _symbol_at(self, namespace: str, attempt: int) -> SymbolRow:
        pool = self.production_symbols if namespace in {"definition", "semantic"} else self.repository.symbols
        index = self.cursor[namespace] + attempt
        return pool[index % len(pool)]

    def _cluster(self, symbol: SymbolRow, count: int) -> list[SymbolRow]:
        candidates = self.repository.by_file.get(symbol.file_path, [symbol])
        unique: list[SymbolRow] = []
        seen: set[str] = set()
        for candidate in [symbol, *candidates]:
            if candidate.symbol_name in seen or candidate.symbol_name.lower() in _COMMON_NAMES:
                continue
            seen.add(candidate.symbol_name)
            unique.append(candidate)
            if len(unique) >= count:
                break
        return unique

    def _definition(self, old: GoldCase, attempt: int) -> tuple[str, tuple[str, ...], str, tuple[str, ...], str] | None:
        symbol = self._symbol_at("definition", attempt)
        if symbol.kind.lower() not in {"class", "function", "method", "interface", "trait"}:
            return None
        count = _shape_count(old.query)
        cluster = self._cluster(symbol, count)
        if not cluster:
            return None
        alternatives: list[str] = []
        gold: set[str] = set()
        for item in cluster:
            prefix = "class" if item.kind.lower() == "class" else "def"
            alternatives.append(f"{prefix} {item.symbol_name}")
            gold.update(self.repository.name_files.get(item.symbol_name, (item.file_path,)))
        existing = _existing_replacement_paths(self.repository.workspace, gold)
        if not existing or len(existing) > 8:
            return None
        query = "|".join(alternatives)
        self.cursor["definition"] += 1
        return (
            query,
            existing,
            symbol.file_path,
            tuple(item.symbol_name for item in cluster),
            "symbol_definition_cluster",
        )

    def _content(self, old: GoldCase, attempt: int) -> tuple[str, tuple[str, ...], str, tuple[str, ...], str] | None:
        if not self.repository.literals:
            return None
        index = self.cursor["content"] + attempt
        literal, paths, source_file = self.repository.literals[index % len(self.repository.literals)]
        existing = _existing_replacement_paths(self.repository.workspace, paths)
        if not existing:
            return None
        self.cursor["content"] += 1
        return literal, existing, source_file, (), "distinctive_source_literal"

    def _semantic(self, old: GoldCase, attempt: int) -> tuple[str, tuple[str, ...], str, tuple[str, ...], str] | None:
        symbol = self._symbol_at("semantic", attempt)
        query = _semantic_query(symbol)
        if not query:
            return None
        existing = _existing_replacement_paths(self.repository.workspace, [symbol.file_path])
        if not existing:
            return None
        self.cursor["semantic"] += 1
        return query, existing, symbol.file_path, (symbol.symbol_name,), "doc_summary_without_identifier"

    def _session(self, old: GoldCase, attempt: int) -> tuple[str, tuple[str, ...], str, tuple[str, ...], str] | None:
        symbol = self._symbol_at("sessions", attempt)
        cluster = self._cluster(symbol, _shape_count(old.query))
        if not cluster:
            return None
        alternatives = []
        for index, item in enumerate(cluster):
            if index == 0 and item.kind.lower() in {"function", "method"}:
                alternatives.append(rf"def\s+{re.escape(item.symbol_name)}\b")
            elif index == 0 and item.kind.lower() == "class":
                alternatives.append(rf"class\s+{re.escape(item.symbol_name)}\b")
            else:
                alternatives.append(re.escape(item.symbol_name))
        query = "|".join(alternatives)
        paths = _rg_files(query, self.repository, max_files=10)
        if not paths:
            return None
        self.cursor["sessions"] += 1
        return query, paths, symbol.file_path, tuple(item.symbol_name for item in cluster), "session_style_regex"

    def _swebench(self, old: GoldCase, attempt: int) -> tuple[str, tuple[str, ...], str, tuple[str, ...], str] | None:
        symbol = self._symbol_at("swebench", attempt)
        count = max(2, _shape_count(old.query))
        cluster = self._cluster(symbol, count)
        if len(cluster) < 2:
            return None
        alternatives = [re.escape(item.symbol_name) for item in cluster]
        if "def " in old.query and cluster[0].kind.lower() in {"function", "method"}:
            alternatives[0] = rf"def\s+{re.escape(cluster[0].symbol_name)}\b"
        query = "|".join(alternatives)
        paths = _rg_files(query, self.repository, max_files=10)
        if not paths:
            return None
        self.cursor["swebench"] += 1
        return (
            query,
            paths,
            symbol.file_path,
            tuple(item.symbol_name for item in cluster),
            "issue_localization_symbol_cluster",
        )


def _write_manifests(
    output_dir: Path,
    cases: Sequence[GoldCase],
    replacements: Sequence[Replacement],
    repositories: dict[str, dict[str, Any]],
    provision_root: Path,
) -> dict[str, Path]:
    by_kind: dict[str, list[GoldCase | Replacement]] = defaultdict(list)
    for case in cases:
        by_kind[case.kind].append(case)
    for replacement in replacements:
        by_kind[replacement.kind].append(replacement)

    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for kind, _source in _MANIFESTS:
        pairs: list[list[str]] = []
        true_map: dict[str, list[str]] = {}
        repos_for_kind: dict[str, dict[str, Any]] = {}
        for item in by_kind[kind]:
            if isinstance(item, GoldCase):
                task_id, query, repo, gold = item.task_id, item.query, item.repo, item.gold_paths
            else:
                task_id, query, repo, gold = item.task_id, item.query, item.repo, item.gold_paths
            pairs.append([query, task_id, repo])
            true_map[task_id] = list(gold)
            metadata = dict(repositories.get(repo, {}))
            metadata["ws"] = str((provision_root / repo / "workspace").resolve())
            metadata["db"] = str((provision_root / repo / "index" / "code_context.sqlite").resolve())
            repos_for_kind[repo] = metadata
        path = output_dir / _OUTPUT_NAMES[kind]
        path.write_text(
            json.dumps(
                {
                    "gold_kind": kind,
                    "benchmark_version": "filebacked-v2",
                    "description": (
                        "Historical scorable cases plus one-for-one deterministic replacements for cases whose "
                        "gold files are absent from the pinned repository revision. Gold was not used for training."
                    ),
                    "pairs": pairs,
                    "true_map": true_map,
                    "repos": repos_for_kind,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        written[kind] = path
    return written


def _validate_outputs(paths: Sequence[Path], provision_root: Path) -> dict[str, Any]:
    cases, _repositories, _hashes = _load_cases_from_paths(paths)
    missing: list[str] = []
    by_kind: Counter[str] = Counter()
    for case in cases:
        by_kind[case.kind] += 1
        workspace = provision_root / case.repo / "workspace"
        if not _existing_gold_paths(workspace, case.gold_paths):
            missing.append(case.case_id)
    return {
        "evaluator_cases": len(cases),
        "by_kind": dict(sorted(by_kind.items())),
        "missing_file_cases": len(missing),
        "missing_case_ids": missing,
    }


def _load_cases_from_paths(paths: Sequence[Path]) -> tuple[list[GoldCase], dict[str, dict[str, Any]], dict[str, str]]:
    temp_dir = paths[0].parent
    expected = {path.name: path for path in paths}
    cases: list[GoldCase] = []
    repositories: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    seen: set[tuple[str, str, str, str]] = set()
    for path in paths:
        raw = json.loads(path.read_text(encoding="utf-8"))
        kind = str(raw.get("gold_kind") or path.stem)
        hashes[path.name] = _sha256(path)
        repositories.update({str(k): dict(v) for k, v in (raw.get("repos") or {}).items() if isinstance(v, dict)})
        true_map = raw.get("true_map") or {}
        for item in raw.get("pairs") or []:
            if not isinstance(item, list) or len(item) < 3:
                continue
            query, task_id, repo = str(item[0]).strip(), str(item[1]), str(item[2])
            gold = true_map.get(task_id) or []
            if isinstance(gold, str):
                gold = [gold]
            gold_paths = tuple(_normalize(str(value)) for value in gold if str(value))
            key = (kind, query, task_id, repo)
            if not query or not gold_paths or key in seen:
                continue
            seen.add(key)
            cases.append(GoldCase(kind, query, task_id, repo, gold_paths, path.name))
    del temp_dir, expected
    return cases, repositories, hashes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--provision-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260730)
    args = parser.parse_args(argv)

    source_dir = args.source_dir.resolve()
    provision_root = args.provision_root.expanduser().resolve()
    output_dir = args.out_dir.resolve()
    loaded, repositories, source_hashes = _load_cases(source_dir)

    valid_cases: list[GoldCase] = []
    invalid_cases: list[GoldCase] = []
    for case in loaded:
        workspace = provision_root / case.repo / "workspace"
        existing = _existing_gold_paths(workspace, case.gold_paths)
        if existing:
            valid_cases.append(GoldCase(case.kind, case.query, case.task_id, case.repo, existing, case.source_manifest))
        else:
            invalid_cases.append(case)

    used_queries = {(case.repo, case.query) for case in loaded}
    repository_cache: dict[str, RepositoryData] = {}
    factories: dict[str, ReplacementFactory] = {}
    replacements: list[Replacement] = []
    for case in invalid_cases:
        if case.repo not in repository_cache:
            repository_cache[case.repo] = _load_repository(case.repo, provision_root)
            factories[case.repo] = ReplacementFactory(
                repository_cache[case.repo], seed=args.seed, used_queries=used_queries
            )
        replacements.append(factories[case.repo].create(case))

    written = _write_manifests(
        output_dir,
        valid_cases,
        replacements,
        repositories,
        provision_root,
    )
    validation = _validate_outputs([written[kind] for kind, _ in _MANIFESTS], provision_root)
    expected_count = len(loaded)
    if len(replacements) != len(invalid_cases):
        raise RuntimeError("replacement count mismatch")
    if validation["evaluator_cases"] != expected_count or validation["missing_file_cases"] != 0:
        raise RuntimeError(f"invalid generated benchmark: {validation}")

    replacement_by_kind_repo = Counter((item.kind, item.repo) for item in replacements)
    audit = {
        "benchmark_version": "filebacked-v2",
        "generation_seed": args.seed,
        "generation_contract": {
            "retrieval_outputs_used": False,
            "model_scores_used": False,
            "benchmark_gold_used_for_training": False,
            "preserve_evaluator_case_count": True,
            "preserve_invalid_case_kind_repo_distribution": True,
        },
        "source_dir": str(source_dir),
        "source_manifest_sha256": source_hashes,
        "provision_root": str(provision_root),
        "source_evaluator_cases": len(loaded),
        "retained_cases": len(valid_cases),
        "removed_missing_file_cases": len(invalid_cases),
        "generated_replacements": len(replacements),
        "removed_by_kind_repo": {
            f"{kind}:{repo}": count for (kind, repo), count in sorted(replacement_by_kind_repo.items())
        },
        "removed_cases": [
            {
                "case_id": case.case_id,
                "kind": case.kind,
                "repo": case.repo,
                "query": case.query,
                "gold_paths": list(case.gold_paths),
                "reason": "no_gold_path_exists_in_pinned_workspace",
            }
            for case in invalid_cases
        ],
        "replacements": [asdict(item) for item in replacements],
        "outputs": {kind: {"path": str(path), "sha256": _sha256(path)} for kind, path in written.items()},
        "validation": validation,
        "known_limitations": [
            "This version fixes impossible missing-file labels only; it does not remove every historically noisy or self-referential label.",
            "Generated replacements are synthetic, deterministic retrieval tasks and should be reported separately from the historical benchmark.",
            "Freeze this version before evaluation and do not tune a model against its results.",
        ],
    }
    audit_path = output_dir / "filebacked_v2_audit.json"
    audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "source_cases": len(loaded),
                "retained": len(valid_cases),
                "replaced": len(replacements),
                "validation": validation,
                "audit": str(audit_path),
                "outputs": {kind: str(path) for kind, path in written.items()},
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
