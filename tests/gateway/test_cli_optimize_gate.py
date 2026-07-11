from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from lemoncrow.gateway.cli import cli


def _record(*, mode: str, verdict: str) -> dict[str, object]:
    return {
        "task_id": "task-1",
        "mode": mode,
        "rep": 1,
        "grader_verdict": verdict,
        "is_error": False,
    }


def test_optimize_gate_outputs_json_verdict(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    rows = [*[_record(mode="off", verdict="pass") for _ in range(950)]]
    rows.extend(_record(mode="off", verdict="fail") for _ in range(50))
    rows.extend(_record(mode="on", verdict="pass") for _ in range(949))
    rows.extend(_record(mode="on", verdict="fail") for _ in range(51))
    runs.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "--root",
            str(tmp_path / ".lemoncrow"),
            "optimize",
            "gate",
            "--runs",
            str(runs),
            "--baseline-cost-usd",
            "10",
            "--candidate-cost-usd",
            "8",
            "--margin",
            "0.05",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["passed"] is True
    assert payload["estimated_cost_savings_usd"] == 2.0
