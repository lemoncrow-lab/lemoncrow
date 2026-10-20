"""Scoring and the baseline-review importer: how findings are matched to labels and to another reviewer's comments.

Pure functions and fixtures only; no git, no model. The inputs are small enough that the right answer is visible.
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.evaluation.baseline_reviews import (
    BaselineError,
    export_comments,
    fetch_pull_comments,
    load_comments,
    parse_comments,
)
from lemoncrow.pro.capabilities.review.evaluation.real_prs import Label, LabelEvidence, Region
from lemoncrow.pro.capabilities.review.evaluation.scoring import (
    Candidate,
    compare_reviewers,
    match_labels,
    percentile,
    similarity,
    tokens,
    wilson,
)
from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError


def label(label_id: str = "bug", path: str = "src/a.py", lines: tuple[int, int] = (50, 52), *symbols: str) -> Label:
    return Label(
        id=label_id,
        summary="Fix the bug",
        regions=(Region(path, lines[0], lines[1]),),
        evidence=LabelEvidence(fix_commit="f" * 40),
        symbols=tuple(symbols),
    )


def finding(fid: str, path: str, start: int, end: int, text: str = "", title: str = "") -> Candidate:
    return Candidate(fid, path, start, end, text, "p1", title)


# --------------------------------------------------------------------------- #
# catching a label
# --------------------------------------------------------------------------- #


def test_a_finding_catches_a_label_by_location_within_the_tolerance() -> None:
    score = match_labels(
        [label()],
        [finding("near", "src/a.py", 54, 60), finding("far", "src/a.py", 70, 80)],
        mode="either",
        tolerance=3,
        pr_paths={"src/a.py"},
    )
    assert [(m.candidate.id, m.rule, m.gap) for m in score.matches] == [("near", "location", 2)]
    assert [c.id for c in score.unmatched] == ["far"] and score.missed == ()


def test_the_tolerance_is_exact_and_a_label_may_set_its_own() -> None:
    edge = finding("edge", "src/a.py", 56, 58)  # 4 lines past the label's last line (52)
    assert match_labels([label()], [edge], mode="location", tolerance=3, pr_paths=set()).caught == 0
    assert match_labels([label()], [edge], mode="location", tolerance=4, pr_paths=set()).caught == 1
    wide = Label("bug", "x", (Region("src/a.py", 50, 52),), LabelEvidence("f" * 40), line_tolerance=10)
    assert match_labels([wide], [edge], mode="location", tolerance=0, pr_paths=set()).caught == 1


def test_a_finding_in_another_file_never_catches_by_location() -> None:
    score = match_labels([label()], [finding("x", "src/b.py", 50, 52)], mode="location", tolerance=3, pr_paths=set())
    assert score.caught == 0 and score.missed[0].id == "bug"


def test_a_finding_that_names_the_edited_symbol_catches_a_bug_outside_the_changed_lines() -> None:
    elsewhere = finding(
        "sym", "src/other.py", 5, 9, text="callers still pass two arguments", title="quote() needs region now"
    )
    unrelated = finding("noise", "src/other.py", 1, 3, text="naming", title="rename x")
    changed = {"src/other.py", "src/a.py"}
    either = match_labels(
        [label("bug", "src/a.py", (500, 500), "quote")],
        [unrelated, elsewhere],
        mode="either",
        tolerance=3,
        pr_paths=changed,
    )
    assert [(m.candidate.id, m.rule) for m in either.matches] == [("sym", "symbol")]
    assert [c.id for c in either.unmatched] == ["noise"]
    # The strict reading, and the file the pull request never touched, both refuse it.
    assert (
        match_labels(
            [label("bug", "src/a.py", (500, 500), "quote")], [elsewhere], mode="location", tolerance=3, pr_paths=changed
        ).caught
        == 0
    )
    assert (
        match_labels(
            [label("bug", "src/a.py", (500, 500), "quote")],
            [elsewhere],
            mode="symbol",
            tolerance=3,
            pr_paths={"src/a.py"},
        ).caught
        == 0
    )


def test_a_symbol_is_a_whole_identifier_not_a_fragment_of_one() -> None:
    lab = label("bug", "src/a.py", (500, 500), "get_all")
    inside = finding("a", "src/x.py", 1, 2, text="the budget_get_all_total helper is wrong")
    whole = finding("b", "src/x.py", 3, 4, text="`Context.get_all()` returns the internal dict")
    score = match_labels([lab], [inside, whole], mode="symbol", tolerance=3, pr_paths={"src/x.py"})
    assert [m.candidate.id for m in score.matches] == ["b"]


def test_assignment_is_one_to_one_and_a_repeat_of_a_caught_bug_is_counted_as_one() -> None:
    twice = [finding("first", "src/a.py", 49, 53), finding("second", "src/a.py", 50, 52)]
    score = match_labels([label()], twice, mode="either", tolerance=3, pr_paths={"src/a.py"})
    assert score.caught == 1 and len(score.unmatched) == 1
    assert score.duplicates == (score.unmatched[0].id,)  # it would have caught the label, had the other not
    assert (
        match_labels(
            [label()], [finding("z", "src/a.py", 90, 91)], mode="either", tolerance=3, pr_paths=set()
        ).duplicates
        == ()
    )


def test_two_labels_and_two_findings_pair_up_without_stealing() -> None:
    labels = [label("one", "src/a.py", (10, 10)), label("two", "src/a.py", (12, 12))]
    findings = [finding("x", "src/a.py", 11, 11), finding("y", "src/a.py", 13, 13)]
    score = match_labels(labels, findings, mode="location", tolerance=3, pr_paths=set())
    assert score.caught == 2 and score.missed == () and score.unmatched == ()


def test_the_same_inputs_always_give_the_same_pairs() -> None:
    labels = [label("one", "src/a.py", (10, 10)), label("two", "src/a.py", (11, 11))]
    findings = [finding(name, "src/a.py", 10, 11) for name in ("c", "a", "b")]
    runs = {
        tuple(
            (m.label.id, m.candidate.id)
            for m in match_labels(labels, order, mode="location", tolerance=3, pr_paths=set()).matches
        )
        for order in (findings, list(reversed(findings)), sorted(findings, key=lambda c: c.id))
    }
    assert len(runs) == 1


def test_a_missed_label_lists_same_file_findings_as_near_misses_for_a_person_to_settle() -> None:
    score = match_labels([label()], [finding("far", "src/a.py", 90, 95)], mode="either", tolerance=3, pr_paths=set())
    assert score.near_misses == (("bug", "far"),)


# --------------------------------------------------------------------------- #
# text similarity and agreement with another reviewer
# --------------------------------------------------------------------------- #


def test_similarity_sees_shared_identifiers_and_ignores_filler() -> None:
    ours = "`previous_defs` holds Line objects now, but `previous_defs[-1]` is still used as an int depth"
    theirs = "The `previous_defs` list now stores Line objects; `previous_defs[-1]` here is compared as an integer."
    other = "Consider adding a docstring and renaming this variable for readability."
    assert similarity(ours, theirs) > 0.3 > similarity(ours, other)
    assert similarity("", theirs) == 0.0 and similarity(ours, ours) == pytest.approx(1.0)
    assert {"previous_defs", "previous", "defs"} <= tokens("previous_defs")
    assert "the" not in tokens("the previous_defs")


def test_agreement_pairs_same_file_nearby_similar_comments_and_lists_the_rest() -> None:
    ours = [
        finding(
            "a",
            "src/x.py",
            10,
            14,
            "get_all returns the internal dict, not a copy, so mutating it corrupts the context",
            "get_all no longer copies",
        ),
        finding("b", "src/x.py", 40, 41, "variable shadows a builtin", "shadowing"),
        finding("c", "src/y.py", 5, 6, "unused import", "unused import"),
    ]
    theirs = [
        Candidate(
            "t1",
            "src/x.py",
            12,
            12,
            "`get_all()` no longer returns a copy; callers that mutate the result corrupt the context.",
        ),
        Candidate("t2", "src/x.py", 42, 42, "Nice use of a helper here."),  # close to b, but about something else
        Candidate("t3", "src/z.py", 7, 7, "this loop is quadratic"),
        Candidate("t4", "", 0, 0, "Overall this looks good (summary)"),  # no file or line: nothing to put it beside
    ]
    agreement = compare_reviewers(ours, theirs, line_window=8, threshold=0.2)
    assert [(p.finding.id, p.comment.id) for p in agreement.agreed] == [("a", "t1")]
    assert sorted(c.id for c in agreement.ours_only) == ["b", "c"]
    assert sorted(c.id for c in agreement.theirs_only) == ["t2", "t3"]  # the summary has no line and is not compared
    assert [(p.finding.id, p.comment.id) for p in agreement.near_misses] == [("b", "t2")]  # unpaired and nearby
    wire = agreement.to_wire()
    assert wire["line_window"] == 8 and wire["agreed"][0]["similarity"] >= 0.2
    json.dumps(wire)


def test_the_window_and_the_threshold_are_the_callers_to_set() -> None:
    ours = [finding("a", "src/x.py", 10, 10, "get_all returns the internal dict", "get_all")]
    theirs = [Candidate("t", "src/x.py", 40, 40, "get_all returns the internal dict")]
    assert compare_reviewers(ours, theirs, line_window=8).agreed == ()
    assert len(compare_reviewers(ours, theirs, line_window=30).agreed) == 1
    assert compare_reviewers(ours, theirs, line_window=30, threshold=1.01).agreed == ()


# --------------------------------------------------------------------------- #
# small statistics
# --------------------------------------------------------------------------- #


def test_the_wilson_interval_is_wide_for_few_trials_and_never_claims_certainty() -> None:
    assert wilson(0, 0) is None
    low, high = wilson(3, 3) or (0, 0)
    assert 0.4 < low < 0.5 and high == 1.0  # three for three still leaves a wide interval
    low, high = wilson(60, 100) or (0, 0)
    assert 0.5 < low < 0.6 < high < 0.7
    assert (
        percentile([], 0.9) is None and percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5 and percentile([5.0], 0.9) == 5.0
    )


# --------------------------------------------------------------------------- #
# the importer
# --------------------------------------------------------------------------- #

BOT = {"login": "review-bot[bot]", "type": "Bot"}
BOT_LOGINS = {"review-bot[bot]"}
HUMAN = {"login": "alice", "type": "User"}


def rest_comment(cid: int, user: dict[str, str], body: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": cid,
        "user": user,
        "body": body,
        "path": "src/x.py",
        "line": 12,
        "start_line": 10,
        "html_url": f"https://github.com/acme/shop/pull/7#discussion_r{cid}",
        "created_at": "2026-10-01T00:00:00Z",
        **extra,
    }


def test_the_importer_keeps_the_baseline_reviewers_root_comments_with_their_lines() -> None:
    payload = [
        rest_comment(1, BOT, "**P1** `get_all()` returns the internal dict"),
        rest_comment(2, BOT, "a reply in the thread", in_reply_to_id=1),
        rest_comment(3, HUMAN, "I disagree"),
        rest_comment(4, BOT, "outdated comment", line=None, original_line=30, start_line=None),
        rest_comment(5, BOT, "   "),  # empty: nothing to compare
    ]
    comments = parse_comments(payload, logins=BOT_LOGINS)
    assert [c.id for c in comments] == ["1", "4"]
    first = comments[0]
    assert (first.path, first.line, first.end_line, first.kind, first.severity) == ("src/x.py", 10, 12, "inline", "P1")
    assert (comments[1].line, comments[1].end_line) == (30, 30)  # an outdated comment keeps its original line
    assert first.candidate().start_line == 10 and first.candidate().title.startswith("**P1**")


def test_a_comment_with_no_file_and_line_is_a_summary_and_is_kept_not_matched() -> None:
    summary = {"id": 9, "user": BOT, "body": "Confidence score: 4/5", "html_url": "u"}
    (comment,) = parse_comments([summary], logins=BOT_LOGINS, default_kind="summary")
    assert comment.kind == "summary" and comment.line == 0 and comment.path == ""


def test_a_wrong_login_is_an_error_naming_the_authors_seen_never_an_empty_baseline() -> None:
    with pytest.raises(BaselineError, match=r"authors seen: alice \(1\), other\[bot\] \(2\)"):
        parse_comments(
            [
                rest_comment(1, HUMAN, "x"),
                rest_comment(2, {"login": "other[bot]"}, "y"),
                rest_comment(3, {"login": "other[bot]"}, "z"),
            ],
            logins=BOT_LOGINS,
        )
    assert (
        len(parse_comments([rest_comment(1, {"login": "my-review-bot[bot]"}, "y")], logins={"my-review-bot[bot]"})) == 1
    )
    assert len(parse_comments([rest_comment(1, HUMAN, "y")], logins=None)) == 1


def test_an_export_round_trips_and_keeps_a_persons_verdict(tmp_path: Path) -> None:
    comments = parse_comments([rest_comment(1, BOT, "`get_all()` returns the internal dict")], logins=BOT_LOGINS)
    export = export_comments(comments, source="github:acme/shop#7")
    export["comments"][0]["verdict"] = "valid"
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(export), encoding="utf-8")
    (again,) = load_comments(path, logins=None)
    assert again.verdict == "valid" and again.body == comments[0].body and export["schema"] == 1
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema": 99, "comments": []}', encoding="utf-8")
    with pytest.raises(BaselineError, match="unsupported export schema"):
        load_comments(bad, logins=None)
    bad.write_text("not json", encoding="utf-8")
    with pytest.raises(BaselineError):
        load_comments(bad, logins=None)


def test_fetching_reads_the_three_lists_through_gh() -> None:
    asked: list[str] = []
    pages = {
        "pulls/7/comments": [rest_comment(1, BOT, "inline finding"), rest_comment(2, HUMAN, "human")],
        "issues/7/comments": [{"id": 3, "user": BOT, "body": "Summary: confidence 4/5", "html_url": "u"}],
        "pulls/7/reviews": [{"id": 4, "user": BOT, "body": "", "html_url": "u"}],
    }

    def runner(args: Any, stdin: str | None) -> str:
        endpoint = args[-1]
        asked.append(endpoint)
        key = next(name for name in pages if endpoint.endswith(name))
        return json.dumps([pages[key]])

    comments = fetch_pull_comments("acme/shop", 7, logins=BOT_LOGINS, runner=runner)
    assert sorted(c.id for c in comments) == ["1", "3"]
    assert {c.kind for c in comments} == {"inline", "summary"}
    assert asked == [
        "repos/acme/shop/pulls/7/comments",
        "repos/acme/shop/issues/7/comments",
        "repos/acme/shop/pulls/7/reviews",
    ]


def test_fetching_a_public_pull_request_falls_back_to_plain_https_when_gh_cannot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def __init__(self, body: Any, link: str = "") -> None:
            self._body = json.dumps(body).encode()
            self.headers = {"Link": link}

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return self._body

    seen: list[str] = []

    def opener(request: Any) -> Response:
        seen.append(request.full_url)
        if request.full_url.endswith("/pulls/7/comments?per_page=100"):
            return Response([rest_comment(1, BOT, "page one")], '<https://api.github.com/page2>; rel="next"')
        if request.full_url == "https://api.github.com/page2":
            return Response([rest_comment(2, BOT, "page two")])
        return Response([])

    import lemoncrow.pro.capabilities.review.evaluation.baseline_reviews as module

    def no_gh(args: Any, stdin: str | None = None) -> str:
        raise GitHubCliError("`gh` is not signed in; run `gh auth login`")

    monkeypatch.setattr(module, "run_gh", no_gh)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    comments = fetch_pull_comments("acme/shop", 7, logins=BOT_LOGINS, opener=opener)
    assert sorted(c.id for c in comments) == ["1", "2"]
    assert seen[1] == "https://api.github.com/page2" and len(seen) == 4


def test_a_github_error_is_reported_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    import lemoncrow.pro.capabilities.review.evaluation.baseline_reviews as module

    monkeypatch.setattr(module, "run_gh", lambda args, stdin=None: (_ for _ in ()).throw(GitHubCliError("no gh")))

    def refuse(request: Any) -> Any:
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    with pytest.raises(BaselineError, match="404"):
        fetch_pull_comments("acme/private", 7, logins=BOT_LOGINS, opener=refuse)


def test_no_reviewer_login_is_assumed() -> None:
    """The bot's login is the one thing only the caller knows, so none of the entry points defaults it."""

    import inspect

    from lemoncrow.pro.capabilities.review.evaluation.benchmark import BenchmarkOptions
    from lemoncrow.pro.capabilities.review.evaluation.corpus_init import init_corpus

    for function in (parse_comments, load_comments, fetch_pull_comments):
        assert inspect.signature(function).parameters["logins"].default is inspect.Parameter.empty, function
    assert inspect.signature(init_corpus).parameters["logins"].default == ()
    assert inspect.signature(BenchmarkOptions).parameters["reviewer_logins"].default == ()


# --------------------------------------------------------------------------- #
# the first run: a customer's pull request URLs -> a corpus with the other reviewer's comments beside each
# --------------------------------------------------------------------------- #


def fake_github(
    pulls: dict[int, dict[str, Any]], comments: dict[int, list[dict[str, Any]]], *, no_compare: bool = False
) -> Any:
    """The slice of ``gh api`` that init-corpus uses, answering from the dicts above."""

    def runner(args: Any, stdin: str | None = None) -> str:
        endpoint = args[-1]  # repos/OWNER/REPO/<kind>/N[/comments|/reviews] or repos/OWNER/REPO/compare/A...B
        parts = endpoint.split("/")
        if parts[3] == "compare":
            if no_compare:
                raise GitHubCliError("HTTP 404")
            return json.dumps({"merge_base_commit": {"sha": "b" * 40}})
        number = int(parts[4])
        if len(parts) == 5:  # the pull request itself
            return json.dumps(pulls[number])
        if parts[3] == "pulls" and parts[5] == "comments":
            return json.dumps([comments.get(number, [])])
        return json.dumps([[]])  # conversation comments and reviews: none

    return runner


def test_init_corpus_writes_an_organization_corpus_with_the_baseline_reviewers_comments(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.evaluation.corpus_init import init_corpus
    from lemoncrow.pro.capabilities.review.evaluation.real_prs import load_real_pr_corpus

    pulls = {
        7: {
            "title": "Add retries",
            "state": "closed",
            "merged": True,
            "head": {"sha": "a" * 40},
            "base": {"sha": "c" * 40},
        },
        9: {"title": "Fix cache", "state": "open", "head": {"sha": "d" * 40}, "base": {"sha": "e" * 40}},
    }
    comments = {7: [rest_comment(1, BOT, "**P1** retries never stop"), rest_comment(2, HUMAN, "lgtm")]}
    out = tmp_path / "private" / "acme.yaml"
    result = init_corpus(
        fake_github(pulls, comments),
        ["https://github.com/acme/shop/pull/7", "acme/shop#9", "acme/shop#7"],
        out=out,
        corpus_id="acme-prs",
        organization="acme",
        approved_by="Pat",
        reference="ticket-1",
        approved_at="2026-10-20",
        logins=BOT_LOGINS,
    )
    assert result.entries == ("shop-7", "shop-9") and "listed twice" in " ".join(result.notes)
    assert [path.name for path in result.comment_files] == ["shop-7.json", "shop-9.json"]
    assert (
        "shop-7: 1 inline and 0 summary comments" in result.notes
        and "shop-9: 0 inline and 0 summary comments" in result.notes
    )

    corpus = load_real_pr_corpus(out, organization_opt_in="acme")
    first, second = corpus.prs
    assert corpus.visibility == "organization" and corpus.opt_in is not None and corpus.opt_in.approved_by == "Pat"
    assert (first.head_sha, first.base_sha, first.labelled) == (
        "a" * 40,
        "b" * 40,
        False,
    )  # the merge base, not the base tip
    assert first.baseline is not None and first.baseline.comments_file == "baseline/shop-7.json"
    assert second.baseline is not None and second.baseline.comments_file == "baseline/shop-9.json"  # silence is data
    (exported,) = parse_comments(
        json.loads((out.parent / "baseline/shop-7.json").read_text(encoding="utf-8")), logins=None
    )
    assert exported.body == "**P1** retries never stop"  # the human's reply was not kept


def test_init_corpus_refuses_to_guess_who_approved_a_customers_data(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.evaluation.corpus_init import InitError, init_corpus, parse_spec

    runner = fake_github({}, {})
    for kwargs in ({"organization": ""}, {"approved_by": ""}, {"reference": ""}):
        args: dict[str, Any] = {"organization": "acme", "approved_by": "Pat", "reference": "t", **kwargs}
        with pytest.raises(InitError, match="--organization, --approved-by and --reference"):
            init_corpus(
                runner, ["acme/shop#1"], out=tmp_path / "c.yaml", corpus_id="x", approved_at="2026-10-20", **args
            )
    with pytest.raises(InitError, match="at least one pull request"):
        init_corpus(
            runner,
            [],
            out=tmp_path / "c.yaml",
            corpus_id="x",
            organization="a",
            approved_by="b",
            reference="c",
            approved_at="d",
        )
    with pytest.raises(InitError, match="not a pull request URL"):
        parse_spec("https://example.com/not-a-pr")


def test_init_corpus_notes_a_missing_merge_base_and_a_wrong_reviewer_login(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.evaluation.corpus_init import init_corpus

    pulls = {3: {"title": "T", "state": "open", "head": {"sha": "a" * 40}, "base": {"sha": "c" * 40}}}
    comments = {3: [rest_comment(1, {"login": "someone-else[bot]"}, "x")]}
    result = init_corpus(
        fake_github(pulls, comments, no_compare=True),
        ["acme/shop#3"],
        out=tmp_path / "c.yaml",
        corpus_id="x",
        organization="acme",
        approved_by="Pat",
        reference="t",
        approved_at="2026-10-20",
        logins=BOT_LOGINS,
    )
    text = " ".join(result.notes)
    assert "no merge base from GitHub" in text and "authors seen: someone-else[bot] (1)" in text
    assert result.comment_files == ()


def test_init_corpus_needs_the_reviewers_login_to_fetch_comments_and_nothing_to_skip_them(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.evaluation.corpus_init import InitError, init_corpus

    pulls = {3: {"title": "T", "state": "open", "head": {"sha": "a" * 40}, "base": {"sha": "c" * 40}}}
    common: dict[str, Any] = {
        "out": tmp_path / "c.yaml",
        "corpus_id": "x",
        "organization": "acme",
        "approved_by": "Pat",
        "reference": "t",
        "approved_at": "2026-10-20",
    }
    with pytest.raises(InitError, match="--reviewer-login"):
        init_corpus(fake_github(pulls, {}), ["acme/shop#3"], **common)
    result = init_corpus(fake_github(pulls, {}), ["acme/shop#3"], fetch_comments=False, **common)
    assert result.comment_files == () and result.entries == ("shop-3",)


def test_the_init_command_keeps_a_customers_corpus_out_of_the_checkout() -> None:
    from click.testing import CliRunner

    from lemoncrow.gateway.cli.developer_tools import developer_tools

    inside = Path(__file__).resolve().parents[2] / "evals/review/customer.yaml"
    result = CliRunner().invoke(
        developer_tools,
        ["eval", "review", "init-corpus", "--pr", "acme/shop#1", "--out", str(inside), "--organization", "acme",
         "--approved-by", "Pat", "--reference", "t"],
    )  # fmt: skip
    assert result.exit_code != 0 and "must stay outside" in result.output and not inside.exists()
