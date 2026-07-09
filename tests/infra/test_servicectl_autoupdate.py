"""Tests for the servicectl daemon auto-update.

Atelier ships only two ways (git checkout, GitHub-release install), so the
daemon auto-update has two paths. The release path is the subtle one: it must
launch a *detached* installer and then NOT signal the daemon to exit — otherwise
the service manager restarts the daemon, it re-checks, and relaunches the
installer in a loop before the first one lands.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atelier.infra.runtime import servicectl_lifecycle as svc

# --------------------------------------------------------------------------- #
# detection                                                                   #
# --------------------------------------------------------------------------- #


def test_detect_release_when_no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "_git_project_root", lambda: None)
    assert svc._detect_auto_update_method() == ("release", None)


def test_detect_git_when_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(svc, "_git_project_root", lambda: tmp_path)
    assert svc._detect_auto_update_method() == ("git", str(tmp_path))


# --------------------------------------------------------------------------- #
# release path: detached installer                                            #
# --------------------------------------------------------------------------- #
def test_update_via_release_respects_explicit_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    monkeypatch.setenv("ATELIER_AUTO_UPDATE_RELEASE", "0")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/bash")

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("must not launch installer when release update is disabled")

    monkeypatch.setattr(svc.subprocess, "Popen", _boom)
    assert svc._update_via_release() is False


def test_update_via_release_noop_when_current(monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    monkeypatch.delenv("ATELIER_AUTO_UPDATE_RELEASE", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/bash")
    monkeypatch.setattr(svc, "_github_latest_version", lambda: "1.0.0")
    monkeypatch.setattr(svc, "_atelier_version", lambda: "1.0.0")

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("must not launch installer when already current")

    monkeypatch.setattr(svc.subprocess, "Popen", _boom)
    assert svc._update_via_release() is False


def test_update_via_release_noop_when_latest_is_lower(monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    monkeypatch.delenv("ATELIER_AUTO_UPDATE_RELEASE", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/bash")
    monkeypatch.setattr(svc, "_github_latest_version", lambda: "1.0.0")
    monkeypatch.setattr(svc, "_atelier_version", lambda: "1.1.0")

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("must not launch installer for a lower version")

    monkeypatch.setattr(svc.subprocess, "Popen", _boom)
    assert svc._update_via_release() is False


def test_update_via_release_launches_detached_installer(monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil
    import urllib.request

    monkeypatch.setenv("ATELIER_AUTO_UPDATE_RELEASE", "1")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/bash")
    monkeypatch.setattr(shutil, "copyfileobj", lambda _src, _dst: None)
    monkeypatch.setattr(svc, "_github_latest_version", lambda: "2.0.0")
    monkeypatch.setattr(svc, "_atelier_version", lambda: "1.0.0")

    class _Resp:
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: _Resp())

    captured: dict[str, object] = {}

    class _Popen:
        def __init__(self, cmd: list[str], **kwargs: object) -> None:
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

    monkeypatch.setattr(svc.subprocess, "Popen", _Popen)

    assert svc._update_via_release() is True
    assert isinstance(captured["cmd"], list) and captured["cmd"][0] == "bash"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["ATELIER_NON_INTERACTIVE"] == "1"  # type: ignore[index]


# --------------------------------------------------------------------------- #
# orchestrator: exit semantics                                                #
# --------------------------------------------------------------------------- #


def test_release_apply_does_not_signal_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Release path returns False even when an installer launches, so the daemon
    does not exit-and-relaunch on the next tick before the installer lands."""
    monkeypatch.setattr(svc, "_detect_auto_update_method", lambda: ("release", None))
    monkeypatch.setattr(svc, "_atelier_version", lambda: "1.0.0")
    monkeypatch.setattr(svc, "_update_via_release", lambda: True)
    assert svc._servicectl_check_and_apply_updates(tmp_path) is False


def test_git_apply_signals_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Git path returns True (caller exits for an immediate restart on new code)."""
    (tmp_path / "pyproject.toml").write_text('version = "9.9.9"\n', encoding="utf-8")
    monkeypatch.setattr(svc, "_detect_auto_update_method", lambda: ("git", str(tmp_path)))
    monkeypatch.setattr(svc, "_atelier_version", lambda: "1.0.0")
    monkeypatch.setattr(svc, "_update_via_git", lambda _root: True)
    monkeypatch.setattr(svc, "_stack_restart", lambda: None)

    import atelier.core.foundation.update_state as update_state

    monkeypatch.setattr(update_state, "write_update_state", lambda **_k: None)
    assert svc._servicectl_check_and_apply_updates(tmp_path) is True


def test_git_apply_noop_when_up_to_date(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(svc, "_detect_auto_update_method", lambda: ("git", str(tmp_path)))
    monkeypatch.setattr(svc, "_atelier_version", lambda: "1.0.0")
    monkeypatch.setattr(svc, "_update_via_git", lambda _root: False)

    def _boom() -> None:
        raise AssertionError("must not restart when up-to-date")

    monkeypatch.setattr(svc, "_stack_restart", _boom)
    assert svc._servicectl_check_and_apply_updates(tmp_path) is False


class _FakeCompleted:
    """Minimal stand-in for subprocess.CompletedProcess used by the git tests."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_update_via_git_tracks_origin_main_not_upstream(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Auto-update fetches and compares against origin/main directly, never the
    current branch's @{u} — which exits 128 on a branch with no upstream and was
    the original recurring 'Auto-update failed' error."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> _FakeCompleted:
        calls.append(cmd)
        if cmd[:2] == ["git", "show"]:
            return _FakeCompleted(stdout='version = "2.0.0"\n')
        if cmd[:2] == ["git", "rev-list"]:
            return _FakeCompleted(stdout="2\n")
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(svc, "_atelier_version", lambda: "1.0.0")
    monkeypatch.setattr(svc.subprocess, "run", fake_run)

    assert svc._update_via_git(str(tmp_path)) is True
    joined = [" ".join(c) for c in calls]
    assert any(j == "git fetch --quiet origin main" for j in joined)
    assert any("rev-list HEAD..origin/main --count" in j for j in joined)
    assert any(c[:3] == ["git", "merge", "--ff-only"] and "origin/main" in c for c in calls)
    assert not any("@{u}" in j for j in joined)


def test_update_via_git_returns_false_when_cannot_fast_forward(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A branch that has diverged from main cannot fast-forward. The daemon must
    skip cleanly (return False) instead of raising CalledProcessError on every
    tick — the bug that spammed 'Auto-update failed'."""

    def fake_run(cmd: list[str], **_kwargs: object) -> _FakeCompleted:
        if cmd[:2] == ["git", "show"]:
            return _FakeCompleted(stdout='version = "2.0.0"\n')
        if cmd[:2] == ["git", "rev-list"]:
            return _FakeCompleted(stdout="5\n")
        if cmd[:2] == ["git", "merge"]:
            return _FakeCompleted(returncode=1, stderr="fatal: Not possible to fast-forward")
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(svc, "_atelier_version", lambda: "1.0.0")
    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    assert svc._update_via_git(str(tmp_path)) is False


def test_update_via_git_skips_when_remote_version_is_not_higher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(cmd: list[str], **_kwargs: object) -> _FakeCompleted:
        if cmd[:2] == ["git", "show"]:
            return _FakeCompleted(stdout='version = "1.0.0"\n')
        if cmd[:2] == ["git", "merge"]:
            raise AssertionError("must not merge a non-higher version")
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(svc, "_atelier_version", lambda: "1.1.0")
    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    assert svc._update_via_git(str(tmp_path)) is False


def test_update_via_git_skips_when_origin_main_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """If origin/main is not present, skip without running rev-list or raising."""

    def fake_run(cmd: list[str], **_kwargs: object) -> _FakeCompleted:
        if cmd[:2] == ["git", "rev-parse"]:
            return _FakeCompleted(returncode=1)
        if cmd[:2] == ["git", "rev-list"]:
            raise AssertionError("must not run rev-list when origin/main is missing")
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    assert svc._update_via_git(str(tmp_path)) is False
