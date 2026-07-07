"""WS10 G16 -- PR-risk profile + commit-provenance classification.

Verifies the risk score rises with blast-radius / churn / missing tests, and
that heuristic commit classification labels representative messages correctly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from atelier.core.capabilities.code_health.pr_risk import (
    classify_commit_message,
    commit_provenance,
    pr_risk,
)


def _write_graph(repo: Path) -> None:
    """base.py imported by many files; lonely.py imported by nobody."""
    src = repo / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "base.py").write_text(
        "def base_fn(x: int) -> int:\n"
        "    total = 0\n"
        "    for i in range(x):\n"
        "        if i % 2 == 0:\n"
        "            total += i\n"
        "        else:\n"
        "            total -= i\n"
        "    return total\n",
        encoding="utf-8",
    )
    for name in ("a", "b", "c", "d"):
        (src / f"{name}.py").write_text(
            f"from src.base import base_fn\n\ndef {name}_fn(x: int) -> int:\n    return base_fn(x)\n",
            encoding="utf-8",
        )
    (src / "lonely.py").write_text(
        "def lonely_fn() -> int:\n    return 1\n",
        encoding="utf-8",
    )


def _index_all(repo: Path, cache_root: Path) -> None:
    from atelier.core.capabilities.semantic_file_memory import SemanticFileMemoryCapability

    cap = SemanticFileMemoryCapability(cache_root)
    for py in sorted((repo / "src").glob("*.py")):
        cap.summarize_file(py)


def test_pr_risk_rises_with_blast_radius_and_missing_tests(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    cache = tmp_path / "cache"
    _write_graph(repo)
    _index_all(repo, cache)

    high = pr_risk(repo_root=repo, atelier_root=cache, paths=["src/base.py"])
    low = pr_risk(repo_root=repo, atelier_root=cache, paths=["src/lonely.py"])

    # base.py has 4 importers + no tests + branchy complexity; lonely.py has none.
    assert high["overall_score"] > low["overall_score"]
    assert high["file_count"] == 1
    base_file = high["files"][0]
    assert base_file["factors"]["blast_radius"]["impacted_files"] >= 4
    assert base_file["factors"]["test_gap"]["missing_tests"] is True
    assert base_file["factors"]["complexity"]["factor"] > 0.0
    assert high["overall_tier"] in {"low", "medium", "high", "critical"}
    assert 0.0 <= high["overall_score"] <= 1.0


def test_pr_risk_test_gap_lowers_score_when_tests_present(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    cache = tmp_path / "cache"
    _write_graph(repo)
    # Add a linked test for base so the test-gap factor is removed.
    tests = repo / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_base.py").write_text(
        "from src.base import base_fn\n\ndef test_base() -> None:\n    assert base_fn(2) == 0\n",
        encoding="utf-8",
    )
    from atelier.core.capabilities.semantic_file_memory import SemanticFileMemoryCapability

    cap = SemanticFileMemoryCapability(cache)
    for py in sorted((repo / "src").glob("*.py")):
        cap.summarize_file(py)
    cap.summarize_file(tests / "test_base.py")

    result = pr_risk(repo_root=repo, atelier_root=cache, paths=["src/base.py"])
    base_file = result["files"][0]
    # The linked test is discovered, so the test-gap penalty is gone.
    assert base_file["factors"]["test_gap"]["missing_tests"] is False
    assert base_file["factors"]["test_gap"]["factor"] == 0.0


def test_pr_risk_empty_paths_yields_zero_fail_open(tmp_path: Path) -> None:
    # Direct call with no targets must not raise -- it returns a valid zero shape.
    # (The graph-tool seam separately rejects empty targets with a ValueError.)
    result = pr_risk(repo_root=tmp_path, atelier_root=tmp_path / "cache", paths=[])
    assert result["kind"] == "pr_risk"
    assert result["overall_score"] == 0.0
    assert result["overall_tier"] == "low"
    assert result["file_count"] == 0


def test_classify_commit_message_samples() -> None:
    cases = {
        "fix: null deref in parser": "bugfix",
        "Fixed a crash when input is empty": "bugfix",
        "feat(api): add pagination support": "feature",
        "Implement retry logic for uploads": "feature",
        "refactor: extract helper from monolith": "refactor",
        "perf: optimize hot loop": "perf",
        "Rename FooService to BarService": "rename",
        'Revert "feat: add pagination"': "revert",
        "docs: update README": "docs",
        "test: add coverage for edge cases": "test",
        "chore: bump dependencies": "chore",
    }
    for message, expected in cases.items():
        verdict = classify_commit_message(message)
        assert verdict["category"] == expected, f"{message!r} -> {verdict}"
        assert 0.0 < verdict["confidence"] <= 1.0


def test_classify_commit_message_revert_body_and_conventional_priority() -> None:
    # Conventional prefix wins over free-text keywords in the body.
    conv = classify_commit_message("feat: add thing\n\nthis also fixes a bug")
    assert conv["category"] == "feature"
    assert conv["signal"] == "conventional_prefix"
    # Revert detected from the body signature even with a plain subject.
    rev = classify_commit_message("Roll back change\n\nThis reverts commit abc123.")
    assert rev["category"] == "revert"


def test_classify_commit_message_file_shape_fallback() -> None:
    docs = classify_commit_message("misc", ["docs/guide.md", "README.md"])
    assert docs["category"] == "docs"
    assert docs["signal"] == "file_shape"
    tests = classify_commit_message("misc", ["tests/test_a.py", "tests/test_b.py"])
    assert tests["category"] == "test"


def _git(args: list[str], repo: Path) -> None:
    import os

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@t.com",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@t.com",
    }
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)


def test_commit_provenance_classifies_real_repo(tmp_path: Path) -> None:
    try:
        import pygit2  # noqa: F401
    except ImportError:
        pytest.skip("pygit2 not available")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "user.email", "t@t.com"], repo)
    (repo / "README.md").write_text("init", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "initial"], repo)

    commits = [
        ("feat: add module", "mod.py", "x = 1\n"),
        ("fix: correct off-by-one", "mod.py", "x = 2\n"),
        ("docs: document module", "GUIDE.md", "guide\n"),
    ]
    for message, fname, content in commits:
        (repo / fname).write_text(content, encoding="utf-8")
        _git(["add", "-A"], repo)
        _git(["commit", "-m", message], repo)

    result = commit_provenance(repo_root=repo, path=None, limit=50)
    assert result["kind"] == "commit_provenance"
    cats = result["by_category"]
    assert cats.get("feature", 0) >= 1
    assert cats.get("bugfix", 0) >= 1
    assert cats.get("docs", 0) >= 1

    # Path-scoped: only commits touching mod.py.
    scoped = commit_provenance(repo_root=repo, path="mod.py", limit=50)
    scoped_cats = scoped["by_category"]
    assert scoped_cats.get("feature", 0) >= 1
    assert scoped_cats.get("bugfix", 0) >= 1
    assert "docs" not in scoped_cats  # GUIDE.md commit excluded


def test_commit_provenance_fail_open_non_git(tmp_path: Path) -> None:
    result = commit_provenance(repo_root=tmp_path / "not_a_repo", path=None, limit=10)
    assert result["kind"] == "commit_provenance"
    assert result["commit_count"] == 0
    assert result["commits"] == []
