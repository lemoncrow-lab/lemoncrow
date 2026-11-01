from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from lemoncrow.pro.capabilities.review.browser_checks import BrowserPolicy, BrowserScenario
from lemoncrow.pro.capabilities.review.runtime_validation import (
    _read_validation_record,
    automatic_runtime_surface_choices,
    browser_policy_digest,
    build_browser_validation_request,
    build_surface_validation_request,
    execute_local_surface_validation,
    normalize_surface_run,
    persist_validation_record,
    runtime_config_digest,
    surface_validation_scenario,
    trusted_policy_digest,
    validate_artifact_reference,
    validate_queue_completion_receipt,
    validate_reproduction_proposal,
)
from lemoncrow.pro.capabilities.review.runtimes import RunnerRegistry, SurfaceRunResult
from lemoncrow.pro.capabilities.review.session_models import (
    ReviewEvidence,
    ReviewRevision,
    ReviewSession,
    ReviewUnit,
)
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_revision_id, new_session_id
from lemoncrow.pro.capabilities.review.surfaces import (
    ReviewSurface,
    ReviewSurfaceConfig,
    RuntimeConfigEntry,
    SurfaceConfigEntry,
    SurfaceContext,
)


def _revision(**overrides: object) -> ReviewRevision:
    base = ReviewRevision(
        id="rr-one",
        review_id="review-one",
        revision_number=1,
        range_mode="commit_range",
        tree_fingerprint="tree-one",
        source_fingerprint="source-one",
        packet_schema_version=1,
        base_sha="base",
        head_sha="head",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _runtime(**values: object) -> RuntimeConfigEntry:
    config = {"command": ["python", "-m", "pytest"], "timeout": 60, **values}
    return RuntimeConfigEntry(id="tests", runner="command", values=config)


def _surface(**overrides: object) -> ReviewSurface:
    base = ReviewSurface(
        id="unit-tests",
        provider="fixture",
        kind="test.command",
        title="Unit tests",
        runtime="tests",
        affected_paths=("src/app.py",),
        capabilities=("source", "execute"),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _request(
    *,
    revision: ReviewRevision | None = None,
    runtime: RuntimeConfigEntry | None = None,
    surface: ReviewSurface | None = None,
    policy: str | None = None,
):
    return build_surface_validation_request(
        revision or _revision(),
        surface or _surface(),
        runtime or _runtime(),
        policy_digest=policy or trusted_policy_digest("version: 1"),
        target_ids=("target:app",),
    )


def _stored_validation_context(
    tmp_path: Path,
) -> tuple[ReviewStore, ReviewRevision, SurfaceContext, ReviewSurfaceConfig, ReviewSurface]:
    store = ReviewStore(tmp_path / "store")
    store.init()
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="local_change",
            repo_root=str(tmp_path.resolve()),
            range_mode="commit_range",
            title="runtime fixture",
        )
    )
    revision = store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=1,
            range_mode="commit_range",
            tree_fingerprint="tree-runtime-one",
            source_fingerprint="source-runtime-one",
            packet_schema_version=1,
            base_sha="base",
            head_sha="head",
        ),
        (
            ReviewUnit(
                revision_id="",
                unit_key="file:src/app.py",
                kind="file",
                path="src/app.py",
                content_fingerprint="content-one",
            ),
        ),
    )
    trusted_runtime = RuntimeConfigEntry(
        id="tests",
        runner="fixture-runner",
        values={"command": ["python", "-m", "trusted_check"], "expected_exit_code": 0},
    )
    trusted_config = ReviewSurfaceConfig(runtimes=(trusted_runtime,))
    reviewed_runtime = RuntimeConfigEntry(
        id="tests",
        runner="fixture-runner",
        values={"command": ["python", "-m", "change_authored_command"], "expected_exit_code": 0},
    )
    context = SurfaceContext(
        repo_root=tmp_path / "mutable-checkout",
        store_root=store.root,
        revision=revision,
        changed_paths=("src/app.py",),
        config=ReviewSurfaceConfig(runtimes=(reviewed_runtime,)),
        store=store,
    )
    context.repo_root.mkdir()
    (context.repo_root / "src").mkdir()
    (context.repo_root / "src" / "app.py").write_text("MUTABLE WORKTREE\n", encoding="utf-8")
    surface = ReviewSurface(
        id="tests",
        provider="fixture",
        kind="test.command",
        title="Trusted checks",
        runtime="tests",
        affected_paths=("src/app.py",),
        capabilities=("source", "execute"),
    )
    return store, revision, context, trusted_config, surface


def _durable_receipt(request, *, job_id: str = "queue-job-one", result_digest: str = "b" * 64):
    import json

    content = {
        "kind": "runtime-validation-queue-completion/v1",
        "org_id": "org-one",
        "repo_id": "repo-one",
        "review_id": request.review_id,
        "revision_id": request.revision_id,
        "request_identity": request.identity,
        "job_id": job_id,
        "attempt": 1,
        "contract_digest": "a" * 64,
        "result_digest": result_digest,
        "queue_started_at": 123.0,
        "queue_completed_at": 125.0,
        "time_basis": "durable_queue_lifecycle",
    }
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**content, "receipt_id": f"queue-receipt:{digest}"}


def test_hosted_completion_persists_exact_queue_job_and_replay(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.evaluation_timeline import _verified_artifact

    store, revision, _context, config, surface = _stored_validation_context(tmp_path)
    runtime = config.runtimes[0]
    request = build_surface_validation_request(
        revision, surface, runtime, policy_digest=trusted_policy_digest("version: 1")
    )
    scenario = surface_validation_scenario(surface, runtime)
    record = normalize_surface_run(
        request,
        scenario,
        SurfaceRunResult(
            surface.id, surface.provider, "new", "passed", "exit 0", runtime.runner, runtime.id, exit_code=0
        ),
    )
    receipt = _durable_receipt(request)
    kwargs = {"source": "external", "execution_intent": "current", "queue_completion_receipt": receipt}
    first = persist_validation_record(store, revision, request, scenario, record, **kwargs)
    assert _read_validation_record(store, first, request)["queue_completion_receipt"] == receipt
    assert _verified_artifact(store, first) is not None
    assert first.source_ref.endswith(":fresh:queue-job-one")
    assert persist_validation_record(store, revision, request, scenario, record, **kwargs).id == first.id
    # The store itself closes cross-replica read-then-insert races; a second
    # writer cannot insert another row even if it passed the earlier lookup.
    with pytest.raises(ValueError, match="already has immutable evidence"):
        store.add_evidence(replace(first, id="ev-race-loser"))
    assert len(store.list_evidence(request.review_id)) == 1

    # Another fenced queue completion is a separate immutable attempt, not a rewrite.
    receipt_two = _durable_receipt(request, job_id="queue-job-two", result_digest="c" * 64)
    second = persist_validation_record(
        store,
        revision,
        request,
        scenario,
        record,
        source="external",
        execution_intent="current",
        queue_completion_receipt=receipt_two,
    )
    assert second.id != first.id
    assert _read_validation_record(store, first, request)["queue_completion_receipt"] == receipt
    assert len(store.list_evidence(request.review_id)) == 2

    # Same queue job, different normalized result: conflicting replay, not reuse.
    changed = {**record, "status": "FAIL", "status_reasons": ["changed"]}
    with pytest.raises(ValueError, match="replay conflicts"):
        persist_validation_record(store, revision, request, scenario, changed, **kwargs)


def test_worker_report_and_queue_receipt_never_claim_independent_process_attestation(tmp_path: Path) -> None:
    """A content digest and fenced queue lifecycle are not signed process proof."""
    from lemoncrow.pro.capabilities.review.evaluation_timeline import execution_receipt
    from lemoncrow.pro.capabilities.review.execution import (
        ExecutionRecipe,
        ExecutionSubject,
        ExecutorClass,
        StageOutcome,
    )
    from lemoncrow.pro.capabilities.review.hermetic import HermeticPolicy, build_execution_result

    store, revision, _context, config, surface = _stored_validation_context(tmp_path)
    runtime = config.runtimes[0]
    request = build_surface_validation_request(
        revision, surface, runtime, policy_digest=trusted_policy_digest("version: 1")
    )
    scenario = surface_validation_scenario(surface, runtime)
    execution = build_execution_result(
        subject=ExecutionSubject(
            review_id=revision.review_id,
            revision_id=revision.id,
            side="new",
            snapshot_identity=request.snapshot_identity,
        ),
        executor_class=ExecutorClass.CUSTOMER_WORKER,
        policy=HermeticPolicy(),
        recipe=ExecutionRecipe(command=("python3", "-m", "trusted_check")),
        stages=(StageOutcome("run", "passed", exit_code=0),),
        namespace="head_untrusted",
        content_access="ai_denied",
        image_digest="worker:sha256:reported",
    )
    normalized = normalize_surface_run(
        request,
        scenario,
        SurfaceRunResult(
            surface.id,
            surface.provider,
            "new",
            "passed",
            "exit 0",
            runtime.runner,
            runtime.id,
            exit_code=0,
            data={"execution": execution.to_wire()},
        ),
    )
    plain = persist_validation_record(
        store,
        revision,
        request,
        scenario,
        normalized,
        source="external",
        execution_intent="current",
        executor_class=ExecutorClass.CUSTOMER_WORKER,
    )
    queued = persist_validation_record(
        store,
        revision,
        request,
        scenario,
        normalized,
        source="external",
        execution_intent="current",
        executor_class=ExecutorClass.CUSTOMER_WORKER,
        queue_completion_receipt=_durable_receipt(request),
    )
    for evidence in (plain, queued):
        receipt = execution_receipt(store, evidence)
        assert receipt is not None
        assert receipt["execution_provenance"] == "validated"
        assert receipt["status"] == "PASS"
        assert receipt["attempt_lifecycle"] == "observed_run"
        assert receipt["executor_class"] == "customer_worker"
        assert receipt["independent_execution"] is False
        assert receipt["trust"] == "typed_observation"
        # Metadata claiming to be signed is not a verifier of the actual
        # process statement. Only the separate hosted signature gate can do so.
        labeled = execution_receipt(store, replace(evidence, attestation_class="signed_attestation"))
        assert labeled is not None and labeled["independent_execution"] is False
    assert plain.id != queued.id


def test_evaluation_receipt_reads_protected_artifact_bytes_before_exposing_inspection_role(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.evaluation_timeline import execution_receipt
    from lemoncrow.pro.capabilities.review.execution import (
        ExecutionRecipe,
        ExecutionSubject,
        ExecutorClass,
        StageOutcome,
    )
    from lemoncrow.pro.capabilities.review.hermetic import HermeticPolicy, build_execution_result
    from lemoncrow.pro.capabilities.review.run_evidence import protect_bytes

    store, revision, _context, config, surface = _stored_validation_context(tmp_path)
    runtime = config.runtimes[0]
    request = build_surface_validation_request(
        revision, surface, runtime, policy_digest=trusted_policy_digest("version: 1")
    )
    scenario = surface_validation_scenario(surface, runtime)
    run_store = store.run_artifacts()
    assert run_store is not None
    artifact = run_store.put(
        revision.review_id,
        protect_bytes(b"test passed")[0],
        role="stdout",
        media_type="text/plain; charset=utf-8",
    )
    result = build_execution_result(
        subject=ExecutionSubject(
            review_id=revision.review_id,
            revision_id=revision.id,
            side="new",
            snapshot_identity=request.snapshot_identity,
        ),
        executor_class=ExecutorClass.AUTHOR_MACHINE,
        policy=HermeticPolicy(),
        recipe=ExecutionRecipe(command=("python3", "-m", "trusted_check")),
        stages=(StageOutcome("run", "passed", exit_code=0),),
        namespace="head_untrusted",
        content_access="ai_denied",
        artifacts=(artifact,),
        image_digest="host:sha256:example",
    )
    normalized = normalize_surface_run(
        request,
        scenario,
        SurfaceRunResult(
            surface.id,
            surface.provider,
            "new",
            "passed",
            "exit 0",
            runtime.runner,
            runtime.id,
            exit_code=0,
            data={"execution": result.to_wire()},
        ),
    )
    evidence = persist_validation_record(store, revision, request, scenario, normalized)
    receipt = execution_receipt(store, evidence)
    assert receipt is not None
    assert receipt["execution_provenance"] == "validated"
    assert receipt["artifact_roles"] == ["stdout"]
    assert receipt["unavailable_artifact_roles"] == []

    (store.root / "review" / "run-artifacts" / revision.review_id / f"{artifact.digest}.bin").write_bytes(
        b"tampered data"
    )
    after = execution_receipt(store, evidence)
    assert after is not None
    assert after["execution_provenance"] == "validated"
    assert after["artifact_roles"] == []
    assert after["unavailable_artifact_roles"] == ["stdout"]
    assert "run-artifact:" not in str(after)


def test_hosted_queue_receipt_rejects_corruption_and_cross_snapshot(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.evaluation_timeline import _verified_artifact

    store, revision, _context, config, surface = _stored_validation_context(tmp_path)
    runtime = config.runtimes[0]
    request = build_surface_validation_request(
        revision, surface, runtime, policy_digest=trusted_policy_digest("version: 1")
    )
    scenario = surface_validation_scenario(surface, runtime)
    record = normalize_surface_run(
        request,
        scenario,
        SurfaceRunResult(
            surface.id, surface.provider, "new", "failed", "exit 2", runtime.runner, runtime.id, exit_code=2
        ),
    )
    receipt = _durable_receipt(request)
    evidence = persist_validation_record(
        store,
        revision,
        request,
        scenario,
        record,
        source="external",
        execution_intent="current",
        queue_completion_receipt=receipt,
    )
    wrong_ref = replace(evidence, source_ref="runtime-validation:stolen")
    with pytest.raises(ValueError, match="different hosted queue association"):
        _read_validation_record(store, wrong_ref, request)
    assert _verified_artifact(store, wrong_ref) is None
    corrupted = {**receipt, "result_digest": "d" * 64}
    with pytest.raises(ValueError, match="digest mismatch"):
        validate_queue_completion_receipt(corrupted, request)
    with pytest.raises(ValueError, match="digest mismatch"):
        persist_validation_record(
            store,
            revision,
            request,
            scenario,
            record,
            source="external",
            execution_intent="current",
            queue_completion_receipt=corrupted,
        )
    with pytest.raises(ValueError, match="exact job and Snapshot"):
        validate_queue_completion_receipt(receipt, replace(request, revision_id="rr-another"))
    with pytest.raises(ValueError, match="exact job and Snapshot"):
        validate_queue_completion_receipt(receipt, request, job_id="queue-job-foreign")
    assert len(store.list_evidence(request.review_id)) == 1


def test_automatic_snapshot_runtime_selection_is_trusted_bounded_and_policy_authorized() -> None:
    required = _surface(id="required", metadata={"surface_id": "required"})
    opted = _surface(id="opted", metadata={"surface_id": "opted", "auto_run": False})
    manual = _surface(id="manual", metadata={"surface_id": "manual", "auto_run": True})
    other_locus = _surface(id="other-locus", metadata={"surface_id": "other-locus"})
    external = _surface(id="external", metadata={"surface_id": "external"})
    unavailable = _surface(id="unavailable", metadata={"surface_id": "unavailable"})
    no_runtime = _surface(id="no-runtime", runtime="missing", metadata={"surface_id": "no-runtime"})
    no_execute = _surface(id="no-execute", capabilities=("source",), metadata={"surface_id": "no-execute"})
    spoof = _surface(id="spoof", metadata={"surface_id": "unconfigured", "auto_run": True})
    wrong_config = _surface(id="wrong-config", metadata={"surface_id": "wrong-config", "auto_run": True})
    choices = (required, opted, manual, other_locus, external, unavailable, no_runtime, no_execute, spoof, wrong_config)
    config = ReviewSurfaceConfig(
        runtimes=(_runtime(),),
        surfaces=tuple(
            SurfaceConfigEntry(
                id=surface.id,
                provider="fixture",
                values={
                    "runtime": "different" if surface.id == "wrong-config" else surface.runtime,
                    "auto_run": surface.id != "required",
                },
            )
            for surface in choices
            if surface.id != "spoof"
        ),
    )
    from lemoncrow.pro.capabilities.review.planned_execution import surface_entry_id

    entries = {
        surface_entry_id(surface): SimpleNamespace(
            id=surface_entry_id(surface),
            status="run" if surface.id != "unavailable" else "limited_by_budget",
            when="manual" if surface.id == "manual" else "push",
            available=surface.id != "unavailable",
            external=surface.id == "external",
            executor="local" if surface.id == "other-locus" else "managed",
            required=surface.id == "required",
        )
        for surface in choices
    }
    plan = SimpleNamespace(
        review_id="review-one",
        revision_id="rr-one",
        policy_error="",
        entry=lambda key: entries.get(key),
    )
    selected = automatic_runtime_surface_choices(
        plan,
        (*reversed(choices), opted),
        config,
        review_id="review-one",
        revision_id="rr-one",
    )
    assert {surface.id for surface in selected} == {"required", "opted"}
    assert len(selected) == 2
    assert (
        automatic_runtime_surface_choices(
            plan,
            choices,
            config,
            review_id="foreign",
            revision_id="rr-one",
        )
        == ()
    )
    assert (
        automatic_runtime_surface_choices(
            plan,
            choices,
            config,
            review_id="review-one",
            revision_id="rr-old",
        )
        == ()
    )
    assert (
        automatic_runtime_surface_choices(
            SimpleNamespace(**{**vars(plan), "policy_error": "untrusted base"}),
            choices,
            config,
            review_id="review-one",
            revision_id="rr-one",
        )
        == ()
    )
    assert (
        automatic_runtime_surface_choices(
            plan,
            choices,
            config,
            review_id="review-one",
            revision_id="rr-one",
            limit=100,
        )
        == ()
    )


def test_automatic_snapshot_runtime_selection_caps_three_and_requires_execution_opt_in() -> None:
    from lemoncrow.pro.capabilities.review.planned_execution import surface_entry_id

    surfaces = tuple(
        _surface(id=f"check-{i}", metadata={"surface_id": f"check-{i}", "auto_run": False}) for i in range(7)
    )
    config = ReviewSurfaceConfig(
        runtimes=(_runtime(),),
        surfaces=tuple(
            SurfaceConfigEntry(id=surface.id, provider="fixture", values={"runtime": "tests", "auto_run": True})
            for surface in surfaces
        ),
    )
    entries = {
        surface_entry_id(surface): SimpleNamespace(
            status="run",
            when="push",
            available=True,
            external=False,
            executor="worker",
            required=False,
        )
        for surface in surfaces
    }
    plan = SimpleNamespace(
        review_id="review-one",
        revision_id="rr-one",
        policy_error="",
        entry=entries.get,
    )
    chosen = automatic_runtime_surface_choices(
        plan,
        tuple(reversed(surfaces)),
        config,
        review_id="review-one",
        revision_id="rr-one",
    )
    assert [surface.id for surface in chosen] == ["check-0", "check-1", "check-2"]


def test_semantic_identity_is_stable_and_ignores_materialization_paths() -> None:
    first = _request()
    second = _request()
    assert first.identity == second.identity
    wire = first.to_wire()
    assert "/tmp/" not in str(wire)
    assert "workspace" not in str(wire)

    assert _request(revision=_revision(id="rr-two", tree_fingerprint="tree-two")).identity != first.identity
    assert _request(runtime=_runtime(timeout=61)).identity != first.identity
    assert _request(policy=trusted_policy_digest("version: 2")).identity != first.identity


def test_runtime_digest_changes_only_with_semantic_runtime_configuration() -> None:
    assert runtime_config_digest(_runtime()) == runtime_config_digest(_runtime())
    assert runtime_config_digest(_runtime(timeout=99)) != runtime_config_digest(_runtime())

    first = RuntimeConfigEntry(
        id="tests",
        runner="command",
        values={
            "command": ["/tmp/lc-run-a/python", "-m", "pytest"],
            "workspace_path": "/tmp/lc-run-a/workspace",
            "timeout": 60,
        },
    )
    second = RuntimeConfigEntry(
        id="tests",
        runner="command",
        values={
            "command": ["/tmp/lc-run-b/python", "-m", "pytest"],
            "workspace_path": "/tmp/lc-run-b/workspace",
            "timeout": 60,
        },
    )
    assert runtime_config_digest(first) == runtime_config_digest(second)
    assert runtime_config_digest(replace(second, values={**second.values, "timeout": 61})) != runtime_config_digest(
        first
    )


def test_browser_identity_reuses_authoritative_browser_scenario_and_policy() -> None:
    scenario = BrowserScenario(
        id="checkout",
        title="Checkout",
        route="/checkout",
        paths=("web/**",),
        required=True,
        viewports=("1280x800",),
    )
    policy = BrowserPolicy(scenarios=(scenario,))
    digest = browser_policy_digest(policy)
    request = build_browser_validation_request(
        _revision(),
        scenario,
        viewport_key="1280x800",
        policy_digest=digest,
        provider="playwright",
    )
    assert request.scenario_kind == "browser"
    assert request.scenario_id == "checkout"
    assert request.viewport_key == "1280x800"

    changed = BrowserPolicy(scenarios=(replace(scenario, required=False),))
    assert browser_policy_digest(changed) != digest


def test_command_exit_code_is_a_narrow_deterministic_check() -> None:
    request = _request()
    runtime = _runtime()
    scenario_surface = _surface()
    from lemoncrow.pro.capabilities.review.runtime_validation import surface_validation_scenario

    scenario = surface_validation_scenario(scenario_surface, runtime, target_ids=request.target_ids)
    passed = normalize_surface_run(
        request,
        scenario,
        SurfaceRunResult(
            scenario_surface.id,
            scenario_surface.provider,
            "new",
            "passed",
            "command exited 0",
            "command",
            runtime.id,
            exit_code=0,
        ),
    )
    assert passed["status"] == "PASS"
    assert passed["deterministic_checks"][0]["name"] == "process exit code"
    assert "only the configured runtime command" in passed["deterministic_checks"][0]["detail"]

    failed = normalize_surface_run(
        request,
        scenario,
        SurfaceRunResult(
            scenario_surface.id,
            scenario_surface.provider,
            "new",
            "failed",
            "command exited 2",
            "command",
            runtime.id,
            exit_code=2,
        ),
    )
    assert failed["status"] == "FAIL"


def test_reporter_pass_without_inspectable_checks_is_unknown() -> None:
    runtime = RuntimeConfigEntry(id="opaque", runner="custom", values={})
    surface = _surface(runtime="opaque")
    request = _request(runtime=runtime, surface=surface)
    from lemoncrow.pro.capabilities.review.runtime_validation import surface_validation_scenario

    scenario = replace(surface_validation_scenario(surface, runtime), expected_exit_code=None)
    record = normalize_surface_run(
        request,
        scenario,
        SurfaceRunResult(
            surface.id,
            surface.provider,
            "new",
            "passed",
            "looks good",
            "custom",
            runtime.id,
        ),
    )
    assert record["status"] == "UNKNOWN"
    assert record["deterministic_checks"] == []


def test_unavailable_before_execution_is_not_run_but_partial_execution_is_unknown() -> None:
    request = _request()
    from lemoncrow.pro.capabilities.review.runtime_validation import surface_validation_scenario

    scenario = surface_validation_scenario(_surface(), _runtime())
    not_run = normalize_surface_run(
        request,
        scenario,
        SurfaceRunResult(
            "unit-tests",
            "fixture",
            "new",
            "unavailable",
            "bubblewrap unavailable",
            "command",
            "tests",
        ),
    )
    assert not_run["status"] == "NOT_RUN"

    partial = normalize_surface_run(
        request,
        scenario,
        SurfaceRunResult(
            "unit-tests",
            "fixture",
            "new",
            "unavailable",
            "command timed out",
            "command",
            "tests",
            duration_ms=5000,
        ),
    )
    assert partial["status"] == "UNKNOWN"


def test_http_status_is_inspectable_and_explicit_expectation_wins() -> None:
    runtime = RuntimeConfigEntry(id="api", runner="http-service", values={"command": ["python", "app.py"]})
    surface = _surface(
        id="health",
        kind="api.request",
        runtime="api",
        metadata={"expected_status": 204},
    )
    request = _request(runtime=runtime, surface=surface)
    from lemoncrow.pro.capabilities.review.runtime_validation import surface_validation_scenario

    scenario = surface_validation_scenario(surface, runtime)
    good = normalize_surface_run(
        request,
        scenario,
        SurfaceRunResult(
            surface.id,
            surface.provider,
            "new",
            "passed",
            "GET /health returned HTTP 204",
            "http-service",
            runtime.id,
            exit_code=0,
            data={"status_code": 204, "assertions": [], "assertions_executed": False},
        ),
    )
    assert good["status"] == "PASS"

    bad = normalize_surface_run(
        request,
        scenario,
        SurfaceRunResult(
            surface.id,
            surface.provider,
            "new",
            "passed",
            "GET /health returned HTTP 200",
            "http-service",
            runtime.id,
            exit_code=0,
            data={"status_code": 200, "assertions": [], "assertions_executed": False},
        ),
    )
    assert bad["status"] == "FAIL"


def test_artifact_reference_requires_same_review_revision_kind_and_hash() -> None:
    request = _request()
    evidence = ReviewEvidence(
        id="ev-shot",
        review_id=request.review_id,
        revision_id=request.revision_id,
        kind="screenshot",
        title="shot",
        content_hash="a" * 64,
        mime_type="image/png",
    )
    assert validate_artifact_reference(evidence, request, "screenshot") is evidence
    with pytest.raises(ValueError, match="same exact Snapshot"):
        validate_artifact_reference(replace(evidence, revision_id="rr-other"), request, "screenshot")
    with pytest.raises(ValueError, match="same exact Snapshot"):
        validate_artifact_reference(replace(evidence, review_id="other"), request, "screenshot")
    with pytest.raises(ValueError, match="wrong kind"):
        validate_artifact_reference(replace(evidence, kind="video"), request, "screenshot")
    with pytest.raises(ValueError, match="missing"):
        validate_artifact_reference(None, request, "screenshot")


def test_reproduction_proposal_is_only_a_pointer_to_trusted_configuration() -> None:
    proposal = validate_reproduction_proposal(
        {
            "scenario_id": "checkout",
            "target_ids": ["target:app"],
            "reason": "Reproduce the failed refresh path",
            "expected_behavior": "Cart remains populated",
        },
        configured_scenario_ids=("checkout",),
        review_target_ids=("target:app", "target:other"),
    )
    assert proposal.scenario_id == "checkout"

    with pytest.raises(ValueError, match="unknown or untrusted"):
        validate_reproduction_proposal(
            {**proposal.to_wire(), "scenario_id": "invented"},
            configured_scenario_ids=("checkout",),
            review_target_ids=("target:app",),
        )
    with pytest.raises(ValueError, match="unsupported fields"):
        validate_reproduction_proposal(
            {**proposal.to_wire(), "command": "rm -rf /"},
            configured_scenario_ids=("checkout",),
            review_target_ids=("target:app",),
        )
    with pytest.raises(ValueError, match="belong to this Review"):
        validate_reproduction_proposal(
            {**proposal.to_wire(), "target_ids": ["target:foreign"]},
            configured_scenario_ids=("checkout",),
            review_target_ids=("target:app",),
        )


def test_local_validation_uses_trusted_runtime_and_converges_duplicate_run(tmp_path: Path) -> None:
    store, revision, context, trusted_config, surface = _stored_validation_context(tmp_path)
    seen_commands: list[tuple[str, ...]] = []
    run_count = 0

    class Runner:
        runner_id = "fixture-runner"

        def setup_candidates(self, _repo_root: Path):
            return ()

        def run(self, run_context, runtime, run_surface, *, side):
            nonlocal run_count
            run_count += 1
            seen_commands.append(runtime.strings("command"))
            configured = run_context.config.runtime("tests")
            assert configured is not None
            assert configured.strings("command") == ("python", "-m", "trusted_check")
            assert run_surface is surface
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "passed",
                "trusted command exited 0",
                self.runner_id,
                runtime.id,
                exit_code=0,
            )

    registry = RunnerRegistry((Runner(),))
    first = execute_local_surface_validation(
        store=store,
        registry=registry,
        context=context,
        trusted_config=trusted_config,
        surface=surface,
        policy_digest=trusted_policy_digest("trusted-base"),
    )
    second = execute_local_surface_validation(
        store=store,
        registry=registry,
        context=context,
        trusted_config=trusted_config,
        surface=surface,
        policy_digest=trusted_policy_digest("trusted-base"),
    )
    assert first.record["status"] == "PASS"
    assert second.reused is True
    assert first.evidence.id == second.evidence.id
    assert first.evidence.source_ref.startswith("runtime-validation:surface:fixture:tests:")
    assert first.evidence.attestation_class == "lemoncrow_verification"
    assert first.evidence.stable_key == first.request.scenario_id
    assert first.evidence.remote_identity == first.request.identity
    assert len(store.list_evidence(revision.review_id)) == 1
    assert store.list_marks(revision.review_id) == ()


def test_validation_identity_keeps_old_result_stale_after_new_snapshot(tmp_path: Path) -> None:
    store, revision, context, trusted_config, surface = _stored_validation_context(tmp_path)

    class Runner:
        runner_id = "fixture-runner"

        def setup_candidates(self, _repo_root: Path):
            return ()

        def run(self, _context, runtime, _surface, *, side):
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "passed",
                "ok",
                self.runner_id,
                runtime.id,
                exit_code=0,
            )

    first = execute_local_surface_validation(
        store=store,
        registry=RunnerRegistry((Runner(),)),
        context=context,
        trusted_config=trusted_config,
        surface=surface,
        policy_digest=trusted_policy_digest("trusted-base"),
    )
    next_revision = store.add_revision(
        replace(
            revision,
            id=new_revision_id(),
            revision_number=2,
            tree_fingerprint="tree-runtime-two",
            source_fingerprint="source-runtime-two",
        ),
        (
            ReviewUnit(
                revision_id="",
                unit_key="file:src/app.py",
                kind="file",
                path="src/app.py",
                content_fingerprint="content-two",
            ),
        ),
    )
    second = execute_local_surface_validation(
        store=store,
        registry=RunnerRegistry((Runner(),)),
        context=replace(context, revision=next_revision),
        trusted_config=trusted_config,
        surface=surface,
        policy_digest=trusted_policy_digest("trusted-base"),
    )

    assert first.request.identity != second.request.identity
    assert first.evidence.revision_id == revision.id
    assert second.evidence.revision_id == next_revision.id
    assert first.evidence.id != second.evidence.id
    assert len(store.list_evidence(revision.review_id)) == 2


def test_current_runtime_result_is_not_published_after_snapshot_supersession(tmp_path: Path) -> None:
    store, revision, context, trusted_config, surface = _stored_validation_context(tmp_path)

    class SupersedingRunner:
        runner_id = "fixture-runner"

        def setup_candidates(self, _repo_root: Path):
            return ()

        def run(self, _context, runtime, _surface, *, side):
            store.add_revision(
                replace(
                    revision,
                    id=new_revision_id(),
                    revision_number=2,
                    tree_fingerprint="tree-runtime-superseding",
                    source_fingerprint="source-runtime-superseding",
                ),
                (
                    ReviewUnit(
                        revision_id="",
                        unit_key="file:src/app.py",
                        kind="file",
                        path="src/app.py",
                        content_fingerprint="content-superseding",
                    ),
                ),
            )
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "passed",
                "old Snapshot completed late",
                self.runner_id,
                runtime.id,
                exit_code=0,
            )

    with pytest.raises(ValueError, match="superseded"):
        execute_local_surface_validation(
            store=store,
            registry=RunnerRegistry((SupersedingRunner(),)),
            context=context,
            trusted_config=trusted_config,
            surface=surface,
            policy_digest=trusted_policy_digest("trusted-base"),
            expected_current_revision_id=revision.id,
        )
    assert store.list_evidence(revision.review_id) == ()


def test_explicit_historical_runtime_evidence_never_projects_as_current_until_current_rerun(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.verification import build_verification_projection

    store, revision, context, trusted_config, surface = _stored_validation_context(tmp_path)
    store.set_current_revision(revision.review_id, revision.id)
    runs = 0

    class Runner:
        runner_id = "fixture-runner"

        def setup_candidates(self, _repo_root: Path):
            return ()

        def run(self, _context, runtime, _surface, *, side):
            nonlocal runs
            runs += 1
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "passed",
                "ok",
                self.runner_id,
                runtime.id,
                exit_code=0,
            )

    registry = RunnerRegistry((Runner(),))
    historical = execute_local_surface_validation(
        store=store,
        registry=registry,
        context=context,
        trusted_config=trusted_config,
        surface=surface,
        policy_digest=trusted_policy_digest("trusted-base"),
        execution_intent="explicit_historical",
    )
    assert historical.evidence.execution_intent == "explicit_historical"
    assert historical.evidence.source_ref.endswith(":explicit_historical")
    projection = build_verification_projection(
        packet=None,
        evidence=store.list_evidence(revision.review_id),
        revision=revision,
        revisions=store.list_revisions(revision.review_id),
    )
    assert projection["summary"]["pass"] == 0

    current = execute_local_surface_validation(
        store=store,
        registry=registry,
        context=context,
        trusted_config=trusted_config,
        surface=surface,
        policy_digest=trusted_policy_digest("trusted-base"),
        execution_intent="current",
        expected_current_revision_id=revision.id,
    )
    assert current.evidence.execution_intent == "current"
    assert current.evidence.id != historical.evidence.id
    assert runs == 2
    projection = build_verification_projection(
        packet=None,
        evidence=store.list_evidence(revision.review_id),
        revision=revision,
        revisions=store.list_revisions(revision.review_id),
    )
    assert projection["summary"]["pass"] == 1


def test_unavailable_local_runner_records_not_run_without_human_state(tmp_path: Path) -> None:
    store, revision, context, trusted_config, surface = _stored_validation_context(tmp_path)
    result = execute_local_surface_validation(
        store=store,
        registry=RunnerRegistry(()),
        context=context,
        trusted_config=trusted_config,
        surface=surface,
        policy_digest=trusted_policy_digest("trusted-base"),
    )

    assert result.record["status"] == "NOT_RUN"
    assert result.evidence.verification_status == "NOT_RUN"
    assert store.list_marks(revision.review_id) == ()


def test_artifact_hash_is_verified_when_bytes_are_available() -> None:
    request = _request()
    payload = b"runtime screenshot"
    evidence = ReviewEvidence(
        id="ev-shot-hash",
        review_id=request.review_id,
        revision_id=request.revision_id,
        kind="screenshot",
        title="shot",
        content_hash=hashlib.sha256(payload).hexdigest(),
        mime_type="image/png",
    )
    assert (
        validate_artifact_reference(
            evidence,
            request,
            "screenshot",
            read_artifact=lambda _item: payload,
        )
        is evidence
    )
    with pytest.raises(ValueError, match="content hash"):
        validate_artifact_reference(
            replace(evidence, content_hash="0" * 64),
            request,
            "screenshot",
            read_artifact=lambda _item: payload,
        )


def test_runtime_validation_cleanup_happens_before_return(tmp_path: Path) -> None:
    store, _revision_row, context, trusted_config, surface = _stored_validation_context(tmp_path)

    class Runner:
        runner_id = "fixture-runner"

        def setup_candidates(self, _repo_root: Path):
            return ()

        def run(self, run_context, runtime, _surface, *, side):
            digest = hashlib.sha256(f"{runtime.id}\0{side}".encode()).hexdigest()[:16]
            workspace = (
                run_context.store_root
                / "review"
                / "runtime-runs"
                / run_context.revision.review_id
                / run_context.revision.id
                / self.runner_id
                / digest
                / "workspace"
            )
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "scratch.txt").write_text("temporary", encoding="utf-8")
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "passed",
                "ok",
                self.runner_id,
                runtime.id,
                exit_code=0,
            )

    result = execute_local_surface_validation(
        store=store,
        registry=RunnerRegistry((Runner(),)),
        context=context,
        trusted_config=trusted_config,
        surface=surface,
        policy_digest=trusted_policy_digest("trusted-base"),
    )
    digest = hashlib.sha256(b"tests\0new").hexdigest()[:16]
    workspace = (
        context.store_root
        / "review"
        / "runtime-runs"
        / context.revision.review_id
        / context.revision.id
        / "fixture-runner"
        / digest
        / "workspace"
    )
    assert result.record["status"] == "PASS"
    assert not workspace.exists()
    assert store.list_marks(context.revision.review_id) == ()
