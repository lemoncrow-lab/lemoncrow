"""Benchmark cases for direct Zoekt-vs-rg comparison.

These cases benchmark broad lexical queries where raw `rg` output can explode
and Zoekt's capped file/snippet payload is expected to be much smaller.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any

from benchmarks.mcp_tools.harness import BaselineMeasurement, BenchCase


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _count_nonempty(lines: list[str]) -> int:
    return len([line for line in lines if line.strip()])


def _build_rg_baseline(case: BenchCase) -> BaselineMeasurement:
    root = _repo_root()
    query = str(case.args.get("query", "")).strip()
    search_path = str(case.args.get("search_path", "src")).strip() or "src"
    if not query:
        return BaselineMeasurement(payload={"error": "empty query"}, input_file_tokens=0, commands=[])

    cmd = ["rg", "-n", "--no-heading", "--fixed-strings", query, search_path]
    proc = subprocess.run(
        cmd,
        cwd=str(root),
        check=False,
        capture_output=True,
        text=True,
    )
    stdout = proc.stdout[:240_000]
    stderr = proc.stderr[:20_000]
    lines = stdout.splitlines()
    payload: dict[str, Any] = {
        "workflow": "fallback_rg",
        "query": query,
        "search_path": search_path,
        "exit_code": proc.returncode,
        "match_lines": _count_nonempty(lines),
        "rg_stdout": stdout,
        "rg_stderr": stderr,
    }
    return BaselineMeasurement(
        payload=payload,
        input_file_tokens=0,
        commands=[" ".join(shlex.quote(part) for part in cmd)],
    )


def _assert_zoekt_payload(result: dict[str, object]) -> None:
    assert "backend" in result, "zoekt result must include backend"
    assert result["backend"] == "zoekt", f"expected backend=zoekt, got {result['backend']!r}"
    file_count = result.get("file_count", 0)
    assert isinstance(file_count, int), f"expected file_count to be int, got {type(file_count).__name__}"
    assert file_count > 0, f"expected non-empty zoekt results, got {result!r}"
    assert isinstance(result.get("files"), list), "zoekt result must include files list"


ZOEKT_CASES: list[BenchCase] = [
    BenchCase(
        op="zoekt",
        label="zoekt-vs-rg/def-broad",
        args={
            "query": "def ",
            "search_path": "src",
            "max_files": 20,
            "max_chars_per_file": 500,
        },
        assert_keys=["backend", "file_count", "files", "total_tokens"],
        custom_assert=_assert_zoekt_payload,
        baseline_builder=_build_rg_baseline,
        min_baseline_tokens=1_000,
    ),
    BenchCase(
        op="zoekt",
        label="zoekt-vs-rg/tool-prefix",
        args={
            "query": "tool_",
            "search_path": "src",
            "max_files": 20,
            "max_chars_per_file": 500,
        },
        assert_keys=["backend", "file_count", "files", "total_tokens"],
        custom_assert=_assert_zoekt_payload,
        baseline_builder=_build_rg_baseline,
        min_baseline_tokens=1_000,
    ),
]
