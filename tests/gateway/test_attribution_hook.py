from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from lemoncrow.gateway.cli.commands.admin import _install_attribution_hook

SCRIPT = Path("integrations/claude/plugin/scripts/install_attribution_hook.sh").resolve()
EMAIL = "302591943+lemoncrow-agent[bot]@users.noreply.github.com"


def _trailer(name: str) -> str:
    return f"Co-Authored-By: {name} <{EMAIL}>"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


def _commit(repo: Path, fname: str, message: str = "msg") -> str:
    (repo / fname).write_text("x", encoding="utf-8")
    _git(repo, "add", "-A")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", message],
        capture_output=True,
        text=True,
        check=True,
    )
    return _git(repo, "log", "-1", "--pretty=%B").stdout


def _session_state(repo: Path, **state: str) -> None:
    path = repo / ".lemoncrow" / "workspace" / "session_state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def _install_script(repo: Path) -> None:
    result = subprocess.run(["bash", str(SCRIPT), str(repo)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _install_admin(repo: Path) -> None:
    assert _install_attribution_hook(repo) is not None


def _new_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


installers = pytest.mark.parametrize("install", [_install_script, _install_admin], ids=["script", "admin"])


def test_installer_adds_trailer_idempotently(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)

    # Installing twice must be idempotent (single managed block).
    for _ in range(2):
        _install_script(repo)
    hook_text = (repo / ".git" / "hooks" / "prepare-commit-msg").read_text(encoding="utf-8")
    assert hook_text.count("# >>> lemoncrow attribution >>>") == 1

    msg = _commit(repo, "a.txt")
    assert msg.count(_trailer("lemoncrow")) == 1

    # A second commit also gets exactly one trailer (no duplication).
    msg2 = _commit(repo, "b.txt")
    assert msg2.count(_trailer("lemoncrow")) == 1


@installers
def test_model_is_folded_into_the_co_author_name(tmp_path: Path, install) -> None:  # type: ignore[no-untyped-def]
    repo = _new_repo(tmp_path)
    _session_state(repo, session_id="sess-123", model="claude-sonnet-5")
    install(repo)

    msg = _commit(repo, "a.txt")

    assert msg.count(_trailer("lemoncrow+claude-sonnet-5")) == 1
    assert msg.count("Co-Authored-By:") == 1
    assert "LemonCrow-Session" not in msg
    assert "LemonCrow-Model" not in msg
    assert "sess-123" not in msg


@installers
def test_unknown_model_falls_back_to_plain_name(tmp_path: Path, install) -> None:  # type: ignore[no-untyped-def]
    repo = _new_repo(tmp_path)
    _session_state(repo, session_id="sess-123")
    install(repo)

    assert _commit(repo, "a.txt").count(_trailer("lemoncrow")) == 1


@installers
def test_model_name_is_sanitised(tmp_path: Path, install) -> None:  # type: ignore[no-untyped-def]
    repo = _new_repo(tmp_path)
    _session_state(repo, model="claude-opus-4-5[1m] <x>")
    install(repo)

    msg = _commit(repo, "a.txt")

    assert msg.count(_trailer("lemoncrow+claude-opus-4-5-1m---x")) == 1


@installers
def test_existing_bot_trailer_is_not_duplicated(tmp_path: Path, install) -> None:  # type: ignore[no-untyped-def]
    repo = _new_repo(tmp_path)
    _session_state(repo, model="claude-sonnet-5")
    install(repo)

    msg = _commit(repo, "a.txt", message=f"msg\n\n{_trailer('lemoncrow')}")

    assert msg.count(f"<{EMAIL}>") == 1


def test_installer_rejects_non_repo(tmp_path: Path) -> None:
    result = subprocess.run(["bash", str(SCRIPT), str(tmp_path)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "not a git repository" in result.stderr
