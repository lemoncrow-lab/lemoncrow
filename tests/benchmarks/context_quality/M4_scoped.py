"""M4 — Scoped pull-context benchmark: retrieval precision + recall.

Measures whether :class:`ScopedContextCapability` retrieves the *right* scope
for a multi-file subtask and keeps noise out of the top results. Runs against a
real ``CodeContextEngine`` index built over a controlled multi-domain fixture
repo (4 domains x 3 files + cross-domain distractors), so the metric reflects
ranking/scoping quality deterministically. Also includes a real-repo variant
labelled from recent multi-file commit history so commit provenance and local
scope retrieval are exercised together.

Targets (README): precision >=0.6, recall >=0.85.
  * recall          = fraction of a subtask's relevant files retrieved.
  * precision@k      = top-k purity at k=|relevant|+1 (exposes one noise slot).

Run explicitly (slow):
    uv run pytest tests/benchmarks/context_quality/M4_scoped.py -v -m slow
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any
from unittest.mock import patch

import pytest

from lemoncrow.core.capabilities.code_context import CodeContextEngine
from lemoncrow.core.capabilities.scoped_context import ScopedContextCapability, Subtask

# domain -> {module_stem: keyword-rich body}
_DOMAINS: dict[str, dict[str, str]] = {
    "auth": {
        "auth_login": "login authenticate credentials password user",
        "auth_session": "session login user lifecycle cookie",
        "auth_tokens": "auth token jwt issue verify user",
    },
    "payments": {
        "pay_charge": "charge credit card payment amount",
        "pay_refund": "refund payment reverse charge",
        "pay_audit": "audit log payment charge refund trail",
    },
    "search": {
        "srch_bm25": "bm25 lexical relevance scoring rank",
        "srch_rank": "rank results scoring search order",
        "srch_index": "index documents search tokens postings",
    },
    "cache": {
        "cache_prefix": "prefix cache key stable hash",
        "cache_planner": "cache planner breakpoint plan reuse",
        "cache_evict": "cache eviction ttl expire evict",
    },
}

# (subtask description, target domain) — each targets one domain's 3 files.
_QUERIES: list[tuple[str, str]] = [
    ("authenticate user login session token", "auth"),
    ("charge refund credit card payment audit", "payments"),
    ("bm25 ranking over the search index documents", "search"),
    ("prefix cache planner breakpoint eviction", "cache"),
]

_CODE_PREFIXES = ("src/", "tests/", "benchmarks/")
_CODE_SUFFIXES = (".py",)


@dataclass(frozen=True)
class RepoHistoryQuery:
    commit_sha: str
    summary: str
    files: list[str]
    affected_paths: list[str]
    description: str
    keywords: list[str]


def _stem_tokens(value: str) -> list[str]:
    return [
        token.lower()
        for token in re.findall(r"[A-Za-z0-9]+", value.replace("-", " ").replace("_", " "))
        if len(token) >= 3
    ]


def _get_engine(repo_root: Path) -> CodeContextEngine:
    repo_id = hashlib.sha256(str(repo_root.resolve()).encode()).hexdigest()[:16]
    lemoncrow_root = Path(os.environ.get("LEMONCROW_ROOT") or Path.home() / ".lemoncrow")
    db_path = lemoncrow_root / "repos" / repo_id / "code.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    previous_code_embedder = os.environ.get("LEMONCROW_CODE_EMBEDDER")
    previous_code_embed_model = os.environ.get("LEMONCROW_CODE_EMBED_MODEL")
    os.environ["LEMONCROW_CODE_EMBEDDER"] = "null"
    os.environ.pop("LEMONCROW_CODE_EMBED_MODEL", None)
    try:
        with patch.object(CodeContextEngine, "_ensure_lineage_ready", return_value=None):
            engine = CodeContextEngine(repo_root=repo_root, db_path=db_path, autosync_enabled=False)
        engine._zoekt_candidate_files = lambda *args, **kwargs: None  # type: ignore[method-assign]
        return engine
    finally:
        if previous_code_embedder is None:
            os.environ.pop("LEMONCROW_CODE_EMBEDDER", None)
        else:
            os.environ["LEMONCROW_CODE_EMBEDDER"] = previous_code_embedder
        if previous_code_embed_model is None:
            os.environ.pop("LEMONCROW_CODE_EMBED_MODEL", None)
        else:
            os.environ["LEMONCROW_CODE_EMBED_MODEL"] = previous_code_embed_model


def _normalize_repo_path(path: str, *, repo_root: Path) -> str:
    if not path:
        return ""
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            return candidate.relative_to(repo_root).as_posix()
        except ValueError:
            return candidate.as_posix()
    return candidate.as_posix()


def _collect_repo_history_queries(repo_root: Path, *, limit: int = 5) -> list[RepoHistoryQuery]:
    from lemoncrow.infra.code_intel.git_history.walker import iter_commit_records

    queries: list[RepoHistoryQuery] = []
    for record in iter_commit_records(repo_root, limit=80, since_sha=None):
        code_files = [
            path
            for path in record.files_touched
            if path.startswith(_CODE_PREFIXES) and path.endswith(_CODE_SUFFIXES) and (repo_root / path).exists()
        ]
        if 2 <= len(code_files) <= 5 and not record.message.lower().startswith(("docs", "chore")):
            selected_files = code_files[:3]
            keywords: list[str] = []
            for file_path in selected_files:
                keywords.extend(_stem_tokens(Path(file_path).stem))
                keywords.extend(_stem_tokens(Path(file_path).parent.name))
            keywords.extend(_stem_tokens(record.message))
            deduped_keywords: list[str] = []
            seen: set[str] = set()
            for keyword in keywords:
                if keyword not in seen:
                    deduped_keywords.append(keyword)
                    seen.add(keyword)
            queries.append(
                RepoHistoryQuery(
                    commit_sha=record.sha,
                    summary=record.message,
                    files=selected_files,
                    affected_paths=selected_files[:2],
                    description=f"root cause history for {record.message}",
                    keywords=deduped_keywords[:8],
                )
            )
        if len(queries) >= limit:
            break
    return queries[:limit]


def _commit_chunk_count(engine: CodeContextEngine) -> int:
    with closing(engine.connection()) as conn:
        count_row = conn.execute("SELECT COUNT(*) AS n FROM commit_chunks").fetchone()
        return int(count_row["n"]) if count_row else 0


def _ensure_commit_chunks(
    engine: CodeContextEngine,
    *,
    repo_root: Path,
    queries: list[RepoHistoryQuery],
) -> int:
    existing = _commit_chunk_count(engine)
    if existing > 0:
        return existing

    previous_code_embedder = os.environ.get("LEMONCROW_CODE_EMBEDDER")
    previous_code_embed_model = os.environ.get("LEMONCROW_CODE_EMBED_MODEL")
    os.environ["LEMONCROW_CODE_EMBEDDER"] = "null"
    os.environ.pop("LEMONCROW_CODE_EMBED_MODEL", None)
    try:
        from lemoncrow.core.capabilities.code_context.engine import _LINEAGE_INDEX_VERSION
        from lemoncrow.infra.code_intel.git_history import embedder as history_embedder_module
        from lemoncrow.infra.code_intel.git_history.embedder import embed_summary
        from lemoncrow.infra.code_intel.git_history.models import CommitSummary
        from lemoncrow.infra.code_intel.git_history.walker import iter_commit_records
        from lemoncrow.infra.embeddings.factory import make_code_embedder

        history_embedder_module._embedder = None
        make_code_embedder.cache_clear()

        target_shas = {query.commit_sha for query in queries}
        if not target_shas:
            return 0

        rows: list[tuple[Any, ...]] = []

        for record in iter_commit_records(repo_root, limit=200, since_sha=None):
            if record.sha not in target_shas:
                continue
            try:
                selected_files = [path for path in record.files_touched if path.startswith(_CODE_PREFIXES)][:3]
                summary_text = record.message.splitlines()[0].strip()[:160]
                summary = CommitSummary(
                    sha=record.sha,
                    author_date=record.author_date,
                    files_touched=selected_files or record.files_touched[:3],
                    summary=f"{summary_text}\nFiles: {', '.join(selected_files[:3])}",
                    summary_model="benchmark:git-log",
                    prompt_version="benchmark-v1",
                )
                rows.append(
                    (
                        summary.sha,
                        summary.author_date,
                        json.dumps(summary.files_touched),
                        None,
                        summary.summary,
                        summary.summary_model,
                        embed_summary(summary),
                        _LINEAGE_INDEX_VERSION,
                    )
                )
            except Exception:  # noqa: BLE001 - benchmark bootstrap should skip malformed seed rows
                continue
            if len(rows) == len(target_shas):
                break

        watermark_sha = rows[-1][0] if rows else None
        engine._replace_commit_chunks(rows, watermark_sha=watermark_sha)
    finally:
        if previous_code_embedder is None:
            os.environ.pop("LEMONCROW_CODE_EMBEDDER", None)
        else:
            os.environ["LEMONCROW_CODE_EMBEDDER"] = previous_code_embedder
        if previous_code_embed_model is None:
            os.environ.pop("LEMONCROW_CODE_EMBED_MODEL", None)
        else:
            os.environ["LEMONCROW_CODE_EMBED_MODEL"] = previous_code_embed_model
        from lemoncrow.infra.code_intel.git_history import embedder as history_embedder_module
        from lemoncrow.infra.embeddings.factory import make_code_embedder

        history_embedder_module._embedder = None
        make_code_embedder.cache_clear()

    return _commit_chunk_count(engine)


def run_repo_history_benchmark(repo_root: Path | None = None) -> dict[str, Any]:
    if repo_root is None:
        env_root = os.environ.get("LEMONCROW_REPO_ROOT")
        repo_root = Path(env_root) if env_root else Path.cwd()

    queries = _collect_repo_history_queries(repo_root)
    if len(queries) < 5:
        return {"error": "not enough multi-file code commits for real-history M4 benchmark", "total": 0}

    engine = _get_engine(repo_root)
    chunk_count = _ensure_commit_chunks(engine, repo_root=repo_root, queries=queries)
    if chunk_count == 0:
        return {"error": "commit_chunks table is empty after benchmark bootstrap", "total": 0}

    cap = ScopedContextCapability(engine)
    precisions: list[float] = []
    recalls: list[float] = []
    commit_hits = 0
    verdicts: list[dict[str, Any]] = []

    for query in queries:
        scoped = cap.pull(
            Subtask(
                description=query.description,
                affected_paths=query.affected_paths,
                keywords=query.keywords,
                budget_tokens=4000,
            )
        )
        relevant_paths = {_normalize_repo_path(path, repo_root=repo_root) for path in query.files}
        expected_commit = f"commit:{query.commit_sha[:7]}"
        relevant_items = set(relevant_paths)
        relevant_items.add(expected_commit)

        ordered: list[str] = []
        seen_items: set[str] = set()
        matched_commit = False
        for chunk in scoped.chunks:
            if chunk.provenance == "commit" and chunk.commit_sha:
                commit_key = f"commit:{chunk.commit_sha[:7]}"
                if commit_key not in seen_items:
                    ordered.append(commit_key)
                    seen_items.add(commit_key)
                if query.commit_sha.startswith(chunk.commit_sha[:7]) or chunk.commit_sha.startswith(
                    query.commit_sha[:7]
                ):
                    matched_commit = True
            normalized_path = _normalize_repo_path(chunk.path, repo_root=repo_root)
            if normalized_path and normalized_path not in seen_items:
                ordered.append(normalized_path)
                seen_items.add(normalized_path)

        retrieved = set(ordered)
        top_k = ordered[: min(len(relevant_items) + 1, len(ordered))]
        precision = len(set(top_k) & relevant_items) / len(top_k) if top_k else 0.0
        recall = len(retrieved & relevant_items) / len(relevant_items)
        precisions.append(precision)
        recalls.append(recall)
        commit_hits += int(matched_commit)
        verdicts.append(
            {
                "commit_sha": query.commit_sha[:7],
                "summary": query.summary,
                "files": query.files,
                "affected_paths": query.affected_paths,
                "keywords": query.keywords,
                "matched_commit": matched_commit,
                "precision": precision,
                "recall": recall,
                "top_items": top_k,
            }
        )

    total = len(queries)
    return {
        "total": total,
        "commit_hit_rate": commit_hits / total,
        "mean_precision": mean(precisions),
        "mean_recall": mean(recalls),
        "verdicts": verdicts,
    }


def _build_fixture(root: Path) -> None:
    for files in _DOMAINS.values():
        for stem, kw in files.items():
            (root / f"{stem}.py").write_text(
                f'def {stem}(x):\n    """{kw} {kw}"""\n    return x  # {kw}\n',
                encoding="utf-8",
            )
    subprocess.run(["git", "init", "-q"], cwd=root, check=False)
    subprocess.run(["git", "add", "-A"], cwd=root, check=False)


@pytest.mark.slow
def test_m4_precision_recall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_CODE_EMBEDDER", "null")
    monkeypatch.delenv("LEMONCROW_CODE_EMBED_MODEL", raising=False)
    _build_fixture(tmp_path)
    cap = ScopedContextCapability(CodeContextEngine(tmp_path))

    precisions: list[float] = []
    recalls: list[float] = []
    for query, domain in _QUERIES:
        relevant = set(_DOMAINS[domain])
        k = len(relevant) + 1  # one slot beyond the relevant set exposes noise
        scoped = cap.pull(Subtask(description=query, budget_tokens=3000))
        ordered: list[str] = []
        for chunk in scoped.chunks:
            stem = Path(chunk.path).stem
            if stem not in ordered:
                ordered.append(stem)
        retrieved = set(ordered)
        top_k = ordered[:k]
        precision = len(set(top_k) & relevant) / len(top_k) if top_k else 0.0
        recall = len(retrieved & relevant) / len(relevant)
        precisions.append(precision)
        recalls.append(recall)

    mean_precision = mean(precisions)
    mean_recall = mean(recalls)

    assert mean_precision >= 0.6, f"precision {mean_precision:.2f} below 0.60 target"
    assert mean_recall >= 0.85, f"recall {mean_recall:.2f} below 0.85 target"


@pytest.mark.slow
def test_m4_repo_history_precision_recall() -> None:
    repo_root_env = os.environ.get("LEMONCROW_REPO_ROOT")
    repo_root = Path(repo_root_env) if repo_root_env else Path.cwd()

    try:
        remotes = subprocess.check_output(["git", "remote", "-v"], cwd=repo_root, text=True, timeout=5)
        if "lemoncrow" not in remotes.lower() and "leanchain" not in remotes.lower():
            pytest.skip("Not running in the lemoncrow repo — skip real-history M4 benchmark")
    except (subprocess.CalledProcessError, OSError):
        pytest.skip("git remote check failed — skip real-history M4 benchmark")

    result = run_repo_history_benchmark(repo_root)
    if "error" in result:
        pytest.skip(f"Skipping real-history M4 benchmark: {result['error']}")

    mean_precision = float(result["mean_precision"])
    mean_recall = float(result["mean_recall"])
    commit_hit_rate = float(result["commit_hit_rate"])

    for verdict in result["verdicts"]:
        print(
            f"[{'PASS' if verdict['matched_commit'] else 'FAIL'}] "
            f"{verdict['commit_sha']} precision={verdict['precision']:.2f} "
            f"recall={verdict['recall']:.2f} :: {verdict['summary']}"
        )

    assert commit_hit_rate >= 0.7, f"real-history M4 commit hit rate {commit_hit_rate:.2f} below 0.70 target"
    assert mean_precision >= 0.45, f"real-history M4 precision {mean_precision:.2f} below 0.45 target"
    assert mean_recall >= 0.55, f"real-history M4 recall {mean_recall:.2f} below 0.55 target"


if __name__ == "__main__":
    import json

    repo_root_env = os.environ.get("LEMONCROW_REPO_ROOT")
    repo_root = Path(repo_root_env) if repo_root_env else Path.cwd()
    print(json.dumps(run_repo_history_benchmark(repo_root), indent=2))
