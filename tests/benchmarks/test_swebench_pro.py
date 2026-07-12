"""Unit tests for the SWE-bench Pro backend folded into ``lc benchmark swe``.

SWE-bench Pro (ScaleAI) has its own loader (:mod:`swebench_pro_data`) and
grader (:mod:`swebench_pro_grade`) -- a structurally different dataset/harness
from SWE-bench (Verified/Lite), not a :data:`swebench_data.SUITE_DEFAULTS`
entry. No Docker / network: the HF dataset load and the grading harness
subprocess are both stubbed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

from benchmarks.codebench import swebench_pro_data, swebench_pro_grade
from benchmarks.codebench.swebench_pro_data import SweBenchProInstance


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
    defaults: dict[str, Any] = dict(suite="swe-pro", dataset=None, instances=None, limit=None, timeout=1800)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _hf_row(instance_id: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "instance_id": instance_id,
        "repo": "o/r",
        "base_commit": "abc123",
        "patch": "diff --git a/f.py b/f.py\n",
        "test_patch": "diff --git a/t.py b/t.py\n",
        "problem_statement": f"fix {instance_id}",
        "requirements": None,
        "interface": None,
        "repo_language": "go",
        "fail_to_pass": "['TestFoo']",  # single-quoted Python repr, not valid JSON
        "pass_to_pass": '["TestBar", "TestBaz"]',  # double-quoted JSON
        "issue_specificity": '["well_specified"]',
        "issue_categories": '["bug_fix"]',
        "before_repo_set_cmd": "git reset --hard abc123",
        "selected_test_files_to_run": '["foo_test.go"]',
        "dockerhub_tag": f"tag-{instance_id}",
    }
    row.update(overrides)
    return row


def _fake_dataset(rows: list[dict[str, Any]]) -> Any:
    """A minimal stand-in for the ``datasets.Dataset`` iterable ``load_dataset`` returns."""
    return rows


# --- _parse_list_field -------------------------------------------------------


def test_parse_list_field_handles_json_double_quoted() -> None:
    assert swebench_pro_data._parse_list_field('["a", "b"]') == ["a", "b"]


def test_parse_list_field_handles_python_repr_single_quoted() -> None:
    """The real dataset mixes quoting styles (fail_to_pass is often single-quoted
    Python repr, which ``json.loads`` rejects); ``ast.literal_eval`` must handle both.
    """
    assert swebench_pro_data._parse_list_field("['TestSearchCache']") == ["TestSearchCache"]


def test_parse_list_field_empty_or_none_is_empty_list() -> None:
    assert swebench_pro_data._parse_list_field("") == []
    assert swebench_pro_data._parse_list_field(None) == []
    assert swebench_pro_data._parse_list_field("[]") == []


def test_parse_list_field_non_list_literal_falls_back_to_single_element() -> None:
    assert swebench_pro_data._parse_list_field("not a list literal at all !!") == ["not a list literal at all !!"]


# --- load_instances -----------------------------------------------------------


def test_load_instances_defaults_to_pinned_ids(monkeypatch: Any) -> None:
    rows = [_hf_row(iid) for iid in swebench_pro_data.SWE_PRO_INSTANCE_IDS]
    captured: dict[str, Any] = {}

    def _fake_load(name: str, split: str) -> Any:
        captured["name"] = name
        captured["split"] = split
        return _fake_dataset(rows)

    monkeypatch.setattr(swebench_pro_data, "_load_dataset_rows", _fake_load)
    out = swebench_pro_data.load_instances()

    assert [inst.instance_id for inst in out] == list(swebench_pro_data.SWE_PRO_INSTANCE_IDS)
    assert captured["name"] == swebench_pro_data.DEFAULT_DATASET
    assert captured["split"] == swebench_pro_data.DEFAULT_SPLIT


def test_load_instances_parses_stringified_list_fields(monkeypatch: Any) -> None:
    iid = swebench_pro_data.SWE_PRO_INSTANCE_IDS[0]
    rows = [_hf_row(iid)]
    monkeypatch.setattr(swebench_pro_data, "_load_dataset_rows", lambda name, split: _fake_dataset(rows))

    out = swebench_pro_data.load_instances(instances=[iid])
    inst = out[0]

    assert inst.fail_to_pass == ["TestFoo"]
    assert inst.pass_to_pass == ["TestBar", "TestBaz"]
    assert inst.selected_test_files_to_run == ["foo_test.go"]
    assert inst.issue_specificity == ["well_specified"]
    assert inst.issue_categories == ["bug_fix"]
    assert inst.requirements is None
    assert inst.interface is None
    assert inst.repo == "o/r"
    assert inst.base_commit == "abc123"


def test_load_instances_explicit_instances_override_default_and_ignore_limit(monkeypatch: Any) -> None:
    custom_id = "instance_custom__repo-deadbeef"
    rows = [_hf_row(custom_id), _hf_row(swebench_pro_data.SWE_PRO_INSTANCE_IDS[0])]
    monkeypatch.setattr(swebench_pro_data, "_load_dataset_rows", lambda name, split: _fake_dataset(rows))

    out = swebench_pro_data.load_instances(instances=[custom_id], limit=0)

    # limit=0 would zero out the default slice, but must not apply to an explicit request.
    assert [inst.instance_id for inst in out] == [custom_id]


def test_load_instances_limit_slices_default_ids(monkeypatch: Any) -> None:
    rows = [_hf_row(iid) for iid in swebench_pro_data.SWE_PRO_INSTANCE_IDS]
    monkeypatch.setattr(swebench_pro_data, "_load_dataset_rows", lambda name, split: _fake_dataset(rows))

    out = swebench_pro_data.load_instances(limit=3)

    assert [inst.instance_id for inst in out] == list(swebench_pro_data.SWE_PRO_INSTANCE_IDS[:3])


def test_load_instances_preserves_requested_order(monkeypatch: Any) -> None:
    ids = list(reversed(swebench_pro_data.SWE_PRO_INSTANCE_IDS[:3]))
    rows = [_hf_row(iid) for iid in swebench_pro_data.SWE_PRO_INSTANCE_IDS[:3]]
    monkeypatch.setattr(swebench_pro_data, "_load_dataset_rows", lambda name, split: _fake_dataset(rows))

    out = swebench_pro_data.load_instances(instances=ids)

    assert [inst.instance_id for inst in out] == ids


def test_load_instances_warns_on_known_bad_instance_when_selected(monkeypatch: Any, capsys: Any) -> None:
    bad_id = next(iter(swebench_pro_data.KNOWN_BAD_INSTANCE_IDS))
    rows = [_hf_row(bad_id)]
    monkeypatch.setattr(swebench_pro_data, "_load_dataset_rows", lambda name, split: _fake_dataset(rows))

    out = swebench_pro_data.load_instances(instances=[bad_id])

    assert [inst.instance_id for inst in out] == [bad_id]
    err = capsys.readouterr().out
    assert "known history of checkout failures" in err
    assert bad_id in err


def test_load_instances_no_warning_when_known_bad_instance_not_selected(monkeypatch: Any, capsys: Any) -> None:
    other_id = "instance_future-architect__vuls-36456cb151894964ba1683ce7da5c35ada789970"
    rows = [_hf_row(other_id)]
    monkeypatch.setattr(swebench_pro_data, "_load_dataset_rows", lambda name, split: _fake_dataset(rows))

    swebench_pro_data.load_instances(instances=[other_id])

    out = capsys.readouterr().out
    assert "known history of checkout failures" not in out


def test_load_instances_missing_requested_id_is_warned_and_dropped(monkeypatch: Any, capsys: Any) -> None:
    present_id = "instance_present__repo-1"
    missing_id = "instance_missing__repo-2"
    rows = [_hf_row(present_id)]
    monkeypatch.setattr(swebench_pro_data, "_load_dataset_rows", lambda name, split: _fake_dataset(rows))

    out = swebench_pro_data.load_instances(instances=[present_id, missing_id])

    assert [inst.instance_id for inst in out] == [present_id]
    printed = capsys.readouterr().out
    assert missing_id in printed


# --- multiswe_run._select_backend("swe-pro") ---------------------------------


def test_select_backend_swe_pro_defaults_dataset_and_instances(monkeypatch: Any) -> None:
    multiswe_run = _load_multiswe_run()
    captured: dict[str, Any] = {}

    def _fake_load_instances(**kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(multiswe_run.swebench_pro_data, "load_instances", _fake_load_instances)
    instances, _grade_fn, label = multiswe_run._select_backend(_swe_args())

    assert instances == []
    assert label == "swebench-pro"
    assert captured["dataset"] == swebench_pro_data.DEFAULT_DATASET
    assert captured["instances"] is None
    assert captured["limit"] is None


def test_select_backend_swe_pro_explicit_instances_override_default(monkeypatch: Any) -> None:
    multiswe_run = _load_multiswe_run()
    captured: dict[str, Any] = {}

    def _fake_load_instances(**kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(multiswe_run.swebench_pro_data, "load_instances", _fake_load_instances)
    multiswe_run._select_backend(_swe_args(instances=["instance_custom__repo-1"]))

    assert captured["instances"] == ["instance_custom__repo-1"]


def test_select_backend_swe_pro_explicit_dataset_overrides_default(monkeypatch: Any) -> None:
    multiswe_run = _load_multiswe_run()
    captured: dict[str, Any] = {}

    def _fake_load_instances(**kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(multiswe_run.swebench_pro_data, "load_instances", _fake_load_instances)
    multiswe_run._select_backend(_swe_args(dataset="local/pro.jsonl"))

    assert captured["dataset"] == "local/pro.jsonl"


def test_select_backend_swe_pro_grade_fn_calls_swebench_pro_grade(monkeypatch: Any) -> None:
    multiswe_run = _load_multiswe_run()
    monkeypatch.setattr(multiswe_run.swebench_pro_data, "load_instances", lambda **kwargs: [])
    captured: dict[str, Any] = {}

    def _fake_grade(insts: Any, patches: Any, **kwargs: Any) -> dict[str, bool]:
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(multiswe_run.swebench_pro_grade, "grade", _fake_grade)
    _, grade_fn, _ = multiswe_run._select_backend(_swe_args(timeout=999))
    grade_fn([], {}, Path("/tmp/work"), 2)

    assert captured["work_dir"] == Path("/tmp/work")
    assert captured["max_workers"] == 2
    assert captured["timeout"] == 999


# --- swebench_pro_grade.grade --------------------------------------------------


def _instance(instance_id: str, **overrides: Any) -> SweBenchProInstance:
    fields: dict[str, Any] = dict(
        instance_id=instance_id,
        repo="o/r",
        base_commit="abc123",
        repo_language="go",
        problem_statement="fix it",
        fail_to_pass=["TestFoo"],
        pass_to_pass=["TestBar"],
        selected_test_files_to_run=["foo_test.go"],
        issue_specificity=["well_specified"],
        issue_categories=["bug_fix"],
        before_repo_set_cmd="git reset --hard abc123",
        dockerhub_tag="tag",
    )
    fields.update(overrides)
    return SweBenchProInstance(**fields)


def test_grade_writes_csv_and_patches_and_parses_eval_results(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(swebench_pro_grade, "_ensure_harness_repo", lambda: tmp_path / "harness")
    (tmp_path / "harness").mkdir()

    insts = [_instance("o__r-2"), _instance("o__r-3")]
    patches = {"o__r-2": "DIFF2", "o__r-3": ""}
    captured: dict[str, Any] = {}

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["cwd"] = kwargs["cwd"]
        work = Path(cmd[cmd.index("--output_dir") + 1])
        (work / "eval_results.json").write_text(json.dumps({"o__r-2": True, "o__r-3": False}), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(swebench_pro_grade.subprocess, "run", _fake_run)
    work_dir = tmp_path / "grade_lemoncrow_rep1"
    resolved = swebench_pro_grade.grade(insts, patches, work_dir=work_dir)

    assert resolved == {"o__r-2": True, "o__r-3": False}
    assert captured["cwd"] == str(tmp_path / "harness")

    cmd = captured["cmd"]
    assert cmd[cmd.index("--dockerhub_username") + 1] == swebench_pro_grade.DOCKERHUB_USERNAME
    assert cmd[cmd.index("--scripts_dir") + 1] == "run_scripts"
    assert "--use_local_docker" in cmd
    assert "--redo" in cmd

    patches_written = json.loads((work_dir / "patches.json").read_text(encoding="utf-8"))
    assert {p["instance_id"]: p["patch"] for p in patches_written} == {"o__r-2": "DIFF2", "o__r-3": ""}
    assert all(p["prefix"] == swebench_pro_grade.MODEL_NAME for p in patches_written)

    with (work_dir / "raw_samples.csv").open(newline="", encoding="utf-8") as f:
        import csv

        rows = list(csv.DictReader(f))
    row_by_id = {r["instance_id"]: r for r in rows}
    assert json.loads(row_by_id["o__r-2"]["fail_to_pass"]) == ["TestFoo"]
    assert json.loads(row_by_id["o__r-2"]["selected_test_files_to_run"]) == ["foo_test.go"]


def test_grade_defaults_missing_instances_to_unresolved(tmp_path: Path, monkeypatch: Any) -> None:
    """An instance absent from eval_results.json (e.g. the harness crashed on it) is unresolved, not an error."""
    monkeypatch.setattr(swebench_pro_grade, "_ensure_harness_repo", lambda: tmp_path / "harness")
    (tmp_path / "harness").mkdir()

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        work = Path(cmd[cmd.index("--output_dir") + 1])
        (work / "eval_results.json").write_text(json.dumps({}), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(swebench_pro_grade.subprocess, "run", _fake_run)
    resolved = swebench_pro_grade.grade([_instance("o__r-1")], {"o__r-1": "DIFF"}, work_dir=tmp_path / "work")

    assert resolved == {"o__r-1": False}


def test_grade_raises_when_harness_writes_no_report(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(swebench_pro_grade, "_ensure_harness_repo", lambda: tmp_path / "harness")
    (tmp_path / "harness").mkdir()
    monkeypatch.setattr(
        swebench_pro_grade.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, "", "boom"),
    )

    try:
        swebench_pro_grade.grade([_instance("o__r-1")], {}, work_dir=tmp_path / "work")
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "eval_results.json" in str(exc)
    assert raised


def test_grade_raises_on_unparseable_report(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(swebench_pro_grade, "_ensure_harness_repo", lambda: tmp_path / "harness")
    (tmp_path / "harness").mkdir()

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        work = Path(cmd[cmd.index("--output_dir") + 1])
        (work / "eval_results.json").write_text("not json", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(swebench_pro_grade.subprocess, "run", _fake_run)

    try:
        swebench_pro_grade.grade([_instance("o__r-1")], {}, work_dir=tmp_path / "work")
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "unparseable" in str(exc)
    assert raised


# --- _ensure_harness_repo -------------------------------------------------------


def test_ensure_harness_repo_skips_clone_when_already_present(tmp_path: Path, monkeypatch: Any) -> None:
    cache_dir = tmp_path / "cached-harness"
    cache_dir.mkdir()
    (cache_dir / "swe_bench_pro_eval.py").write_text("# stub", encoding="utf-8")

    def _fail_if_called(cmd: list[str], **kwargs: Any) -> None:
        raise AssertionError("git clone should not run when the harness is already cached")

    monkeypatch.setattr(swebench_pro_grade.subprocess, "run", _fail_if_called)

    assert swebench_pro_grade._ensure_harness_repo(cache_dir) == cache_dir


def test_ensure_harness_repo_clones_when_absent(tmp_path: Path, monkeypatch: Any) -> None:
    cache_dir = tmp_path / "fresh-harness"
    captured: dict[str, Any] = {}

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(swebench_pro_grade.subprocess, "run", _fake_run)

    result = swebench_pro_grade._ensure_harness_repo(cache_dir)

    assert result == cache_dir
    cmd = captured["cmd"]
    assert cmd[:2] == ["git", "clone"]
    assert cmd[-2:] == [swebench_pro_grade.HARNESS_REPO_URL, str(cache_dir)]
