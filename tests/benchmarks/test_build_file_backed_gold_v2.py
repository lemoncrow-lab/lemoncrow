from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "benchmarks" / "codebench" / "build_file_backed_gold_v2.py"
SPEC = importlib.util.spec_from_file_location("build_file_backed_gold_v2", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_normalize_preserves_hidden_directory_names() -> None:
    assert MODULE._normalize(".github/workflows/test.yml") == ".github/workflows/test.yml"
    assert MODULE._normalize("./.github/workflows/test.yml") == ".github/workflows/test.yml"
    assert MODULE._normalize("/tmp/cache/file.md") == "tmp/cache/file.md"


def test_existing_gold_paths_resolve_inside_workspace(tmp_path: Path) -> None:
    hidden = tmp_path / ".github" / "workflow.yml"
    hidden.parent.mkdir(parents=True)
    hidden.write_text("name: test\n", encoding="utf-8")
    outside = tmp_path.parent / "outside-file.md"
    outside.write_text("outside\n", encoding="utf-8")

    assert MODULE._existing_gold_paths(tmp_path, [".github/workflow.yml"]) == (".github/workflow.yml",)
    assert MODULE._existing_gold_paths(tmp_path, [str(outside)]) == ()


def test_replacement_paths_exclude_benchmark_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "src" / "service.py"
    benchmark = tmp_path / "benchmarks" / "data.py"
    source.parent.mkdir(parents=True)
    benchmark.parent.mkdir(parents=True)
    source.write_text("def service(): pass\n", encoding="utf-8")
    benchmark.write_text("PAIR = 1\n", encoding="utf-8")

    assert MODULE._existing_replacement_paths(tmp_path, ["src/service.py", "benchmarks/data.py"]) == ("src/service.py",)


def test_loader_uses_frozen_evaluator_case_identity(tmp_path: Path) -> None:
    document = {
        "gold_kind": "semantic",
        "repos": {"owner__repo": {"base_commit": "abc"}},
        "pairs": [
            ["find retries", "task-1", "owner__repo"],
            ["find retries", "task-1", "owner__repo"],
            ["find retries", "task-2", "owner__repo"],
        ],
        "true_map": {"task-1": ["src/a.py"], "task-2": ["src/a.py"]},
    }
    for kind, filename in MODULE._MANIFESTS:
        payload = document if kind == "semantic" else {"gold_kind": kind, "repos": {}, "pairs": [], "true_map": {}}
        (tmp_path / filename).write_text(json.dumps(payload), encoding="utf-8")

    cases, repositories, _hashes = MODULE._load_cases(tmp_path)

    assert len(cases) == 2
    assert {case.task_id for case in cases} == {"task-1", "task-2"}
    assert repositories["owner__repo"]["base_commit"] == "abc"
