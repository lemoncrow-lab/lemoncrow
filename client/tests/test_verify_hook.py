"""``lc verify hook install|uninstall`` against real repositories, and the hook it writes run by a real ``git push``."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from lemoncrow_client import uninstall
from lemoncrow_client.verify import verify_main
from lemoncrow_client.verify_hook import HOOK_MARKER, HOOK_MARKER_PREFIX, hook_script

CLIENT_SRC = Path(__file__).resolve().parents[1] / "src"
DEFINITION = 'version = 1\n\n[[checks]]\nid = "no-bad"\npaths = ["src"]\nrun = "! grep -q BAD src/*"\n'


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    config = home / "gitconfig"
    config.write_text("[user]\n\tname = Tester\n\temail = <redacted-email>\n[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LEMONCROW_HOME", raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    # The `lc` the hook calls: this checkout's client.
    lc = tmp_path / "bin" / "lc"
    lc.parent.mkdir()
    lc.write_text(f'#!/bin/sh\nPYTHONPATH={CLIENT_SRC} exec {sys.executable} -m lemoncrow_client "$@"\n')
    lc.chmod(0o755)
    monkeypatch.setenv("LEMONCROW_LC_BIN", str(lc))
    git(tmp_path, "init", "-q", "--bare", "origin.git")
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / ".lemoncrow").mkdir()
    (repo / ".lemoncrow" / "environment.toml").write_text(DEFINITION)
    (repo / "src" / "a.txt").write_text("fine\n")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "remote", "add", "origin", str(tmp_path / "origin.git"))
    git(repo, "push", "-q", "-u", "origin", "main")
    monkeypatch.chdir(repo / "src")
    return repo


def lc_verify(*arguments: str) -> tuple[int, str]:
    sink = io.StringIO()
    return verify_main(list(arguments), sink), sink.getvalue()


def hook_of(repo: Path) -> Path:
    return repo / ".git" / "hooks" / "pre-push"


def commit(repo: Path, text: str) -> None:
    (repo / "src" / "a.txt").write_text(text)
    git(repo, "commit", "-q", "-am", text.strip())


def test_install_writes_the_hook_once_and_records_it(repo: Path) -> None:
    code, out = lc_verify("hook", "install")

    hook = hook_of(repo)
    assert code == 0 and f"installed: {hook}" in out
    assert hook.read_text() == hook_script(os.environ["LEMONCROW_LC_BIN"]) and os.access(hook, os.X_OK)
    assert lc_verify("hook", "install") == (0, f"lc verify: pre-push hook unchanged: {hook}\n")
    record = Path(os.environ["HOME"]) / ".lemoncrow" / "installed-hooks.txt"
    assert record.read_text().splitlines() == [str(hook)]


def test_the_hook_stops_a_push_whose_check_fails_and_lets_a_passing_one_through(repo: Path) -> None:
    assert lc_verify("hook", "install")[0] == 0
    commit(repo, "BAD\n")

    refused = git(repo, "push", "-q", "origin", "main", check=False)

    assert refused.returncode != 0 and "FAILED: no-bad" in refused.stderr + refused.stdout
    commit(repo, "good again\n")
    assert git(repo, "push", "-q", "origin", "main", check=False).returncode == 0


def test_without_lc_the_hook_stops_the_push_and_says_how_to_remove_it(repo: Path) -> None:
    hook = hook_of(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(hook_script(str(repo / "gone" / "lc")))
    hook.chmod(0o755)
    commit(repo, "fine, changed\n")

    refused = subprocess.run(
        ["git", "push", "-q", "origin", "main"],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": "/usr/bin:/bin"},
    )

    assert (
        refused.returncode != 0
        and "lc was not found" in refused.stderr
        and "delete .git/hooks/pre-push" in refused.stderr
    )


def test_a_hook_that_is_not_ours_is_left_alone_unless_forced(repo: Path) -> None:
    hook = hook_of(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho mine\n")

    code, out = lc_verify("hook", "install")

    assert code == 2 and "is not this hook, so it was left alone" in out
    assert hook.read_text() == "#!/bin/sh\necho mine\n"
    assert lc_verify("hook", "uninstall")[0] == 2 and hook.read_text() == "#!/bin/sh\necho mine\n"
    assert lc_verify("hook", "install", "--force")[0] == 0 and HOOK_MARKER in hook.read_text()


def test_an_older_hook_of_ours_is_refreshed_without_force(repo: Path) -> None:
    hook = hook_of(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\n{HOOK_MARKER_PREFIX} v0\nLC=/old/lc\n")

    code, out = lc_verify("hook", "install")

    assert code == 0 and "replaced" in out and HOOK_MARKER in hook.read_text()


def test_print_writes_nothing_and_a_missing_definition_installs_nothing(repo: Path) -> None:
    code, out = lc_verify("hook", "install", "--print")
    assert code == 0 and out.startswith("#!/bin/sh\n") and not hook_of(repo).exists()

    (repo / ".lemoncrow" / "environment.toml").unlink()
    code, out = lc_verify("hook", "install")
    assert code == 2 and "environment.toml is missing" in out and not hook_of(repo).exists()


def test_core_hooks_path_is_where_the_hook_goes(repo: Path) -> None:
    git(repo, "config", "core.hooksPath", ".githooks")

    assert lc_verify("hook", "install")[0] == 0

    assert HOOK_MARKER in (repo / ".githooks" / "pre-push").read_text() and not hook_of(repo).exists()


def test_uninstall_removes_only_this_hook(repo: Path) -> None:
    assert lc_verify("hook", "uninstall") == (0, f"lc verify: no pre-push hook at {hook_of(repo)}; nothing to remove\n")
    assert lc_verify("hook", "install")[0] == 0

    assert lc_verify("hook", "uninstall") == (0, f"lc verify: removed {hook_of(repo)}\n")
    assert not hook_of(repo).exists()


@pytest.mark.parametrize("arguments", [[], ["install", "--yes"], ["uninstall", "--force"], ["remove"]])
def test_a_bad_command_line_is_usage(repo: Path, arguments: list[str]) -> None:
    code, out = lc_verify("hook", *arguments)

    assert code == 2 and "lc verify hook install" in out and not hook_of(repo).exists()


def test_uninstall_finds_the_verify_hook_by_the_same_marker() -> None:
    assert uninstall._VERIFY_HOOK_MARKER == HOOK_MARKER_PREFIX
