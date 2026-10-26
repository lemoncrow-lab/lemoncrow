"""The ``benchmark_summary`` of a golden-score report: there when the scoring is publishable, absent (with the reason) when not.

Every report here comes out of the real command over a tiny invented answer key (``golden_support``) with a stand-in judge,
and the round-trip tests hand the written file to the real importer (``scripts/render_code_review_benchmark.py``) on a
temporary copy of the published file whose dataset is made to match that tiny corpus and whose LemonCrow block is reset to
pending, so the tests hold whether the committed file is pending or published. Nothing is real: the comments, the costs
and the commits are invented, and no tool is named (the published file's own first row stands in for "another tool").
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow import __version__
from lemoncrow.gateway.cli.developer_tools import developer_tools
from lemoncrow.pro.capabilities.review.evaluation import golden_judge
from lemoncrow.pro.capabilities.review.evaluation.benchmark_report import BASIS
from lemoncrow.pro.capabilities.review.evaluation.benchmark_summary import (
    CompetitorRow,
    build_benchmark_summary,
)
from lemoncrow.pro.capabilities.review.evaluation.golden_judge import (
    ClaudeCliJudge,
    JudgeRequest,
    JudgeUsage,
    RawAnswer,
)
from scripts import render_code_review_benchmark as bench
from tests.gateway.golden_support import make_key, write_corpus

ROOT = Path(__file__).resolve().parents[2]
ENGINE_COMMIT = "1234567890abcdef1234567890abcdef12345678"
#: A comment whose text holds the needle is the same issue as the first golden comment of its pull request.
NEEDLES = ("stale", "flag", "retries")


@pytest.fixture(autouse=True)
def stand_in_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real fixed judge (identity, cache) with its model call replaced by a needle match."""

    def ask(self: ClaudeCliJudge, request: JudgeRequest) -> RawAnswer:
        found = tuple(
            (request.comment_id(index), "G1", "same defect")
            for index, text in enumerate(request.comments)
            if any(needle in text for needle in NEEDLES)
        )
        return RawAnswer(found, JudgeUsage(calls=1, input_tokens=10, output_tokens=2, usd=0.01, seconds=0.5), "model-x")

    monkeypatch.setattr(ClaudeCliJudge, "__call__", ask)


# --------------------------------------------------------------------------- #
# a tiny complete run, through the real command
# --------------------------------------------------------------------------- #


def finding(severity: str, title: str) -> dict[str, Any]:
    return {
        "id": f"{severity}-{title}",
        "severity": severity,
        "title": title,
        "explanation": "why",
        "consequence": "so",
        "path": "a.py",
        "lines": [4, 6],
    }


def review(findings: list[dict[str, Any]], calls: int, usd: float, **extra: Any) -> dict[str, Any]:
    return {
        "status": "ran",
        "complete": True,
        "findings": findings,
        "model_calls": calls,
        "input_tokens": 100,
        "output_tokens": 10,
        "provider_usd": usd,
        "wall_seconds": 5.0,
        "capture_seconds": 1.0,
        **extra,
    }


def benchmark_report(**overrides: Any) -> dict[str, Any]:
    """Three reviewed pull requests: 3 caught, 2 noise comments and the Critical golden comment missed."""

    runs = {
        "alpha-01": review([finding("p0", "cache is stale"), finding("p2", "naming")], calls=2, usd=0.05),
        "alpha-02": review([finding("p1", "old flag still read")], calls=3, usd=0.10),
        "beta-01": review([finding("p1", "retries are unbounded"), finding("p2", "a nit")], calls=4, usd=0.20),
    }
    report: dict[str, Any] = {
        "kind": "review-benchmark",
        "run": {
            "mode": "live",
            "basis": BASIS["live"],
            "driver": "route-x",
            "model": "review-model-x",
            "code": {"git_head": ENGINE_COMMIT},
        },
        "prs": [{"id": pr_id, "skipped": "", "depths": {"auto": run}} for pr_id, run in runs.items()],
    }
    report.update(overrides)
    return report


class Setup:
    """An answer key, a corpus that pins it and a way to run ``golden-score`` against them."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.key_dir, self.commit = make_key(tmp_path)
        self.corpus = write_corpus(tmp_path, self.commit)
        self.out = tmp_path / "out"
        self.count = 0

    def run(self, *args: str, exit_code: int = 0) -> tuple[dict[str, Any], str, Any]:
        """Run the command; the JSON report, its Markdown and the click result."""

        self.count += 1
        name = f"report-{self.count}"
        result = CliRunner().invoke(
            developer_tools,
            [
                "eval",
                "review",
                "golden-score",
                "--corpus",
                str(self.corpus),
                "--golden-dir",
                str(self.key_dir),
                "--judge-cache",
                str(self.tmp_path / "judge-cache"),
                "--out-dir",
                str(self.out),
                "--name",
                name,
                *args,
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == exit_code, result.output
        if exit_code == 2:
            return {}, "", result
        return (
            json.loads((self.out / f"{name}.json").read_text(encoding="utf-8")),
            (self.out / f"{name}.md").read_text(encoding="utf-8"),
            result,
        )

    def lemoncrow(self, report: dict[str, Any] | None = None, *args: str, exit_code: int = 0) -> Any:
        path = self.tmp_path / "benchmark.json"
        path.write_text(json.dumps(report or benchmark_report()), encoding="utf-8")
        return self.run("--report", str(path), *args, exit_code=exit_code)

    def comments(self, *args: str, files: dict[str, list[str]] | None = None, exit_code: int = 0) -> Any:
        """Another tool's saved comments, one file per pull request; one (3 caught, 3 noise, 1 missed) by default."""

        folder = self.tmp_path / "comments"
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir()
        files = files or {
            "alpha-01": ["the cache is stale after a plan change", "naming nit", "another nit"],
            "alpha-02": ["the old flag is read in one place", "the flag docs repeat it"],
            "beta-01": ["retries never stop"],
        }
        for pr_id, texts in files.items():
            body = {
                "comments": [
                    {"id": f"c{i}", "path": "a.py", "lines": "3", "text": text} for i, text in enumerate(texts)
                ]
            }
            (folder / f"{pr_id}.json").write_text(json.dumps(body), encoding="utf-8")
        return self.run("--reviewer-comments", str(folder), *args, exit_code=exit_code)


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return Setup(tmp_path)


def other_tool() -> tuple[str, str]:
    """The id and name of a ``third_party`` row of the published file: the importer wants one that exists."""

    row = bench.load(ROOT)["third_party"][0]
    return row["id"], row["tool"]


def tool_options() -> list[str]:
    tool_id, tool = other_tool()
    return [
        "--tool-id",
        tool_id,
        "--tool-name",
        tool,
        "--comments-description",
        "its posted comments, read from the pull request pages",
        "--comments-url",
        "https://example.invalid/where-they-are",
        "--comments-collected-on",
        "2031-01-02",
    ]


# --------------------------------------------------------------------------- #
# the importer, on a temporary copy of the published file
# --------------------------------------------------------------------------- #


def published_copy(tmp_path: Path, setup: Setup) -> Path:
    """A temporary repository root: the published file with the dataset made to match the tiny corpus, and a pending run.

    Three pull requests in two repositories; six golden comments, two of them Low, so four are scored. The committed
    file may already carry a real 50-pull-request run, which does not fit this dataset: the copy gets LemonCrow and the
    rescored row reset to pending and the README section emptied, and only the file and the README are copied, so no
    raw run report of the checkout comes along.
    """

    root = tmp_path / "repo"
    for relative in (bench.DATA_RELPATH, bench.README_RELPATH):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, root / relative)
    data = copy.deepcopy(bench.load(root))
    data["lemoncrow"] = {"status": "pending"}
    data["competitor_rescored"] = None
    spots = [data["dataset"]["origin"], data["dataset"]["answer_key"], data["method"]["source"]]
    for spot in (*spots, *data["third_party"], *data["vendor_claims"]):
        spot["verification"]["status"] = "primary_read"  # as it must be before anything is published
    dataset = data["dataset"]
    dataset["pr_count"] = 3
    dataset["languages"] = ["Python", "Go"]
    dataset["repositories"] = [
        {"name": "Alpha", "language": "Python", "prs": 2},
        {"name": "Beta", "language": "Go", "prs": 1},
    ]
    dataset["answer_key"].update(commit=setup.commit, golden_comments=6, low_severity=2, scored_comments=4)
    (root / bench.DATA_RELPATH).write_text(bench.dump(data), encoding="utf-8")
    readme = root / bench.README_RELPATH
    readme.write_text(bench.splice(readme.read_text(encoding="utf-8"), ""), encoding="utf-8")
    assert bench.problems(bench.load(root), root) == [] and not (root / bench.RUNS_RELPATH).exists()
    assert bench.check(root) == []
    return root


def imported(root: Path, report: Path, rescored: str | None = None) -> dict[str, Any]:
    """Merge the report as ``--from-run`` does, with the problems checked before and after the report is copied in."""

    merged, target, raw = bench.merge_run(bench.load(root), report, rescored, root)
    assert bench.problems(merged, None) == []
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    assert bench.problems(merged, root) == []
    # and the whole of `make benchmark-publish RUN=...`: merge, write, render
    assert (
        bench.main(["--root", str(root), "--from-run", str(report), *(["--rescored", rescored] if rescored else [])])
        == 0
    )
    assert bench.check(root) == []
    return merged


# --------------------------------------------------------------------------- #
# LemonCrow's own reviews
# --------------------------------------------------------------------------- #


def test_a_complete_live_run_gets_a_summary_the_importer_accepts(tmp_path: Path, setup: Setup) -> None:
    report, markdown, _ = setup.lemoncrow()
    summary = report["benchmark_summary"]
    assert "benchmark_summary_omitted" not in report
    assert set(summary) == bench.SUMMARY_FIELDS["lemoncrow"]
    assert summary["prs_reviewed"] == 3 and summary["answer_key_commit"] == setup.commit
    assert summary["date"] == report["generated_at"][:10]
    # 3 caught of 5 comments scored (precision 60), 3 of the 4 counted golden comments (recall 75), F = 2*3 / (2*3 + 2 + 1)
    assert (summary["precision"], summary["recall"], summary["f_score"]) == (60.0, 75.0, 66.67)
    assert summary["counts"] == {
        "tool_comments_scored": 5,
        "tool_comments_matched": 3,
        "golden_scored": 4,
        "golden_matched": 3,
    }
    assert summary["judge"] == {"model": golden_judge.JUDGE_MODEL, "prompt_sha256": golden_judge.full_prompt_digest()}
    assert summary["review"] == {"model": "review-model-x", "route": "route-x", "depth": "auto"}
    assert summary["engine"] == {"version": __version__, "commit": ENGINE_COMMIT}
    assert summary["cost"] == {"median_usd_per_pr": 0.1, "total_usd": 0.35, "median_calls_per_pr": 3, "total_calls": 9}
    assert "Ready to publish: yes" in markdown and "benchmark-publish" in markdown

    root = published_copy(tmp_path, setup)
    merged = imported(root, setup.out / "report-1.json")
    assert merged["lemoncrow"]["status"] == "published" and merged["lemoncrow"]["precision"] == 60.0
    published = bench.load(root)["lemoncrow"]
    assert published["f_score"] == 66.67 and published["run_report"]["url"].endswith(
        f"lemoncrow-{summary['date']}.json"
    )


def test_the_report_names_the_full_judge_prompt_digest_whose_start_is_the_short_one(setup: Setup) -> None:
    report, _, _ = setup.lemoncrow()
    judge = report["judge"]
    assert len(judge["prompt_sha256"]) == 64 and judge["prompt_sha256"].startswith(judge["prompt_digest"])
    assert judge["prompt_sha256"] == golden_judge.full_prompt_digest()
    assert golden_judge.prompt_digest() == golden_judge.full_prompt_digest()[:16]


def test_a_review_that_did_not_complete_is_partial_and_gets_no_summary(setup: Setup) -> None:
    runs = benchmark_report()
    runs["prs"][1]["depths"]["auto"]["complete"] = False
    runs["prs"][1]["depths"]["auto"]["reason"] = "Partially reviewed"
    report, markdown, result = setup.lemoncrow(runs)
    assert "benchmark_summary" not in report
    assert "1 review(s) were partial (alpha-02)" in report["benchmark_summary_omitted"]
    assert "Ready to publish: no. No `benchmark_summary` was written because 1 review(s) were partial" in markdown
    assert "ready to publish: nothing yet (1 review(s) were partial" in result.output


def test_a_pull_request_that_could_not_be_scored_keeps_the_summary_out(setup: Setup) -> None:
    runs = benchmark_report()
    runs["prs"][2]["depths"]["auto"] = {"status": "error", "reason": "capture failed", "errors": ["capture failed"]}
    report, _, _ = setup.lemoncrow(runs, exit_code=3)
    assert "benchmark_summary" not in report
    assert "1 of the corpus's 3 pull requests could not be scored (beta-01)" in report["benchmark_summary_omitted"]


def test_a_run_limited_to_some_pull_requests_is_not_the_benchmark(setup: Setup) -> None:
    report, _, _ = setup.lemoncrow(None, "--pr", "alpha-01", "--pr", "alpha-02")
    assert report["scored_prs"] == 2 and "benchmark_summary" not in report
    assert "limited to 2 pull request(s) with --pr" in report["benchmark_summary_omitted"]


def test_a_report_that_covers_fewer_pull_requests_than_the_corpus_keeps_the_summary_out(setup: Setup) -> None:
    runs = benchmark_report()
    del runs["prs"][2]
    report, _, _ = setup.lemoncrow(runs)
    assert report["corpus"]["prs"] == 3 and report["scored_prs"] == 2 and "benchmark_summary" not in report
    assert "only 2 of the corpus's 3 pull requests were scored" in report["benchmark_summary_omitted"]


@pytest.mark.parametrize("mode", ["replay", "fake"])
def test_a_replayed_or_scripted_review_gets_no_summary(setup: Setup, mode: str) -> None:
    runs = benchmark_report()
    runs["run"].update(mode=mode, basis=BASIS[mode])
    report, markdown, _ = setup.lemoncrow(runs)
    assert "benchmark_summary" not in report
    assert "the reviews were not a live provider run" in report["benchmark_summary_omitted"]
    assert BASIS[mode] in report["benchmark_summary_omitted"] and "Ready to publish: no" in markdown


def test_a_review_with_an_unknown_cost_gets_no_summary(setup: Setup) -> None:
    runs = benchmark_report()
    runs["prs"][0]["depths"]["auto"]["provider_usd"] = None  # the provider did not report it; it is not zero
    report, _, _ = setup.lemoncrow(runs)
    assert report["cost"]["usd"]["unknown_prs"] == 1 and "benchmark_summary" not in report
    assert "cost of some reviews is unknown" in report["benchmark_summary_omitted"]


def test_a_non_default_finding_threshold_gets_no_summary(setup: Setup) -> None:
    report, _, _ = setup.lemoncrow(None, "--finding-threshold", "high")
    assert "benchmark_summary" not in report and "finding threshold 'high'" in report["benchmark_summary_omitted"]


def test_a_benchmark_report_that_did_not_record_its_commit_gets_no_summary(setup: Setup) -> None:
    runs = benchmark_report()
    runs["run"]["code"] = {"git_head": ""}
    report, _, _ = setup.lemoncrow(runs)
    assert "benchmark_summary" not in report and "40-hex git commit" in report["benchmark_summary_omitted"]


@pytest.mark.parametrize("dirty", [True, False])
def test_a_benchmark_report_from_a_checkout_with_uncommitted_changes_gets_no_summary(setup: Setup, dirty: bool) -> None:
    runs = benchmark_report()
    runs["run"]["code"] = {"git_head": ENGINE_COMMIT, "dirty": dirty}
    report, markdown, _ = setup.lemoncrow(runs)
    assert report["reviewer"]["benchmark_code"] == {"git_head": ENGINE_COMMIT, "dirty": dirty}
    if dirty:
        assert "benchmark_summary" not in report and "Ready to publish: no" in markdown
        assert "uncommitted changes to tracked files" in report["benchmark_summary_omitted"]
    else:  # a clean tree is accepted, as is a report without the field
        assert report["benchmark_summary"]["engine"]["commit"] == ENGINE_COMMIT


def test_the_review_model_and_route_are_recorded_in_the_reviewer_block(setup: Setup) -> None:
    report, _, _ = setup.lemoncrow()
    assert (report["reviewer"]["benchmark_driver"], report["reviewer"]["benchmark_model"]) == (
        "route-x",
        "review-model-x",
    )


def test_competitor_options_with_a_lemoncrow_report_are_a_usage_error(setup: Setup) -> None:
    _, _, result = setup.lemoncrow(None, *tool_options(), exit_code=2)
    assert "use --reviewer-comments" in result.output


# --------------------------------------------------------------------------- #
# the builder on a complete report with one thing taken away
# --------------------------------------------------------------------------- #


def drop(path: str) -> Any:
    def mutate(report: dict[str, Any]) -> None:
        *parents, last = path.split(".")
        node = report
        for part in parents:
            node = node[part]
        del node[last]

    return mutate


def put(path: str, value: Any) -> Any:
    def mutate(report: dict[str, Any]) -> None:
        *parents, last = path.split(".")
        node = report
        for part in parents:
            node = node[part]
        node[last] = value

    return mutate


def both(first: Any, second: Any) -> Any:
    def mutate(report: dict[str, Any]) -> None:
        first(report)
        second(report)

    return mutate


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (put("judge.driver", "fake"), "was not the fixed judge"),
        (put("judge.model", "other"), "was not the fixed judge"),
        (drop("judge.prompt_sha256"), "full sha256 of the judge prompt"),
        (put("judge.prompt_sha256", "f" * 64), "does not match the report's own short digest"),
        (drop("reviewer.benchmark_model"), "does not name the review's model, route and depth"),
        (drop("reviewer.benchmark_driver"), "does not name the review's model, route and depth"),
        (put("reviewer.benchmark_basis", ""), "the report does not say what they were"),
        (put("cost", None), "no cost of the reviews"),
        (put("cost.model_calls.unknown_prs", 1), "cost of some reviews is unknown"),
        (put("cost.prs", 2), "cost of some reviews is unknown"),
        (put("cost.usd.median", None), "cost figures are not all numbers"),
        (put("reviewer.benchmark_code.dirty", True), "uncommitted changes to tracked files"),
        (put("key.commit", "abc"), "40-hex commit"),
        (put("generated_at", "yesterday"), "no generated_at date"),
        (put("overall.golden_counted", 9), "are not its 3 caught plus 1 missed"),
        (put("overall.strict", {"tp": 0, "fp": 0, "fn": 4}), "precision is undefined"),
        (
            both(put("overall.strict", {"tp": 0, "fp": 2, "fn": 0}), put("overall.golden_counted", 0)),
            "no golden comment",
        ),
        (put("reviewer.kind", "other"), "has no published block"),
    ],
)
def test_one_missing_value_keeps_the_summary_out_and_the_reason_names_it(
    setup: Setup, mutate: Any, reason: str
) -> None:
    report, _, _ = setup.lemoncrow()
    assert build_benchmark_summary(report, engine_version=__version__).summary == report["benchmark_summary"]
    mutate(report)
    before = copy.deepcopy(report)
    outcome = build_benchmark_summary(report, engine_version=__version__)
    assert outcome.summary is None and reason in outcome.why_not
    assert report == before  # the builder reads the report; it changes nothing


def test_an_empty_engine_version_keeps_the_summary_out(setup: Setup) -> None:
    report, _, _ = setup.lemoncrow()
    assert "engine version is unknown" in build_benchmark_summary(report, engine_version=" ").why_not


def test_a_report_with_no_comments_scored_has_no_precision_to_publish(setup: Setup) -> None:
    runs = benchmark_report()
    for item in runs["prs"]:
        item["depths"]["auto"]["findings"] = []
    report, _, _ = setup.lemoncrow(runs)
    assert report["overall"]["strict"]["precision"] is None
    assert "precision is undefined" in report["benchmark_summary_omitted"]


# --------------------------------------------------------------------------- #
# another tool's comments, scored by the same judge
# --------------------------------------------------------------------------- #


def test_another_tools_comments_with_the_options_get_a_summary_the_importer_accepts(
    tmp_path: Path, setup: Setup
) -> None:
    report, markdown, _ = setup.comments(*tool_options())
    summary = report["benchmark_summary"]
    tool_id, tool = other_tool()
    assert set(summary) == bench.SUMMARY_FIELDS["competitor_rescored"]
    assert (summary["tool_id"], summary["tool"]) == (tool_id, tool)
    assert summary["comments_source"] == {
        "description": "its posted comments, read from the pull request pages",
        "url": "https://example.invalid/where-they-are",
        "collected_on": "2031-01-02",
    }
    # 3 caught of 6 comments scored (the repeated flag comment is a strict false positive), 3 of 4 golden comments
    assert (summary["precision"], summary["recall"], summary["f_score"]) == (50.0, 75.0, 60.0)
    assert summary["counts"] == {
        "tool_comments_scored": 6,
        "tool_comments_matched": 3,
        "golden_scored": 4,
        "golden_matched": 3,
    }
    assert "review" not in summary and "engine" not in summary and "cost" not in summary
    assert report["cost"] is None and "Ready to publish: yes" in markdown and f"RESCORED={tool_id}" in markdown

    root = published_copy(tmp_path, setup)
    merged = imported(root, setup.out / "report-1.json", rescored=tool_id)
    assert merged["competitor_rescored"]["status"] == "published" and merged["competitor_rescored"]["tool"] == tool
    assert bench.load(root)["competitor_rescored"]["run_report"]["url"].endswith(
        f"rescored-{tool_id}-{summary['date']}.json"
    )


@pytest.mark.parametrize(
    "given",
    [
        [],
        ["--tool-id", "some-tool"],
        ["--tool-id", "some-tool", "--tool-name", "Some Tool", "--comments-description", "a page"],
    ],
)
def test_another_tools_comments_without_all_the_options_get_no_summary_and_the_reason_names_them(
    setup: Setup, given: list[str]
) -> None:
    report, markdown, result = setup.comments(*given)
    assert "benchmark_summary" not in report
    reason = report["benchmark_summary_omitted"]
    if given:
        assert "is missing" in reason and "--comments-collected-on" in reason
    else:
        assert "needs its name, id and the source of its comments" in reason and "--tool-id" in reason
    assert "Ready to publish: no" in markdown and "ready to publish: nothing yet" in result.output
    assert report["overall"]["strict"]["tp"] == 3  # the scoring itself is unchanged


def test_another_tools_comments_that_were_only_partly_read_get_no_summary(setup: Setup) -> None:
    setup.comments()
    truncated = setup.tmp_path / "comments" / "beta-01.json"
    body = json.loads(truncated.read_text(encoding="utf-8"))
    body["extraction"] = {"page_truncated": True, "stated_comments": 4}
    truncated.write_text(json.dumps(body), encoding="utf-8")
    report, _, _ = setup.run("--reviewer-comments", str(setup.tmp_path / "comments"), *tool_options())
    assert report["partial_reviews"] == ["beta-01"] and "benchmark_summary" not in report
    assert "1 review(s) were partial (beta-01)" in report["benchmark_summary_omitted"]


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("--tool-id", "Some Tool", "lowercase slug"),
        ("--comments-url", "http://example.invalid/x", "https URL"),
        ("--comments-collected-on", "02/01/2031", "YYYY-MM-DD"),
        ("--comments-collected-on", "2031-02-30", "YYYY-MM-DD"),
    ],
)
def test_a_malformed_competitor_option_stops_the_command_before_any_scoring(
    setup: Setup, option: str, value: str, message: str
) -> None:
    (setup.tmp_path / "comments").mkdir()
    _, _, result = setup.run(
        "--reviewer-comments", str(setup.tmp_path / "comments"), *tool_options(), option, value, exit_code=2
    )
    assert message in result.output and not (setup.tmp_path / "judge-cache").exists()


def test_the_competitor_row_says_which_options_are_missing_and_which_are_wrong() -> None:
    assert CompetitorRow().given() is False and len(CompetitorRow().missing()) == 5
    row = CompetitorRow(tool_id="Bad Id", url="http://x", collected_on="soon")
    assert row.given() and len(row.malformed()) == 3 and "--tool-name" in row.missing()
    assert CompetitorRow("a-b", "A B", "text", "https://x.invalid/p", "2031-01-02").malformed() == []


def test_a_competitor_report_given_to_the_builder_without_a_row_is_left_out(setup: Setup) -> None:
    report, _, _ = setup.comments(*tool_options())
    assert build_benchmark_summary(report, engine_version="1").summary is None
    assert build_benchmark_summary(report, engine_version="1", competitor=CompetitorRow()).summary is None
    row = CompetitorRow(
        *other_tool(),
        "its posted comments, read from the pull request pages",
        "https://example.invalid/where-they-are",
        "2031-01-02",
    )
    assert build_benchmark_summary(report, engine_version="1", competitor=row).summary == report["benchmark_summary"]
