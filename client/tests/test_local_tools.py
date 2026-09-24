"""The tools that run on the developer's machine.

These are the tools that must keep working when the server does not, so they
are tested without one. Each test asserts the behaviour *and* one of the
boundaries the package claims: no detached process, no download, no write
outside the worktree, no invented answer where the index is the only honest
source.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest
from _audit_source import PACKAGE_ROOT, code_only, iter_modules
from lemoncrow_client.config import load_config
from lemoncrow_client.errors import ClientError, ErrorCode
from lemoncrow_client.localtools import LocalContext, executor_for
from lemoncrow_client.localtools.shell import cancel_active_bash_commands


@pytest.fixture
def context(worktree: Path, state_dir: Path) -> LocalContext:
    config = load_config({"LEMONCROW_HOME": str(state_dir), "HOME": str(state_dir.parent)}, cwd=worktree)
    return LocalContext(config=config, repo_root=worktree, sync=None, environment=dict(os.environ))


def run(context: LocalContext, executor: str, arguments: dict[str, object]):
    return executor_for(executor)(context, arguments)


def text(result) -> str:
    return "\n".join(str(block.get("text", "")) for block in result.content)


# --------------------------------------------------------------------------- #
# bash                                                                        #
# --------------------------------------------------------------------------- #


def test_bash_runs_a_command_in_the_worktree_and_reports_the_exit_code(context: LocalContext) -> None:
    result = run(context, "bash", {"command": "pwd && printf hello"})
    assert str(context.repo_root) in text(result)
    assert "hello" in text(result)
    assert "[exit 0]" in text(result)
    assert not result.is_error


def test_bash_reports_a_failure_as_an_error_result_not_an_exception(context: LocalContext) -> None:
    result = run(context, "bash", {"command": "exit 3"})
    assert result.is_error
    assert "[exit 3]" in text(result)


def test_bash_runs_an_array_sequentially_even_when_one_fails(context: LocalContext) -> None:
    result = run(context, "bash", {"command": ["exit 1", "printf second"]})
    body = text(result)
    assert "## lc:cmd 1/2" in body and "## lc:cmd 2/2" in body
    assert "second" in body


_BIG = 300_000


def test_bash_trims_large_output_and_spills_the_full_text(context: LocalContext) -> None:
    """Trimmed output must be recoverable, not just gone."""
    result = run(context, "bash", {"command": f"head -c {_BIG} /dev/zero | tr '\\0' 'x'"})
    body = text(result)
    assert len(body) < 25_000
    assert f"[lc: shrunk {_BIG}" in body
    spill_path = Path(body.rsplit("full: ", 1)[1].split("]", 1)[0])
    assert spill_path.is_file()
    assert spill_path.is_relative_to(context.config.state_dir)
    assert spill_path.read_text(encoding="utf-8") == "x" * _BIG


def test_bash_trim_still_marks_the_cut_when_the_spill_fails(
    context: LocalContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow_client.localtools import shell

    monkeypatch.setattr(shell, "spill_text", lambda *_args, **_kwargs: None)
    result = run(context, "bash", {"command": f"head -c {_BIG} /dev/zero | tr '\\0' 'x'"})
    body = text(result)
    assert "chars omitted" in body
    assert body.endswith("[output compacted]\n[exit 0]")
    assert "full: " not in body


def test_bash_spill_retention_evicts_the_oldest_files_past_the_cap(
    context: LocalContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_MAX_FILES", "2")
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_TTL_SECONDS", "0")
    for size in (_BIG, _BIG + 1, _BIG + 2):
        run(context, "bash", {"command": f"head -c {size} /dev/zero | tr '\\0' 'x'"})
    spill_dir = context.config.state_dir / "spill"
    assert len(list(spill_dir.glob("bash-*.txt"))) == 2


def test_bash_keeps_test_failures_and_drops_the_passing_noise(context: LocalContext) -> None:
    passing = "".join(f"tests/test_a.py::test_ok_{i} PASSED\n" for i in range(60))
    report = (
        "collected 61 items\n"
        + passing
        + "=================================== FAILURES ===================================\n"
        + "___ test_broken ___\n"
        + "    assert 1 == 2\n"
        + "=========================== short test summary info ============================\n"
        + "FAILED tests/test_a.py::test_broken - assert 1 == 2\n"
        + "1 failed, 60 passed in 0.12s\n"
    )
    fake = context.repo_root / "pytest"
    fake.write_text("#!/bin/sh\ncat <<'EOF'\n" + report + "EOF\nexit 1\n", encoding="utf-8")
    fake.chmod(0o755)

    result = run(context, "bash", {"command": "./pytest"})
    body = text(result)

    assert result.is_error
    assert "[ran: ./pytest -q --tb=short]" in body
    assert "FAILED tests/test_a.py::test_broken" in body
    assert "1 failed, 60 passed" in body
    assert "test_ok_17 PASSED" not in body
    assert "[exit 1]" in body


def test_bash_keeps_the_lines_around_an_error_in_a_long_log(context: LocalContext) -> None:
    result = run(context, "bash", {"command": "seq 1 20000 | sed 's/^9000$/ERROR: disk full/'"})
    body = text(result)
    assert "ERROR: disk full" in body
    assert "lines omitted" in body
    assert len(body) < 10_000


def test_bash_redacts_secrets_from_output(context: LocalContext) -> None:
    result = run(context, "bash", {"command": "printf 'ghp_abcdefghijklmnopqrstuvwx\\n'"})
    body = text(result)
    assert "<redacted-github-token>" in body
    assert "ghp_abcdefghijklmnopqrstuvwx" not in body


def test_bash_replaces_an_identical_rerun_with_a_marker(context: LocalContext) -> None:
    first = text(run(context, "bash", {"command": "seq 1 400"}))
    second = text(run(context, "bash", {"command": "seq 1 400"}))
    assert "400" in first
    assert second.startswith("unchanged: output byte-identical to this command's previous run")
    assert "[exit 0]" in second


def test_bash_kills_the_whole_process_group_on_timeout(context: LocalContext) -> None:
    """A timeout must not leave the child -- or its children -- behind."""
    marker = context.repo_root / "survivor.txt"
    result = run(
        context,
        "bash",
        {"command": f"(sleep 30 && touch {marker}) & sleep 30", "timeout": 1},
    )
    assert "timed out" in text(result)
    assert not marker.exists()


def test_bash_leaves_no_thread_or_child_behind(context: LocalContext) -> None:
    before = threading.active_count()
    run(context, "bash", {"command": "printf x"})
    assert threading.active_count() == before


def test_server_drain_can_cancel_an_active_foreground_bash(context: LocalContext) -> None:
    started = context.repo_root / "started.txt"
    survivor = context.repo_root / "survivor-after-drain.txt"
    outcome: list[object] = []

    thread = threading.Thread(
        target=lambda: outcome.append(
            run(
                context,
                "bash",
                {
                    "command": f"touch {started}; sleep 30; touch {survivor}",
                    "timeout": 60,
                },
            )
        )
    )
    thread.start()
    for _ in range(100):
        if started.exists():
            break
        threading.Event().wait(0.02)
    assert started.exists(), "the shell command never started"

    assert cancel_active_bash_commands() >= 1
    thread.join(timeout=5)
    assert not thread.is_alive(), "drain cancellation must release the shell worker"
    assert not survivor.exists(), "the cancelled process group must not outlive the drain"
    assert outcome and outcome[0].is_error


@pytest.mark.parametrize("argument", [{"bg": True}, {"interactive": True}, {"id": "abc"}])
def test_bash_refuses_every_mode_that_would_outlive_the_call(
    context: LocalContext, argument: dict[str, object]
) -> None:
    with pytest.raises(ClientError) as caught:
        run(context, "bash", {"command": "sleep 1", **argument})
    assert caught.value.code is ErrorCode.LOCAL_TOOL_UNAVAILABLE


def test_bash_refuses_a_cwd_outside_the_worktree(context: LocalContext, tmp_path: Path) -> None:
    with pytest.raises(ClientError) as caught:
        run(context, "bash", {"command": "pwd", "cwd": str(tmp_path)})
    assert caught.value.code is ErrorCode.PAYLOAD_INVALID


def test_the_package_never_starts_a_detached_session() -> None:
    """``start_new_session`` is the pattern the security review objected to.

    Read as code, so the docstring in ``shell.py`` that explains why it is
    absent does not count as a use of it.
    """
    offenders = [
        path.relative_to(PACKAGE_ROOT).as_posix() for path in iter_modules() if "start_new_session" in code_only(path)
    ]
    assert offenders == []


# --------------------------------------------------------------------------- #
# edit                                                                        #
# --------------------------------------------------------------------------- #


def test_edit_replaces_a_unique_string_and_reports_the_path(context: LocalContext) -> None:
    result = run(context, "edit", {"edits": [{"path": "pkg/alpha.py", "old": "'alpha'", "new": "'A'"}]})
    assert result.changed_paths == ("pkg/alpha.py",)
    assert (context.repo_root / "pkg/alpha.py").read_text() == "def alpha():\n    return 'A'\n"


def test_edit_collapses_same_file_replacements_into_one_summary(context: LocalContext) -> None:
    result = run(
        context,
        "edit",
        {
            "edits": [
                {"path": "pkg/alpha.py", "old": "'alpha'", "new": "'A'"},
                {"path": "pkg/alpha.py", "old": "'A'", "new": "'B'"},
            ]
        },
    )

    assert text(result) == "edited pkg/alpha.py (2 replacements)"
    assert result.changed_paths == ("pkg/alpha.py",)
    assert result.structured == {"changed_paths": ["pkg/alpha.py"], "edits": 2}


def test_edit_refuses_an_ambiguous_old_rather_than_guessing(context: LocalContext) -> None:
    target = context.repo_root / "pkg" / "dup.py"
    target.write_text("x = 1\nx = 1\n", encoding="utf-8")
    with pytest.raises(ClientError) as caught:
        run(context, "edit", {"edits": [{"path": "pkg/dup.py", "old": "x = 1", "new": "y = 2"}]})
    assert "not unique" in caught.value.message
    assert target.read_text() == "x = 1\nx = 1\n", "a refused edit must change nothing"


def test_edit_batched_ranges_use_the_line_numbers_the_caller_read(context: LocalContext) -> None:
    target = context.repo_root / "pkg" / "lines.txt"
    target.write_text("a\nb\nc\nd\ne\n", encoding="utf-8")

    result = run(
        context,
        "edit",
        {
            "edits": [
                {"path": "pkg/lines.txt:L1-L1", "new": "A1\nA2\nA3\n"},
                {"path": "pkg/lines.txt:L4-L4", "new": "D\n"},
            ]
        },
    )

    assert target.read_text(encoding="utf-8") == "A1\nA2\nA3\nb\nc\nD\ne\n"
    assert result.changed_paths == ("pkg/lines.txt",)


def test_edit_a_failing_hunk_leaves_the_whole_batch_unwritten(context: LocalContext) -> None:
    alpha = context.repo_root / "pkg" / "alpha.py"
    before = alpha.read_text(encoding="utf-8")

    with pytest.raises(ClientError):
        run(
            context,
            "edit",
            {
                "edits": [
                    {"path": "pkg/alpha.py", "old": "'alpha'", "new": "'A'"},
                    {"path": "pkg/beta.py", "old": "not in the file", "new": "x"},
                ]
            },
        )

    assert alpha.read_text(encoding="utf-8") == before


def test_edit_a_miss_ships_the_disk_text_to_retry_with(context: LocalContext) -> None:
    with pytest.raises(ClientError) as caught:
        run(
            context,
            "edit",
            {"edits": [{"path": "pkg/alpha.py", "old": "def alpha():\n    return 'b'\n", "new": "x = 1\n"}]},
        )

    assert caught.value.code is ErrorCode.PAYLOAD_INVALID
    assert caught.value.details["retry_with"]["old_string"] == "def alpha():\n    return 'alpha'\n"


def test_edit_refuses_python_that_no_longer_parses(context: LocalContext) -> None:
    alpha = context.repo_root / "pkg" / "alpha.py"
    before = alpha.read_text(encoding="utf-8")

    with pytest.raises(ClientError) as caught:
        run(context, "edit", {"edits": [{"path": "pkg/alpha.py", "old": "return 'alpha'", "new": "return ("}]})

    assert "parse error" in caught.value.message
    assert alpha.read_text(encoding="utf-8") == before


def test_edit_rolls_back_a_test_only_assertion_removal(context: LocalContext) -> None:
    test_file = context.repo_root / "tests" / "test_alpha.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_a():\n    assert 1\n    assert 2\n", encoding="utf-8")

    with pytest.raises(ClientError) as caught:
        run(
            context,
            "edit",
            {
                "edits": [
                    {"path": "tests/test_alpha.py", "old": "    assert 1\n    assert 2\n", "new": "    assert 2\n"}
                ]
            },
        )

    assert caught.value.details["test_weakening"][0]["path"] == "tests/test_alpha.py"
    assert test_file.read_text(encoding="utf-8") == "def test_a():\n    assert 1\n    assert 2\n"


def test_edit_reports_an_inexact_match_in_the_summary(context: LocalContext) -> None:
    target = context.repo_root / "pkg" / "quote.txt"
    target.write_text("say “hi”\n", encoding="utf-8")

    result = run(context, "edit", {"edits": [{"path": "pkg/quote.txt", "old": 'say "hi"', "new": "say bye"}]})

    assert text(result) == "edited pkg/quote.txt (1 replacement, normalized match)"
    assert target.read_text(encoding="utf-8") == "say bye\n"


@pytest.mark.parametrize(
    "entry",
    [
        {"path": "pkg/alpha.py:minified:L1-L2", "new": "x = 1\n"},
        {"kind": "symbol", "symbol": "pkg.alpha.alpha", "new": "x"},
    ],
)
def test_edit_refuses_what_needs_the_server(context: LocalContext, entry: dict[str, object]) -> None:
    alpha = context.repo_root / "pkg" / "alpha.py"
    before = alpha.read_text(encoding="utf-8")

    with pytest.raises(ClientError) as caught:
        run(context, "edit", {"edits": [entry]})

    assert caught.value.code is ErrorCode.PAYLOAD_INVALID
    assert alpha.read_text(encoding="utf-8") == before


def test_edit_replaces_a_line_range(context: LocalContext) -> None:
    run(context, "edit", {"edits": [{"path": "pkg/alpha.py:L2-L2", "new": "    return 'ranged'"}]})
    assert (context.repo_root / "pkg/alpha.py").read_text() == "def alpha():\n    return 'ranged'\n"


def test_edit_with_range_and_old_replaces_only_old_inside_scope(context: LocalContext) -> None:
    target = context.repo_root / "pkg" / "scoped.py"
    target.write_text("a1\na2\na3\na4\na5\n", encoding="utf-8")

    run(
        context,
        "edit",
        {"edits": [{"path": "pkg/scoped.py:L2-L5", "old": "a2\na3\n", "new": "B2\nB3\n"}]},
    )

    assert target.read_text(encoding="utf-8") == "a1\nB2\nB3\na4\na5\n"


def test_edit_with_range_and_old_does_not_match_outside_scope(context: LocalContext) -> None:
    target = context.repo_root / "pkg" / "scoped.py"
    target.write_text("needle\na2\na3\nneedle\n", encoding="utf-8")

    with pytest.raises(ClientError):
        run(
            context,
            "edit",
            {"edits": [{"path": "pkg/scoped.py:L2-L3", "old": "needle\n", "new": "changed\n"}]},
        )

    assert target.read_text(encoding="utf-8") == "needle\na2\na3\nneedle\n"


def test_edit_creates_a_file_only_with_replace(context: LocalContext) -> None:
    with pytest.raises(ClientError):
        run(context, "edit", {"edits": [{"path": "pkg/new.py", "old": "a", "new": "b"}]})
    run(context, "edit", {"edits": [{"path": "pkg/new.py", "new": "created\n", "replace": True}]})
    assert (context.repo_root / "pkg/new.py").read_text() == "created\n"


def test_edit_leaves_no_temporary_file_behind(context: LocalContext) -> None:
    run(context, "edit", {"edits": [{"path": "pkg/alpha.py", "old": "'alpha'", "new": "'A'"}]})
    litter = [path.name for path in (context.repo_root / "pkg").iterdir() if path.name.startswith(".")]
    assert litter == []


@pytest.mark.parametrize("escape", ["../outside.py", "/etc/passwd", "pkg/../../outside.py"])
def test_edit_refuses_to_write_outside_the_worktree(context: LocalContext, escape: str) -> None:
    with pytest.raises(ClientError) as caught:
        run(context, "edit", {"edits": [{"path": escape, "new": "x", "replace": True}]})
    assert caught.value.code is ErrorCode.PAYLOAD_INVALID


# --------------------------------------------------------------------------- #
# grep                                                                        #
# --------------------------------------------------------------------------- #


def test_grep_finds_matches_with_context_and_never_shells_out(
    context: LocalContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("grep must not spawn a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    result = run(context, "grep", {"regex": "def alpha", "before": 0, "after": 1})
    assert text(result) == "pkg/alpha.py\n@@ 1-2\ndef alpha():\n    return 'alpha'"


def test_grep_accepts_the_main_packages_argument_aliases(context: LocalContext) -> None:
    result = run(context, "grep", {"pattern": "def beta", "output_mode": "paths"})
    assert text(result) == "# grep (1 files)\n\npkg/beta.py"


def test_grep_searches_exactly_what_the_manifest_would_contain(context: LocalContext) -> None:
    """Local search and server search must describe the same repository."""
    result = run(context, "grep", {"regex": ".", "mode": "paths_only"})
    found = set(text(result).splitlines())
    assert "build/artifact.o" not in found, "an ignored path must not be searched"
    assert "noisy.log" not in found
    assert "pkg/alpha.py" in found
    named = run(context, "grep", {"regex": "ignored", "path": "noisy.log"})
    assert "noisy.log" in text(named), "a file named outright is searched, as with rg"


def test_grep_globs_are_relative_to_the_search_path(context: LocalContext) -> None:
    top = run(context, "grep", {"regex": "def", "glob": "*.py", "mode": "paths_only"})
    assert text(top) == "# grep (0 files)"
    nested = run(context, "grep", {"regex": "def", "glob": "pkg/*.py", "mode": "paths_only"})
    assert "pkg/alpha.py" in text(nested)
    scoped = run(context, "grep", {"regex": "def", "path": "pkg", "glob": "*.py", "mode": "paths_only"})
    assert "pkg/alpha.py" in text(scoped)


def test_grep_ranked_map_points_at_the_best_file_first(context: LocalContext) -> None:
    (context.repo_root / "pkg" / "many.py").write_text("z = 1\nz = 2\nz = 3\n", encoding="utf-8")
    result = run(context, "grep", {"regex": "z", "mode": "ranked_map"})
    assert text(result).splitlines()[0] == "→ pkg/many.py:L1-L3"


def test_grep_scopes_matches_to_a_line_range(context: LocalContext) -> None:
    result = run(context, "grep", {"regex": "alpha", "path": "pkg/beta.py:L4-L5"})
    assert text(result) == "pkg/beta.py\n@@ 5-5\n    return alpha()"


def test_grep_says_no_matches_and_refuses_an_unknown_type(context: LocalContext) -> None:
    assert text(run(context, "grep", {"regex": "no-such-text-anywhere"})) == "no matches"
    with pytest.raises(ClientError) as caught:
        run(context, "grep", {"regex": "x", "type": "cobol"})
    assert "known:" in caught.value.message


def test_grep_treats_an_invalid_regex_as_a_literal(context: LocalContext) -> None:
    (context.repo_root / "call.py").write_text("print(alpha)\n", encoding="utf-8")
    assert text(run(context, "grep", {"regex": "print("})) == "call.py\n@@ 1-1\nprint(alpha)"


def test_grep_spills_a_large_result_to_a_named_file(context: LocalContext) -> None:
    lines = "".join(f"needle {index} " + "x" * 60 + "\n" for index in range(200))
    (context.repo_root / "pkg" / "hay.py").write_text(lines, encoding="utf-8")
    body = text(run(context, "grep", {"regex": "needle"}))
    assert body.startswith("[lc: spilled; ")
    spill_path = Path(body.removeprefix("[lc: spilled; ").split("]", 1)[0])
    assert spill_path.is_relative_to(context.config.state_dir)
    spilled = json.loads(spill_path.read_text(encoding="utf-8"))
    assert spilled["mode"] == "file_paths_with_content"
    assert spilled["content"][0]["text"].startswith("pkg/hay.py\n@@ 1-1\nneedle 0 ")


# --------------------------------------------------------------------------- #
# read_from_disk -- the offline fallback                                      #
# --------------------------------------------------------------------------- #


def test_read_from_disk_is_always_flagged_degraded(context: LocalContext) -> None:
    result = run(context, "read_from_disk", {"files": ["pkg/alpha.py"]})
    assert result.degraded
    assert result.degraded_reason == "server_unreachable:read_from_disk"
    assert "1\tdef alpha():" in text(result)


def test_read_from_disk_honours_a_line_range(context: LocalContext) -> None:
    result = run(context, "read_from_disk", {"files": ["pkg/beta.py:L4-L5"]})
    body = text(result)
    assert "4\tdef beta():" in body
    assert "from pkg.alpha" not in body


def test_read_from_disk_parses_the_read_tools_path_grammar(context: LocalContext) -> None:
    def read(entry: object) -> str:
        return text(run(context, "read_from_disk", {"files": [entry]})).split("\n", 1)[1]

    assert read("pkg/beta.py:4-5") == "4\tdef beta():\n5\t    return alpha()"
    assert read("pkg/beta.py:L4-L5:full") == "4\tdef beta():\n5\t    return alpha()"
    assert read({"path": "pkg/beta.py", "range": "L4-L5"}) == "4\tdef beta():\n5\t    return alpha()"
    assert read({"file_path": "pkg/beta.py", "range": "5"}) == "5\t    return alpha()"
    assert read("pkg/beta.py:L4-") == "4\tdef beta():\n5\t    return alpha()"
    assert read("pkg/beta.py:head=1") == "1\tfrom pkg.alpha import alpha"
    assert read("pkg/beta.py:tail=1") == "5\t    return alpha()"
    assert read("pkg/beta.py:weird") == "[no such file]"


def test_read_from_disk_accepts_an_explicit_absolute_file(context: LocalContext, tmp_path: Path) -> None:
    target = tmp_path / "outside.md"
    target.write_text("outside the repository\n", encoding="utf-8")
    result = run(context, "read_from_disk", {"files": [f"{target}:full"]})
    assert f"## {target}" in text(result)
    assert "outside the repository" in text(result)


def test_read_from_disk_refuses_a_symbol_lookup_rather_than_approximating_one(
    context: LocalContext,
) -> None:
    """The index is the only honest source for a symbol; a grep wearing that
    name would be a wrong answer that looks right."""
    with pytest.raises(ClientError) as caught:
        run(context, "read_from_disk", {"symbol": "alpha"})
    assert caught.value.code is ErrorCode.SERVER_SESSION_UNAVAILABLE


def test_read_from_disk_reports_a_missing_file_without_failing_the_batch(
    context: LocalContext,
) -> None:
    result = run(context, "read_from_disk", {"files": ["pkg/alpha.py", "pkg/gone.py"]})
    assert "[no such file]" in text(result)
    assert "def alpha" in text(result)


# --------------------------------------------------------------------------- #
# blame                                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_blame_reports_authorship_from_real_git_history(tmp_path: Path, state_dir: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("one\ntwo\n", encoding="utf-8")
    for argv in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "Test"],
        ["add", "a.py"],
        ["commit", "-qm", "first"],
    ):
        subprocess.run(["git", "-C", str(root), *argv], check=True, capture_output=True)
    config = load_config({"LEMONCROW_HOME": str(state_dir), "HOME": str(state_dir.parent)}, cwd=root)
    context = LocalContext(config=config, repo_root=root, environment=dict(os.environ))
    assert "Test" in text(run(context, "blame", {"path": "a.py", "include_churn": True}))
    assert "first" in text(run(context, "blame", {}))


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_blame_shows_churn_by_default_and_always_the_lines(tmp_path: Path, state_dir: Path) -> None:
    """``include_churn`` defaults to true, as the contract says; line-level blame is always there."""
    root = tmp_path / "repo"
    root.mkdir()
    for argv in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "Test"],
    ):
        subprocess.run(["git", "-C", str(root), *argv], check=True, capture_output=True)
    for body, message in (("one\n", "first"), ("one\ntwo\n", "second")):
        (root / "a.py").write_text(body, encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "a.py"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", message], check=True, capture_output=True)
    config = load_config({"LEMONCROW_HOME": str(state_dir), "HOME": str(state_dir.parent)}, cwd=root)
    context = LocalContext(config=config, repo_root=root, environment=dict(os.environ))
    churned = text(run(context, "blame", {"path": "a.py"}))
    assert churned.splitlines()[:2] == ["## a.py \u2014 2 commits; lines by author", "     2  Test"]
    assert "one" in churned and "two" in churned
    plain = text(run(context, "blame", {"path": "a.py", "include_churn": "false"}))
    assert not plain.startswith("## ")
    assert "one" in plain and "two" in plain


def test_blame_refuses_symbol_attribution_that_needs_the_link_index(context: LocalContext) -> None:
    with pytest.raises(ClientError) as caught:
        run(context, "blame", {"symbol_name": "alpha"})
    assert caught.value.code is ErrorCode.TOOL_NOT_SERVER_SIDE


# --------------------------------------------------------------------------- #
# scan / codemod -- optional programs this client never installs              #
# --------------------------------------------------------------------------- #


_HAS_ASTGREP = shutil.which("ast-grep") is not None
_requires_astgrep = pytest.mark.skipif(not _HAS_ASTGREP, reason="ast-grep is not installed")
_VULNERABLE = "import os\nfrom flask import request\n\n\ndef view():\n    q = request.args.get('q')\n    eval(q)\n    os.system(q)\n"


def _no_astgrep(context: LocalContext, monkeypatch: pytest.MonkeyPatch) -> LocalContext:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    return dataclasses.replace(context, environment={})


def test_codemod_names_a_missing_ast_grep_and_never_fetches_it(
    context: LocalContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ClientError) as caught:
        run(_no_astgrep(context, monkeypatch), "codemod", {"pattern": "alpha()"})
    assert caught.value.code is ErrorCode.LOCAL_TOOL_UNAVAILABLE
    assert "ast-grep" in caught.value.message
    assert caught.value.details.get("program") == "ast-grep"
    assert not (context.repo_root / ".lemoncrow" / "bin").exists()


def test_scan_without_ast_grep_runs_the_taint_check_and_says_the_rules_were_skipped(
    context: LocalContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    (context.repo_root / "view.py").write_text(_VULNERABLE, encoding="utf-8")
    result = run(_no_astgrep(context, monkeypatch), "scan", {"path": "view.py"})
    payload = json.loads(text(result))
    assert {finding["source"] for finding in payload["findings"]} == {"taint"}
    assert payload["summary"]["rules_skipped"].startswith("ast-grep unavailable")
    assert result.degraded and "rule pack skipped" in result.degraded_reason
    assert not (context.repo_root / ".lemoncrow" / "bin").exists()


@_requires_astgrep
def test_scan_reports_rule_pack_and_taint_findings(context: LocalContext) -> None:
    (context.repo_root / "view.py").write_text(_VULNERABLE, encoding="utf-8")
    result = run(context, "scan", {"path": "view.py"})
    payload = json.loads(text(result))
    assert {(finding["rule_id"], finding["source"]) for finding in payload["findings"]} >= {
        ("py-eval-exec", "rule"),
        ("taint-eval", "taint"),
        ("taint-os-system", "taint"),
    }
    assert payload["summary"]["total"] == len(payload["findings"])
    assert not result.degraded


def test_scan_refuses_a_path_outside_the_worktree(context: LocalContext) -> None:
    with pytest.raises(ClientError) as caught:
        run(context, "scan", {"path": "../elsewhere"})
    assert caught.value.code is ErrorCode.PAYLOAD_INVALID


@_requires_astgrep
def test_codemod_lists_matches_one_based_under_their_file(context: LocalContext) -> None:
    (context.repo_root / "pkg").mkdir(exist_ok=True)
    (context.repo_root / "pkg" / "a.py").write_text("x = 1\nif x == None:\n    pass\n", encoding="utf-8")
    result = run(context, "codemod", {"pattern": "$X == None", "language": "python"})
    assert text(result) == "- pkg/a.py\n  - 2 \u2014 x == None"


@_requires_astgrep
def test_codemod_previews_a_rewrite_unless_dry_run_is_false(context: LocalContext) -> None:
    target = context.repo_root / "a.py"
    target.write_text("if x == None:\n    pass\n", encoding="utf-8")
    arguments = {"pattern": "$X == None", "rewrite": "$X is None", "language": "python"}
    preview = run(context, "codemod", arguments)
    assert json.loads(text(preview))["files_changed"] == ["a.py"]
    assert "+if x is None:" in json.loads(text(preview))["diff"]
    assert preview.changed_paths == ()
    assert target.read_text(encoding="utf-8") == "if x == None:\n    pass\n"
    applied = run(context, "codemod", {**arguments, "dry_run": False})
    assert applied.changed_paths == ("a.py",)
    assert target.read_text(encoding="utf-8") == "if x is None:\n    pass\n"


def test_no_module_in_the_package_can_download_or_self_update() -> None:
    """The findings that got the previous install rejected, as a code scan."""
    forbidden = (
        "urlretrieve",
        "pip install",
        "go install",
        "install.sh",
        "git pull",
        "cloudflared",
        "enable-linger",
        "LaunchAgents",
        "systemd",
        "tarfile",
        "unpack_archive",
    )
    for path in iter_modules():
        body = code_only(path)
        for needle in forbidden:
            assert needle not in body, f"{path.name} uses {needle!r}"


# --------------------------------------------------------------------------- #
# sql                                                                         #
# --------------------------------------------------------------------------- #


def _sample_db(root: Path) -> Path:
    target = root / "data.sqlite"
    with sqlite3.connect(target) as connection:
        connection.execute("CREATE TABLE widget (id INTEGER PRIMARY KEY, label TEXT)")
        connection.execute("INSERT INTO widget VALUES (1, 'alpha')")
    connection.close()
    return target


def _sql_context(context: LocalContext, **environment: str) -> LocalContext:
    """The context with only the given environment: the developer's own DSNs stay out."""
    return dataclasses.replace(context, environment=environment)


def _widgets(root: Path) -> int:
    connection = sqlite3.connect(root / "data.sqlite")
    try:
        return int(connection.execute("SELECT count(*) FROM widget").fetchone()[0])
    finally:
        connection.close()


def test_sql_discovers_a_dsn_from_dotenv_only_when_the_operator_allows_it(context: LocalContext) -> None:
    _sample_db(context.repo_root)
    (context.repo_root / ".env").write_text("DATABASE_URL=sqlite:///data.sqlite\n", encoding="utf-8")
    query = {"action": "query", "sql": "SELECT label FROM widget"}
    refused = run(_sql_context(context), "sql", query)
    assert refused.is_error
    assert "LEMONCROW_SQL_AUTODISCOVER=1" in text(refused)
    found = run(_sql_context(context, LEMONCROW_SQL_AUTODISCOVER="1"), "sql", query)
    assert text(found) == '### sql query result \u00b7 1 rows \u00b7 auto-limit\nlabel\n"alpha"'


def test_sql_writes_only_with_write_true_and_the_operator_switch(context: LocalContext) -> None:
    _sample_db(context.repo_root)
    delete = {"action": "query", "sql": "DELETE FROM widget", "connection": "sqlite:///data.sqlite"}
    refused = run(_sql_context(context), "sql", delete)
    assert refused.is_error
    assert "pass write=true" in text(refused)
    refused = run(_sql_context(context), "sql", {**delete, "write": True})
    assert "LEMONCROW_SQL_ALLOW_WRITES=1" in text(refused)
    assert _widgets(context.repo_root) == 1
    applied = run(_sql_context(context, LEMONCROW_SQL_ALLOW_WRITES="1"), "sql", {**delete, "write": True})
    assert text(applied) == "### sql query result \u00b7 0 rows \u00b7 1 affected"
    assert _widgets(context.repo_root) == 0


def test_sql_opens_read_only_so_a_read_cannot_write(context: LocalContext) -> None:
    """Enforced by SQLite, not only by inspecting the SQL."""
    _sample_db(context.repo_root)
    connection = {"connection": "sqlite:///data.sqlite"}
    for statement in ("SELECT 1; DROP TABLE widget", "PRAGMA journal_mode(wal)"):
        result = run(_sql_context(context), "sql", {"action": "query", "sql": statement, **connection})
        assert result.is_error, statement
    with sqlite3.connect(context.repo_root / "data.sqlite") as check:
        assert check.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    check.close()
    assert _widgets(context.repo_root) == 1


def test_sql_notes_the_missing_driver_rather_than_installing_one(context: LocalContext) -> None:
    result = run(
        _sql_context(context),
        "sql",
        {"action": "query", "sql": "SELECT 1", "connection": "postgresql://user:secret@h/db"},
    )
    assert result.is_error
    payload = json.loads(text(result))
    assert payload["driver_required"] is True
    assert payload["connection"] == "postgresql://user:****@h/db"


def test_sql_describes_and_searches_the_schema(context: LocalContext) -> None:
    _sample_db(context.repo_root)
    connection = {"connection": "sqlite:///data.sqlite"}
    table = run(_sql_context(context), "sql", {"action": "table", "name": "widget", **connection})
    assert text(table) == "### sql table widget\n  - id INTEGER pk\n  - label TEXT"
    found = run(_sql_context(context), "sql", {"action": "search", "name": "lab", **connection})
    assert text(found) == "### sql search\n- widget\n  - label TEXT"


def test_sql_spills_an_oversized_cell_whole(context: LocalContext) -> None:
    _sample_db(context.repo_root)
    arguments = {
        "action": "query",
        "sql": "SELECT printf('%.5000c', 'q') || 'END' AS big",
        "connection": "sqlite:///data.sqlite",
    }
    body = text(run(_sql_context(context), "sql", arguments))
    assert "END" not in body
    spilled = Path(body.rsplit("full: ", 1)[1].rstrip(']"'))
    assert spilled.read_text(encoding="utf-8") == "q" * 5000 + "END"


def test_bash_output_past_the_byte_cap_is_spilled_whole(context: LocalContext) -> None:
    from lemoncrow_client.kit.bash_output import MAX_OUTPUT_BYTES

    size = MAX_OUTPUT_BYTES + 1000
    body = text(run(context, "bash", {"command": f"head -c {size} /dev/zero | tr '\\0' 'x'"}))
    assert f"[lc: shrunk {size}" in body
    spill_path = Path(body.rsplit("full: ", 1)[1].split("]", 1)[0])
    assert spill_path.read_text(encoding="utf-8") == "x" * size


def test_sessions_sharing_a_worktree_keep_their_own_bash_history(
    context: LocalContext, worktree: Path, state_dir: Path
) -> None:
    other = LocalContext(config=context.config, repo_root=worktree, sync=None, environment=dict(os.environ))
    run(context, "bash", {"command": "seq 1 400"})
    assert text(run(other, "bash", {"command": "seq 1 400"})).startswith("1\n")
    assert text(run(context, "bash", {"command": "seq 1 400"})).startswith("unchanged:")


def test_a_test_created_in_one_session_is_a_contract_in_another(context: LocalContext, worktree: Path) -> None:
    other = LocalContext(config=context.config, repo_root=worktree, sync=None, environment=dict(os.environ))
    body = "def test_one():\n    assert 1 == 1\n    assert 2 == 2\n"
    run(context, "edit", {"edits": [{"path": "tests/test_owned.py", "new": body, "replace": True}]})
    weakened = [{"path": "tests/test_owned.py", "old": "    assert 2 == 2\n", "new": ""}]
    with pytest.raises(ClientError, match="weakened an existing test contract"):
        run(other, "edit", {"edits": weakened})
    assert not run(context, "edit", {"edits": weakened}).is_error


def test_bash_blocks_destructive_git_before_running_it(context: LocalContext, worktree: Path) -> None:
    (worktree / "cleanup.sh").write_text("echo start\ngit clean -fd\n", encoding="utf-8")
    for command, reason in (
        ("git reset --hard", "git reset --hard blocked"),
        ("echo ok && timeout 5 git reset --hard", "git reset --hard blocked"),
        ("bash -c 'git clean -fd'", "git clean -fd blocked"),
        ("bash cleanup.sh", "blocked command inside script"),
    ):
        result = run(context, "bash", {"command": command})
        assert result.is_error, command
        assert reason in text(result), command
        assert text(result).endswith("[exit -1]"), command
        assert "ok" not in text(result).splitlines()[0], command


def test_bash_blocks_a_shell_write_outside_the_worktree_and_temp_dir(context: LocalContext, tmp_path: Path) -> None:
    outside = "/nonexistent-lemoncrow-dir/x.txt"
    blocked = run(context, "bash", {"command": f"cat > {outside} <<'EOF'\nhi\nEOF"})
    assert blocked.is_error
    assert "shell write outside the allowed write roots blocked" in text(blocked)
    scratch = tmp_path / "scratch.txt"
    allowed = run(context, "bash", {"command": f"cat > {scratch} <<'EOF'\nhi\nEOF"})
    assert not allowed.is_error
    assert scratch.read_text(encoding="utf-8") == "hi\n"
    inside = run(context, "bash", {"command": "cat > notes.txt <<'EOF'\nhi\nEOF"})
    assert not inside.is_error


def test_bash_runs_file_reads_in_process(
    context: LocalContext, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow_client.localtools import shell

    def no_fork(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an inline op must not start a shell")

    monkeypatch.setattr(shell, "_run_one", no_fork)
    (worktree / "notes.txt").write_text("".join(f"line {i}\n" for i in range(1, 11)), encoding="utf-8")
    assert text(run(context, "bash", {"command": "head -n 3 notes.txt"})) == "line 1\nline 2\nline 3\n[exit 0]"
    assert text(run(context, "bash", {"command": "wc -l notes.txt"})) == "10 notes.txt\n[exit 0]"
    missing = run(context, "bash", {"command": "tail -n 2 missing.txt"})
    assert missing.is_error
    assert "No such file or directory" in text(missing)


def test_a_blocked_command_in_an_array_does_not_stop_the_others(context: LocalContext) -> None:
    result = run(context, "bash", {"command": ["echo one", "git reset --hard", "echo three"]})
    body = text(result)
    assert result.is_error
    assert "## lc:cmd 1/3\none\n[exit 0]" in body
    assert "## lc:cmd 2/3\ngit reset --hard blocked\n[exit -1]" in body
    assert "## lc:cmd 3/3\nthree\n[exit 0]" in body


def test_bash_seeks_instead_of_formatting_a_whole_big_file(context: LocalContext, worktree: Path) -> None:
    with open(worktree / "big.bin", "wb") as handle:
        handle.truncate(9 * 1024 * 1024)
    body = text(run(context, "bash", {"command": "od -c big.bin | tail -n 2"}))
    assert body.startswith("[LemonCrow: `od … | tail` over a 9MB file")
    assert body.endswith("[exit 0]")
