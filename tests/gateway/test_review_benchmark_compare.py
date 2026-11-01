"""The reviewer comparison: raw TP / FP / FN, intervals, a DIY baseline beside LemonCrow, rerun stability, the plan.

Real git (a local origin), the real review pipeline, and the ``fake`` driver. Nothing here calls a model, and nothing it
writes is a result.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from lemoncrow.gateway.cli.developer_tools import developer_tools
from lemoncrow.pro.capabilities.review.evaluation.benchmark import (
    BenchmarkError,
    BenchmarkOptions,
    ScriptedExecutor,
    run_benchmark,
)
from lemoncrow.pro.capabilities.review.evaluation.benchmark_report import build_report, render_markdown
from lemoncrow.pro.capabilities.review.evaluation.diy_review import (
    DIY_SCHEMA,
    TRUNCATION_NOTICE,
    DiyPrompt,
    load_diy_prompt,
    parse_findings,
    user_message,
)
from lemoncrow.pro.capabilities.review.evaluation.real_prs import load_real_pr_corpus
from lemoncrow.pro.capabilities.review.intelligence_runtime import StructuredExecution
from tests.gateway.benchmark_support import corpus_dict, make_origin, write_corpus

PROMPT_FILE = Path(__file__).resolve().parents[2] / "evals" / "review" / "diy_review_prompt.v1.txt"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "LEMONCROW_LLM_BACKEND",
        "LEMONCROW_OPENAI_MODEL",
        "LEMONCROW_REVIEW_REPO_CONTEXT",
        "CI",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", "/nonexistent/sg")


def diy_prompt() -> DiyPrompt:
    return load_diy_prompt(PROMPT_FILE)


class FlakyDiy:
    """The scripted model, except that its third DIY answer (the second run on the first pull request) misses the bug."""

    def __init__(self) -> None:
        self.inner = ScriptedExecutor()
        self.diy_calls = 0

    def __call__(self, **kwargs: Any) -> StructuredExecution:
        execution = self.inner(**kwargs)
        if kwargs.get("output_schema") != DIY_SCHEMA:
            return execution
        self.diy_calls += 1
        if self.diy_calls != 3:
            return execution
        kept = [row for row in execution.result["findings"] if row["file"] != "src/api/pricing.py"]
        return dataclasses.replace(execution, result={"findings": kept})


@pytest.fixture(scope="module")
def shared(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """One two-pull-request run of LemonCrow and the DIY baseline, twice each, with one flipped DIY answer."""

    mp = pytest.MonkeyPatch()
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LEMONCROW_LLM_BACKEND", "LEMONCROW_REVIEW_REPO_CONTEXT"):
        mp.delenv(name, raising=False)
    mp.setenv("LEMONCROW_AST_GREP_BIN", "/nonexistent/sg")
    try:
        root = tmp_path_factory.mktemp("compare")
        origin = make_origin(root)
        corpus = load_real_pr_corpus(write_corpus(root, origin))
        options = BenchmarkOptions(
            depths=("auto",),
            mode="fake",
            workdir=root / "work",
            pushes=False,
            reviewers=("lemoncrow", "diy"),
            repeat=2,
            diy_prompt=diy_prompt(),
        )
        scripted = FlakyDiy()
        results, ledger, facts = run_benchmark(corpus, options, scripted=scripted)  # type: ignore[arg-type]
        report = build_report(
            corpus, options, results, ledger, facts, name="compare", generated_at="2026-10-31T00:00:00+00:00"
        )
    finally:
        mp.undo()
    return {"report": report, "markdown": render_markdown(report), "results": results, "ledger": ledger}


def reviewer(report: dict[str, Any], reviewer_id: str) -> dict[str, Any]:
    return next(row for row in report["comparison"]["reviewers"] if row["id"] == reviewer_id)


def test_lemoncrow_and_the_diy_baseline_sit_side_by_side_with_tp_fp_fn_and_intervals(shared: dict[str, Any]) -> None:
    comparison = shared["report"]["comparison"]
    assert [row["id"] for row in comparison["reviewers"]] == ["lemoncrow:auto", "diy"]
    diy = reviewer(shared["report"], "diy")
    totals = diy["totals"]
    # The bug is found; the other two findings are a docstring remark beside it and one on an unrelated file.
    assert (totals["tp"], totals["fp"], totals["fn"]) == (1, 2, 0)
    assert totals["precision"]["value"] == pytest.approx(1 / 3, abs=1e-3)
    assert totals["recall"]["value"] == 1.0
    assert totals["f1"]["value"] == 0.5
    for rate in (totals["precision"], totals["recall"], totals["f1"]):
        low, high = rate["ci95"]
        assert 0 <= low <= rate["value"] <= high <= 1 and rate["interval"].startswith("wilson")
    lemoncrow = reviewer(shared["report"], "lemoncrow:auto")["totals"]
    assert (lemoncrow["tp"], lemoncrow["fn"]) == (1, 0) and lemoncrow["precision"]["ci95"] is not None


def test_every_true_positive_names_its_label_and_every_false_positive_its_reason(shared: dict[str, Any]) -> None:
    diy = reviewer(shared["report"], "diy")
    run = next(e for e in diy["per_pr"] if e["pr"] == "shop-12")["runs"][0]
    (match,) = run["matches"]
    assert match["label"] == "missing-region" and match["rule"] in {"location", "symbol"}
    assert match["finding"]["path"] == "src/api/pricing.py" and match["label_summary"]
    reasons = {item["reason"]: item for item in run["false_positives"]}
    assert set(reasons) == {"duplicate_of_caught_label", "no_label_in_file"}
    assert (
        reasons["no_label_in_file"]["finding"]["path"] == "src/api/invoice.py" and reasons["no_label_in_file"]["detail"]
    )
    assert diy["false_positive_reasons"] == {"duplicate_of_caught_label": 1, "no_label_in_file": 1}
    unlabelled = next(e for e in diy["per_pr"] if e["pr"] == "shop-14")
    assert "tp" not in unlabelled["runs"][0] and unlabelled["labelled"] is False  # nothing is known there


def test_cost_and_wall_time_per_pull_request_say_who_priced_them(shared: dict[str, Any]) -> None:
    for reviewer_id in ("lemoncrow:auto", "diy"):
        row = reviewer(shared["report"], reviewer_id)
        assert row["cost"]["usd_basis"] == "provider" and row["cost"]["per_pr"]["n"] == 4
        assert row["cost"]["per_pr"]["mean"] > 0 and row["cost"]["unknown_cost_runs"] == 0
        assert row["wall_seconds"]["n"] == 4 and row["wall_seconds"]["mean"] is not None
    diy = reviewer(shared["report"], "diy")
    assert diy["cost"]["calls_per_pr"] == 1.0  # one plain prompt, one call


def test_reruns_report_agreement_flipping_findings_and_the_spread_of_precision_and_recall(
    shared: dict[str, Any],
) -> None:
    diy = reviewer(shared["report"], "diy")
    assert [run["repeat"] for run in diy["runs"]] == [1, 2]
    second = diy["runs"][1]
    assert (second["tp"], second["fp"], second["fn"]) == (0, 1, 1)  # the rerun missed the bug
    stab = diy["stability"]
    assert stab["runs_per_pr"] == 2 and stab["prs_compared"] == 2
    assert stab["mean_jaccard_prs_with_findings"] == pytest.approx(1 / 3, abs=1e-3)  # 1 shared of 3 distinct
    assert stab["mean_jaccard"] == pytest.approx((1 / 3 + 1) / 2, abs=1e-3)
    assert (stab["findings"], stab["stable_findings"], stab["flipping_findings"]) == (3, 1, 2)
    assert (stab["labels_scored"], stab["labels_flipping"]) == (1, 1)
    assert (stab["recall_across_reruns"]["min"], stab["recall_across_reruns"]["max"]) == (0.0, 1.0)
    assert stab["precision_across_reruns"]["n"] == 2
    # LemonCrow's scripted answers do not move: full agreement, nothing flips.
    steady = reviewer(shared["report"], "lemoncrow:auto")["stability"]
    assert steady["mean_jaccard_prs_with_findings"] == 1.0 and steady["flipping_findings"] == 0
    assert steady["labels_flipping"] == 0
    # The first run is the headline; the rerun is kept as raw data.
    assert diy["headline_run"] == 1 and diy["totals"]["tp"] == 1


def test_the_diy_row_is_scorable_by_golden_score_like_any_depth(shared: dict[str, Any]) -> None:
    from lemoncrow.pro.capabilities.review.evaluation.golden_scoring import submissions_from_benchmark_report

    report = shared["report"]
    assert set(report["prs"][0]["depths"]) == {"auto", "diy"}  # `golden-score --report ... --depth diy`
    submissions = submissions_from_benchmark_report(report, "diy", ["shop-12", "shop-14"])
    assert submissions["shop-12"].reviewed and len(submissions["shop-12"].comments) == 3
    assert submissions["shop-14"].reviewed and submissions["shop-14"].comments == ()
    assert submissions["shop-12"].usage.model_calls == 1


def test_the_report_records_how_matching_is_judged_and_the_diy_prompt_verbatim(shared: dict[str, Any]) -> None:
    comparison = shared["report"]["comparison"]
    scoring = comparison["scoring"]
    assert scoring["llm_judge"] is False and scoring["judge_prompt"] is None and scoring["matcher_version"]
    assert scoring["label_match"]["line_tolerance"] == 3 and scoring["label_match"]["text_similarity_used"] is False
    assert scoring["agreement_with_imported_reviewers"]["threshold"] == 0.2
    prompt = comparison["diy_prompt"]
    assert prompt["text"] == PROMPT_FILE.read_text(encoding="utf-8").strip() and len(prompt["sha256"]) == 64
    assert prompt["version"] == "v1" and prompt["file"] == "diy_review_prompt.v1.txt"
    options = shared["report"]["run"]["options"]
    assert options["reviewers"] == ["lemoncrow", "diy"] and options["repeat"] == 2
    assert options["diy_prompt"]["sha256"] == prompt["sha256"]
    meta = comparison["run"]
    assert meta["corpus_digest"] == shared["report"]["corpus"]["digest"] and meta["corpus_id"] == "test-prs"
    assert meta["date"] == "2026-10-31" and meta["model"] and meta["driver"] and meta["depths"] == ["auto"]
    assert meta["reviewers"] == ["lemoncrow", "diy"] and meta["repeat"] == 2 and meta["mode"] == "fake"
    json.dumps(shared["report"])  # plain data


def test_the_markdown_has_the_comparison_table_the_stability_table_and_the_prompt(shared: dict[str, Any]) -> None:
    markdown = shared["markdown"]
    for needle in (
        "## Reviewer comparison",
        "| Reviewer | PRs | TP | FP | FN | Precision (95% CI) |",
        "LemonCrow (auto)",
        "DIY prompt (v1)",
        "### Rerun stability (2 runs per pull request)",
        "`duplicate_of_caught_label`",
        "### How findings are matched",
        "Review this diff. List the bugs you find as file:line: severity: description.",
    ):
        assert needle in markdown, needle


def test_the_diy_reviewer_alone_needs_no_lemoncrow_depths(tmp_path: Path) -> None:
    origin = make_origin(tmp_path)
    corpus = load_real_pr_corpus(write_corpus(tmp_path, origin))
    options = BenchmarkOptions(
        depths=("auto",), mode="fake", workdir=tmp_path / "work", reviewers=("diy",), diy_prompt=diy_prompt()
    )
    results, ledger, facts = run_benchmark(corpus, options)
    report = build_report(corpus, options, results, ledger, facts, name="diy-only")
    assert report["aggregate"] == {} and [row["id"] for row in report["comparison"]["reviewers"]] == ["diy"]
    assert ledger.calls == 2  # one call per pull request, nothing else
    assert render_markdown(report).count("DIY prompt (v1)") >= 1


def test_another_tools_comments_become_a_comparator_row_scored_the_same_way(tmp_path: Path) -> None:
    origin = make_origin(tmp_path)
    comments = {
        "schema": 1,
        "reviewer": "review-bot[bot]",
        "comments": [
            {"id": "11", "author": "review-bot[bot]", "path": "src/api/pricing.py", "line": 6, "end_line": 6, "body": "KeyError on a missing region"},
            {"id": "12", "author": "review-bot[bot]", "path": "src/api/invoice.py", "line": 9, "end_line": 9, "body": "Rename label."},
        ],
    }  # fmt: skip
    (tmp_path / "baseline").mkdir()
    (tmp_path / "baseline/shop-12.json").write_text(json.dumps(comments), encoding="utf-8")
    document = corpus_dict(origin)
    document["prs"][0]["baseline"] = {"comments_file": "baseline/shop-12.json"}
    path = tmp_path / "real_prs.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    corpus = load_real_pr_corpus(path)
    options = BenchmarkOptions(
        depths=("auto",), mode="fake", workdir=tmp_path / "work", pushes=False, reviewers=("lemoncrow", "diy"),
        diy_prompt=diy_prompt(),
    )  # fmt: skip
    results, ledger, facts = run_benchmark(corpus, options)
    report = build_report(corpus, options, results, ledger, facts, name="vs", baseline_usd_per_review=1.0)
    row = reviewer(report, "imported:review-bot[bot]")
    assert (row["totals"]["tp"], row["totals"]["fp"], row["totals"]["fn"]) == (1, 1, 0)
    assert row["cost"]["usd_basis"] == "supplied" and row["cost"]["per_pr"]["mean"] == 1.0
    assert row["wall_seconds"]["n"] == 0
    assert report["comparison"]["same_pull_requests"] is True
    assert "review-bot[bot] (imported comments)" in render_markdown(report)


def test_a_recorded_diy_run_replays_to_the_same_findings(tmp_path: Path) -> None:
    origin = make_origin(tmp_path)
    corpus = load_real_pr_corpus(write_corpus(tmp_path, origin))
    common: dict[str, Any] = {
        "depths": ("auto",),
        "workdir": tmp_path / "work",
        "reviewers": ("diy",),
        "diy_prompt": diy_prompt(),
    }
    recorded = BenchmarkOptions(mode="fake", record_dir=tmp_path / "fixtures", **common)
    first, _l, _f = run_benchmark(corpus, recorded)
    replayed = BenchmarkOptions(mode="replay", fixtures_dir=tmp_path / "fixtures", **common)
    second, ledger, _f = run_benchmark(corpus, replayed)
    assert not ledger.stopped
    for a, b in zip(first, second, strict=True):
        assert [f.to_wire() for f in a.attempts["diy"][0].findings] == [
            f.to_wire() for f in b.attempts["diy"][0].findings
        ]
        assert b.attempts["diy"][0].complete


# --------------------------------------------------------------------------- #
# the pieces
# --------------------------------------------------------------------------- #


def test_options_refuse_what_would_make_a_comparison_meaningless(tmp_path: Path) -> None:
    with pytest.raises(BenchmarkError, match="needs its prompt"):
        BenchmarkOptions(mode="fake", reviewers=("diy",))
    with pytest.raises(BenchmarkError, match="reviewer must be"):
        BenchmarkOptions(mode="fake", reviewers=("someone",))
    with pytest.raises(BenchmarkError, match="--repeat must be"):
        BenchmarkOptions(mode="fake", repeat=0)
    with pytest.raises(BenchmarkError, match="answer every rerun from the first recording"):
        BenchmarkOptions(mode="fake", repeat=2, record_dir=tmp_path)


def test_a_diff_at_the_bound_is_sent_whole_and_past_it_is_cut_and_said_so() -> None:
    diff = "\n".join(f"+line {n}" for n in range(200))
    whole, cut = user_message(diff, max_chars=len(diff))
    assert not cut and diff in whole and "truncated" not in whole
    shown, cut = user_message(diff, max_chars=len(diff) - 1)
    assert cut and TRUNCATION_NOTICE.split("{")[0] in shown and f"of {len(diff)} characters shown" in shown
    assert "+line 199" not in shown  # cut at a line end, not mid-line


def test_the_models_answer_is_parsed_without_repairing_it() -> None:
    found = parse_findings(
        {
            "findings": [
                {"file": "b/src/a.py", "line": 7, "severity": "Critical", "description": "boom"},
                {"file": "", "line": 1, "severity": "low", "description": "no path"},
                {"file": "src/a.py", "line": "x", "severity": "odd", "description": "unanchored"},
            ]
        }
    )
    assert [(f.path, f.line, f.severity) for f in found] == [("src/a.py", 0, "p2"), ("src/a.py", 7, "p0")]


# --------------------------------------------------------------------------- #
# the command
# --------------------------------------------------------------------------- #


def invoke(*args: str) -> Any:
    return CliRunner().invoke(developer_tools, ["eval", "review", "benchmark", *args], catch_exceptions=False)


def test_plan_prints_the_call_bound_and_maximum_spend_for_reviewers_times_repeats(tmp_path: Path) -> None:
    origin = make_origin(tmp_path)
    corpus = write_corpus(tmp_path, origin)
    result = invoke(
        "--corpus", str(corpus), "--plan", "--reviewer", "lemoncrow,diy", "--repeat", "3", "--depth", "auto", "--no-pushes",
        "--max-usd", "20", "--max-calls", "100", "--workdir", str(tmp_path / "work"),
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    for needle in (
        "reviewers lemoncrow, diy",
        "3 run(s) per pull request and reviewer",
        "6 full reviews",
        "6 DIY reviews",
        "lemoncrow auto: 2 PRs x 3 run(s) x at most 20 call(s) = 120",
        "diy: 2 PRs x 3 run(s) x 1 call = 6",
        "at most 126 model calls in all",
        "call bound: 100 (--max-calls 100",
        "maximum spend: $20 (--max-usd)",
    ):
        assert needle in result.output, needle
    assert not (tmp_path / "work").exists()  # nothing was fetched
    no_dollars = invoke("--corpus", str(corpus), "--plan", "--reviewer", "diy", "--max-calls", "10")
    assert "maximum spend: not bounded in dollars" in no_dollars.output
    assert "call bound: 2 (--max-calls 10" in no_dollars.output


def test_the_command_runs_both_reviewers_and_writes_the_raw_data(tmp_path: Path) -> None:
    origin = make_origin(tmp_path)
    corpus = write_corpus(tmp_path, origin)
    out = tmp_path / "reports"
    result = invoke(
        "--corpus", str(corpus), "--driver", "fake", "--reviewer", "lemoncrow,diy", "--repeat", "2", "--depth", "auto",
        "--no-pushes", "--workdir", str(tmp_path / "work"), "--out-dir", str(out), "--name", "both",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert (
        "DIY prompt (v1)" in result.output
        and "LemonCrow (auto)" in result.output
        and "runs: agreement" in result.output
    )
    report = json.loads((out / "both.json").read_text(encoding="utf-8"))
    assert [row["id"] for row in report["comparison"]["reviewers"]] == ["lemoncrow:auto", "diy"]
    assert "--reviewer lemoncrow,diy" in report["run"]["command"] and "--repeat 2" in report["run"]["command"]
    assert (out / "both.md").read_text(encoding="utf-8").count("## Reviewer comparison") == 1
    bad = invoke("--corpus", str(corpus), "--driver", "fake", "--reviewer", "nobody", "--workdir", str(tmp_path / "w2"))
    assert bad.exit_code != 0 and "choose from lemoncrow, diy" in bad.output
