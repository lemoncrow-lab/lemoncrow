"""Verify-before-done: throwaway working state must not demand a verification run.

Scratchpad and temp files are intermediate state the agent writes for its own
use -- the gate nagging "run tests" there costs a turn on something with no
suite and no consumer. A temp path is exempt only when it falls outside the
project being worked in, so a repo cloned under /tmp stays gated. Bench mode
stays strict everywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.verify_gate import (
    is_scratch_path,
    is_verifiable_path,
    scratch_prefixes,
    temp_roots,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "LEMONCROW_BENCH_MODE",
        "LEMONCROW_VERIFY_SKIP_PATHS",
        "LEMONCROW_VERIFY_SKIP_SUFFIXES",
    ):
        monkeypatch.delenv(var, raising=False)
    # Pin the project root so temp-vs-project resolution is deterministic.
    monkeypatch.chdir(Path(__file__).resolve().parents[2])


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/report.html",
        "/tmp/nested/dir/probe.py",
        "/var/tmp/dump.json",
        "/tmp/claude-1000/-some-project/abc/scratchpad/four-seams.html",
        # a scratchpad segment is exempt wherever it lives
        "/home/me/work/scratchpad/notes.py",
    ],
)
def test_throwaway_paths_are_not_verifiable(path: str) -> None:
    assert is_scratch_path(path) is True
    assert is_verifiable_path(path, include_docs=True) is False


@pytest.mark.parametrize(
    "path",
    [
        "src/lemoncrow/pro/capabilities/verify_gate.py",
        "/home/me/repo/src/app.py",
        "frontend/src/review/ReviewReader.tsx",
        # a sibling directory whose name merely starts with a temp root
        "/tmpfoo/app.py",
        # 'scratchpad' as a filename fragment, not a path segment
        "/home/me/repo/scratchpad_helpers.py",
    ],
)
def test_project_paths_stay_verifiable(path: str) -> None:
    assert is_scratch_path(path) is False
    assert is_verifiable_path(path) is True


def test_project_living_under_tmp_is_still_gated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A repo cloned to /tmp is a real project; only files outside it are scratch.
    repo = tmp_path / "myrepo"
    (repo / "src").mkdir(parents=True)
    monkeypatch.chdir(repo)
    assert is_scratch_path(str(repo / "src" / "app.py")) is False
    assert is_verifiable_path(str(repo / "src" / "app.py")) is True
    # ...while a temp file written from that repo is not part of it
    assert is_scratch_path(str(tmp_path / "notes.py")) is True


def test_explicit_project_root_overrides_cwd(tmp_path: Path) -> None:
    repo = tmp_path / "ws"
    repo.mkdir()
    assert is_scratch_path(str(repo / "mod.py"), project_root=str(repo)) is False
    assert is_scratch_path(str(tmp_path / "other.py"), project_root=str(repo)) is True


def test_relative_paths_resolve_against_cwd() -> None:
    # A bare relative path is inside the project by construction.
    assert is_scratch_path("src/app.py") is False


def test_bench_mode_keeps_everything_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_BENCH_MODE", "on")
    assert is_scratch_path("/tmp/answer.py") is False
    assert is_scratch_path("/work/scratchpad/answer.py") is False
    assert is_verifiable_path("/tmp/answer.py", include_docs=True) is True


def test_extra_prefixes_are_unconditional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_VERIFY_SKIP_PATHS", "/srv/cache, /data/dumps")
    assert scratch_prefixes() == ("/srv/cache", "/data/dumps")
    assert is_scratch_path("/srv/cache/x.py") is True
    assert is_scratch_path("/data/other/x.py") is False


def test_tmpdir_env_is_honoured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import tempfile

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", None)  # clear cached gettempdir()
    assert str(tmp_path) in temp_roots()
