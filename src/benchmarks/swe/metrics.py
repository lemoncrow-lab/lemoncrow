"""Per-attempt run metrics + JSONL writer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RunMetrics(BaseModel):
    """One row per (task, mode, attempt). Strict schema."""

    model_config = ConfigDict(extra="forbid")

    # identity
    task_id: str
    mode: str
    attempt: int = 1

    # outcome
    resolved: bool = False
    patch_generated: bool = False
    patch_path: str | None = None

    # cost / tokens / time
    tokens_input: int = 0
    tokens_output: int = 0
    estimated_cost_usd: float = 0.0
    wall_time_seconds: float = 0.0

    # work shape
    turns: int = 0
    tool_calls: int = 0
    file_reads: int = 0
    searches: int = 0
    repeated_commands: int = 0

    # atelier-specific
    monitor_events: int = 0
    compression_events: int = 0
    reasonblocks_retrieved: int = 0
    reasonblock_hits: int = 0
    rubric_status: str | None = None
    rescue_count: int = 0
    trace_id: str | None = None
    failure_cluster_ids: list[str] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    budget_cap_hit: bool = False
    error: str | None = None

    def to_jsonl(self) -> str:
        return self.model_dump_json()


def write_metrics(rows: list[RunMetrics], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(r.to_jsonl())
            f.write("\n")
    return path


def read_metrics(path: Path) -> list[RunMetrics]:
    out: list[RunMetrics] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(RunMetrics.model_validate_json(line))
    return out


def aggregate(rows: list[RunMetrics]) -> dict[str, Any]:
    """Mode-level aggregates used by the report."""
    by_mode: dict[str, dict[str, Any]] = {}
    for r in rows:
        m = by_mode.setdefault(
            r.mode,
            {
                "attempts": 0,
                "resolved": 0,
                "patches": 0,
                "tokens_input": 0,
                "tokens_output": 0,
                "cost_usd": 0.0,
                "wall_time_seconds": 0.0,
                "turns": 0,
                "tool_calls": 0,
                "monitor_events": 0,
                "compression_events": 0,
                "reasonblock_hits": 0,
                "rescue_count": 0,
                "rubric_pass": 0,
                "budget_cap_hit": 0,
            },
        )
        m["attempts"] += 1
        m["resolved"] += int(r.resolved)
        m["patches"] += int(r.patch_generated)
        m["tokens_input"] += r.tokens_input
        m["tokens_output"] += r.tokens_output
        m["cost_usd"] += r.estimated_cost_usd
        m["wall_time_seconds"] += r.wall_time_seconds
        m["turns"] += r.turns
        m["tool_calls"] += r.tool_calls
        m["monitor_events"] += r.monitor_events
        m["compression_events"] += r.compression_events
        m["reasonblock_hits"] += r.reasonblock_hits
        m["rescue_count"] += r.rescue_count
        m["rubric_pass"] += int(r.rubric_status == "pass")
        m["budget_cap_hit"] += int(r.budget_cap_hit)
    for m in by_mode.values():
        attempts = max(1, m["attempts"])
        m["resolve_rate"] = round(m["resolved"] / attempts, 4)
        m["patch_rate"] = round(m["patches"] / attempts, 4)
        m["cost_usd"] = round(m["cost_usd"], 6)
        m["wall_time_seconds"] = round(m["wall_time_seconds"], 3)
    return by_mode


def to_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path
