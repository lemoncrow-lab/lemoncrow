"""The run ledger's ``"git"`` anchor: the forward fix that makes correlation exact.

No session record stored a commit sha before this key existed, which is why
``lc review`` provenance has to score candidates instead of joining on one.
Stamping HEAD into ``run.json`` closes that for every future run -- but only if
it is free: the resolution reads ``.git`` directly, because forking ``git`` from
the multi-threaded MCP process costs seconds per call, and it degrades to empty
strings rather than ever failing a snapshot.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pygit2
import pytest

from lemoncrow.infra.runtime.run_ledger import RunLedger


def _init_repo(root: Path) -> Any:
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.com"
    return repo


def _commit(repo: Any, root: Path) -> str:
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    signature = pygit2.Signature("Fixture Tester", "fixture@example.com", 1700000000, 0)
    return str(repo.create_commit("HEAD", signature, signature, "one", tree, []))


def test_snapshot_records_git_head_and_branch(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    repo = _init_repo(root)
    sha = _commit(repo, root)

    ledger = RunLedger(session_id="s1", agent="claude", workspace_path=root)
    anchor = ledger.snapshot()["git"]

    assert anchor["head"] == sha
    assert anchor["branch"] == "main"
    assert Path(anchor["repo_root"]) == root.resolve()


def test_snapshot_records_git_from_a_nested_workspace(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    repo = _init_repo(root)
    sha = _commit(repo, root)
    nested = root / "src" / "pkg"
    nested.mkdir(parents=True)

    ledger = RunLedger(session_id="s1", agent="claude", workspace_path=nested)

    assert ledger.snapshot()["git"]["head"] == sha


def test_snapshot_git_empty_outside_repo(tmp_path: Path) -> None:
    workspace = tmp_path / "plain"
    workspace.mkdir()

    ledger = RunLedger(session_id="s1", agent="claude", workspace_path=workspace)

    assert ledger.snapshot()["git"] == {"head": "", "branch": "", "repo_root": ""}


def test_snapshot_git_empty_without_a_workspace() -> None:
    ledger = RunLedger(session_id="s1", agent="claude")

    assert ledger.snapshot()["git"] == {"head": "", "branch": "", "repo_root": ""}


def test_snapshot_does_not_fork_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    repo = _init_repo(root)
    sha = _commit(repo, root)

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("run ledger must never fork a subprocess to read git state")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "check_output", boom)

    ledger = RunLedger(session_id="s1", agent="claude", workspace_path=root)

    assert ledger.snapshot()["git"]["head"] == sha


def test_git_anchor_is_resolved_once(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    repo = _init_repo(root)
    sha = _commit(repo, root)

    ledger = RunLedger(session_id="s1", agent="claude", workspace_path=root)
    first = ledger.git_anchor()
    # HEAD can move mid-session; the value that makes the run correlatable is
    # the one it started from, so the cached answer must not drift.
    (root / ".git" / "refs" / "heads" / "main").write_text("f" * 40 + "\n", encoding="utf-8")

    assert first["head"] == sha
    assert ledger.git_anchor() == first
    assert ledger.snapshot()["git"] == first


def test_git_anchor_is_a_copy(tmp_path: Path) -> None:
    ledger = RunLedger(session_id="s1", agent="claude", workspace_path=tmp_path)
    anchor = ledger.git_anchor()
    anchor["head"] = "tampered"

    assert ledger.git_anchor()["head"] == ""


def test_load_tolerates_missing_git_key(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps({"session_id": "s1", "agent": "claude", "status": "running", "events": []}),
        encoding="utf-8",
    )

    ledger = RunLedger.load(path)

    assert ledger.session_id == "s1"
    assert ledger.snapshot()["git"] == {"head": "", "branch": "", "repo_root": ""}


def test_load_restores_a_recorded_git_anchor(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    repo = _init_repo(root)
    _commit(repo, root)
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "session_id": "s1",
                "agent": "claude",
                "workspace_path": str(root),
                "events": [],
                "git": {"head": "d" * 40, "branch": "feature", "repo_root": str(root)},
            }
        ),
        encoding="utf-8",
    )

    ledger = RunLedger.load(path)

    # The recorded sha is the run's truth; re-resolving would report whatever
    # HEAD happens to be at read time.
    assert ledger.snapshot()["git"]["head"] == "d" * 40
    assert ledger.snapshot()["git"]["branch"] == "feature"


def test_persisted_run_json_carries_the_anchor(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    repo = _init_repo(root)
    sha = _commit(repo, root)
    store = tmp_path / "store"

    ledger = RunLedger(session_id="s1", agent="claude", root=store, workspace_path=root)
    written = ledger.persist()

    assert json.loads(written.read_text(encoding="utf-8"))["git"]["head"] == sha
