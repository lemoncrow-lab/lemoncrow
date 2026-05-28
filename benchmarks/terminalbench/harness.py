"""Pydantic config models and YAML loader for TerminalBench task registry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class TaskSpec(BaseModel):
    """Specification for a single TerminalBench task."""

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str = ""
    category: str = "code_editing"
    timeout_seconds: int = Field(default=1800, ge=60)


class RunConfig(BaseModel):
    """Configuration for a TerminalBench benchmark run."""

    model_config = ConfigDict(extra="forbid")

    tasks_path: str = "benchmarks/terminalbench/tasks.yaml"
    output_dir: str = "benchmarks/terminalbench/outputs"
    attempts_per_task: int = Field(default=1, ge=1)
    timeout_seconds: int = Field(default=1800, ge=60)
    dataset_name: str = "terminal-bench-core"
    dataset_version: str = "0.1.1"


def load_tasks(path: str | Path) -> list[TaskSpec]:
    """Load and validate task list from a tasks.yaml file.

    The YAML file must have a ``tasks`` key containing a flat list of task ID
    strings. Each string is wrapped into a ``TaskSpec(id=task_id)`` object.

    Args:
        path: Path to the tasks.yaml file.

    Returns:
        List of validated ``TaskSpec`` objects.

    Raises:
        FileNotFoundError: If the path does not exist.
        ValueError: If the YAML content is not a mapping.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"tasks file not found: {p}")
    raw: Any = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"tasks file must be a YAML mapping, got {type(raw).__name__}")
    task_ids: list[str] = raw.get("tasks", [])
    return [TaskSpec(id=task_id) for task_id in task_ids]


def load_dataset_meta(path: str | Path) -> dict[str, str]:
    """Load dataset metadata from a tasks.yaml file.

    Args:
        path: Path to the tasks.yaml file.

    Returns:
        Dict with ``name`` and ``version`` keys from the ``dataset`` section.

    Raises:
        FileNotFoundError: If the path does not exist.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"tasks file not found: {p}")
    raw: Any = yaml.safe_load(p.read_text()) or {}
    result: dict[str, str] = raw.get("dataset", {})
    return result
