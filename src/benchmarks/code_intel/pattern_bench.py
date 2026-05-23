"""Deterministic benchmark for the M5 structural pattern workflow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from atelier.core.capabilities.repo_map.budget import count_tokens
from atelier.gateway.adapters.mcp_server import tool_code, tool_smart_read, tool_smart_search
from atelier.infra.code_intel.astgrep import PatternRewriteResult


@dataclass(frozen=True)
class PatternBenchResult:
    """Summary of the structural-pattern token comparison."""

    pattern_total_tokens: int
    baseline_total_tokens: int
    token_ratio: float
    files_changed: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "pattern_total_tokens": self.pattern_total_tokens,
            "baseline_total_tokens": self.baseline_total_tokens,
            "token_ratio": self.token_ratio,
            "files_changed": self.files_changed,
        }


def _write_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "http.py").write_text(
        "import requests\n\n"
        "def fetch_user(url: str) -> object:\n"
        "    return requests.get(url)\n\n"
        "def fetch_status(url: str) -> object:\n"
        "    return requests.get(url)\n",
        encoding="utf-8",
    )
    (root / "src" / "worker.py").write_text(
        "import requests\n\n" "def sync(url: str) -> object:\n" "    return requests.get(url)\n",
        encoding="utf-8",
    )


def _measure_baseline_tokens(repo_root: Path) -> int:
    search_payload = tool_smart_search({"query": "requests.get", "path": str(repo_root / "src"), "budget_tokens": 4000})
    unique_paths = sorted({str(match["path"]) for match in search_payload.get("matches", [])})
    read_tokens = 0
    edit_payloads: list[dict[str, str]] = []
    for path in unique_paths:
        read_payload = tool_smart_read({"path": path, "max_lines": 20})
        read_tokens += count_tokens(json.dumps(read_payload, sort_keys=True, default=str))
        edit_payloads.append(
            {
                "file_path": str(Path(path).relative_to(repo_root)),
                "old_string": "requests.get(url)",
                "new_string": "requests.get(url, timeout=30)",
            }
        )
    return (
        count_tokens(json.dumps(search_payload, sort_keys=True, default=str))
        + read_tokens
        + count_tokens(json.dumps({"edits": edit_payloads}, sort_keys=True, default=str))
    )


def run_pattern_bench(work_dir: Path | None = None) -> PatternBenchResult:
    """Compare pattern dry-run rewrite tokens against the text-search/read/edit baseline."""

    bench_root = (work_dir or Path.cwd()) / "code_intel_pattern"
    repo_root = bench_root / "fixture_repo"
    _write_fixture_repo(repo_root)
    baseline_total_tokens = _measure_baseline_tokens(repo_root)

    def fake_rewrite(self, *, pattern: str, rewrite: str, language=None, file_glob=None, dry_run=True):  # type: ignore[no-untyped-def]
        del self, pattern, rewrite, language, file_glob, dry_run
        diff = (
            "--- a/src/http.py\n+++ b/src/http.py\n@@\n"
            "-    return requests.get(url)\n+    return requests.get(url, timeout=30)\n"
            "--- a/src/worker.py\n+++ b/src/worker.py\n@@\n"
            "-    return requests.get(url)\n+    return requests.get(url, timeout=30)\n"
        )
        return PatternRewriteResult(diff=diff, files_changed=["src/http.py", "src/worker.py"])

    with patch("atelier.core.capabilities.code_context.engine.AstGrepAdapter.rewrite", new=fake_rewrite):
        pattern_payload = tool_code(
            {
                "op": "pattern",
                "repo_root": str(repo_root),
                "pattern": "requests.get($URL)",
                "rewrite": "requests.get($URL, timeout=30)",
                "dry_run": True,
                "language": "python",
                "budget_tokens": 180,
            }
        )

    pattern_total_tokens = int(pattern_payload.get("total_tokens", 0) or 0)
    ratio = pattern_total_tokens / baseline_total_tokens if baseline_total_tokens else 0.0
    return PatternBenchResult(
        pattern_total_tokens=pattern_total_tokens,
        baseline_total_tokens=baseline_total_tokens,
        token_ratio=ratio,
        files_changed=list(pattern_payload.get("files_changed", [])),
    )


__all__ = ["PatternBenchResult", "run_pattern_bench"]
