"""RC12-C capability support dispositions: what a deployment cannot run says so, with the way out.

Each test pins one carried item to its final state (docs/status/capabilities.toml): rendered diffs run on
the local locus unless the deployment states it wired another, SQL observation is SQLite only, and a locus
or engine that cannot run an entry is ``not_available`` with a setup reason, never a silent skip or a pass.
"""

from __future__ import annotations

import pytest

from lemoncrow.pro.capabilities.review.capability_catalog import DETECTION_FACTS, STATIC_CATALOG, DetectedCapabilities
from lemoncrow.pro.capabilities.review.run_plan import (
    PlanInputs,
    SettingOrigin,
    build_run_plan,
    parse_run_settings,
    resolve_run_settings,
)
from lemoncrow.pro.capabilities.review.run_plan_service import executor_loci

REPO = SettingOrigin("trusted_base_repository", "base revision .lemoncrow/review.yaml")
RENDER_ENTRIES = ("visual.dom_a11y", "visual.matrix")


def _plan(
    run_on: str,
    *,
    effort: str = "deep",
    allowed: tuple[str, ...] | None = None,
    executor_loci: dict[str, tuple[str, ...]] | None = None,
) -> object:
    settings = resolve_run_settings([parse_run_settings({"effort": effort}, origin=REPO)], default_run_on=run_on)  # type: ignore[arg-type]
    loci = allowed or (run_on,)
    return build_run_plan(
        PlanInputs(
            review_id="review-1",
            revision_id="revision-1",
            trigger="open",
            settings=settings,
            detected=DetectedCapabilities(facts=frozenset(DETECTION_FACTS)),
            changed_paths=("src/app.tsx",),
            allowed_run_on=loci,  # type: ignore[arg-type]
            allowed_run_on_by_class={"runtime": loci, "ai": loci},  # type: ignore[dict-item]
            executor_loci=executor_loci or {},  # type: ignore[arg-type]
        )
    )


@pytest.mark.parametrize("run_on", ["worker", "managed"])
def test_rendered_diffs_off_the_local_locus_are_not_available_with_a_setup_reason(run_on: str) -> None:
    plan = _plan(run_on)
    for entry_id in RENDER_ENTRIES:
        row = plan.entry(entry_id)  # type: ignore[attr-defined]
        assert row is not None and row.status == "not_available", (entry_id, row)
        assert "runs only where the revision is held" in row.reason
        assert f"places it on the {run_on} executor" in row.reason
        # The setup reason says what to do, not just what is wrong -- and only what this deployment can do:
        # a hosted deployment (worker / managed executors only) has no local executor to switch to.
        assert "has no local executor" in row.reason and "local-only" in row.reason
        assert "`lc review`" in row.reason
        assert "Set this deployment's runtime executor" not in row.reason


@pytest.mark.parametrize("run_on", ["worker", "managed"])
def test_a_deployment_that_wired_rendering_on_its_executor_plans_it_there(run_on: str) -> None:
    plan = _plan(run_on, executor_loci={"render_diff": (run_on,)})
    for entry_id in RENDER_ENTRIES:
        row = plan.entry(entry_id)  # type: ignore[attr-defined]
        assert row is not None and row.status == "run", (entry_id, row.status, row.reason)
        assert row.executor == run_on
    assert plan.digest != _plan(run_on).digest  # type: ignore[attr-defined]


def test_wiring_one_locus_does_not_open_the_other() -> None:
    row = _plan("managed", executor_loci={"render_diff": ("worker",)}).entry("visual.dom_a11y")  # type: ignore[attr-defined]
    assert row is not None and row.status == "not_available"
    assert "places it on the managed executor" in row.reason


def test_a_plan_without_a_stated_locus_keeps_its_digest() -> None:
    assert _plan("worker").digest == _plan("worker", executor_loci={"render_diff": ()}).digest  # type: ignore[attr-defined]


def test_only_a_known_executor_can_be_extended_and_only_onto_hosted_loci() -> None:
    assert executor_loci({"render_diff": ["worker", "managed", "local", "x"]}) == {"render_diff": ("managed", "worker")}
    assert executor_loci({"web_preview": ["worker"], "render_diff": "worker"}) == {}
    assert executor_loci(None) == {}


def test_a_deployment_that_also_has_the_local_executor_is_told_to_switch_to_it() -> None:
    plan = _plan("worker", allowed=("local", "worker"))
    for entry_id in RENDER_ENTRIES:
        row = plan.entry(entry_id)  # type: ignore[attr-defined]
        assert row is not None and row.status == "not_available", (entry_id, row)
        assert "Set this deployment's runtime executor to the local executor" in row.reason
        assert "has no local executor" not in row.reason


def test_rendered_diffs_run_on_the_local_locus() -> None:
    plan = _plan("local")
    for entry_id in RENDER_ENTRIES:
        row = plan.entry(entry_id)  # type: ignore[attr-defined]
        assert row is not None and row.status == "run", (entry_id, row.status, row.reason)


def test_only_the_local_locus_is_declared_for_render_diffs() -> None:
    # The declaration the planner reads: widening it needs a driver that runs where the Snapshot is not held.
    for entry_id in RENDER_ENTRIES:
        assert STATIC_CATALOG[entry_id].loci == ("local",)
        assert STATIC_CATALOG[entry_id].locus_hint


def test_sql_observation_is_sqlite_only_and_every_record_says_so() -> None:
    from lemoncrow.pro.capabilities.review.runtime_observation import LIMITATIONS, PYTHON_OBSERVER

    # Nothing but the sqlite3 module is wrapped: a server database is a connection, never a captured query.
    assert "sqlite3" in PYTHON_OBSERVER
    for driver in ("psycopg", "pymysql", "mysql", "asyncpg", "sqlalchemy", "pyodbc", "cx_Oracle"):
        assert driver not in PYTHON_OBSERVER, driver
    stated = " ".join(LIMITATIONS)
    assert "SQL is captured for SQLite" in stated and "server databases are visible as connections only" in stated


def test_a_server_database_connection_is_a_network_observation_not_a_query() -> None:
    from lemoncrow.pro.capabilities.review.runtime_observation import build_observation

    raw = {"tier": "hooks", "rows": [{"t": "connect", "addr": "203.0.113.7", "port": 5432, "result": "ENETUNREACH"}]}
    observation = build_observation(side="head", raw=raw)
    assert {(item.category, item.identity) for item in observation.effects} == {("network", "connect 203.0.113.7:5432")}
    assert observation.query_count == 0
