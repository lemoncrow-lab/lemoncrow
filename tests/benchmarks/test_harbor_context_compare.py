from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.harbor.compare_context_arms import ARMS, load_run, render, summarize


def _write_trial(
    run: Path,
    *,
    task: str,
    arm: str,
    reward: float,
    cost: float,
    input_tokens: int,
    cache_tokens: int,
    output_tokens: int,
    headroom_saved: int = 0,
) -> None:
    trial = run / f"{task}__rep"
    (trial / "agent").mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps(
            {
                "task_id": {"name": task},
                "agent_result": {
                    "cost_usd": cost,
                    "n_input_tokens": input_tokens,
                    "n_cache_tokens": cache_tokens,
                    "n_output_tokens": output_tokens,
                },
                "verifier_result": {"rewards": {"reward": reward}},
                "started_at": "2026-09-20T10:00:00Z",
                "finished_at": "2026-09-20T10:01:00Z",
            }
        )
    )
    (trial / "config.json").write_text(json.dumps({"agent": {"kwargs": {"context_arm": arm}}}))
    (trial / "agent" / "claude-run.json").write_text(
        "\n".join(
            [
                json.dumps({"type": "assistant"}),
                json.dumps({"type": "assistant"}),
                json.dumps({"type": "result", "num_turns": 2}),
            ]
        )
    )
    if headroom_saved:
        (trial / "agent" / "headroom-stats.json").write_text(
            json.dumps(
                {
                    "summary": {
                        "api_requests": 7,
                        "compression": {
                            "total_tokens_saved_all_layers": headroom_saved,
                            "total_tokens_removed": headroom_saved,
                        },
                    },
                    "agent_usage": {"totals": {"requests": 7, "tokens_saved": headroom_saved}},
                    "requests": {"total": 7},
                    "savings": {"total_tokens": headroom_saved},
                }
            )
        )


def test_four_arm_summary_uses_common_tasks_and_separates_headroom_counter(tmp_path: Path) -> None:
    runs: dict[str, Path] = {}
    loaded = {}
    for index, arm in enumerate(ARMS):
        run = tmp_path / arm
        run.mkdir()
        runs[arm] = run
        _write_trial(
            run,
            task="common",
            arm=arm,
            reward=1.0,
            cost=1.0 - index * 0.1,
            input_tokens=1000 - index * 100,
            cache_tokens=500,
            output_tokens=100,
            headroom_saved=250 if arm == "lemoncrow-headroom" else 0,
        )
        _write_trial(
            run,
            task=f"only-{arm}",
            arm=arm,
            reward=0.0,
            cost=9.0,
            input_tokens=9000,
            cache_tokens=9000,
            output_tokens=9000,
        )
        loaded[arm], _ = load_run(run, arm)

    common = {"common"}
    summaries = [summarize(arm, runs[arm], loaded[arm], common) for arm in ARMS]
    assert all(summary.tasks == 1 and summary.trials == 1 for summary in summaries)
    headroom = next(summary for summary in summaries if summary.arm == "lemoncrow-headroom")
    assert headroom.headroom_tokens_saved == 250
    assert headroom.headroom_requests == 7
    assert headroom.avg_cost_usd == pytest.approx(0.7)

    output = render(summaries)
    assert "tail tokens_saved_total  : 250" in output
    assert "billed cost vs LemonCrow" in output
    assert "end-to-end measurement" in output


def test_load_run_rejects_mislabeled_context_arm(tmp_path: Path) -> None:
    run = tmp_path / "raw"
    run.mkdir()
    _write_trial(
        run,
        task="task",
        arm="lemoncrow",
        reward=1.0,
        cost=1.0,
        input_tokens=1,
        cache_tokens=1,
        output_tokens=1,
    )
    with pytest.raises(ValueError, match="labeled as 'raw'"):
        load_run(run, "raw")
