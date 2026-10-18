"""The effective run plan drives the Review executors: executed == displayed == persisted."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review import ai_review as ai_review_module
from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.run_plan import ManualPick, load_run_plan
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore

TOKEN = "run-plan-token"
PORT = 45998
BASE = f"http://127.0.0.1:{PORT}"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

_SOURCE = "def authorize(user):\n    return user.is_admin\n"
_CHANGED = "def authorize(user):\n    if user is None:\n        return True\n    return user.is_admin\n"


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _commit(repo: Any, message: str, offset: int) -> None:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    signature = pygit2.Signature("Fixture Tester", "fixture@example.invalid", 1700000000 + offset, 0)
    repo.create_commit("HEAD", signature, signature, message, tree, parents)


def _review(
    tmp_path: Path,
    *,
    base_config: str = "",
    head_config: str | None = None,
    **register: Any,
) -> SimpleNamespace:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    repo = pygit2.init_repository(str(repo_root), initial_head="main")
    _write(repo_root, "src/auth.py", _SOURCE)
    _write(repo_root, "tests/test_auth.py", "def test_ok():\n    assert True\n")
    _write(repo_root, "pyproject.toml", "[project]\nname = 'fixture'\n\n[tool.pytest.ini_options]\n")
    if base_config:
        _write(repo_root, ".lemoncrow/review.yaml", base_config)
    _commit(repo, "base", 0)
    _write(repo_root, "src/auth.py", _CHANGED)
    if head_config is not None:
        _write(repo_root, ".lemoncrow/review.yaml", head_config)
    _commit(repo, "change", 1)

    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, base="HEAD~1", head="HEAD")
    build = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, repo_root, rng, title=build.packet.title)
    revision = snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)
    store.publish_snapshot(session.id, revision.id)
    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=repo_root,
        port=PORT,
        **register,
    )
    client = TestClient(app, base_url=BASE, raise_server_exceptions=False)
    return SimpleNamespace(client=client, store=store, review_id=session.id, revision_id=revision.id, app=app)


def _fake_ai(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class Result:
        def __init__(self, effort: str) -> None:
            self.effort = effort

        def to_wire(self) -> dict[str, Any]:
            return {"status": "completed", "effort": self.effort, "provider_calls": 0}

    def fake_run(**kwargs: Any) -> Result:
        calls.append(kwargs)
        return Result(str(kwargs["effort"]))

    monkeypatch.setattr(ai_review_module, "run_independent_review", fake_run)
    return calls


def _post(review: SimpleNamespace, path: str, body: dict[str, Any]) -> Any:
    return review.client.post(
        f"/api/reviews/{review.review_id}/{path}",
        headers=HEADERS,
        json={"expected_revision_id": review.revision_id, **body},
    )


def _get(review: SimpleNamespace, path: str) -> Any:
    return review.client.get(f"/api/reviews/{review.review_id}/{path}", headers=HEADERS)


def test_executed_ai_review_is_exactly_the_displayed_persisted_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path, base_config="review:\n  effort: thorough\n")
    calls = _fake_ai(monkeypatch)
    shown = _get(review, "run-plan")
    assert shown.status_code == 200, shown.text
    displayed = shown.json()["plan"]
    assert displayed["selection"] == {"source": "preset", "name": "thorough", "base": "thorough"}

    response = _post(review, "ai-review", {"trigger": "manual"})
    assert response.status_code == 200, response.text
    stage = calls[0]["stage_plan"]
    persisted = load_run_plan(review.store, review.revision_id)
    assert persisted is not None
    assert stage.plan_digest == persisted.digest == displayed["digest"]
    assert response.json()["ai_review"]["run_plan_digest"] == displayed["digest"]
    assert calls[0]["effort"] == stage.effort == persisted.entry("ai.investigators").depth == "thorough"  # type: ignore[union-attr]
    assert stage.hypotheses is persisted.runs("ai.hypotheses")


def test_head_side_review_settings_are_future_policy_only(tmp_path: Path) -> None:
    review = _review(
        tmp_path,
        base_config="review:\n  effort: quick\n",
        head_config=(
            "review:\n  effort: custom\n  custom: [ai.investigators, ai.hypotheses]\n  capabilities:\n    ai: always\n"
        ),
    )
    payload = _get(review, "run-plan").json()
    assert payload["plan"]["selection"]["name"] == "quick"
    assert payload["settings"]["effort"]["effective_value"] == "quick"
    assert payload["settings"]["effort"]["origin"]["layer"] == "trusted_base_repository"
    assert payload["settings"]["capabilities"]["ai"]["effective_value"] == "auto"
    assert not any(row["id"] == "ai.hypotheses" for row in payload["plan"]["entries"])
    future = payload["future_settings"]
    assert future["changed"] is True and future["proposed"]["effort"] == "custom"


def test_ai_family_off_refuses_manual_ai_and_synthesis_and_blocks_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path, base_config="review:\n  capabilities:\n    ai: off\n")
    calls = _fake_ai(monkeypatch)
    refused = _post(review, "ai-review", {"trigger": "manual", "effort": "deep"})
    assert refused.status_code == 409, refused.text
    assert "not in the effective run plan" in refused.json()["detail"]
    synthesis = _post(review, "synthesis", {})
    assert synthesis.status_code == 409, synthesis.text
    projection = _get(review, "projection").json()
    assert projection["ai_review"]["auto_eligible"] is False
    assert projection["ai_review"]["run_plan"]["ai"]["investigators"] is False
    assert calls == []


def test_run_deeper_beyond_the_budget_is_limited_and_executes_the_limited_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _review(tmp_path, base_config="review:\n  budget:\n    per_review_model_calls: 2\n")
    calls = _fake_ai(monkeypatch)
    response = _post(review, "ai-review", {"trigger": "manual", "effort": "deep"})
    assert response.status_code == 200, response.text
    persisted = load_run_plan(review.store, review.revision_id)
    assert persisted is not None
    investigators = persisted.entry("ai.investigators")
    assert investigators is not None and investigators.budget_reduced_from == "deep"
    assert calls[0]["effort"] == investigators.depth == "auto"
    assert calls[0]["stage_plan"].max_review_calls == 2
    assert calls[0]["stage_plan"].plan_digest == persisted.digest
    hypotheses = persisted.entry("ai.hypotheses")
    assert hypotheses is not None and hypotheses.status == "limited_by_budget"


def test_hosted_durable_submitter_receives_the_persisted_plan(tmp_path: Path) -> None:
    submitted: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def submitter(*args: Any, **kwargs: Any) -> dict[str, Any]:
        submitted.append((args, kwargs))
        return {"status": "queued"}

    review = _review(tmp_path, base_config="review:\n  effort: deep\n", ai_review_submitter=submitter)
    response = _post(review, "ai-review", {"trigger": "manual"})
    assert response.status_code == 200, response.text
    args, kwargs = submitted[0]
    persisted = load_run_plan(review.store, review.revision_id)
    assert persisted is not None
    assert kwargs["run_plan"]["digest"] == persisted.digest
    assert args[2] == "deep" and args[3] == "manual"


def test_run_specific_previews_then_adds_only_and_rejects_unknown_ids(tmp_path: Path) -> None:
    review = _review(tmp_path, base_config="review:\n  effort: quick\n")
    before = _get(review, "run-plan").json()["plan"]
    preview = _post(review, "run-plan/run-specific", {"entries": ["ai.hypotheses"]})
    assert preview.status_code == 200, preview.text
    assert preview.json()["confirmed"] is False
    assert preview.json()["preview"]["incremental"] == {"minutes": 0.0, "model_calls": 1}
    assert load_run_plan(review.store, review.revision_id).digest == before["digest"]  # type: ignore[union-attr]

    confirmed = _post(review, "run-plan/run-specific", {"entries": ["ai.hypotheses"], "confirm": True})
    assert confirmed.status_code == 200, confirmed.text
    plan = confirmed.json()["plan"]
    added = next(row for row in plan["entries"] if row["id"] == "ai.hypotheses")
    assert (added["status"], added["origin"]) == ("run", "manual")
    assert plan["parent_digest"] == before["digest"]
    assert confirmed.json()["next"] == [{"entry": "ai.hypotheses", "action": "ai_review", "depth": "quick"}]
    still = {row["id"] for row in before["entries"] if row["status"] == "run"}
    assert still <= {row["id"] for row in plan["entries"] if row["status"] == "run"}
    assert _get(review, "run-plan").json()["plan"]["digest"] == plan["digest"]

    unknown = _post(review, "run-plan/run-specific", {"entries": ["checks.bogus"]})
    assert unknown.status_code == 422 and "checks.bogus" in unknown.json()["detail"]
    missing = _post(review, "run-plan/run-specific", {"entries": ["browser:missing"]})
    assert missing.status_code == 422 and "browser:missing" in missing.json()["detail"]


def test_a_confirmed_pick_survives_a_policy_change_on_the_same_snapshot(tmp_path: Path) -> None:
    budget = {"per_review_model_calls": 50}

    def policy(_session: Any, _revision: Any) -> dict[str, Any]:
        layer = {"origin": {"layer": "organization", "source": "operator"}, "settings": {"budget": dict(budget)}}
        return {
            "behavior": {"effort": {"effective_value": "quick", "origin": {"layer": "organization"}}},
            "run_plan": {"layers": [layer], "allowed_run_on": ["local"], "default_run_on": "local"},
        }

    review = _review(tmp_path, base_config="review:\n  effort: quick\n", intelligence_policy=policy)
    confirmed = _post(review, "run-plan/run-specific", {"entries": ["ai.hypotheses"], "confirm": True})
    assert confirmed.status_code == 200, confirmed.text
    before = confirmed.json()["plan"]
    budget["per_review_model_calls"] = 60
    plan = _get(review, "run-plan").json()["plan"]
    assert plan["inputs_digest"] != before["inputs_digest"]
    assert [pick["id"] for pick in plan["manual"]] == ["ai.hypotheses"]
    row = next(row for row in plan["entries"] if row["id"] == "ai.hypotheses")
    assert (row["status"], row["origin"]) == ("run", "manual")
    assert plan["manual_dropped"] == []
    assert load_run_plan(review.store, review.revision_id).manual == (ManualPick("ai.hypotheses", "quick"),)  # type: ignore[union-attr]


_BROWSER = """browser_checks:
  trusted_origins: ["https://preview.example.test"]
  scenarios:
    - id: settings-smoke
      title: Settings smoke
      route: /settings
      paths: ["src/*.py"]
    - id: auth-required
      title: Auth required
      route: /auth
      paths: ["src/auth.py"]
      required: true
"""


@pytest.mark.parametrize(("effort", "scheduled"), [("quick", False), ("auto", True)])
def test_browser_scenarios_are_scheduled_by_the_plan_and_required_ones_always(
    tmp_path: Path, effort: str, scheduled: bool
) -> None:
    review = _review(tmp_path, base_config=f"review:\n  effort: {effort}\n{_BROWSER}")
    verification = _get(review, "verification").json()
    rows = {
        row["browser"]["scenario"]: row
        for row in verification["checks"]
        if row.get("browser") and row["browser"]["scenario"]
    }
    optional = rows["settings-smoke"]
    assert optional["run_plan"]["scheduled"] is scheduled
    assert ("Not scheduled by the effective run plan" in optional["detail"]) is not scheduled
    required = rows["auth-required"]
    assert required["required"] is True and "run_plan" not in required
    plan = _get(review, "run-plan").json()["plan"]
    floor = next(row for row in plan["entries"] if row["id"] == "browser:auth-required")
    assert floor["origin"] == "policy_floor" and floor["required"] is True


def test_trusted_base_review_settings_round_trip_through_the_run_plan_api(tmp_path: Path) -> None:
    review = _review(
        tmp_path,
        base_config=(
            "review:\n  effort: custom\n  run_on: local\n  custom_from: thorough\n"
            "  custom:\n    - checks.test\n    - id: ai.investigators\n      paths: ['src/**']\n"
            "  budget:\n    per_review_minutes: 15\n    per_review_model_calls: 6\n"
            "  capabilities:\n    visual: off\n"
        ),
    )
    payload = _get(review, "run-plan").json()
    settings = payload["settings"]
    origin = {"layer": "trusted_base_repository", "source": settings["effort"]["origin"]["source"]}
    assert settings["effort"] == {"effective_value": "custom", "mode": "override", "origin": origin}
    assert settings["custom_from"]["effective_value"] == "thorough"
    assert settings["custom"]["effective_value"] == ["checks.test", {"id": "ai.investigators", "paths": ["src/**"]}]
    assert settings["run_on"]["effective_value"] == "local"
    assert settings["budget"]["per_review_minutes"]["effective_value"] == 15
    assert settings["budget"]["per_review_model_calls"] == {"effective_value": 6, "mode": "override", "origin": origin}
    assert settings["budget"]["monthly_minutes"]["mode"] == "inherit"
    assert settings["capabilities"]["visual"]["effective_value"] == "off"
    assert settings["capabilities"]["ai"]["mode"] == "inherit"
    plan = payload["plan"]
    assert plan["selection"] == {"source": "custom", "name": "custom", "base": "thorough"}
    assert plan["budget"]["ceilings"] == {"per_review_minutes": 15.0, "per_review_model_calls": 6}
    assert {row["id"]: row["paths"] for row in plan["entries"] if row["origin"] == "custom"}["ai.investigators"] == [
        "src/**"
    ]


def test_detected_capabilities_only_appear_in_the_settings_preview(tmp_path: Path) -> None:
    review = _review(tmp_path)
    payload = _get(review, "run-plan").json()
    assert "test_command" in payload["detected"]["facts"]
    shown = {row["id"] for depth in payload["depths"]["depths"].values() for row in depth["entries"]}
    assert "checks.test" in shown
    assert not shown & {"visual.screenshot", "migrations.execute", "infra.render"}
    assert payload["depths"]["monthly_reviews"] == 1
    assert payload["settings"]["budget"]["per_review_minutes"]["effective_value"] is None


_RUNTIME = """version: 1
runtimes:
  - id: checks
    runner: fixture-runner
    command: [python, -m, safe_check]
surfaces:
  - id: checks
    provider: service
    runtime: checks
    services: [tests]
"""


@pytest.mark.parametrize("family", ["auto", "off"])
def test_runtime_validation_runs_only_as_a_planned_surface_pick(tmp_path: Path, family: str) -> None:
    from lemoncrow.pro.capabilities.review.runtimes import RunnerRegistry, SurfaceRunResult

    ran: list[str] = []

    class Runner:
        runner_id = "fixture-runner"

        def setup_candidates(self, _repo_root: Path) -> tuple[()]:
            return ()

        def run(self, context: Any, runtime: Any, surface: Any, *, side: str) -> SurfaceRunResult:
            ran.append(surface.id)
            return SurfaceRunResult(
                surface.id, surface.provider, side, "passed", "ok", self.runner_id, runtime.id, exit_code=0
            )

    config = f"{_RUNTIME}review:\n  capabilities:\n    deep_runtime: {family}\n"
    review = _review(tmp_path, base_config=config, runner_registry=RunnerRegistry((Runner(),)))
    response = review.client.post(
        f"/api/reviews/{review.review_id}/runtime-validation",
        headers=HEADERS,
        json={"revision_id": review.revision_id, "provider": "service", "surface_id": "checks:tests", "side": "new"},
    )
    persisted = load_run_plan(review.store, review.revision_id)
    assert persisted is not None
    entry = persisted.entry("surface:checks")
    assert entry is not None
    if family == "off":
        assert response.status_code == 409, response.text
        assert "surface:checks is not in the effective run plan" in response.json()["detail"]
        assert entry.status == "skipped" and ran == []
        return
    assert response.status_code == 201, response.text
    assert response.json()["run_plan"] == {"digest": persisted.digest, "entry": "surface:checks"}
    assert (entry.status, entry.origin) == ("run", "manual")
    assert ran == ["checks:tests"]
    # The local execution is metered per catalog entry, executor and stage; so is
    # the plan's own test-impact map build (RC12-B), on the Snapshot that built it.
    compute = _get(review, "run-plan").json()["compute"]
    assert [(row["entry_id"], row["executor"], row["stage"], row["runs"]) for row in compute["rows"]] == [
        ("review.test_impact_map", "review_host", "impact_map_build", 1),
        ("surface:checks", "local", "runtime_validation", 1),
    ]
    assert compute["totals"]["runs"] == 2


def _hosted_policy(*, allowed: tuple[str, ...] = ("local",), required_checks: tuple[dict[str, Any], ...] = ()) -> Any:
    def policy(_session: Any, _revision: Any) -> dict[str, Any]:
        return {
            "behavior": {"effort": {"effective_value": "auto", "origin": {"layer": "built_in_default"}}},
            "requirements": {
                "settings": {
                    "required_checks": {"effective_value": list(required_checks), "origin": {"layer": "organization"}}
                }
            },
            "run_plan": {
                "layers": [
                    {
                        "origin": {"layer": "organization", "source": "operator_review_policy"},
                        "settings": {
                            "required": ["ai.investigators"],
                            "budget": {"monthly_managed_usd": 5},
                            "run_on": "managed",
                        },
                        "errors": [],
                    }
                ],
                "allowed_run_on": list(allowed),
                "default_run_on": allowed[0],
            },
        }

    return policy


def _condition(review: SimpleNamespace, condition_id: str) -> dict[str, Any] | None:
    readiness = _get(review, "readiness")
    assert readiness.status_code == 200, readiness.text
    return next((row for row in readiness.json()["conditions"] if row["id"] == condition_id), None)


def test_required_lemoncrow_check_without_an_executor_is_planned_visibly_and_blocks_readiness(
    tmp_path: Path,
) -> None:
    review = _review(
        tmp_path,
        intelligence_policy=_hosted_policy(
            required_checks=(
                {"stable_key": "security:sast", "attestation_class": "lemoncrow_verification"},
                {"stable_key": "build", "attestation_class": "provider_ci", "provider": "github"},
            )
        ),
    )
    plan = _get(review, "run-plan").json()["plan"]
    row = next(item for item in plan["entries"] if item["id"] == "check:security:sast")
    assert (row["status"], row["required"], row["floor_kind"]) == ("not_available", True, "check")
    assert row["reason"].startswith("required · no executor")
    assert not [item["id"] for item in plan["entries"] if "build" in item["id"]]
    assert plan["required_blockers"] == ["check:security:sast"]
    condition = _condition(review, "run-plan-required")
    assert condition is not None and condition["state"] == "pending"
    assert "security:sast (no executor)" in condition["detail"]


def test_managed_spend_cap_limits_required_ai_and_readiness_stays_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_ai(monkeypatch)
    review = _review(
        tmp_path,
        intelligence_policy=_hosted_policy(allowed=("managed",)),
        run_plan_usage=lambda _session, _revision: {"monthly_managed_usd": 5.0},
    )
    plan = _get(review, "run-plan").json()["plan"]
    investigators = next(item for item in plan["entries"] if item["id"] == "ai.investigators")
    assert (investigators["status"], investigators["required"]) == ("limited_by_budget", True)
    assert "monthly managed spend cap reached" in investigators["reason"]
    condition = _condition(review, "run-plan-required")
    assert condition is not None and condition["state"] == "pending"
    assert "(Limited by budget)" in condition["detail"]
    response = _post(review, "ai-review", {"trigger": "manual", "effort": "auto"})
    assert response.status_code == 409, response.text
    assert calls == []


def test_managed_spend_cap_routes_required_ai_to_an_allowed_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_ai(monkeypatch)
    review = _review(
        tmp_path,
        intelligence_policy=_hosted_policy(allowed=("managed", "local")),
        run_plan_usage=lambda _session, _revision: {"monthly_managed_usd": 5.0},
    )
    plan = _get(review, "run-plan").json()["plan"]
    investigators = next(item for item in plan["entries"] if item["id"] == "ai.investigators")
    assert (investigators["status"], investigators["executor"]) == ("run", "local")
    assert _condition(review, "run-plan-required") is None
    response = _post(review, "ai-review", {"trigger": "manual"})
    assert response.status_code == 200, response.text
    assert calls and calls[0]["stage_plan"].investigators


def _trusted_base_policy(state: str, *, by_class: dict[str, list[str]] | None = None) -> Any:
    def policy(_session: Any, _revision: Any) -> dict[str, Any]:
        invalid = state == "invalid"
        return {
            "behavior": (
                None if invalid else {"effort": {"effective_value": "thorough", "origin": {"layer": "organization"}}}
            ),
            "trusted_base": {
                "path": ".lemoncrow/review.yaml",
                "policy_state": state,
                "detail": "review.effort must be quick, auto, thorough, deep or custom" if invalid else "",
            },
            "run_plan": {
                "layers": [],
                "allowed_run_on": ["managed"],
                "default_run_on": "managed",
                **({"allowed_run_on_by_class": by_class} if by_class else {}),
            },
        }

    return policy


def test_invalid_trusted_base_policy_schedules_nothing_and_blocks_readiness(tmp_path: Path) -> None:
    submitted: list[Any] = []

    def submitter(*args: Any, **kwargs: Any) -> dict[str, Any]:
        submitted.append((args, kwargs))
        return {"status": "queued"}

    review = _review(tmp_path, intelligence_policy=_trusted_base_policy("invalid"), ai_review_submitter=submitter)
    session = review.store.get_session(review.review_id)
    revision = review.store.get_revision(review.revision_id)
    scheduled = review.app.state.review_auto_scheduler(session, revision)
    assert scheduled["status"] == "not_planned"
    assert "review.effort must be quick" in scheduled["detail"]
    plan = _get(review, "run-plan").json()["plan"]
    assert plan["policy_state"] == "invalid"
    assert plan["policy_error"] == (
        "trusted-base .lemoncrow/review.yaml is invalid: review.effort must be quick, auto, thorough, deep or custom"
    )
    assert not [row["id"] for row in plan["entries"] if row["status"] in {"run", "on_request"}]
    assert _post(review, "ai-review", {"trigger": "manual", "effort": "deep"}).status_code == 409
    assert submitted == []
    condition = _condition(review, "run-plan-required")
    assert condition is not None and condition["state"] == "pending"
    assert "Review policy invalid" in condition["detail"]


def test_provisional_trusted_base_policy_plans_as_before(tmp_path: Path) -> None:
    review = _review(tmp_path, intelligence_policy=_trusted_base_policy("provisional"))
    plan = _get(review, "run-plan").json()["plan"]
    assert (plan["policy_state"], plan["policy_error"]) == ("valid", "")
    assert plan["selection"]["name"] == "thorough"
    assert [row["id"] for row in plan["entries"] if row["status"] == "run"]


def test_hosted_ai_and_runtime_loci_reach_the_plan_per_entry(tmp_path: Path) -> None:
    review = _review(
        tmp_path,
        base_config=_RUNTIME,
        intelligence_policy=_trusted_base_policy("final", by_class={"ai": ["managed"], "runtime": ["worker"]}),
    )
    plan = _get(review, "run-plan").json()["plan"]
    executors = {row["id"]: row["executor"] for row in plan["entries"]}
    assert executors["ai.investigators"] == "managed"
    assert executors["surface:checks"] == "worker" and executors["checks.test"] == "worker"
    assert plan["run_on_note"] == ""


# --------------------------------------------------------------------------- #
# RC6: the organization execution block wins over every lower layer
# --------------------------------------------------------------------------- #

_AI_BLOCK = "blocked by organization policy (execution_block.ai)"
_EXECUTION_BLOCK = "blocked by organization policy (execution_block.execution)"
# The repository asks for everything, at the highest depth, on every family.
_EVERYTHING = _RUNTIME + "review:\n  effort: deep\n  capabilities: {ai: always, checks: always}\n"


def _blocked_policy(block: Any, *, allowed: tuple[str, ...] = ("local",)) -> Any:
    base = _hosted_policy(
        allowed=allowed,
        required_checks=({"stable_key": "tests", "attestation_class": "lemoncrow_verification"},),
    )

    def policy(session: Any, revision: Any) -> dict[str, Any]:
        effective = base(session, revision)
        return effective if block is None else {**effective, "organization_block": block}

    return policy


def test_organization_ai_and_execution_block_beats_repository_floors_and_manual_picks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_ai(monkeypatch)
    review = _review(
        tmp_path,
        base_config=_EVERYTHING,
        intelligence_policy=_blocked_policy({"ai": True, "execution": True}),
    )
    plan = _get(review, "run-plan").json()["plan"]
    entries = {row["id"]: row for row in plan["entries"]}
    ai = [row for entry_id, row in entries.items() if entry_id.startswith("ai.")]
    assert ai and all(row["status"] == "not_available" and _AI_BLOCK in row["reason"] for row in ai)
    # The organization layer's own required AI floor cannot lift it either.
    investigators = entries["ai.investigators"]
    assert investigators["required"] is True
    assert investigators["reason"] == f"required · no executor ({_AI_BLOCK})"
    runtime = [entries[key] for key in ("checks.test", "surface:checks")]
    assert all(row["status"] == "not_available" and _EXECUTION_BLOCK in row["reason"] for row in runtime)
    # A required check this build has no executor for (the ``default`` class) never
    # runs: it stays not_available, with or without the block.
    assert entries["check:tests"]["status"] == "not_available"
    assert not [row["id"] for row in plan["entries"] if row["status"] == "run" and not row["id"].startswith("side_")]

    # Requests from a person cannot lift it: manual AI review, a manual pick, Ask.
    assert _post(review, "ai-review", {"trigger": "manual", "effort": "deep"}).status_code == 409
    picked = _post(review, "run-plan/run-specific", {"entries": ["ai.investigators", "checks.test"], "confirm": True})
    assert picked.status_code == 200, picked.text
    amended = {row["id"]: row for row in _get(review, "run-plan").json()["plan"]["entries"]}
    assert amended["ai.investigators"]["status"] == "not_available"
    assert amended["checks.test"]["status"] == "not_available"
    ask = review.client.post(
        f"/api/reviews/{review.review_id}/ask",
        headers=HEADERS,
        json={"body": "what changed?", "revision_number": 0, "driver": "ollama"},
    )
    assert ask.status_code == 409, ask.text
    assert ask.json()["detail"] == f"Review Ask is unavailable: {_AI_BLOCK}"
    assert calls == []


def test_organization_execution_block_alone_leaves_ai_review_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_ai(monkeypatch)
    review = _review(tmp_path, base_config=_EVERYTHING, intelligence_policy=_blocked_policy({"execution": True}))
    entries = {row["id"]: row for row in _get(review, "run-plan").json()["plan"]["entries"]}
    assert entries["ai.investigators"]["status"] == "run"
    assert entries["checks.test"]["status"] == "not_available"
    assert _EXECUTION_BLOCK in entries["checks.test"]["reason"]
    assert _post(review, "ai-review", {"trigger": "manual"}).status_code == 200
    assert calls


@pytest.mark.parametrize("block", [None, {"ai": False, "execution": False}])
def test_no_organization_block_plans_as_before(tmp_path: Path, block: Any) -> None:
    review = _review(tmp_path, base_config=_EVERYTHING, intelligence_policy=_blocked_policy(block))
    entries = {row["id"]: row for row in _get(review, "run-plan").json()["plan"]["entries"]}
    assert entries["ai.investigators"]["status"] == "run"
    assert "blocked by organization" not in json.dumps(entries)


@pytest.mark.parametrize("block", [{"ai": "yes"}, ["ai"], {"ai": True, "note": "x"}])
def test_a_malformed_organization_block_blocks_everything(tmp_path: Path, block: Any) -> None:
    review = _review(tmp_path, base_config=_EVERYTHING, intelligence_policy=_blocked_policy(block))
    entries = {row["id"]: row for row in _get(review, "run-plan").json()["plan"]["entries"]}
    assert entries["ai.investigators"]["status"] == "not_available"
    assert entries["checks.test"]["status"] == "not_available"
    assert "malformed" in entries["ai.investigators"]["reason"]
