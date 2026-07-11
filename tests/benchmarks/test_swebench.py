"""Unit tests for the SWE-bench (Python) backend folded into ``lemon benchmark swe``.

No Docker / network: the loader reads a local JSONL (swebench supports it) and
the grader's harness subprocess is stubbed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

from benchmarks.codebench import swebench_data, swebench_grade


def _ensure_benchmarks_package() -> None:
    ROOT = Path(__file__).resolve().parents[2]
    import benchmarks

    benchmarks.__path__ = [*benchmarks.__path__, str(ROOT / "benchmarks")]
    codebench_pkg = sys.modules.get("benchmarks.codebench")
    if codebench_pkg is None:
        codebench_pkg = types.ModuleType("benchmarks.codebench")
        sys.modules["benchmarks.codebench"] = codebench_pkg
    codebench_paths = list(getattr(codebench_pkg, "__path__", []))
    root_codebench_path = str(ROOT / "benchmarks" / "codebench")
    if root_codebench_path not in codebench_paths:
        codebench_paths.append(root_codebench_path)
    codebench_pkg.__path__ = codebench_paths


def _load_multiswe_run() -> Any:
    _ensure_benchmarks_package()
    import importlib

    return importlib.import_module("benchmarks.codebench.multiswe_run")


def _swe_args(**overrides: Any) -> argparse.Namespace:
    defaults = dict(
        suite="swe-bench-verified",
        dataset=None,
        instances=None,
        min_changed_files=2,
        limit=None,
        timeout=1800,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _row(instance_id: str, *, n_files: int, repo: str = "o/r", base: str = "abc") -> dict[str, Any]:
    patch = "".join(f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n" for i in range(n_files))
    return {
        "instance_id": instance_id,
        "repo": repo,
        "base_commit": base,
        "problem_statement": f"fix {instance_id}",
        "patch": patch,
        "test_patch": "diff --git a/t.py b/t.py\n",
        "FAIL_TO_PASS": "[]",
        "PASS_TO_PASS": "[]",
    }


def _write(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / "swe.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_image_ref_namespaces_and_rewrites_double_underscore() -> None:
    assert (
        swebench_data.image_ref("astropy__astropy-12907")
        == "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest"
    )


def test_strip_gold_test_files_drops_colliding_sections() -> None:
    """Sections for files the gold test patch owns are dropped; solution code stays."""
    model_patch = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/tests/roots/r/conf.py b/tests/roots/r/conf.py\n"
        "new file mode 100644\n--- /dev/null\n+++ b/tests/roots/r/conf.py\n@@ -0,0 +1 @@\n+pass\n"
    )
    gold_test = (
        "diff --git a/tests/roots/r/conf.py b/tests/roots/r/conf.py\ndiff --git a/tests/test_x.py b/tests/test_x.py\n"
    )
    out = swebench_grade._strip_gold_test_files(model_patch, gold_test)
    assert "a/src/app.py" in out  # solution code kept
    assert "tests/roots/r/conf.py" not in out  # gold owns it -> dropped to avoid collision
    # No gold test patch (or empty model patch) -> unchanged passthrough.
    assert swebench_grade._strip_gold_test_files(model_patch, "") == model_patch
    assert swebench_grade._strip_gold_test_files("", gold_test) == ""


def test_select_backend_swe_lite_defaults_dataset_and_instances(monkeypatch: Any) -> None:
    """``--suite swe-lite`` with no --dataset/--instance fills in the pinned defaults."""
    multiswe_run = _load_multiswe_run()
    captured: dict[str, Any] = {}

    def _fake_load_instances(**kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(multiswe_run.swebench_data, "load_instances", _fake_load_instances)
    instances, _grade_fn, label = multiswe_run._select_backend(_swe_args(suite="swe-lite"))

    assert instances == []
    assert label == "swebench"
    # swe-lite now pins baseline-solvable VERIFIED instances (the princeton
    # Lite split is no longer used), so the dataset default is Verified.
    assert captured["dataset"] == swebench_data.DEFAULT_DATASET
    assert captured["instances"] == list(swebench_data.SWE_LITE_INSTANCE_IDS)


def test_select_backend_swe_lite_explicit_instance_overrides_default(monkeypatch: Any) -> None:
    multiswe_run = _load_multiswe_run()
    captured: dict[str, Any] = {}

    def _fake_load_instances(**kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(multiswe_run.swebench_data, "load_instances", _fake_load_instances)
    multiswe_run._select_backend(_swe_args(suite="swe-lite", instances=["django__django-14999"]))

    assert captured["instances"] == ["django__django-14999"]


def test_select_backend_swe_lite_explicit_dataset_overrides_default(monkeypatch: Any) -> None:
    multiswe_run = _load_multiswe_run()
    captured: dict[str, Any] = {}

    def _fake_load_instances(**kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(multiswe_run.swebench_data, "load_instances", _fake_load_instances)
    multiswe_run._select_backend(_swe_args(suite="swe-lite", dataset="local/lite.jsonl"))

    assert captured["dataset"] == "local/lite.jsonl"
    assert captured["instances"] == list(swebench_data.SWE_LITE_INSTANCE_IDS)


def test_select_backend_swe_lite_limit_narrows_default_instances(monkeypatch: Any) -> None:
    """--limit without an explicit --instance slices the pinned default list."""
    multiswe_run = _load_multiswe_run()
    captured: dict[str, Any] = {}

    def _fake_load_instances(**kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(multiswe_run.swebench_data, "load_instances", _fake_load_instances)
    multiswe_run._select_backend(_swe_args(suite="swe-lite", limit=3))

    assert captured["instances"] == list(swebench_data.SWE_LITE_INSTANCE_IDS[:3])


def test_select_backend_swe_lite_grade_uses_lite_dataset(monkeypatch: Any) -> None:
    """Grading must use the suite's resolved dataset (same as instance loading)."""
    multiswe_run = _load_multiswe_run()
    monkeypatch.setattr(multiswe_run.swebench_data, "load_instances", lambda **kwargs: [])
    captured: dict[str, Any] = {}

    def _fake_grade(insts: Any, patches: Any, **kwargs: Any) -> dict[str, bool]:
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(multiswe_run.swebench_grade, "grade", _fake_grade)
    _, grade_fn, _ = multiswe_run._select_backend(_swe_args(suite="swe-lite"))
    grade_fn([], {}, Path("/tmp/work"), 1)

    assert captured["dataset_name"] == swebench_data.DEFAULT_DATASET


def test_select_backend_swe_bench_verified_unaffected_by_swe_lite(monkeypatch: Any) -> None:
    """swe-bench-verified keeps its original no-default-instances behavior."""
    multiswe_run = _load_multiswe_run()
    captured: dict[str, Any] = {}

    def _fake_load_instances(**kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(multiswe_run.swebench_data, "load_instances", _fake_load_instances)
    multiswe_run._select_backend(_swe_args(suite="swe-bench-verified", limit=3))

    assert captured["dataset"] == swebench_data.DEFAULT_DATASET
    assert captured["instances"] is None
    assert captured["limit"] == 3


def test_grade_writes_predictions_and_parses_resolved(tmp_path: Path, monkeypatch: Any) -> None:
    insts = [
        swebench_data.SweBenchInstance("o__r-2", "o/r", "abc", "python", "img2", "fix", 2),
        swebench_data.SweBenchInstance("o__r-3", "o/r", "def", "python", "img3", "fix", 2),
    ]
    patches = {"o__r-2": "DIFF2", "o__r-3": ""}
    captured: dict[str, Any] = {}

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cwd = Path(kwargs["cwd"])
        captured["cmd"] = cmd
        captured["preds"] = [
            json.loads(line) for line in (cwd / "predictions.jsonl").read_text().splitlines() if line.strip()
        ]
        report = {"resolved_ids": ["o__r-2"], "unresolved_ids": ["o__r-3"]}
        (cwd / f"lemoncrow-codebench.{cwd.name}.json").write_text(json.dumps(report), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(swebench_grade.subprocess, "run", _fake_run)
    resolved = swebench_grade.grade(insts, patches, work_dir=tmp_path / "grade_lemoncrow_rep1", dataset_name="X")

    assert resolved == {"o__r-2": True, "o__r-3": False}
    preds = captured["preds"]
    assert {p["instance_id"] for p in preds} == {"o__r-2", "o__r-3"}
    assert all(p["model_name_or_path"] == "lemoncrow-codebench" for p in preds)
    assert next(p for p in preds if p["instance_id"] == "o__r-2")["model_patch"] == "DIFF2"
    cmd = captured["cmd"]
    assert cmd[cmd.index("--dataset_name") + 1] == "X"
    assert cmd[cmd.index("--run_id") + 1] == "grade_lemoncrow_rep1"
    assert cmd[cmd.index("--namespace") + 1] == "swebench"
    assert "--instance_ids" in cmd
