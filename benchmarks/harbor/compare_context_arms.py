#!/usr/bin/env python3
"""Compare the four context-optimization Harbor arms on common tasks.

Arms:
  raw                  vanilla Claude Code
  rtk                  vanilla Claude Code + RTK Bash rewrite hook
  lemoncrow            LemonCrow plugin/MCP (including LC's RTK-aware bash path)
  lemoncrow-headroom   LemonCrow + Headroom live-zone MCP-tail compression

Each argument accepts a dated Harbor job directory, an arm results parent
directory, or "latest". When omitted, the latest run under
benchmarks/harbor/results/<arm>/ is used.

Example:
    uv run python benchmarks/harbor/compare_context_arms.py
    uv run python benchmarks/harbor/compare_context_arms.py \
      --raw /path/to/raw/run --rtk /path/to/rtk/run \
      --lemoncrow /path/to/lc/run --headroom /path/to/lc-headroom/run
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
ARMS = ("raw", "rtk", "lemoncrow", "lemoncrow-headroom")


@dataclass(frozen=True)
class TrialMetrics:
    task: str
    reward: float
    cost_usd: float | None
    input_tokens: int
    cache_tokens: int
    output_tokens: int
    turns: int
    wall_seconds: float | None
    headroom_tokens_saved: int
    headroom_requests: int


@dataclass(frozen=True)
class ArmSummary:
    arm: str
    run: str
    tasks: int
    trials: int
    pass_rate: float
    avg_cost_usd: float | None
    avg_input_tokens: float
    avg_cache_tokens: float
    avg_output_tokens: float
    avg_turns: float
    avg_wall_seconds: float | None
    headroom_tokens_saved: int
    headroom_requests: int


def _dated_children(path: Path) -> list[Path]:
    return sorted(
        child
        for child in path.iterdir()
        if child.is_dir() and any(grandchild.name == "result.json" for grandchild in child.glob("*/result.json"))
    )


def resolve_run(ref: str | None, arm: str) -> Path:
    if ref in (None, "", "latest"):
        parent = RESULTS / arm
        if not parent.is_dir():
            raise FileNotFoundError(f"no results directory for {arm}: {parent}")
        candidates = _dated_children(parent)
        if not candidates:
            raise FileNotFoundError(f"no Harbor runs under {parent}")
        return candidates[-1]

    path = Path(ref).expanduser().resolve()
    if not path.is_dir():
        raise FileNotFoundError(f"run directory not found: {path}")
    if any(path.glob("*/result.json")):
        return path
    candidates = _dated_children(path)
    if not candidates:
        raise FileNotFoundError(f"no Harbor runs under {path}")
    return candidates[-1]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _stream_result(path: Path) -> tuple[int, dict[str, Any]]:
    """Return (turn count, terminal stream-json result)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0, {}

    assistant_turns = 0
    terminal: dict[str, Any] = {}
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "assistant":
            assistant_turns += 1
        if event.get("type") == "result":
            terminal = event
    turns = int(terminal.get("num_turns") or assistant_turns or 0)
    return turns, terminal


def _headroom_stats(path: Path) -> tuple[int, int]:
    data = _read_json(path)
    if not data:
        return 0, 0

    summary = data.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    compression = summary.get("compression")
    compression = compression if isinstance(compression, dict) else {}
    agent_usage = data.get("agent_usage")
    agent_usage = agent_usage if isinstance(agent_usage, dict) else {}
    agent_totals = agent_usage.get("totals")
    agent_totals = agent_totals if isinstance(agent_totals, dict) else {}
    savings = data.get("savings")
    savings = savings if isinstance(savings, dict) else {}
    requests_block = data.get("requests")
    requests_block = requests_block if isinstance(requests_block, dict) else {}

    # 0.37 exposes aggregate compression under summary.compression and exact
    # request accounting under agent_usage/requests. Keep legacy fallbacks so
    # old trial artifacts remain comparable.
    saved = (
        compression.get("total_tokens_saved_all_layers")
        or compression.get("total_tokens_removed")
        or agent_totals.get("tokens_saved")
        or savings.get("total_tokens")
        or data.get("tokens_saved_total")
        or summary.get("total_tokens_saved")
        or 0
    )
    requests = (
        requests_block.get("total")
        or summary.get("api_requests")
        or agent_totals.get("requests")
        or data.get("requests_total")
        or 0
    )
    try:
        return max(0, int(saved)), max(0, int(requests))
    except (TypeError, ValueError):
        return 0, 0


def _headroom_tail_stats(path: Path) -> tuple[int, int]:
    """Return estimated tokens saved and compressed-result count from tail JSONL."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0, 0
    saved = 0
    requests = 0
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        try:
            before = int(row.get("tokens_before") or 0)
            after = int(row.get("tokens_after") or 0)
        except (TypeError, ValueError):
            continue
        saved += max(0, before - after)
        requests += 1
    return saved, requests


def _wall_seconds(result: dict[str, Any]) -> float | None:
    started = result.get("started_at")
    finished = result.get("finished_at")
    if not isinstance(started, str) or not isinstance(finished, str):
        return None
    try:
        start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(finished.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (end_dt - start_dt).total_seconds())


def _recorded_context_arm(trial_dir: Path) -> str | None:
    cfg = _read_json(trial_dir / "config.json")
    agent = cfg.get("agent")
    if not isinstance(agent, dict):
        return None
    kwargs = agent.get("kwargs")
    if not isinstance(kwargs, dict):
        return None
    value = kwargs.get("context_arm")
    return str(value) if value is not None else None


def load_run(run: Path, expected_arm: str) -> tuple[list[TrialMetrics], set[str]]:
    trials: list[TrialMetrics] = []
    recorded_arms: set[str] = set()
    for result_path in sorted(run.glob("*/result.json")):
        trial_dir = result_path.parent
        result = _read_json(result_path)
        task_id = result.get("task_id")
        if not isinstance(task_id, dict):
            continue
        task = str(task_id.get("name") or trial_dir.name.split("__", 1)[0])
        verifier = result.get("verifier_result")
        verifier = verifier if isinstance(verifier, dict) else {}
        rewards = verifier.get("rewards")
        rewards = rewards if isinstance(rewards, dict) else {}
        try:
            reward = float(rewards.get("reward") or 0.0)
        except (TypeError, ValueError):
            reward = 0.0

        agent_result = result.get("agent_result")
        agent_result = agent_result if isinstance(agent_result, dict) else {}
        cost_raw = agent_result.get("cost_usd")
        try:
            cost = float(cost_raw) if cost_raw is not None else None
        except (TypeError, ValueError):
            cost = None

        turns, stream_result = _stream_result(trial_dir / "agent" / "claude-run.json")
        if not turns:
            turns = int(stream_result.get("num_turns") or 0)
        saved, requests = _headroom_tail_stats(trial_dir / "agent" / "headroom-tail-stats.jsonl")
        if not requests:
            saved, requests = _headroom_stats(trial_dir / "agent" / "headroom-stats.json")

        recorded = _recorded_context_arm(trial_dir)
        if recorded:
            recorded_arms.add(recorded)

        trials.append(
            TrialMetrics(
                task=task,
                reward=reward,
                cost_usd=cost,
                input_tokens=int(agent_result.get("n_input_tokens") or 0),
                cache_tokens=int(agent_result.get("n_cache_tokens") or 0),
                output_tokens=int(agent_result.get("n_output_tokens") or 0),
                turns=turns,
                wall_seconds=_wall_seconds(result),
                headroom_tokens_saved=saved,
                headroom_requests=requests,
            )
        )

    mismatched = {arm for arm in recorded_arms if arm != expected_arm}
    if mismatched:
        raise ValueError(
            f"{run} is labeled as {expected_arm!r} but trial configs record context_arm={sorted(mismatched)!r}"
        )
    return trials, recorded_arms


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def summarize(arm: str, run: Path, trials: list[TrialMetrics], common_tasks: set[str]) -> ArmSummary:
    scoped = [trial for trial in trials if trial.task in common_tasks]
    costs = [trial.cost_usd for trial in scoped if trial.cost_usd is not None]
    walls = [trial.wall_seconds for trial in scoped if trial.wall_seconds is not None]
    return ArmSummary(
        arm=arm,
        run=str(run),
        tasks=len({trial.task for trial in scoped}),
        trials=len(scoped),
        pass_rate=_mean([trial.reward for trial in scoped]),
        avg_cost_usd=_mean([float(value) for value in costs]) if costs else None,
        avg_input_tokens=_mean([float(trial.input_tokens) for trial in scoped]),
        avg_cache_tokens=_mean([float(trial.cache_tokens) for trial in scoped]),
        avg_output_tokens=_mean([float(trial.output_tokens) for trial in scoped]),
        avg_turns=_mean([float(trial.turns) for trial in scoped]),
        avg_wall_seconds=_mean([float(value) for value in walls]) if walls else None,
        headroom_tokens_saved=sum(trial.headroom_tokens_saved for trial in scoped),
        headroom_requests=sum(trial.headroom_requests for trial in scoped),
    )


def _pct_delta(value: float | None, baseline: float | None) -> str:
    if value is None or baseline in (None, 0):
        return "—"
    return f"{(value - baseline) / baseline * 100:+.1f}%"


def _fmt(value: float | None, digits: int = 0) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}"


def render(summaries: list[ArmSummary]) -> str:
    raw = next(summary for summary in summaries if summary.arm == "raw")
    lc = next(summary for summary in summaries if summary.arm == "lemoncrow")
    lines = [
        "Four-way context benchmark (common tasks only)",
        "",
        (
            f"{'arm':<21} {'trials':>6} {'pass':>7} {'avg$':>8} {'Δ$raw':>8} "
            f"{'input':>10} {'cache':>10} {'output':>9} {'turns':>7} {'wall s':>8}"
        ),
        "-" * 104,
    ]
    for summary in summaries:
        lines.append(
            f"{summary.arm:<21} {summary.trials:>6} {summary.pass_rate * 100:>6.1f}% "
            f"{_fmt(summary.avg_cost_usd, 3):>8} {_pct_delta(summary.avg_cost_usd, raw.avg_cost_usd):>8} "
            f"{_fmt(summary.avg_input_tokens):>10} {_fmt(summary.avg_cache_tokens):>10} "
            f"{_fmt(summary.avg_output_tokens):>9} {_fmt(summary.avg_turns, 1):>7} "
            f"{_fmt(summary.avg_wall_seconds, 1):>8}"
        )

    headroom = next(summary for summary in summaries if summary.arm == "lemoncrow-headroom")
    lines.extend(
        [
            "",
            "Incremental Headroom telemetry",
            f"  tail tokens_saved_total  : {headroom.headroom_tokens_saved:,}",
            f"  compressed tool results  : {headroom.headroom_requests:,}",
            f"  billed cost vs LemonCrow : {_pct_delta(headroom.avg_cost_usd, lc.avg_cost_usd)}",
            f"  pass-rate delta vs LC    : {(headroom.pass_rate - lc.pass_rate) * 100:+.1f} pp",
            (
                "  note                     : tail tokens_saved is Headroom's live-zone compression estimate; "
                "billed provider cost/tokens above are the end-to-end measurement."
            ),
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--raw", default=None)
    parser.add_argument("--rtk", default=None)
    parser.add_argument("--lemoncrow", default=None)
    parser.add_argument("--headroom", default=None)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    refs = {
        "raw": args.raw,
        "rtk": args.rtk,
        "lemoncrow": args.lemoncrow,
        "lemoncrow-headroom": args.headroom,
    }
    runs = {arm: resolve_run(refs[arm], arm) for arm in ARMS}
    loaded: dict[str, list[TrialMetrics]] = {}
    labels: dict[str, set[str]] = {}
    for arm in ARMS:
        loaded[arm], labels[arm] = load_run(runs[arm], arm)

    task_sets = [{trial.task for trial in loaded[arm]} for arm in ARMS]
    common_tasks = set.intersection(*task_sets) if task_sets else set()
    if not common_tasks:
        raise SystemExit("no common tasks across the four runs")

    summaries = [summarize(arm, runs[arm], loaded[arm], common_tasks) for arm in ARMS]
    if args.as_json:
        print(
            json.dumps(
                {
                    "common_tasks": sorted(common_tasks),
                    "arms": [asdict(summary) for summary in summaries],
                    "recorded_context_arms": {arm: sorted(labels[arm]) for arm in ARMS},
                },
                indent=2,
            )
        )
        return

    print(render(summaries))
    print("")
    print("Runs")
    for summary in summaries:
        print(f"  {summary.arm:<21} {summary.run}")


if __name__ == "__main__":
    main()
