"""Per-part proof on every local revision (P11-1, OWNER-14 and OWNER-15): outcomes, pinning, bounds."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import part_proof as pp
from lemoncrow.pro.capabilities.review.test_impact import ChangedReach
from lemoncrow.pro.environment.runners import RunnerRegistry
from tests.gateway import test_review_check_runs_api as fixture
from tests.gateway.test_review_check_runs_api import _Scripted

# --------------------------------------------------------------------------- #
# pure derivation
# --------------------------------------------------------------------------- #


def _unit(file: str, head: str, klass: str = "unchanged") -> Any:
    return SimpleNamespace(file=file, head=head, klass=klass, subject=f"{file}::t")


def _differential(
    verdict: str,
    tests: tuple[Any, ...] = (),
    adequacy: tuple[Any, ...] = (),
    reasons: tuple[str, ...] = (),
    attempts: tuple[Any, ...] = (),
) -> Any:
    return SimpleNamespace(head_verdict=verdict, tests=tests, adequacy=adequacy, reasons=reasons, attempts=attempts)


def _stub_revision(head: str = "h1", base: str = "b1") -> Any:
    return SimpleNamespace(
        id="r1",
        review_id="p",
        range_mode="commit_range",
        head_sha=head,
        base_sha=base,
        source_fingerprint="",
        tree_fingerprint="",
    )


def _reach(**tests: tuple[str, ...]) -> ChangedReach:
    mapping = {path.replace("__", "/").replace("_py", ".py"): value for path, value in tests.items()}
    return ChangedReach(mapping, all_tests=tuple(sorted({item for row in mapping.values() for item in row})))


_REACH = ChangedReach(
    {"calc.py": ("tests/test_calc.py",), "other.py": ("tests/test_other.py",), "orphan.py": ()},
    all_tests=("tests/test_calc.py", "tests/test_other.py"),
)
_BATCH = ("tests/test_calc.py", "tests/test_other.py")


def test_every_outcome_derives_from_what_the_tests_did() -> None:
    adequate = SimpleNamespace(file="tests/test_calc.py", verdict="demonstrates")
    evidence = pp.evidence_from_differential(
        _differential(
            "FAIL",
            tests=(_unit("tests/test_calc.py", "passed"), _unit("tests/test_other.py", "failed")),
            adequacy=(adequate,),
        ),
        _BATCH,
    )
    rows = pp.derive_batch(_REACH, evidence, _BATCH)
    assert rows["calc.py"].outcome == pp.PROVEN  # fails on base, passes on head
    assert rows["other.py"].outcome == pp.FAILS
    assert "orphan.py" not in rows  # nothing reaches it: the record's default says "not tested"

    passing = pp.evidence_from_differential(_differential("PASS"), _BATCH)
    assert {path: row.outcome for path, row in pp.derive_batch(_REACH, passing, _BATCH).items()} == {
        "calc.py": pp.EXERCISED,
        "other.py": pp.EXERCISED,
    }


def test_a_reported_file_outside_the_selection_falls_back_to_the_test_id() -> None:
    """A runner may report a path that is no repository path (absolute, or its own); the test id still names it."""

    failing = SimpleNamespace(
        subject="tests.test_calc.CalcTest::test_add",
        file="/private/tmp/run/tests/test_calc.py",
        head="failed",
        klass="",
    )
    fixed = SimpleNamespace(subject="tests.test_other::test_x", file="tests.py", head="passed", klass="fixed")
    evidence = pp.evidence_from_differential(_differential("FAIL", tests=(failing, fixed)), _BATCH)
    assert evidence.failed == {"tests/test_calc.py"} and evidence.proven == {"tests/test_other.py"}
    rows = pp.derive_batch(_REACH, evidence, _BATCH)
    assert rows["calc.py"].outcome == pp.FAILS and rows["other.py"].outcome == pp.PROVEN


def test_a_part_whose_tests_never_ran_is_pending_never_exercised() -> None:
    passing = pp.evidence_from_differential(_differential("PASS"), ("tests/test_calc.py",))
    rows = pp.derive_batch(_REACH, passing, ("tests/test_calc.py",))
    assert rows["calc.py"].outcome == pp.EXERCISED
    assert rows["other.py"].outcome == pp.NOT_RUN_YET and rows["other.py"].reaching == 1
    over = pp.derive_batch(_REACH, passing, ("tests/test_calc.py",), pending=pp.OVER_BUDGET)
    assert over["other.py"].outcome == pp.OVER_BUDGET


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("bubblewrap is not installed", "no_sandbox"),
        ("refusing to run unsandboxed", "no_sandbox"),
        ("the runner exploded", "error"),
    ],
)
def test_a_run_that_could_not_start_is_couldnt_run_with_a_reason(reason: str, code: str) -> None:
    evidence = pp.evidence_from_differential(
        _differential("NOT_RUN", reasons=(reason,)),
        _BATCH,
    )
    rows = pp.derive_batch(_REACH, evidence, _BATCH)
    assert {row.outcome for row in rows.values()} == {pp.COULDNT_RUN}
    assert {row.reason for row in rows.values()} == {code}
    assert code in pp.REASONS


def test_a_failure_no_test_file_can_be_blamed_for_is_couldnt_run_not_a_pass() -> None:
    evidence = pp.evidence_from_differential(_differential("FAIL"), _BATCH)
    rows = pp.derive_batch(_REACH, evidence, _BATCH)
    assert rows["calc.py"].outcome == pp.COULDNT_RUN and rows["calc.py"].reason == "unattributed"


def test_unknown_reach_is_couldnt_run_with_its_own_reason() -> None:
    reach = ChangedReach({}, frozenset({"calc.py"}), "no test-impact map for this base (not built)")
    assert pp.derive_batch(reach, pp.TestEvidence(), ())["calc.py"] == pp.PartOutcome(pp.COULDNT_RUN, "no_map")
    assert pp.unknown_reason("more than 2000 changed paths") == "too_large"
    assert pp.unknown_reason("tests in languages outside the import graph") == "foreign_tests"


def test_a_failing_reaching_test_is_never_hidden_by_a_later_merge() -> None:
    revision = _stub_revision()
    failing = pp.merged(None, {"calc.py": pp.PartOutcome(pp.FAILS)}, revision=revision)
    later = pp.merged(failing, {"calc.py": pp.PartOutcome(pp.EXERCISED)}, revision=revision)
    assert later.files["calc.py"].outcome == pp.FAILS
    fresh = pp.merged(failing, {"calc.py": pp.PartOutcome(pp.EXERCISED)}, revision=revision, replace=True)
    assert fresh.files["calc.py"].outcome == pp.EXERCISED
    waiting = pp.merged(fresh, {"calc.py": pp.PartOutcome(pp.NOT_RUN_YET)}, revision=revision, replace=True)
    assert waiting.files["calc.py"].outcome == pp.EXERCISED  # a part a run did not reach keeps its earlier fact


def test_batches_run_the_parts_in_view_first_and_stop_at_the_bound() -> None:
    reach = ChangedReach(
        {f"src/m{i}.py": (f"tests/test_m{i}.py",) for i in range(10)},
        all_tests=tuple(f"tests/test_m{i}.py" for i in range(10)),
    )
    front, back = pp.plan_batches(reach, ["src/m7.py", "src/m3.py"], first=2, rest=3)
    assert front == ("tests/test_m7.py", "tests/test_m3.py")
    assert len(back) == 3 and not set(front) & set(back)
    done_front, done_back = pp.plan_batches(reach, ["src/m7.py"], ["tests/test_m7.py"], first=2, rest=3)
    assert "tests/test_m7.py" not in (*done_front, *done_back)
    # Tests past both bounds are pending, not silently dropped.
    evidence = pp.evidence_from_differential(_differential("PASS"), (*front, *back))
    rows = pp.derive_batch(reach, evidence, (*front, *back))
    assert sum(1 for row in rows.values() if row.outcome == pp.NOT_RUN_YET) == 10 - len(front) - len(back)


def test_not_tested_is_never_blank_and_never_a_pass() -> None:
    assert pp.NOT_TESTED in pp.LABELS and pp.LABELS[pp.NOT_TESTED] == "Not tested"
    assert pp.PartOutcome(pp.NOT_TESTED).to_wire() == {"o": "not_tested"}
    assert pp.NOT_TESTED not in {pp.PROVEN, pp.EXERCISED}
    for outcome in pp.OUTCOMES:
        assert pp.LABELS[outcome].strip()
    revision = _stub_revision()
    empty = pp.projection(revision, pp.merged(None, {}, revision=revision), fallback=pp.NOT_RUN_YET)
    assert empty["default"] == {"o": "not_tested"} and empty["reach"] == "file"
    unknown = pp.projection(revision, None, fallback=pp.COULDNT_RUN, fallback_reason="no_sandbox")
    assert unknown["default"] == {"o": "couldnt_run", "r": "no_sandbox"} and unknown["recorded"] is False
    # A stored row that is not a known outcome is dropped, never rendered blank.
    assert pp.PartOutcome.from_wire({"o": "pass"}) is None
    assert pp.ProofRecord.from_wire({"v": pp.VERSION, "default": "pass", "files": {}}) is None


def test_eligibility_is_declared_recipe_plus_sandbox_and_never_unsandboxed() -> None:
    declared = SimpleNamespace(kind="test", origin="config")
    discovered = SimpleNamespace(kind="test", origin="discovered")

    def catalog(*recipes: Any, base: str = "present") -> Any:
        return SimpleNamespace(
            base_state=base, configured=lambda: tuple(item for item in recipes if item.origin == "config")
        )

    assert pp.eligibility(catalog(declared), run_on="local", sandbox="seatbelt") == ([declared], "")
    assert pp.eligibility(catalog(declared), run_on="local", sandbox="")[1] == "no_sandbox"
    assert pp.eligibility(catalog(discovered), run_on="local", sandbox="seatbelt")[1] == "no_recipe"
    assert pp.eligibility(catalog(declared), run_on="hosted", sandbox="seatbelt")[1] == "hosted"
    assert pp.eligibility(catalog(declared, base="provisional"), run_on="local", sandbox="seatbelt")[1] == (
        "base_unreadable"
    )


def test_a_run_is_credited_only_with_the_test_files_its_report_names() -> None:
    from lemoncrow.pro.capabilities.review.part_proof_api import _selected
    from lemoncrow.pro.capabilities.review.recipes import CheckRecipe

    recipe = CheckRecipe(id="unit", kind="test", command=("run",), report="junit", origin="config")
    one_file = SimpleNamespace(
        economy={},
        recipe=recipe,
        tests=(SimpleNamespace(subject="tests.test_calc::test_add", file="", head="passed", klass="unchanged"),),
    )
    assert _selected(one_file, ChangedReach({}, all_tests=("tests/test_other.py",))) == []  # maps to no known test file
    reach = ChangedReach({"calc.py": ("tests/test_calc.py",)}, all_tests=("tests/test_calc.py", "tests/test_other.py"))
    assert _selected(one_file, reach) == ["tests/test_calc.py"]  # never "every test"
    impact = SimpleNamespace(
        economy={"selection": {"mode": "impact", "tests": ["tests/test_other.py"]}}, recipe=recipe, tests=()
    )
    assert _selected(impact, reach) == ["tests/test_other.py"]


# --------------------------------------------------------------------------- #
# end to end through the Review API with a scripted runner (no real sandbox)
# --------------------------------------------------------------------------- #

_HEAD = {
    "calc.py": fixture.CALC_HEAD,
    "tests/test_calc.py": fixture.TESTS_HEAD,
    "orphan.py": "VALUE = 1\n",
}
_PASS = {"test_add": "passed", "test_sub": "passed", "test_mul": "passed", "test_neg": "passed"}


@pytest.fixture(autouse=True)
def _sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "local_sandbox", lambda: "seatbelt")
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", "/nonexistent/sg")


@pytest.fixture(autouse=True)
def _drain_background_runs() -> Any:
    """A test that schedules a background run must not leave one behind, even when it fails.

    A stray queued thread would otherwise outlive its test, hold the module's slots and starve the next one
    (a regression then hangs the file instead of failing a test). After each test wait briefly for the
    scheduled runs to finish, then clear the bookkeeping and give the next test fresh slots.
    """

    import threading
    import time

    from lemoncrow.pro.capabilities.review import part_proof_api as api

    yield
    deadline = time.monotonic() + 5.0
    while (api._SCHEDULED or api._RUNNING) and time.monotonic() < deadline:
        time.sleep(0.02)
    stuck = [f"still scheduled or running after 5 s: {sorted({*api._SCHEDULED, *api._RUNNING})}"] * bool(
        api._SCHEDULED or api._RUNNING
    )
    # A regression that deadlocks on the lock must fail its own test, not hang this teardown: a lock that
    # cannot be taken is replaced, like the slots, so the next test starts clean.
    if api._RUNNING_LOCK.acquire(timeout=2.0):
        try:
            api._SCHEDULED.clear()
            api._RUNNING.clear()
        finally:
            api._RUNNING_LOCK.release()
    else:
        stuck.append("the running lock could not be taken: a background run is deadlocked on it")
        api._RUNNING_LOCK = threading.Lock()
        api._SCHEDULED.clear()
        api._RUNNING.clear()
    api._BACKGROUND_SLOTS = threading.BoundedSemaphore(2)
    if stuck:  # cleaned up above so the next test starts fresh, but a stuck run must not pass silently
        pytest.fail("; ".join(stuck), pytrace=False)


def _review(
    tmp_path: Path, script: Any, config: str = fixture.CONFIG, run_on: str = ""
) -> tuple[SimpleNamespace, _Scripted]:
    runner = _Scripted(script)
    review = fixture._review(
        tmp_path,
        base=fixture._base_files(config),
        head=dict(_HEAD),
        runner_registry=RunnerRegistry((runner,), run_on=run_on),
    )
    return review, runner


def _proof(review: SimpleNamespace, revision_id: str = "") -> dict[str, Any]:
    response = review.client.get(
        f"/api/reviews/{review.review_id}/part-proof",
        headers=fixture.HEADERS,
        params={"revision_id": revision_id} if revision_id else {},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def _auto(review: SimpleNamespace, in_view: list[str] | None = None, revision_id: str = "") -> dict[str, Any]:
    response = review.client.post(
        f"/api/reviews/{review.review_id}/part-proof/run",
        headers=fixture.HEADERS,
        json={"revision_id": revision_id or review.revision_id, "in_view": in_view or []},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def _outcome(payload: dict[str, Any], path: str) -> str:
    return str((payload["files"].get(path) or payload["default"])["o"])


def _fails_on_base(runtime_id: str, side: str, _count: int) -> tuple[dict[str, str], int]:
    # The head passes; the head's test files fail against the base source (the adequacy probe).
    if runtime_id.endswith(":probe") and side == "old":
        return {"test_add": "passed", "test_sub": "failed", "test_neg": "failed", "test_add_zero": "passed"}, 50
    return dict(_PASS), 50


def _passes_everywhere(runtime_id: str, side: str, count: int) -> tuple[dict[str, str], int]:
    return dict(_PASS), 50


def test_a_test_that_fails_on_base_and_passes_on_head_proves_the_part(tmp_path: Path) -> None:
    review, runner = _review(tmp_path, _fails_on_base)
    before = _proof(review)
    assert before["default"] == {"o": "not_run_yet"} and before["recorded"] is False  # a run is coming, not blank
    after = _auto(review, ["calc.py"])
    assert after["recorded"] is True and after["source"] == "auto" and after["reach"] == "file"
    assert _outcome(after, "calc.py") == "proven"
    assert after["files"]["calc.py"]["t"] == ["tests/test_calc.py"]
    assert _outcome(after, "orphan.py") == "not_tested"  # no test reaches it: the labelled default, not blank
    assert after["labels"]["not_tested"] == "Not tested"
    assert _proof(review) == {**after}  # the batched read serves the stored record
    sides = {(runtime, side) for runtime, side, _ in runner.calls}
    assert ("recipe:unit", "old") in sides and ("recipe:unit", "new") in sides  # base and head both ran


def test_a_passing_reaching_test_is_exercised(tmp_path: Path) -> None:
    review, _runner = _review(tmp_path, _passes_everywhere)
    payload = _auto(review)
    assert _outcome(payload, "calc.py") == "exercised"
    assert _outcome(payload, "orphan.py") == "not_tested"


def test_a_failing_reaching_test_is_fails(tmp_path: Path) -> None:
    def failing(runtime_id: str, side: str, count: int) -> tuple[dict[str, str], int]:
        cases = dict(_PASS)
        if side == "new" and not runtime_id.endswith(":probe"):
            cases["test_sub"] = "failed"
        return cases, 50

    review, _runner = _review(tmp_path, failing)
    assert _outcome(_auto(review), "calc.py") == "fails"


def test_without_a_sandbox_nothing_runs_and_the_reason_is_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pp, "local_sandbox", lambda: "")
    review, runner = _review(tmp_path, _passes_everywhere)
    read = _proof(review)
    assert read["default"] == {"o": "couldnt_run", "r": "no_sandbox"}
    ran = _auto(review, ["calc.py"])
    assert ran["default"] == {"o": "couldnt_run", "r": "no_sandbox"} and ran["recorded"] is False
    assert runner.calls == []  # never run unsandboxed automatically
    assert ran["reasons"]["no_sandbox"]


def test_a_repo_with_no_declared_test_recipe_is_couldnt_run_not_blank(tmp_path: Path) -> None:
    review, runner = _review(tmp_path, _passes_everywhere, config="check_policy:\n  reruns: 1\n")
    assert _auto(review)["default"] == {"o": "couldnt_run", "r": "no_recipe"}
    assert runner.calls == []


def test_a_review_out_of_minutes_runs_nothing_and_says_so(tmp_path: Path) -> None:
    review, runner = _review(
        tmp_path, _passes_everywhere, config=fixture.CONFIG + "review:\n  budget:\n    per_review_minutes: 1\n"
    )
    payload = _auto(review, ["calc.py"])
    assert runner.calls == []
    assert _outcome(payload, "calc.py") == "over_budget"
    assert payload["labels"]["over_budget"] == "Not run: over this review's budget"


def test_a_new_revision_never_shows_an_older_record(tmp_path: Path) -> None:
    review, _runner = _review(tmp_path, _passes_everywhere)
    first = _auto(review)
    assert first["recorded"] is True
    second_id = fixture._new_snapshot(review, {"calc.py": fixture.CALC_HEAD + "\n# follow-up\n"})
    second = _proof(review, second_id)
    assert second["revision_id"] == second_id and second["recorded"] is False
    assert second["files"] == {} and second["default"] == {"o": "not_run_yet"}
    assert second["head_identity"] != first["head_identity"]
    assert _proof(review, review.revision_id)["files"] == first["files"]  # the first still has its own


def test_a_result_from_another_commit_is_refused_and_never_served(tmp_path: Path) -> None:
    review, _runner = _review(tmp_path, _passes_everywhere)
    first_record = pp.read_records(review.store, [_revision(review, review.revision_id)])
    assert first_record == {}
    _auto(review)
    older = _revision(review, review.revision_id)
    stored = pp.read_records(review.store, [older])[review.revision_id]
    second_id = fixture._new_snapshot(review, {"calc.py": fixture.CALC_HEAD + "\n# follow-up\n"})
    newer = _revision(review, second_id)
    with pytest.raises(pp.ProofCommitMismatch):
        pp.write_record(review.store, newer, stored)
    # Even a row planted under the newer revision's stamp is dropped on read: it names another commit.
    from lemoncrow.pro.capabilities.review.store import DerivedResult

    review.store.put_derived_results(
        [
            DerivedResult(
                review_id=str(newer.review_id),
                revision_id=str(newer.id),
                kind=pp.KIND,
                scope_key=pp.SCOPE,
                stamp=pp.stamp_of(newer),
                payload=stored.to_wire(),
            )
        ]
    )
    assert pp.read_records(review.store, [newer]) == {}
    assert _proof(review, second_id)["recorded"] is False


def test_a_historical_revision_never_runs_on_its_own(tmp_path: Path) -> None:
    review, runner = _review(tmp_path, _passes_everywhere)
    fixture._new_snapshot(review, {"calc.py": fixture.CALC_HEAD + "\n# follow-up\n"})
    payload = _auto(review, revision_id=review.revision_id)
    assert payload["default"] == {"o": "couldnt_run", "r": "historical"}
    assert runner.calls == []


def test_a_reviewers_run_writes_the_same_record_in_the_same_vocabulary(tmp_path: Path) -> None:
    review, _runner = _review(tmp_path, _fails_on_base)
    picked = fixture._run(review, "checks.recipe:unit", pick=True)
    assert picked.status_code == 200, picked.text
    assert picked.json()["part_proof"] == {"recorded": True, "parts": picked.json()["part_proof"]["parts"]}
    payload = _proof(review)
    assert payload["recorded"] is True and payload["source"] == "check_run"
    assert _outcome(payload, "orphan.py") == "not_tested"
    assert set(payload["labels"]) == set(pp.OUTCOMES)  # one vocabulary


def _revision(review: SimpleNamespace, revision_id: str) -> Any:
    return review.store.get_revision(revision_id)


# --------------------------------------------------------------------------- #
# fix round 1: pure rules (M1, M2, m3, m5, m9, disabled)
# --------------------------------------------------------------------------- #


def _changed(changes: dict[str, str | None], impact: Any = None, **kwargs: Any) -> ChangedReach:
    from lemoncrow.pro.capabilities.review.test_impact import HeadChange, changed_reach
    from tests.gateway import test_review_test_impact as impact_fixture

    rows = [HeadChange.of(path, text, deleted=text is None) for path, text in changes.items()]
    return changed_reach(
        impact_fixture._map() if impact is None else impact, rows, base_sha=impact_fixture.BASE, **kwargs
    )


def test_a_changed_fixture_or_data_file_is_couldnt_run_never_not_tested() -> None:
    """M1: a file a test may read at run time is unknown, exactly as select_recipe_tests treats it."""

    reach = _changed({"src/pkg/a.py": "def a():\n    return 3\n", "data/fixture.json": "{}", "data/seed.sql": "x"})
    assert reach.tests["src/pkg/a.py"] == ("tests/test_a.py",)
    assert {"data/fixture.json", "data/seed.sql"} <= reach.unknown
    assert "data/fixture.json" not in reach.tests  # it never reads as "reached by nothing"
    rows = pp.derive_batch(reach, pp.TestEvidence(), ())
    assert rows["data/fixture.json"] == pp.PartOutcome(pp.COULDNT_RUN, "runtime_read")
    assert rows["data/seed.sql"].reason == "runtime_read" and "runtime_read" in pp.REASONS
    # A change of only such files decides nothing as a whole: the default stays "not tested" for source files.
    only = _changed({"data/fixture.json": "{}"})
    assert pp.undecided_reason(only) == ""
    assert pp.derive_batch(only, pp.TestEvidence(), ())["data/fixture.json"].reason == "runtime_read"


def test_reach_mirrors_the_recipes_declared_inputs_and_import_shadowing() -> None:
    plain = _changed({"tools/run.sh": "x"})
    assert plain.reasons["tools/run.sh"] == "runtime_read"
    declared = _changed({"tools/run.sh": "x"}, declared={"tools/run.sh"})
    assert (
        set(declared.tests["tools/run.sh"]) == set(declared.all_tests) and declared.all_tests
    )  # every test reaches it
    from tests.gateway import test_review_test_impact as impact_fixture

    unresolved = dict(impact_fixture.FILES, **{"tests/test_new.py": "import pkg.missing\n"})
    shadow = _changed({"src/pkg/missing.py": ""}, impact=impact_fixture._map(unresolved))
    assert shadow.reasons["src/pkg/missing.py"] == "shadowing"
    assert pp.derive_batch(shadow, pp.TestEvidence(), ())["src/pkg/missing.py"] == pp.PartOutcome(
        pp.COULDNT_RUN, "shadowing"
    )


def test_a_deleted_file_makes_no_test_claim_and_says_so() -> None:
    """m9: a deleted changed file is explicit, not "Not tested"."""

    reach = _changed({"src/pkg/b.py": None, "src/pkg/a.py": "def a():\n    return 3\n"})
    assert reach.deleted == frozenset({"src/pkg/b.py"}) and "src/pkg/b.py" not in reach.tests
    rows = pp.derive_batch(reach, pp.TestEvidence(), ())
    assert rows["src/pkg/b.py"] == pp.PartOutcome(pp.COULDNT_RUN, "deleted")
    assert "deleted" in pp.REASONS


def test_a_head_only_run_never_downgrades_a_proven_part_but_fails_still_overrides() -> None:
    """M2: exercised never replaces proven for the same base and head identity."""

    revision = _stub_revision()
    proven = pp.merged(None, {"calc.py": pp.PartOutcome(pp.PROVEN, "", ("tests/test_calc.py",))}, revision=revision)
    head_only = pp.merged(proven, {"calc.py": pp.PartOutcome(pp.EXERCISED)}, revision=revision, replace=True)
    assert head_only.files["calc.py"].outcome == pp.PROVEN
    failing = pp.merged(head_only, {"calc.py": pp.PartOutcome(pp.FAILS)}, revision=revision, replace=True)
    assert failing.files["calc.py"].outcome == pp.FAILS
    # The proof is pinned to the exact identity: another head starts fresh and may simply be exercised.
    other = _stub_revision(head="h2")
    moved = pp.merged(proven, {"calc.py": pp.PartOutcome(pp.EXERCISED)}, revision=other, replace=True)
    assert moved.files["calc.py"].outcome == pp.EXERCISED


def test_a_flaky_head_test_is_couldnt_run_flaky_not_a_pass_and_not_a_failure() -> None:
    """m5: follow the check-run vocabulary: flaky is neither passed nor failed."""

    flaky = pp.evidence_from_differential(
        _differential("PASS", tests=(_unit("tests/test_calc.py", "flaky"), _unit("tests/test_other.py", "passed"))),
        _BATCH,
    )
    assert flaky.flaky == frozenset({"tests/test_calc.py"}) and "tests/test_calc.py" not in flaky.passed
    rows = pp.derive_batch(_REACH, flaky, _BATCH)
    assert rows["calc.py"] == pp.PartOutcome(pp.COULDNT_RUN, "flaky", ("tests/test_calc.py",), 1)
    assert rows["other.py"].outcome == pp.EXERCISED
    # Another test file that reaches the part passes cleanly: the part is exercised by it.
    wide = ChangedReach({"calc.py": _BATCH}, all_tests=_BATCH)
    both = pp.evidence_from_differential(
        _differential("PASS", tests=(_unit("tests/test_calc.py", "flaky"), _unit("tests/test_other.py", "passed"))),
        _BATCH,
    )
    assert pp.derive_batch(wide, both, _BATCH)["calc.py"].outcome == pp.EXERCISED
    assert "flaky" in pp.REASONS


def test_a_failing_test_past_the_two_hundredth_selected_still_fails_its_part() -> None:
    """m5: the stored selection is truncated to 200; the run's own untruncated list is what is credited."""

    from lemoncrow.pro.capabilities.review.part_proof_api import _selected
    from lemoncrow.pro.capabilities.review.recipes import CheckRecipe

    recipe = CheckRecipe(id="unit", kind="test", command=("run", "{tests}"), report="junit", origin="config")
    names = tuple(f"tests/test_{index:03d}.py" for index in range(250))
    reach = ChangedReach({"calc.py": (names[249],)}, all_tests=names)
    differential = SimpleNamespace(
        economy={"selection": {"mode": "impact", "tests": list(names[:200]), "selected": 250}},
        recipe=recipe,
        selected_files=names,
        head_verdict="FAIL",
        tests=(SimpleNamespace(subject="x", file=names[249], head="failed", klass="unchanged"),),
        adequacy=(),
        reasons=(),
        attempts=(),
    )
    selected = _selected(differential, reach)
    assert names[249] in selected and len(selected) == 250
    evidence = pp.evidence_from_differential(differential, selected)
    assert pp.derive_batch(reach, evidence, selected)["calc.py"].outcome == pp.FAILS


def test_eligibility_honours_the_per_part_proof_switch() -> None:
    declared = SimpleNamespace(kind="test", origin="config")
    off = SimpleNamespace(
        base_state="present", configured=lambda: (declared,), policy=SimpleNamespace(part_proof=False)
    )
    on = SimpleNamespace(base_state="present", configured=lambda: (declared,), policy=SimpleNamespace(part_proof=True))
    assert pp.eligibility(off, run_on="local", sandbox="seatbelt") == ([], "disabled")
    assert pp.eligibility(on, run_on="local", sandbox="seatbelt")[1] == ""
    from lemoncrow.pro.capabilities.review.recipes import CheckPolicy, parse_check_policy

    assert CheckPolicy().part_proof is True
    assert parse_check_policy("check_policy:\n  part_proof: false\n")[0].part_proof is False
    assert parse_check_policy("check_policy:\n  part_proof: maybe\n")[0].part_proof is True  # invalid: the default
    assert "disabled" in pp.REASONS


def test_a_concurrent_writer_never_loses_a_batch(tmp_path: Path) -> None:
    """m3: the write is a compare-and-set on the row stamp, retried from the newer row."""

    review, _runner = _review(tmp_path, _passes_everywhere)
    revision = _revision(review, review.revision_id)
    seen: list[int | None] = []

    def change(old: pp.ProofRecord | None) -> pp.ProofRecord:
        seen.append(old.version if old is not None else None)
        if len(seen) == 1:  # another process lands its batch between our read and our write
            pp.update_record(
                review.store,
                revision,
                lambda other: pp.merged(other, {"other.py": pp.PartOutcome(pp.EXERCISED)}, revision=revision),
            )
        return pp.merged(old, {"calc.py": pp.PartOutcome(pp.PROVEN)}, revision=revision)

    record = pp.update_record(review.store, revision, change)
    assert seen == [None, 1] and record.version == 2
    assert {path: row.outcome for path, row in record.files.items()} == {"calc.py": pp.PROVEN, "other.py": pp.EXERCISED}
    assert pp.read_records(review.store, [revision])[str(revision.id)].files.keys() == {"calc.py", "other.py"}


def test_a_write_that_keeps_losing_the_race_is_refused_not_silently_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review, _runner = _review(tmp_path, _passes_everywhere)
    revision = _revision(review, review.revision_id)
    pp.update_record(review.store, revision, lambda old: pp.merged(old, {}, revision=revision))
    monkeypatch.setattr(review.store, "compare_and_set_derived_result", lambda *args, **kwargs: False)
    with pytest.raises(RuntimeError, match="being written by another run"):
        pp.update_record(review.store, revision, lambda old: pp.merged(old, {}, revision=revision))


# --------------------------------------------------------------------------- #
# fix round 1: several parts, batches and recipes end to end (B1, m1, m2, m9, not_run_yet at scale)
# --------------------------------------------------------------------------- #

_PARTS = ("alpha", "beta", "gamma")


def _multi(
    tmp_path: Path,
    script: Any,
    *,
    config: str = fixture.CONFIG,
    extra_base: dict[str, str] | None = None,
    extra_head: dict[str, str | None] | None = None,
) -> tuple[SimpleNamespace, _Scripted]:
    """Three changed modules, each reached by its own test file (plus calc and its tests unchanged)."""

    base = fixture._base_files(config)
    for name in _PARTS:
        base[f"{name}.py"] = "VALUE = 1\n"
        base[f"tests/test_{name}.py"] = f"from {name} import VALUE\n\n\ndef test_{name}():\n    assert VALUE\n"
    base.update(extra_base or {})
    head: dict[str, str | None] = {f"{name}.py": "VALUE = 2\n" for name in _PARTS}
    head.update(extra_head or {})
    runner = _Scripted(script)
    review = fixture._review(tmp_path, base=base, head=head, runner_registry=RunnerRegistry((runner,)))
    return review, runner


def _record(review: SimpleNamespace) -> pp.ProofRecord | None:
    return pp.read_records(review.store, [_revision(review, review.revision_id)]).get(review.revision_id)


def _two_batches(monkeypatch: pytest.MonkeyPatch, first: int = 1, rest: int = 2) -> None:
    from lemoncrow.pro.capabilities.review import part_proof_api as api

    original = pp.plan_batches
    monkeypatch.setattr(
        api.pp,
        "plan_batches",
        lambda reach, in_view, ran=(), **_kw: original(reach, in_view, ran, first=first, rest=rest),
    )


def test_a_part_a_pending_test_reaches_never_reads_not_tested_while_the_run_is_underway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1(a): the tests of parts 2 and 3 are in a later batch; they wait as "not run yet", never "not tested"."""

    _two_batches(monkeypatch)
    seen: list[tuple[bool, dict[str, str]]] = []
    holder: dict[str, SimpleNamespace] = {}

    def script(runtime_id: str, side: str, count: int) -> tuple[dict[str, str], int]:
        record = _record(holder["review"])
        assert record is not None, "the record must exist before the first batch runs"
        seen.append(
            (
                record.complete,
                {name: (record.files.get(f"{name}.py") or pp.PartOutcome(pp.NOT_TESTED)).outcome for name in _PARTS},
            )
        )
        return dict(_PASS), 50

    review, runner = _multi(tmp_path, script)
    holder["review"] = review
    final = _auto(review, ["alpha.py"])
    assert len(runner.calls) >= 4  # two batches, base and head each
    assert seen and all(not complete for complete, _ in seen[:2])  # still running: incomplete
    for _complete, outcomes in seen:
        assert pp.NOT_TESTED not in outcomes.values(), outcomes
    assert {_outcome(final, f"{name}.py") for name in _PARTS} == {"exercised"}
    assert final["complete"] is True and final["running"] is False


def test_a_run_that_stops_between_batches_leaves_a_record_the_next_visit_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1(b): a crash after the first batch leaves waiting rows and an incomplete record, never recorded-and-done."""

    from lemoncrow.pro.capabilities.review import part_proof_api as api

    _two_batches(monkeypatch)
    review, runner = _multi(tmp_path, lambda *_args: (dict(_PASS), 50))
    real = api._run_one
    calls: list[tuple[str, ...]] = []

    def crashing(*args: Any, **kwargs: Any) -> Any:
        calls.append(tuple(args[5]))
        if len(calls) == 2:
            raise RuntimeError("the machine went to sleep")
        return real(*args, **kwargs)

    monkeypatch.setattr(api, "_run_one", crashing)
    response = review.client.post(
        f"/api/reviews/{review.review_id}/part-proof/run",
        headers=fixture.HEADERS,
        json={"revision_id": review.revision_id, "in_view": ["alpha.py"]},
    )
    assert response.status_code == 500
    stopped = _proof(review)
    assert stopped["recorded"] is True and stopped["complete"] is False and stopped["running"] is False
    assert _outcome(stopped, "alpha.py") == "exercised"
    assert _outcome(stopped, "beta.py") == "not_run_yet" and _outcome(stopped, "gamma.py") == "not_run_yet"
    monkeypatch.setattr(api, "_run_one", real)
    resumed = _auto(review, ["alpha.py"])
    assert resumed["complete"] is True
    assert {_outcome(resumed, f"{name}.py") for name in _PARTS} == {"exercised"}
    first_calls = len(runner.calls)
    again = _auto(review)  # nothing left to run for a complete record's parts
    assert again["complete"] is True and len(runner.calls) >= first_calls


def test_a_reviewers_run_of_one_recipe_leaves_the_other_recipes_parts_waiting_not_untested(tmp_path: Path) -> None:
    """B1(c): reach is computed for every configured test recipe, so the other recipe's files are not "Not tested"."""

    config = fixture.CONFIG.replace(
        "check_recipes:\n",
        "check_recipes:\n"
        "  - id: api\n    kind: test\n    command: python3 run_tests.py {report}\n    report: junit\n    cwd: api\n"
        "  - id: web\n    kind: test\n    command: python3 run_tests.py {report}\n    report: junit\n    cwd: web\n",
        1,
    )
    review, _runner = _multi(
        tmp_path,
        lambda *_args: (dict(_PASS), 50),
        config=config,
        extra_base={
            "api/svc.py": "VALUE = 1\n",
            "api/tests/test_svc.py": "from api.svc import VALUE\n",
            "web/ui.py": "VALUE = 1\n",
            "web/tests/test_ui.py": "from web.ui import VALUE\n",
        },
        extra_head={"api/svc.py": "VALUE = 2\n", "web/ui.py": "VALUE = 2\n"},
    )
    picked = fixture._run(review, "checks.recipe:api", pick=True)
    assert picked.status_code == 200, picked.text
    payload = _proof(review)
    assert payload["recorded"] is True and payload["complete"] is False
    # The scripted report names only tests/test_calc.py, so the api run decides nothing here; what matters is that
    # the web recipe's file is waiting (its tests reach it and have not run), not "not tested", and the record is open.
    assert _outcome(payload, "web/ui.py") == "not_run_yet"
    assert _outcome(payload, "api/svc.py") != "not_tested"
    done = _auto(review)
    assert done["complete"] is True and _outcome(done, "web/ui.py") in {"exercised", "proven"}
    assert _outcome(done, "api/svc.py") in {"exercised", "proven"}


def test_the_automatic_run_is_not_a_reviewers_pick_and_never_forces_a_skipped_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m1: no ManualPick is recorded for the automatic proof, and an entry the plan skips is "not planned"."""

    from lemoncrow.pro.capabilities.review.run_plan_service import RunPlanService

    amended: list[Any] = []
    real = RunPlanService.amend

    def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
        amended.append(args)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(RunPlanService, "amend", spy)
    review, runner = _review(tmp_path, _passes_everywhere)
    assert _outcome(_auto(review), "calc.py") == "exercised"
    assert amended == []  # the plan was never amended: not a reviewer's pick
    assert runner.calls

    from lemoncrow.pro.capabilities.review import planned_execution

    def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise planned_execution.PlannedExecutionRefused("this recipe is skipped")

    other, other_runner = _review(tmp_path / "skipped", _passes_everywhere)
    monkeypatch.setattr(planned_execution, "admit_planned_execution", refused)
    payload = _auto(other)
    assert other_runner.calls == []  # nothing ran, and nothing was forced onto the plan
    assert payload["files"]["calc.py"] == {"o": "couldnt_run", "r": "not_planned", "n": 1}
    assert "not_planned" in payload["reasons"]
    assert amended == []


def test_without_a_review_ceiling_the_proposed_default_bound_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m2: a review that sets no per_review_minutes still has a fail-closed bound."""

    from lemoncrow.pro.capabilities.review.run_plan import PROPOSED_BUDGET_DEFAULTS, BudgetUsage
    from lemoncrow.pro.capabilities.review.run_plan_service import RunPlanService

    spent = float(PROPOSED_BUDGET_DEFAULTS["per_review_minutes"])
    review, runner = _review(tmp_path, _passes_everywhere)
    monkeypatch.setattr(RunPlanService, "usage", lambda self, session, revision: BudgetUsage(review_minutes=spent))
    payload = _auto(review, ["calc.py"])
    assert runner.calls == []
    assert _outcome(payload, "calc.py") == "over_budget"
    under, under_runner = _review(tmp_path / "under", _passes_everywhere)
    monkeypatch.setattr(
        RunPlanService, "usage", lambda self, session, revision: BudgetUsage(review_minutes=spent - 5.0)
    )
    assert _outcome(_auto(under), "calc.py") == "exercised" and under_runner.calls


def test_a_recording_failure_never_fails_a_completed_check_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """m4: the check ran; a failure to record its part proof is logged and returns part_proof null, not a 500."""

    from lemoncrow.pro.capabilities.review import part_proof_api

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the store is locked")

    monkeypatch.setattr(part_proof_api, "record_check_run", broken)
    review, runner = _review(tmp_path, _passes_everywhere)
    response = fixture._run(review, "checks.recipe:unit", pick=True)
    assert response.status_code == 200, response.text
    assert response.json()["part_proof"] is None and response.json()["verdicts"]["checks.recipe:unit"] == "PASS"
    assert runner.calls


def test_a_reviewers_head_only_run_keeps_a_part_proven(tmp_path: Path) -> None:
    """M2 end to end: Run test after the automatic proof does not turn "Proven" into "Exercised"."""

    review, _runner = _review(tmp_path, _fails_on_base)
    assert _outcome(_auto(review, ["calc.py"]), "calc.py") == "proven"
    again = fixture._run(review, "checks.recipe:unit", pick=True)
    assert again.status_code == 200, again.text
    assert _outcome(_proof(review), "calc.py") == "proven"


def test_a_deleted_changed_file_is_explicit_not_not_tested(tmp_path: Path) -> None:
    """m9, end to end: a file the change deletes has its own outcome and reason."""

    review, _runner = _multi(
        tmp_path,
        lambda *_args: (dict(_PASS), 50),
        extra_base={"legacy.py": "OLD = 1\n"},
        extra_head={"legacy.py": None},
    )
    payload = _auto(review)
    assert payload["files"]["legacy.py"] == {"o": "couldnt_run", "r": "deleted"}


def test_a_changed_fixture_file_is_couldnt_run_end_to_end(tmp_path: Path) -> None:
    review, _runner = _multi(
        tmp_path,
        lambda *_args: (dict(_PASS), 50),
        extra_base={"data/fixture.json": "{}\n"},
        extra_head={"data/fixture.json": '{"a": 1}\n'},
    )
    payload = _auto(review)
    assert payload["files"]["data/fixture.json"] == {"o": "couldnt_run", "r": "runtime_read"}
    assert _outcome(payload, "alpha.py") == "exercised"


def test_a_large_change_beyond_the_batch_bounds_says_so_for_what_it_will_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large change: tests past both bounds are labelled "couldn't run: past the bound", never "not tested", never run unbounded."""

    monkeypatch.setattr(pp, "plan_batches", _bounded(first=1, rest=1))
    review, runner = _multi(tmp_path, lambda *_args: (dict(_PASS), 50))
    payload = _auto(review, ["alpha.py"])
    outcomes = {name: _outcome(payload, f"{name}.py") for name in _PARTS}
    assert outcomes["alpha"] == "exercised" and outcomes["beta"] == "exercised"
    # Past both bounds on a finished record: nothing runs it automatically, so it must not promise a later run.
    assert payload["files"]["gamma.py"] == {"o": "couldnt_run", "r": "past_bound", "n": 1}
    assert payload["reasons"]["past_bound"] and "Run test" in payload["reasons"]["past_bound"]
    assert payload["labels"]["couldnt_run"] == "Couldn't run" and payload["labels"]["not_run_yet"] == "Not run yet"
    assert len({call[:2] for call in runner.calls}) <= 2 and payload["complete"] is True


def _bounded(first: int, rest: int) -> Any:
    original = pp.plan_batches
    return lambda reach, in_view, ran=(), **_kw: original(reach, in_view, ran, first=first, rest=rest)


# --------------------------------------------------------------------------- #
# fix round 1: the local capture trigger (M4)
# --------------------------------------------------------------------------- #


def _wait_complete(review: SimpleNamespace, seconds: float = 20.0) -> pp.ProofRecord | None:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        record = _record(review)
        if record is not None and record.complete:
            return record
        time.sleep(0.05)
    return _record(review)


def _schedule(review: SimpleNamespace) -> dict[str, Any]:
    scheduler = review.client.app.state.review_part_proof_scheduler
    return dict(scheduler(review.review_id))


def test_a_local_capture_starts_the_proof_in_the_background_without_a_reader(tmp_path: Path) -> None:
    review, runner = _review(tmp_path, _fails_on_base)
    queued = _schedule(review)
    assert queued["status"] == "queued" and queued["revision_id"] == review.revision_id
    record = _wait_complete(review)
    assert record is not None and record.complete and record.source == "auto"
    assert record.files["calc.py"].outcome == pp.PROVEN
    assert runner.calls
    assert _schedule(review)["status"] == "done"  # a complete record is never run again


@pytest.mark.parametrize("case", ["no_sandbox", "no_recipe", "hosted", "disabled"])
def test_a_capture_never_starts_a_proof_it_may_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    config = fixture.CONFIG
    run_on = ""
    if case == "no_sandbox":
        monkeypatch.setattr(pp, "local_sandbox", lambda: "")
    elif case == "no_recipe":
        config = "check_policy:\n  reruns: 1\n"
    elif case == "hosted":
        run_on = "worker"
    else:
        config = fixture.CONFIG.replace("check_policy:\n", "check_policy:\n  part_proof: false\n", 1)
    review, runner = _review(tmp_path, _passes_everywhere, config=config, run_on=run_on)
    result = _schedule(review)
    assert result["status"] == "not_eligible" and result["reason"] == case
    assert _wait_complete(review, 0.3) is None and runner.calls == []


# --------------------------------------------------------------------------- #
# fix round 1: the automatic run reuses carried runs (M3)
# --------------------------------------------------------------------------- #


def test_a_second_snapshot_with_an_unchanged_closure_carries_instead_of_running_again(tmp_path: Path) -> None:
    from tests.gateway import test_review_execution_economy as economy

    world = economy._World(tmp_path)
    review = world.review(economy.ALPHA_HEAD)

    def auto() -> dict[str, Any]:
        response = world.client.post(
            f"/api/reviews/{review.review_id}/part-proof/run",
            headers=economy.HEADERS,
            json={"revision_id": review.revision_id, "in_view": ["pkg/alpha.py"]},
        )
        assert response.status_code == 200, response.text
        return dict(response.json())

    first = auto()
    assert first["files"]["pkg/alpha.py"]["o"] in {"exercised", "proven"}
    ran = len(world.runner.calls)
    assert ran >= 1
    # Snapshot 2 adds a module no selected test loads: test_alpha's closure is unchanged.
    world.push(review, {"pkg/gamma.py": "GAMMA = 1\n"})
    second = auto()
    assert len(world.runner.calls) == ran, f"the unchanged closure ran again: {world.runner.calls[ran:]}"
    assert second["files"]["pkg/alpha.py"]["o"] in {"exercised", "proven"}
    assert second["recorded"] is True and second["complete"] is True
    # A change to what the test loads runs it again.
    world.push(review, {"pkg/alpha.py": "def alpha():\n    return 1  # changed again\n"})
    third = auto()
    assert len(world.runner.calls) > ran and third["files"]["pkg/alpha.py"]["o"] in {"exercised", "proven"}


# --------------------------------------------------------------------------- #
# round 3: a superseded run stops, a capture runs once, the bound counts the next batch, hosted records nothing
# --------------------------------------------------------------------------- #


def test_a_snapshot_captured_mid_batch_stops_the_run_and_reads_not_run_yet_not_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 1: the 409 "Review advanced" is a stop, not an error that lets every remaining batch spend minutes."""

    from lemoncrow.pro.capabilities.review import part_proof_api as api

    _two_batches(monkeypatch)
    holder: dict[str, SimpleNamespace] = {}
    advanced: list[str] = []

    def script(runtime_id: str, side: str, count: int) -> tuple[dict[str, str], int]:
        if not advanced:  # the first batch is underway when a newer Snapshot is captured
            advanced.append(fixture._new_snapshot(holder["review"], {"alpha.py": "VALUE = 3\n"}))
        return dict(_PASS), 50

    review, _runner = _multi(tmp_path, script)
    holder["review"] = review
    real = api._run_one
    states: list[str] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        states.append(result[2])
        return result

    monkeypatch.setattr(api, "_run_one", spy)
    _auto(review, ["alpha.py"])
    assert states == ["superseded"]  # the second batch never started
    record = _record(review)
    assert record is not None and record.complete is False
    assert {record.files[f"{name}.py"].outcome for name in _PARTS} == {"not_run_yet"}  # honest: no false error


def test_a_review_that_advanced_before_a_batch_starts_runs_no_more_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review import part_proof_api as api

    _two_batches(monkeypatch)
    review, runner = _multi(tmp_path, lambda *_args: (dict(_PASS), 50))
    real = api._run_one
    calls: list[int] = []

    def advancing(*args: Any, **kwargs: Any) -> Any:
        calls.append(len(runner.calls))
        result = real(*args, **kwargs)
        if len(calls) == 1:
            fixture._new_snapshot(review, {"alpha.py": "VALUE = 3\n"})
        return result

    monkeypatch.setattr(api, "_run_one", advancing)
    _auto(review, ["alpha.py"])
    assert len(calls) == 1  # the newer Snapshot is checked before the second batch spends anything
    record = _record(review)
    assert record is not None and record.complete is False
    assert record.files["alpha.py"].outcome == "exercised"
    assert record.files["beta.py"].outcome == "not_run_yet" and record.files["gamma.py"].outcome == "not_run_yet"


def test_a_second_capture_of_a_snapshot_queues_no_second_run(tmp_path: Path) -> None:
    """Item 2: the revision is marked before the thread starts, so two quick captures make one run."""

    from lemoncrow.pro.capabilities.review import part_proof_api as api

    review, runner = _review(tmp_path, _fails_on_base)
    held = api._BACKGROUND_SLOTS
    held.acquire()
    held.acquire()  # both slots held: the first run waits for one, before it is "running"
    try:
        assert _schedule(review)["status"] == "queued"
        assert _schedule(review)["status"] == "running"  # not a second queued thread
    finally:
        held.release()
        held.release()
    record = _wait_complete(review)
    assert record is not None and record.complete
    assert _wait_scheduled_empty(api) and runner.calls


def _wait_scheduled_empty(api: Any, seconds: float = 10.0) -> bool:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not api._SCHEDULED and not api._RUNNING:
            return True
        time.sleep(0.02)
    return False


def test_captures_beyond_the_queue_bound_are_dropped_with_a_log_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from lemoncrow.pro.capabilities.review import part_proof_api as api

    monkeypatch.setattr(api, "_MAX_SCHEDULED", 1)
    review, _runner = _review(tmp_path, _passes_everywhere)
    held = api._BACKGROUND_SLOTS
    held.acquire()
    held.acquire()
    try:
        assert _schedule(review)["status"] == "queued"
        fixture._new_snapshot(review, {"calc.py": fixture.CALC_HEAD + "\n# follow-up\n"})
        with caplog.at_level("WARNING"):
            dropped = _schedule(review)
        assert dropped["status"] == "dropped" and dropped["reason"] == "queue_full"
        assert any("dropped" in record.getMessage() for record in caplog.records)
    finally:
        held.release()
        held.release()
    assert _wait_scheduled_empty(api)


def test_a_second_trigger_never_reruns_or_reopens_a_complete_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 2: a complete record (here with a test past the bounds) is the end of the plan, not a place to resume."""

    monkeypatch.setattr(pp, "plan_batches", _bounded(first=1, rest=1))
    review, runner = _multi(tmp_path, lambda *_args: (dict(_PASS), 50))
    first = _auto(review, ["alpha.py"])
    assert first["complete"] is True and first["files"]["gamma.py"]["r"] == "past_bound"
    spent = len(runner.calls)
    again = _auto(review, ["gamma.py"])  # even asking for the waiting part runs nothing
    assert len(runner.calls) == spent
    assert again["complete"] is True and again["files"]["gamma.py"]["r"] == "past_bound"


def test_the_bound_counts_the_next_batchs_estimate_not_only_what_is_spent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 4: a batch that starts at 14.9 of 15 minutes would run far past 15, so it is refused."""

    from lemoncrow.pro.capabilities.review import part_proof_api as api
    from lemoncrow.pro.capabilities.review.run_plan import PROPOSED_BUDGET_DEFAULTS, BudgetUsage
    from lemoncrow.pro.capabilities.review.run_plan_service import RunPlanService

    bound = float(PROPOSED_BUDGET_DEFAULTS["per_review_minutes"])
    estimates: list[float] = []
    real = api._over_bound

    def probe(preview: Any, entry_id: str, live: Any) -> bool:
        estimates.append(float(preview.entry(entry_id).estimated_minutes))
        return real(preview, entry_id, live)

    monkeypatch.setattr(api, "_over_bound", probe)
    review, _runner = _review(tmp_path, _passes_everywhere)
    _auto(review, ["calc.py"])
    estimate = estimates[0]
    assert 0.0 < estimate < bound - 1.0, estimate
    spent = bound - estimate / 2  # under the bound by itself, over it once the batch's estimate is counted
    monkeypatch.setattr(RunPlanService, "usage", lambda self, session, revision: BudgetUsage(review_minutes=spent))
    tight, tight_runner = _review(tmp_path / "tight", _passes_everywhere)
    payload = _auto(tight, ["calc.py"])
    assert tight_runner.calls == [] and _outcome(payload, "calc.py") == "over_budget"


@pytest.mark.parametrize("case", ["hosted", "disabled"])
def test_a_reviewers_run_records_no_part_proof_when_the_review_is_not_eligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Item 6: hosted and switched-off reviews keep no part-proof record, even from a check run."""

    from lemoncrow.pro.capabilities.review import part_proof_api as api

    config = fixture.CONFIG
    if case == "hosted":
        # The check route itself will not run on a worker-shaped registry in this fixture, so only the
        # recording sees a hosted review: that is the call that must refuse to write.
        monkeypatch.setattr(api, "_run_on", lambda _deps: "worker")
    else:
        config = fixture.CONFIG.replace("check_policy:\n", "check_policy:\n  part_proof: false\n", 1)
    review, runner = _review(tmp_path, _passes_everywhere, config=config)
    response = fixture._run(review, "checks.recipe:unit", pick=True)
    assert response.status_code == 200, response.text
    assert runner.calls  # the check itself ran
    assert response.json()["part_proof"] is None
    assert _record(review) is None


def test_a_store_that_is_hosted_never_starts_or_rewrites_a_run_whatever_the_runner_locus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P11-4 review: a hosted store has no worker here, so the runner locus says "local"; the store still decides.

    The probe was a POST on a hosted store: the runner ran and the CI record's source became ``auto``. Every entry
    that starts a run or writes a record (the POST, the capture hop, a reviewer's check run) must refuse.
    """

    review, runner = _review(tmp_path, _fails_on_base)
    revision = _revision(review, review.revision_id)
    head, base = pp.identities(revision)
    ci_record = pp.ProofRecord(
        revision_id=review.revision_id,
        head_identity=head,
        base_identity=base,
        files={"calc.py": pp.PartOutcome(pp.EXERCISED)},
        default=pp.COULDNT_RUN,
        default_reason="no_ci_result",
        source=pp.CI_SOURCE,
        complete=True,
        ci_order="2026-10-01T10:00:00Z|00000000000000000001",
    )
    pp.update_record(review.store, revision, lambda _old: ci_record)
    seeded = _record(review)
    assert seeded is not None and seeded.source == pp.CI_SOURCE

    monkeypatch.setattr(review.store, "hosted_attachment_supported", lambda: False)  # the hosted store's answer

    shown = _auto(review, ["calc.py"])
    assert runner.calls == [] and shown["source"] == "ci" and _outcome(shown, "calc.py") == "exercised"
    assert _schedule(review) == {"status": "not_eligible", "reason": "hosted", "revision_id": review.revision_id}
    response = fixture._run(review, "checks.recipe:unit", pick=True)
    assert response.status_code == 200, response.text
    assert response.json()["part_proof"] is None  # the check itself may run; it writes no part proof
    after = _record(review)
    assert after is not None and after.to_wire() == seeded.to_wire()  # not rewritten, not even re-versioned

    # With no CI record yet, a request starts nothing and reads "no CI result", never "not run yet".
    empty, empty_runner = _review(tmp_path / "empty", _fails_on_base)
    monkeypatch.setattr(empty.store, "hosted_attachment_supported", lambda: False)
    nothing = _auto(empty, ["calc.py"])
    assert empty_runner.calls == [] and nothing["recorded"] is False
    assert nothing["default"] == {"o": "couldnt_run", "r": "no_ci_result"} and nothing["running"] is False
    assert _record(empty) is None


# --------------------------------------------------------------------------- #
# round 4: a batch that never started is retried fresh through the real execute_check, once, then ends
# --------------------------------------------------------------------------- #


class _Flaky(_Scripted):
    """Raises on its first ``failures`` calls (the base and the head side of the first attempt), then behaves."""

    def __init__(self, script: Any, failures: int) -> None:
        super().__init__(script)
        self.failures = failures
        self.attempted = 0

    def run(self, context: Any, runtime: Any, surface: Any, *, side: str) -> Any:
        self.attempted += 1
        if self.attempted <= self.failures:
            raise RuntimeError("the runner exploded")
        return super().run(context, runtime, surface, side=side)


def _flaky(tmp_path: Path, failures: int) -> tuple[SimpleNamespace, _Flaky]:
    runner = _Flaky(_passes_everywhere, failures)
    review = fixture._review(
        tmp_path,
        base=fixture._base_files(fixture.CONFIG),
        head=dict(_HEAD),
        runner_registry=RunnerRegistry((runner,)),
    )
    return review, runner


def test_a_batch_that_never_started_runs_again_fresh_instead_of_replaying_its_error(tmp_path: Path) -> None:
    """N1: the retry goes through the real execute_check; a plain rerun would replay the stored error forever."""

    review, runner = _flaky(tmp_path, failures=2)
    first = _auto(review, ["calc.py"])
    assert (first["files"]["calc.py"]["o"], first["files"]["calc.py"]["r"]) == ("couldnt_run", "error")
    assert first["complete"] is False
    assert first["reasons"]["error"] and "Run test" in first["reasons"]["error"]
    stored = _record(review)
    assert stored is not None and stored.ran_tests == () and stored.tries == {"tests/test_calc.py": 1}
    attempted = runner.attempted
    second = _auto(review, ["calc.py"])
    assert runner.attempted > attempted  # it executed again; the stored error was not replayed
    assert _outcome(second, "calc.py") == "exercised" and second["complete"] is True


def test_a_runner_that_always_fails_ends_complete_after_one_retry_and_spends_no_more(tmp_path: Path) -> None:
    review, runner = _flaky(tmp_path, failures=10_000)
    first = _auto(review, ["calc.py"])
    assert first["complete"] is False
    after_first = runner.attempted
    second = _auto(review, ["calc.py"])  # the one fresh retry
    assert runner.attempted > after_first
    assert second["complete"] is True and second["files"]["calc.py"]["r"] == "error"
    assert "Run test" in second["reasons"]["error"]
    settled = runner.attempted
    for _ in range(3):  # further page sessions and capture runs spend nothing
        again = _auto(review, ["calc.py"])
        assert again["complete"] is True and again["files"]["calc.py"]["r"] == "error"
    assert runner.attempted == settled
    assert _schedule(review)["status"] == "done"


def test_a_reader_that_races_a_run_in_progress_gets_running_back_instead_of_deadlocking(tmp_path: Path) -> None:
    """N2: the "already running" answer used to read the projection while holding the non-reentrant lock."""

    import threading

    from lemoncrow.pro.capabilities.review import part_proof_api as api

    review, runner = _review(tmp_path, _passes_everywhere)
    api._RUNNING.add(review.revision_id)  # a capture run for this Snapshot is underway
    result: dict[str, Any] = {}
    worker = threading.Thread(target=lambda: result.update(_auto(review, ["calc.py"])), daemon=True)
    try:
        worker.start()
        worker.join(10.0)
        assert not worker.is_alive(), "the reader's run request deadlocked on the running lock"
        assert result["running"] is True and runner.calls == []
    finally:
        api._RUNNING.discard(review.revision_id)


# --------------------------------------------------------------------------- #
# round 4 follow-up: only a batch that ran (or failed to start) is counted as a try
# --------------------------------------------------------------------------- #


def _refused_end_state(payload: dict[str, Any], review: SimpleNamespace, outcome: str, reason: str) -> None:
    record = _record(review)
    assert record is not None and record.tries == {} and record.ran_tests == ()
    assert payload["complete"] is True  # nothing to retry: the end state is final, not an open record
    row = payload["files"]["calc.py"]
    assert (row["o"], row.get("r", "")) == (outcome, reason)


def test_a_budget_refusal_before_the_batch_counts_no_try_and_ends_complete(tmp_path: Path) -> None:
    review, runner = _review(
        tmp_path, _passes_everywhere, config=fixture.CONFIG + "review:\n  budget:\n    per_review_minutes: 1\n"
    )
    payload = _auto(review, ["calc.py"])
    assert runner.calls == []
    _refused_end_state(payload, review, "over_budget", "")


def test_a_budget_409_inside_execute_check_counts_no_try_and_ends_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    from lemoncrow.pro.capabilities.review import part_proof_api as api

    def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise HTTPException(status_code=409, detail="a fresh run (about 3 min) does not fit per_review_minutes: 15")

    review, runner = _review(tmp_path, _passes_everywhere)
    monkeypatch.setattr(api, "_over_bound", lambda *_args: False)  # the pre-check passed; the run itself is refused
    monkeypatch.setattr(api, "execute_check", refused)
    payload = _auto(review, ["calc.py"])
    assert runner.calls == []
    _refused_end_state(payload, review, "over_budget", "")


def test_a_not_planned_result_counts_no_try_and_ends_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import planned_execution

    def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise planned_execution.PlannedExecutionRefused("this recipe is skipped")

    review, runner = _review(tmp_path, _passes_everywhere)
    monkeypatch.setattr(planned_execution, "admit_planned_execution", refused)
    payload = _auto(review, ["calc.py"])
    assert runner.calls == []
    _refused_end_state(payload, review, "couldnt_run", "not_planned")
