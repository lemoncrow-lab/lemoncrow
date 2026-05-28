"""Tests for ab.runner — AB-01 (CLI), AB-03 (resumability), AB-06 (output layout)."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ab.aggregate import compute_summary
from ab.runner import load_suite_tasks, run_cell, write_config


def test_load_suite_tasks_terminalbench():
    tasks = load_suite_tasks("terminalbench", 5)
    assert len(tasks) == 5
    assert tasks[0] == "hello-world"
    assert all(isinstance(t, str) for t in tasks)


def test_load_suite_tasks_unknown_suite():
    with pytest.raises(ValueError, match="Unknown suite"):
        load_suite_tasks("nonexistent", 5)


def test_load_suite_tasks_respects_n_tasks():
    tasks_3 = load_suite_tasks("terminalbench", 3)
    assert len(tasks_3) == 3
    tasks_10 = load_suite_tasks("terminalbench", 10)
    assert len(tasks_10) == 10


def test_write_config_atomic():
    with tempfile.TemporaryDirectory() as d:
        out_dir = Path(d)
        config = {"run_id": "smoke", "n_reps": 3}
        write_config(out_dir, config)
        assert (out_dir / "config.json").exists()
        loaded = json.loads((out_dir / "config.json").read_text())
        assert loaded == config
        tmp_files = list(out_dir.glob("*.tmp"))
        assert tmp_files == [], f"stale .tmp files: {tmp_files}"


def test_run_cell_skip_if_exists_ab03():
    with tempfile.TemporaryDirectory() as d:
        raw_dir = Path(d) / "raw"
        raw_dir.mkdir()
        trial_dir = Path(d) / "trials"
        trial_dir.mkdir()
        dest = raw_dir / "hello-world__on__rep1.json"
        dest.write_text(json.dumps({"grader_is_resolved": True}))

        with patch("terminalbench.agent_adapter.run_terminalbench_trial") as mock_trial:
            result = run_cell(
                "hello-world",
                "on",
                1,
                raw_dir,
                trial_dir,
                "claude-sonnet-4-5",
                "terminal-bench-core",
                "0.1.1",
            )
        mock_trial.assert_not_called()
        assert result is True


def test_run_cell_writes_atomically_ab03():
    mock_result = MagicMock()
    mock_result.is_error = False
    mock_result.to_dict.return_value = {"task_id": "hello-world", "grader_is_resolved": True}

    with tempfile.TemporaryDirectory() as d:
        raw_dir = Path(d) / "raw"
        raw_dir.mkdir()
        trial_dir = Path(d) / "trials"
        trial_dir.mkdir()

        with patch("terminalbench.agent_adapter.run_terminalbench_trial", return_value=mock_result):
            result = run_cell(
                "hello-world",
                "on",
                1,
                raw_dir,
                trial_dir,
                "claude-sonnet-4-5",
                "terminal-bench-core",
                "0.1.1",
            )

        dest = raw_dir / "hello-world__on__rep1.json"
        assert dest.exists(), "raw file not written"
        tmp = dest.with_suffix(".tmp")
        assert not tmp.exists(), ".tmp file not cleaned up (atomic write broken)"
        loaded = json.loads(dest.read_text())
        assert loaded["task_id"] == "hello-world"
        assert result is True


def test_summary_json_schema_ab06():
    with tempfile.TemporaryDirectory() as d:
        raw_dir = Path(d) / "raw"
        raw_dir.mkdir()
        for task in ["taskX", "taskY"]:
            for rep in range(1, 3):
                (raw_dir / f"{task}__on__rep{rep}.json").write_text(json.dumps({"grader_is_resolved": True}))

        summary = compute_summary("test-run", raw_dir)
        assert summary["run_id"] == "test-run"
        assert "generated_at" in summary
        assert isinstance(summary["cells"], dict)
        for cell_key, cell_data in summary["cells"].items():
            assert "__" in cell_key, f"cell key missing __ separator: {cell_key}"
            parts = cell_key.split("__")
            assert len(parts) == 2, f"cell key should be task__mode, got: {cell_key}"
            assert "passed" in cell_data
            assert "total" in cell_data
            assert "ci_lower" in cell_data
            assert "ci_upper" in cell_data
            assert "p_hat" not in cell_data, "AB-05: p_hat must never appear in summary"
            assert 0.0 <= cell_data["ci_lower"] <= cell_data["ci_upper"] <= 1.0
