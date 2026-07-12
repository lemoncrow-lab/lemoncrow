from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path("integrations/claude/plugin/scripts/install_attribution_hook.sh").resolve()
TRAILER = "Co-Authored-By: lemoncrow <302591943+lemoncrow-agent[bot]@users.noreply.github.com>"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


def _commit(repo: Path, fname: str) -> str:
    (repo / fname).write_text("x", encoding="utf-8")
    _git(repo, "add", "-A")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "msg"],
        capture_output=True,
        text=True,
        check=True,
    )
    return _git(repo, "log", "-1", "--pretty=%B").stdout


def test_installer_adds_trailer_idempotently(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")

    # Installing twice must be idempotent (single managed block).
    for _ in range(2):
        result = subprocess.run(["bash", str(SCRIPT), str(repo)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    hook_text = (repo / ".git" / "hooks" / "prepare-commit-msg").read_text(encoding="utf-8")
    assert hook_text.count("# >>> lemoncrow attribution >>>") == 1

    msg = _commit(repo, "a.txt")
    assert msg.count(TRAILER) == 1

    # A second commit also gets exactly one trailer (no duplication).
    msg2 = _commit(repo, "b.txt")
    assert msg2.count(TRAILER) == 1


def test_installer_rejects_non_repo(tmp_path: Path) -> None:
    result = subprocess.run(["bash", str(SCRIPT), str(tmp_path)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "not a git repository" in result.stderr
