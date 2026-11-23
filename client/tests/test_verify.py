"""``lc verify`` and ``.lemoncrow/environment.toml`` against real Git repositories."""

from __future__ import annotations

import io
import re
import subprocess
from pathlib import Path

import pytest
from lemoncrow_client.environment import DefinitionError, parse_environment
from lemoncrow_client.verify import verify_main

ZERO = "0" * 40

DEFINITION = """\
version = 1

[[checks]]
id = "py"
paths = ["*.py", ":!experiments"]
run = "cat \\"$LC_CHANGED\\" > \\"$OUT\\""

[[checks]]
id = "docs"
paths = ["docs"]
run = "exit 3"

[[checks]]
id = "never"
paths = ["nothing-here"]
run = "exit 9"
"""


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = home / "gitconfig"
    config.write_text("[user]\n\tname = Tester\n\temail = tester@example.test\n[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("OUT", str(tmp_path / "changed.out"))


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``main`` pushed to a bare ``origin`` with a definition, ``src``, ``docs`` and ``experiments``."""

    git(tmp_path, "init", "-q", "--bare", "origin.git")
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    for name, text in {
        ".lemoncrow/environment.toml": DEFINITION,
        "src/a.py": "a = 1\n",
        "docs/guide.md": "guide\n",
        "experiments/e.py": "e = 1\n",
    }.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "remote", "add", "origin", str(tmp_path / "origin.git"))
    git(repo, "push", "-q", "-u", "origin", "main")
    git(repo, "switch", "-q", "-c", "feature")
    monkeypatch.chdir(repo / "src")  # any folder inside the repository works
    return repo


def lc_verify(*arguments: str, refs: str | None = None) -> tuple[int, str]:
    sink = io.StringIO()
    code = verify_main(list(arguments), sink, refs=io.StringIO(refs) if refs is not None else None)
    return code, sink.getvalue()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("version = 2\n", "version must be 1"),
        ("version = true\n", "version must be 1"),
        ('version = 1\n[toolchain]\nimage = "x"\nshell = "bash"\n', "toolchain: unknown key 'shell'"),
        ('version = 1\n[[checks]]\nid = "a"\npaths = ["src"]\nrun = "true"\nretries = 5\n', "unknown key 'retries'"),
        ('version = 1\n[[checks]]\nid = "A b"\npaths = ["src"]\nrun = "true"\n', "id must match"),
        ('version = 1\n[[checks]]\nid = "a"\npaths = []\nrun = "true"\n', "checks[0] (a): paths"),
        ('version = 1\n[[checks]]\nid = "a"\npaths = ["src"]\nrun = " "\n', "checks[0] (a): run"),
        (
            'version = 1\n[[checks]]\nid = "a"\npaths = ["x"]\nrun = "true"\n[[checks]]\nid = "a"\npaths = ["y"]\nrun = "true"\n',
            "duplicate id 'a'",
        ),
        ("version = ", "not valid TOML"),
    ],
)
def test_definition_refuses_what_nothing_reads_or_cannot_run(text: str, message: str) -> None:
    with pytest.raises(DefinitionError, match=re.escape(message)):
        parse_environment(text)
    assert [check.id for check in parse_environment(DEFINITION).checks] == ["py", "docs", "never"]


def test_selection_follows_git_pathspecs_with_untracked_files_deletions_and_exclusions(repo: Path) -> None:
    (repo / "experiments" / "new.py").write_text("n = 1\n")
    assert lc_verify("--list") == (0, "")  # an excluded path selects nothing

    (repo / "src" / "b.py").write_text("b = 1\n")  # untracked
    (repo / "docs" / "guide.md").unlink()  # a deletion counts
    assert lc_verify("--list") == (0, "py\ndocs\n")
    assert lc_verify("--all", "--list") == (0, "py\ndocs\n")
    assert lc_verify("--base", "no-such-ref")[0] == 2


def test_runs_selected_checks_with_the_changed_listing_and_fails_closed(repo: Path, tmp_path: Path) -> None:
    (repo / "src" / "a.py").write_text("a = 2\n")
    (repo / "docs" / "guide.md").unlink()
    code, output = lc_verify()
    assert code == 1
    assert "[py] $" in output and "[docs] $" in output and "[never]" not in output
    assert "FAILED: docs" in output
    # The listing names changed paths that still exist; the deleted guide is not in it.
    assert (tmp_path / "changed.out").read_text() == "src/a.py\n"

    (repo / ".lemoncrow" / "environment.toml").unlink()
    code, output = lc_verify()
    assert code == 2 and "environment.toml is missing" in output


def test_pre_push_checks_exactly_the_pushed_commits(repo: Path) -> None:
    origin_main = git(repo, "rev-parse", "origin/main")
    (repo / "docs" / "guide.md").write_text("guide, revised\n")
    git(repo, "commit", "-q", "-am", "docs")
    tip = git(repo, "rev-parse", "HEAD")
    (repo / "src" / "a.py").write_text("a = 2\n")  # uncommitted: not part of the push

    new_branch = f"refs/heads/feature {tip} refs/heads/feature {ZERO}\n"
    tag = f"refs/tags/v1 {tip} refs/tags/v1 {ZERO}\n"
    deletion = f"(delete) {ZERO} refs/heads/old {origin_main}\n"
    assert lc_verify("--pre-push", "origin", "url", "--list", refs=new_branch + tag + deletion) == (0, "docs\n")
    assert lc_verify("--pre-push", "--list", refs=f"refs/heads/main {tip} refs/heads/main {origin_main}\n") == (
        0,
        "docs\n",
    )
    # Only tags or deletions: nothing to check, and nothing runs.
    code, output = lc_verify("--pre-push", refs=tag + deletion)
    assert code == 0 and "nothing to check" in output
    # No ref lines (run by hand, or `make verify`): HEAD plus uncommitted work.
    assert lc_verify("--pre-push", "--list", refs="") == (0, "py\ndocs\n")


def _define(repo: Path, text: str) -> None:
    (repo / ".lemoncrow" / "environment.toml").write_text(text)
    git(repo, "commit", "-q", "-am", "definition")


def test_a_timeout_stops_the_check_and_every_process_it_started(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "survivor"
    _define(
        repo,
        'version = 1\n[[checks]]\nid = "slow"\npaths = ["src"]\ntimeout = 1\n'
        f'run = "(sleep 3; touch {marker}) & sleep 30"\n'
        '[[checks]]\nid = "quick"\npaths = ["src"]\ntimeout = 30\nrun = "true"\n',
    )
    (repo / "src" / "a.py").write_text("a = 2\n")
    code, output = lc_verify()
    assert code == 1 and "[slow] stopped after its timeout of 1 s" in output and "FAILED: slow\n" in output
    import time

    time.sleep(3.5)
    assert not marker.exists()  # the background child went with its check


def test_attr_pathspecs_select_by_the_working_tree_attributes(repo: Path) -> None:
    (repo / ".gitattributes").write_text("docs/** generated=yes\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-q", "-m", "attributes")
    _define(
        repo,
        'version = 1\n[[checks]]\nid = "generated"\npaths = [":(attr:generated=yes)"]\nrun = "true"\n'
        '[[checks]]\nid = "hand-written"\npaths = [":(attr:!generated)", ":!.lemoncrow", ":!.gitattributes"]\n'
        'run = "true"\n',
    )
    git(repo, "push", "-q", "origin", "feature:main")
    git(repo, "fetch", "-q", "origin")
    (repo / "docs" / "guide.md").write_text("guide, revised\n")
    assert lc_verify("--list") == (0, "generated\n")
    (repo / "src" / "a.py").write_text("a = 2\n")
    assert lc_verify("--list") == (0, "generated\nhand-written\n")


def test_verify_records_the_fingerprint_each_check_ran_under(repo: Path) -> None:
    from lemoncrow_client.environment import VERIFY_RECORD_DIR, read_verify_record

    _define(
        repo,
        'version = 1\n[toolchain]\nimage = "python:3.12"\n[[dependencies]]\nlockfile = "uv.lock"\n'
        'install = "uv sync"\n[[checks]]\nid = "py"\npaths = ["src"]\nkind = "test"\nrun = "true"\n'
        '[[checks]]\nid = "bad"\npaths = ["src"]\nrun = "exit 4"\n[[checks]]\nid = "docs"\npaths = ["docs"]\n'
        'run = "true"\n',
    )
    (repo / "uv.lock").write_text("lock 1\n")
    git(repo, "add", "uv.lock")
    git(repo, "commit", "-q", "-m", "lock")
    (repo / "src" / "a.py").write_text("a = 2\n")
    git(repo, "commit", "-q", "-am", "change")
    head = git(repo, "rev-parse", "HEAD")
    assert lc_verify()[0] == 1
    record = read_verify_record(repo / ".git", head)
    assert record is not None and record["dirty"] is False
    assert sorted(record["checks"]) == ["bad", "py"]  # only what ran
    assert record["checks"]["py"]["status"] == "passed" and record["checks"]["bad"]["status"] == "failed"
    py = record["checks"]["py"]["fingerprint"]
    assert py.toolchain == "python:3.12" and py.image_digest == "" and not py.reproducible
    assert (
        py.lockfile_digest.startswith("sha256:")
        and py.check_digest != record["checks"]["bad"]["fingerprint"].check_digest
    )

    # The lockfile is part of it: the same check under another lockfile is another environment.
    (repo / "uv.lock").write_text("lock 2\n")
    assert lc_verify()[0] == 1
    again = read_verify_record(repo / ".git", head)
    assert again is not None and again["dirty"] is True
    assert again["checks"]["py"]["fingerprint"].differences(py) == ("lockfile_digest",)
    assert (repo / ".git" / VERIFY_RECORD_DIR / f"{head}.json").is_file()
