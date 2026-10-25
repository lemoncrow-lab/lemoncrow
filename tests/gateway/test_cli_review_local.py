"""``lc review --ai`` with no pull request: the working tree, the index or a range, reviewed on the caller's own model.

A real git repository and the real engine path (capture, the Review API's AI endpoint, the run plan, the hard call cap,
reconcile and ``severity_for``); only the model is a fake. Nothing here uses a provider key, a local agent CLI or GitHub.
Exit codes are what a hook reads: 0 clean, 1 a finding at or above ``--fail-on`` is open, 2 incomplete under
``--strict``, 3 usage error.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.pro.capabilities.review import intelligence_runtime
from lemoncrow.pro.capabilities.review.pr_bot import local
from lemoncrow.pro.capabilities.review.pr_bot.findings import BotFinding
from lemoncrow.pro.capabilities.review.pr_bot.render import verdict
from lemoncrow.pro.capabilities.review.pr_bot.state import parse_state
from tests.gateway.pr_bot_noise_support import CONFIG, review_yaml
from tests.gateway.pr_bot_support import (
    AUTH_BUGGY,
    AUTH_FIXED,
    BASE,
    CACHE_BUGGY,
    RULE,
    TOKEN_BUGGY,
    FakeModel,
    git,
)

ENV_CLEARED = (
    "LEMONCROW_OPENAI_MODEL",
    "LEMONCROW_LITELLM_MODEL",
    "LEMONCROW_REVIEW_MODEL",
    "LEMONCROW_REVIEW_PROVIDER",
    "ANTHROPIC_API_KEY",
    "GITHUB_ACTIONS",
    "GITHUB_STEP_SUMMARY",
    "NO_COLOR",
)


@dataclass
class Lab:
    repo: Path
    store: Path
    model: FakeModel
    monkeypatch: pytest.MonkeyPatch

    def write(self, files: dict[str, str]) -> None:
        for name, text in files.items():
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def commit(self, message: str = "change") -> str:
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", message)
        return git(self.repo, "rev-parse", "HEAD")

    def run(self, *args: str, ok: tuple[int, ...] = (0, 1, 2, 3)) -> Any:
        result = CliRunner().invoke(
            cli, ["--root", str(self.store), "review", "--repo-root", str(self.repo), "--ai", *args]
        )
        assert result.exit_code in ok, (result.exit_code, result.output)
        return result

    def wire(self, *args: str, ok: tuple[int, ...] = (0, 1, 2)) -> dict[str, Any]:
        result = self.run("--json", *args, ok=ok)
        wire = json.loads(result.stdout)
        assert wire["exit"]["code"] == result.exit_code
        return wire

    def forget_calls(self) -> None:
        self.model.reset()


def make_lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> Lab:
    for name in ENV_CLEARED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for key, value in (("user.name", "A"), ("user.email", "a@example.invalid"), ("commit.gpgsign", "false")):
        git(repo, "config", key, value)
    model = FakeModel()
    monkeypatch.setattr(intelligence_runtime, "execute_structured", model)
    lab = Lab(repo, tmp_path / "store", model, monkeypatch)
    lab.write(files)
    lab.commit("base")
    return lab


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    return make_lab(tmp_path, monkeypatch, BASE)


@pytest.fixture
def ruled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    """A repository whose committed ``.lemoncrow/review.yaml`` has a critical rule for ``src/auth.py``: a violation is a P0."""

    return make_lab(tmp_path, monkeypatch, {**BASE, ".lemoncrow/review.yaml": RULE})


# --------------------------------------------------------------------------- #
# the same findings and severity as the pull request bot, rendered for a terminal
# --------------------------------------------------------------------------- #


def test_the_working_tree_is_reviewed_with_the_bots_severity_and_nothing_is_posted(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.sources import github

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a local review never talks to GitHub")

    monkeypatch.setattr(github, "run_gh", forbidden)
    lab.write({"src/auth.py": AUTH_BUGGY, "src/cache.py": CACHE_BUGGY})
    result = lab.run()
    assert result.exit_code == 0, result.output
    out = result.stdout
    # The bot rates the unrated finding a P1 and holds the unrated performance finding back as a P2; so does this.
    assert out.startswith("LemonCrow review · working tree against HEAD\n")
    assert "No must-fix findings · 1 should-fix · 1 optional" in out
    assert "P1 · should fix  src/auth.py:2-3  [security]" in out and "lets a missing principal through" in out
    assert "1 optional finding (P2):" in out and "grows without a bound" in out
    assert "lemoncrow:state" not in out and "<!--" not in out
    assert sorted(lab.model.paths()) == ["src/auth.py", "src/cache.py"]


def test_a_critical_rule_violation_is_a_p0_and_exits_1(ruled: Lab) -> None:
    ruled.write({"src/auth.py": AUTH_BUGGY})
    result = ruled.run()
    assert result.exit_code == 1
    assert "1 must-fix before merge" in result.stdout
    assert "P0 · must fix  src/auth.py" in result.stdout
    assert "Exit 1: 1 open finding at or above --fail-on p0 (a P0)" in result.stdout


def test_the_severity_is_the_one_the_pull_request_bot_stores(ruled: Lab) -> None:
    ruled.write({"src/auth.py": AUTH_BUGGY, "src/cache.py": CACHE_BUGGY})
    wire = ruled.wire()
    # The rule check cites the repository's rule (a P0); the ordinary investigation of the same code is unrated (a P1).
    rows = {(row["category"], row["title"]): row["severity"] for row in wire["findings"]}
    assert rows == {
        ("critical_rule", "lets a missing principal through"): "p0",
        ("security", "lets a missing principal through"): "p1",
        ("performance", "grows without a bound"): "p2",
    }
    assert wire["verdict"] == {
        "state": "must_fix",
        "headline": "1 must-fix before merge",
        "p0": 1,
        "p1": 1,
        "p2": 1,
    }


# --------------------------------------------------------------------------- #
# the repository's noise controls: the trusted base's .lemoncrow/review.yaml, as on a pull request
# --------------------------------------------------------------------------- #

AUTH_TITLE = "lets a missing principal through"
CACHE_TITLE = "grows without a bound"


def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str) -> Lab:
    """A repository whose committed ``.lemoncrow/review.yaml`` is *config*."""

    return make_lab(tmp_path, monkeypatch, {**BASE, CONFIG: config})


def test_a_finding_below_the_threshold_is_held_back_and_shown_apart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = configured(tmp_path, monkeypatch, review_yaml(strictness=3))
    lab.write({"src/auth.py": AUTH_BUGGY, "src/cache.py": CACHE_BUGGY})  # a P1 and a P2
    result = lab.run("--fail-on", "p1")
    out = result.stdout
    assert result.exit_code == 1  # the P1 is surfaced; only the P2 is held back
    # In the headline as a count, in no other count, and in a section of its own that says why.
    assert "No must-fix findings · 1 should-fix · 1 held back" in out
    assert "1 finding held back below strictness 3 (from .lemoncrow/review.yaml)" in out
    assert "not counted toward --fail-on. A P0 is never held back:" in out
    assert f"  P2  src/cache.py:5  {CACHE_TITLE}" in out
    assert "optional finding" not in out and "1 open finding at or above --fail-on p1 (a P0 or P1)" in out
    # The P1 is a finding in full; the held-back one is one line.
    assert f"P1 · should fix  src/auth.py:2-3  [security]\n    {AUTH_TITLE}" in out
    assert "grows without a bound\n    " not in out


def test_a_p2_under_strictness_3_never_counts_toward_fail_on_and_is_in_the_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = configured(tmp_path, monkeypatch, review_yaml(strictness=3))
    lab.write({"src/cache.py": CACHE_BUGGY})  # only a P2
    wire = lab.wire("--fail-on", "p1")
    assert wire["exit"] == {"code": 0, "reason": "", "fail_on": "p1", "strict": False}
    assert wire["verdict"] == {
        "state": "clean",
        "headline": "No must-fix findings",
        "p0": 0,
        "p1": 0,
        "p2": 0,
        "held_back": 1,
    }
    assert [(row["severity"], row["title"], row["state"]) for row in wire["held_back"]] == [("p2", CACHE_TITLE, "new")]
    # Still tracked: lowering the threshold shows it again with no new review.
    assert [row["title"] for row in wire["findings"]] == [CACHE_TITLE]
    text = lab.run().stdout
    assert "No must-fix findings · 1 held back" in text and "Exit " not in text


def test_without_a_threshold_nothing_is_held_back_and_the_json_reads_as_it_always_did(lab: Lab) -> None:
    lab.write({"src/cache.py": CACHE_BUGGY})
    wire = lab.wire()
    assert "held_back" not in wire and "held_back" not in wire["verdict"]
    assert wire["verdict"]["p2"] == 1
    assert "1 optional finding (P2):" in lab.run().stdout and "held back below" not in lab.run().stdout


def test_a_strictness_that_holds_back_a_p1_stops_it_failing_a_p1_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = configured(tmp_path, monkeypatch, review_yaml(finding_threshold="critical"))
    lab.write({"src/auth.py": AUTH_BUGGY})  # one P1
    wire = lab.wire("--fail-on", "p1")
    assert wire["exit"]["code"] == 0 and wire["verdict"]["p1"] == 0 and wire["verdict"]["held_back"] == 1
    assert [row["severity"] for row in wire["held_back"]] == ["p1"]
    assert "held back below threshold critical" in lab.run("--fail-on", "p1").stdout


def test_a_p0_is_never_held_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lab = configured(tmp_path, monkeypatch, review_yaml(finding_threshold="critical") + RULE)
    lab.write({"src/auth.py": AUTH_BUGGY, "src/cache.py": CACHE_BUGGY})
    wire = lab.wire()
    assert wire["exit"]["code"] == 1 and wire["verdict"]["state"] == "must_fix"
    assert (wire["verdict"]["p0"], wire["verdict"]["held_back"]) == (1, 2)
    assert sorted(row["severity"] for row in wire["held_back"]) == ["p1", "p2"]  # the P1 and the P2; never the P0
    text = lab.run().stdout
    assert "P0 · must fix  src/auth.py" in text and "2 findings held back below threshold critical" in text
    assert "Exit 1: 1 open finding at or above --fail-on p0 (a P0)" in text


def test_a_held_back_finding_stays_held_back_when_the_result_is_carried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = configured(tmp_path, monkeypatch, review_yaml(strictness=3))
    lab.write({"src/auth.py": AUTH_BUGGY, "src/cache.py": CACHE_BUGGY})
    first = lab.wire("--fail-on", "p1")
    lab.forget_calls()
    again = lab.wire("--fail-on", "p1")
    assert lab.model.calls == [] and again["cached"] is True
    assert again["verdict"] == first["verdict"]
    assert [(row["severity"], row["title"]) for row in again["held_back"]] == [("p2", CACHE_TITLE)]
    assert again["exit"]["code"] == 1


def test_an_incomplete_rerun_keeps_the_threshold_the_last_review_ran_under(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = configured(tmp_path, monkeypatch, review_yaml(strictness=3))
    lab.write({"src/auth.py": AUTH_BUGGY, "src/cache.py": CACHE_BUGGY})
    lab.wire("--fail-on", "p1")
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.delenv("LEMONCROW_LLM_BACKEND")
    lab.write({"src/plain.py": "VALUE = 3\n"})  # new code, and no model to review it with
    wire = lab.wire("--fail-on", "p1")
    assert wire["complete"] is False
    assert (wire["verdict"]["p1"], wire["verdict"]["p2"], wire["verdict"]["held_back"]) == (1, 0, 1)
    assert [row["title"] for row in wire["held_back"]] == [CACHE_TITLE]


def test_a_rule_severity_in_the_trusted_base_applies_to_a_local_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lowered = configured(tmp_path, monkeypatch, review_yaml(rules={"no-bug-markers": {"severity": "p1"}}) + RULE)
    lowered.write({"src/auth.py": AUTH_BUGGY})
    wire = lowered.wire()
    # The rule's violation is a P1 now, so it is no gate; without the setting it is a P0 (see the tests above).
    assert sorted((row["category"], row["severity"]) for row in wire["findings"]) == [
        ("critical_rule", "p1"),
        ("security", "p1"),
    ]
    assert wire["exit"]["code"] == 0 and wire["verdict"]["p0"] == 0 and wire["verdict"]["p1"] == 2


def test_ignored_paths_are_not_reviewed_and_the_json_says_how_many(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = configured(tmp_path, monkeypatch, review_yaml(ignore=["src/cache.py"]))
    lab.write({"src/auth.py": AUTH_BUGGY, "src/cache.py": CACHE_BUGGY})
    wire = lab.wire()
    assert lab.model.paths() == ["src/auth.py"]  # the model never reads the ignored file
    assert [row["title"] for row in wire["findings"]] == [AUTH_TITLE]
    assert wire["coverage"]["files"] == ["src/auth.py"]
    assert "1 file left out by review.ignore" in wire["notes"]


def test_a_setting_that_cannot_be_applied_is_a_note_and_the_default_stands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = configured(tmp_path, monkeypatch, review_yaml(strictness=9))
    lab.write({"src/cache.py": CACHE_BUGGY})
    wire = lab.wire()
    assert any(note.startswith("config: ") and "strictness must be 1, 2 or 3" in note for note in wire["notes"])
    assert wire["verdict"]["p2"] == 1 and "held_back" not in wire  # the default threshold: a P2 is listed, not held


# --------------------------------------------------------------------------- #
# exit codes
# --------------------------------------------------------------------------- #


def test_a_clean_change_exits_0(lab: Lab) -> None:
    lab.write({"src/plain.py": "VALUE = 2\n"})
    result = lab.run()
    assert result.exit_code == 0 and "No must-fix findings" in result.stdout
    assert "Exit " not in result.stdout


@pytest.mark.parametrize(
    ("fail_on", "code"),
    [("none", 0), ("p0", 0), ("p1", 1)],
)
def test_fail_on_chooses_which_open_findings_exit_1(lab: Lab, fail_on: str, code: int) -> None:
    lab.write({"src/auth.py": AUTH_BUGGY})  # one P1
    assert lab.run("--fail-on", fail_on).exit_code == code


def test_fail_on_defaults_to_p0_and_none_never_exits_1_even_for_a_p0(ruled: Lab) -> None:
    ruled.write({"src/auth.py": AUTH_BUGGY})
    assert ruled.run().exit_code == 1
    assert ruled.run("--fail-on", "none").exit_code == 0
    assert ruled.run("--fail-on", "p1").exit_code == 1


def test_an_incomplete_review_fails_open_and_exits_2_only_under_strict(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_REVIEW_INTELLIGENCE_MAX_CALLS", "1")  # the hard cap: one of two candidates is covered
    lab.write({"src/auth.py": AUTH_BUGGY, "src/cache.py": CACHE_BUGGY})
    open_ = lab.run()
    assert open_.exit_code == 0
    assert "Review incomplete: the model-call budget ran out" in open_.stdout
    assert len(lab.model.calls) == 1  # the cap still bounds the review
    lab.forget_calls()
    strict = lab.run("--strict", "--fresh")
    assert strict.exit_code == 2
    assert "Exit 2: the review is incomplete and --strict is set: the model-call budget ran out" in strict.stdout
    assert lab.wire("--strict", "--fresh", ok=(2,))["complete"] is False


def test_a_found_p0_outranks_an_incomplete_review_under_strict(ruled: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    ruled.write({"src/auth.py": AUTH_BUGGY})
    assert ruled.run("--strict").exit_code == 1  # the P0 is found and carried
    monkeypatch.setenv("LEMONCROW_REVIEW_INTELLIGENCE_MAX_CALLS", "1")
    ruled.write({"src/cache.py": CACHE_BUGGY, "src/token.py": TOKEN_BUGGY})
    result = ruled.run("--strict")  # the cap leaves new work unreviewed: incomplete, but the open P0 still decides
    assert result.exit_code == 1 and "Incomplete:" in result.stdout
    assert ruled.run("--strict", "--fail-on", "none").exit_code == 2


def test_risk_signals_left_unexamined_are_incomplete_coverage(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    # The call cap allows two investigations; with three risk signals one is left unexamined: "partially reviewed",
    # never clean. (Without the cap Auto examines all three: a change this small is covered whole.)
    monkeypatch.setenv("LEMONCROW_REVIEW_INTELLIGENCE_MAX_CALLS", "2")
    lab.write({"src/auth.py": AUTH_BUGGY, "src/cache.py": CACHE_BUGGY, "src/token.py": TOKEN_BUGGY})
    plain = lab.run()
    assert plain.exit_code == 0 and "Partially reviewed: 2 of 3 risk signals examined" in plain.stdout
    wire = lab.wire("--strict", "--fresh", ok=(2,))
    assert wire["complete"] is False and wire["verdict"]["state"] == "partial"
    assert wire["incomplete_reason"] == "Partially reviewed: 2 of 3 risk signals examined"
    assert (wire["coverage"]["candidates"], wire["coverage"]["investigated"]) == (3, 2)
    monkeypatch.setenv("LEMONCROW_REVIEW_INTELLIGENCE_MAX_CALLS", "20")
    deeper = lab.wire("--strict", "--fresh", "--depth", "thorough", ok=(0, 2))
    assert deeper["coverage"]["investigated"] >= wire["coverage"]["investigated"]


def test_no_model_route_fails_open_and_exits_2_under_strict(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.delenv("LEMONCROW_LLM_BACKEND")
    lab.write({"src/auth.py": AUTH_BUGGY})
    soft = lab.run()
    assert soft.exit_code == 0
    assert "Review incomplete: no model route is configured" in soft.stdout
    hard = lab.run("--strict")
    assert hard.exit_code == 2 and "no model route is configured" in hard.stdout
    assert lab.model.calls == []


def test_the_kill_switch_is_respected_and_no_model_is_called(lab: Lab) -> None:
    lab.store.mkdir(parents=True, exist_ok=True)  # `--root` is the LemonCrow root whose settings hold the switch
    (lab.store / "plugin_settings.json").write_text(json.dumps({"intelligence.enabled": False}))
    lab.write({"src/auth.py": AUTH_BUGGY})
    soft = lab.run()
    assert soft.exit_code == 0 and "AI review is turned off" in soft.stdout
    assert lab.run("--strict").exit_code == 2
    assert lab.model.calls == []


def test_a_provider_that_fails_is_an_incomplete_review_not_a_finding(lab: Lab) -> None:
    lab.model.fail = True
    lab.write({"src/auth.py": AUTH_BUGGY})
    assert lab.run().exit_code == 0
    wire = lab.wire("--strict", "--fresh", ok=(2,))
    assert "model calls failed" in wire["incomplete_reason"] and wire["findings"] == []


def test_an_unexpected_error_in_the_review_is_incomplete_and_never_exit_1(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.pr_bot import engine

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the review API fell over")

    monkeypatch.setattr(engine, "run_ai_stage", broken)
    lab.write({"src/auth.py": AUTH_BUGGY})
    soft = lab.run()
    assert soft.exit_code == 0 and "the review could not run (RuntimeError: the review API fell over)" in soft.stdout
    assert lab.run("--strict").exit_code == 2


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--fail-on", "bogus"], "is not one of"),
        (["--depth", "huge"], "is not one of"),
        (["--mark", "src/a.py"], "--ai reviews a change on its own; drop --mark"),
        (["--list-reviews"], "drop --current/--resume-author/--list-reviews"),
        (["--publish"], "a local --ai review posts nothing"),
        (["--dry-run"], "--dry-run only applies to --publish or --publish-bot"),
        (["--base", "no-such-revision"], "no-such-revision"),
        (["no-such-revision..HEAD"], "no-such-revision"),
        (["--max-usd-per-pr", "0"], "0 is not in the range"),
        (["--no-such-flag"], "No such option"),
    ],
)
def test_a_bad_request_is_a_usage_error_with_exit_code_3(lab: Lab, args: list[str], message: str) -> None:
    lab.write({"src/auth.py": AUTH_BUGGY})
    result = lab.run(*args, ok=(3,))
    assert result.exit_code == 3 and message in result.output
    assert lab.model.calls == []


def test_a_directory_that_is_not_a_repository_is_a_usage_error(lab: Lab, tmp_path: Path) -> None:
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    result = CliRunner().invoke(cli, ["--root", str(lab.store), "review", "--repo-root", str(elsewhere), "--ai"])
    assert result.exit_code == 3 and lab.model.calls == []


def test_a_pull_request_review_is_unchanged_by_all_this(lab: Lab) -> None:
    # --publish-bot still needs a pull request, and a bad --fail-on for a pull request keeps its own exit code.
    result = CliRunner().invoke(
        cli, ["--root", str(lab.store), "review", "--repo-root", str(lab.repo), "--publish-bot"]
    )
    assert result.exit_code == 1 and "--publish-bot posts to a pull request; add --pr NUMBER|URL" in result.output


# --------------------------------------------------------------------------- #
# which change is reviewed
# --------------------------------------------------------------------------- #


def test_staged_reviews_the_index_not_the_working_tree(lab: Lab) -> None:
    lab.write({"src/auth.py": AUTH_BUGGY})
    git(lab.repo, "add", "src/auth.py")
    lab.write({"src/auth.py": AUTH_FIXED})  # the working tree has moved on; the index has not
    result = lab.run("--staged")
    assert "staged changes against HEAD" in result.stdout
    assert "lets a missing principal through" in result.stdout
    assert lab.run("--working-tree", "--fresh").stdout.count("lets a missing principal through") == 0


def test_a_range_is_reviewed_from_its_base_to_its_head(lab: Lab) -> None:
    git(lab.repo, "checkout", "-qb", "feature")
    lab.write({"src/auth.py": AUTH_BUGGY})
    lab.commit("buggy auth")
    git(lab.repo, "checkout", "-q", "main")  # the working tree is clean and on main: only the range names the change
    ranged = lab.run("--base", "main", "--head", "feature")
    assert "main..feature" in ranged.stdout and "lets a missing principal through" in ranged.stdout
    assert "lets a missing principal through" in lab.run("main..feature").stdout
    git(lab.repo, "checkout", "-q", "feature")
    single = lab.run("main")  # one revision: from its merge base with HEAD
    assert "main..HEAD" in single.stdout and "lets a missing principal through" in single.stdout


def test_a_clean_tree_reviews_the_last_commit_and_says_so(lab: Lab) -> None:
    lab.write({"src/auth.py": AUTH_BUGGY})
    lab.commit("buggy auth")
    result = lab.run()
    assert "working tree clean: reviewing the last commit instead" in result.stdout
    assert "lets a missing principal through" in result.stdout


def test_nothing_to_review_is_clean_and_needs_no_model(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.delenv("LEMONCROW_LLM_BACKEND")
    result = lab.run("--staged", "--strict")  # nothing is staged, and there is no model: still nothing to review
    assert result.exit_code == 0 and "Nothing to review: no changed files in this range." in result.stdout
    wire = lab.wire("--staged", "--strict", ok=(0,))
    assert wire["empty"] is True and wire["findings"] == [] and wire["complete"] is True
    assert lab.model.calls == []


# --------------------------------------------------------------------------- #
# --json
# --------------------------------------------------------------------------- #


def test_json_is_a_stable_machine_readable_result(ruled: Lab) -> None:
    ruled.write({"src/auth.py": AUTH_BUGGY, "src/cache.py": CACHE_BUGGY})
    result = ruled.run("--json", "--fail-on", "p1")
    wire = json.loads(result.stdout)  # stdout is the document and nothing else; progress is on stderr
    assert set(wire) == {
        "schema",
        "kind",
        "scope",
        "empty",
        "cached",
        "verdict",
        "complete",
        "incomplete_reason",
        "exit",
        "findings",
        "coverage",
        "depth",
        "model",
        "usage",
        "usage_total",
        "notes",
    }
    assert (wire["schema"], wire["kind"], wire["empty"], wire["cached"], wire["depth"]) == (
        1,
        "local_review",
        False,
        False,
        "auto",
    )
    assert wire["scope"]["mode"] == "working_tree" and wire["scope"]["label"] == "working tree against HEAD"
    assert len(wire["scope"]["base"]) == 40 and len(wire["scope"]["head"]) == 40
    assert wire["exit"] == {
        "code": 1,
        "reason": "2 open findings at or above --fail-on p1 (a P0 or P1)",
        "fail_on": "p1",
        "strict": False,
    }
    assert set(wire["verdict"]) == {"state", "headline", "p0", "p1", "p2"}
    assert wire["complete"] is True and wire["incomplete_reason"] == ""
    assert set(wire["coverage"]) == {"files", "targets", "rereviewed", "unchanged", "candidates", "investigated"}
    assert wire["coverage"]["files"] == ["src/auth.py", "src/cache.py"]
    assert set(wire["usage"]) >= {"calls", "input_tokens", "output_tokens", "cost_usd"} and wire["usage"][
        "calls"
    ] == len(ruled.model.calls)
    assert wire["usage_total"]["calls"] == len(ruled.model.calls) and wire["model"].endswith("gpt-4o-mini")
    for row in wire["findings"]:  # the rows the pull request bot's hidden state carries
        assert {"id", "fingerprint", "severity", "category", "state", "path", "line", "title", "explanation"} <= set(
            row
        )
        assert row["severity"] in {"p0", "p1", "p2"} and row["state"] == "new"
    assert "\x1b" not in result.stdout


def test_json_for_an_incomplete_review_says_why(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.delenv("LEMONCROW_LLM_BACKEND")
    lab.write({"src/auth.py": AUTH_BUGGY})
    wire = lab.wire("--strict", ok=(2,))
    assert wire["complete"] is False and "no model route is configured" in wire["incomplete_reason"]
    assert wire["exit"]["code"] == 2 and wire["findings"] == [] and wire["coverage"]["targets"] == 0


# --------------------------------------------------------------------------- #
# carried results: the same fingerprints as the pull request bot
# --------------------------------------------------------------------------- #


def test_a_rerun_on_an_unchanged_tree_makes_no_model_call_and_keeps_its_findings(ruled: Lab) -> None:
    ruled.write({"src/auth.py": AUTH_BUGGY, "src/cache.py": CACHE_BUGGY})
    first = ruled.wire()
    assert first["cached"] is False and ruled.model.calls
    ruled.forget_calls()

    again = ruled.wire()
    assert ruled.model.calls == []  # zero model calls
    assert again["cached"] is True and again["usage"]["calls"] == 0
    assert again["exit"]["code"] == 1  # the open P0 is carried, so the hook still stops
    assert [(row["id"], row["state"]) for row in again["findings"]] == [
        (row["id"], "carried") for row in first["findings"]
    ]
    assert again["usage_total"] == first["usage_total"]
    assert again["coverage"]["rereviewed"] == 0 and again["coverage"]["unchanged"] == first["coverage"]["targets"]
    text = ruled.run().stdout
    assert "its results are reused, with no model call" in text and "carried from the last review" in text
    assert ruled.model.calls == []


def test_a_change_re_reviews_only_the_changed_file_and_a_fix_closes_its_finding(lab: Lab) -> None:
    lab.model.cite_lines.add("lets a missing principal through")  # a fix is only claimed for cited lines
    lab.write({"src/auth.py": AUTH_BUGGY, "src/cache.py": CACHE_BUGGY})
    lab.run("--fail-on", "p1")
    lab.forget_calls()

    lab.write({"src/auth.py": AUTH_FIXED})
    wire = lab.wire("--fail-on", "p1")
    assert lab.model.paths() == ["src/auth.py"]  # cache.py was not shown to the model again
    states = {row["title"]: row["state"] for row in wire["findings"]}
    assert states == {"lets a missing principal through": "fixed", "grows without a bound": "carried"}
    assert wire["exit"]["code"] == 0 and wire["verdict"]["p1"] == 0  # the fixed P1 no longer stops --fail-on p1


def test_fresh_ignores_the_carried_result(lab: Lab) -> None:
    lab.write({"src/auth.py": AUTH_BUGGY})
    lab.run()
    lab.forget_calls()
    lab.run("--fresh")
    assert lab.model.paths() == ["src/auth.py"]


def test_a_changed_review_policy_re_reviews_everything_once(lab: Lab) -> None:
    lab.write({"src/auth.py": AUTH_BUGGY})
    lab.run()
    lab.forget_calls()
    assert lab.run("--depth", "thorough").exit_code == 0  # the depth is part of what makes a result reusable
    assert lab.model.calls


def test_a_range_is_continued_from_the_last_push_of_the_same_branch(lab: Lab) -> None:
    git(lab.repo, "checkout", "-qb", "feature")
    lab.write({"src/auth.py": AUTH_BUGGY})
    lab.commit("buggy auth")
    lab.run("main..HEAD")
    lab.forget_calls()
    lab.write({"src/cache.py": CACHE_BUGGY})
    lab.commit("buggy cache")
    lab.run("main..HEAD")
    assert lab.model.paths() == ["src/cache.py"]  # auth.py is the same commit's content: carried
    lab.forget_calls()
    lab.run("main..HEAD")
    assert lab.model.calls == []


def test_the_result_is_kept_in_the_store_in_the_pull_request_bots_format(lab: Lab) -> None:
    lab.write({"src/auth.py": AUTH_BUGGY})
    lab.run()
    (state_file,) = list((lab.store / local.STATE_DIR).rglob("*.state"))
    assert state_file.stat().st_mode & 0o777 == 0o600
    state = parse_state(state_file.read_text(encoding="utf-8"))
    assert state is not None and state.complete and state.reviews == 1 and state.reviewed_units
    assert [finding.title for finding in state.findings] == ["lets a missing principal through"]
    assert state.pull_request == "local:working tree against HEAD" and len(state.head_sha) == 40


def test_a_corrupt_carried_state_costs_a_review_never_a_wrong_answer(lab: Lab) -> None:
    lab.write({"src/auth.py": AUTH_BUGGY})
    lab.run()
    (state_file,) = list((lab.store / local.STATE_DIR).rglob("*.state"))
    state_file.write_text("<!-- lemoncrow:state v1\n{not json\n-->", encoding="utf-8")
    lab.forget_calls()
    assert lab.run().exit_code == 0 and lab.model.paths() == ["src/auth.py"]


def test_the_dollar_and_run_budgets_stop_a_scope_from_spending_more(lab: Lab) -> None:
    lab.write({"src/auth.py": AUTH_BUGGY})
    lab.run("--max-runs-per-pr", "1")
    lab.forget_calls()
    lab.write({"src/cache.py": CACHE_BUGGY})
    soft = lab.run("--max-runs-per-pr", "1")
    assert lab.model.calls == []  # budget reached: nothing new is reviewed
    assert soft.exit_code == 0 and "Review budget reached: 1 review used of 1 allowed" in soft.stdout
    assert "lets a missing principal through" in soft.stdout  # the earlier findings stand
    hard = lab.run("--max-runs-per-pr", "1", "--strict")
    assert hard.exit_code == 2 and "Review budget reached: 1 review used of 1 allowed" in hard.stdout
    lab.run("--max-usd-per-pr", "0.0000001")
    assert lab.model.calls == []  # the first review's cost is known (a model in the price table): the cap holds


# --------------------------------------------------------------------------- #
# what a terminal is shown
# --------------------------------------------------------------------------- #


def test_model_and_file_text_cannot_inject_terminal_escapes() -> None:
    hostile = BotFinding(
        id="f1",
        fingerprint="fp-1",
        severity="p1",
        category="security",
        state="new",
        path="src/\x1b]0;owned\x07evil.py",
        line=3,
        end_line=3,
        title="\x1b[2Jtitle\x1b[31m",
        explanation="why\x00 \x9b31m",
        consequence="so what\x1b[0m",
    )
    report = local.LocalReport(scope_label="working tree against HEAD", findings=(hostile,))
    report.verdict = verdict([hostile], complete=True)
    text = local.render_text(report)
    assert "\x1b" not in text and "\x00" not in text and "\x9b" not in text and "\x07" not in text
    assert "title" in text and "evil.py" in text


def test_color_is_off_for_no_color_and_for_a_pipe(ruled: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    ruled.write({"src/auth.py": AUTH_BUGGY})
    assert "\x1b" not in ruled.run().stdout  # CliRunner is not a terminal
    assert "\x1b" not in ruled.run("--no-color").stdout
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b" not in ruled.run().stdout


# --------------------------------------------------------------------------- #
# the exit decision, alone
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("p0", "p1", "incomplete", "fail_on", "strict", "code"),
    [
        (0, 0, False, "p0", False, 0),
        (1, 0, False, "p0", False, 1),
        (0, 1, False, "p0", False, 0),
        (0, 1, False, "p1", False, 1),
        (1, 1, False, "none", False, 0),
        (0, 0, True, "p0", False, 0),
        (0, 0, True, "p0", True, 2),
        (0, 0, True, "none", True, 2),
        (1, 0, True, "p0", True, 1),
        (0, 1, True, "p0", True, 2),
        (0, 0, False, "p0", True, 0),
    ],
)
def test_exit_decision_table(p0: int, p1: int, incomplete: bool, fail_on: str, strict: bool, code: int) -> None:
    got, reason = local.exit_decision(p0=p0, p1=p1, incomplete=incomplete, reason="why", fail_on=fail_on, strict=strict)
    assert got == code and bool(reason) == bool(code)


def test_the_environment_a_hook_inherits_is_not_changed_by_a_review(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    before = dict(os.environ)
    lab.write({"src/plain.py": "VALUE = 2\n"})
    lab.run()
    assert dict(os.environ) == before
