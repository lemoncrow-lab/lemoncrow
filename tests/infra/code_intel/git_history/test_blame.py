from __future__ import annotations

import subprocess
from dataclasses import is_dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lemoncrow.infra.code_intel.git_history.blame import BlameAnnotator
from lemoncrow.infra.code_intel.git_history.models import BlameRangeAnnotation, BlameRequest


def _git(args: list[str], repo_root: Path, *, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout.strip()


def _commit(repo_root: Path, *, message: str, author_name: str, author_email: str, when: datetime) -> str:
    env = {
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": author_email,
        "GIT_AUTHOR_DATE": when.isoformat(),
        "GIT_COMMITTER_DATE": when.isoformat(),
    }
    _git(["add", "service.py"], repo_root, env=env)
    _git(["commit", "-m", message], repo_root, env=env)
    return _git(["rev-parse", "HEAD"], repo_root)


def _build_blame_fixture(tmp_path: Path) -> tuple[Path, str, str]:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(["init"], repo_root)
    _git(["config", "user.name", "Fixture Tester"], repo_root)
    _git(["config", "user.email", "fixture@example.com"], repo_root)

    now = datetime.now(tz=UTC)
    service_path = repo_root / "service.py"

    service_path.write_text(
        "def risk_score() -> int:\n    value = 1\n    return value\n",
        encoding="utf-8",
    )
    _commit(
        repo_root,
        message="add risk score",
        author_name="Alice",
        author_email="alice@example.com",
        when=now - timedelta(days=240),
    )

    service_path.write_text(
        "def risk_score() -> int:\n    value = 3\n    return value\n",
        encoding="utf-8",
    )
    indexed_sha = _commit(
        repo_root,
        message="tune risk score",
        author_name="Bob",
        author_email="bob@example.com",
        when=now - timedelta(days=30),
    )

    service_path.write_text(
        "def risk_score() -> int:\n    value = 5\n    return value\n",
        encoding="utf-8",
    )
    head_sha = _commit(
        repo_root,
        message="finalize risk score",
        author_name="Carol",
        author_email="carol@example.com",
        when=now - timedelta(days=7),
    )
    return repo_root, indexed_sha, head_sha


def test_blame_annotator_returns_real_repo_metadata_with_optional_churn(tmp_path: Path) -> None:
    repo_root, _indexed_sha, head_sha = _build_blame_fixture(tmp_path)
    annotator = BlameAnnotator(repo_root)

    annotation = annotator.annotate(
        BlameRequest(
            file_path="service.py",
            line_start=1,
            line_end=3,
            index_sha=head_sha,
            head_sha=head_sha,
        )
    )

    assert isinstance(annotation, BlameRangeAnnotation)
    assert annotation.last_author == "carol@example.com"
    assert annotation.last_commit_sha == head_sha
    assert annotation.age_days >= 0
    assert annotation.churn is not None
    assert annotation.churn.commit_count == 2
    assert annotation.churn.score > 0.0


def test_blame_annotator_reuses_typed_cached_results_for_repeated_requests(tmp_path: Path) -> None:
    repo_root, _indexed_sha, head_sha = _build_blame_fixture(tmp_path)
    annotator = BlameAnnotator(repo_root)
    request = BlameRequest(
        file_path="service.py",
        line_start=1,
        line_end=3,
        index_sha=head_sha,
        head_sha=head_sha,
    )

    first = annotator.annotate(request)
    second = annotator.annotate(request)

    assert is_dataclass(first)
    assert first is second


def test_blame_annotator_marks_local_edits_and_stale_index_explicitly(tmp_path: Path) -> None:
    repo_root, indexed_sha, head_sha = _build_blame_fixture(tmp_path)
    (repo_root / "service.py").write_text(
        "def risk_score() -> int:\n    value = 5\n    return value\n# local note\n",
        encoding="utf-8",
    )
    annotator = BlameAnnotator(repo_root)

    annotation = annotator.annotate(
        BlameRequest(
            file_path="service.py",
            line_start=1,
            line_end=3,
            index_sha=indexed_sha,
            head_sha=head_sha,
        )
    )

    assert annotation.local_edits is True
    assert annotation.freshness == "stale"
    assert annotation.index_sha == indexed_sha
    assert annotation.head_sha == head_sha
