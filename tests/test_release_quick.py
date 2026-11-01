"""The fast release-candidate profile is strict but cannot qualify a release."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import release_quick as quick


def _passing_report(commit: str) -> dict:
    return {
        "verdict": "pass",
        "releasable": False,
        "selection": {"partial": True, "run": list(quick.QUICK_IDS)},
        "git": {"commit": commit, "dirty": False, "dirty_at_end": False},
        "cleanup_problems": [],
        "components": [{"id": name, "verdict": "pass"} for name in quick.QUICK_IDS],
    }


def test_quick_selection_names_real_components_and_all_dependencies() -> None:
    quick.validate_selection()
    assert "suite-root" not in quick.QUICK_IDS
    assert "suite-enterprise" not in quick.QUICK_IDS
    assert "review-e2e" not in quick.QUICK_IDS
    assert "perf-slo" not in quick.QUICK_IDS
    assert {"pg-upgrade", "pg-pitr", "candidate-upgrade", "candidate-compiled"} <= set(quick.QUICK_IDS)


@pytest.mark.parametrize(
    "change",
    [
        {"releasable": True},
        {"verdict": "fail"},
        {"selection": {"partial": False, "run": list(quick.QUICK_IDS)}},
        {"selection": {"partial": True, "run": list(quick.QUICK_IDS[:-1])}},
        {"git": {"commit": "wrong", "dirty": False, "dirty_at_end": False}},
        {"git": {"commit": "commit", "dirty": True, "dirty_at_end": False}},
        {"git": {"commit": "commit", "dirty": False, "dirty_at_end": True}},
        {"cleanup_problems": ["leaked container"]},
        {"components": [{"id": "bootstrap-python", "verdict": "pass"}]},
    ],
)
def test_quick_report_rejects_partial_or_unattested_evidence(change: dict) -> None:
    report = {**_passing_report("commit"), **change}
    assert not quick.qualified_quick_report(report, commit="commit")


def test_quick_report_accepts_only_complete_nonrelease_pass() -> None:
    assert quick.qualified_quick_report(_passing_report("commit"), commit="commit")


def test_quick_runner_accepts_gate_partial_success_without_releasing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report_path = tmp_path / "quick.json"
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        if "--preflight" not in command:
            report_path.write_text(json.dumps(_passing_report("commit")), encoding="utf-8")
        return SimpleNamespace(returncode=0 if "--preflight" in command else 2)

    monkeypatch.setattr(quick.subprocess, "run", run)
    monkeypatch.setattr(quick.subprocess, "check_output", lambda *_a, **_kw: "commit\n")
    assert quick.main(["--tag", "v0.8.0", "--report", str(report_path)]) == 0
    assert len(calls) == 2
    assert "--only" in calls[0] and "--checkout" in calls[1]
    assert "--preflight" not in calls[1]


def test_quick_runner_refuses_failed_partial_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report_path = tmp_path / "quick.json"
    monkeypatch.setattr(quick.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(returncode=1))
    assert quick.main(["--tag", "v0.8.0", "--report", str(report_path)]) == 1


def test_only_full_qualification_can_write_release_record() -> None:
    makefile = (quick.release_gate.REPO / "Makefile").read_text(encoding="utf-8")
    recipe = makefile.split("release-quick:", 1)[1].split("\n\n", 1)[0]
    assert "scripts.release_quick" in recipe
    assert "release_record stamp" not in recipe
    assert "release-gate" not in recipe
    assert "release-qualify:" in makefile
