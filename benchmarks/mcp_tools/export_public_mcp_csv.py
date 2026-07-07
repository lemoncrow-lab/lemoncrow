# ruff: noqa: E402
"""Run the public MCP benchmark surface and export per-case results to CSV.

Usage:
    uv run python benchmarks/mcp_tools/export_public_mcp_csv.py
    uv run python benchmarks/mcp_tools/export_public_mcp_csv.py --csv-out /tmp/public-mcp.csv
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from atelier.gateway.cli.progress import ProgressReporter

from benchmarks.mcp_tools._env import configure_benchmark_runtime
from benchmarks.mcp_tools.bench_code import _tool_name_for_case_args, code_tool_dispatch
from benchmarks.mcp_tools.bench_context import (
    _disable_autosync_watcher,
    _disable_background_worker_spawn,
    _preseed_bootstrap,
)
from benchmarks.mcp_tools.bench_edit import _run_edit_case
from benchmarks.mcp_tools.bench_external_indexers import (
    default_benchmark_root,
    prepare_cached_repo_snapshot,
    repo_cache_key,
)
from benchmarks.mcp_tools.bench_rescue import run_rescue_suite
from benchmarks.mcp_tools.bench_shell import _patch_paths as _patch_shell_paths
from benchmarks.mcp_tools.bench_sql import _patch_db as _patch_sql_db
from benchmarks.mcp_tools.bench_verify import run_verify_suite
from benchmarks.mcp_tools.cases.code import CODE_CASES
from benchmarks.mcp_tools.cases.compact import COMPACT_CASES
from benchmarks.mcp_tools.cases.context import CONTEXT_CASES
from benchmarks.mcp_tools.cases.edit import EDIT_CASES
from benchmarks.mcp_tools.cases.grep import GREP_CASES
from benchmarks.mcp_tools.cases.memory import MEMORY_CASES
from benchmarks.mcp_tools.cases.read import READ_CASES
from benchmarks.mcp_tools.cases.rescue import RESCUE_CASES
from benchmarks.mcp_tools.cases.search import SEARCH_CASES
from benchmarks.mcp_tools.cases.shell import SHELL_CASES
from benchmarks.mcp_tools.cases.sql import SQL_CASES
from benchmarks.mcp_tools.cases.trace import TRACE_CASES
from benchmarks.mcp_tools.cases.verify import VERIFY_CASES
from benchmarks.mcp_tools.harness import BenchCase, CaseResult, ToolReport, run_case
from benchmarks.mcp_tools.reporter import render_summary


def _repo_root() -> Path:
    return ROOT


_REPO_SNAPSHOT_ROOT: Path | None = None

# Code-intel indexing is expensive to build cold (10-30s+ for this repo).
# Capture the real ATELIER_ROOT once at import time -- before any suite's
# _reset_runtime() call below can overwrite it with a throwaway per-suite
# temp path -- so code-intel suites can restore it afterward and reuse the
# persistent index instead of rebuilding from scratch on every run (and,
# without this, once per code-intel suite within a single run).
_REAL_ATELIER_ROOT = Path(os.environ.get("ATELIER_ROOT") or Path.home() / ".atelier")


def _repo_workspace_root() -> Path:
    global _REPO_SNAPSHOT_ROOT
    if _REPO_SNAPSHOT_ROOT is not None:
        return _REPO_SNAPSHOT_ROOT
    repo_root = _repo_root()
    cache_root = default_benchmark_root(repo_root) / "mcp-cache" / "snapshots"
    _REPO_SNAPSHOT_ROOT = prepare_cached_repo_snapshot(
        repo_root,
        cache_root,
        name="public-mcp-repo",
        cache_key=repo_cache_key(repo_root),
    )
    return _REPO_SNAPSHOT_ROOT


def _runtime_root(artifact_root: Path, tool_name: str) -> Path:
    return artifact_root / "public-mcp-runtime" / tool_name


def _reset_runtime(root: Path, *, workspace_root: Path | None = None) -> Path:
    configured = configure_benchmark_runtime(root, workspace_root=workspace_root)
    from atelier.gateway.adapters import mcp_server

    mcp_server._reset_runtime_cache_for_testing()
    mcp_server._current_ledger = None
    return configured


def _tool_report(tool_name: str, results: list[CaseResult]) -> ToolReport:
    return ToolReport(tool_name=tool_name, results=results)


class ShardStatusReporter(ProgressReporter):
    """Child-process progress reporter that writes shard status JSON instead of stdout."""

    def __init__(self, shard_name: str, total: int, status_file: Path) -> None:
        super().__init__("mcp", total=total, heartbeat_seconds=0)
        self.shard_name = shard_name
        self.status_file = status_file

    def fail(self, message: str) -> None:
        self.current = message
        self._emit("failed")

    def _emit(self, title: str) -> None:
        self._last_title = title
        payload = {
            "shard": self.shard_name,
            "status": (
                "failed" if title == "failed" else ("complete" if self.total and self.done >= self.total else "running")
            ),
            "title": title,
            "current": self.current,
            "done": self.done,
            "total": self.total or 0,
            "updated_at": time.time(),
        }
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.status_file.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        temp_path.replace(self.status_file)


def _run_simple_suite(
    tool_name: str,
    root: Path,
    cases: list[BenchCase],
    tool_fn: Callable[[dict[str, Any]], Any],
    *,
    workspace_root: Path | None = None,
    progress: ProgressReporter | None = None,
) -> ToolReport:
    _reset_runtime(root, workspace_root=workspace_root)
    results: list[CaseResult] = []
    for case in cases:
        if progress is not None:
            progress.phase("running MCP tool benchmark", current=f"{tool_name} {case.label}")
        results.append(run_case(case, tool_fn))
        if progress is not None:
            progress.step("running MCP tool benchmark", current=f"{tool_name} {case.label}")
    return _tool_report(tool_name, results)


def _run_read_suite(artifact_root: Path, progress: ProgressReporter | None = None) -> ToolReport:
    from atelier.gateway.adapters.mcp_server import tool_smart_read

    return _run_simple_suite(
        "read",
        _runtime_root(artifact_root, "read"),
        READ_CASES,
        tool_smart_read,
        workspace_root=_repo_workspace_root(),
        progress=progress,
    )


def _run_search_suite(artifact_root: Path, progress: ProgressReporter | None = None) -> ToolReport:
    from atelier.gateway.adapters.mcp_server import tool_smart_search

    return _run_simple_suite(
        "search",
        _runtime_root(artifact_root, "search"),
        SEARCH_CASES,
        tool_smart_search,
        workspace_root=_repo_workspace_root(),
        progress=progress,
    )


def _run_grep_suite(artifact_root: Path, progress: ProgressReporter | None = None) -> ToolReport:
    from atelier.gateway.adapters.mcp_server import tool_grep

    return _run_simple_suite(
        "grep",
        _runtime_root(artifact_root, "grep"),
        GREP_CASES,
        tool_grep,
        workspace_root=_repo_workspace_root(),
        progress=progress,
    )


def _run_context_suite(artifact_root: Path, progress: ProgressReporter | None = None) -> ToolReport:
    from atelier.gateway.adapters.mcp_server import tool_get_context

    root = _runtime_root(artifact_root, "context")
    _reset_runtime(root, workspace_root=_repo_workspace_root())
    _disable_background_worker_spawn()
    _disable_autosync_watcher()
    results: list[CaseResult] = []
    cold_start = [case for case in CONTEXT_CASES if case.label == "context/cold-start"]
    remaining = [case for case in CONTEXT_CASES if case.label != "context/cold-start"]
    for case in cold_start:
        if progress is not None:
            progress.phase("running MCP tool benchmark", current=f"context {case.label}")
        results.append(run_case(case, tool_get_context))
        if progress is not None:
            progress.step("running MCP tool benchmark", current=f"context {case.label}")
    _preseed_bootstrap(tool_get_context)
    for case in remaining:
        if progress is not None:
            progress.phase("running MCP tool benchmark", current=f"context {case.label}")
        results.append(run_case(case, tool_get_context))
        if progress is not None:
            progress.step("running MCP tool benchmark", current=f"context {case.label}")
    return _tool_report("context", results)


def _run_trace_suite(artifact_root: Path, progress: ProgressReporter | None = None) -> ToolReport:
    from atelier.gateway.adapters.mcp_server import tool_record_trace

    return _run_simple_suite(
        "trace",
        _runtime_root(artifact_root, "trace"),
        TRACE_CASES,
        tool_record_trace,
        progress=progress,
    )


def _run_memory_suite(artifact_root: Path, progress: ProgressReporter | None = None) -> ToolReport:
    root = _runtime_root(artifact_root, "memory")
    _reset_runtime(root)
    from atelier.gateway.adapters import mcp_server

    def tool_fn(args: dict[str, Any]) -> Any:
        payload = dict(args)
        archive_text = payload.pop("_archive_text", None)
        archive_source = str(payload.pop("_archive_source", "tool_output"))
        archive_source_ref = payload.pop("_archive_source_ref", "")
        archive_tags = payload.pop("_archive_tags", None)
        if isinstance(archive_text, str) and archive_text:
            mcp_server._memory_archive(
                agent_id=payload.get("agent_id"),
                text=archive_text,
                source=archive_source,
                source_ref=str(archive_source_ref),
                tags=list(archive_tags or []),
            )
        return mcp_server.tool_memory(payload)

    results: list[CaseResult] = []
    for case in MEMORY_CASES:
        if progress is not None:
            progress.phase("running MCP tool benchmark", current=f"memory {case.label}")
        results.append(run_case(case, tool_fn))
        if progress is not None:
            progress.step("running MCP tool benchmark", current=f"memory {case.label}")
    return _tool_report("memory", results)


def _run_sql_suite(artifact_root: Path, progress: ProgressReporter | None = None) -> ToolReport:
    root = _runtime_root(artifact_root, "sql")
    _reset_runtime(root)
    db_path = root / "test.db"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT
        );
        INSERT INTO users VALUES (1, 'Alice', 'alice@example.com');
        INSERT INTO users VALUES (2, 'Bob', 'bob@example.com');
        INSERT INTO users VALUES (3, 'Carol', 'carol@example.com');
        """)
    conn.commit()
    conn.close()
    from atelier.gateway.adapters.mcp_server import tool_sql

    results: list[CaseResult] = []
    for case in SQL_CASES:
        if progress is not None:
            progress.phase("running MCP tool benchmark", current=f"sql {case.label}")
        patched_case = BenchCase(
            op=case.op,
            label=case.label,
            args=_patch_sql_db(case.args, db_path),
            assert_keys=case.assert_keys,
            custom_assert=case.custom_assert,
            baseline_tokens=case.baseline_tokens,
        )
        results.append(run_case(patched_case, tool_sql))
        if progress is not None:
            progress.step("running MCP tool benchmark", current=f"sql {case.label}")
    return _tool_report("sql", results)


def _run_edit_suite(artifact_root: Path, progress: ProgressReporter | None = None) -> ToolReport:
    root = _runtime_root(artifact_root, "edit")
    workspace = _reset_runtime(root)
    from atelier.gateway.adapters.mcp_server import tool_smart_edit

    results: list[CaseResult] = []
    for case in EDIT_CASES:
        if progress is not None:
            progress.phase("running MCP tool benchmark", current=f"edit {case.label}")
        results.append(_run_edit_case(case, tool_smart_edit, workspace))
        if progress is not None:
            progress.step("running MCP tool benchmark", current=f"edit {case.label}")
    return _tool_report("edit", results)


def _run_code_suite_cases(
    tool_name: str,
    code_cases: list[BenchCase],
    artifact_root: Path,
    progress: ProgressReporter | None = None,
) -> ToolReport:
    root = _runtime_root(artifact_root, tool_name)
    _reset_runtime(root, workspace_root=_repo_workspace_root())
    os.environ["ATELIER_ROOT"] = str(_REAL_ATELIER_ROOT)

    results: list[CaseResult] = []
    for case in code_cases:
        if progress is not None:
            progress.phase("running MCP tool benchmark", current=f"{tool_name} {case.label}")
        results.append(run_case(case, code_tool_dispatch))
        if progress is not None:
            progress.step("running MCP tool benchmark", current=f"{tool_name} {case.label}")
    return _tool_report(tool_name, results)


def _code_tool_cases() -> dict[str, list[BenchCase]]:
    grouped: dict[str, list[BenchCase]] = {}
    for case in CODE_CASES:
        grouped.setdefault(_tool_name_for_case_args(case.args), []).append(case)
    return grouped


CODE_TOOL_CASES = _code_tool_cases()


def _code_suite_runner(tool_name: str) -> Callable[[Path, ProgressReporter], ToolReport]:
    def runner(root: Path, progress: ProgressReporter) -> ToolReport:
        return _run_code_suite_cases(tool_name, CODE_TOOL_CASES[tool_name], root, progress)

    return runner


def _run_code_suite(artifact_root: Path, progress: ProgressReporter | None = None) -> list[ToolReport]:
    return [
        _run_code_suite_cases(tool_name, CODE_TOOL_CASES[tool_name], artifact_root, progress)
        for tool_name in sorted(CODE_TOOL_CASES)
    ]


def _run_compact_suite(artifact_root: Path, progress: ProgressReporter | None = None) -> ToolReport:
    root = _runtime_root(artifact_root, "compact")
    _reset_runtime(root)
    from atelier.gateway.adapters import mcp_server
    from atelier.infra.runtime.run_ledger import RunLedger

    def tool_fn(args: dict[str, Any]) -> Any:
        payload = dict(args)
        seed = dict(payload.pop("_seed", {}) or {})
        session_id = str(payload.get("session_id") or seed.get("session_id") or "bench-compact")
        previous = mcp_server._current_ledger
        ledger = RunLedger(session_id=session_id, agent="benchmark", root=root)
        ledger.task = str(seed.get("task") or "")
        ledger.token_count = int(seed.get("token_count") or 0)
        ledger.current_plan = list(seed.get("current_plan") or [])
        ledger.files_touched = list(seed.get("files_touched") or [])
        ledger.tools_called = list(seed.get("tools_called") or [])
        ledger.commands_run = list(seed.get("commands_run") or [])
        ledger.tests_run = list(seed.get("tests_run") or [])
        ledger.errors_seen = list(seed.get("errors_seen") or [])
        ledger.repeated_failures = list(seed.get("repeated_failures") or [])
        ledger.verified_facts = list(seed.get("verified_facts") or [])
        ledger.open_questions = list(seed.get("open_questions") or [])
        ledger.active_playbooks = list(seed.get("active_playbooks") or [])
        for event in seed.get("tool_events") or []:
            if isinstance(event, dict):
                ledger.record_tool_call(
                    str(event.get("tool") or "tool"),
                    args=dict(event.get("args") or {}),
                    output=str(event.get("output") or ""),
                )
        for event in seed.get("command_events") or []:
            if isinstance(event, dict):
                ledger.record_command(
                    str(event.get("command") or ""),
                    ok=bool(event.get("ok")),
                    stdout=str(event.get("stdout") or ""),
                    stderr=str(event.get("stderr") or ""),
                )
        mcp_server._current_ledger = ledger
        try:
            return mcp_server.tool_compact(payload)
        finally:
            mcp_server._current_ledger = previous

    results: list[CaseResult] = []
    for case in COMPACT_CASES:
        if progress is not None:
            progress.phase("running MCP tool benchmark", current=f"compact {case.label}")
        results.append(run_case(case, tool_fn))
        if progress is not None:
            progress.step("running MCP tool benchmark", current=f"compact {case.label}")
    return _tool_report("compact", results)


def _run_shell_suite(artifact_root: Path, progress: ProgressReporter | None = None) -> ToolReport:
    root = _runtime_root(artifact_root, "shell")
    workspace = _reset_runtime(root)
    sentinel = workspace / "sentinel.txt"
    sentinel.write_text("sentinel_content line1\nsentinel_content line2\n", encoding="utf-8")
    src = workspace / "src"
    src.mkdir(exist_ok=True)
    (src / "module.py").write_text("# module with needle_token\ndef needle_token():\n    return 42\n", encoding="utf-8")
    from atelier.gateway.adapters.mcp_server import tool_bash

    results: list[CaseResult] = []
    for case in SHELL_CASES:
        if progress is not None:
            progress.phase("running MCP tool benchmark", current=f"shell {case.label}")
        patched_case = BenchCase(
            op=case.op,
            label=case.label,
            args=_patch_shell_paths(case.args, workspace),
            assert_keys=case.assert_keys,
            custom_assert=case.custom_assert,
            baseline_tokens=case.baseline_tokens,
        )
        results.append(run_case(patched_case, tool_bash))
        if progress is not None:
            progress.step("running MCP tool benchmark", current=f"shell {case.label}")
    return _tool_report("shell", results)


def _normalize_case_value(value: Any) -> Any:
    repo_root = str(_repo_root())
    temp_root = tempfile.gettempdir()
    home_root = str(Path.home())
    if isinstance(value, dict):
        return {key: _normalize_case_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_case_value(item) for item in value]
    if isinstance(value, str):
        normalized = value.replace(repo_root, "$REPO_ROOT")
        normalized = normalized.replace(home_root, "$HOME")
        normalized = normalized.replace(temp_root, "$TMP")
        return normalized
    return value


def _extract_case_input(case: BenchCase) -> str:
    args = case.args
    if case.op == "edit":
        edits = args.get("edits") or []
        if isinstance(edits, list):
            summary: list[str] = []
            for edit in edits[:3]:
                if not isinstance(edit, dict):
                    continue
                target = str(edit.get("file_path") or edit.get("path") or edit.get("name") or "<unknown>")
                if "old_string" in edit and "new_string" in edit:
                    summary.append(f"{target} replace")
                elif edit.get("overwrite"):
                    summary.append(f"{target} write")
                else:
                    summary.append(target)
            if summary:
                return "; ".join(summary)
    for key in (
        "query",
        "symbol",
        "symbol_name",
        "qualified_name",
        "path",
        "pattern",
        "command",
        "sql",
    ):
        value = args.get(key)
        if value:
            return str(_normalize_case_value(value))
    if args.get("queries"):
        return json.dumps(_normalize_case_value(args["queries"]), ensure_ascii=False, sort_keys=True)
    if args.get("task"):
        status = str(args.get("status") or "").strip()
        task = str(args["task"]).strip()
        return f"{status} {task}".strip()
    if args.get("fact"):
        return str(_normalize_case_value(args["fact"]))
    return json.dumps(_normalize_case_value(args), ensure_ascii=False, sort_keys=True)


def _case_description(case: BenchCase) -> str:
    case_input = _extract_case_input(case)
    return f"{case.op}: {case_input}" if case_input else case.label


def _flatten_reports(reports: list[ToolReport]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for report in reports:
        for result in report.results:
            rows.append(
                {
                    "tool": report.tool_name,
                    "label": result.case.label,
                    "op": result.case.op,
                    "passed": result.passed,
                    "atelier_tokens": result.atelier_tokens,
                    "baseline_tokens": result.baseline_tokens,
                    "input_file_tokens": result.input_file_tokens,
                    "tokens_saved": result.tokens_saved,
                    "savings_pct": round(result.savings_pct, 2),
                    "effective_tokens": round(result.effective_tokens, 2),
                    "elapsed_ms": round(result.elapsed_ms, 2),
                    "spill_probe_tokens": result.spill_probe_tokens,
                    "spill_probe_hits": result.spill_probe_hits,
                    "failure": result.failure,
                    "case_description": _case_description(result.case),
                    "case_input": _extract_case_input(result.case),
                    "stable_args_json": json.dumps(
                        _normalize_case_value(result.case.args), ensure_ascii=False, sort_keys=True
                    ),
                    "baseline_commands_json": json.dumps(
                        [_normalize_case_value(command) for command in result.baseline_commands],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "args_json": json.dumps(result.case.args, ensure_ascii=False, sort_keys=True),
                    "response_json": json.dumps(result.response, ensure_ascii=False, sort_keys=True),
                }
            )
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "tool",
                "label",
                "op",
                "passed",
                "atelier_tokens",
                "baseline_tokens",
                "input_file_tokens",
                "tokens_saved",
                "savings_pct",
                "effective_tokens",
                "elapsed_ms",
                "spill_probe_tokens",
                "spill_probe_hits",
                "failure",
                "case_description",
                "case_input",
                "stable_args_json",
                "baseline_commands_json",
                "args_json",
                "response_json",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes"}


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _summarize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_tool: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_tool.setdefault(str(row.get("tool", "")), []).append(row)

    summary_rows: list[dict[str, Any]] = []
    total_cases = 0
    total_passed = 0
    total_saved = 0
    total_effective = 0.0
    all_savings_values: list[float] = []
    for tool_name, tool_rows in sorted(by_tool.items()):
        cases = len(tool_rows)
        passed = sum(1 for row in tool_rows if _to_bool(row.get("passed")))
        failed = cases - passed
        total_saved_tokens = sum(_to_int(row.get("tokens_saved")) for row in tool_rows)
        total_effective_tokens = sum(_to_float(row.get("effective_tokens")) for row in tool_rows)
        savings_values = [
            _to_float(row.get("savings_pct")) for row in tool_rows if _to_int(row.get("baseline_tokens")) > 0
        ]
        summary_rows.append(
            {
                "tool": tool_name,
                "cases": cases,
                "passed": passed,
                "failed": failed,
                "total_saved_tokens": total_saved_tokens,
                "total_effective_tokens": round(total_effective_tokens, 2),
                "avg_effective_tokens": round(total_effective_tokens / cases, 2) if cases else 0.0,
                "avg_savings_pct": round(sum(savings_values) / len(savings_values), 2) if savings_values else 0.0,
            }
        )
        total_cases += cases
        total_passed += passed
        total_saved += total_saved_tokens
        total_effective += total_effective_tokens
        all_savings_values.extend(savings_values)

    summary_rows.append(
        {
            "tool": "TOTAL",
            "cases": total_cases,
            "passed": total_passed,
            "failed": total_cases - total_passed,
            "total_saved_tokens": total_saved,
            "total_effective_tokens": round(total_effective, 2),
            "avg_effective_tokens": round(total_effective / total_cases, 2) if total_cases else 0.0,
            "avg_savings_pct": (
                round(sum(all_savings_values) / len(all_savings_values), 2) if all_savings_values else 0.0
            ),
        }
    )
    return summary_rows


def _write_summary_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "tool",
                "cases",
                "passed",
                "failed",
                "total_saved_tokens",
                "total_effective_tokens",
                "avg_effective_tokens",
                "avg_savings_pct",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def _public_case_total() -> int:
    return sum(size for _name, size, _runner in _suite_specs())


def _suite_aliases() -> dict[str, list[str]]:
    return {"code": sorted(CODE_TOOL_CASES)}


def _suite_specs() -> list[tuple[str, int, Callable[[Path, ProgressReporter], ToolReport | list[ToolReport]]]]:
    specs: list[tuple[str, int, Callable[[Path, ProgressReporter], ToolReport | list[ToolReport]]]] = [
        ("context", len(CONTEXT_CASES), _run_context_suite),
        (
            "rescue",
            len(RESCUE_CASES),
            lambda root, progress: run_rescue_suite(_runtime_root(root, "rescue"), progress),
        ),
        ("trace", len(TRACE_CASES), _run_trace_suite),
        (
            "verify",
            len(VERIFY_CASES),
            lambda root, progress: run_verify_suite(_runtime_root(root, "verify"), progress),
        ),
        ("memory", len(MEMORY_CASES), _run_memory_suite),
        ("read", len(READ_CASES), _run_read_suite),
        ("edit", len(EDIT_CASES), _run_edit_suite),
        ("sql", len(SQL_CASES), _run_sql_suite),
        ("grep", len(GREP_CASES), _run_grep_suite),
        ("search", len(SEARCH_CASES), _run_search_suite),
        ("compact", len(COMPACT_CASES), _run_compact_suite),
        ("shell", len(SHELL_CASES), _run_shell_suite),
    ]
    # "search" is already a top-level suite (SEARCH_CASES); skip it here to avoid duplicates.
    for tool_name in sorted(CODE_TOOL_CASES):
        if tool_name == "search":
            continue
        specs.append((tool_name, len(CODE_TOOL_CASES[tool_name]), _code_suite_runner(tool_name)))
    return specs


def _select_suite_specs(
    suite_names: list[str] | None,
) -> list[tuple[str, int, Callable[[Path, ProgressReporter], ToolReport | list[ToolReport]]]]:
    specs = _suite_specs()
    if suite_names is None:
        return specs
    aliases = _suite_aliases()
    selected: set[str] = set()
    for name in suite_names:
        clean = name.strip()
        if not clean:
            continue
        if clean in aliases:
            selected.update(aliases[clean])
        else:
            selected.add(clean)
    by_name = {name: (name, size, runner) for name, size, runner in specs}
    unknown = sorted(selected - set(by_name))
    if unknown:
        raise ValueError(f"Unknown MCP suite(s): {', '.join(unknown)}")
    return [by_name[name] for name, _size, _runner in specs if name in selected]


def _plan_suite_shards(
    suite_names: list[str] | None,
    *,
    jobs: int,
) -> list[list[str]]:
    specs = _select_suite_specs(suite_names)
    shard_count = min(max(1, jobs), len(specs))
    shards: list[tuple[int, list[str]]] = [(0, []) for _ in range(shard_count)]
    for name, size, _runner in sorted(specs, key=lambda item: item[1], reverse=True):
        shard_index = min(range(len(shards)), key=lambda index: shards[index][0])
        total, names = shards[shard_index]
        names.append(name)
        shards[shard_index] = (total + size, names)
    return [names for _total, names in shards if names]


def run_public_surface(
    artifact_root: Path,
    *,
    suite_names: list[str] | None = None,
    progress: ProgressReporter | None = None,
) -> list[ToolReport]:
    selected_specs = _select_suite_specs(suite_names)
    reporter = progress or ProgressReporter("mcp", total=sum(size for _name, size, _runner in selected_specs))
    reporter.start("starting public MCP benchmark", current=str(artifact_root))
    reports: list[ToolReport] = []
    for _suite_name, _size, runner in selected_specs:
        result = runner(artifact_root, reporter)
        if isinstance(result, list):
            reports.extend(result)
        else:
            reports.append(result)
    reporter.finish("public MCP benchmark complete")
    return reports


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_status_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        return None


def _render_shard_progress(
    shard_groups: dict[int, list[str]],
    status_files: dict[int, Path],
    *,
    completed_shards: int,
    total_shards: int,
    total_cases: int,
) -> str:
    statuses: list[dict[str, Any]] = []
    for index, status_file in sorted(status_files.items()):
        status = _read_status_file(status_file)
        if status is None:
            continue
        status["index"] = index
        status["suite_names"] = shard_groups.get(index, [])
        statuses.append(status)
    if not statuses:
        return ""
    completed_cases = sum(_to_int(status.get("done")) for status in statuses)
    parts = [f"shards {completed_shards}/{total_shards} | cases {completed_cases}/{total_cases}"]
    for status in statuses:
        suite_names = ",".join(status.get("suite_names") or [])
        current = str(status.get("current") or "").strip()
        parts.append(
            f"shard-{status['index']} [{suite_names}] "
            f"{_to_int(status.get('done'))}/{_to_int(status.get('total'))} "
            f"{status.get('status', 'running')}" + (f" current {current}" if current else "")
        )
    return " ; ".join(parts)


def _run_parallel_surface(
    artifact_root: Path,
    csv_out: Path,
    *,
    suite_names: list[str] | None,
    jobs: int,
) -> list[dict[str, str]]:
    shard_groups = _plan_suite_shards(suite_names, jobs=jobs)
    shard_root = artifact_root / "parallel-shards"
    shard_root.mkdir(parents=True, exist_ok=True)
    log_root = shard_root / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    progress = ProgressReporter("mcp", total=len(shard_groups))
    progress.start(
        "starting parallel MCP benchmark",
        current=f"{len(shard_groups)} shard(s) x {jobs} job(s)",
    )

    commands: list[tuple[int, list[str], Path, Path]] = []
    shard_names: dict[int, list[str]] = {}
    status_files: dict[int, Path] = {}
    for index, names in enumerate(shard_groups, start=1):
        child_artifact_root = shard_root / f"shard-{index}"
        child_csv_out = shard_root / f"shard-{index}.csv"
        status_file = shard_root / f"shard-{index}.status.json"
        log_file = log_root / f"shard-{index}.log"
        shard_names[index] = names
        status_files[index] = status_file
        command = [
            sys.executable,
            "-m",
            "benchmarks.mcp_tools.export_public_mcp_csv",
            "--artifact-root",
            str(child_artifact_root),
            "--csv-out",
            str(child_csv_out),
            "--jobs",
            "1",
            "--suites",
            ",".join(names),
            "--progress-file",
            str(status_file),
            "--shard-name",
            f"shard-{index}",
        ]
        commands.append((index, command, child_csv_out, log_file))

    def _run_child(index: int, command: list[str], expected_csv: Path, log_file: Path) -> tuple[int, Path]:
        completed = subprocess.run(
            command,
            cwd=_repo_root(),
            capture_output=True,
            text=True,
            check=False,
        )
        log_file.write_text(
            "\n".join(
                [
                    "$ " + " ".join(command),
                    "",
                    "[stdout]",
                    completed.stdout,
                    "",
                    "[stderr]",
                    completed.stderr,
                ]
            ),
            encoding="utf-8",
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"MCP shard {index} failed with exit code {completed.returncode}\n"
                f"Log: {log_file}\n"
                f"STDOUT:\n{completed.stdout[-4000:]}\nSTDERR:\n{completed.stderr[-4000:]}"
            )
        if not expected_csv.is_file():
            raise RuntimeError(f"MCP shard {index} did not produce {expected_csv}")
        return index, expected_csv

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(jobs, len(commands))) as executor:
        futures = {
            executor.submit(_run_child, index, command, shard_csv, log_file): (index, shard_csv)
            for index, command, shard_csv, log_file in commands
        }
        completed_csvs: list[Path] = []
        last_snapshot = ""
        pending = set(futures)
        while pending:
            done, pending = concurrent.futures.wait(
                pending,
                timeout=1.0,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            snapshot = _render_shard_progress(
                shard_names,
                status_files,
                completed_shards=len(completed_csvs),
                total_shards=len(commands),
                total_cases=sum(size for _name, size, _runner in _select_suite_specs(suite_names)),
            )
            if snapshot and snapshot != last_snapshot:
                progress.phase("running MCP shards", current=snapshot)
                last_snapshot = snapshot
            for future in done:
                index, shard_csv = future.result()
                completed_csvs.append(shard_csv)
                progress.step("MCP shard complete", current=f"shard-{index}")

    rows: list[dict[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()
    for shard_csv in sorted(completed_csvs):
        for row in _read_csv_rows(shard_csv):
            key = (row.get("tool", ""), row.get("label", ""))
            if key in seen_keys:
                raise RuntimeError(f"Duplicate MCP benchmark row detected for {key}")
            seen_keys.add(key)
            rows.append(row)
    rows.sort(key=lambda row: (row.get("tool", ""), row.get("label", "")))
    _write_csv(csv_out, rows)
    progress.finish("parallel MCP benchmark complete")
    return rows


def _resolve_jobs(requested_jobs: int, suite_names: list[str] | None) -> int:
    if requested_jobs > 0:
        return requested_jobs
    suite_count = len(_select_suite_specs(suite_names))
    detected = max(os.cpu_count() or 1, 1)
    return max(1, min(suite_count, 32, detected))


def main() -> int:
    repo_root = _repo_root()
    artifact_root_default = default_benchmark_root(repo_root)
    csv_default = artifact_root_default / "public_mcp_benchmark.csv"
    parser = argparse.ArgumentParser(description="Run public MCP benchmark surface and export CSV.")
    parser.add_argument("--artifact-root", default=str(artifact_root_default))
    parser.add_argument("--csv-out", default=str(csv_default))
    parser.add_argument("--jobs", type=int, default=0)
    parser.add_argument("--suites", default="")
    parser.add_argument("--progress-file", default="")
    parser.add_argument("--shard-name", default="")
    args = parser.parse_args()

    artifact_root = Path(args.artifact_root).expanduser().resolve()
    csv_out = Path(args.csv_out).expanduser().resolve()
    suite_names = [name.strip() for name in str(args.suites).split(",") if name.strip()] or None
    resolved_jobs = _resolve_jobs(args.jobs, suite_names)
    if resolved_jobs > 1:
        rows = _run_parallel_surface(artifact_root, csv_out, suite_names=suite_names, jobs=resolved_jobs)
        _write_summary_csv(csv_out.with_name("summary.csv"), _summarize_rows(rows))
        print(f"Parallel MCP benchmark complete: {len(rows)} rows across {resolved_jobs} job(s)")
        print(f"CSV written to {csv_out}")
        return 0

    progress: ProgressReporter | None = None
    if args.progress_file:
        progress = ShardStatusReporter(
            str(args.shard_name or "shard"),
            total=sum(size for _name, size, _runner in _select_suite_specs(suite_names)),
            status_file=Path(str(args.progress_file)).expanduser().resolve(),
        )
    reports = run_public_surface(artifact_root, suite_names=suite_names, progress=progress)
    print(render_summary(reports))
    rows = _flatten_reports(reports)
    _write_csv(csv_out, rows)
    _write_summary_csv(csv_out.with_name("summary.csv"), _summarize_rows(rows))
    print(f"CSV written to {csv_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
