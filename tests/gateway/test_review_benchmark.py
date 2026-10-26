"""The real-PR review benchmark end to end: real git, the real review pipeline, a scripted model.

A local origin plays GitHub: the benchmark shallow-fetches pinned commits from it, captures each range, runs the AI stage
at each depth through the same engine the Action uses, simulates a follow-up push with and without carry-forward, and
writes the report. Only the model is scripted (a ``BUG:`` marker in the changed code is what it "finds"), so what is
tested is the harness, never a model. Nothing here, and nothing it writes, is a result.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from lemoncrow.gateway.cli.developer_tools import developer_tools
from lemoncrow.pro.capabilities.review.evaluation.benchmark import (
    BenchmarkError,
    BenchmarkOptions,
    BudgetStop,
    CallRecord,
    Pricer,
    PrResult,
    RunMeter,
    ScriptedExecutor,
    SpendLedger,
    run_benchmark,
)
from lemoncrow.pro.capabilities.review.evaluation.benchmark_report import build_report, render_markdown
from lemoncrow.pro.capabilities.review.evaluation.real_prs import RealPrCorpus, load_real_pr_corpus
from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallUsage, StructuredExecution
from tests.gateway.benchmark_support import Origin, corpus_dict, git, make_origin, write_corpus
from tests.gateway.pr_bot_support import one_call_per_file

DEPTHS = ("quick", "auto", "deep")


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "LEMONCROW_LLM_BACKEND",
        "LEMONCROW_OPENAI_MODEL",
        "LEMONCROW_REVIEW_REPO_CONTEXT",
        "LEMONCROW_REVIEW_REPO_CONTEXT_TOKENS",
        "CI",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", "/nonexistent/sg")


class World:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.origin: Origin = make_origin(root)
        self.corpus_path = write_corpus(root, self.origin)
        self.workdir = root / "work"

    def corpus(self, **kwargs: Any) -> RealPrCorpus:
        return load_real_pr_corpus(self.corpus_path, **kwargs)

    def options(self, **kwargs: Any) -> BenchmarkOptions:
        values: dict[str, Any] = {"depths": DEPTHS, "mode": "fake", "workdir": self.workdir}
        values.update(kwargs)
        return BenchmarkOptions(**values)

    def run(self, **kwargs: Any) -> tuple[list[PrResult], SpendLedger, dict[str, Any], BenchmarkOptions]:
        options = self.options(**kwargs)
        results, ledger, facts = run_benchmark(self.corpus(), options)
        return results, ledger, facts, options


@pytest.fixture(scope="module")
def shared(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """One scripted run of the whole benchmark, read by the tests that only look at its results."""

    mp = pytest.MonkeyPatch()
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LEMONCROW_LLM_BACKEND", "LEMONCROW_REVIEW_REPO_CONTEXT"):
        mp.delenv(name, raising=False)
    mp.setenv("LEMONCROW_AST_GREP_BIN", "/nonexistent/sg")
    try:
        world = World(tmp_path_factory.mktemp("bench"))
        scripted = ScriptedExecutor()
        options = world.options()
        results, ledger, facts = run_benchmark(world.corpus(), options, scripted=scripted)
        corpus = world.corpus()
        report = build_report(
            corpus, options, results, ledger, facts, name="scripted", generated_at="2026-10-20T00:00:00+00:00"
        )
    finally:
        mp.undo()
    return {
        "world": world,
        "results": results,
        "ledger": ledger,
        "report": report,
        "markdown": render_markdown(report),
        "scripted": scripted,
    }


def pr(report: dict[str, Any], pr_id: str) -> dict[str, Any]:
    return next(item for item in report["prs"] if item["id"] == pr_id)


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #


def test_a_scripted_run_reviews_every_pull_request_at_every_depth(shared: dict[str, Any]) -> None:
    report = shared["report"]
    assert report["run"]["mode"] == "fake" and report["schema"] == 1 and report["kind"] == "review-benchmark"
    assert set(report["aggregate"]) == set(DEPTHS)
    for pr_id in ("shop-12", "shop-14"):
        assert set(pr(report, pr_id)["depths"]) == set(DEPTHS)
    json.dumps(report)  # the JSON report is plain data


def test_the_marked_bug_is_caught_and_scored_against_its_label_with_the_rule_that_fired(shared: dict[str, Any]) -> None:
    labelled = pr(shared["report"], "shop-12")["depths"]
    caught = labelled["auto"]["labels"]
    assert (caught["expected"], caught["caught"]) == (1, 1)
    (match,) = caught["matches"]
    assert match["label"] == "missing-region" and match["rule"] in {"location", "symbol"}
    assert labelled["auto"]["findings_by_severity"]["p1"] >= 1
    finding = labelled["auto"]["findings"][0]
    assert finding["path"] == "src/api/pricing.py" and "ignores a missing region" in finding["title"]
    assert labelled["deep"]["labels"]["caught"] == 1


def test_an_unlabelled_pull_request_gets_cost_and_findings_but_no_catch_or_noise(shared: dict[str, Any]) -> None:
    run = pr(shared["report"], "shop-14")["depths"]["auto"]
    assert "labels" not in run and run["model_calls"] >= 0 and "findings_by_severity" in run
    agg = shared["report"]["aggregate"]["auto"]
    assert agg["catch"]["labelled_prs"] == 1 and agg["catch"]["expected"] == 1  # shop-14 is not in the denominator


def test_quick_is_the_trusted_bases_review_effort_and_calls_the_model_at_most_once(shared: dict[str, Any]) -> None:
    quick = pr(shared["report"], "shop-14")["depths"]["quick"]
    assert quick["status"] == "ran" and quick["complete"] and quick["model_calls"] <= 1
    # Several independent targets: Quick spends its one call on the highest-ranked work and leaves the rest pending
    # (the review says so: "Partially reviewed"), where it used to make no call at all and report itself incomplete.
    multi = pr(shared["report"], "shop-12")["depths"]["quick"]
    assert multi["status"] == "ran" and multi["model_calls"] == 1
    # Both changed files carry the same kind of signal and are small, so the one call reaches them all.
    assert multi["investigated"] == multi["risk_candidates"] > 0


def test_depth_raises_the_calls_and_the_cost(shared: dict[str, Any]) -> None:
    agg = shared["report"]["aggregate"]
    assert (
        agg["quick"]["model_calls"]["mean"] <= agg["auto"]["model_calls"]["mean"] <= agg["deep"]["model_calls"]["mean"]
    )
    assert agg["auto"]["provider_usd"]["mean"] < agg["deep"]["provider_usd"]["mean"]
    assert agg["auto"]["input_tokens"]["mean"] < agg["deep"]["input_tokens"]["mean"]


def test_every_figure_the_task_asks_for_is_in_the_report(shared: dict[str, Any]) -> None:
    agg = shared["report"]["aggregate"]["deep"]
    for key in ("model_calls", "input_tokens", "output_tokens", "provider_usd", "wall_seconds", "model_seconds"):
        assert agg[key]["n"] >= 1 and agg[key]["mean"] is not None
    assert set(agg["findings"]) >= {"p0", "p1", "p2", "total"}
    assert agg["catch"]["rate"] == 1.0 and agg["catch"]["ci95"] is not None
    assert agg["noise"]["rate"] is not None and "duplicates" in agg["noise"]
    assert agg["cost_per_catch_usd"] is not None
    assert agg["context_tokens"]["mean"] is not None  # the cost of G4's repository context is visible per review
    run = pr(shared["report"], "shop-12")["depths"]["deep"]
    assert run["wall_seconds"] > 0 and run["input_tokens"] > 0 and run["provider_usd"] > 0


def test_a_follow_up_push_costs_less_with_carry_forward_than_re_reviewing_everything(shared: dict[str, Any]) -> None:
    push = shared["report"]["pushes"]["deep"]
    assert push["pushes"] == 1 and push["complete_pairs"] == 1 and not push["unavailable"]
    carry, full = push["carry_forward"], push["full"]
    assert carry["model_calls"]["mean"] < full["model_calls"]["mean"]
    assert carry["provider_usd"]["mean"] < full["provider_usd"]["mean"]
    assert carry["rereviewed_targets_mean"] < full["rereviewed_targets_mean"]
    assert push["saved"]["calls_fraction"] > 0 and push["saved"]["usd_fraction"] > 0
    # The fix commit was applied: the finding on the fixed code is gone.
    assert push["findings_after_push"]["fixed"] >= 1
    cell = pr(shared["report"], "shop-12")["depths"]["deep"]["pushes"]["apply-fix"]
    assert cell["applied"] in {"as written", "three-way merge", "without context"}
    assert cell["extra"]["calls_with_carry_forward"] < cell["extra"]["calls_without"]


def test_the_scripted_model_answers_the_re_check_so_the_fixed_finding_is_fixed_not_just_unreported(
    shared: dict[str, Any],
) -> None:
    states = shared["report"]["pushes"]["deep"]["findings_after_push"]
    assert states["fixed"] >= 1 and states["unreported"] == 0
    # It spoke the protocol: asked about the finding on the fixed code, it said resolved.
    asked = [
        row
        for prompt in shared["scripted"].prompts
        if isinstance(prompt.get("prior_findings"), dict)
        for row in prompt["prior_findings"]["value"]
    ]
    assert asked and {row["title"] for row in asked} == {"quote ignores a missing region"}


class _SilentOnRecheck(ScriptedExecutor):
    """A model that does not repeat the finding and does not answer the re-check either."""

    def __call__(self, **kwargs: Any) -> StructuredExecution:
        execution = super().__call__(**kwargs)
        return dataclasses.replace(execution, result={**execution.result, "prior_findings": []})


def test_a_finding_the_re_check_did_not_answer_is_counted_as_no_longer_reported_never_as_fixed(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    options = world.options(depths=("deep",))
    corpus = world.corpus()
    results, ledger, facts = run_benchmark(corpus, options, scripted=_SilentOnRecheck())
    report = build_report(
        corpus, options, results, ledger, facts, name="scripted", generated_at="2026-10-20T00:00:00+00:00"
    )
    states = report["pushes"]["deep"]["findings_after_push"]
    assert states["fixed"] == 0 and states["unreported"] >= 1
    cell = pr(report, "shop-12")["depths"]["deep"]["pushes"]["apply-fix"]
    assert cell["carry_forward"]["states"]["unreported"] >= 1 and cell["carry_forward"]["states"]["fixed"] == 0


def test_the_reference_team_projection_follows_from_the_reports_own_means(shared: dict[str, Any]) -> None:
    team = shared["report"]["reference_team"]
    assert (team["developers"], team["prs_per_month"], team["reviews_per_pr"]) == (10, 300, 1.2)
    row = team["by_depth"]["deep"]
    agg = shared["report"]["aggregate"]["deep"]
    push = shared["report"]["pushes"]["deep"]
    expected = round(300 * (agg["provider_usd"]["mean"] + 0.2 * push["carry_forward"]["provider_usd"]["mean"]), 2)
    assert row["monthly_usd_with_carry_forward"] == expected
    assert row["monthly_usd_without"] > row["monthly_usd_with_carry_forward"]
    assert "not a quote" in team["basis"]


def test_a_scripted_report_says_so_and_no_number_in_it_is_ready_to_publish(shared: dict[str, Any]) -> None:
    report, markdown = shared["report"], shared["markdown"]
    assert markdown.startswith("# Review benchmark: scripted")
    assert "Basis: the scripted driver (no model was called)" in markdown and "Do not quote it" in markdown
    publication = report["publication"]
    for claim in ("cost_per_review", "quality", "head_to_head"):
        assert publication[claim]["ready"] is False
        assert any(
            "only a live run can support a published figure" in reason for reason in publication[claim]["missing"]
        )
    assert "measured" not in markdown.lower()  # nothing the harness writes calls a number measured
    assert "wall_seconds" in markdown and "Reference team (a projection)" in markdown


def test_an_incomplete_review_is_not_scored_as_having_missed_a_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    one_call_per_file(monkeypatch)  # the pull request's two small files get a call each, so a ceiling can cut one
    """A review the call ceiling cut short never finished: it has no catch or miss to score."""

    world = World(tmp_path)
    results, ledger, facts, options = world.run(max_calls=1, depths=("auto",), pushes=False)
    report = build_report(world.corpus(), options, results, ledger, facts, name="cut")
    run = pr(report, "shop-12")["depths"]["auto"]
    assert run["complete"] is False and run["stopped_by_budget"] and "labels" not in run
    assert "the call ceiling of 1 was reached" in render_markdown(report)


def test_a_missed_label_says_whether_the_model_was_ever_asked(tmp_path: Path) -> None:
    world = World(tmp_path)
    document = corpus_dict(world.origin)
    pricing = {"id": "ghost", "summary": "A bug nobody marked", "symbols": ["nothing_like_this"],
               "evidence": {"fix_commit": world.origin.fix, "attribution": "blame"}}  # fmt: skip
    document["prs"][1]["labels"] = [{**pricing, "regions": [{"path": "src/api/reports.py", "lines": [2, 2]}]}]
    document["prs"].append(
        {
            "id": "shop-15", "repository": "acme/shop", "clone_url": world.origin.url, "pull": 15, "title": "Lower-case text",
            "license": "MIT", "base_sha": world.origin.plain, "head_sha": world.origin.helper,
            "provenance": {"source": "public_repository", "repository": "https://github.com/acme/shop",
                           "commit": world.origin.helper, "note": "t"},
            "labels": [{**pricing, "id": "ghost-2", "regions": [{"path": "src/helpers.py", "lines": [2, 2]}]}],
        }
    )  # fmt: skip
    # shop-15 changes only documentation, so triage finds nothing a model could look at.
    notes = world.origin.path / "docs" / "notes.md"
    notes.parent.mkdir()
    notes.write_text("Notes.\n", encoding="utf-8")
    git(world.origin.path, "add", "-A")
    git(world.origin.path, "commit", "-qm", "Notes (#16)")
    docs_only = git(world.origin.path, "rev-parse", "HEAD")
    last = document["prs"][-1]
    last["base_sha"], last["head_sha"], last["provenance"]["commit"] = world.origin.helper, docs_only, docs_only
    last["labels"][0]["regions"] = [{"path": "docs/notes.md", "lines": [1, 1]}]
    world.corpus_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    results, ledger, facts, options = world.run(depths=("auto",), pushes=False)
    report = build_report(world.corpus(), options, results, ledger, facts, name="causes")

    asked = pr(report, "shop-14")["depths"]["auto"]["labels"]["missed_detail"]
    unasked = pr(report, "shop-15")["depths"]["auto"]["labels"]["missed_detail"]
    assert asked == [{"label": "ghost", "cause": "asked_not_reported"}]
    assert unasked == [{"label": "ghost-2", "cause": "no_risk_candidate"}]  # triage never asked the model
    assert report["aggregate"]["auto"]["catch"]["missed_by_cause"] == {
        "no_risk_candidate": 1,
        "not_asked": 0,
        "asked_not_reported": 1,
    }
    markdown = render_markdown(report)
    assert "triage found no risk signal, so no model was asked" in markdown
    assert "the model was asked about this file and did not report it" in markdown
    assert (
        pr(report, "shop-15")["depths"]["auto"]["model_calls"] == 0
        and report["aggregate"]["auto"]["zero_call_reviews"] == 1
    )


# --------------------------------------------------------------------------- #
# the ceiling, the price, the record
# --------------------------------------------------------------------------- #


def test_the_ceiling_stops_the_run_and_leaves_a_partial_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    one_call_per_file(monkeypatch)  # the pull request's two small files get a call each, so a ceiling can cut one
    world = World(tmp_path)
    results, ledger, facts, options = world.run(max_calls=1, depths=("auto", "deep"))

    assert ledger.stopped == "the call ceiling of 1 was reached" and ledger.calls == 1
    first, second = results
    assert first.reviews["auto"].stopped_by_budget and not first.reviews["auto"].complete
    assert second.reviews["auto"].status == "skipped" and second.reviews["auto"].reason == ledger.stopped
    assert [run.status for run in (first.reviews["deep"], second.reviews["deep"])] == ["skipped", "skipped"]
    assert not first.pushes and not second.pushes  # no push is simulated after the run stopped
    report = build_report(world.corpus(), options, results, ledger, facts, name="partial")
    assert report["run"]["ledger"]["stopped"] and report["publication"]["cost_per_review"]["ready"] is False
    assert any("stopped early" in reason for reason in report["publication"]["cost_per_review"]["missing"])
    assert report["aggregate"]["deep"]["skipped"] == 2 and report["aggregate"]["deep"]["complete"] == 0
    assert "stopped:" in render_markdown(report)


def test_a_dollar_ceiling_needs_a_price_and_stops_without_one() -> None:
    ledger = SpendLedger(max_calls=None, max_usd=1.0)
    ledger.reserve()
    ledger.settle(None, "mystery-model")
    assert "--max-usd cannot be enforced" in ledger.stopped
    with pytest.raises(BudgetStop):
        ledger.reserve()

    paid = SpendLedger(max_calls=None, max_usd=0.0005)
    paid.reserve()
    paid.settle(0.0006, "m")
    with pytest.raises(BudgetStop, match=r"spending ceiling of \$0.00"):
        paid.reserve()
    assert paid.to_wire()["spent_usd"] == pytest.approx(0.0006)


def test_a_dollar_ceiling_is_enforced_with_prices_you_supply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_OPENAI_MODEL", "unlisted-model")  # a model the local price table has no price for
    world = World(tmp_path)
    scripted = ScriptedExecutor(report_cost=False)  # the provider reports tokens but no dollars
    options = world.options(
        depths=("auto",), max_usd=0.000001, input_usd_per_mtok=1.0, output_usd_per_mtok=5.0, pushes=False
    )
    results, ledger, _facts = run_benchmark(world.corpus(), options, scripted=scripted)
    assert ledger.stopped.startswith("the spending ceiling") and ledger.unpriced_calls == 0
    first = results[0].reviews["auto"]
    assert first.calls and all(call.usd_source == "override" and call.usd is not None for call in first.calls)

    unpriced = world.options(depths=("auto",), max_usd=1.0, pushes=False)
    results, ledger, _facts = run_benchmark(world.corpus(), unpriced, scripted=ScriptedExecutor(report_cost=False))
    assert "reported no cost" in ledger.stopped and ledger.unpriced_calls >= 1


def test_the_pricer_prefers_what_the_provider_reported_then_the_table_then_your_numbers() -> None:
    def execution(model: str, **usage: Any) -> StructuredExecution:
        return StructuredExecution(result={}, driver="openai", model=model, usage=ModelCallUsage(calls=1, **usage))

    pricer = Pricer(2.0, 10.0)
    assert pricer.price(execution("m", input_tokens=10, output_tokens=5, provider_cost=0.5, currency="USD")) == (
        0.5,
        "provider",
    )
    assert pricer.price(execution("m", input_tokens=1_000_000, output_tokens=100_000)) == (3.0, "override")
    assert pricer.price(execution("m", input_tokens=None, output_tokens=None)) == (None, "unknown")
    assert (
        pricer.price(execution("m", input_tokens=5, output_tokens=5, provider_cost=0.1, currency="EUR"))[1]
        == "override"
    )
    table = Pricer().price(execution("gpt-4o-mini", input_tokens=1_000_000, output_tokens=1_000_000))
    assert table[1] == "price_table" and table[0] is not None and table[0] > 0
    assert Pricer().price(execution("no-such-model", input_tokens=1, output_tokens=1)) == (None, "unknown")
    assert (
        Pricer().can_price("gpt-4o-mini")
        and not Pricer().can_price("no-such-model")
        and Pricer(1.0, 1.0).can_price("x")
    )


def test_the_meter_records_failures_and_charges_the_shared_ledger() -> None:
    ledger = SpendLedger(max_calls=2, max_usd=None)
    calls: list[int] = []

    def inner(**kwargs: Any) -> StructuredExecution:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("provider down")
        return StructuredExecution(
            result={},
            driver="openai",
            model="m",
            usage=ModelCallUsage(calls=1, input_tokens=4, output_tokens=2, provider_cost=0.01, currency="USD"),
        )

    meter = RunMeter(inner, ledger, Pricer())
    with pytest.raises(RuntimeError):
        meter(output_schema={}, prompt="p", system="s")
    meter(output_schema={}, prompt="p", system="s")
    with pytest.raises(BudgetStop):
        meter(output_schema={}, prompt="p", system="s")
    assert [(c.ok, c.usd) for c in meter.calls] == [(False, None), (True, 0.01)]
    assert meter.refused and ledger.calls == 2 and ledger.unpriced_calls == 1


def test_options_refuse_a_run_that_could_spend_without_limit() -> None:
    with pytest.raises(BenchmarkError, match="needs a spending ceiling"):
        BenchmarkOptions(mode="live")
    with pytest.raises(BenchmarkError, match="needs --fixtures-dir"):
        BenchmarkOptions(mode="replay")
    with pytest.raises(BenchmarkError, match="depth must be"):
        BenchmarkOptions(mode="fake", depths=("auto", "medium"))
    with pytest.raises(BenchmarkError, match="listed twice"):
        BenchmarkOptions(mode="fake", depths=("auto", "auto"))
    assert BenchmarkOptions(mode="live", max_usd=5.0).max_usd == 5.0


def test_a_live_run_never_starts_under_pytest_whatever_the_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    options = world.options(mode="live", max_usd=1.0)
    with pytest.raises(BenchmarkError, match="never runs under pytest"):
        run_benchmark(world.corpus(), options)


def test_recorded_calls_replay_to_the_same_findings_and_the_same_cost(tmp_path: Path) -> None:
    world = World(tmp_path)
    fixtures = tmp_path / "fixtures"
    recorded, _ledger, facts, _options = world.run(depths=("auto", "deep"), record_dir=fixtures)
    assert sorted(p.name for p in fixtures.iterdir()) == ["shop-12.json", "shop-14.json"]
    saved = json.loads((fixtures / "shop-12.json").read_text(encoding="utf-8"))
    assert saved["recorded_with"]["mode"] == "fake" and "no provider was contacted" in saved["recorded_with"]["note"]
    assert saved["case_input_digest"] == world.corpus().prs[0].input_digest()

    replayed, _l2, facts2, options2 = world.run(depths=("auto", "deep"), mode="replay", fixtures_dir=fixtures)
    assert facts2["driver"] == facts["driver"] and facts2["model"] == facts["model"]
    for left, right in zip(recorded, replayed, strict=True):
        for depth in ("auto", "deep"):
            a, b = left.reviews[depth], right.reviews[depth]
            assert [f.id for f in a.findings] == [f.id for f in b.findings]
            assert a.usage()["provider_usd"] == b.usage()["provider_usd"]
            assert a.usage()["input_tokens"] == b.usage()["input_tokens"] and a.complete == b.complete
    report = build_report(world.corpus(), options2, replayed, _l2, facts2, name="replayed")
    assert report["run"]["mode"] == "replay" and "replay of fixtures" in report["run"]["basis"]
    assert report["aggregate"]["deep"]["catch"]["caught"] == 1
    assert "not comparable" in report["run"]["wall_time_note"]


def test_a_replay_never_serves_a_call_whose_prompt_changed(tmp_path: Path) -> None:
    world = World(tmp_path)
    fixtures = tmp_path / "fixtures"
    world.run(depths=("auto",), record_dir=fixtures)
    # Same range, but the context pack is switched off: the prompts differ, so no recorded call has their identity.
    os.environ["LEMONCROW_REVIEW_REPO_CONTEXT"] = "0"
    try:
        results, _ledger, facts, _options = world.run(
            depths=("auto",), mode="replay", fixtures_dir=fixtures, pushes=False
        )
    finally:
        os.environ.pop("LEMONCROW_REVIEW_REPO_CONTEXT", None)
    labelled = results[0].reviews["auto"]
    assert not labelled.complete and labelled.errors and not labelled.findings
    assert any("replay misses" in note and "exact prompt" in note for note in facts["notes"])


def test_a_replay_of_a_changed_pull_request_is_stale_not_reused(tmp_path: Path) -> None:
    world = World(tmp_path)
    fixtures = tmp_path / "fixtures"
    world.run(depths=("auto",), record_dir=fixtures, pushes=False)
    document = corpus_dict(world.origin)
    document["prs"][0]["head_sha"] = world.origin.fix  # the pinned inputs changed: another range under review
    document["prs"][0]["provenance"]["commit"] = world.origin.fix
    world.corpus_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    results, _ledger, facts, _options = world.run(depths=("auto",), mode="replay", fixtures_dir=fixtures, pushes=False)
    assert not results[0].reviews["auto"].complete
    assert any("pinned inputs changed since recording" in note for note in facts["notes"])


# --------------------------------------------------------------------------- #
# the other reviewer's comments
# --------------------------------------------------------------------------- #


def test_another_reviewers_comments_are_compared_with_our_findings_and_with_the_labels(tmp_path: Path) -> None:
    world = World(tmp_path)
    line = 6
    comments = {
        "schema": 1,
        "reviewer": "review-bot[bot]",
        "comments": [
            {
                "id": "11",
                "author": "review-bot[bot]",
                "path": "src/api/pricing.py",
                "line": line,
                "end_line": line,
                "body": "**P1** quote ignores a missing region: `FACTORS[region]` raises KeyError",
                "verdict": "valid",
            },
            {
                "id": "12",
                "author": "review-bot[bot]",
                "path": "src/api/invoice.py",
                "line": 9,
                "end_line": 9,
                "body": "Consider renaming `label` for clarity.",
            },
            {
                "id": "13",
                "author": "review-bot[bot]",
                "path": "",
                "line": 0,
                "end_line": 0,
                "body": "Confidence 4/5",
                "kind": "summary",
            },
        ],
    }
    (tmp_path / "baseline").mkdir()
    (tmp_path / "baseline/shop-12.json").write_text(json.dumps(comments), encoding="utf-8")
    document = corpus_dict(world.origin)
    document["prs"][0]["baseline"] = {"comments_file": "baseline/shop-12.json"}
    world.corpus_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    results, ledger, facts, options = world.run(depths=("auto",), pushes=False)
    report = build_report(
        world.corpus(),
        options,
        results,
        ledger,
        facts,
        name="vs",
        baseline_usd_per_review=1.0,
        baseline_label="supplied",
    )
    baseline = report["baseline"]
    assert baseline["prs_with_comments"] == 1 and baseline["inline_comments"] == 2 and baseline["summary_comments"] == 1
    assert baseline["judged"] == {"valid": 1, "invalid": 0}
    auto = baseline["by_depth"]["auto"]
    assert auto["agreed"] == 1 and auto["theirs_only"] == 1 and auto["share_of_theirs_we_also_raised"] == 0.5
    own = baseline["against_labels"]
    assert (own["expected"], own["caught"], own["comments"], own["unmatched"]) == (1, 1, 2, 1)
    assert baseline["usd_per_review_supplied"] == {"value": 1.0, "label": "supplied"}
    assert baseline["usd_ratio_by_depth"]["auto"] > 1  # your price over ours, both inputs shown
    markdown = render_markdown(report)
    assert (
        "## Against the other reviewer" in markdown and "It is your input, not something this run found out" in markdown
    )
    assert "Agreement is not accuracy" in markdown


def test_a_missing_comments_file_is_noted_not_fatal(tmp_path: Path) -> None:
    world = World(tmp_path)
    document = corpus_dict(world.origin)
    document["prs"][0]["baseline"] = {"comments_file": "nowhere.json"}
    world.corpus_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    results, _l, _f, _o = world.run(depths=("auto",), pushes=False)
    assert results[0].comments == [] and "baseline comments unavailable" in results[0].baseline_note


# --------------------------------------------------------------------------- #
# the command
# --------------------------------------------------------------------------- #


def invoke(*args: str) -> Any:
    return CliRunner().invoke(developer_tools, ["eval", "review", "benchmark", *args], catch_exceptions=False)


def test_plan_prints_what_would_run_and_its_call_bound_and_touches_nothing(tmp_path: Path) -> None:
    world = World(tmp_path)
    result = invoke("--corpus", str(world.corpus_path), "--plan", "--max-usd", "20", "--workdir", str(world.workdir))
    assert result.exit_code == 0, result.output
    assert "2 pull requests, 1 labelled, 1 labels" in result.output and "max $20" in result.output
    assert "6 full reviews" in result.output and "push reviews" in result.output
    assert "at most " in result.output and "dollars cannot be bounded before a call returns its tokens" in result.output
    assert not world.workdir.exists()  # nothing was fetched


def test_a_live_run_without_a_ceiling_or_under_pytest_is_refused_before_any_call(tmp_path: Path) -> None:
    world = World(tmp_path)
    no_ceiling = invoke("--corpus", str(world.corpus_path), "--workdir", str(world.workdir))
    assert no_ceiling.exit_code != 0 and "needs a spending ceiling" in no_ceiling.output
    under_pytest = invoke("--corpus", str(world.corpus_path), "--workdir", str(world.workdir), "--max-usd", "1")
    assert under_pytest.exit_code != 0 and "never runs under pytest" in under_pytest.output
    assert not world.workdir.exists()


def test_the_scripted_driver_writes_a_json_and_a_markdown_report(tmp_path: Path) -> None:
    world = World(tmp_path)
    out = tmp_path / "reports"
    result = invoke(
        "--corpus", str(world.corpus_path), "--driver", "fake", "--depth", "auto,deep", "--workdir", str(world.workdir),
        "--out-dir", str(out), "--name", "cli-run", "--no-pushes",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "basis the scripted driver" in result.output and "ready to publish: nothing yet" in result.output
    report = json.loads((out / "cli-run.json").read_text(encoding="utf-8"))
    assert report["name"] == "cli-run" and set(report["aggregate"]) == {"auto", "deep"}
    assert report["run"]["command"].startswith("lemoncrow dev eval review benchmark --corpus ")
    assert "--driver fake" in report["run"]["command"] and "--no-pushes" in report["run"]["command"]
    assert "--depth auto,deep" in report["run"]["command"] and "--max-usd" not in report["run"]["command"]
    assert report["run"]["code"]["prompt_version"] >= 1 and report["run"]["code"]["repo_context"]["enabled"] is True
    assert (out / "cli-run.md").read_text(encoding="utf-8").startswith("# Review benchmark: cli-run")
    assert report["pushes"]["auto"]["pushes"] == 0


def test_the_command_is_reachable_where_the_notes_name_it(tmp_path: Path) -> None:
    for path in (["eval", "review", "benchmark"], ["eval", "review-benchmark"]):
        result = CliRunner().invoke(developer_tools, [*path, "--help"], catch_exceptions=False)
        assert result.exit_code == 0 and "--max-usd" in result.output
    for sub in ("import-reviews", "derive-label"):
        assert CliRunner().invoke(developer_tools, ["eval", "review", sub, "--help"]).exit_code == 0


def test_the_ceiling_stops_the_command_with_a_partial_report_and_exit_code_3(tmp_path: Path) -> None:
    world = World(tmp_path)
    out = tmp_path / "reports"
    capped = CliRunner().invoke(
        developer_tools,
        ["eval", "review", "benchmark", "--corpus", str(world.corpus_path), "--driver", "fake", "--depth", "auto,deep",
         "--workdir", str(world.workdir), "--out-dir", str(out), "--name", "capped", "--max-calls", "2"],
        catch_exceptions=False,
    )  # fmt: skip
    assert capped.exit_code == 3, capped.output
    assert "stopped: the call ceiling of 2 was reached (partial report written)" in capped.output
    report = json.loads((out / "capped.json").read_text(encoding="utf-8"))
    assert report["run"]["ledger"]["stopped"] and report["run"]["ledger"]["calls"] == 2
    assert "--max-calls 2" in report["run"]["command"]


def test_a_customers_corpus_writes_its_report_outside_the_checkout_only(tmp_path: Path) -> None:
    world = World(tmp_path)
    document = corpus_dict(world.origin)
    document.update(
        visibility="organization",
        organization="acme",
        opt_in={"organization": "acme", "approved_by": "Pat", "approved_at": "2026-10-20", "reference": "t-1"},
    )
    for entry in document["prs"]:
        entry["provenance"]["source"] = "organization_review"
        entry["license"] = "proprietary"
    customer = tmp_path / "customer.yaml"
    customer.write_text(yaml.safe_dump(document), encoding="utf-8")
    base = ["eval", "review", "benchmark", "--corpus", str(customer), "--driver", "fake", "--depth", "auto", "--no-pushes",
            "--workdir", str(world.workdir)]  # fmt: skip
    refused = CliRunner().invoke(developer_tools, base)
    assert refused.exit_code != 0 and "requires an explicit organization opt-in" in refused.output
    no_dir = CliRunner().invoke(developer_tools, [*base, "--organization-opt-in", "acme"])
    assert no_dir.exit_code != 0 and "needs --out-dir outside this checkout" in no_dir.output
    here = Path(__file__).resolve().parents[2] / "evals/review/reports"
    inside = CliRunner().invoke(developer_tools, [*base, "--organization-opt-in", "acme", "--out-dir", str(here)])
    assert inside.exit_code != 0 and "must stay outside" in inside.output
    ok = CliRunner().invoke(
        developer_tools,
        [*base, "--organization-opt-in", "acme", "--out-dir", str(tmp_path / "private")],
        catch_exceptions=False,
    )
    assert ok.exit_code == 0 and (tmp_path / "private").is_dir()


def test_import_reviews_normalises_an_export_and_reports_the_authors_it_saw(tmp_path: Path) -> None:
    raw = [
        {
            "id": 1,
            "user": {"login": "review-bot[bot]"},
            "path": "a.py",
            "line": 3,
            "body": "**P1** bad",
            "html_url": "u",
        },
        {"id": 2, "user": {"login": "alice"}, "path": "a.py", "line": 3, "body": "human"},
    ]
    source = tmp_path / "raw.json"
    source.write_text(json.dumps(raw), encoding="utf-8")
    out = tmp_path / "baseline/pr-1.json"
    result = CliRunner().invoke(
        developer_tools,
        ["eval", "review", "import-reviews", "--from-json", str(source), "--reviewer-login", "review-bot[bot]",
         "--out", str(out)],
        catch_exceptions=False,
    )  # fmt: skip
    assert result.exit_code == 0 and "1 comments (1 inline, 0 summary) by review-bot[bot]" in result.output
    exported = json.loads(out.read_text(encoding="utf-8"))
    assert (
        exported["schema"] == 1
        and exported["reviewer"] == "review-bot[bot]"
        and exported["comments"][0]["severity"] == "P1"
        and exported["comments"][0]["verdict"] == ""
    )

    wrong = CliRunner().invoke(
        developer_tools,
        [
            "eval",
            "review",
            "import-reviews",
            "--from-json",
            str(source),
            "--out",
            str(out),
            "--reviewer-login",
            "other[bot]",
        ],
    )
    assert wrong.exit_code != 0 and "authors seen" in wrong.output
    both = CliRunner().invoke(developer_tools, ["eval", "review", "import-reviews", "--out", str(out)])
    assert both.exit_code != 0 and "exactly one of --pr or --from-json" in both.output


def test_import_reviews_has_no_default_bot_login(tmp_path: Path) -> None:
    source = tmp_path / "raw.json"
    source.write_text(json.dumps([{"id": 1, "user": {"login": "anyone"}, "path": "a.py", "line": 3, "body": "x"}]))
    out = tmp_path / "out.json"
    args = ["eval", "review", "import-reviews", "--from-json", str(source), "--out", str(out)]
    missing = CliRunner().invoke(developer_tools, args)
    assert missing.exit_code == 2 and "--reviewer-login" in missing.output and not out.exists()
    everyone = CliRunner().invoke(developer_tools, [*args, "--all-authors"], catch_exceptions=False)
    assert everyone.exit_code == 0 and "1 comments" in everyone.output and out.exists()


def test_fetching_the_baseline_reviewers_comments_needs_its_login(tmp_path: Path) -> None:
    world = World(tmp_path)
    refused = CliRunner().invoke(
        developer_tools,
        ["eval", "review", "benchmark", "--corpus", str(world.corpus_path), "--driver", "fake", "--fetch-baseline"],
    )
    assert refused.exit_code == 2 and "--fetch-baseline needs --reviewer-login" in refused.output
    # A corpus entry that asks for a fetch without any login is noted, not guessed at.
    document = corpus_dict(world.origin)
    document["prs"][0]["baseline"] = {"fetch": True}
    world.corpus_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    results, _l, _f, _o = world.run(depths=("auto",), pushes=False)
    assert results[0].comments == [] and "name the baseline reviewer's bot login" in results[0].baseline_note


def test_derive_label_prints_a_corpus_entry_from_git_alone(tmp_path: Path) -> None:
    origin = make_origin(tmp_path)
    result = CliRunner().invoke(
        developer_tools,
        ["eval", "review", "derive-label", "--repo", str(origin.path), "--fix", origin.fix, "--introduced", origin.bug,
         "--id", "shop-12", "--repository", "acme/shop", "--pull", "12", "--license", "MIT", "--symbols", "region"],
        catch_exceptions=False,
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    entry = yaml.safe_load(result.output)[0]
    assert entry["labels"][0]["evidence"]["fix_commit"] == origin.fix and entry["labels"][0]["symbols"] == ["region"]
    assert entry["labels"][0]["summary"] == "Fix the region regression (#13)"
    assert entry["pushes"][0]["apply_commit"] == origin.fix
    # The entry it prints is a valid corpus entry.
    corpus = {"version": 1, "id": "x", "visibility": "public", "prs": [entry]}
    path = tmp_path / "derived.yaml"
    path.write_text(yaml.safe_dump(corpus), encoding="utf-8")
    assert load_real_pr_corpus(path).prs[0].labels[0].regions[0].path == "src/api/pricing.py"


def test_call_records_serialise_without_the_prompt() -> None:
    record = CallRecord("finding_investigation", True, 0.5, 10, 5, 0.01, "provider")
    assert set(record.to_wire()) == {"stage", "ok", "seconds", "input_tokens", "output_tokens", "usd", "usd_source"}
