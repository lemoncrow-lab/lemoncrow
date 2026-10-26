"""Scoring a reviewer's comments against an answer key: the matching rules, the totals, the judge's cache and the command.

The judge is a fake everywhere except in one test that runs the real :class:`ClaudeCliJudge` against a stand-in
``claude`` executable (which checks the flags it is started with). Nothing here calls a model.
"""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli.developer_tools import developer_tools
from lemoncrow.pro.capabilities.review.evaluation import golden_judge
from lemoncrow.pro.capabilities.review.evaluation.golden_comments import (
    GoldenComment,
    load_golden_corpus,
    open_golden_key,
)
from lemoncrow.pro.capabilities.review.evaluation.golden_judge import (
    CachedJudge,
    ClaudeCliJudge,
    JudgeError,
    JudgeRequest,
    JudgeUsage,
    JudgeVerdict,
    RawAnswer,
    check_pairs,
    request_digest,
)
from lemoncrow.pro.capabilities.review.evaluation.golden_scoring import (
    PostedComment,
    PrUsage,
    Submission,
    build_report,
    comments_from_findings,
    load_reviewer_comments,
    metrics,
    render_markdown,
    resolve,
    score_corpus,
    score_pr,
    submissions_from_benchmark_report,
)
from tests.gateway.golden_support import make_key, write_corpus

REPO_ROOT = Path(__file__).resolve().parents[2]


def golden(*items: tuple[str, str]) -> list[GoldenComment]:
    return [GoldenComment(f"G{i}", text, severity) for i, (text, severity) in enumerate(items, start=1)]


def ids(count: int) -> list[str]:
    return [f"C{i}" for i in range(1, count + 1)]


def pairs(*items: tuple[str, str]) -> list[tuple[str, str, str]]:
    return [(c, g, "same defect") for c, g in items]


class FakeJudge:
    """Says a comment matches the golden comments listed for the first needle its text contains."""

    identity: Mapping[str, str] = {"driver": "fake", "model": "fake", "prompt_version": "t", "prompt_digest": "t"}

    def __init__(self, matches: Mapping[str, Sequence[str]] | None = None, fail: bool = False) -> None:
        self.matches = dict(matches or {})
        self.fail = fail
        self.requests: list[JudgeRequest] = []

    def __call__(self, request: JudgeRequest) -> JudgeVerdict:
        self.requests.append(request)
        if self.fail:
            raise JudgeError("the judge is down")
        found: list[tuple[str, str, str]] = []
        for index, text in enumerate(request.comments):
            for needle, targets in self.matches.items():
                if needle in text:
                    found += [(request.comment_id(index), target, "same defect") for target in targets]
        return JudgeVerdict(
            tuple(found),
            JudgeUsage(calls=1, input_tokens=100, output_tokens=10, usd=0.02, seconds=2.0),
            False,
            "d" * 64,
            "claude-a, claude-b",
        )


# --------------------------------------------------------------------------- #
# the one-to-one assignment and the counts
# --------------------------------------------------------------------------- #


def test_a_matched_comment_is_a_true_positive_an_unmatched_one_a_false_positive_a_missed_golden_a_false_negative() -> (
    None
):
    goldens = golden(("a", "High"), ("b", "Critical"), ("c", "Medium"))
    result = resolve(pairs(("C1", "G1")), ids(2), goldens)
    assert (result.tp, result.fp, result.fn) == (1, 1, 2)
    assert [v.outcome for v in result.comments] == ["tp", "fp"]
    assert [(o.id, o.status) for o in result.goldens] == [("G1", "caught"), ("G2", "missed"), ("G3", "missed")]


def test_a_comment_that_matches_two_golden_comments_counts_once() -> None:
    result = resolve(pairs(("C1", "G1"), ("C1", "G2")), ids(1), golden(("a", "High"), ("b", "High")))
    assert (result.tp, result.fp, result.fn) == (1, 0, 1)  # one catch; the other golden comment is still missed


def test_the_most_severe_golden_comment_is_taken_first() -> None:
    result = resolve(pairs(("C1", "G1"), ("C1", "G2")), ids(1), golden(("a", "Medium"), ("b", "Critical")))
    assert [(o.id, o.status) for o in result.goldens] == [("G1", "missed"), ("G2", "caught")]


def test_two_comments_on_the_same_issue_are_one_catch_and_one_false_positive_strictly_and_two_catches_leniently() -> (
    None
):
    result = resolve(pairs(("C1", "G1"), ("C2", "G1")), ids(2), golden(("a", "High")))
    assert (result.tp, result.fp, result.fn) == (1, 1, 0)
    assert result.comments[1].outcome == "fp" and result.comments[1].duplicate_of == "G1"
    assert (result.lenient_tp, result.lenient_fp) == (2, 0)


def test_an_assignment_that_needs_to_move_an_earlier_comment_is_found() -> None:
    # C1 could take G1 or G2; C2 can only take G1. The maximum matching gives C1 -> G2 and C2 -> G1.
    result = resolve(pairs(("C1", "G1"), ("C1", "G2"), ("C2", "G1")), ids(2), golden(("a", "High"), ("b", "High")))
    assert (result.tp, result.fp, result.fn) == (2, 0, 0)


def test_a_low_severity_golden_comment_is_neither_a_false_negative_nor_a_catch() -> None:
    goldens = golden(("a", "High"), ("typo", "Low"))
    result = resolve([], ids(0), goldens)
    assert (result.tp, result.fp, result.fn, result.neutral_goldens) == (0, 0, 1, 1)  # only G1 can be missed
    assert [o.status for o in result.goldens] == ["missed", "excluded"]


def test_a_comment_that_matches_only_a_low_golden_comment_is_neither_a_true_nor_a_false_positive() -> None:
    result = resolve(pairs(("C1", "G2")), ids(1), golden(("a", "High"), ("typo", "Low")))
    assert (result.tp, result.fp, result.fn, result.neutral_comments) == (0, 0, 1, 1)
    assert result.comments[0].outcome == "neutral"
    assert result.goldens[1].status == "excluded" and result.goldens[1].by == ""


def test_a_comment_that_matches_a_low_and_a_taken_counted_golden_comment_is_neutral() -> None:
    result = resolve(pairs(("C1", "G1"), ("C2", "G1"), ("C2", "G2")), ids(2), golden(("a", "High"), ("typo", "Low")))
    assert [v.outcome for v in result.comments] == ["tp", "neutral"]


def test_a_pair_naming_an_unknown_comment_or_golden_is_ignored() -> None:
    result = resolve(pairs(("C9", "G1"), ("C1", "G9")), ids(1), golden(("a", "High")))
    assert (result.tp, result.fp, result.fn) == (0, 1, 1)
    kept, dropped = check_pairs(
        [("c1", "g1", " ok "), ("C1", "G1", "again"), ("C5", "G1", ""), ("C1", "G7", "")],
        JudgeRequest(("x",), (("a", "High"),)),
    )
    assert kept == (("C1", "G1", "ok"),) and dropped == 3  # ids are case-insensitive; a repeat counts as dropped


def test_precision_recall_and_f_follow_the_definitions() -> None:
    m = metrics(2, 1, 2)
    assert m["precision"] == pytest.approx(2 / 3, abs=1e-4) and m["recall"] == 0.5
    assert m["f"] == pytest.approx(2 * (2 / 3) * 0.5 / ((2 / 3) + 0.5), abs=1e-4)
    assert metrics(0, 0, 3)["precision"] is None and metrics(0, 0, 3)["recall"] == 0.0 and metrics(0, 0, 3)["f"] == 0.0
    assert metrics(0, 0, 0) == {"tp": 0, "fp": 0, "fn": 0, "precision": None, "recall": None, "f": None}


# --------------------------------------------------------------------------- #
# one pull request, and the report
# --------------------------------------------------------------------------- #


def submission(*texts: str, **kwargs: Any) -> Submission:
    return Submission(comments=tuple(PostedComment(f"f{i}", text) for i, text in enumerate(texts, start=1)), **kwargs)


def test_a_review_with_no_comments_needs_no_judge_and_misses_every_counted_golden_comment() -> None:
    judge = FakeJudge()
    score = score_pr("p", "r/r", "go", submission(), golden(("a", "High"), ("b", "Low"), ("c", "Medium")), judge)
    assert judge.requests == [] and score.resolution is not None
    assert (score.resolution.tp, score.resolution.fp, score.resolution.fn) == (0, 0, 2)


def test_the_judge_is_asked_once_per_pull_request_with_every_comment_and_the_whole_golden_set() -> None:
    judge = FakeJudge({"cache": ["G1"]})
    comment = PostedComment("f1", "cache is stale", path="a.py", line=7)
    score_pr(
        "p",
        "r/r",
        "go",
        Submission(comments=(comment, PostedComment("f2", "other"))),
        golden(("a", "High"), ("b", "Low")),
        judge,
    )
    (request,) = judge.requests
    assert request.comments == ("[a.py:7] cache is stale", "other")
    assert request.goldens == (("a", "High"), ("b", "Low"))
    assert "C2" in request.render() and "G2" in request.render()


def test_a_judge_that_fails_leaves_the_pull_request_unscored_not_scored_as_zero() -> None:
    score = score_pr("p", "r/r", "go", submission("x"), golden(("a", "High")), FakeJudge(fail=True))
    assert score.resolution is None and "the judge is down" in score.error


def test_a_review_that_did_not_run_is_not_scored() -> None:
    score = score_pr(
        "p",
        "r/r",
        "go",
        Submission(reviewed=False, note="the review did not run: boom"),
        golden(("a", "High")),
        FakeJudge(),
    )
    assert score.resolution is None and score.error.endswith("boom")


def test_numbering_other_than_g1_in_order_is_refused() -> None:
    with pytest.raises(ValueError, match="numbered G1"):
        score_pr("p", "r/r", "go", submission("x"), [GoldenComment("G2", "a", "High")], FakeJudge())


def test_the_report_sums_counts_over_pull_requests_by_language_and_leaves_unscored_ones_out(tmp_path: Path) -> None:
    key_dir, commit = make_key(tmp_path)
    corpus = load_golden_corpus(write_corpus(tmp_path, commit))
    key = open_golden_key(corpus.source, key_dir, cache_root=tmp_path / "cache")
    judge = FakeJudge({"stale": ["G1"], "units": ["G2"], "retry": ["G1"], "nit": ["G3"]})
    usage = PrUsage(model_calls=3, input_tokens=1000, output_tokens=100, usd=0.5, wall_seconds=30.0)
    submissions = {
        "alpha-01": submission(
            "cache is stale",
            "wrong units",
            "noise one",
            "a nit",
            usage=usage,
            complete=False,
            note="Partially reviewed",
        ),
        "alpha-02": Submission(reviewed=False, note="the review did not run: timeout"),
        "beta-01": submission(
            "retry forever",
            usage=PrUsage(model_calls=1, input_tokens=None, output_tokens=None, usd=None, wall_seconds=10.0),
        ),
    }
    scores = score_corpus(corpus, key, submissions, judge)
    report = build_report(corpus, scores, judge, name="t", reviewer={"label": "fake", "description": "a fake"})
    overall = report["overall"]["strict"]
    # alpha-01: G1 and G2 caught, "noise one" is a false positive, "a nit" matches the Low G3 (neutral); beta-01: caught.
    assert (overall["tp"], overall["fp"], overall["fn"]) == (3, 1, 0)
    assert report["overall"]["neutral_comments"] == 1 and report["overall"]["golden_excluded"] == 2
    assert report["by_language"]["python"]["strict"]["tp"] == 2 and report["by_language"]["go"]["strict"]["tp"] == 1
    assert report["scored_prs"] == 2 and report["unscored"] == [
        {"id": "alpha-02", "reason": "the review did not run: timeout"}
    ]
    assert report["partial_reviews"] == ["alpha-01"]
    assert report["recall_by_severity"]["Critical"] == {"golden": 1, "caught": 1, "recall": 1.0}
    cost = report["cost"]
    assert cost["usd"]["total"] == 0.5 and cost["usd"]["unknown_prs"] == 1  # unknown is not zero
    assert cost["model_calls"]["total"] == 4 and cost["wall_seconds"]["total"] == 40.0
    assert report["judge"]["questions"] == 2 and report["judge"]["cost_now"]["calls"] == 2
    assert report["judge"]["model_reported"] == [
        "claude-a",
        "claude-b",
    ]  # one model per entry, however the CLI listed them
    markdown = render_markdown(report)
    assert "| **All** | 2 | 3 | 1 | 0 |" in markdown and "alpha-02" in markdown and "not scored" in markdown
    json.dumps(report)  # plain data


# --------------------------------------------------------------------------- #
# where the comments come from
# --------------------------------------------------------------------------- #


def finding(severity: str, title: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": f"{severity}-{title}",
        "severity": severity,
        "title": title,
        "explanation": "why",
        "consequence": "so",
        "path": "a.py",
        "lines": [4, 6],
        **extra,
    }


def test_a_finding_is_posted_unless_the_threshold_holds_it_back() -> None:
    rows = [finding("p0", "zero"), finding("p1", "one"), finding("p2", "two")]
    posted, held = comments_from_findings(rows)
    assert [c.severity for c in posted] == ["p0", "p1", "p2"] and held == 0  # P0 and P1 inline, P2 in the summary list
    assert posted[0].text == "zero\nwhy\nso" and (posted[0].path, posted[0].line) == ("a.py", 4)
    posted, held = comments_from_findings(rows, threshold="high")
    assert [c.severity for c in posted] == ["p0", "p1"] and held == 1
    posted, held = comments_from_findings(rows, threshold="critical")
    assert [c.severity for c in posted] == ["p0"] and held == 2


def benchmark_report(**runs: Any) -> dict[str, Any]:
    return {
        "kind": "review-benchmark",
        "run": {"driver": "claude", "model": "sonnet", "basis": "a live provider run", "code": {"git_head": "abc"}},
        "prs": [{"id": pr_id, "skipped": "", "depths": {"auto": run}} for pr_id, run in runs.items()],
    }


def test_a_benchmark_report_gives_each_pull_request_its_posted_findings_and_what_the_review_cost() -> None:
    run = {
        "status": "ran",
        "complete": True,
        "reason": "",
        "findings": [finding("p1", "x")],
        "model_calls": 2,
        "input_tokens": 900,
        "output_tokens": 90,
        "provider_usd": 0.31,
        "wall_seconds": 20.0,
        "capture_seconds": 2.5,
    }
    failed = {"status": "error", "reason": "capture failed", "errors": ["capture failed: no"], "findings": []}
    report = benchmark_report(**{"alpha-01": run, "alpha-02": failed})
    got = submissions_from_benchmark_report(report, "auto", ["alpha-01", "alpha-02", "beta-01"])
    assert got["alpha-01"].usage == PrUsage(2, 900, 90, 0.31, 22.5) and len(got["alpha-01"].comments) == 1
    assert not got["alpha-02"].reviewed and "capture failed" in got["alpha-02"].note
    assert not got["beta-01"].reviewed and got["beta-01"].note == "not in the report"
    assert not submissions_from_benchmark_report(report, "deep", ["alpha-01"])["alpha-01"].reviewed
    with pytest.raises(ValueError, match="review-benchmark"):
        submissions_from_benchmark_report({"kind": "other"}, "auto", [])


def test_another_reviewers_comment_files_are_read_and_a_missing_file_is_an_unreviewed_pull_request(
    tmp_path: Path,
) -> None:
    (tmp_path / "alpha-01.json").write_text(
        json.dumps(
            {
                "extraction": {"page_truncated": True, "stated_comments": 4},
                "comments": [{"id": "c1", "path": "a.py", "lines": "+5 to +9", "text": "t"}, {"text": "u"}],
            }
        ),
        encoding="utf-8",
    )
    got = load_reviewer_comments(tmp_path, ["alpha-01", "alpha-02"])
    assert [(c.path, c.line, c.text) for c in got["alpha-01"].comments] == [("a.py", 5, "t"), ("", 0, "u")]
    assert "2 of 4" in got["alpha-01"].note and got["alpha-01"].reviewed
    assert not got["alpha-02"].reviewed


# --------------------------------------------------------------------------- #
# the judge: its cache, its request, the real CLI wrapper
# --------------------------------------------------------------------------- #


def request(*comments: str, goldens: tuple[tuple[str, str], ...] = (("a", "High"),)) -> JudgeRequest:
    return JudgeRequest(tuple(comments), goldens)


def test_a_cached_answer_is_free_and_identical(tmp_path: Path) -> None:
    asked: list[JudgeRequest] = []

    def asker(req: JudgeRequest) -> RawAnswer:
        asked.append(req)
        return RawAnswer(
            (("C1", "G1", "same"),), JudgeUsage(calls=1, input_tokens=50, output_tokens=5, usd=0.01, seconds=1.0), "m"
        )

    judge = CachedJudge(asker, tmp_path)
    first = judge(request("x"))
    again = judge(request("x"))
    assert len(asked) == 1 and not first.cached and again.cached
    assert first.pairs == again.pairs and again.usage == JudgeUsage() and first.usage.usd == 0.01
    assert judge.has(request("x")) and not judge.has(request("y"))


def test_the_cache_key_covers_each_comment_the_golden_set_and_the_severity() -> None:
    base = request_digest(request("x"))
    assert request_digest(request("x")) == base
    assert request_digest(request("y")) != base
    assert request_digest(request("x", "z")) != base
    assert request_digest(request("x", goldens=(("a", "High"), ("b", "Low")))) != base
    assert request_digest(request("x", goldens=(("a", "Low"),))) != base
    assert request_digest(request("x"), model="opus") != base


def test_cached_only_never_asks(tmp_path: Path) -> None:
    def asker(req: JudgeRequest) -> RawAnswer:
        raise AssertionError("must not be asked")

    with pytest.raises(JudgeError, match="cached-only"):
        CachedJudge(asker, tmp_path, cached_only=True)(request("x"))
    unscored = score_pr(
        "p", "r/r", "go", submission("x"), golden(("a", "High")), CachedJudge(asker, tmp_path, cached_only=True)
    )
    assert unscored.resolution is None


def test_an_unreadable_cache_entry_is_asked_again(tmp_path: Path) -> None:
    asks = []

    def asker(req: JudgeRequest) -> RawAnswer:
        asks.append(1)
        return RawAnswer((), JudgeUsage(calls=1))

    judge = CachedJudge(asker, tmp_path)
    judge(request("x"))
    (next(tmp_path.glob("*.json"))).write_text("{not json", encoding="utf-8")
    assert not judge(request("x")).cached and len(asks) == 2


def fake_claude(tmp_path: Path, replies: list[str]) -> tuple[Path, Path]:
    """A stand-in ``claude`` that logs its arguments and working directory and prints the next reply."""

    log = tmp_path / "claude.log"
    state = tmp_path / "claude.count"
    script = tmp_path / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"replies = {replies!r}\n"
        f"count = int(open({str(state)!r}).read()) if os.path.exists({str(state)!r}) else 0\n"
        f"open({str(state)!r}, 'w').write(str(count + 1))\n"
        f"open({str(log)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'cwd': os.getcwd(), 'listing': os.listdir('.')}}) + '\\n')\n"
        "sys.stdout.write(replies[min(count, len(replies) - 1)])\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, log


def claude_result(pairs: list[dict[str, str]]) -> str:
    return json.dumps(
        {
            "type": "result",
            "structured_output": {"pairs": pairs},
            "total_cost_usd": 0.012,
            "usage": {
                "input_tokens": 40,
                "output_tokens": 12,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 0,
            },
            "modelUsage": {"claude-sonnet-x": {}},
        }
    )


def test_the_claude_cli_judge_runs_with_no_tools_no_settings_and_an_empty_directory(tmp_path: Path) -> None:
    executable, log = fake_claude(tmp_path, [claude_result([{"comment": "C1", "golden": "G1", "reason": "same"}])])
    answer = ClaudeCliJudge(executable=str(executable))(
        request("the cache is stale", goldens=(("cache never invalidated", "High"),))
    )
    assert answer.pairs == (("C1", "G1", "same"),)
    assert (
        answer.usage.calls == 1
        and answer.usage.usd == 0.012
        and answer.usage.input_tokens == 140
        and answer.usage.output_tokens == 12
    )
    (entry,) = [json.loads(line) for line in log.read_text().splitlines()]
    argv = entry["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--setting-sources") + 1] == ""
    assert (
        "--strict-mcp-config" in argv
        and "--no-session-persistence" in argv
        and argv[argv.index("--output-format") + 1] == "json"
    )
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema["required"] == ["pairs"]
    prompt = argv[argv.index("-p") + 1]
    assert "same underlying issue" in prompt and "the cache is stale" in prompt and "cache never invalidated" in prompt
    assert entry["listing"] == []  # an empty working directory: the repository under review is not there
    assert entry["cwd"] != str(tmp_path) and "lemoncrow-golden-judge-" in entry["cwd"]


def test_the_claude_cli_judge_asks_again_once_after_an_answer_that_is_not_the_schema(tmp_path: Path) -> None:
    good = claude_result([])
    executable, log = fake_claude(tmp_path, ["this is not json", good])
    answer = ClaudeCliJudge(executable=str(executable))(request("x"))
    assert answer.pairs == () and len(log.read_text().splitlines()) == 2
    (tmp_path / "again").mkdir()
    executable, _ = fake_claude(tmp_path / "again", ["nope", "still nope"])
    with pytest.raises(JudgeError, match="claude CLI judge failed"):
        ClaudeCliJudge(executable=str(executable))(request("x"))


def test_the_judge_without_a_claude_cli_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    with pytest.raises(JudgeError, match="not installed"):
        ClaudeCliJudge()(request("x"))


def test_the_judge_identity_names_the_model_and_the_prompt() -> None:
    judge = CachedJudge(lambda req: RawAnswer((), JudgeUsage()), Path("/nonexistent"))
    assert judge.identity["driver"] == "claude" and judge.identity["model"] == "sonnet"
    assert judge.identity["prompt_version"] == golden_judge.JUDGE_PROMPT_VERSION
    assert len(judge.identity["prompt_digest"]) == 16


# --------------------------------------------------------------------------- #
# the command
# --------------------------------------------------------------------------- #


def run_cli(*args: str) -> Any:
    return CliRunner().invoke(developer_tools, ["eval", "review", "golden-score", *args], catch_exceptions=False)


def test_the_command_scores_a_benchmark_report_and_a_rerun_costs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_dir, commit = make_key(tmp_path)
    corpus_path = write_corpus(tmp_path, commit)
    asks: list[JudgeRequest] = []

    def fake_ask(self: ClaudeCliJudge, req: JudgeRequest) -> RawAnswer:
        asks.append(req)
        found = tuple((f"C{i + 1}", "G1", "same") for i, text in enumerate(req.comments) if "stale" in text)
        return RawAnswer(
            found, JudgeUsage(calls=1, input_tokens=10, output_tokens=2, usd=0.01, seconds=0.5), "claude-sonnet-x"
        )

    monkeypatch.setattr(ClaudeCliJudge, "__call__", fake_ask)
    run = {
        "status": "ran",
        "complete": True,
        "findings": [finding("p0", "cache is stale"), finding("p2", "naming")],
        "model_calls": 2,
        "input_tokens": 100,
        "output_tokens": 10,
        "provider_usd": 0.05,
        "wall_seconds": 9.0,
        "capture_seconds": 1.0,
    }
    report_path = tmp_path / "bench.json"
    report_path.write_text(json.dumps(benchmark_report(**{"alpha-01": run})), encoding="utf-8")
    common = [
        "--corpus",
        str(corpus_path),
        "--golden-dir",
        str(key_dir),
        "--judge-cache",
        str(tmp_path / "jc"),
        "--report",
        str(report_path),
    ]

    plan = run_cli(*common, "--plan")
    assert plan.exit_code == 0 and "1 judge calls would be made" in plan.output and asks == []

    result = run_cli(*common, "--out-dir", str(tmp_path / "out"), "--name", "r1", "--pr", "alpha-01")
    assert result.exit_code == 0, result.output
    report = json.loads((tmp_path / "out" / "r1.json").read_text())
    assert report["overall"]["strict"] == {"tp": 1, "fp": 1, "fn": 1, "precision": 0.5, "recall": 0.5, "f": 0.5}
    assert (
        report["judge"]["asked_now"] == 1
        and report["reviewer"]["depth"] == "auto"
        and report["key"]["commit"] == commit
    )
    assert (tmp_path / "out" / "r1.md").is_file() and "precision" in result.output
    assert len(asks) == 1

    again = run_cli(
        *common, "--out-dir", str(tmp_path / "out"), "--name", "r2"
    )  # no --pr: the pull requests the report holds
    assert again.exit_code == 0 and len(asks) == 1  # answered from the cache
    rescored = json.loads((tmp_path / "out" / "r2.json").read_text())
    assert rescored["judge"]["answered_from_cache"] == 1 and rescored["judge"]["cost_now"]["calls"] == 0
    assert rescored["overall"] == report["overall"]

    assert "1 judge calls" not in run_cli(*common, "--plan").output  # now cached: nothing to ask


def test_the_command_exits_3_and_lists_what_it_could_not_score(tmp_path: Path) -> None:
    key_dir, commit = make_key(tmp_path)
    corpus_path = write_corpus(tmp_path, commit)
    failed = {"status": "error", "reason": "capture failed", "errors": ["capture failed: no"], "findings": []}
    report_path = tmp_path / "bench.json"
    report_path.write_text(json.dumps(benchmark_report(**{"alpha-01": failed})), encoding="utf-8")
    base = ["--corpus", str(corpus_path), "--golden-dir", str(key_dir), "--judge-cache", str(tmp_path / "jc")]
    result = run_cli(*base, "--report", str(report_path), "--out-dir", str(tmp_path / "out"), "--name", "none")
    assert result.exit_code == 3
    report = json.loads((tmp_path / "out" / "none.json").read_text())
    # Only the pull request the report holds is in play, and a language with nothing scored is not a row of zeros.
    assert report["scored_prs"] == 0 and [item["id"] for item in report["unscored"]] == ["alpha-01"]
    assert report["by_language"] == {}
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps(benchmark_report()), encoding="utf-8")
    nothing = run_cli(*base, "--report", str(empty), "--out-dir", str(tmp_path / "out"))
    assert nothing.exit_code == 1 and "holds none of this corpus" in nothing.output


def test_the_command_needs_exactly_one_source_and_an_out_dir_outside_the_checkout(tmp_path: Path) -> None:
    key_dir, commit = make_key(tmp_path)
    corpus_path = write_corpus(tmp_path, commit)
    base = ["--corpus", str(corpus_path), "--golden-dir", str(key_dir)]
    neither = CliRunner().invoke(developer_tools, ["eval", "review", "golden-score", *base])
    assert neither.exit_code == 2 and "exactly one source" in neither.output
    (tmp_path / "c").mkdir()
    no_out = CliRunner().invoke(
        developer_tools, ["eval", "review", "golden-score", *base, "--reviewer-comments", str(tmp_path / "c")]
    )
    assert no_out.exit_code == 2 and "--out-dir is required" in no_out.output
    inside = CliRunner().invoke(
        developer_tools,
        [
            "eval",
            "review",
            "golden-score",
            *base,
            "--reviewer-comments",
            str(tmp_path / "c"),
            "--out-dir",
            str(REPO_ROOT / "evals" / "review" / "reports"),
        ],
    )
    assert inside.exit_code != 0 and "outside" in inside.output


def test_a_key_at_the_wrong_commit_stops_the_command(tmp_path: Path) -> None:
    key_dir, _commit = make_key(tmp_path)
    corpus_path = write_corpus(tmp_path, "f" * 40)
    (tmp_path / "c").mkdir()
    result = CliRunner().invoke(
        developer_tools,
        [
            "eval",
            "review",
            "golden-score",
            "--corpus",
            str(corpus_path),
            "--golden-dir",
            str(key_dir),
            "--reviewer-comments",
            str(tmp_path / "c"),
            "--out-dir",
            str(tmp_path / "o"),
        ],
    )
    assert result.exit_code == 1 and "pins ffffffffffff" in result.output
