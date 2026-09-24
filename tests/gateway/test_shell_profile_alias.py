"""The `lcr` alias the installer writes, and the uninstaller's removal of it.

A shell alias is state we put in a file we do not own, so the two things worth
pinning are that a re-install does not duplicate it and that `uninstall.sh`
takes it away again. It lives inside the same sentinel block as the PATH export
precisely so the second property comes for free -- these tests are what stops
someone moving it out of the block.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "scripts" / "lib" / "common.sh"
UNINSTALL = ROOT / "scripts" / "uninstall.sh"

_HARNESS = """
set -u
C_FRAME=""; C_RESET=""; C_GREEN=""
LEMONCROW_BIN_DIR="$HOME/.lemoncrow/bin"
LEMONCROW_NODE_DIR="$HOME/.lemoncrow/node"
source {common} >/dev/null 2>&1 || true
_ensure_path_persistence >/dev/null 2>&1
"""


def _persist(home: Path, *, shell: str, times: int = 1, env_extra: dict[str, str] | None = None) -> str:
    """Run the installer's profile writer under a throwaway HOME."""
    env = os.environ.copy()
    env.update({"HOME": str(home), "SHELL": shell})
    env.update(env_extra or {})
    script = _HARNESS.format(common=COMMON) * times
    subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True, env=env)
    profile = {
        "/bin/bash": home / ".bashrc",
        "/usr/bin/zsh": home / ".zshrc",
        "/usr/bin/fish": home / ".config" / "fish" / "config.fish",
    }[shell]
    return profile.read_text() if profile.exists() else ""


def test_the_installer_writes_the_review_alias(tmp_path: Path) -> None:
    assert "alias lcr='lc review'" in _persist(tmp_path, shell="/bin/bash")


def test_a_second_install_does_not_write_it_twice(tmp_path: Path) -> None:
    profile = _persist(tmp_path, shell="/bin/bash", times=3)
    assert profile.count("alias lcr") == 1


def test_fish_gets_fish_syntax(tmp_path: Path) -> None:
    # `alias name=value` is not fish; writing it would break every new shell.
    profile = _persist(tmp_path, shell="/usr/bin/fish")
    assert "alias lcr 'lc review'" in profile
    assert "alias lcr=" not in profile


def test_the_alias_can_be_declined(tmp_path: Path) -> None:
    profile = _persist(tmp_path, shell="/bin/bash", env_extra={"LEMONCROW_NO_ALIAS": "1"})
    assert "alias lcr" not in profile
    assert "export PATH=" in profile  # the rest of the block still lands


def test_installer_reports_exact_path_directories(tmp_path: Path) -> None:
    (tmp_path / ".lemoncrow" / "node" / "bin").mkdir(parents=True)
    env = os.environ.copy()
    env.update({"HOME": str(tmp_path), "SHELL": "/bin/bash"})

    result = subprocess.run(
        [
            "bash",
            "-c",
            _HARNESS.format(common=COMMON).replace(
                "_ensure_path_persistence >/dev/null 2>&1", "_ensure_path_persistence"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert f"Added {tmp_path}/.lemoncrow to PATHs in {tmp_path}/.bashrc" in result.stdout


def test_uninstall_takes_the_alias_away_with_the_block(tmp_path: Path) -> None:
    home = tmp_path
    _persist(home, shell="/bin/bash")
    profile = home / ".bashrc"
    profile.write_text("echo before\n" + profile.read_text() + "echo after\n")

    env = os.environ.copy()
    env.update({"HOME": str(home), "SHELL": "/bin/bash"})
    subprocess.run(
        ["bash", str(UNINSTALL), "--yes"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )

    remaining = profile.read_text()
    assert "alias lcr" not in remaining
    assert "lemoncrow" not in remaining
    # Only our block goes; the user's own lines are theirs.
    assert "echo before" in remaining
    assert "echo after" in remaining
