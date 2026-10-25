"""``lc review hook``: the git hook that runs the local AI review on what is pushed or committed.

The installer is checked on real repositories (a hook that exists is never overwritten without ``--force``; installing
twice writes nothing the second time), the hook script is run by a real ``sh`` against a stub ``lc``, the range of a
push is planned from real git history, and ``hook run`` is driven the way git drives it, with ref updates on standard
input, on the fake model of the local review tests.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.pro.capabilities.review.local_cli import hook as hooks
from tests.gateway.pr_bot_support import AUTH_BUGGY, BASE, CACHE_BUGGY, RULE, git
from tests.gateway.test_cli_review_local import Lab, make_lab

FOREIGN = "#!/bin/sh\necho 'my own pre-push check'\n"


@pytest.fixture
def plain(tmp_path: Path) -> Path:
    """A repository with one commit and no remote."""

    repo = tmp_path / "plain"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for key, value in (("user.name", "A"), ("user.email", "a@example.invalid"), ("commit.gpgsign", "false")):
        git(repo, "config", key, value)
    (repo / "README.md").write_text("hi\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "first")
    return repo


def install(repo: Path, *args: str, ok: bool = True) -> Any:
    result = CliRunner().invoke(cli, ["review", "hook", "install", "--repo-root", str(repo), *args])
    assert (result.exit_code == 0) == ok, result.output
    return result


# --------------------------------------------------------------------------- #
# the installer
# --------------------------------------------------------------------------- #


def test_print_shows_the_script_and_writes_nothing(plain: Path) -> None:
    result = install(plain, "--print")
    assert result.output == hooks.hook_script("pre-push")
    assert result.output.startswith("#!/bin/sh\n# lemoncrow-review-hook v2 (pre-push)\n")
    assert 'LEMONCROW_HOOK_STOP=87 "$LC" review hook run --pre-push --fail-on p0 --depth auto "$@"' in result.output
    assert not (plain / ".git" / "hooks" / "pre-push").exists()
    subprocess.run(["sh", "-n"], input=result.output, text=True, check=True)  # it parses


def test_install_writes_an_executable_hook_and_a_second_install_changes_nothing(plain: Path) -> None:
    first = install(plain)
    path = plain / ".git" / "hooks" / "pre-push"
    assert "Installed the pre-push review hook" in first.output and str(path) in first.output
    assert path.read_text() == hooks.hook_script("pre-push")
    assert path.stat().st_mode & stat.S_IXUSR
    before = path.stat().st_mtime_ns

    second = install(plain)
    assert "Already installed" in second.output and "nothing changed" in second.output
    assert path.stat().st_mtime_ns == before and path.read_text() == hooks.hook_script("pre-push")


def test_a_hook_that_is_not_ours_is_never_overwritten_without_force(plain: Path) -> None:
    path = plain / ".git" / "hooks" / "pre-push"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FOREIGN)
    path.chmod(0o755)
    refused = install(plain, ok=False)
    assert "already exists and is not a LemonCrow hook" in refused.output
    assert "--print" in refused.output and "--force" in refused.output
    assert path.read_text() == FOREIGN
    forced = install(plain, "--force")
    assert "Replaced the pre-push review hook" in forced.output
    assert path.read_text() == hooks.hook_script("pre-push")


def test_our_own_hook_with_other_options_is_not_replaced_without_force_either(plain: Path) -> None:
    install(plain)
    path = plain / ".git" / "hooks" / "pre-push"
    refused = install(plain, "--fail-on", "p1", "--strict", ok=False)
    assert "is a LemonCrow review hook with other options" in refused.output
    assert path.read_text() == hooks.hook_script("pre-push")
    install(plain, "--fail-on", "p1", "--strict", "--force")
    text = path.read_text()
    assert (
        "--fail-on p1" in text and "--strict" in text and "--strict also stops it when the review is incomplete" in text
    )


def test_pre_commit_is_its_own_hook_and_leaves_pre_push_alone(plain: Path) -> None:
    install(plain, "--pre-commit", "--depth", "thorough")
    commit = plain / ".git" / "hooks" / "pre-commit"
    assert '"$LC" review hook run --pre-commit --fail-on p0 --depth thorough "$@"' in commit.read_text()
    assert not (plain / ".git" / "hooks" / "pre-push").exists()
    install(plain, "--pre-push")
    assert (plain / ".git" / "hooks" / "pre-push").exists()


def test_the_hook_goes_where_git_will_run_it(plain: Path, tmp_path: Path) -> None:
    git(plain, "config", "core.hooksPath", str(tmp_path / "shared-hooks"))
    install(plain)
    assert (tmp_path / "shared-hooks" / "pre-push").read_text() == hooks.hook_script("pre-push")
    assert not (plain / ".git" / "hooks" / "pre-push").exists()


def test_installing_outside_a_repository_is_an_error(tmp_path: Path) -> None:
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    result = install(nowhere, ok=False)
    assert "not inside a Git repository" in result.output


# --------------------------------------------------------------------------- #
# the script itself, run by sh
# --------------------------------------------------------------------------- #


@pytest.fixture
def stub_lc(tmp_path: Path) -> Path:
    """A fake ``lc`` that records how it was called and exits with ``$STUB_EXIT``."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    lc = bin_dir / "lc"
    lc.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" > "$STUB_LOG"\ncat >> "$STUB_LOG"\nexit "${STUB_EXIT:-0}"\n', encoding="utf-8"
    )
    lc.chmod(0o755)
    return bin_dir


def run_hook(
    script: str, tmp_path: Path, bin_dir: Path, *, stdin: str = "", **env: str
) -> subprocess.CompletedProcess[str]:
    hook = tmp_path / "hook.sh"
    hook.write_text(script)
    full = {"PATH": f"{bin_dir}:/usr/bin:/bin", "STUB_LOG": str(tmp_path / "stub.log"), **env}
    return subprocess.run(
        ["/bin/sh", str(hook), "origin", "git@example.invalid:o/r.git"],
        input=stdin,
        text=True,
        capture_output=True,
        env=full,
        check=False,
    )


def test_the_script_hands_git_s_arguments_and_the_ref_list_to_lc_and_stops_only_on_the_stop_code(
    tmp_path: Path, stub_lc: Path
) -> None:
    refs = "refs/heads/f aaaa refs/heads/f bbbb\n"
    stopped = run_hook(hooks.hook_script("pre-push"), tmp_path, stub_lc, stdin=refs, STUB_EXIT=str(hooks.STOP_CODE))
    assert stopped.returncode == 1 and "could not run" not in stopped.stderr
    # Anything else lc exits with is LemonCrow's trouble, not a finding: the push goes through, with one line saying so.
    for code in (0, 1, 2, 3, 126, 127, 139):
        done = run_hook(hooks.hook_script("pre-push"), tmp_path, stub_lc, stdin=refs, STUB_EXIT=str(code))
        assert done.returncode == 0, code
        assert ("could not run" in done.stderr) == (code != 0), done.stderr
    log = (tmp_path / "stub.log").read_text()
    assert (
        log.splitlines()[0] == "review hook run --pre-push --fail-on p0 --depth auto origin git@example.invalid:o/r.git"
    )
    assert "refs/heads/f aaaa refs/heads/f bbbb" in log  # stdin reached lc
    assert done.stderr.count("\n") == 1  # one line, not a traceback


def test_the_script_calls_the_lc_that_wrote_it_even_when_the_path_has_another(tmp_path: Path, stub_lc: Path) -> None:
    other = tmp_path / "other-bin"
    other.mkdir()
    own = other / "lc"
    own.write_text('#!/bin/sh\necho "own lc" > "$STUB_LOG"\nexit "${STUB_EXIT:-0}"\n')
    own.chmod(0o755)
    script = hooks.hook_script("pre-push", lc_path=str(own))
    done = run_hook(script, tmp_path, stub_lc, STUB_EXIT=str(hooks.STOP_CODE))
    assert done.returncode == 1 and (tmp_path / "stub.log").read_text() == "own lc\n"  # not the PATH one
    # A path that no longer exists falls back to the PATH, and a path with a space in it works.
    spaced = tmp_path / "a b"
    spaced.mkdir()
    (spaced / "lc").write_text("#!/bin/sh\nexit 87\n")
    (spaced / "lc").chmod(0o755)
    assert run_hook(hooks.hook_script("pre-push", lc_path=str(spaced / "lc")), tmp_path, stub_lc).returncode == 1
    gone = run_hook(hooks.hook_script("pre-push", lc_path=str(tmp_path / "gone" / "lc")), tmp_path, stub_lc)
    assert gone.returncode == 0 and (tmp_path / "stub.log").read_text().startswith("review hook run")


def test_a_crashing_lc_never_stops_a_push(tmp_path: Path) -> None:
    broken = tmp_path / "bin"
    broken.mkdir()
    lc = broken / "lc"
    lc.write_text("#!/bin/sh\necho 'Traceback (most recent call last):' >&2\necho 'ModuleNotFoundError' >&2\nexit 1\n")
    lc.chmod(0o755)
    done = run_hook(hooks.hook_script("pre-push", lc_path=str(lc)), tmp_path, broken)
    assert done.returncode == 0 and "lc exited 1); not blocking this push" in done.stderr


def test_the_script_skips_on_request_and_never_blocks_for_want_of_lc(tmp_path: Path, stub_lc: Path) -> None:
    script = hooks.hook_script("pre-push")
    skipped = run_hook(script, tmp_path, stub_lc, STUB_EXIT="1", LEMONCROW_REVIEW_SKIP="1")
    assert skipped.returncode == 0 and not (tmp_path / "stub.log").exists()
    empty = tmp_path / "empty"
    empty.mkdir()
    missing = run_hook(script, tmp_path, empty)
    assert missing.returncode == 0 and "lc was not found; not reviewing this push" in missing.stderr


def test_the_pre_commit_script_calls_the_staged_review(tmp_path: Path, stub_lc: Path) -> None:
    done = run_hook(hooks.hook_script("pre-commit", fail_on="p1", strict=True), tmp_path, stub_lc)
    assert done.returncode == 0
    assert (
        (tmp_path / "stub.log")
        .read_text()
        .splitlines()[0]
        .startswith("review hook run --pre-commit --fail-on p1 --depth auto --strict")
    )


# --------------------------------------------------------------------------- #
# what a push publishes
# --------------------------------------------------------------------------- #


@pytest.fixture
def clone(tmp_path: Path) -> tuple[Path, Path]:
    """``(clone, origin)``: main is pushed; ``feature`` has two local commits that are not."""

    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin))
    repo = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(repo))
    for key, value in (("user.name", "A"), ("user.email", "a@example.invalid"), ("commit.gpgsign", "false")):
        git(repo, "config", key, value)
    (repo / "a.txt").write_text("1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "push", "-q", "origin", "main")
    git(repo, "remote", "set-head", "origin", "main")
    git(repo, "checkout", "-qb", "feature")
    for name in ("one", "two"):
        (repo / f"{name}.txt").write_text(name + "\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", name)
    return repo, origin


def update(repo: Path, remote_sha: str = hooks.ZERO, *, ref: str = "refs/heads/feature") -> hooks.RefUpdate:
    return hooks.RefUpdate(ref, git(repo, "rev-parse", ref), ref, remote_sha)


def test_a_new_branch_is_reviewed_from_where_it_leaves_the_default_branch(clone: tuple[Path, Path]) -> None:
    repo, _origin = clone
    plan = hooks.plan_push(repo, update(repo))
    assert (plan.base, plan.head) == (git(repo, "rev-parse", "main"), git(repo, "rev-parse", "feature"))
    assert plan.note == "" and plan.label == "refs/heads/feature"


def test_an_existing_branch_is_reviewed_from_what_the_remote_has(clone: tuple[Path, Path]) -> None:
    repo, _origin = clone
    first = git(repo, "rev-parse", "feature~1")
    plan = hooks.plan_push(repo, update(repo, first))
    assert (plan.base, plan.head) == (first, git(repo, "rev-parse", "feature"))


def test_a_rewritten_branch_is_reviewed_from_the_merge_base(clone: tuple[Path, Path]) -> None:
    repo, _origin = clone
    old_tip = git(repo, "rev-parse", "feature")
    git(repo, "reset", "-q", "--hard", "feature~1")
    (repo / "three.txt").write_text("three\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "rewritten")
    plan = hooks.plan_push(repo, update(repo, old_tip))  # a force push: the remote's tip is not an ancestor
    assert plan.base == git(repo, "rev-parse", "feature~1") and plan.head == git(repo, "rev-parse", "feature")


@pytest.mark.parametrize("case", ["delete", "tag", "up-to-date", "unknown-remote-commit"])
def test_pushes_with_nothing_to_review_say_so(clone: tuple[Path, Path], case: str) -> None:
    repo, _origin = clone
    tip = git(repo, "rev-parse", "feature")
    if case == "delete":
        plan = hooks.plan_push(repo, hooks.RefUpdate("(delete)", hooks.ZERO, "refs/heads/feature", tip))
        assert not plan.base and "deleting a branch" in plan.note
    elif case == "tag":
        git(repo, "tag", "v1")
        plan = hooks.plan_push(repo, hooks.RefUpdate("refs/tags/v1", tip, "refs/tags/v1", hooks.ZERO))
        assert not plan.base and "a tag" in plan.note
    elif case == "up-to-date":
        plan = hooks.plan_push(repo, update(repo, tip))
        assert not plan.base and "already has these commits" in plan.note
    else:
        # The remote holds a commit this clone never fetched: it is reviewed like a new branch.
        plan = hooks.plan_push(repo, update(repo, "f" * 40))
        assert plan.base == git(repo, "rev-parse", "main")


def test_without_a_default_branch_only_the_last_commit_is_reviewed_and_it_says_so(plain: Path) -> None:
    (plain / "b.txt").write_text("b\n")
    git(plain, "add", "-A")
    git(plain, "commit", "-qm", "second")
    plan = hooks.plan_push(plain, update(plain, ref="refs/heads/main"))
    assert plan.base == git(plain, "rev-parse", "HEAD~1") and "reviewing only the last commit" in plan.note
    first = hooks.plan_push(
        plain, hooks.RefUpdate("refs/heads/main", git(plain, "rev-parse", "HEAD~1"), "refs/heads/main", hooks.ZERO)
    )
    assert not first.base and "first commit of a repository" in first.note


def test_the_ref_updates_git_sends_are_parsed_and_bounded() -> None:
    rows = hooks.parse_updates("refs/heads/a " + "1" * 40 + " refs/heads/a " + "0" * 40 + "\nnot an update\n")
    assert [(row.local_ref, row.remote_sha) for row in rows] == [("refs/heads/a", "0" * 40)]
    many = "\n".join(f"refs/heads/b{n} {'1' * 40} refs/heads/b{n} {'0' * 40}" for n in range(100))
    assert len(hooks.parse_updates(many)) == 32


# --------------------------------------------------------------------------- #
# `hook run`, as git runs it
# --------------------------------------------------------------------------- #


@pytest.fixture
def pushed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Lab, Path]:
    """A repository with a critical rule on ``src/auth.py``, an origin that has ``main``, and the fake model."""

    lab = make_lab(tmp_path, monkeypatch, {**BASE, ".lemoncrow/review.yaml": RULE})
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin))
    git(lab.repo, "remote", "add", "origin", str(origin))
    git(lab.repo, "push", "-q", "origin", "main")
    git(lab.repo, "fetch", "-q", "origin")
    git(lab.repo, "remote", "set-head", "origin", "main")
    git(lab.repo, "checkout", "-qb", "feature")
    return lab, origin


def hook_run(lab: Lab, stdin: str, *args: str, **env: str) -> Any:
    return CliRunner().invoke(
        cli,
        ["--root", str(lab.store), "review", "hook", "run", "--repo-root", str(lab.repo), *args, "origin", "url"],
        input=stdin,
        env=env or None,
    )


def push_line(lab: Lab, ref: str = "refs/heads/feature", remote_sha: str = hooks.ZERO) -> str:
    return f"{ref} {git(lab.repo, 'rev-parse', ref)} {ref} {remote_sha}\n"


def test_a_push_with_an_open_p0_is_stopped_and_the_review_is_shown(pushed: tuple[Lab, Path]) -> None:
    lab, _origin = pushed
    lab.write({"src/auth.py": AUTH_BUGGY})
    lab.commit("buggy auth")
    result = hook_run(lab, push_line(lab), "--pre-push")
    assert result.exit_code == 1
    assert "LemonCrow review of refs/heads/feature (" in result.stderr
    assert "P0 · must fix  src/auth.py" in result.stderr
    assert "stopped by an open finding at or above p0" in result.stderr and "LEMONCROW_REVIEW_SKIP=1" in result.stderr


def test_a_clean_push_goes_through_and_a_repeat_makes_no_model_call(pushed: tuple[Lab, Path]) -> None:
    lab, _origin = pushed
    lab.write({"src/plain.py": "VALUE = 2\n"})
    lab.commit("plain change")
    assert hook_run(lab, push_line(lab), "--pre-push").exit_code == 0
    assert lab.model.calls == [] or lab.model.paths() == ["src/plain.py"]
    lab.forget_calls()
    again = hook_run(lab, push_line(lab), "--pre-push")
    assert again.exit_code == 0 and lab.model.calls == []  # the same range: carried, no model


def test_the_next_push_of_a_branch_reviews_only_what_is_new(pushed: tuple[Lab, Path]) -> None:
    lab, _origin = pushed
    lab.write({"src/auth.py": AUTH_BUGGY})
    lab.commit("buggy auth")
    assert hook_run(lab, push_line(lab), "--pre-push").exit_code == 1
    lab.forget_calls()
    lab.write({"src/cache.py": CACHE_BUGGY})
    lab.commit("buggy cache")
    # Same base (the remote never took the first push), a longer branch: auth.py is carried and still stops it.
    assert hook_run(lab, push_line(lab), "--pre-push").exit_code == 1
    assert lab.model.paths() == ["src/cache.py"]


def test_fail_on_none_lets_a_p0_through(pushed: tuple[Lab, Path]) -> None:
    lab, _origin = pushed
    lab.write({"src/auth.py": AUTH_BUGGY})
    lab.commit("buggy auth")
    assert hook_run(lab, push_line(lab), "--pre-push", "--fail-on", "none").exit_code == 0


def test_the_hook_fails_open_without_a_model_and_blocks_only_under_strict(
    pushed: tuple[Lab, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    lab, _origin = pushed
    lab.write({"src/auth.py": AUTH_BUGGY})
    lab.commit("buggy auth")
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.delenv("LEMONCROW_LLM_BACKEND")
    soft = hook_run(lab, push_line(lab), "--pre-push")
    assert soft.exit_code == 0 and "no model route is configured" in soft.stderr
    hard = hook_run(lab, push_line(lab), "--pre-push", "--strict")
    assert hard.exit_code == 2 and "incomplete and --strict is set" in hard.stderr
    assert lab.model.calls == []


def test_an_unexpected_error_never_reads_as_a_finding(
    pushed: tuple[Lab, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.pr_bot import local

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(local, "run_local_review", broken)
    lab, _origin = pushed
    lab.write({"src/auth.py": AUTH_BUGGY})
    lab.commit("buggy auth")
    soft = hook_run(lab, push_line(lab), "--pre-push")
    assert soft.exit_code == 0 and "failed unexpectedly (RuntimeError: boom); not blocking" in soft.stderr
    assert hook_run(lab, push_line(lab), "--pre-push", "--strict").exit_code == 2


def test_skip_deletes_and_tags_cost_nothing(pushed: tuple[Lab, Path]) -> None:
    lab, _origin = pushed
    lab.write({"src/auth.py": AUTH_BUGGY})
    lab.commit("buggy auth")
    skipped = hook_run(lab, push_line(lab), "--pre-push", LEMONCROW_REVIEW_SKIP="1")
    assert skipped.exit_code == 0
    tip = git(lab.repo, "rev-parse", "feature")
    git(lab.repo, "tag", "v1")
    lines = f"(delete) {hooks.ZERO} refs/heads/gone {tip}\nrefs/tags/v1 {tip} refs/tags/v1 {hooks.ZERO}\n"
    quiet = hook_run(lab, lines, "--pre-push")
    assert quiet.exit_code == 0 and "deleting a branch" in quiet.stderr and "a tag" in quiet.stderr
    assert hook_run(lab, "", "--pre-push").exit_code == 0
    assert lab.model.calls == []


def test_several_ref_updates_stop_the_push_if_any_has_a_p0(pushed: tuple[Lab, Path]) -> None:
    lab, _origin = pushed
    lab.write({"src/auth.py": AUTH_BUGGY})
    lab.commit("buggy auth")
    git(lab.repo, "checkout", "-q", "main")
    git(lab.repo, "checkout", "-qb", "docs")
    lab.write({"README.md": "docs\n"})
    lab.commit("docs")
    lines = push_line(lab, "refs/heads/docs") + push_line(lab, "refs/heads/feature")
    result = hook_run(lab, lines, "--pre-push")
    assert result.exit_code == 1
    assert "LemonCrow review of refs/heads/docs" in result.stderr and "refs/heads/feature" in result.stderr


def test_pre_commit_reviews_what_is_staged(pushed: tuple[Lab, Path]) -> None:
    lab, _origin = pushed
    lab.write({"src/auth.py": AUTH_BUGGY})
    git(lab.repo, "add", "src/auth.py")
    result = hook_run(lab, "", "--pre-commit")
    assert result.exit_code == 1 and "LemonCrow review of the staged changes" in result.stderr
    assert "staged changes against HEAD" in result.stderr
    nothing = pushed[0]
    git(nothing.repo, "reset", "-q")
    assert hook_run(nothing, "", "--pre-commit").exit_code == 0


def test_a_terminal_on_standard_input_is_a_usage_error_not_a_hang(
    pushed: tuple[Lab, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    lab, _origin = pushed
    monkeypatch.setattr(hooks, "_stdin_is_terminal", lambda: True)
    result = hook_run(lab, "", "--pre-push")
    assert result.exit_code == 3 and "reads git's ref updates on standard input" in result.output
    bad = hook_run(lab, "", "--pre-push", "--fail-on", "bogus")
    assert bad.exit_code == 3 and "is not one of" in bad.output  # a usage error is 3, never 2 (incomplete)


def test_the_hook_inherits_nothing_it_should_not(pushed: tuple[Lab, Path]) -> None:
    lab, _origin = pushed
    before = dict(os.environ)
    lab.write({"src/plain.py": "VALUE = 3\n"})
    lab.commit("plain")
    hook_run(lab, push_line(lab), "--pre-push")
    assert dict(os.environ) == before
