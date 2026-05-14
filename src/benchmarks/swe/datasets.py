"""Dataset loaders.

Three sources are supported:

* ``swe_bench_lite`` and ``swe_bench_verified`` — load via the optional
  ``datasets`` package (HuggingFace) when available; fall back to a built-in
  tiny mock dataset so unit tests run offline.
* ``custom`` — read a JSONL file at ``custom_tasks_path``.

A task is a plain dict with at minimum ``instance_id``, ``problem_statement``,
``repo``, and ``base_commit``. Gold patches are kept in a separate field
(``patch``) and **never** passed downstream.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmarks.swe.config import BenchConfig

# Forbidden fields the agent must never see during solving.
GOLD_KEYS = ("patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS")


@dataclass(frozen=True)
class Task:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    hints_text: str = ""
    extra: dict[str, Any] | None = None  # opaque, agent-safe metadata

    def to_agent_payload(self) -> dict[str, Any]:
        """Strip gold/eval-only fields before handing to the agent."""
        return {
            "instance_id": self.instance_id,
            "repo": self.repo,
            "base_commit": self.base_commit,
            "problem_statement": self.problem_statement,
            "hints_text": self.hints_text,
        }


# ---------------- mock built-in dataset (offline, deterministic) ----------- #

_MOCK_TASKS: list[dict[str, Any]] = [
    {
        "instance_id": "mock__add-1",
        "repo": "mock/calc",
        "base_commit": "deadbeef",
        "problem_statement": (
            "Function `add(a, b)` returns `a - b` instead of `a + b`. Fix it in calc.py."
        ),
        "hints_text": "tests in tests/test_calc.py",
        # gold patch present so eval can use it; agent never sees this field.
        "patch": "--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-def add(a,b): return a-b\n+def add(a,b): return a+b\n",
    },
    {
        "instance_id": "mock__off-by-one-2",
        "repo": "mock/range",
        "base_commit": "cafef00d",
        "problem_statement": "Loop iterates n-1 times instead of n times. Fix in range_loop.py.",
        "patch": "--- a/range_loop.py\n+++ b/range_loop.py\n@@\n-for i in range(n-1):\n+for i in range(n):\n",
    },
    {
        "instance_id": "mock__null-deref-3",
        "repo": "mock/svc",
        "base_commit": "12345678",
        "problem_statement": "NullPointerException when user has no profile. Guard in svc.py.",
        "patch": "--- a/svc.py\n+++ b/svc.py\n@@\n-return user.profile.name\n+return user.profile.name if user.profile else 'anon'\n",
    },
]


def _load_mock(limit: int, ids: list[str] | None) -> list[dict[str, Any]]:
    rows = _MOCK_TASKS
    if ids:
        rows = [r for r in rows if r["instance_id"] in ids]
    return rows[:limit]


def _load_jsonl(path: Path, limit: int, ids: list[str] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    if ids:
        rows = [r for r in rows if r.get("instance_id") in ids]
    return rows[:limit]


def _load_huggingface(
    name: str, split: str, limit: int, ids: list[str] | None
) -> list[dict[str, Any]]:
    try:
        from datasets import load_dataset
    except ImportError:
        return []
    hf_name = {
        "swe_bench_lite": "princeton-nlp/SWE-bench_Lite",
        "swe_bench_verified": "princeton-nlp/SWE-bench_Verified",
    }.get(name, name)
    ds = load_dataset(hf_name, split=split)
    rows: list[dict[str, Any]] = []
    for row in ds:
        if ids and row.get("instance_id") not in ids:
            continue
        rows.append(dict(row))
        if len(rows) >= limit:
            break
    return rows


def _row_to_task(row: dict[str, Any]) -> Task:
    extra = {
        k: v
        for k, v in row.items()
        if k
        not in (
            "instance_id",
            "repo",
            "base_commit",
            "problem_statement",
            "hints_text",
            *GOLD_KEYS,
        )
    }
    return Task(
        instance_id=row["instance_id"],
        repo=row.get("repo", ""),
        base_commit=row.get("base_commit", ""),
        problem_statement=row.get("problem_statement", ""),
        hints_text=row.get("hints_text", ""),
        extra=extra or None,
    )


def load_tasks(cfg: BenchConfig) -> list[Task]:
    """Resolve the configured dataset to a list of :class:`Task`."""
    name = cfg.dataset_name
    ids = cfg.task_ids
    limit = cfg.task_limit
    if cfg.custom_tasks_path:
        rows = _load_jsonl(Path(cfg.custom_tasks_path), limit, ids)
    elif name in {"mock", "mock_swe"}:
        rows = _load_mock(limit, ids)
    elif name in {"swe_bench_lite", "swe_bench_verified"}:
        rows = _load_huggingface(name, cfg.split, limit, ids) or _load_mock(limit, ids)
    else:
        # treat as a JSONL path
        candidate = Path(name)
        rows = _load_jsonl(candidate, limit, ids) if candidate.is_file() else _load_mock(limit, ids)
    return [_row_to_task(r) for r in rows]


def gold_patch_lookup(cfg: BenchConfig) -> dict[str, str]:
    """Return ``{instance_id: gold_patch}`` for evaluation only.

    This map is **never** passed to the agent. It is consumed by
    ``swebench_eval`` when verifying predictions.
    """
    out: dict[str, str] = {}
    if cfg.custom_tasks_path:
        rows = _load_jsonl(Path(cfg.custom_tasks_path), cfg.task_limit, cfg.task_ids)
    elif cfg.dataset_name in {"swe_bench_lite", "swe_bench_verified"}:
        rows = _load_huggingface(
            cfg.dataset_name, cfg.split, cfg.task_limit, cfg.task_ids
        ) or _load_mock(cfg.task_limit, cfg.task_ids)
    else:
        rows = _load_mock(cfg.task_limit, cfg.task_ids)
    for r in rows:
        if "patch" in r:
            out[r["instance_id"]] = r["patch"]
    return out


def iter_tasks(cfg: BenchConfig) -> Iterator[Task]:
    yield from load_tasks(cfg)
