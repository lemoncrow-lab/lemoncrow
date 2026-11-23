"""Review evaluation replay (CI mode): measured selections, traceability, stale fixtures, no live spend."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import ai_review, intelligence_runtime
from lemoncrow.pro.capabilities.review.evaluation import fixtures as eval_fixtures
from lemoncrow.pro.capabilities.review.evaluation.corpus import Corpus, load_builtin_corpus, load_corpus
from lemoncrow.pro.capabilities.review.evaluation.digests import HARNESS_VERSION, prompt_component
from lemoncrow.pro.capabilities.review.evaluation.fixtures import (
    LiveBudget,
    LiveBudgetExhausted,
    LiveRecordingRefused,
    RecordedWith,
    SyntheticReviewer,
    ensure_live_recording_allowed,
    live_responder,
)
from lemoncrow.pro.capabilities.review.evaluation.gate import load_gate_config
from lemoncrow.pro.capabilities.review.evaluation.harness import EvalReport, ReplaySpec, record_corpus, replay_corpus
from lemoncrow.pro.capabilities.review.evaluation.pipeline import Reviewer, Selection
from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallUsage, StructuredExecution

REPO = Path(__file__).resolve().parents[2]
GATE_DIR = REPO / "evals" / "review"
RERECORD = "re-record with `uv run lc dev eval review record --synthetic` and commit the fixtures"


def _block_live_paths(patch: pytest.MonkeyPatch) -> list[str]:
    """Record any attempt to reach a model driver or provider; every one must stay unused."""

    attempts: list[str] = []

    def refuse(name: str) -> Callable[..., Any]:
        def call(*_args: Any, **_kwargs: Any) -> Any:
            attempts.append(name)
            raise AssertionError(f"CI replay reached the live path {name}")

        return call

    from lemoncrow.infra import internal_llm
    from lemoncrow.infra.internal_llm import openai_client

    patch.setattr(intelligence_runtime, "execute_structured", refuse("execute_structured"))
    patch.setattr(intelligence_runtime, "_execute_cli_structured", refuse("_execute_cli_structured"))
    patch.setattr(ai_review, "execute_structured", refuse("ai_review.execute_structured"))
    patch.setattr(openai_client, "chat_with_result", refuse("openai_client.chat_with_result"))
    patch.setattr(internal_llm, "chat_stream", refuse("internal_llm.chat_stream"))
    patch.setattr(eval_fixtures, "live_responder", refuse("live_responder"))
    return attempts


@pytest.fixture(scope="module")
def builtin() -> Iterator[tuple[EvalReport, list[str]]]:
    """One CI-mode replay of the whole built-in corpus under every gate selection."""

    with pytest.MonkeyPatch.context() as patch:
        attempts = _block_live_paths(patch)
        report = replay_corpus(load_builtin_corpus(REPO), load_gate_config(GATE_DIR).spec)
        yield report, attempts


def test_eval_replay_store_clones_are_fast_and_isolated(tmp_path: Path) -> None:
    import sqlite3

    from lemoncrow.pro.capabilities.review.evaluation.pipeline import _fresh_store
    from lemoncrow.pro.capabilities.review.store import ReviewStore

    first = _fresh_store(tmp_path / "first")
    second = _fresh_store(tmp_path / "second")

    first_conn = first._connect()
    second_conn = second._connect()
    try:
        assert first_conn.execute("PRAGMA synchronous").fetchone()[0] == 0
        assert second_conn.execute("PRAGMA synchronous").fetchone()[0] == 0
        first_conn.execute("CREATE TABLE eval_clone_marker (value TEXT)")
        first_conn.commit()
    finally:
        first._release(first_conn)
        second._release(second_conn)

    with sqlite3.connect(second.db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='eval_clone_marker'"
            ).fetchone()[0]
            == 0
        )

    normal = ReviewStore(tmp_path / "normal")
    normal.init()
    normal_conn = normal._connect()
    try:
        assert normal_conn.execute("PRAGMA synchronous").fetchone()[0] != 0
    finally:
        normal._release(normal_conn)


def _subset(tmp_path: Path, *case_ids: str) -> Corpus:
    copy = tmp_path / "corpus"
    shutil.copytree(GATE_DIR / "corpus" / "builtin", copy)
    return load_corpus(copy, case_ids=case_ids)


def _spec(*selections: str, model: str | None = None) -> ReplaySpec:
    reviewer = load_gate_config(GATE_DIR).spec.reviewer
    return ReplaySpec(
        reviewer=Reviewer(reviewer.driver, model or reviewer.model),
        selections=tuple(Selection.preset(name) for name in selections),
    )


def test_every_selection_is_measured_on_the_builtin_corpus(builtin: tuple[EvalReport, list[str]]) -> None:
    report, _attempts = builtin

    assert report.complete, f"stale or missing fixtures: {report.fixture_events()}; {RERECORD}"
    assert set(report.selections) == {"quick", "auto", "thorough", "deep", "custom:static-only"}
    for label, metrics in report.selections.items():
        assert isinstance(metrics["catch"]["rate"], float), label
        assert isinstance(metrics["noise"]["rate"], float), label
        assert isinstance(metrics["behavior"]["rate"], float), label
        per_review = metrics["cost"]["per_review"]
        assert isinstance(per_review["estimated_compute_minutes"], float), label
        assert isinstance(per_review["model_calls"], float), label
        assert metrics["cost"]["reviews"] == len(report.corpus.cases), label


def test_the_corpus_discriminates_between_selections(builtin: tuple[EvalReport, list[str]]) -> None:
    report, _attempts = builtin
    rates = {label: metrics["catch"]["rate"] for label, metrics in report.selections.items()}
    static_only = report.selections["custom:static-only"]

    assert len({rates[name] for name in ("quick", "auto", "thorough", "deep")}) > 1
    assert static_only["catch"]["rate"] == 0.0
    assert static_only["cost"]["total"]["model_calls"] == 0
    assert static_only["behavior"]["rate"] == 1.0
    assert (
        report.selections["deep"]["cost"]["total"]["model_calls"]
        > report.selections["quick"]["cost"]["total"]["model_calls"]
    )


def test_ci_replay_never_constructs_a_live_driver(builtin: tuple[EvalReport, list[str]]) -> None:
    report, attempts = builtin

    assert attempts == []
    assert sum(len(run.model_calls) for run in report.runs) > 0  # answered, from fixtures only


def test_metrics_trace_to_per_case_review_records(builtin: tuple[EvalReport, list[str]]) -> None:
    report, _attempts = builtin
    wire = report.to_wire()
    records = {(row["case_id"], row["selection"]): row for row in wire["records"]}

    assert all(row["review_id"] and row["revision_id"] for row in wire["records"])
    for label, metrics in wire["selections"].items():
        assert set(metrics["cost"]["cases"]) == set(metrics["cases"])
        for row in metrics["catch"]["caught"]:
            record = records[(row["case"], label)]
            assert row["finding"] in {item["ref"] for item in record["findings"]}
        for row in metrics["noise"]["false"]:
            record = records[(row["case"], label)]
            assert row["finding"] in {item["ref"] for item in record["findings"]}
        for row in metrics["behavior"]["caught"]:
            record = records[(row["case"], label)]
            assert row["observed"] in {item["ref"] for item in record["behavior"]}
        for row in metrics["catch"]["missed"] + metrics["behavior"]["missed"]:
            assert (row["case"], label) in records
        for entry in metrics["entries"].values():
            for ref in entry["catches"] + entry["false_findings"] + entry["behavior_caught"]:
                assert ref.split("#", 1)[0] in metrics["cases"]


def test_replay_is_deterministic(tmp_path: Path) -> None:
    corpus = _subset(tmp_path, "multi-file-ranked", "clean-auth-and-index")
    spec = _spec("auto", "thorough")

    first = replay_corpus(corpus, spec).snapshot()
    second = replay_corpus(corpus, spec).snapshot()

    assert first == second
    assert first["selections"]["thorough"]["caught"] == ["multi-file-ranked#pagination-dropped"]
    assert first["selections"]["auto"]["missed"] == ["multi-file-ranked#pagination-dropped"]
    assert first["selections"]["auto"]["false_findings"]


def test_checked_in_fixtures_are_reproducible_by_the_synthetic_reviewer(tmp_path: Path) -> None:
    corpus = _subset(tmp_path, "auth-env-bypass", "multi-file-ranked", "clean-local-rename")
    spec = load_gate_config(GATE_DIR).spec
    reviewer = spec.reviewer

    result = record_corpus(
        corpus,
        spec,
        responder_for=lambda case: SyntheticReviewer(case, driver=reviewer.driver, model=reviewer.model),
        recorded_with=RecordedWith("synthetic", reviewer.driver, reviewer.model),
    )

    assert result.ok and len(result.written) == 3
    for path in result.written:
        checked_in = GATE_DIR / "corpus" / "builtin" / "fixtures" / path.name
        assert path.read_bytes() == checked_in.read_bytes(), f"{path.name} is out of date; {RERECORD}"


def test_execution_fixture_synthetic_recording_round_trips_byte_exactly(tmp_path: Path) -> None:
    corpus = _subset(tmp_path, "execution-runtime-network")
    spec = _spec("thorough")
    reviewer = spec.reviewer

    result = record_corpus(
        corpus,
        spec,
        responder_for=lambda case: SyntheticReviewer(case, driver=reviewer.driver, model=reviewer.model),
        recorded_with=RecordedWith("synthetic", reviewer.driver, reviewer.model),
    )

    assert result.ok and [path.name for path in result.execution_written] == [
        "execution-runtime-network.execution.json"
    ]
    for path in (*result.written, *result.execution_written):
        checked_in = GATE_DIR / "corpus" / "builtin" / "fixtures" / path.name
        assert path.read_bytes() == checked_in.read_bytes(), f"{path.name} is out of date; {RERECORD}"
    replay = replay_corpus(corpus, spec)
    assert replay.complete
    assert replay.selections["thorough"]["behavior"]["caught"]


def test_execution_fixture_replay_never_starts_a_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import runner_host as runtimes

    attempts: list[str] = []

    def refuse(name: str) -> Callable[..., Any]:
        def call(*_args: Any, **_kwargs: Any) -> Any:
            attempts.append(name)
            raise AssertionError(f"fixture replay reached process path {name}")

        return call

    # Capture reads the trusted base's .gitattributes with Git plumbing (read-tree into a scratch index, then
    # check-attr), which runs no filter, hook or reviewed code; every other process stays refused.
    real_run, real_popen = subprocess.run, subprocess.Popen
    plumbing: list[str] = []

    def is_plumbing(argv: Any) -> bool:
        # Exactly `read-tree <sha>` (no -u or -m: nothing is checked out, so no filter runs) and `check-attr --cached`.
        if not isinstance(argv, list) or argv[:2] != ["git", "-C"] or len(argv) < 5:
            return False
        if argv[3] == "read-tree":
            return len(argv) == 5 and re.fullmatch(r"[0-9a-f]{40,64}", argv[4]) is not None
        return argv[3] == "check-attr" and argv[4] == "--cached"

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if is_plumbing(argv):
            return real_run(argv, *args, **kwargs)  # starts its process through ``popen`` below
        return refuse("subprocess.run")(argv, *args, **kwargs)

    def popen(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if is_plumbing(argv):
            plumbing.append(argv[3])
            return real_popen(argv, *args, **kwargs)
        return refuse("subprocess.Popen")(argv, *args, **kwargs)

    monkeypatch.setattr(runtimes, "built_in_runner_registry", refuse("runner-registry"))
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)
    report = replay_corpus(_subset(tmp_path, "execution-runtime-network"), _spec("thorough"))

    assert report.complete
    assert attempts == []
    assert set(plumbing) <= {"read-tree", "check-attr"}
    assert report.selections["thorough"]["behavior"]["rate"] == 1.0


def test_missing_execution_fixture_is_incomplete_and_gate_is_stale(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.evaluation.gate import evaluate_gate

    corpus = _subset(tmp_path, "execution-test-failure")
    (corpus.fixtures_dir / "execution-test-failure.execution.json").unlink()
    report = replay_corpus(corpus, _spec("auto"))

    assert not report.complete
    assert any(event["status"] == "missing" for event in report.fixture_events())
    assert evaluate_gate(load_gate_config(GATE_DIR), report, mode="enforce").status == "stale"


def test_stale_execution_fixture_is_incomplete_and_never_reused(tmp_path: Path) -> None:
    corpus = _subset(tmp_path, "execution-test-failure")
    path = corpus.fixtures_dir / "execution-test-failure.execution.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["case_input_digest"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    report = replay_corpus(corpus, _spec("auto"))
    events = report.fixture_events()

    assert not report.complete
    assert any(event["status"] == "stale" and "case inputs changed" in event["reason"] for event in events)
    assert report.selections["auto"]["behavior"]["caught"] == []


def test_execution_fixture_request_digest_mutation_fails_closed(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.evaluation.corpus import digest

    corpus = _subset(tmp_path, "execution-runtime-network")
    path = corpus.fixtures_dir / "execution-runtime-network.execution.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    row = next(item for item in raw["runs"] if item["entry_id"] == "side_effects.runtime")
    row["unit_digest"] = "sha256:" + "f" * 64
    row["key"] = digest(
        {
            "entry_id": row["entry_id"],
            "side": row["side"],
            "unit_digest": row["unit_digest"],
            "variant": row["variant"],
            "case_input_digest": row["case_input_digest"],
        }
    )
    path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    report = replay_corpus(corpus, _spec("thorough"))

    assert not report.complete
    assert any("recipe or analysis digest changed" in event["reason"] for event in report.fixture_events())


def test_builtin_execution_cases_are_caught_by_their_product_entries(
    builtin: tuple[EvalReport, list[str]],
) -> None:
    report, _attempts = builtin
    records = {(run.case_id, run.selection): run for run in report.runs}
    expected = {
        ("execution-test-failure", "auto"): "checks.test",
        ("execution-test-outcome-flip", "thorough"): "behavior.test_diff",
        ("execution-runtime-network", "thorough"): "side_effects.runtime",
        ("execution-migration-failure", "deep"): "migrations.execute",
        ("execution-compat-mixed-version", "deep"): "compat.mixed_version",
    }
    for (case_id, selection), entry_id in expected.items():
        outcome = next(row for row in report.outcomes[selection] if row["case_id"] == case_id)
        assert outcome["behavior_caught"], (case_id, selection)
        observed = outcome["behavior_caught"][0]["observed"]
        behavior = next(row for row in records[(case_id, selection)].behavior if row.ref == observed)
        assert behavior.entry == entry_id


def test_builtin_replay_has_no_estimate_only_execution_rows(builtin: tuple[EvalReport, list[str]]) -> None:
    report, _attempts = builtin
    estimate_only = [
        (run.case_id, run.selection, row.id)
        for run in report.runs
        for row in run.plan
        if row.status == "run" and not row.executed and "estimated cost only" in row.note
    ]
    assert estimate_only == []


def test_gate_i_execution_fixture_harness_version_is_three() -> None:
    assert HARNESS_VERSION == 3


def test_failed_recording_keeps_old_fixtures_and_live_stops_at_the_first_failure(tmp_path: Path) -> None:
    corpus = _subset(tmp_path, "token-compare-timing", "cache-unbounded")
    before = {path.name: path.read_bytes() for path in corpus.fixtures_dir.glob("*.json")}
    attempts: list[str] = []

    def failing(case: Any) -> Callable[..., StructuredExecution]:
        def respond(**_kwargs: Any) -> StructuredExecution:
            attempts.append(case.id)
            raise RuntimeError("provider unavailable")

        return respond

    recorded_with = RecordedWith("synthetic", "openai", "synthetic-reviewer-v1")
    spec = _spec("quick")
    every = record_corpus(corpus, spec, responder_for=failing, recorded_with=recorded_with)
    first = record_corpus(corpus, spec, responder_for=failing, recorded_with=recorded_with, stop_on_failure=True)

    assert set(every.failed) == {"token-compare-timing", "cache-unbounded"} and every.written == ()
    assert list(first.failed) == ["token-compare-timing"]
    assert attempts.count("cache-unbounded") == 1  # only the first recording reached it
    assert {path.name: path.read_bytes() for path in corpus.fixtures_dir.glob("*.json")} == before


def test_fixture_for_changed_case_inputs_is_reported_stale_and_never_reused(tmp_path: Path) -> None:
    corpus = _subset(tmp_path, "token-compare-timing")
    case_file = corpus.root / "cases" / "token-compare-timing.yaml"
    case_file.write_text(
        case_file.read_text(encoding="utf-8").replace("return presented == expected", "return expected == presented"),
        encoding="utf-8",
    )
    corpus = load_corpus(corpus.root, case_ids=["token-compare-timing"])

    report = replay_corpus(corpus, _spec("quick"))
    (run,) = report.runs

    assert not report.complete
    assert [(event.status, event.reason) for event in run.fixture_events] == [
        ("stale", "case inputs changed since recording")
    ]
    assert run.findings == () and run.model_calls == ()
    assert report.selections["quick"]["incomplete"][0]["case"] == "token-compare-timing"


def test_prompt_template_change_is_reported_stale_and_never_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = _subset(tmp_path, "token-compare-timing")
    before = prompt_component()["digest"]
    original = ai_review._investigation_prompt

    def revised(*args: Any, **kwargs: Any) -> tuple[str, str]:
        system, prompt = original(*args, **kwargs)
        return system + "\nRevised investigation instructions.", prompt

    monkeypatch.setattr(ai_review, "_investigation_prompt", revised)
    report = replay_corpus(corpus, _spec("quick"))
    (run,) = report.runs

    assert prompt_component()["digest"] != before
    assert [(event.status, event.stage, event.reason) for event in run.fixture_events] == [
        ("stale", "finding_investigation", "prompt template (system prompt) changed")
    ]
    assert run.findings == () and run.model_calls == ()


def test_model_change_and_missing_fixtures_are_reported(tmp_path: Path) -> None:
    corpus = _subset(tmp_path, "token-compare-timing", "cache-unbounded")
    (corpus.fixtures_dir / "cache-unbounded.json").unlink()

    report = replay_corpus(corpus, _spec("quick", model="another-model"))
    events = {run.case_id: run.fixture_events for run in report.runs}

    assert events["cache-unbounded"][0].status == "missing"
    assert events["cache-unbounded"][0].reason == "no fixtures were recorded for this case"
    assert events["token-compare-timing"][0].status == "stale"
    assert events["token-compare-timing"][0].reason.startswith(
        "model changed: recorded with openai/synthetic-reviewer-v1"
    )
    assert all(run.findings == () for run in report.runs)


def test_live_recording_is_refused_in_ci_and_under_pytest() -> None:
    calls: list[dict[str, Any]] = []

    def underlying(**kwargs: Any) -> StructuredExecution:
        calls.append(kwargs)
        raise AssertionError("a provider must never be called")

    with pytest.raises(LiveRecordingRefused, match="pytest"):
        live_responder(LiveBudget(max_calls=1), underlying=underlying)
    with pytest.raises(LiveRecordingRefused, match="CI"):
        ensure_live_recording_allowed({"CI": "true"})
    ensure_live_recording_allowed({})
    assert calls == []


def test_live_budget_needs_a_positive_cap() -> None:
    with pytest.raises(ValueError, match="max-calls"):
        LiveBudget(max_calls=0)
    with pytest.raises(ValueError, match="max-usd"):
        LiveBudget(max_calls=1, max_usd=0)


def test_live_budget_stops_at_the_call_and_dollar_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    # Exercise the cap logic only: the guard is bypassed and the "provider" is a local fake.
    monkeypatch.setattr(eval_fixtures, "ensure_live_recording_allowed", lambda env=None: None)
    costs = iter([0.4, 0.4, 0.3, 0.3, None])
    calls: list[int] = []

    def fake_provider(**_kwargs: Any) -> StructuredExecution:
        calls.append(1)
        return StructuredExecution(
            result={}, driver="openai", model="m", usage=ModelCallUsage(calls=1, provider_cost=next(costs))
        )

    by_calls = live_responder(LiveBudget(max_calls=2), underlying=fake_provider)
    by_calls()
    by_calls()
    with pytest.raises(LiveBudgetExhausted, match="model-call budget of 2"):
        by_calls()
    assert len(calls) == 2

    budget = LiveBudget(max_calls=10, max_usd=0.5)
    by_dollars = live_responder(budget, underlying=fake_provider)
    by_dollars()
    by_dollars()  # the cap is checked between calls, so this one may overshoot it
    with pytest.raises(LiveBudgetExhausted, match=r"cost budget of \$0.50"):
        by_dollars()
    assert len(calls) == 4 and budget.spent_usd == pytest.approx(0.6)

    unpriced = live_responder(LiveBudget(max_calls=10, max_usd=1.0), underlying=fake_provider)
    with pytest.raises(LiveBudgetExhausted, match="did not report a cost"):
        unpriced()
