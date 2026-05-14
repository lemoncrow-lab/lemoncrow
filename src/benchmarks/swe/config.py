"""Benchmark configuration loader.

Reads YAML files of the shape declared in :class:`BenchConfig`. All fields
are validated through pydantic so a typo in a config fails fast.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from benchmarks.swe.modes import Mode

AgentHost = Literal["claude", "codex", "opencode", "copilot", "gemini", "mock"]


class BenchConfig(BaseModel):
    """Strict YAML schema for a SWE-bench harness run."""

    model_config = ConfigDict(extra="forbid")

    dataset_name: str = "swe_bench_lite"
    split: str = "dev"
    task_limit: int = Field(default=20, ge=1)
    task_ids: list[str] | None = None

    agent_host: AgentHost = "mock"
    model: str = "mock-1"

    modes: list[Mode] = Field(default_factory=lambda: [Mode.VANILLA, Mode.ATELIER_FORCED_WORKFLOW])
    attempts_per_task: int = Field(default=1, ge=1)
    max_turns: int = Field(default=20, ge=1)
    max_cost_usd: float = Field(default=2.0, ge=0)
    timeout_seconds: int = Field(default=600, ge=1)

    output_dir: str = "benchmarks/swe/outputs"
    use_service: bool = False
    use_remote_mcp: bool = False
    warm_reasonblocks_path: str | None = None

    seed: int = 7
    custom_tasks_path: str | None = None

    @field_validator("modes")
    @classmethod
    def _at_least_one_mode(cls, v: list[Mode]) -> list[Mode]:
        if not v:
            raise ValueError("at least one mode must be configured")
        return v

    def warm_required_but_missing(self) -> bool:
        return Mode.ATELIER_WARM_REASONBLOCKS in self.modes and not self.warm_reasonblocks_path


def load_config(path: str | Path) -> BenchConfig:
    """Load and validate a benchmark YAML file."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"config not found: {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config must be a YAML mapping, got {type(raw).__name__}")
    return BenchConfig(**raw)
