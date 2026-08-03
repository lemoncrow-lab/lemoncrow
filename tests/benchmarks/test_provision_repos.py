from __future__ import annotations

import json
import subprocess
from pathlib import Path

from benchmarks.codebench import provision_repos


def test_workspace_has_checkout_rejects_git_only_partial_clone(tmp_path: Path) -> None:
    ws = tmp_path / "partial"
    (ws / ".git").mkdir(parents=True)

    assert not provision_repos._workspace_has_checkout(ws)
    (ws / "README.md").write_text("ready\n")
    assert provision_repos._workspace_has_checkout(ws)


def test_eval_provision_recognizes_canonical_lemoncrow_prefix(monkeypatch, tmp_path: Path) -> None:
    ws = tmp_path / "lemoncrow-workspace"
    gold = tmp_path / "pairs.json"
    gold.write_text(
        json.dumps(
            {
                "pairs": [],
                "true_map": {},
                "repos": {
                    "lemoncrow__lemoncrow": {
                        "ws": str(ws),
                        "anchor": "v0.3.9",
                        "base_commit": "v0.3.9",
                    }
                },
            }
        )
    )

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(cmd)
        if "checkout" in cmd:
            ws.mkdir(parents=True, exist_ok=True)
            (ws / "README.md").write_text("fixture\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(provision_repos.subprocess, "run", fake_run)

    provision_repos.ensure_eval_workspaces([gold])

    remote_calls = [cmd for cmd in calls if "remote" in cmd and "add" in cmd]
    assert remote_calls
    assert provision_repos._LEMONCROW_URL in remote_calls[0]
    assert any("v0.3.9" in cmd for cmd in calls)


def test_eval_provision_falls_back_to_local_historical_tag(monkeypatch, tmp_path: Path) -> None:
    ws = tmp_path / "lemoncrow-workspace"
    gold = tmp_path / "pairs.json"
    gold.write_text(
        json.dumps(
            {
                "pairs": [],
                "true_map": {},
                "repos": {
                    "lemoncrow__lemoncrow": {
                        "ws": str(ws),
                        "anchor": "v0.3.9",
                        "base_commit": "v0.3.9",
                    }
                },
            }
        )
    )

    local_source = tmp_path / "source"
    (local_source / ".git").mkdir(parents=True)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(cmd)
        if "fetch" in cmd:
            raise subprocess.CalledProcessError(128, cmd)
        if "checkout" in cmd:
            ws.mkdir(parents=True, exist_ok=True)
            (ws / "README.md").write_text("historical fixture\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(provision_repos, "_local_lemoncrow_checkout", lambda: local_source)
    monkeypatch.setattr(provision_repos.subprocess, "run", fake_run)

    provision_repos.ensure_eval_workspaces([gold])

    assert any(cmd[:2] == ["git", "clone"] and str(local_source) in cmd for cmd in calls)
    assert provision_repos._workspace_has_checkout(ws)
