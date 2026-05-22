"""Deterministic benchmark for deleted-history search on the public code surface."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from atelier.core.capabilities.repo_map.budget import count_tokens
from atelier.gateway.adapters.mcp_server import tool_code


@dataclass
class GraveyardBenchResult:
    query: str
    budget_tokens: int
    since: str
    touched_by: str
    result_count: int
    rename_target: str | None
    uncached_total_tokens: int
    cached_total_tokens: int
    uncached_cache_hit: bool
    cached_cache_hit: bool
    uncached_provenance: str
    cached_provenance: str
    manual_total_tokens: int
    deleted_to_manual_ratio: float
    deleted_workflow_steps: int
    manual_workflow_steps: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "budget_tokens": self.budget_tokens,
            "since": self.since,
            "touched_by": self.touched_by,
            "result_count": self.result_count,
            "rename_target": self.rename_target,
            "uncached_total_tokens": self.uncached_total_tokens,
            "cached_total_tokens": self.cached_total_tokens,
            "uncached_cache_hit": self.uncached_cache_hit,
            "cached_cache_hit": self.cached_cache_hit,
            "uncached_provenance": self.uncached_provenance,
            "cached_provenance": self.cached_provenance,
            "manual_total_tokens": self.manual_total_tokens,
            "deleted_to_manual_ratio": self.deleted_to_manual_ratio,
            "deleted_workflow_steps": self.deleted_workflow_steps,
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
    author_name: str = "Fixture Tester",
    author_email: str = "fixture@example.com",
    author_date: str | None = None,
) -> str:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }
    )
    if author_date is not None:
        env["GIT_AUTHOR_DATE"] = author_date
        env["GIT_COMMITTER_DATE"] = author_date
    _git(["add", "-A"], repo_root, env=env)
    _git(["commit", "-m", message], repo_root, env=env)
    return _git(["rev-parse", "HEAD"], repo_root, env=env)


def _write_fixture_repo(repo_root: Path) -> tuple[str, str]:
    repo_root.mkdir(parents=True, exist_ok=True)
    _git(["init"], repo_root)
    _git(["config", "user.name", "Fixture Tester"], repo_root)
    _git(["config", "user.email", "fixture@example.com"], repo_root)
    (repo_root / "legacy.py").write_text(
        "class LegacyCheckout:\n" "    def process(self) -> int:\n" "        return 1\n",
        encoding="utf-8",
    )
    _commit_all(repo_root, "add legacy symbol", author_date="2024-01-01T00:00:00+00:00")
    _git(["mv", "legacy.py", "modern.py"], repo_root)
    (repo_root / "modern.py").write_text(
        "class ModernCheckout:\n" "    def process(self) -> int:\n" "        return 2\n",
        encoding="utf-8",
    )
    rename_sha = _commit_all(
        repo_root,
        "rename legacy symbol",
        author_email="renames@example.com",
        author_date="2025-02-01T00:00:00+00:00",
    )
    (repo_root / "retired.py").write_text(
        "def old_worker() -> int:\n" "    return 7\n",
        encoding="utf-8",
    )
    _commit_all(repo_root, "add retired worker", author_date="2025-02-10T00:00:00+00:00")
    (repo_root / "retired.py").unlink()
    delete_sha = _commit_all(
        repo_root,
        "delete retired worker",
        author_email="history@example.com",
        author_date="2025-03-01T00:00:00+00:00",
    )
    return rename_sha, delete_sha


def _manual_archaeology_tokens(repo_root: Path, *, rename_sha: str, query: str, since: str, touched_by: str) -> int:
    transcript = {
        "workflow": [
            {
                "command": "git log --all --follow --name-status -- modern.py",
                "stdout": _git(["log", "--all", "--follow", "--name-status", "--", "modern.py"], repo_root),
            },
            {
                "command": "git log --all --follow --name-status -- legacy.py",
                "stdout": _git(["log", "--all", "--follow", "--name-status", "--", "legacy.py"], repo_root),
            },
            {
                "command": f"git show {rename_sha}:modern.py",
                "stdout": _git(["show", f"{rename_sha}:modern.py"], repo_root),
            },
            {
                "command": f"git log --all --since {since} --author {touched_by} -- modern.py legacy.py",
                "stdout": _git(
                    ["log", "--all", "--since", since, "--author", touched_by, "--", "modern.py", "legacy.py"],
                    repo_root,
                ),
            },
            {"goal": f"Find the current public identity for {query} from raw git output."},
        ]
    }
    return count_tokens(json.dumps(transcript, sort_keys=True))


def run_graveyard_bench(
    work_dir: Path | None = None,
    *,
    query: str = "ModernCheckout",
    since: str = "2025-01-01",
    touched_by: str = "renames@example.com",
    budget_tokens: int = 400,
) -> GraveyardBenchResult:
    bench_root = (work_dir or Path.cwd()) / "code_intel_graveyard_search"
    repo_root = bench_root / "fixture_repo"
    rename_sha, _delete_sha = _write_fixture_repo(repo_root)
    tool_code({"op": "index", "repo_root": str(repo_root), "budget_tokens": 4000})

    first = tool_code(
        {
            "op": "search",
            "repo_root": str(repo_root),
            "query": query,
            "scope": "deleted",
            "since": since,
            "touched_by": touched_by,
            "limit": 5,
            "budget_tokens": budget_tokens,
        }
    )
    second = tool_code(
        {
            "op": "search",
            "repo_root": str(repo_root),
            "query": query,
            "scope": "deleted",
            "since": since,
            "touched_by": touched_by,
            "limit": 5,
            "budget_tokens": budget_tokens,
        }
    )
    manual_total_tokens = _manual_archaeology_tokens(
        repo_root,
        rename_sha=rename_sha,
        query=query,
        since=since,
        touched_by=touched_by,
    )
    uncached_total_tokens = int(first.get("total_tokens", 0) or 0)
    first_item = dict(first.get("items", [])[0]) if first.get("items") else {}

    return GraveyardBenchResult(
        query=query,
        budget_tokens=budget_tokens,
        since=since,
        touched_by=touched_by,
        result_count=len(first.get("items", [])),
        rename_target=cast(str | None, first_item.get("rename_target")),
        uncached_total_tokens=uncached_total_tokens,
        cached_total_tokens=int(second.get("total_tokens", 0) or 0),
        uncached_cache_hit=bool(first.get("cache_hit")),
        cached_cache_hit=bool(second.get("cache_hit")),
        uncached_provenance=str(first.get("provenance") or ""),
        cached_provenance=str(second.get("provenance") or ""),
        manual_total_tokens=manual_total_tokens,
        deleted_to_manual_ratio=(uncached_total_tokens / manual_total_tokens) if manual_total_tokens else 0.0,
        deleted_workflow_steps=1,
        manual_workflow_steps=3,
    )


__all__ = ["GraveyardBenchResult", "run_graveyard_bench"]
