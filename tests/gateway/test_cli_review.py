"""``lc review`` CLI surface: registration, JSON shape, and clean failure.

The error paths matter as much as the happy one here -- a review command that
dumps a traceback when it is run outside a repository is worse than useless in
a hook or a CI step.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import click
import pygit2
import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli

_WORKSPACE_ENV_VARS = (
    "CURSOR_WORKSPACE_ROOT",
    "LEMONCROW_WORKSPACE_ROOT",
    "CLAUDE_WORKSPACE_ROOT",
    "VSCODE_CWD",
)


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reject ast-grep discovery before it can reach the managed download path.

    ``lc review`` runs the impact detectors, and ast-grep's managed bootstrap
    would try to *download* a binary into each throwaway repo. ``sg`` is the one
    candidate name ``_reject_reason`` refuses outright.
    """

    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


def _ctx() -> click.Context:
    return click.Context(cli, info_name="lc")


def _signature(offset: int) -> pygit2.Signature:
    return pygit2.Signature("Fixture Tester", "fixture@example.com", 1700000000 + offset, 0)


def _commit(repo: Any, message: str, offset: int) -> str:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    signature = _signature(offset)
    return str(repo.create_commit("HEAD", signature, signature, message, tree, parents))


def _fixture_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.com"

    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    (root / "README.md").write_text("# fixture\n", encoding="utf-8")
    _commit(repo, "seed", 0)

    (root / "src" / "app.py").write_text("def one():\n    return 2\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text("def test_one():\n    assert True\n", encoding="utf-8")
    _commit(repo, "change the return value", 60)
    return root


def _invoke(tmp_path: Path, args: list[str], *, suppress_default_open: bool = True) -> Any:
    runner = CliRunner()
    command_args = list(args)
    if suppress_default_open and "--open" not in command_args and "--no-open" not in command_args:
        command_args.append("--no-open")
    return runner.invoke(cli, ["--root", str(tmp_path / "store"), "review", *command_args], catch_exceptions=False)


def test_review_registered_and_help_exits_zero() -> None:
    ctx = _ctx()
    assert "review" in cli.list_commands(ctx)
    assert cli.get_command(ctx, "review") is not None

    result = CliRunner().invoke(cli, ["review", "--help"])
    assert result.exit_code == 0, result.output
    assert "--json" in result.output
    assert "--staged" in result.output
    assert "--all" in result.output
    assert "--no-open" in result.output
    # The default range and the current browser product are product claims; the
    # help must not regress to the deleted selected-file/three-pane workspace.
    assert "UNCOMMITTED working-tree changes" in result.output
    assert "continuous multi-file diff stream" in result.output
    assert "overlapping review targets" in result.output
    assert "raw unit count" in result.output
    assert "three-pane" not in result.output
    assert "mark files reviewed" not in result.output


def test_review_help_leads_with_the_review_one_liner() -> None:
    """The first prose line of ``lc review --help`` is the product story.

    Click derives the root ``lc --help`` entry and the shell-completion
    description from this same first docstring line
    (``commands/__init__.py::_completion_entries``), so it is the one-liner the
    review-first positioning rests on -- pin it rather than let a docstring edit
    silently reword three surfaces at once.
    """

    result = CliRunner().invoke(cli, ["review", "--help"])
    assert result.exit_code == 0, result.output

    body = [line.strip() for line in result.output.splitlines() if line.strip()]
    # [0] is the `Usage:` line click always prints first.
    assert body[1] == "Review what the agent just did: what changed, what it affects, who made it."

    # The HTML report is named in the description, not only in the option list:
    # `--open` is the surface the review workspace grows from.
    description = result.output.partition("Options:")[0]
    assert "`--open`" in description


def test_review_short_help_matches_the_root_listing() -> None:
    """``lc --help``'s review line comes from the same one-liner, truncated."""

    review = cli.get_command(_ctx(), "review")
    assert review is not None
    assert review.get_short_help_str(limit=200) == (
        "Review what the agent just did: what changed, what it affects, who made it."
    )


def test_review_json_shape(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--json"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    assert payload["schema_version"] == 2
    for key in (
        "generated_at",
        "repo_root",
        "range_mode",
        "base_rev",
        "head_rev",
        "base_sha",
        "head_sha",
        "files",
        "symbols",
        "impact",
        "order",
        "provenance",
        "evidence",
        "index_status",
        "degraded",
        "stats",
    ):
        assert key in payload, key
    assert isinstance(payload["files"], list)
    assert payload["provenance"]["status"] == "unknown"
    assert set(payload["stats"]) == {"files", "additions", "deletions", "hunks", "symbols", "impact_sites"}


def test_review_json_round_trips_through_to_dict(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
    from lemoncrow.pro.capabilities.review.packet import build_review_packet

    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--json"])
    payload = json.loads(result.output)

    packet = build_review_packet(
        repo_root,
        resolve_rev_range(repo_root, "HEAD~1"),
        store_root=tmp_path / "store",
    )
    expected = json.loads(json.dumps(packet.to_dict(), default=str))
    # `generated_at` is a wall-clock stamp; everything else must be identical.
    payload.pop("generated_at")
    expected.pop("generated_at")
    assert payload == expected


def test_review_outside_git_repo_errors_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    for name in _WORKSPACE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LEMONCROW_WORKSPACE_ROOT", str(outside))

    runner = CliRunner()
    result = runner.invoke(cli, ["--root", str(tmp_path / "store"), "review", "--repo-root", str(outside)])
    assert result.exit_code != 0
    assert "not a git repository" in result.output
    assert "Traceback" not in result.output


def test_review_explicit_repo_root_never_falls_back_to_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit ``--repo-root`` that is not a checkout must fail, not review elsewhere.

    Regression guard: the workspace fallback used to apply to an explicitly
    named directory too, so pointing ``--repo-root`` at a non-repo silently
    produced a packet for whatever repository the *workspace* resolved to --
    a confident answer to a question nobody asked.
    """

    repo_root = _fixture_repo(tmp_path)
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    for name in _WORKSPACE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # The workspace *is* a real repository here -- the fallback would succeed.
    monkeypatch.setenv("LEMONCROW_WORKSPACE_ROOT", str(repo_root))

    runner = CliRunner()
    result = runner.invoke(cli, ["--root", str(tmp_path / "store"), "review", "--repo-root", str(outside)])
    assert result.exit_code != 0
    assert "not a git repository" in result.output
    assert str(repo_root) not in result.output


def test_review_no_changes_exits_zero_with_hint(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD..HEAD", "--no-color"])
    assert result.exit_code == 0, result.output
    assert "no changes" in result.output


def test_review_text_output_lists_files(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--no-color"])
    assert result.exit_code == 0, result.output
    assert "src/app.py" in result.output
    assert "tests/test_app.py" in result.output
    assert "START HERE" in result.output
    assert "Human review  REQUIRED" in result.output


def test_review_rich_output_still_names_every_file(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1"])
    assert result.exit_code == 0, result.output
    assert "src/app.py" in result.output
    assert "Human review  REQUIRED" in result.output


def test_bare_review_reviews_the_uncommitted_working_tree(tmp_path: Path) -> None:
    """The 80% moment is "the agent stopped -- is this safe to commit?".

    That question is about the working tree, not a pull request, and it is the
    only mode whose caller line numbers are exact by construction: the index and
    the tree agree, so nothing has to be re-anchored to a blob.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")

    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    assert payload["range_mode"] == "working_tree"
    assert payload["head_rev"] == "WORKDIR"
    assert "src/app.py" in [item["path"] for item in payload["files"]]

    # `--working-tree` is what installed scripts and the docs say; it must keep
    # meaning exactly what the bare form now means.
    explicit = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--json"])
    assert explicit.exit_code == 0, explicit.output
    assert json.loads(explicit.output)["files"] == payload["files"]


def test_bare_review_on_a_clean_tree_reviews_the_last_commit_and_says_so(tmp_path: Path) -> None:
    """An empty packet is not what someone who just committed wanted to see.

    The substitution is stated rather than performed silently: the range label
    alone leaves the reader to work out which change they are looking at.
    """

    repo_root = _fixture_repo(tmp_path)

    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--no-color"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "working tree clean — reviewing the last commit instead"
    assert "HEAD~1..HEAD" in result.output
    assert "no changes in this range" not in result.output

    # An explicit range substituted nothing, so it must not claim it did.
    explicit = _invoke(tmp_path, ["--repo-root", str(repo_root), "--base", "HEAD~1", "--no-color"])
    assert explicit.exit_code == 0, explicit.output
    assert "working tree clean" not in explicit.output


def test_review_bad_revision_errors_cleanly(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "no-such-ref"])
    assert result.exit_code != 0
    assert "cannot resolve revision" in result.output
    assert "Traceback" not in result.output


def test_review_no_impact_and_no_provenance_are_recorded(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "HEAD~1", "--json", "--no-impact", "--no-provenance"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "impact_disabled" in payload["degraded"]
    assert "provenance_disabled" in payload["degraded"]


# ---------------------------------------------------------------------------
# --track: durable review state through the CLI
# ---------------------------------------------------------------------------


def test_mark_state_choices_match_the_model() -> None:
    """The CLI's restated choice list may never drift from the real vocabulary."""

    from lemoncrow.gateway.cli.commands.review import _MARK_STATE_CHOICES
    from lemoncrow.pro.capabilities.review.session_models import MARK_STATES

    assert _MARK_STATE_CHOICES == MARK_STATES


def test_bare_review_does_not_persist_anything(tmp_path: Path) -> None:
    """Constraint 6: ``lc review`` with no flags is what it always was."""

    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1"])
    assert result.exit_code == 0, result.output
    assert "REVIEW SESSION" not in result.output
    assert not (tmp_path / "store" / "lemoncrow_reviews.db").exists()


def test_track_records_a_session_and_reopens_it_on_the_next_run(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    first = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--track"])
    assert first.exit_code == 0, first.output
    assert "REVIEW SESSION" in first.output

    review_id = first.output.split("REVIEW SESSION")[1].split()[0]
    second = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--track"])
    assert review_id in second.output
    assert "revision 1" in second.output


def test_marks_made_in_one_invocation_are_visible_in_the_next(tmp_path: Path) -> None:
    """Phase 1 acceptance through the CLI: two processes, one review state."""

    repo_root = _fixture_repo(tmp_path)
    marked = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--mark", "src/app.py"])
    assert marked.exit_code == 0, marked.output

    listed = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--marks"])
    assert listed.exit_code == 0, listed.output
    assert "reviewed" in listed.output
    assert "src/app.py" in listed.output.split("MARKS")[1]


def test_a_mark_target_that_matches_nothing_fails_loudly(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--mark", "src/never.py"])
    assert result.exit_code != 0
    assert "no reviewable unit matches" in result.output
    assert "Traceback" not in result.output


def test_units_lists_position_free_keys(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--units"])
    assert result.exit_code == 0, result.output
    body = result.output.split("UNITS")[1]
    assert "fil:" in body
    assert "hun:" in body
    assert "src/app.py" in body


def test_a_unit_key_can_be_marked_directly(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    listed = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--units"])
    key = next(
        line.split()[0]
        for line in listed.output.splitlines()
        if line.strip().startswith("fil:") and "src/app.py" in line
    )
    marked = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--mark", key, "--marks"])
    assert marked.exit_code == 0, marked.output
    # The listing prints the path once the key resolves, which is the proof the
    # key was accepted as a target in the first place.
    assert "src/app.py" in marked.output.split("MARKS")[1]
    assert "1 reviewed" in marked.output


def test_needs_changes_is_recorded_as_itself(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "HEAD~1", "--mark", "src/app.py", "--mark-state", "needs_changes", "--marks"],
    )
    assert result.exit_code == 0, result.output
    assert "needs_changes" in result.output.split("MARKS")[1]


# Two functions appended to the committed `def one(): return 2`, so one path
# carries more than one review target and the expansion has something to expand.
_TWO_MORE_SYMBOLS = "def one():\n    return 2\n\n\ndef two():\n    return 3\n\n\ndef three():\n    return 4\n"


def test_marking_by_path_moves_the_target_counters(tmp_path: Path) -> None:
    """The one mark ``--mark PATH`` creates has to be visible to the same numbers.

    ``resolve_unit`` answers a path with the file unit, but progress and closure
    are denominated in review *targets*, which are keyed to symbols or hunks.
    So the documented CLI action stored a verdict where nothing counted it and
    one block of output said ``marks 1 reviewed`` directly above
    ``targets 3 (0/3 reviewed)``.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text(_TWO_MORE_SYMBOLS, encoding="utf-8")
    base = ["--repo-root", str(repo_root), "--working-tree"]

    marked = _invoke(tmp_path, [*base, "--mark", "src/app.py", "--finish", "--json"])
    assert marked.exit_code == 0, marked.output
    state = json.loads(marked.stderr.strip().splitlines()[-1])

    progress = state["target_progress"]
    assert progress["target_count"] > 1, progress
    assert progress["reviewed"] == progress["target_count"], progress
    closure = state["closure"]
    assert closure["reviewed_targets"] == closure["target_count"], closure
    assert closure["unreviewed_targets"] == 0, closure


def test_needs_changes_by_path_reaches_the_closure_tally(tmp_path: Path) -> None:
    """A recorded objection must survive into the record of the finish decision.

    This is the worse half of the same defect: two change-requests typed by path
    were reported at ``--finish`` as ``still unreviewed 2 / still needs changes
    0``, so the finish decision was filed over an objection it never mentioned.
    """

    repo_root = _fixture_repo(tmp_path)
    result = _invoke(
        tmp_path,
        [
            "--repo-root",
            str(repo_root),
            "HEAD~1",
            "--mark",
            "src/app.py",
            "--mark-state",
            "needs_changes",
            "--finish",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    closure = json.loads(result.stderr.strip().splitlines()[-1])["closure"]
    assert closure["needs_changes"] > 0, closure
    assert closure["unreviewed_targets"] < closure["target_count"], closure


def test_a_path_mark_names_the_targets_it_landed_on(tmp_path: Path) -> None:
    """Reader spec §29.3: a bare path must not silently mark the whole file.

    It no longer marks the whole file -- it marks every target on the path --
    but a reviewer who typed one word and had three verdicts recorded still has
    to be told which three, in the same labels every other surface uses.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text(_TWO_MORE_SYMBOLS, encoding="utf-8")
    base = ["--repo-root", str(repo_root), "--working-tree"]

    result = _invoke(tmp_path, [*base, "--mark", "src/app.py", "--json"])
    assert result.exit_code == 0, result.output
    assert "src/app.py: recorded on 3 review targets" in result.output
    assert "src/app.py::two" in result.output
    assert "src/app.py::three" in result.output
    assert "src/app.py#0" in result.output

    state = json.loads(result.stderr.strip().splitlines()[-1])
    (row,) = state["path_marks"]
    assert row["target"] == "src/app.py"
    # The two added definitions, plus the hunk that owns the blank lines between
    # them -- every target on the path, named the way the browser names it.
    assert {item["label"] for item in row["covered"]} == {
        "src/app.py::two",
        "src/app.py::three",
        "src/app.py#0",
    }

    # A unit key already names one judgment, so it is recorded as typed and
    # reported as nothing -- there is no expansion to disclose.
    key = next(item["unit_key"] for item in row["covered"])
    by_key = _invoke(tmp_path, [*base, "--mark", key, "--json"])
    assert by_key.exit_code == 0, by_key.output
    assert "recorded on" not in by_key.output
    assert "path_marks" not in json.loads(by_key.stderr.strip().splitlines()[-1])


def test_marking_by_the_file_unit_key_moves_the_same_counters_a_path_does(tmp_path: Path) -> None:
    """``src/app.py`` and its ``fil:`` key are one claim typed two ways.

    ``lc review --units`` prints the ``fil:`` key and the walkthrough documents
    it right beside the path (``a path, or a fil:/hun:/sym: key``). Widening
    only the path spelling left the documented sibling landing exactly where the
    defect was -- ``marks 1 reviewed`` printed directly over ``targets 3 (0/3
    reviewed)`` -- because the file unit is a key no target counter reads.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text(_TWO_MORE_SYMBOLS, encoding="utf-8")
    base = ["--repo-root", str(repo_root), "--working-tree"]
    file_key = _symbol_unit_key(tmp_path, repo_root, "src/app.py")

    marked = _invoke(tmp_path, [*base, "--mark", file_key, "--finish", "--json"])
    assert marked.exit_code == 0, marked.output
    state = json.loads(marked.stderr.strip().splitlines()[-1])

    progress = state["target_progress"]
    assert progress["target_count"] == 3, progress
    assert progress["reviewed"] == 3, progress
    closure = state["closure"]
    assert closure["reviewed_targets"] == closure["target_count"], closure
    assert closure["unreviewed_targets"] == 0, closure

    # And it is disclosed in the same words, for the same reason: the reviewer
    # typed one thing and three verdicts were recorded.
    assert f"{file_key}: recorded on 3 review targets" in marked.output
    (row,) = state["path_marks"]
    assert row["target"] == file_key
    assert {item["label"] for item in row["covered"]} == {
        "src/app.py::two",
        "src/app.py::three",
        "src/app.py#0",
    }


def test_a_target_label_can_be_typed_straight_back(tmp_path: Path) -> None:
    """Reader spec §29.3's first sentence: accept target labels cleanly.

    The path form names the targets it landed on, in the labels the browser and
    the feedback bundle use. A label the tool puts on screen and then refuses as
    input is worse than one it never printed, so the disclosure has to round
    trip: ``--mark 'src/app.py::two'`` used to be ``Error: no reviewable unit
    matches``.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text(_TWO_MORE_SYMBOLS, encoding="utf-8")
    base = ["--repo-root", str(repo_root), "--working-tree"]

    shown = _invoke(tmp_path, [*base, "--mark", "src/app.py", "--json"])
    assert shown.exit_code == 0, shown.output
    (row,) = json.loads(shown.stderr.strip().splitlines()[-1])["path_marks"]
    labels = sorted(item["label"] for item in row["covered"])
    assert "src/app.py::two" in labels, labels

    # A second store over the same tree, so the label is the only thing typed.
    for label in labels:
        sandbox = tmp_path / f"typed-{label.replace('/', '_').replace(':', '-').replace('#', '-')}"
        typed = _invoke(sandbox, [*base, "--mark", label, "--marks", "--json"])
        assert typed.exit_code == 0, typed.output
        state = json.loads(typed.stderr.strip().splitlines()[-1])
        # One label names one judgment: recorded there, nowhere else, and
        # announced as nothing because nothing was widened.
        assert state["target_progress"]["reviewed"] == 1, (label, state["target_progress"])
        (mark,) = state["marks"]
        assert mark["label"] == label, mark
        assert "recorded on" not in typed.output
        assert "path_marks" not in state
        # Nothing in the repository is spelled like this label, so nothing was
        # outranked and there is nothing to disclose.
        assert "shadowed_marks" not in state, state["shadowed_marks"]


def test_a_repository_file_outranks_a_label_spelled_the_same_way(tmp_path: Path) -> None:
    """A label is path-shaped, so a real file can be spelled exactly like one.

    ``src/app.py::two`` is the label of a symbol target in ``src/app.py`` *and* a
    perfectly legal file name. Resolving the label first sent every verdict on a
    file the reviewer actually named into a symbol in a different file -- and
    said nothing, because the widening disclosure fires only when the resolution
    widened. The file in the repository wins; the label it beat is named.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text(_TWO_MORE_SYMBOLS, encoding="utf-8")
    (repo_root / "src" / "app.py::two").write_text("a file named like somebody else's label\n", encoding="utf-8")
    base = ["--repo-root", str(repo_root), "--working-tree"]

    result = _invoke(tmp_path, [*base, "--mark", "src/app.py::two", "--marks", "--json"])
    assert result.exit_code == 0, result.output
    state = json.loads(result.stderr.strip().splitlines()[-1])

    # Every verdict landed in the file that was named, not in src/app.py.
    assert {mark["path"] for mark in state["marks"]} == {"src/app.py::two"}, state["marks"]

    # And the reading that lost is on stderr and in the JSON, not swallowed.
    assert "also the label of another review target" in result.output
    (row,) = state["shadowed_marks"]
    assert row["target"] == "src/app.py::two"
    assert {item["path"] for item in row["shadowed"]} == {"src/app.py"}, row
    # The unit key it names is typeable, which is the point of naming it.
    keyed = _invoke(tmp_path / "by-key", [*base, "--mark", row["shadowed"][0]["unit_key"], "--marks", "--json"])
    assert keyed.exit_code == 0, keyed.output
    assert {mark["path"] for mark in json.loads(keyed.stderr.strip().splitlines()[-1])["marks"]} == {"src/app.py"}


def test_a_path_that_widened_nothing_announces_nothing(tmp_path: Path) -> None:
    """The disclosure fires on a widening, not on every path.

    A binary derives exactly one target and that target *is* the file, so
    ``--mark assets/logo.bin`` landed on the one thing it named. Echoing
    ``recorded on 1 review target -- assets/logo.bin`` is the reviewer's own
    word read back to them, and the ``path_marks`` key documented as present
    only for a widening was there too.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "assets").mkdir()
    (repo_root / "assets" / "logo.bin").write_bytes(b"\x00\x01\x02\xff\xfe")
    base = ["--repo-root", str(repo_root), "--working-tree"]

    result = _invoke(tmp_path, [*base, "--mark", "assets/logo.bin", "--json"])
    assert result.exit_code == 0, result.output
    assert "recorded on" not in result.output
    state = json.loads(result.stderr.strip().splitlines()[-1])
    assert "path_marks" not in state
    # The verdict itself still landed -- this is about the disclosure, not the
    # mark, and a binary is recorded as `unknown` rather than `reviewed`.
    assert state["mark_counts"] == {"unknown": 1}, state["mark_counts"]


def test_a_path_mark_leaves_a_new_sibling_target_unreviewed_not_changed(tmp_path: Path) -> None:
    """The lifecycle a path mark now follows, pinned by the case that changed.

    A path mark used to be one claim on the file unit, so appending an unrelated
    definition moved the file's fingerprint and reopened everything the reviewer
    had already read: the whole file came back as *changed since my review*. It
    is now recorded on the targets the path covered, so the definition that did
    not move stays reviewed and the new one arrives as new work.

    That is the right answer. Re-asking a human to re-read code that did not
    change is the exact failure ``--since-my-review`` exists to prevent -- see
    :func:`test_since_my_review_names_only_what_the_agent_rewrote`, which
    refuses to reopen a whole *file* that did not move -- and "new" versus
    "changed" is a distinction the file-level claim could not make at all.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    base = ["--repo-root", str(repo_root), "--working-tree"]
    marked = _invoke(tmp_path, [*base, "--mark", "src/app.py"])
    assert marked.exit_code == 0, marked.output
    assert "src/app.py: recorded on 1 review target -- src/app.py::one" in marked.output

    # `one` is untouched; the file grows a sibling the reviewer has not seen.
    (repo_root / "src" / "app.py").write_text(
        "def one():\n    return 3\n\n\ndef three():\n    return 3\n", encoding="utf-8"
    )
    after = _invoke(tmp_path, [*base, "--since-my-review"])
    assert after.exit_code == 0, after.output

    assert "changed since my review  0" in after.output
    assert "unchanged reviewed       1" in after.output
    assert "CHANGED SINCE MY REVIEW" not in after.output
    assert "NEW SINCE MY LAST REVIEW  (1)" in after.output
    assert "src/app.py::three" in after.output.split("NEW SINCE MY LAST REVIEW")[1]


def test_json_tracking_keeps_stdout_parseable(tmp_path: Path) -> None:
    """The review-state block is a second document; stdout stays the packet."""

    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--json", "--track"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 2

    state = json.loads(result.stderr.strip().splitlines()[-1])
    assert state["review_id"].startswith("rev-")
    assert state["revision_number"] == 1
    assert state["unit_count"] > 0


def test_a_reviewed_count_says_when_its_content_has_moved(tmp_path: Path) -> None:
    """A bare "1 reviewed" over rewritten content is the stale-mark hazard.

    Promoting the mark to ``changed_since_review`` is revision reconciliation's
    call (PR-R5), but the headline count must not read as "this much is done"
    while the fingerprints underneath it have moved.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    marked = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--mark", "src/app.py"])
    assert marked.exit_code == 0, marked.output
    assert "against content that has since changed" not in marked.output

    (repo_root / "src" / "app.py").write_text("def one():\n    return 99\n", encoding="utf-8")
    after = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--track"])
    assert after.exit_code == 0, after.output
    assert "1 against content that has since changed" in after.output


def test_per_unit_marks_on_one_file_do_not_print_as_identical_rows(tmp_path: Path) -> None:
    """A file, a hunk and a symbol mark are three claims, not one row printed thrice.

    The listing used to render ``mark['path'] or mark['unit_key']``, so all
    three printed as the same ``reviewed same src/app.py`` line and per-unit
    marking looked broken.

    The file claim is now reachable only where the whole file *is* the one
    review target -- a ``fil:`` key on a file that derived symbol or hunk
    targets expands onto them, exactly as its path does -- so the binary carries
    that third kind here.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    (repo_root / "logo.bin").write_bytes(b"\x00\x01\x02\xff\xfe")
    base = ["--repo-root", str(repo_root), "--working-tree"]
    listed = _invoke(tmp_path, [*base, "--units"])
    assert listed.exit_code == 0, listed.output

    by_kind = {
        line.split()[1]: line.split()[0]
        for line in listed.output.split("UNITS")[1].splitlines()
        if line.strip() and ("src/app.py#" in line or "src/app.py::" in line or "logo.bin" in line)
    }
    assert {"file", "hunk", "symbol"} == set(by_kind), by_kind

    args = [*base, "--marks"]
    for key in by_kind.values():
        args += ["--mark", key]
    marked = _invoke(tmp_path, args)
    assert marked.exit_code == 0, marked.output

    # The binary's downgrade notice lands in the same captured stream, so keep
    # the rows whose kind column says they are mark rows.
    rows = [
        line.strip()
        for line in marked.output.split("MARKS")[1].splitlines()
        if len(line.split()) == 4 and line.split()[2] in {"file", "hunk", "symbol"}
    ]
    assert len(rows) == len(by_kind), rows
    assert len(set(rows)) == len(rows), f"per-unit marks printed as identical rows: {rows}"
    assert any(row.endswith("logo.bin") for row in rows)
    assert any("src/app.py#" in row for row in rows)
    assert any("src/app.py::" in row for row in rows)


def test_a_downgraded_mark_is_reported_where_it_happens(tmp_path: Path) -> None:
    """A ``reviewed`` request that could not be honoured must say so.

    It used to be visible only as ``1 unknown`` in an aggregate counter, which
    a reviewer who asked for ``reviewed`` has no reason to read as a refusal.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "logo.bin").write_bytes(b"\x00\x01\x02\xff\xfe")
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--mark", "logo.bin", "--marks"])
    assert result.exit_code == 0, result.output
    assert "logo.bin: recorded as unknown, not reviewed" in result.output
    assert "could not be fingerprinted" in result.output
    assert "NOT RECORDED AS ASKED" in result.output


def test_a_downgraded_mark_reaches_json_consumers_too(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "logo.bin").write_bytes(b"\x00\x01\x02\xff\xfe")
    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--json", "--mark", "logo.bin"],
    )
    assert result.exit_code == 0, result.output
    json.loads(result.stdout)  # stdout stays the packet

    state = json.loads(result.stderr.strip().splitlines()[-1])
    assert len(state["downgraded_marks"]) == 1
    downgrade = state["downgraded_marks"][0]
    assert downgrade["target"] == "logo.bin"
    assert downgrade["requested"] == "reviewed"
    assert downgrade["recorded"] == "unknown"


def test_a_mark_is_not_downgraded_when_the_content_was_fingerprinted(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "HEAD~1", "--mark", "src/app.py", "--marks"])
    assert result.exit_code == 0, result.output
    assert "NOT RECORDED AS ASKED" not in result.output
    assert "1 reviewed" in result.output


def test_a_file_saved_while_the_packet_is_built_is_not_marked_as_reviewed(tmp_path: Path) -> None:
    """End to end over the TOCTOU window the CLI used to leave open.

    The impact pass is slow enough for a developer to hit save inside it. What
    lands in the store must be the packet that was displayed, fingerprinted
    from the same bytes -- never a re-read of whatever is on disk by then.
    """

    from lemoncrow.pro.capabilities.review import packet as packet_module

    repo_root = _fixture_repo(tmp_path)
    target = repo_root / "src" / "app.py"
    target.write_text("def one():\n    return 3\n", encoding="utf-8")

    real_load = packet_module.load_blobs
    saved_after: list[str] = []

    def _save_during_the_build(root: Path, rng: Any, files: Any) -> Any:
        blobs = real_load(root, rng, files)
        # The developer saves again while the build is still running.
        target.write_text("def one():\n    return 4242\n", encoding="utf-8")
        saved_after.append(target.read_text(encoding="utf-8"))
        return blobs

    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(packet_module, "load_blobs", _save_during_the_build)
    try:
        result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--mark", "src/app.py", "--json"])
    finally:
        monkeypatched.undo()

    assert result.exit_code == 0, result.output
    assert saved_after, "the fixture never simulated a save"
    packet = json.loads(result.stdout)
    patches = "\n".join(hunk["patch"] for item in packet["files"] for hunk in item["hunks"])
    assert "4242" not in patches, "the displayed packet is from before the save"

    state = json.loads(result.stderr.strip().splitlines()[-1])
    assert state["mark_counts"] == {"reviewed": 1}

    # And the mark still matches the stored revision on the very next run over
    # the *same* content -- which it cannot if the fingerprint described the
    # post-save bytes the reviewer was never shown.
    target.write_text("def one():\n    return 3\n", encoding="utf-8")
    again = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--marks"])
    assert again.exit_code == 0, again.output
    rows = [line.strip() for line in again.output.split("MARKS")[1].splitlines() if line.strip()]
    assert any(row.startswith("reviewed") and " same " in row for row in rows), rows


# --------------------------------------------------------------------------- #
# --since-my-review
# --------------------------------------------------------------------------- #


def test_since_my_review_is_registered_and_documented() -> None:
    result = CliRunner().invoke(cli, ["review", "--help"])
    assert result.exit_code == 0, result.output
    assert "--since-my-review" in result.output


def test_since_my_review_names_only_what_the_agent_rewrote(tmp_path: Path) -> None:
    """The terminal shape of the killer feature, exercised end to end.

    Two files reviewed, one of them rewritten. The summary must put *changed
    since my review* first and must not ask the human to re-read the file that
    did not move -- a tool that reopens both is a tool nobody trusts twice.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    (repo_root / "src" / "other.py").write_text("def two():\n    return 4\n", encoding="utf-8")

    marked = _invoke(
        tmp_path,
        [
            "--repo-root",
            str(repo_root),
            "--working-tree",
            "--mark",
            "src/app.py",
            "--mark",
            "src/other.py",
        ],
    )
    assert marked.exit_code == 0, marked.output

    # `one` itself is rewritten, not merely joined by a neighbour: a mark typed
    # as a path is recorded on the targets that path covers, so appending an
    # unrelated definition would leave the reviewed one honestly still valid.
    (repo_root / "src" / "app.py").write_text(
        "def one():\n    return 5\n\n\ndef three():\n    return 3\n", encoding="utf-8"
    )
    after = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--since-my-review"])
    assert after.exit_code == 0, after.output

    assert "SINCE MY REVIEW" in after.output
    assert "CHANGED SINCE MY REVIEW" in after.output
    block = after.output.split("CHANGED SINCE MY REVIEW")[1]
    assert "src/app.py" in block.split("UNCHANGED REVIEWED")[0]
    assert "src/other.py" not in block.split("UNCHANGED REVIEWED")[0]
    assert "UNCHANGED REVIEWED" in after.output


def test_since_my_review_on_an_untouched_tree_says_so_and_moves_nothing(tmp_path: Path) -> None:
    """Idempotency has to be visible, or a second run reads as a broken one."""

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--mark", "src/app.py"])

    again = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--since-my-review"])
    assert again.exit_code == 0, again.output
    # Phrased against the reviewer's last *review*, not against the last write:
    # every path records a revision now, so "the last snapshot" would name
    # whichever command the developer happened to type first.
    assert "unchanged since your last review" in again.output
    assert "changed since my review  0" in again.output


def test_since_my_review_reaches_json_consumers_on_stderr(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--mark", "src/app.py"])
    (repo_root / "src" / "app.py").write_text("def one():\n    return 42\n", encoding="utf-8")

    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--since-my-review", "--json"])
    assert result.exit_code == 0, result.output
    json.loads(result.stdout)  # stdout stays the packet
    state = json.loads(result.stderr.strip().splitlines()[-1])
    frontier = state["frontier"]
    assert [row["path"] for row in frontier["changed_since_review"]] == ["src/app.py"]
    assert frontier["previous_revision_number"] == 1
    # Every key here is rendered by a surface. `created` and `dropped_marks`
    # were shipped for a year and read by nothing; `removed_units` shipped as a
    # bare count of things it could not name.
    assert "created" not in frontier
    assert "dropped_marks" not in frontier
    assert frontier["removed_units"] == []


# --------------------------------------------------------------------------- #
# `--open`: the workspace is the surface, the static report is the fallback
# --------------------------------------------------------------------------- #


def _no_browser(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Swallow the browser launch and record what would have been opened."""

    import webbrowser

    opened: list[str] = []

    def _open(target: str, *args: Any, **kwargs: Any) -> bool:
        opened.append(target)
        return True

    monkeypatch.setattr(webbrowser, "open", _open)
    return opened


def test_open_without_a_bundle_falls_back_to_the_static_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pip install ships no built frontend; `--open` still has to work.

    The fallback is the documented one -- say what is missing, write the static
    report, and exit zero. Crashing because a bundle was not built would make
    `--open` unusable everywhere LemonCrow is installed rather than developed.
    """

    from lemoncrow.pro.capabilities.review import workspace as workspace_mod

    monkeypatch.setattr(workspace_mod, "bundle_dir", lambda: None)
    opened = _no_browser(monkeypatch)

    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--open"])

    assert result.exit_code == 0, result.output
    assert "no built frontend bundle found" in result.stderr
    assert "Review workspace:" not in result.stderr
    reports = list((tmp_path / "store" / "review").glob("*.html"))
    assert reports, "the fallback has to leave a report behind, not just a message"
    assert opened == [reports[0].resolve().as_uri()]


def test_open_with_a_bundle_opens_the_workspace_and_writes_no_second_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--open` no longer implies `--html`.

    Two surfaces built from the same packet disagree the moment a mark lands,
    so the workspace path must not also freeze a copy to disk.
    """

    from lemoncrow.pro.capabilities.review import workspace as workspace_mod

    handle = workspace_mod.WorkspaceHandle(
        pid=4242,
        url="http://127.0.0.1:54321",
        token="s3cret-token",
        started_at=0.0,
        repo_root=str(tmp_path / "repo"),
    )
    monkeypatch.setattr(workspace_mod, "bundle_dir", lambda: tmp_path / "bundle")
    monkeypatch.setattr(workspace_mod, "ensure_workspace", lambda *a, **k: handle)
    opened = _no_browser(monkeypatch)

    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--open"])

    assert result.exit_code == 0, result.output
    assert "http://127.0.0.1:54321/review#t=<token>&r=rev-" in result.stderr
    # The token reaches the browser, never the terminal.
    assert "s3cret-token" not in result.output
    assert "s3cret-token" not in result.stderr
    assert opened and opened[0].startswith("http://127.0.0.1:54321/review#t=s3cret-token&r=rev-")
    assert not list((tmp_path / "store" / "review").glob("*.html"))


def test_a_workspace_that_will_not_start_still_reviews(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused bind or a dead child is a notice, not an exit code."""

    from lemoncrow.pro.capabilities.review import workspace as workspace_mod

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("refused to bind to 0.0.0.0")

    monkeypatch.setattr(workspace_mod, "bundle_dir", lambda: tmp_path / "bundle")
    monkeypatch.setattr(workspace_mod, "ensure_workspace", _boom)
    _no_browser(monkeypatch)

    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--open"])

    assert result.exit_code == 0, result.output
    assert "could not start the review workspace" in result.stderr
    assert "refused to bind to 0.0.0.0" in result.stderr
    assert "no built frontend bundle found" in result.stderr


def test_html_still_writes_a_report_on_its_own(tmp_path: Path) -> None:
    """`--html PATH` is a standalone surface and does not need `--open`."""

    repo_root = _fixture_repo(tmp_path)
    out = tmp_path / "report.html"
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--html", str(out)])

    assert result.exit_code == 0, result.output
    assert out.exists()
    assert f"HTML report: {out}" in result.output


def test_review_opens_workspace_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import workspace as workspace_mod

    handle = workspace_mod.WorkspaceHandle(
        pid=4242,
        url="http://127.0.0.1:54321",
        token="default-open-token",
        started_at=0.0,
        repo_root=str(tmp_path / "repo"),
    )
    monkeypatch.setattr(workspace_mod, "bundle_dir", lambda: tmp_path / "bundle")
    monkeypatch.setattr(workspace_mod, "ensure_workspace", lambda *a, **k: handle)
    opened = _no_browser(monkeypatch)

    repo_root = _fixture_repo(tmp_path)
    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree"],
        suppress_default_open=False,
    )

    assert result.exit_code == 0, result.output
    assert "Review workspace:" in result.stderr
    assert opened and opened[0].startswith("http://127.0.0.1:54321/review#t=default-open-token&r=rev-")


def test_no_open_keeps_terminal_review_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _no_browser(monkeypatch)
    repo_root = _fixture_repo(tmp_path)

    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--no-open"],
        suppress_default_open=False,
    )

    assert result.exit_code == 0, result.output
    assert opened == []
    assert "Review workspace:" not in result.output
    assert "HTML report:" not in result.output
    assert not list((tmp_path / "store" / "review").glob("*.html"))


def test_json_does_not_open_unless_explicitly_requested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _no_browser(monkeypatch)
    repo_root = _fixture_repo(tmp_path)

    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--json"],
        suppress_default_open=False,
    )

    assert result.exit_code == 0, result.output
    json.loads(result.stdout)
    assert opened == []
    assert "Review workspace:" not in result.stderr


# --------------------------------------------------------------------------- #
# comments: the terminal half of PR-R4
# --------------------------------------------------------------------------- #


def test_annotation_kind_choices_match_the_model() -> None:
    """The CLI's four dispositions are the model's four, not a parallel list.

    The choices are restated in the command module so ``lc --help`` never
    imports the review package; this pins the copy to the original, so a fifth
    kind cannot be introduced on one side alone.
    """

    from lemoncrow.gateway.cli.commands.review import _ANNOTATION_KIND_CHOICES
    from lemoncrow.pro.capabilities.review.session_models import ANNOTATION_KINDS

    assert _ANNOTATION_KIND_CHOICES == ANNOTATION_KINDS


def test_comment_records_an_anchored_comment_and_says_where(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")

    result = _invoke(
        tmp_path,
        [
            "--repo-root",
            str(repo_root),
            "--working-tree",
            "--comment",
            "return 3 is not what the caller expects",
            "--on",
            "src/app.py:L2",
            "--comment-kind",
            "request_change",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "COMMENT RECORDED" in result.output
    assert "request_change" in result.output
    assert "src/app.py:L2" in result.output
    assert "comments 1 open" in result.output
    assert "not anchored" not in result.output


def test_comments_lists_what_is_stored_with_its_anchor(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--comment", "why 3?", "--on", "src/app.py:L2"],
    )

    listed = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--comments"])
    assert listed.exit_code == 0, listed.output
    assert "ANNOTATIONS" in listed.output
    assert "why 3?" in listed.output
    assert "src/app.py:L2" in listed.output


def test_comments_on_a_review_with_none_says_so(tmp_path: Path) -> None:
    """An empty list is a statement, not a missing section."""

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--comments"])

    assert result.exit_code == 0, result.output
    assert "ANNOTATIONS" in result.output
    assert "none recorded" in result.output


def test_comment_without_a_target_is_refused_before_anything_is_stored(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--comment", "floating"])

    assert result.exit_code != 0
    assert "--comment needs --on" in result.output


def test_a_malformed_target_is_refused_with_the_spelling_that_works(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--comment", "x", "--on", "src/app.py"],
    )

    assert result.exit_code != 0
    assert "PATH:L10" in result.output


def test_a_comment_on_a_file_outside_the_review_is_refused(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--comment", "x", "--on", "README.md:L1"],
    )

    assert result.exit_code != 0
    assert "not a file in revision" in result.output


def test_a_comment_survives_the_line_moving_and_says_which_rung_moved_it(tmp_path: Path) -> None:
    """The end-to-end promise: same text, new line, comment carried forward.

    ``COMMENT ANCHORS`` names the rung and both line numbers, because "it moved"
    is the one claim the reviewer has to take on trust, and showing where from
    and where to is what turns it back into something they can check.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--comment", "why 3?", "--on", "src/app.py:L2"],
    )

    (repo_root / "src" / "app.py").write_text(
        "import logging\n\nLOG = logging.getLogger(__name__)\n\n\ndef one():\n    return 3\n",
        encoding="utf-8",
    )
    after = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--since-my-review", "--comments"])

    assert after.exit_code == 0, after.output
    assert "COMMENT ANCHORS" in after.output
    assert "relocated" in after.output
    assert "L2→L7" in after.output
    assert "src/app.py:L7" in after.output
    assert "1 open" in after.output


def test_an_ambiguous_relocation_orphans_rather_than_guessing(tmp_path: Path) -> None:
    """Spec §5.1's hard rule, on the terminal surface.

    Two byte-identical homes for the commented text is exactly the case where a
    tool that picks one is right half the time and unfalsifiable the rest.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--comment", "why 3?", "--on", "src/app.py:L2"],
    )

    # Both copies now carry byte-identical context: `def one():` above and
    # nothing but blank lines below. Neither is more plausible than the other,
    # which is the whole point.
    (repo_root / "src" / "app.py").write_text(
        "def one():\n    return 3\n\n\n\ndef one():\n    return 3\n", encoding="utf-8"
    )
    after = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--since-my-review", "--comments"])

    assert after.exit_code == 0, after.output
    assert "orphaned" in after.output
    assert "candidate locations" in after.output
    assert "comments 1 orphaned" in after.output


def test_a_reply_threads_under_the_comment_it_answers(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    first = _invoke(
        tmp_path,
        [
            "--repo-root",
            str(repo_root),
            "--working-tree",
            "--comment",
            "why 3?",
            "--on",
            "src/app.py:L2",
            "--json",
        ],
    )
    assert first.exit_code == 0, first.output
    created = json.loads(first.stderr.strip().splitlines()[-1])["created_annotation"]

    reply = _invoke(
        tmp_path,
        [
            "--repo-root",
            str(repo_root),
            "--working-tree",
            "--comment",
            "because the fixture says so",
            "--reply-to",
            created["id"],
            "--comments",
        ],
    )
    assert reply.exit_code == 0, reply.output
    assert "↳" in reply.output
    assert "because the fixture says so" in reply.output


def test_comments_reach_json_consumers_on_stderr(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")

    result = _invoke(
        tmp_path,
        [
            "--repo-root",
            str(repo_root),
            "--working-tree",
            "--comment",
            "why 3?",
            "--on",
            "src/app.py:L2",
            "--comments",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    json.loads(result.stdout)  # stdout stays the packet
    state = json.loads(result.stderr.strip().splitlines()[-1])
    assert state["annotation_counts"] == {"open": 1}
    assert state["created_annotation"]["location"] == "src/app.py:L2"
    assert state["created_annotation"]["anchored"] is True
    assert [row["body"] for row in state["annotations"]] == ["why 3?"]


# --------------------------------------------------------------------------- #
# anchor honesty: every surviving comment says how it was re-found
# --------------------------------------------------------------------------- #


def test_every_anchor_method_has_a_plain_language_label() -> None:
    """No surface may print a rung's identifier at a human.

    Total over the vocabulary, so adding a rung without a sentence for it fails
    here rather than shipping ``exact_text_context`` to a reviewer.
    """

    from lemoncrow.pro.capabilities.review.session_models import (
        ANCHOR_METHOD_LABELS,
        ANCHOR_METHODS,
        EXACT_ANCHOR_METHODS,
        HEURISTIC_ANCHOR_METHODS,
    )

    assert set(ANCHOR_METHOD_LABELS) == set(ANCHOR_METHODS)
    assert all(label and label != method for method, label in ANCHOR_METHOD_LABELS.items())
    # Only rung 1 may claim the line was never re-derived.
    assert EXACT_ANCHOR_METHODS == ("identical_blob",)
    assert set(EXACT_ANCHOR_METHODS).isdisjoint(HEURISTIC_ANCHOR_METHODS)


def test_a_heuristic_relocation_is_never_drawn_like_an_untouched_file(tmp_path: Path) -> None:
    """The judge's case: rename the function, rewrite the docstring, keep the line.

    Both context blocks change, so rung 2 finds nothing and rung 4 re-finds the
    comment by its text alone -- landing on the very line it started on. Before
    this, the reviewer was told **nothing**: the move was filtered out as
    ``unchanged`` and the comment card said only ``in compute_total``, a
    function that no longer exists. Plan SS4.4: *never silently move a comment
    to a different line after a revision.*
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "svc.py").write_text(
        "def compute_total(items):\n"
        '    """Add up the item prices and return the total."""\n'
        "    return sum(item.price for item in items)\n",
        encoding="utf-8",
    )
    first = _invoke(
        tmp_path,
        [
            "--repo-root",
            str(repo_root),
            "--working-tree",
            "--comment",
            "prices can be None here",
            "--on",
            "src/svc.py:L3",
        ],
    )
    assert first.exit_code == 0, first.output

    (repo_root / "src" / "svc.py").write_text(
        "def total_price(items):\n"
        '    """Sum every price on the invoice, in minor units."""\n'
        "    return sum(item.price for item in items)\n",
        encoding="utf-8",
    )
    after = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--since-my-review", "--comments"],
    )

    assert after.exit_code == 0, after.output
    # The rung, in words, on the comment itself.
    assert "anchor: re-found by its text alone" in after.output
    # And in the audit block, which used to drop it for landing on the same line.
    assert "COMMENT ANCHORS  (1)" in after.output
    assert "relocated" in after.output
    assert "same line" in after.output
    # Never the stale definition the comment was written against.
    assert "compute_total" not in after.output
    assert "in total_price" in after.output


def test_a_comment_in_an_untouched_file_still_says_which_rung_carried_it(tmp_path: Path) -> None:
    """Rung 1 is the one honest ``unchanged``, and it says so too."""

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--comment", "why 3?", "--on", "src/app.py:L2"],
    )
    again = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--since-my-review", "--comments"],
    )

    assert again.exit_code == 0, again.output
    assert "anchor: file unchanged since the comment" in again.output


def test_the_anchor_rung_and_its_label_reach_json_consumers(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    result = _invoke(
        tmp_path,
        [
            "--repo-root",
            str(repo_root),
            "--working-tree",
            "--comment",
            "why 3?",
            "--on",
            "src/app.py:L2",
            "--comments",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    state = json.loads(result.stderr.strip().splitlines()[-1])
    row = state["annotations"][0]
    assert row["anchor_method"] == "identical_blob"
    assert row["anchor_method_label"] == "file unchanged since the comment"


# --------------------------------------------------------------------------- #
# recording a revision and reconciling it are one operation
# --------------------------------------------------------------------------- #

_ALPHA_BEFORE = '''def alpha_1(value):
    """Original docstring for alpha."""
    total = value * 2
    return total
'''

# Renamed, re-documented, one line left byte-identical -- so the relocation
# ladder can still find the commented line, and every honest surface has to
# report that it *searched* for it.
_ALPHA_AFTER = '''def renamed_alpha(value):
    """Completely rewritten prose describing the new behaviour."""
    total = value * 2
    return total + 1
'''


def _reviewed_then_rewritten(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """A sandbox where the file was reviewed and commented, then rewritten.

    Returns ``(sandbox, repo_root)``; the sandbox is the ``--root`` store, so two
    of these share nothing at all.

    The verdict is recorded on the *hunk* unit, by key, because these tests are
    about a mark that survives the rewrite and goes stale. The hunk is the only
    unit here that does both: the rewrite renames the file's one definition, so
    a symbol mark -- and therefore a mark typed as the path or as the file's
    ``fil:`` key, both of which expand onto the targets the file covers -- is
    discarded outright, which is the scenario
    :func:`test_a_tree_that_went_backwards_says_so_and_calls_none_of_it_new`
    already owns.
    """

    sandbox = tmp_path / name
    repo_root = _fixture_repo(sandbox)
    (repo_root / "src" / "app.py").write_text(_ALPHA_BEFORE, encoding="utf-8")
    base = ["--repo-root", str(repo_root), "--working-tree"]
    _invoke(sandbox, [*base, "--comment", "why times two?", "--on", "src/app.py:L3"])
    _invoke(sandbox, [*base, "--mark", _symbol_unit_key(sandbox, repo_root, "src/app.py#0")])
    (repo_root / "src" / "app.py").write_text(_ALPHA_AFTER, encoding="utf-8")
    return sandbox, repo_root


def _without_session_id(output: str) -> str:
    """Blank the one token that is legitimately per-sandbox."""

    return re.sub(r"rev-[0-9a-f-]{36}", "<session>", output)


def test_looking_at_the_comments_first_does_not_freeze_the_review_in_the_past(tmp_path: Path) -> None:
    """The A/B that this defect needed and did not have.

    Two identical repositories, one identical rewrite, and the *only* difference
    is whether the developer ran ``lc review --comments`` -- the documented way
    to see comments -- before asking what changed since their review.

    That used to decide the answer. Every path but ``--since-my-review``
    recorded a revision without reconciling anything, so the plain run advanced
    the stored revision and the later ``--since-my-review`` found the latest
    revision already equal to the one it was about to take, concluded nothing
    had happened, and left a human's ``reviewed`` standing over rewritten code
    and the comment labelled "file unchanged since the comment" on a file that
    had been rewritten -- permanently, because no later run would ever see a
    transition to reconcile either.

    Asserting the two orderings agree is the point. A test that exercised only
    ``--since-my-review`` passed throughout.
    """

    first_box, first_repo = _reviewed_then_rewritten(tmp_path, "comments_first")
    _invoke(first_box, ["--repo-root", str(first_repo), "--working-tree", "--comments"])
    ordering_a = _invoke(
        first_box,
        ["--repo-root", str(first_repo), "--working-tree", "--since-my-review", "--comments"],
    )

    second_box, second_repo = _reviewed_then_rewritten(tmp_path, "frontier_first")
    ordering_b = _invoke(
        second_box,
        ["--repo-root", str(second_repo), "--working-tree", "--since-my-review", "--comments"],
    )

    assert ordering_a.exit_code == 0, ordering_a.output
    assert ordering_b.exit_code == 0, ordering_b.output
    assert _without_session_id(ordering_a.output) == _without_session_id(ordering_b.output)

    # Pinned positively as well, so the two agreeing on something false would
    # still fail: the mark did not survive the rewrite, and the comment says it
    # was searched for rather than found where it was left.
    for output in (ordering_a.output, ordering_b.output):
        assert "changed since my review  1" in output
        assert "anchor: re-found by its text alone" in output
        assert "in renamed_alpha" in output
        assert "file unchanged since the comment" not in output
        assert "in alpha_1" not in output
        assert "still valid" not in output


def test_the_plain_run_reconciles_before_it_ever_prints_a_stale_verdict(tmp_path: Path) -> None:
    """``--comments`` alone must not describe the file it is looking at as unchanged.

    This is the first half of the A/B on its own, because it is the screen the
    developer actually saw: the stale sentence was rendered here, and copied
    verbatim into the ``--feedback`` Markdown handed to the agent.
    """

    sandbox, repo_root = _reviewed_then_rewritten(tmp_path, "plain")
    base = ["--repo-root", str(repo_root), "--working-tree"]

    listed = _invoke(sandbox, [*base, "--comments"])
    assert listed.exit_code == 0, listed.output
    assert "file unchanged since the comment" not in listed.output
    assert "in alpha_1" not in listed.output
    assert "anchor: re-found by its text alone · in renamed_alpha" in listed.output
    assert "1 changed_since_review" in listed.output

    bundle = _invoke(sandbox, [*base, "--feedback"])
    assert bundle.exit_code == 0, bundle.output
    assert "file unchanged since the comment" not in bundle.output
    assert "`alpha_1`" not in bundle.output


def test_looking_twice_records_nothing_the_first_look_did_not(tmp_path: Path) -> None:
    """Reconciling on every path may not turn reading into writing.

    Four invocations across three flags over one unchanged tree: one revision,
    one anchor event per comment, one mark. An audit table that grows because
    somebody looked is not evidence, and a frontier that moves because somebody
    looked is not a frontier.
    """

    from lemoncrow.pro.capabilities.review.store import ReviewStore

    sandbox, repo_root = _reviewed_then_rewritten(tmp_path, "idempotent")
    base = ["--repo-root", str(repo_root), "--working-tree"]
    for args in (["--comments"], ["--since-my-review"], ["--units"], ["--comments"]):
        assert _invoke(sandbox, [*base, *args]).exit_code == 0

    store = ReviewStore(sandbox / "store")
    (session,) = store.list_sessions()
    revisions = store.list_revisions(session.id)
    assert [revision.revision_number for revision in revisions] == [1, 2]

    (annotation,) = store.list_annotations(session.id)
    events = [move for move in store.anchor_moves_on(revisions[1].id) if move.annotation_id == annotation.id]
    assert len(events) == 1
    assert len(store.list_marks(session.id)) == 1


# --------------------------------------------------------------------------- #
# the undo: content that changed and changed back
# --------------------------------------------------------------------------- #

# Line 3 carries the comment. Line 6 is `def beta_1(y):` -- named here because
# it is the line the tool used to point a comment at after an undo, and the
# assertions below check that number against this file rather than in the
# abstract.
_UNDO_BEFORE = """def alpha_1(x):
    y = x + 1
    return x * 2  # doubled


def beta_1(y):
    return y - 1


def gamma_1(z):
    return z * z
"""

# Renamed and three lines inserted, so the commented line moves 3 -> 6 and the
# owning definition is a name that only exists in this version of the file.
_UNDO_AFTER = """def renamed_alpha(x):
    extra = 0
    another = 1
    third = 2
    y = x + 1
    return x * 2  # doubled


def beta_1(y):
    return y - 1


def gamma_1(z):
    return z * z
"""


def _commented_then_rewritten(tmp_path: Path, name: str, *, on: str = "src/app.py:L3") -> tuple[Path, Path]:
    """A sandbox at revision 2: one comment written on revision 1, then a rewrite."""

    sandbox = tmp_path / name
    repo_root = _fixture_repo(sandbox)
    (repo_root / "src" / "app.py").write_text(_UNDO_BEFORE, encoding="utf-8")
    base = ["--repo-root", str(repo_root), "--working-tree"]
    _invoke(sandbox, [*base, "--comment", "why doubled?", "--on", on])
    (repo_root / "src" / "app.py").write_text(_UNDO_AFTER, encoding="utf-8")
    assert _invoke(sandbox, [*base, "--since-my-review", "--comments"]).exit_code == 0
    return sandbox, repo_root


def test_an_undo_puts_the_comment_back_where_the_reviewer_left_it(tmp_path: Path) -> None:
    """An undo restores the file; it must also restore the comment's geometry.

    The agent renames the function and inserts lines, the anchor correctly
    relocates, and the agent then undoes the edit -- so the file is byte-identical
    to when the comment was written. The tree therefore fingerprints back to a
    revision *already on file*, and one the comment already owns its origin
    anchor event on.

    Asking "is there any event on this revision?" answered yes and skipped
    re-anchoring, leaving the row carrying the geometry of the revision in
    between: a line number and an owning definition that exist nowhere in the
    tree, over a replayed origin verdict reading "file unchanged since the
    comment". Every claim below is checked against the file's real contents.
    """

    sandbox, repo_root = _commented_then_rewritten(tmp_path, "undo")
    base = ["--repo-root", str(repo_root), "--working-tree"]

    moved = _invoke(sandbox, [*base, "--comments"])
    assert moved.exit_code == 0, moved.output
    assert "src/app.py:L6" in moved.output
    assert "in renamed_alpha" in moved.output

    (repo_root / "src" / "app.py").write_text(_UNDO_BEFORE, encoding="utf-8")
    restored = _invoke(sandbox, [*base, "--since-my-review", "--comments"])
    assert restored.exit_code == 0, restored.output

    # What the file actually says now, so the assertions are anchored to it.
    lines = (repo_root / "src" / "app.py").read_text(encoding="utf-8").splitlines()
    assert lines[2] == "    return x * 2  # doubled"
    assert lines[5] == "def beta_1(y):"
    assert "renamed_alpha" not in "\n".join(lines)

    assert "src/app.py:L3" in restored.output
    assert "in alpha_1" in restored.output
    # The three separate lies: the wrong line, a definition that does not exist,
    # and the origin's verdict replayed as this revision's.
    assert "src/app.py:L6" not in restored.output
    assert "renamed_alpha" not in restored.output
    assert "file unchanged since the comment" not in restored.output
    # The audit row for this revision describes the move that actually happened.
    assert "L6→L3" in restored.output

    # And the same sentence must not reach the agent or the browser either.
    bundle = _invoke(sandbox, [*base, "--feedback"])
    assert bundle.exit_code == 0, bundle.output
    assert "renamed_alpha" not in bundle.output
    assert "L6" not in bundle.output


def test_the_same_undo_plus_one_line_is_the_control(tmp_path: Path) -> None:
    """The control that isolates the trigger, kept so a regression cannot hide.

    Identical edit history, one trailing comment line appended after the undo:
    same content, a *new* tree fingerprint, no revision already on file and so
    no prior anchor event to mistake for an answer. This case was always right,
    and it is what proved the fault lay in the idempotency test rather than in
    the relocation ladder. It must stay right.
    """

    sandbox, repo_root = _commented_then_rewritten(tmp_path, "undo_control")
    base = ["--repo-root", str(repo_root), "--working-tree"]
    (repo_root / "src" / "app.py").write_text(_UNDO_BEFORE + "# tail\n", encoding="utf-8")

    restored = _invoke(sandbox, [*base, "--since-my-review", "--comments"])
    assert restored.exit_code == 0, restored.output
    assert "src/app.py:L3" in restored.output
    assert "in alpha_1" in restored.output
    assert "renamed_alpha" not in restored.output


def test_an_undo_un_orphans_a_comment_whose_text_is_back(tmp_path: Path) -> None:
    """The same trigger, seen from the other side: a fabricated orphan.

    The rewrite genuinely destroys the commented line, so orphaning it on
    revision 2 is correct. The undo brings the text back, byte for byte, at the
    line it was written on -- and a comment reported as "selected text gone and
    its context was not found" about text sitting untouched in front of the
    reader is worse than no anchoring at all: it sends them hunting for a
    deletion nobody made.
    """

    sandbox = tmp_path / "undo_orphan"
    repo_root = _fixture_repo(sandbox)
    (repo_root / "src" / "app.py").write_text(_UNDO_BEFORE, encoding="utf-8")
    base = ["--repo-root", str(repo_root), "--working-tree"]
    _invoke(sandbox, [*base, "--comment", "why plus one?", "--on", "src/app.py:L2"])

    (repo_root / "src" / "app.py").write_text("def alpha_1(x):\n    return 0\n", encoding="utf-8")
    gone = _invoke(sandbox, [*base, "--since-my-review", "--comments"])
    assert gone.exit_code == 0, gone.output
    assert "orphaned" in gone.output

    (repo_root / "src" / "app.py").write_text(_UNDO_BEFORE, encoding="utf-8")
    back = _invoke(sandbox, [*base, "--since-my-review", "--comments"])
    assert back.exit_code == 0, back.output

    assert (repo_root / "src" / "app.py").read_text(encoding="utf-8").splitlines()[1] == "    y = x + 1"
    assert "open       comment" in back.output
    assert "src/app.py:L2" in back.output
    assert "orphaned   comment" not in back.output
    assert "0 open · 1 orphaned" not in back.output
    assert "selected text gone" not in back.output


# --------------------------------------------------------------------------- #
# the frontier: durable state, not a shadow of the marks table
# --------------------------------------------------------------------------- #


def _symbol_unit_key(sandbox: Path, repo_root: Path, label: str) -> str:
    """The stored ``unit_key`` behind a printed unit label, via the JSON surface."""

    listed = _invoke(sandbox, ["--repo-root", str(repo_root), "--working-tree", "--units", "--json"])
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.stderr.strip().splitlines()[-1])
    for unit in payload["units"]:
        if unit["label"] == label:
            return str(unit["unit_key"])
    raise AssertionError(f"no unit labelled {label!r} in {[u['label'] for u in payload['units']]}")


def _symbol_reviewed_then_renamed(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """A sandbox whose only verdict is on a symbol the agent then renames away.

    The mark is on the *symbol*, not the file, and that is the whole scenario:
    a symbol mark's unit leaves the review outright when the symbol is renamed,
    so reconciliation deletes the row -- taking with it the only evidence a
    marks-derived frontier had that this reviewer had ever looked at anything.
    """

    sandbox = tmp_path / name
    repo_root = _fixture_repo(sandbox)
    (repo_root / "src" / "app.py").write_text(_UNDO_BEFORE, encoding="utf-8")
    key = _symbol_unit_key(sandbox, repo_root, "src/app.py::alpha_1")
    marked = _invoke(sandbox, ["--repo-root", str(repo_root), "--working-tree", "--mark", key])
    assert marked.exit_code == 0, marked.output
    (repo_root / "src" / "app.py").write_text(_UNDO_AFTER, encoding="utf-8")
    return sandbox, repo_root


def test_the_frontier_survives_the_verdict_that_reconciliation_deletes(tmp_path: Path) -> None:
    """Whoever looks first must not eat the reviewer's history.

    The previously-fixed order-dependence, relocated from the revisions table to
    the marks table. The baseline was derived from ``reviewed_revision_id`` over
    the reviewer's marks -- and reconciliation is entitled to *delete* a mark
    whose unit left the review. Rename the one symbol they approved and their
    only mark goes, so whichever command reconciled first destroyed the sole
    input the next one had. Every later command then answered "nothing reviewed
    yet" about code that was reviewed.

    Two sandboxes, one identical rename, and the only difference is what the
    developer typed first. The frontier block has to come out byte for byte the
    same.
    """

    first_box, first_repo = _symbol_reviewed_then_renamed(tmp_path, "marks_comments_first")
    _invoke(first_box, ["--repo-root", str(first_repo), "--working-tree", "--comments"])
    ordering_a = _invoke(first_box, ["--repo-root", str(first_repo), "--working-tree", "--since-my-review"])

    second_box, second_repo = _symbol_reviewed_then_renamed(tmp_path, "marks_frontier_first")
    ordering_b = _invoke(second_box, ["--repo-root", str(second_repo), "--working-tree", "--since-my-review"])

    assert ordering_a.exit_code == 0, ordering_a.output
    assert ordering_b.exit_code == 0, ordering_b.output

    def frontier_block(output: str) -> str:
        body = output.partition("SINCE MY REVIEW")[2]
        assert body, output
        return _without_session_id("SINCE MY REVIEW" + body)

    assert frontier_block(ordering_a.output) == frontier_block(ordering_b.output)

    # Pinned positively too, so two runs agreeing on something false still fail.
    for output in (ordering_a.output, ordering_b.output):
        assert "you last saw revision 1" in output
        assert "nothing reviewed yet" not in output
        assert "new                      1" in output
        assert "not yet reviewed         4" in output


def test_a_discarded_verdict_is_reported_on_every_command_and_every_run(tmp_path: Path) -> None:
    """A destroyed verdict belongs to the revision, not to whoever reconciled it.

    The ship blocker this replaces: the block was rendered off the *invocation's*
    ``RefreshResult.discarded``, and reconciliation runs once per revision. So
    the one event on this screen that looking again cannot recover was shown to
    whichever command happened to reconcile first, once, and to nobody
    afterwards -- type ``--marks`` or ``--comments`` before
    ``--since-my-review`` and a human's approval was deleted with the banner
    printed on no run at all, while the row proving the loss sat in
    ``review_discarded_marks`` carrying the exact words that belonged on screen.

    Four first-commands x three consecutive runs. Every cell says the same
    thing, because a reviewer's typing order is not a fact about their review.
    """

    # A bare `lc review` is deliberately not in the matrix: it tracks nothing,
    # so it opens no session and destroys nothing. Every command that *does*
    # track is here.
    firsts = (
        ("marks", ["--marks"]),
        ("comments", ["--comments"]),
        ("frontier", ["--since-my-review"]),
        ("track", ["--track"]),
    )
    for name, first in firsts:
        sandbox, repo_root = _symbol_reviewed_then_renamed(tmp_path, f"discard_{name}_first")
        base = ["--repo-root", str(repo_root), "--working-tree"]

        blocks: list[str] = []
        for run in range(3):
            # Run 1 types *first*; runs 2 and 3 ask a different question, which
            # is what a reviewer actually does and what used to lose the banner.
            result = _invoke(sandbox, [*base, *(first if run == 0 else ["--since-my-review"])])
            assert result.exit_code == 0, result.output
            assert "VERDICTS DISCARDED  (1)" in result.output, (name, run, result.output)
            assert "src/app.py::alpha_1" in result.output
            assert "the unit it attested to is not in this revision" in result.output
            tail = result.output.partition("VERDICTS DISCARDED")[2].splitlines()
            body: list[str] = []
            for line in tail:
                if not line.strip():
                    break
                body.append(line)
            blocks.append("\n".join(body))

        # Idempotent in text, not merely present: a second row for the same loss
        # would read as a second loss.
        assert blocks[0] == blocks[1] == blocks[2], (name, blocks)

        # The JSON says it too, on a run that is not the one that reconciled.
        as_json = _invoke(sandbox, [*base, "--since-my-review", "--json"])
        assert as_json.exit_code == 0, as_json.output
        state = json.loads(as_json.stderr.strip().splitlines()[-1])
        (discarded,) = state["discarded_verdicts"]
        assert discarded["state"] == "reviewed"
        assert discarded["label"] == "src/app.py::alpha_1"
        # And inside the frontier document, so a consumer reading only
        # `--since-my-review` is not the one consumer left in the dark.
        assert state["frontier"]["discarded_verdicts"] == state["discarded_verdicts"]


def test_a_discarded_verdict_outlives_the_revision_that_discarded_it(tmp_path: Path) -> None:
    """Three revisions, and the loss is still the reviewer's loss on the third.

    The ship blocker: the discard report was scoped to ``revision.id`` while the
    frontier beside it was scoped to the reviewer's *baseline*. Those are the
    same revision for exactly one agent edit. Reconciliation writes the row on
    revision 2, the reviewer's frontier stays on revision 1, and from revision 3
    onwards ``LEFT THE REVIEW`` still named the unit while the discard list came
    back empty underneath it -- so the screen read

        LEFT THE REVIEW  (1)
          symbol  src/app.py::alpha_1
          no longer in the change set -- nothing here to re-read

    over an approval this tool had deleted, and the browser's "your reviewed
    verdict went with it" annotation went with it too.

    Walks revision 1 (mark) -> 2 (rename) -> 3 (another edit) and asserts on
    revision 3, where the old scope answered nothing. Revision 2 is asserted on
    the way through so a fix that trades one revision for another still fails.
    """

    sandbox, repo_root = _symbol_reviewed_then_renamed(tmp_path, "discard_outlives_its_revision")
    base = ["--repo-root", str(repo_root), "--working-tree"]

    second = _invoke(sandbox, [*base, "--since-my-review"])
    assert second.exit_code == 0, second.output
    assert "revision 2  (you last saw revision 1)" in second.output
    assert "VERDICTS DISCARDED  (1)" in second.output

    # Revision 3: the agent edits again and touches nothing the reviewer judged.
    (repo_root / "src" / "app.py").write_text(_UNDO_AFTER + "\n\ndef delta_1(w):\n    return w\n", encoding="utf-8")

    third = _invoke(sandbox, [*base, "--since-my-review"])
    assert third.exit_code == 0, third.output
    assert "revision 3  (you last saw revision 1)" in third.output
    # Once, not twice: two revisions passed, one verdict was destroyed.
    assert "VERDICTS DISCARDED  (1)" in third.output, third.output
    assert "src/app.py::alpha_1" in third.output
    left = third.output.partition("LEFT THE REVIEW")[2]
    assert "src/app.py::alpha_1  — your 'reviewed' verdict went with it" in left
    assert "nothing here to re-read" not in left
    assert "see VERDICTS DISCARDED above" in left

    # Every surface, not only the one that asks about the frontier.
    marks = _invoke(sandbox, [*base, "--marks"])
    assert marks.exit_code == 0, marks.output
    assert "VERDICTS DISCARDED  (1)" in marks.output, marks.output

    as_json = _invoke(sandbox, [*base, "--since-my-review", "--json"])
    assert as_json.exit_code == 0, as_json.output
    state = json.loads(as_json.stderr.strip().splitlines()[-1])
    (discarded,) = state["discarded_verdicts"]
    assert discarded["state"] == "reviewed"
    assert discarded["label"] == "src/app.py::alpha_1"
    assert state["frontier"]["discarded_verdicts"] == state["discarded_verdicts"]
    (removed,) = state["frontier"]["removed_units"]
    assert removed["discarded_state"] == "reviewed", removed


def test_a_discarded_verdict_stops_being_news_once_the_reviewer_moves_past_it(tmp_path: Path) -> None:
    """The other half of the rule, and the reason it is not "forever".

    A discard is reportable while its unit is still absent *and* the reviewer's
    frontier has not reached the revision that destroyed it. Recording a verdict
    is the act of having taken a revision in, and the banner is on screen while
    they do it -- so once their frontier moves to that revision or past it, the
    loss is history rather than news. Without a stop rule the block would be
    permanent furniture on a review that never ends.
    """

    sandbox, repo_root = _symbol_reviewed_then_renamed(tmp_path, "discard_stops_being_news")
    base = ["--repo-root", str(repo_root), "--working-tree"]
    assert "VERDICTS DISCARDED  (1)" in _invoke(sandbox, [*base, "--since-my-review"]).output

    key = _symbol_unit_key(sandbox, repo_root, "src/app.py::beta_1")
    marked = _invoke(sandbox, [*base, "--mark", key])
    assert marked.exit_code == 0, marked.output
    # Still told on the run that moves them: the baseline this run reports
    # against is the one they arrived with.
    assert "VERDICTS DISCARDED  (1)" in marked.output, marked.output

    after = _invoke(sandbox, [*base, "--since-my-review"])
    assert after.exit_code == 0, after.output
    assert "VERDICTS DISCARDED" not in after.output, after.output


def test_a_restored_unit_stops_announcing_the_verdict_that_left_with_it(tmp_path: Path) -> None:
    """ "Still absent" is the other condition, and an undo is what tests it.

    The verdict stays destroyed -- :func:`unseen_units` keeps the restored unit
    honestly *not new and not reviewed* -- but there is something to look at
    again, so "this left the review and took your approval" is no longer the
    sentence. A window keyed only on revision numbers would keep announcing it.
    """

    sandbox, repo_root = _symbol_reviewed_then_renamed(tmp_path, "discard_after_undo")
    base = ["--repo-root", str(repo_root), "--working-tree"]
    assert "VERDICTS DISCARDED  (1)" in _invoke(sandbox, [*base, "--since-my-review"]).output

    (repo_root / "src" / "app.py").write_text(_UNDO_BEFORE, encoding="utf-8")
    undone = _invoke(sandbox, [*base, "--since-my-review"])
    assert undone.exit_code == 0, undone.output
    assert "VERDICTS DISCARDED" not in undone.output, undone.output
    assert "LEFT THE REVIEW" not in undone.output, undone.output


def test_a_unit_that_left_carrying_a_verdict_is_not_called_nothing_to_re_read(tmp_path: Path) -> None:
    """``LEFT THE REVIEW`` used to say the opposite of what happened.

    Under a symbol the reviewer had approved, the block printed *no longer in
    the change set -- nothing here to re-read*. There was plenty to re-read: the
    verdict they recorded on it had just been destroyed. True of a unit nobody
    had judged, false of one a human approved, and printed identically for both.
    """

    sandbox, repo_root = _symbol_reviewed_then_renamed(tmp_path, "left_with_a_verdict")
    result = _invoke(sandbox, ["--repo-root", str(repo_root), "--working-tree", "--since-my-review"])
    assert result.exit_code == 0, result.output

    left = result.output.partition("LEFT THE REVIEW")[2]
    assert left, result.output
    assert "src/app.py::alpha_1  — your 'reviewed' verdict went with it" in left
    assert "nothing here to re-read" not in left
    assert "see VERDICTS DISCARDED above" in left


def test_a_tree_that_went_backwards_says_so_and_calls_none_of_it_new(tmp_path: Path) -> None:
    """An undo can put the tree on a revision *older* than the reviewer's frontier.

    Mark ``alpha_1`` at revision 1, let the agent rename it (revision 2, verdict
    correctly discarded), approve the renamed symbol there, then undo. The tree
    fingerprints back to revision 1 and two sentences used to be false at once:

    * ``revision 1  (you last saw revision 2)`` -- no reviewer has a model in
      which the number they last saw exceeds the one in front of them.
    * ``NEW SINCE MY LAST REVIEW: src/app.py::alpha_1`` -- the exact unit they
      marked reviewed two revisions ago, byte for byte back.

    Both came from comparing on ``revision_number``. The frontier is now the
    revision the reviewer last recorded a verdict against, whichever number it
    carries, and ``new`` is decided on content they have not seen rather than on
    a key the baseline happened to lack.
    """

    sandbox = tmp_path / "revert_past_the_frontier"
    repo_root = _fixture_repo(sandbox)
    base = ["--repo-root", str(repo_root), "--working-tree"]

    (repo_root / "src" / "app.py").write_text(_UNDO_BEFORE, encoding="utf-8")
    alpha = _symbol_unit_key(sandbox, repo_root, "src/app.py::alpha_1")
    assert _invoke(sandbox, [*base, "--mark", alpha]).exit_code == 0

    # Revision 2: the rename discards that verdict, and the reviewer records one
    # on the renamed symbol -- which is what puts their frontier on revision 2.
    (repo_root / "src" / "app.py").write_text(_UNDO_AFTER, encoding="utf-8")
    renamed = _symbol_unit_key(sandbox, repo_root, "src/app.py::renamed_alpha")
    assert _invoke(sandbox, [*base, "--mark", renamed]).exit_code == 0

    (repo_root / "src" / "app.py").write_text(_UNDO_BEFORE, encoding="utf-8")
    back = _invoke(sandbox, [*base, "--since-my-review", "--json"])
    assert back.exit_code == 0, back.output
    state = json.loads(back.stderr.strip().splitlines()[-1])
    assert state["revision_number"] == 1
    frontier = state["frontier"]
    assert frontier["previous_revision_number"] == 2
    assert "src/app.py::alpha_1" not in [row["label"] for row in frontier["new"]]

    text = _invoke(sandbox, [*base, "--since-my-review"]).output
    assert "SINCE MY REVIEW  revision 1  (the tree went back; you last saw revision 2)" in text
    assert "(you last saw revision 2)" not in text.replace("the tree went back; you last saw revision 2", "")
    new_block = text.partition("NEW SINCE MY LAST REVIEW")[2]
    assert "src/app.py::alpha_1" not in new_block.partition("\n\n")[0]


def test_units_that_left_the_review_are_named_rather_than_counted(tmp_path: Path) -> None:
    """``removed_units`` was computed, shipped as an integer and rendered nowhere.

    A file whose edit an agent reverted leaves the change set entirely, taking
    its verdicts and its comment threads out of every other list on the screen.
    "4" is a number the reviewer cannot look up anywhere; the names are the
    only form of that fact they can act on.
    """

    sandbox = tmp_path / "left_the_review"
    repo_root = _fixture_repo(sandbox)
    base = ["--repo-root", str(repo_root), "--working-tree"]
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    (repo_root / "README.md").write_text("# fixture\n\nnotes\n", encoding="utf-8")
    assert _invoke(sandbox, [*base, "--mark", "src/app.py"]).exit_code == 0

    # The agent reverts its own README edit: those units are not in the review
    # any more, and nothing else on screen will say where they went.
    (repo_root / "README.md").write_text("# fixture\n", encoding="utf-8")
    result = _invoke(sandbox, [*base, "--since-my-review", "--json"])
    assert result.exit_code == 0, result.output
    state = json.loads(result.stderr.strip().splitlines()[-1])
    assert "README.md" in [row["label"] for row in state["frontier"]["removed_units"]]

    text = _invoke(sandbox, [*base, "--since-my-review"]).output
    block = text.partition("LEFT THE REVIEW")[2]
    assert block, text
    assert "README.md" in block
    assert "no longer in the change set" in block


def test_three_looks_at_an_unchanged_tree_are_one_answer_three_times(tmp_path: Path) -> None:
    """Idempotence is the same answer twice, not a frozen answer.

    Re-anchoring on every look is what keeps the row describing the revision
    being reported; this is the other half of that bargain. Three consecutive
    looks at a tree nobody touched must render identical text and leave the
    store byte-for-byte where they found it -- one revision, one anchor event,
    one mark, one frontier row.
    """

    from lemoncrow.pro.capabilities.review.store import ReviewStore

    sandbox = tmp_path / "three_looks"
    repo_root = _fixture_repo(sandbox)
    (repo_root / "src" / "app.py").write_text(_UNDO_BEFORE, encoding="utf-8")
    base = ["--repo-root", str(repo_root), "--working-tree"]
    _invoke(sandbox, [*base, "--comment", "why doubled?", "--on", "src/app.py:L3"])
    _invoke(sandbox, [*base, "--mark", _symbol_unit_key(sandbox, repo_root, "src/app.py::beta_1")])

    def snapshot() -> tuple[Any, ...]:
        store = ReviewStore(sandbox / "store")
        (session,) = store.list_sessions()
        with store._transaction() as conn:
            events = conn.execute(
                "SELECT annotation_id, revision_id, method, status, detail FROM annotation_anchor_events"
                " ORDER BY created_at, id"
            ).fetchall()
        return (
            tuple(revision.tree_fingerprint for revision in store.list_revisions(session.id)),
            tuple((mark.unit_key, mark.state, mark.reviewed_revision_id) for mark in store.list_marks(session.id)),
            store.frontier_revision(session.id),
            tuple(tuple(row) for row in events),
            tuple(
                (item.state, item.anchor.start_line, item.anchor_method) for item in store.list_annotations(session.id)
            ),
        )

    before = snapshot()
    looks = [_invoke(sandbox, [*base, "--since-my-review", "--comments"]) for _ in range(3)]
    for look in looks:
        assert look.exit_code == 0, look.output
    assert looks[0].output == looks[1].output == looks[2].output
    assert snapshot() == before


def test_an_orphaned_comment_does_not_claim_a_symbol_from_the_dead_revision(tmp_path: Path) -> None:
    """``_orphan`` claims nothing; the surfaces have to keep that promise.

    "not relocated" and "in ``gone_forever``" on one line is a card that denies
    in four words what it asserted in the previous four. Where the comment was
    *written* is worth saying -- it is how a reader finds the old code -- but it
    has to be said as history.
    """

    sandbox = tmp_path / "orphan"
    repo_root = _fixture_repo(sandbox)
    (repo_root / "src" / "app.py").write_text(
        'def gone_forever(value):\n    """Doomed."""\n    marker = value + 1\n    return marker\n',
        encoding="utf-8",
    )
    base = ["--repo-root", str(repo_root), "--working-tree"]
    _invoke(sandbox, [*base, "--comment", "why plus one?", "--on", "src/app.py:L3"])

    (repo_root / "src" / "app.py").write_text(
        'def totally_new_thing(payload):\n    """Nothing here resembles what was there."""\n'
        "    assembled = [payload, payload]\n    return assembled\n",
        encoding="utf-8",
    )
    result = _invoke(sandbox, [*base, "--since-my-review", "--comments"])

    assert result.exit_code == 0, result.output
    assert "anchor: not relocated · originally in gone_forever" in result.output
    assert "· in gone_forever" not in result.output
    assert "comments 1 orphaned" in result.output


def test_the_orphan_payload_separates_where_it_is_from_where_it_was(tmp_path: Path) -> None:
    """The JSON every other surface reads has to make the distinction too.

    A single ``symbol`` field cannot: the workspace card and the feedback bundle
    both rendered it as an address, and an agent handed that bundle would go
    looking for a definition that no longer exists.
    """

    sandbox = tmp_path / "orphan_json"
    repo_root = _fixture_repo(sandbox)
    (repo_root / "src" / "app.py").write_text(
        'def gone_forever(value):\n    """Doomed."""\n    marker = value + 1\n    return marker\n',
        encoding="utf-8",
    )
    base = ["--repo-root", str(repo_root), "--working-tree"]
    _invoke(sandbox, [*base, "--comment", "why plus one?", "--on", "src/app.py:L3"])
    (repo_root / "src" / "app.py").write_text(
        'def totally_new_thing(payload):\n    """Nothing here resembles what was there."""\n'
        "    assembled = [payload, payload]\n    return assembled\n",
        encoding="utf-8",
    )
    result = _invoke(sandbox, [*base, "--since-my-review", "--comments", "--json"])

    assert result.exit_code == 0, result.output
    state = json.loads(result.stderr.strip().splitlines()[-1])
    (row,) = state["annotations"]
    assert row["anchored"] is False
    assert row["symbol"] == ""
    assert row["origin_symbol"] == "gone_forever"


# --------------------------------------------------------------------------- #
# same-named symbols in one file
# --------------------------------------------------------------------------- #


def test_two_methods_named_run_do_not_print_as_one_row(tmp_path: Path) -> None:
    """The judge's second case. Distinct keys were never the problem; the rows were.

    ``svc.py::run`` printed twice sends a reviewer to whichever method they
    happen to open. In a real repository the same collapse put one name in
    UNCHANGED REVIEWED and NEW SINCE MY LAST REVIEW at the same time.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "svc.py").write_text(
        "class Reader:\n"
        "    def run(self):\n"
        "        return 'read'\n"
        "\n"
        "\n"
        "class Writer:\n"
        "    def run(self):\n"
        "        return 'write'\n",
        encoding="utf-8",
    )
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--units", "--json"])

    assert result.exit_code == 0, result.output
    state = json.loads(result.stderr.strip().splitlines()[-1])
    labels = [row["label"] for row in state["units"] if row["kind"] == "symbol"]
    assert "src/svc.py::Reader.run" in labels
    assert "src/svc.py::Writer.run" in labels
    assert len(labels) == len(set(labels)), labels


def test_two_definitions_nesting_cannot_separate_get_a_line_disambiguator(tmp_path: Path) -> None:
    """Containment is not always enough; the label still has to tell them apart."""

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "svc.py").write_text(
        "def run():\n    return 1\n\n\ndef run():\n    return 2\n",
        encoding="utf-8",
    )
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--units", "--json"])

    assert result.exit_code == 0, result.output
    state = json.loads(result.stderr.strip().splitlines()[-1])
    labels = [row["label"] for row in state["units"] if row["kind"] == "symbol"]
    assert labels == ["src/svc.py::run@L1", "src/svc.py::run@L5"]


def test_marks_on_same_named_symbols_print_as_two_rows(tmp_path: Path) -> None:
    """A mark on one twin must not read as a mark on the other."""

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "svc.py").write_text(
        "def run():\n    return 1\n\n\ndef run():\n    return 2\n",
        encoding="utf-8",
    )
    listed = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--units", "--json"])
    state = json.loads(listed.stderr.strip().splitlines()[-1])
    keys = [row["unit_key"] for row in state["units"] if row["kind"] == "symbol"]
    assert len(keys) == 2

    marked = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--mark", keys[0], "--marks", "--no-color"],
    )
    assert marked.exit_code == 0, marked.output
    assert "src/svc.py::run@L1" in marked.output
    assert "src/svc.py::run@L5" not in marked.output


# --------------------------------------------------------------------------- #
# --feedback and --finish: plan SS5.5's last two actions
# --------------------------------------------------------------------------- #


def test_feedback_and_finish_are_registered_and_documented() -> None:
    result = CliRunner().invoke(cli, ["review", "--help"])
    assert result.exit_code == 0
    assert "--feedback" in result.output
    assert "--finish" in result.output
    assert "--reopen-review" in result.output


def test_feedback_renders_the_bundle_and_says_it_delivered_nothing(tmp_path: Path) -> None:
    """Plan SS10.1's bundle, reachable from the terminal.

    The disclaimer is load-bearing: this renders Markdown and stops, and a
    reviewer who read the header and assumed the agent had been told would stop
    watching for a reply that is never coming.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    _invoke(
        tmp_path,
        [
            "--repo-root",
            str(repo_root),
            "--working-tree",
            "--comment",
            "return 3 is unexplained",
            "--on",
            "src/app.py:L2",
            "--comment-kind",
            "request_change",
        ],
    )
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--feedback", "--no-color"])

    assert result.exit_code == 0, result.output
    assert "FEEDBACK BUNDLE  (1 open · 0 orphaned · 0 resolved)" in result.output
    assert "nothing was sent to any agent, host or pull request" in result.output
    assert "## Review feedback" in result.output
    assert "### src/app.py:L2 — request change" in result.output
    assert "return 3 is unexplained" in result.output
    # Even here, the comment says how it is anchored.
    assert "file unchanged since the comment" in result.output
    assert "Human review REQUIRED" in result.output


def test_feedback_reaches_json_consumers_on_stderr(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--feedback", "--json"])

    assert result.exit_code == 0, result.output
    json.loads(result.stdout)
    state = json.loads(result.stderr.strip().splitlines()[-1])
    assert state["feedback"]["open"] == 0
    assert state["feedback"]["markdown"].startswith("## Review feedback")


def test_finish_records_the_decision_and_what_was_still_outstanding(tmp_path: Path) -> None:
    """Plan SS4.1: the human closes the review; the tool reports, never refuses.

    Nothing here reads as ``approved`` and nothing blocks a close over an open
    comment. What the reviewer is owed is the tally next to their own decision.
    """

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--comment", "why 3?", "--on", "src/app.py:L2"],
    )
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--finish", "--no-color"])

    assert result.exit_code == 0, result.output
    assert "REVIEW FINISHED" in result.output
    assert "comments still open      1" in result.output
    assert "APPROVED" not in result.output

    reopened = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--reopen-review", "--no-color"],
    )
    assert reopened.exit_code == 0, reopened.output
    assert "REVIEW REOPENED" in reopened.output


def test_finish_and_reopen_together_are_refused(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--finish", "--reopen-review"],
    )
    assert result.exit_code != 0
    assert "opposite things" in result.output


def test_discard_and_finish_together_are_refused(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--finish", "--discard-review"],
    )
    assert result.exit_code != 0
    assert "opposite things" in result.output


def test_discard_review_is_read_only_until_explicit_restore(tmp_path: Path) -> None:
    """Discard says deletion plainly and cannot be mutated through a normal reopen."""

    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    result = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--discard-review", "--no-color"],
    )
    assert result.exit_code == 0, result.output
    assert "REVIEW DISCARDED" in result.output
    assert "retention may permanently delete this review and its evidence" in result.output

    blocked = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--marks"])
    assert blocked.exit_code != 0
    assert "discarded and read-only" in blocked.output
    assert "--reopen-review" in blocked.output

    restored = _invoke(
        tmp_path,
        ["--repo-root", str(repo_root), "--working-tree", "--reopen-review", "--no-color"],
    )
    assert restored.exit_code == 0, restored.output
    assert "REVIEW REOPENED" in restored.output

    from lemoncrow.pro.capabilities.review.store import ReviewStore

    store = ReviewStore(tmp_path / "store")
    open_sessions = [session for session in store.list_sessions(status="open")]
    assert len(open_sessions) == 1
    assert store.prune(older_than_days=0) == 0


def test_finish_survives_into_the_stored_session(tmp_path: Path) -> None:
    repo_root = _fixture_repo(tmp_path)
    (repo_root / "src" / "app.py").write_text("def one():\n    return 3\n", encoding="utf-8")
    result = _invoke(tmp_path, ["--repo-root", str(repo_root), "--working-tree", "--finish", "--json"])
    assert result.exit_code == 0, result.output
    state = json.loads(result.stderr.strip().splitlines()[-1])
    assert state["closure"]["status"] == "finished"

    from lemoncrow.pro.capabilities.review.store import ReviewStore

    store = ReviewStore(tmp_path / "store")
    session = store.get_session(state["review_id"])
    assert session is not None
    assert session.status == "finished"
