"""V3 honest context-savings replay harness.

The harness simulates a host CLI dispatch loop over recorded synthetic transcripts.
Atelier itself does not call an LLM; token accounting is deterministic over recorded
host-native and Atelier-tool outputs.
"""

from __future__ import annotations

import csv
import json
import os
import shlex
import sqlite3
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tiktoken

from atelier.infra.runtime.cost_tracker import estimate_cost
from atelier.infra.storage.ids import make_uuid7
from atelier.infra.storage.sqlite_store import SQLiteStore

_CORPUS_DIR = Path(__file__).parent / "replay_corpus"
_ENCODING = tiktoken.get_encoding("cl100k_base")


@dataclass(frozen=True)
class ReplayPromptResult:
    id: str
    task_type: str
    baseline_input_tokens: int
    optimized_input_tokens: int
    lever: str

    @property
    def tokens_saved(self) -> int:
        return self.baseline_input_tokens - self.optimized_input_tokens

    @property
    def reduction_pct(self) -> float:
        if self.baseline_input_tokens == 0:
            return 0.0
        return self.tokens_saved / self.baseline_input_tokens * 100.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_type": self.task_type,
            "baseline_input_tokens": self.baseline_input_tokens,
            "optimized_input_tokens": self.optimized_input_tokens,
            "tokens_saved": self.tokens_saved,
            "reduction_pct": round(self.reduction_pct, 2),
            "lever": self.lever,
        }


@dataclass
class ReplayResult:
    session_id: str
    n_prompts: int
    median_input_tokens_baseline: int
    median_input_tokens_optimized: int
    reduction_pct: float
    lever_totals: dict[str, int] = field(default_factory=dict)
    prompts: list[ReplayPromptResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "n_prompts": self.n_prompts,
            "median_input_tokens_baseline": self.median_input_tokens_baseline,
            "median_input_tokens_optimized": self.median_input_tokens_optimized,
            "reduction_pct": round(self.reduction_pct, 2),
            "lever_totals": self.lever_totals,
            "prompts": [prompt.to_dict() for prompt in self.prompts],
        }


@dataclass(frozen=True)
class CommandMeasurement:
    mode: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost_usd: float
    elapsed_ms: int
    success: bool
    exit_code: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "elapsed_ms": self.elapsed_ms,
            "success": self.success,
            "exit_code": self.exit_code,
        }


@dataclass(frozen=True)
class PairedCommandPromptResult:
    id: str
    task_type: str
    task: str
    baseline: CommandMeasurement
    atelier: CommandMeasurement

    @property
    def tokens_saved(self) -> int:
        return self.baseline.total_tokens - self.atelier.total_tokens

    @property
    def reduction_pct(self) -> float:
        if self.baseline.total_tokens == 0:
            return 0.0
        return self.tokens_saved / self.baseline.total_tokens * 100.0

    @property
    def cost_saved_usd(self) -> float:
        return self.baseline.cost_usd - self.atelier.cost_usd

    @property
    def time_saved_ms(self) -> int:
        return self.baseline.elapsed_ms - self.atelier.elapsed_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_type": self.task_type,
            "task": self.task,
            "baseline": self.baseline.to_dict(),
            "atelier": self.atelier.to_dict(),
            "tokens_saved": self.tokens_saved,
            "reduction_pct": round(self.reduction_pct, 2),
            "cost_saved_usd": round(self.cost_saved_usd, 6),
            "time_saved_ms": self.time_saved_ms,
        }


@dataclass
class PairedCommandBenchmarkResult:
    session_id: str
    model: str
    n_prompts: int
    total_tokens_baseline: int
    total_tokens_atelier: int
    total_cost_baseline_usd: float
    total_cost_atelier_usd: float
    total_time_baseline_ms: int
    total_time_atelier_ms: int
    baseline_success_rate: float
    atelier_success_rate: float
    prompts: list[PairedCommandPromptResult] = field(default_factory=list)

    @property
    def tokens_saved(self) -> int:
        return self.total_tokens_baseline - self.total_tokens_atelier

    @property
    def reduction_pct(self) -> float:
        if self.total_tokens_baseline == 0:
            return 0.0
        return self.tokens_saved / self.total_tokens_baseline * 100.0

    @property
    def cost_saved_usd(self) -> float:
        return self.total_cost_baseline_usd - self.total_cost_atelier_usd

    @property
    def time_saved_ms(self) -> int:
        return self.total_time_baseline_ms - self.total_time_atelier_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "model": self.model,
            "n_prompts": self.n_prompts,
            "total_tokens_baseline": self.total_tokens_baseline,
            "total_tokens_atelier": self.total_tokens_atelier,
            "tokens_saved": self.tokens_saved,
            "reduction_pct": round(self.reduction_pct, 2),
            "total_cost_baseline_usd": round(self.total_cost_baseline_usd, 6),
            "total_cost_atelier_usd": round(self.total_cost_atelier_usd, 6),
            "cost_saved_usd": round(self.cost_saved_usd, 6),
            "total_time_baseline_ms": self.total_time_baseline_ms,
            "total_time_atelier_ms": self.total_time_atelier_ms,
            "time_saved_ms": self.time_saved_ms,
            "baseline_success_rate": round(self.baseline_success_rate, 4),
            "atelier_success_rate": round(self.atelier_success_rate, 4),
            "prompts": [prompt.to_dict() for prompt in self.prompts],
        }


def _count_tokens(text: str) -> int:
    return len(_ENCODING.encode(text))


def _load_corpus(corpus_dir: Path = _CORPUS_DIR) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(corpus_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def _config_fingerprint(root: Path) -> str:
    import hashlib

    config = root / "config.toml"
    data = config.read_bytes() if config.exists() else b""
    return hashlib.sha256(data).hexdigest()[:16]


def _default_command_tasks(max_prompts: int = 5) -> list[dict[str, str]]:
    rows = _load_corpus()[:max_prompts]
    return [
        {
            "id": str(row["id"]),
            "task_type": str(row["task_type"]),
            "task": str(row["task"]),
        }
        for row in rows
    ]


def _declared_metrics(output: str) -> dict[str, Any]:
    stripped = output.strip()
    candidates = [stripped, *reversed(stripped.splitlines())]
    for candidate in candidates:
        candidate = candidate.strip()
        if not (candidate.startswith("{") and candidate.endswith("}")):
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _run_command_measurement(
    *,
    command_template: str,
    prompt: str,
    root: Path,
    mode: str,
    model: str,
    timeout_s: float,
) -> CommandMeasurement:
    if not command_template.strip():
        raise ValueError(f"{mode} command must not be empty")
    env = {
        **os.environ,
        "ATELIER_BENCH_PROMPT": prompt,
        "ATELIER_BENCH_MODE": mode,
        "ATELIER_ROOT": str(root),
    }
    rendered = command_template.format(prompt=prompt, root=str(root), mode=mode)
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            shlex.split(rendered),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
            env=env,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
        metrics = _declared_metrics(output)
        input_tokens = int(metrics.get("input_tokens", _count_tokens(prompt)))
        output_tokens = int(metrics.get("output_tokens", _count_tokens(output)))
        cache_read_tokens = int(metrics.get("cache_read_tokens", 0))
        cost_usd = float(metrics.get("cost_usd", estimate_cost(model, input_tokens, output_tokens, cache_read_tokens)))
        success = bool(metrics.get("success", proc.returncode == 0))
        return CommandMeasurement(
            mode=mode,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cost_usd=cost_usd,
            elapsed_ms=elapsed_ms,
            success=success,
            exit_code=proc.returncode,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        output = "\n".join(str(part) for part in (exc.stdout, exc.stderr) if part)
        input_tokens = _count_tokens(prompt)
        output_tokens = _count_tokens(output)
        return CommandMeasurement(
            mode=mode,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=0,
            cost_usd=estimate_cost(model, input_tokens, output_tokens, 0),
            elapsed_ms=elapsed_ms,
            success=False,
            exit_code=124,
        )


def run_paired_command_benchmark(
    root: str | Path | None = None,
    *,
    baseline_command: str,
    atelier_command: str,
    tasks: list[dict[str, str]] | None = None,
    model: str = "claude-sonnet-4.6",
    timeout_s: float = 600.0,
    max_prompts: int = 5,
) -> PairedCommandBenchmarkResult:
    """Run real paired commands and compare baseline vs Atelier-enabled metrics.

    Commands receive ``ATELIER_BENCH_PROMPT``, ``ATELIER_BENCH_MODE``, and
    ``ATELIER_ROOT`` in the environment. They may also emit a JSON object on
    stdout/stderr with ``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
    ``cost_usd``, and ``success`` for provider-native accounting. If omitted,
    this harness estimates prompt/output tokens locally from captured text.
    """
    resolved_root = Path(root or os.environ.get("ATELIER_ROOT", str(Path.home() / ".atelier")))
    store = SQLiteStore(resolved_root)
    store.init()
    selected_tasks = tasks or _default_command_tasks(max_prompts=max_prompts)
    if not selected_tasks:
        raise ValueError("paired command benchmark requires at least one task")

    prompts: list[PairedCommandPromptResult] = []
    for index, task in enumerate(selected_tasks, start=1):
        prompt_text = str(task["task"])
        baseline = _run_command_measurement(
            command_template=baseline_command,
            prompt=prompt_text,
            root=resolved_root,
            mode="baseline",
            model=model,
            timeout_s=timeout_s,
        )
        atelier = _run_command_measurement(
            command_template=atelier_command,
            prompt=prompt_text,
            root=resolved_root,
            mode="atelier",
            model=model,
            timeout_s=timeout_s,
        )
        prompts.append(
            PairedCommandPromptResult(
                id=str(task.get("id") or f"task-{index}"),
                task_type=str(task.get("task_type") or "ad_hoc"),
                task=prompt_text,
                baseline=baseline,
                atelier=atelier,
            )
        )

    n_prompts = len(prompts)
    session_id = f"bench-{make_uuid7()}"
    result = PairedCommandBenchmarkResult(
        session_id=session_id,
        model=model,
        n_prompts=n_prompts,
        total_tokens_baseline=sum(prompt.baseline.total_tokens for prompt in prompts),
        total_tokens_atelier=sum(prompt.atelier.total_tokens for prompt in prompts),
        total_cost_baseline_usd=sum(prompt.baseline.cost_usd for prompt in prompts),
        total_cost_atelier_usd=sum(prompt.atelier.cost_usd for prompt in prompts),
        total_time_baseline_ms=sum(prompt.baseline.elapsed_ms for prompt in prompts),
        total_time_atelier_ms=sum(prompt.atelier.elapsed_ms for prompt in prompts),
        baseline_success_rate=sum(1 for prompt in prompts if prompt.baseline.success) / n_prompts,
        atelier_success_rate=sum(1 for prompt in prompts if prompt.atelier.success) / n_prompts,
        prompts=prompts,
    )
    _persist_paired_command_result(store.db_path, result, datetime.now(UTC), resolved_root)
    _write_paired_command_json(resolved_root, result)
    return result


def run_replay(
    root: str | Path | None = None,
    *,
    corpus_dir: Path = _CORPUS_DIR,
    csv_path: Path | None = None,
) -> ReplayResult:
    resolved_root = Path(root or os.environ.get("ATELIER_ROOT", str(Path.home() / ".atelier")))
    store = SQLiteStore(resolved_root)
    store.init()
    rows = _load_corpus(corpus_dir)
    if len(rows) < 50:
        raise ValueError(f"replay corpus must contain at least 50 transcripts, got {len(rows)}")

    prompts: list[ReplayPromptResult] = []
    lever_totals: dict[str, int] = {}
    for row in rows:
        baseline_text = "\n".join([str(row["task"]), str(row["baseline"])])
        atelier_text = "\n".join([str(row["task"]), str(row["atelier"])])
        baseline_tokens = _count_tokens(baseline_text)
        optimized_tokens = _count_tokens(atelier_text)
        result = ReplayPromptResult(
            id=str(row["id"]),
            task_type=str(row["task_type"]),
            baseline_input_tokens=baseline_tokens,
            optimized_input_tokens=optimized_tokens,
            lever=str(row["lever"]),
        )
        prompts.append(result)
        lever_totals[result.lever] = lever_totals.get(result.lever, 0) + result.tokens_saved

    total_baseline = sum(prompt.baseline_input_tokens for prompt in prompts)
    total_optimized = sum(prompt.optimized_input_tokens for prompt in prompts)
    reduction_pct = (total_baseline - total_optimized) / total_baseline * 100.0
    session_id = f"bench-{make_uuid7()}"
    completed_at = datetime.now(UTC)
    replay_result = ReplayResult(
        session_id=session_id,
        n_prompts=len(prompts),
        median_input_tokens_baseline=int(statistics.median(p.baseline_input_tokens for p in prompts)),
        median_input_tokens_optimized=int(statistics.median(p.optimized_input_tokens for p in prompts)),
        reduction_pct=reduction_pct,
        lever_totals=lever_totals,
        prompts=prompts,
    )
    _persist_result(store.db_path, replay_result, completed_at, resolved_root)
    if csv_path is not None:
        _write_csv(csv_path, replay_result)
    return replay_result


def _persist_result(db_path: Path, result: ReplayResult, completed_at: datetime, root: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO benchmark_run (
                id, started_at, completed_at, suite, git_sha, config_fingerprint,
                n_prompts, median_input_tokens_baseline, median_input_tokens_optimized,
                reduction_pct, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.session_id,
                completed_at.isoformat(),
                completed_at.isoformat(),
                "savings_replay_v3",
                _git_sha(),
                _config_fingerprint(root),
                result.n_prompts,
                result.median_input_tokens_baseline,
                result.median_input_tokens_optimized,
                result.reduction_pct,
                "synthetic host-transcript replay; deterministic token accounting",
            ),
        )
        for prompt in result.prompts:
            conn.execute(
                """
                INSERT INTO benchmark_prompt_result (
                    id, session_id, prompt_id, task_type, input_tokens_baseline,
                    input_tokens_optimized, reduction_pct, duration_ms, created_at,
                    baseline_input_tokens, optimized_input_tokens, lever_attribution_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"bpr-{make_uuid7()}",
                    result.session_id,
                    prompt.id,
                    prompt.task_type,
                    prompt.baseline_input_tokens,
                    prompt.optimized_input_tokens,
                    prompt.reduction_pct,
                    0,
                    completed_at.isoformat(),
                    prompt.baseline_input_tokens,
                    prompt.optimized_input_tokens,
                    json.dumps({prompt.lever: prompt.tokens_saved}, sort_keys=True),
                ),
            )


def _persist_paired_command_result(
    db_path: Path,
    result: PairedCommandBenchmarkResult,
    completed_at: datetime,
    root: Path,
) -> None:
    median_baseline = int(statistics.median(prompt.baseline.total_tokens for prompt in result.prompts))
    median_atelier = int(statistics.median(prompt.atelier.total_tokens for prompt in result.prompts))
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO benchmark_run (
                id, started_at, completed_at, suite, git_sha, config_fingerprint,
                n_prompts, median_input_tokens_baseline, median_input_tokens_optimized,
                reduction_pct, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.session_id,
                completed_at.isoformat(),
                completed_at.isoformat(),
                "paired_command_savings_v1",
                _git_sha(),
                _config_fingerprint(root),
                result.n_prompts,
                median_baseline,
                median_atelier,
                result.reduction_pct,
                "paired real command benchmark; commands may provide provider-native token/cost metrics",
            ),
        )
        for prompt in result.prompts:
            conn.execute(
                """
                INSERT INTO benchmark_prompt_result (
                    id, session_id, prompt_id, task_type, input_tokens_baseline,
                    input_tokens_optimized, reduction_pct, duration_ms, created_at,
                    baseline_input_tokens, optimized_input_tokens, lever_attribution_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"bpr-{make_uuid7()}",
                    result.session_id,
                    prompt.id,
                    prompt.task_type,
                    prompt.baseline.total_tokens,
                    prompt.atelier.total_tokens,
                    prompt.reduction_pct,
                    prompt.baseline.elapsed_ms + prompt.atelier.elapsed_ms,
                    completed_at.isoformat(),
                    prompt.baseline.total_tokens,
                    prompt.atelier.total_tokens,
                    json.dumps(
                        {
                            "tokens_saved": prompt.tokens_saved,
                            "cost_saved_usd": round(prompt.cost_saved_usd, 6),
                            "time_saved_ms": prompt.time_saved_ms,
                            "baseline_success": prompt.baseline.success,
                            "atelier_success": prompt.atelier.success,
                        },
                        sort_keys=True,
                    ),
                ),
            )


def _write_paired_command_json(root: Path, result: PairedCommandBenchmarkResult) -> Path:
    path = root / "benchmarks" / "savings" / "latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return path


def _write_csv(path: Path, result: ReplayResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "id",
                "task_type",
                "baseline_input_tokens",
                "optimized_input_tokens",
                "tokens_saved",
                "reduction_pct",
                "lever",
            ],
        )
        writer.writeheader()
        for prompt in result.prompts:
            writer.writerow(prompt.to_dict())


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    csv_path = None
    if "--csv" in args:
        idx = args.index("--csv")
        csv_path = Path(args[idx + 1])
    result = run_replay(csv_path=csv_path)
    print(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
