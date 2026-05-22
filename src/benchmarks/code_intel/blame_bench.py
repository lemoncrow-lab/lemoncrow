"""Deterministic benchmark for the public `code op="blame"` surface."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from atelier.core.capabilities.code_context import CodeContextEngine
from atelier.core.capabilities.repo_map.budget import count_tokens
from atelier.gateway.adapters.mcp_server import tool_code


@dataclass
class BlameBenchResult:
    query: str
    budget_tokens: int
    cold_total_tokens: int
    hot_total_tokens: int
    cold_cache_hit: bool
    hot_cache_hit: bool
    cold_provenance: str
    hot_provenance: str
    cold_elapsed_ms: float
    hot_elapsed_ms: float
    last_author: str
    churn_commit_count: int | None
    manual_total_tokens: int
    blame_to_manual_ratio: float
    blame_workflow_steps: int
    manual_workflow_steps: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "budget_tokens": self.budget_tokens,
            "cold_total_tokens": self.cold_total_tokens,
            "hot_total_tokens": self.hot_total_tokens,
            "cold_cache_hit": self.cold_cache_hit,
            "hot_cache_hit": self.hot_cache_hit,
            "cold_provenance": self.cold_provenance,
            "hot_provenance": self.hot_provenance,
            "cold_elapsed_ms": self.cold_elapsed_ms,
            "hot_elapsed_ms": self.hot_elapsed_ms,
            "last_author": self.last_author,
            "churn_commit_count": self.churn_commit_count,
            "manual_total_tokens": self.manual_total_tokens,
            "blame_to_manual_ratio": self.blame_to_manual_ratio,
            "blame_workflow_steps": self.blame_workflow_steps,
            "manual_workflow_steps": self.manual_workflow_steps,
        }


def _git(args: list[str], repo_root: Path, *, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout.strip()


def _commit_all(
    repo_root: Path,
    message: str,
    *,
    author_name: str,
    author_email: str,
    author_date: str,
) -> str:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
            "GIT_AUTHOR_DATE": author_date,
            "GIT_COMMITTER_DATE": author_date,
        }
    )
    _git(["add", "-A"], repo_root, env=env)
    _git(["commit", "-m", message], repo_root, env=env)
    return _git(["rev-parse", "HEAD"], repo_root, env=env)


def _write_scip_fixture(repo_root: Path, *, index_sha: str) -> None:
    engine = CodeContextEngine(repo_root)
    source = (repo_root / "service.py").read_text(encoding="utf-8")
    artifact_dir = repo_root / ".atelier" / "cache" / "scip" / engine.repo_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "repo_id": engine.repo_id,
        "language": "python",
        "index_sha": index_sha,
        "symbols": [
            {
                "symbol_id": "scip-risk-score",
                "repo_id": engine.repo_id,
                "file_path": "service.py",
                "language": "python",
                "symbol_name": "risk_score",
                "qualified_name": "risk_score",
                "kind": "function",
                "signature": "def risk_score() -> int:",
                "start_byte": source.index("def risk_score"),
                "end_byte": len(source.encode("utf-8")),
                "start_line": 1,
                "end_line": 3,
                "content_hash": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "source": source,
                "provenance": "scip",
            }
        ],
    }
    (artifact_dir / "python.scip").write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _write_fixture_repo(repo_root: Path) -> str:
    repo_root.mkdir(parents=True, exist_ok=True)
    _git(["init"], repo_root)
    _git(["config", "user.name", "Fixture Tester"], repo_root)
    _git(["config", "user.email", "fixture@example.com"], repo_root)
    now = datetime.now(tz=UTC)
    (repo_root / "service.py").write_text(
        "def risk_score() -> int:\n"
        "    value = 1\n"
        "    return value\n",
        encoding="utf-8",
    )
    _commit_all(
        repo_root,
        "add risk score",
        author_name="Alice",
        author_email="alice@example.com",
        author_date=(now - timedelta(days=240)).isoformat(),
    )
    (repo_root / "service.py").write_text(
        "def risk_score() -> int:\n"
        "    value = 3\n"
        "    return value\n",
        encoding="utf-8",
    )
    _commit_all(
        repo_root,
        "tune risk score",
        author_name="Bob",
        author_email="bob@example.com",
        author_date=(now - timedelta(days=30)).isoformat(),
    )
    (repo_root / "service.py").write_text(
        "def risk_score() -> int:\n"
        "    value = 5\n"
        "    return value\n",
        encoding="utf-8",
    )
    head_sha = _commit_all(
        repo_root,
        "finalize risk score",
        author_name="Carol",
        author_email="carol@example.com",
        author_date=(now - timedelta(days=7)).isoformat(),
    )
    _write_scip_fixture(repo_root, index_sha=head_sha)
    return head_sha


def _manual_blame_tokens(repo_root: Path) -> int:
    transcript = {
        "workflow": [
            {
                "command": "git log --follow --stat -- service.py",
                "stdout": _git(["log", "--follow", "--stat", "--", "service.py"], repo_root),
            },
            {
                "command": "git blame -L 1,3 -- service.py",
                "stdout": _git(["blame", "-L", "1,3", "--", "service.py"], repo_root),
            },
            {
                "command": "git log --since=180 days ago -- service.py",
                "stdout": _git(["log", "--since=180 days ago", "--", "service.py"], repo_root),
            },
            {
                "command": "git show HEAD~1:service.py",
                "stdout": _git(["show", "HEAD~1:service.py"], repo_root),
            },
            {"goal": "Find the current owner and churn for risk_score from raw git output."},
        ]
    }
    return count_tokens(json.dumps(transcript, sort_keys=True))


def run_blame_bench(
    work_dir: Path | None = None,
    *,
    query: str = "risk_score",
    budget_tokens: int = 320,
) -> BlameBenchResult:
    bench_root = (work_dir or Path.cwd()) / "code_intel_blame"
    repo_root = bench_root / "fixture_repo"
    _write_fixture_repo(repo_root)

    cold_start = time.perf_counter()
    cold = tool_code({"op": "blame", "repo_root": str(repo_root), "query": query, "budget_tokens": budget_tokens})
    cold_elapsed_ms = (time.perf_counter() - cold_start) * 1000
    hot_start = time.perf_counter()
    hot = tool_code({"op": "blame", "repo_root": str(repo_root), "query": query, "budget_tokens": budget_tokens})
    hot_elapsed_ms = (time.perf_counter() - hot_start) * 1000
    manual_total_tokens = _manual_blame_tokens(repo_root)

    return BlameBenchResult(
        query=query,
        budget_tokens=budget_tokens,
        cold_total_tokens=int(cold.get("total_tokens", 0) or 0),
        hot_total_tokens=int(hot.get("total_tokens", 0) or 0),
        cold_cache_hit=bool(cold.get("cache_hit")),
        hot_cache_hit=bool(hot.get("cache_hit")),
        cold_provenance=str(cold.get("provenance") or ""),
        hot_provenance=str(hot.get("provenance") or ""),
        cold_elapsed_ms=round(cold_elapsed_ms, 3),
        hot_elapsed_ms=round(hot_elapsed_ms, 3),
        last_author=str(cold.get("last_author") or ""),
        churn_commit_count=int(cold.get("churn", {}).get("commit_count", 0)) if isinstance(cold.get("churn"), dict) else None,
        manual_total_tokens=manual_total_tokens,
        blame_to_manual_ratio=(int(cold.get("total_tokens", 0) or 0) / manual_total_tokens) if manual_total_tokens else 0.0,
        blame_workflow_steps=1,
        manual_workflow_steps=3,
    )


__all__ = ["BlameBenchResult", "run_blame_bench"]
