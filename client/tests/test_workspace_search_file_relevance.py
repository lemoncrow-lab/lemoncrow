"""Tests of the read-only, source-derived retrieval quality harness."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from lemoncrow_client.lite.index import LiteIndex
from lemoncrow_client.workspace_members import WorkspaceRepo

from benchmarks import workspace_search_file_relevance as evaluator


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "workspace" / "Fixture"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    (root / "api.py").write_text(
        "def unique_company_invoice_handler():\n    return 'approved'\n"
        "def duplicated_implementation():\n    return True\n",
        encoding="utf-8",
    )
    (root / "second.py").write_text("def duplicated_implementation():\n    return False\n", encoding="utf-8")
    (root / "worker.ts").write_text("export function authorizeAccountSwitching() { return true; }\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_worker.py").write_text(
        "def unindexed_internal_test_handler():\n    return True\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
        cwd=root,
        check=True,
    )
    return root


def test_gold_is_derived_from_unique_source_definitions_not_search_results(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    repo = WorkspaceRepo(root=root, relpath="Fixture")
    cases = evaluator.source_cases(repo, cap=10)
    assert {(case.identifier, case.path) for case in cases} == {
        ("unique_company_invoice_handler", "api.py"),
        ("authorizeAccountSwitching", "worker.ts"),
    }
    assert cases == evaluator.source_cases(repo, cap=10)
    assert evaluator.phrasing("authorizeAccountSwitching") == "where is authorize account switching implemented"


def test_reader_cannot_create_or_mutate_index(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    writer = LiteIndex(root)
    writer.prepare()
    before = writer.db_path.stat()
    reader = evaluator.ExistingIndex(root)
    with pytest.raises(AssertionError, match="never initialize or write"):
        reader._connect(initialize=True)
    with pytest.raises(AssertionError, match="never initialize or write"):
        reader._connect(readonly=False)
    with contextlib.closing(reader._connect(readonly=True)) as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM files")
    result = reader.search("unique_company_invoice_handler", limit=10)
    assert result["files"][0]["path"] == "api.py"
    after = writer.db_path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def test_aggregate_eval_does_not_export_private_queries_or_snippets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path)
    writer = LiteIndex(root)
    writer.prepare()
    monkeypatch.setattr(evaluator, "ROOT_REPOS", ("Fixture",))
    report = evaluator.evaluate(root.parent, cases_per_repo=1, global_cases_per_repo=1)
    assert report["read_only_existing_indexes"] is True
    assert report["local"]["identifier"]["hit_1"] == 1
    assert report["cross_repo_complete"]["identifier"]["hit_1"] == 1
    serialized = json.dumps(report)
    assert "unique_company_invoice_handler" not in serialized
    assert "authorizeAccountSwitching" not in serialized
    assert "approved" not in serialized


def test_rank_and_mrr_count_missing_files_without_padding() -> None:
    assert evaluator.rank(["foo", "bar"], "bar") == 2
    assert evaluator.rank(["foo", "bar"], "missing") is None
    assert evaluator.rank(["foo", "bar"], "bar", cutoff=1) is None
    assert evaluator.summary([1, 3, None]) == {
        "cases": 3,
        "hit_1": 1,
        "hit_3": 2,
        "hit_5": 2,
        "hit_10": 2,
        "mrr_10": 0.4444,
    }
