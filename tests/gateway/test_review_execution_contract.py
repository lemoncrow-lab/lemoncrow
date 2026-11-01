"""Execution-result contract, executor class, protection/retention and reproduction (Phase 9R 4, 36, 37, 39)."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from lemoncrow.pro.capabilities.review.ai_review import _evidence_label
from lemoncrow.pro.capabilities.review.api import _evidence
from lemoncrow.pro.capabilities.review.execution import (
    ExecutionRecipe,
    ExecutionResult,
    ExecutionSubject,
    ExecutorClass,
    SinkCapture,
    StageOutcome,
    cache_namespace,
    executor_class_for_locus,
    executor_satisfies,
    normalize_executor_requirement,
    parse_executor_class,
    readable_cache_namespaces,
    verification_row,
)
from lemoncrow.pro.capabilities.review.hermetic import (
    DependencyCache,
    HermeticPolicy,
    build_execution_result,
    load_hermetic_policy,
    provision_npm_dependencies,
    workspace_digest,
)
from lemoncrow.pro.capabilities.review.reproduce import (
    ReproductionError,
    load_execution_record,
    materialize_snapshot,
    reproduce_execution,
)
from lemoncrow.pro.capabilities.review.run_evidence import (
    MaskRegion,
    RedactionPolicy,
    RunArtifactStore,
    mask_png,
    protect_bytes,
    protect_text,
    review_reference_resolver,
)
from lemoncrow.pro.capabilities.review.runtime_validation import (
    execute_local_surface_validation,
    trusted_policy_digest,
)
from lemoncrow.pro.capabilities.review.runtimes import CommandRunner, RunnerRegistry, SurfaceRunResult
from lemoncrow.pro.capabilities.review.session_models import (
    ReviewEvidence,
    ReviewMark,
    ReviewRevision,
    ReviewSession,
    ReviewUnit,
)
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_revision_id, new_session_id
from lemoncrow.pro.capabilities.review.surface_providers import ServiceSurfaceProvider
from lemoncrow.pro.capabilities.review.surfaces import (
    ReviewSurface,
    ReviewSurfaceConfig,
    RuntimeConfigEntry,
    SurfaceContext,
    load_review_surface_config,
)
from lemoncrow.pro.capabilities.review.verification import build_verification_projection

# Secret-shaped values are assembled at runtime so no literal credential lives in the source.
GITHUB_TOKEN = "ghp_" + "Z1" * 18
AWS_KEY = "AKIA" + "QWERTYUIOPASDFGH"


def _bwrap_works() -> bool:
    binary = shutil.which("bwrap")
    if not binary:
        return False
    try:
        proc = subprocess.run(
            [binary, "--unshare-all", "--ro-bind", "/", "/", "true"], capture_output=True, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


needs_bwrap = pytest.mark.skipif(
    not _bwrap_works(), reason="bubblewrap with unprivileged user/network namespaces is unavailable on this host"
)


def _result(
    executor: ExecutorClass = ExecutorClass.AUTHOR_MACHINE,
    *,
    status: str = "passed",
    output: str = "ok",
    **overrides: Any,
) -> ExecutionResult:
    options: dict[str, Any] = {
        "subject": ExecutionSubject(review_id="review-one", revision_id="rr-one", snapshot_identity="tree:abc"),
        "executor_class": executor,
        "policy": HermeticPolicy(),
        "recipe": ExecutionRecipe(command=("python3", "check.py")),
        "stages": (StageOutcome("run", status, exit_code=0 if status == "passed" else 1, output=output),),  # type: ignore[arg-type]
        "namespace": "head_untrusted",
        "content_access": "ai_denied",
        "image_digest": "sha256:img",
        "toolchain": (("python3", "Python 3.13"),),
    }
    options.update(overrides)
    return build_execution_result(**options)


# --------------------------------------------------------------------------- #
# Contract (items 4, 36, 37)
# --------------------------------------------------------------------------- #


def test_identity_binds_subject_executor_and_fingerprint_and_refuses_tampering() -> None:
    result = _result()
    wire = result.to_wire()
    assert ExecutionResult.from_wire(wire).identity == result.identity == wire["identity"]
    assert wire["environment_fingerprint"] == result.fingerprint.digest
    assert wire["environment_fingerprint"].startswith("envfp:sha256:")

    promoted = {**wire, "executor_class": "managed_sandbox"}
    with pytest.raises(ValueError, match="identity"):
        ExecutionResult.from_wire(promoted)
    moved = {**wire, "subject": {**wire["subject"], "workspace_digest": "f" * 64}}
    with pytest.raises(ValueError, match="identity"):
        ExecutionResult.from_wire(moved)
    with pytest.raises(ValueError, match="schema version"):
        ExecutionResult.from_wire({**wire, "schema_version": 99})
    # Outcomes are not identity: a failing rerun of the same execution keeps it.
    assert _result(status="failed").identity == result.identity
    # The environment fingerprint is: another image is another execution.
    assert _result(image_digest="sha256:other").identity != result.identity
    assert result.fingerprint.differences(_result(image_digest="sha256:other").fingerprint) == ("image_digest",)


def test_executor_class_mapping_is_fail_closed() -> None:
    assert parse_executor_class("root") is ExecutorClass.AUTHOR_MACHINE
    assert parse_executor_class(None) is ExecutorClass.AUTHOR_MACHINE
    assert ExecutorClass.AUTHOR_MACHINE.self_attested and not ExecutorClass.MANAGED_SANDBOX.self_attested
    assert executor_class_for_locus("local") is ExecutorClass.AUTHOR_MACHINE
    assert executor_class_for_locus("customer") is ExecutorClass.CUSTOMER_WORKER
    # A self-hosted control plane's internal worker is customer compute, not a LemonCrow sandbox.
    assert executor_class_for_locus("managed", operator="customer") is ExecutorClass.CUSTOMER_WORKER
    assert executor_class_for_locus("managed", operator="lemoncrow") is ExecutorClass.MANAGED_SANDBOX
    assert [item.value for item in ExecutorClass] == ["author_machine", "customer_worker", "managed_sandbox"]


def test_trusted_reservation_never_accepts_author_machine_or_forged_classes() -> None:
    trusted = normalize_executor_requirement("trusted")
    assert trusted == ("customer_worker", "managed_sandbox")
    assert not executor_satisfies("author_machine", trusted)
    assert not executor_satisfies("Managed_Sandbox ", trusted)
    assert not executor_satisfies("", trusted)
    assert executor_satisfies("customer_worker", trusted) and executor_satisfies("managed_sandbox", trusted)
    assert executor_satisfies("author_machine", normalize_executor_requirement("any"))
    with pytest.raises(ValueError, match="unknown executor class"):
        normalize_executor_requirement(["managed_sandbox", "laptop"])


def test_verification_statement_names_the_location_image_and_unverified_integrations() -> None:
    local = _result()
    assert local.verification.statement == "author machine sandbox (self-attested) · image sha256:img · network denied"
    managed = local.with_executor(ExecutorClass.MANAGED_SANDBOX)
    assert managed.verification.statement == "managed sandbox · image sha256:img · network denied"
    assert managed.identity != local.identity
    wire = managed.to_wire()
    assert {row["name"] for row in wire["verification"]["not_verified"]} == {
        "payment providers",
        "third-party APIs",
        "production data",
    }
    assert all(row["status"] == "not_verified" for row in wire["verification"]["not_verified"])
    # An empty list on the wire cannot erase the honesty floor.
    stripped = {**wire, "verification": {**wire["verification"], "not_verified": []}}
    assert ExecutionResult.from_wire(stripped).verification.not_verified


def test_sinks_never_deliver_and_build_run_stages_are_offline() -> None:
    with pytest.raises(ValueError, match="never delivers"):
        SinkCapture(kind="smtp", destination="smtp:x", delivered=True)
    with pytest.raises(ValueError, match="offline"):
        StageOutcome("run", "passed", network="registry_only")
    with pytest.raises(ValueError, match="offline"):
        StageOutcome("build", "passed", network="allowed")
    assert StageOutcome("fetch", "passed", network="registry_only").network == "registry_only"
    assert SinkCapture(kind="http", destination="http://x").to_wire()["delivered"] is False


def test_cache_namespaces_isolate_untrusted_heads() -> None:
    assert cache_namespace("old", "external") == "base"
    assert cache_namespace("new", "unknown") == "head_untrusted"
    assert cache_namespace("new", "trusted_internal") == "head_trusted"
    assert readable_cache_namespaces("head_untrusted") == ("head_untrusted",)
    assert readable_cache_namespaces("base") == ("base",)
    assert readable_cache_namespaces("head_trusted") == ("head_trusted", "base")


# --------------------------------------------------------------------------- #
# Protection before storage and tiered retention (item 39)
# --------------------------------------------------------------------------- #


def test_secrets_and_configured_patterns_are_redacted_and_binary_is_withheld() -> None:
    policy = RedactionPolicy(literals=("fixture-password-77",), patterns=(r"ACME-\d{4}",))
    text, count = protect_text(f"token {GITHUB_TOKEN} key {AWS_KEY} pw fixture-password-77 id ACME-1234", policy)
    assert GITHUB_TOKEN not in text and AWS_KEY not in text
    assert "fixture-password-77" not in text and "ACME-1234" not in text
    assert count >= 4
    secret_binary = b"\xff\xfe" + AWS_KEY.encode()
    protected, _ = protect_bytes(secret_binary, policy)
    assert AWS_KEY.encode() not in protected and protected.startswith(b"<binary payload withheld")
    # Literal secrets never appear in the exported policy.
    assert "fixture-password-77" not in json.dumps(policy.to_wire())


def test_screenshot_regions_are_masked_before_storage() -> None:
    image_module = pytest.importorskip("PIL.Image", reason="Pillow is not installed; masked screenshots are withheld")
    canvas = image_module.new("RGB", (20, 20), (255, 255, 255))
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    masked = mask_png(buffer.getvalue(), (MaskRegion(0, 0, 10, 10),))
    with image_module.open(io.BytesIO(masked)) as out:
        rgb = out.convert("RGB")
        assert rgb.getpixel((5, 5)) == (0, 0, 0)
        assert rgb.getpixel((15, 15)) == (255, 255, 255)


def test_unreferenced_raw_artifacts_expire_and_referenced_ones_are_retained(tmp_path: Path) -> None:
    now = [1_000_000.0]
    store = RunArtifactStore(tmp_path / "runs", retention_seconds=3600, clock=lambda: now[0])

    def put(text: str, **kwargs: Any) -> Any:
        payload, _ = protect_bytes(text.encode())
        return store.put("review-one", payload, role="sink_payload", media_type="text/plain", **kwargs)

    loose = put("loose")
    pinned = put("cited by a finding")
    resolved = put("carried by a required check")
    summary = put("summary", retention="summary")
    store.pin("review-one", pinned.digest, "finding:f-1")
    with pytest.raises(ValueError):
        store.pin("review-one", loose.digest, "because")

    now[0] += 3601
    removed = store.expire(resolver=lambda _review, meta: meta["digest"] == resolved.digest)

    assert removed == (loose.ref,)
    assert store.read("review-one", loose.digest) is None
    for kept in (pinned, resolved, summary):
        assert store.read("review-one", kept.digest) is not None
    assert summary.expires_at is None

    store.unpin("review-one", pinned.digest, "finding:f-1")
    now[0] += 3601
    assert pinned.ref in store.expire()
    store.delete_review("review-one")
    assert not (tmp_path / "runs" / "review-one").exists()


class _MemoryArtifactBackend:
    """A hosted-style Review artifact backend: opaque keys, no local files."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def write(self, key: str, payload: bytes) -> str:
        self.blobs[key] = bytes(payload)
        return ""

    def read(self, key: str) -> bytes | None:
        return self.blobs.get(key)

    def delete_prefix(self, prefix: str) -> None:
        for key in [key for key in self.blobs if key.startswith(prefix)]:
            del self.blobs[key]

    def list_keys(self, prefix: str) -> tuple[str, ...]:
        return tuple(sorted(key for key in self.blobs if key.startswith(prefix)))


class _UnlistableArtifactBackend(_MemoryArtifactBackend):
    list_keys = None  # type: ignore[assignment]


def test_evaluation_receipt_advertises_only_readable_digest_checked_artifacts(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.evaluation_timeline import _available_run_artifact_roles

    store, revision = _stored_review(tmp_path)
    artifacts = store.run_artifacts()
    assert artifacts is not None
    review_id = revision.review_id
    evidence = SimpleNamespace(review_id=review_id)
    good = artifacts.put(review_id, protect_bytes(b"safe output")[0], role="stdout", media_type="text/plain")
    missing = artifacts.put(review_id, protect_bytes(b"lost trace")[0], role="trace", media_type="application/zip")
    cross_tenant = replace(good, ref=f"run-artifact:another-review:{good.digest}")
    oversized = replace(good, bytes=9 * 1024 * 1024)
    forged = replace(good, ref=f"run-artifact:{review_id}:{'a' * 64}")
    bytes_path = store.root / "review" / "run-artifacts" / review_id

    assert _available_run_artifact_roles(store, evidence, (good,)) == (["stdout"], [])
    assert _available_run_artifact_roles(store, evidence, (cross_tenant,)) == ([], ["stdout"])
    assert _available_run_artifact_roles(store, evidence, (oversized,)) == ([], ["stdout"])
    assert _available_run_artifact_roles(store, evidence, (forged,)) == ([], ["stdout"])
    assert _available_run_artifact_roles(store, evidence, (good, forged)) == ([], ["stdout"])
    (bytes_path / f"{missing.digest}.bin").unlink()
    assert _available_run_artifact_roles(store, evidence, (good, missing)) == (["stdout"], ["trace"])
    (bytes_path / f"{good.digest}.bin").write_bytes(b"tampered data")
    assert _available_run_artifact_roles(store, evidence, (good,)) == ([], ["stdout"])
    assert _available_run_artifact_roles(store, evidence, ()) == ([], [])


def test_evaluation_receipt_has_one_aggregate_artifact_read_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review import evaluation_timeline

    store, revision = _stored_review(tmp_path)
    artifacts = store.run_artifacts()
    assert artifacts is not None
    review_id = revision.review_id
    first = artifacts.put(review_id, protect_bytes(b"one-two")[0], role="stdout", media_type="text/plain")
    second = artifacts.put(review_id, protect_bytes(b"three-4")[0], role="stderr", media_type="text/plain")
    evidence = SimpleNamespace(review_id=review_id)
    monkeypatch.setattr(evaluation_timeline, "_MAX_VERIFIED_RUN_BYTES", first.bytes + 1)
    assert evaluation_timeline._available_run_artifact_roles(store, evidence, (first, second)) == (
        ["stdout"],
        ["stderr"],
    )
    assert evaluation_timeline._available_run_artifact_roles(store, evidence, (second, first)) == (
        ["stderr"],
        ["stdout"],
    )


def test_hosted_backend_run_artifacts_keep_the_retention_contract_without_local_files(tmp_path: Path) -> None:
    now = [1_000_000.0]
    backend = _MemoryArtifactBackend()
    store = RunArtifactStore(tmp_path / "local", retention_seconds=3600, clock=lambda: now[0], backend=backend)

    def put(text: str) -> Any:
        return store.put("review-one", protect_bytes(text.encode())[0], role="sink:smtp", media_type="text/plain")

    loose, pinned = put("loose"), put("cited by a finding")
    assert store.hosted and store.read("review-one", loose.digest) == b"loose"
    assert sorted(backend.blobs) == sorted(
        f"review/run-artifacts/review-one/{ref.digest}.{suffix}"
        for ref in (loose, pinned)
        for suffix in ("bin", "json")
    )
    assert not (tmp_path / "local").exists()
    store.pin("review-one", pinned.digest, "finding:f-1")

    now[0] += 3601
    store.maybe_expire()  # no resolver: an unaware pass never deletes cited bytes
    assert store.read("review-one", loose.digest) is not None
    assert store.expire(resolver=lambda _review, _meta: False) == (loose.ref,)
    assert not [key for key in backend.blobs if loose.digest in key]
    assert store.read("review-one", pinned.digest) is not None

    store.delete_review("review-one")
    assert backend.blobs == {}
    with pytest.raises(ValueError):
        store.put("..", protect_bytes(b"x")[0], role="sink:smtp", media_type="text/plain")


def test_a_backend_that_cannot_list_keys_stores_no_run_artifacts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="retention could not be enforced"):
        RunArtifactStore(tmp_path, backend=_UnlistableArtifactBackend())
    assert ReviewStore(tmp_path / "store", artifact_backend=_UnlistableArtifactBackend()).run_artifacts() is None
    local = ReviewStore(tmp_path / "local").run_artifacts()
    assert local is not None and not local.hosted and local.resolver is not None


def _stored_review(tmp_path: Path) -> tuple[ReviewStore, ReviewRevision]:
    store = ReviewStore(tmp_path / "store")
    store.init()
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="local_change",
            repo_root=str(tmp_path.resolve()),
            range_mode="commit_range",
            title="execution fixture",
        )
    )
    revision = store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=1,
            range_mode="commit_range",
            tree_fingerprint="tree-one",
            source_fingerprint="source-one",
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
                content_fingerprint="c",
            ),
        ),
    )
    return store, revision


def test_store_resolver_keeps_check_artifacts_and_review_deletion_closes_the_store(tmp_path: Path) -> None:
    store, revision = _stored_review(tmp_path)
    runs = RunArtifactStore.for_store(store.root, retention_seconds=3600)
    kept = runs.put(
        revision.review_id, protect_bytes(b"check payload")[0], role="sink_payload", media_type="text/plain"
    )
    loose = runs.put(
        revision.review_id, protect_bytes(b"loose payload")[0], role="sink_payload", media_type="text/plain"
    )
    for evidence_id, stable_key, ref in (("ev-check", "tests", kept.ref), ("ev-plain", "", loose.ref)):
        payload = json.dumps({"data": {"execution": {"artifacts": [{"ref": ref}]}}}).encode()
        rel, digest, size = store.write_evidence_artifact(revision.review_id, revision.id, evidence_id, payload)
        store.add_evidence(
            ReviewEvidence(
                id=evidence_id,
                review_id=revision.review_id,
                revision_id=revision.id,
                kind="verification",
                title=evidence_id,
                artifact_path=rel,
                content_hash=digest,
                bytes=size,
                source="test",
                source_ref=evidence_id,
                stable_key=stable_key,
                attestation_class="lemoncrow_verification" if stable_key else "",
                executor_class="author_machine",
            )
        )
    resolver = review_reference_resolver(store)
    assert resolver(revision.review_id, {"digest": kept.digest}) is True
    assert resolver(revision.review_id, {"digest": loose.digest}) is False
    # A Review whose state cannot be read keeps its bytes (no silent loss of cited evidence).
    broken = SimpleNamespace(list_annotations=lambda _rid: (_ for _ in ()).throw(RuntimeError("locked")))
    assert review_reference_resolver(broken)("x", {"digest": "0" * 64}) is True

    store.set_current_revision(revision.review_id, revision.id)
    store.update_session_status_if_current_revision(revision.review_id, revision.id, "archived")
    assert store.delete_archived_session(revision.review_id) is True
    assert not (store.root / "review" / "run-artifacts" / revision.review_id).exists()


def _execution_evidence(store: ReviewStore, revision: ReviewRevision, evidence_id: str, ref: str) -> None:
    """An uncited, unattested execution row (a preview capture) carrying ``ref``."""

    payload = json.dumps({"data": {"execution": {"artifacts": [{"ref": ref}]}}}).encode()
    rel, digest, size = store.write_evidence_artifact(revision.review_id, revision.id, evidence_id, payload)
    store.add_evidence(
        ReviewEvidence(
            id=evidence_id,
            review_id=revision.review_id,
            revision_id=revision.id,
            kind="screenshot",
            title=evidence_id,
            artifact_path=rel,
            content_hash=digest,
            bytes=size,
            source="test",
            source_ref=evidence_id,
            executor_class="author_machine",
        )
    )


def test_human_behavior_judgments_and_observations_keep_their_run_artifacts(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.behavior import ManualCheckItem, manual_check_evidence

    store, revision = _stored_review(tmp_path)
    now = [1_000_000.0]
    runs = RunArtifactStore.for_store(store.root, retention_seconds=3600, clock=lambda: now[0])

    def put(text: bytes) -> Any:
        return runs.put(revision.review_id, protect_bytes(text)[0], role="sink_payload", media_type="text/plain")

    judged = put(b"rendered email, head")
    noted = put(b"named in a judgment note")
    observed = put(b"named in a human observation")
    loose = put(b"nobody looked")
    _execution_evidence(store, revision, "ev-preview", judged.ref)

    def resolve(digest: str) -> bool:
        return review_reference_resolver(store)(revision.review_id, {"digest": digest})

    # Before any human looked, an uncited preview capture is not referenced.
    assert not resolve(judged.digest) and not resolve(loose.digest)

    # An agent's behavior judgment and a human unit (non-behavior) mark keep nothing.
    for unit_key, actor in (("beh:visual:agent", "agent"), ("file:src/app.py", "human")):
        store.set_mark(
            ReviewMark(
                review_id=revision.review_id,
                unit_key=unit_key,
                state="reviewed",
                reviewed_revision_id=revision.id,
                content_fingerprint="fp",
                actor_type=actor,  # type: ignore[arg-type]
                note=noted.ref,
            )
        )
    assert not resolve(judged.digest) and not resolve(noted.digest)

    # A human judgment on a behavior target keeps the Snapshot's execution evidence and any ref it names.
    store.set_mark(
        ReviewMark(
            review_id=revision.review_id,
            unit_key="beh:visual:0123",
            state="needs_changes",
            reviewed_revision_id=revision.id,
            content_fingerprint="fp",
            reviewer_id="alice",
            note=f"email body changed, see {noted.ref}",
        )
    )
    assert resolve(judged.digest) and resolve(noted.digest)
    assert not resolve(observed.digest) and not resolve(loose.digest)

    # A human-observation checklist result keeps the run artifact its note cites.
    item = ManualCheckItem(item_id="mc-email", title="Signup email", integration="email", steps=("send",))
    store.add_evidence(
        manual_check_evidence(
            item,
            review_id=revision.review_id,
            revision_id=revision.id,
            reviewer_id="alice",
            status="PASS",
            note=f"looked right in {observed.ref}",
        )
    )
    assert resolve(observed.digest)
    assert not resolve(loose.digest)

    now[0] += 3601
    removed = runs.expire(resolver=review_reference_resolver(store))
    assert removed == (loose.ref,)
    for kept in (judged, noted, observed):
        assert runs.read(revision.review_id, kept.digest) is not None


def test_a_human_observation_alone_keeps_its_snapshot_execution_evidence(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.behavior import ManualCheckItem, manual_check_evidence

    store, revision = _stored_review(tmp_path)
    runs = RunArtifactStore.for_store(store.root, retention_seconds=3600)
    capture = runs.put(
        revision.review_id, protect_bytes(b"queue body")[0], role="sink_payload", media_type="text/plain"
    )
    _execution_evidence(store, revision, "ev-preview", capture.ref)
    resolver = review_reference_resolver(store)
    assert resolver(revision.review_id, {"digest": capture.digest}) is False
    item = ManualCheckItem(item_id="mc-queue", title="Queue message", integration="queue", steps=("enqueue",))
    store.add_evidence(
        manual_check_evidence(
            item, review_id=revision.review_id, revision_id=revision.id, reviewer_id="bob", status="FAIL"
        )
    )
    assert review_reference_resolver(store)(revision.review_id, {"digest": capture.digest}) is True


# --------------------------------------------------------------------------- #
# Dispatcher stamping and protection on the persisted record
# --------------------------------------------------------------------------- #


def _validation_context(tmp_path: Path) -> tuple[ReviewStore, SurfaceContext, ReviewSurfaceConfig, ReviewSurface]:
    store, revision = _stored_review(tmp_path)
    runtime = RuntimeConfigEntry(id="tests", runner="fixture-runner", values={"command": ["python3", "check.py"]})
    context = SurfaceContext(
        repo_root=tmp_path / "checkout",
        store_root=store.root,
        revision=revision,
        changed_paths=("src/app.py",),
        config=ReviewSurfaceConfig(runtimes=(runtime,)),
        store=store,
    )
    surface = ReviewSurface(
        id="tests",
        provider="fixture",
        kind="test.command",
        title="Trusted checks",
        runtime="tests",
        affected_paths=("src/app.py",),
        capabilities=("source", "execute"),
    )
    return store, context, ReviewSurfaceConfig(runtimes=(runtime,)), surface


def _fixture_registry(data: dict[str, Any], output: str) -> RunnerRegistry:
    class Runner:
        runner_id = "fixture-runner"

        def setup_candidates(self, _repo_root: Path) -> tuple[()]:
            return ()

        def run(self, _context: Any, runtime: Any, surface: Any, *, side: str) -> SurfaceRunResult:
            return SurfaceRunResult(
                surface.id,
                surface.provider,
                side,
                "passed",
                "command exited 0",
                self.runner_id,
                runtime.id,
                exit_code=0,
                output=output,
                data=data,
            )

    return RunnerRegistry((Runner(),))


def test_dispatcher_stamps_executor_class_over_the_runner_claim_and_redacts_before_storage(
    tmp_path: Path,
) -> None:
    store, context, trusted, surface = _validation_context(tmp_path)
    claimed = _result(ExecutorClass.MANAGED_SANDBOX, output=f"leaked {GITHUB_TOKEN}").to_wire()
    registry = _fixture_registry({"execution": claimed, "note": f"aws {AWS_KEY}"}, f"token {GITHUB_TOKEN}")

    outcome = execute_local_surface_validation(
        store=store,
        registry=registry,
        context=context,
        trusted_config=trusted,
        surface=surface,
        policy_digest=trusted_policy_digest("trusted-base"),
    )

    assert outcome.evidence.executor_class == "author_machine"
    stored = store.read_evidence_artifact(outcome.evidence) or b""
    assert GITHUB_TOKEN.encode() not in stored and AWS_KEY.encode() not in stored
    execution = json.loads(stored)["data"]["execution"]
    parsed = ExecutionResult.from_wire(execution)
    assert parsed.executor_class is ExecutorClass.AUTHOR_MACHINE and execution["self_attested"] is True
    assert parsed.verification.statement.startswith("author machine sandbox (self-attested)")
    # The Reader gets the verification environment as structured fields, persisted on the row.
    reread = store.get_evidence_by_source(
        outcome.evidence.review_id, outcome.evidence.source, outcome.evidence.source_ref
    )
    assert reread is not None
    row = verification_row(reread.execution_verification)
    assert row is not None
    assert row["statement"] == "author machine sandbox (self-attested) · image sha256:img · network denied"
    assert (row["executor_class"], row["image_digest"], row["network"]) == ("author_machine", "sha256:img", "denied")
    assert [item["name"] for item in row["not_verified"]] == [
        "payment providers",
        "third-party APIs",
        "production data",
    ]
    assert _evidence(None, [reread], revision_id=reread.revision_id)[0]["execution"] == row
    revision = store.latest_revision(reread.review_id)
    assert revision is not None
    projection = build_verification_projection(packet=None, evidence=[reread], revision=revision, revisions=[revision])
    assert [check["execution"] for check in projection["checks"]] == [row]
    # Detail stays the validation summary; the environment is not smuggled through free text.
    assert "image sha256:img" not in reread.detail
    # The model sees only the trusted title, typed status and executor class.
    label = _evidence_label(replace(outcome.evidence, detail=f"output {GITHUB_TOKEN}"))
    assert GITHUB_TOKEN not in label and "output" not in label and "executor=author_machine" in label


def test_forged_execution_record_is_dropped_with_a_stated_reason(tmp_path: Path) -> None:
    store, context, trusted, surface = _validation_context(tmp_path)
    forged = {**_result(ExecutorClass.MANAGED_SANDBOX).to_wire(), "identity": "0" * 64}

    outcome = execute_local_surface_validation(
        store=store,
        registry=_fixture_registry({"execution": forged}, "ok"),
        context=context,
        trusted_config=trusted,
        surface=surface,
        policy_digest=trusted_policy_digest("trusted-base"),
    )

    data = json.loads(store.read_evidence_artifact(outcome.evidence) or b"{}")["data"]
    assert "execution" not in data
    assert data["execution_error"].startswith("execution record rejected")
    assert outcome.evidence.executor_class == "author_machine"
    with pytest.raises(ReproductionError, match="execution record rejected"):
        load_execution_record({"data": data})


# --------------------------------------------------------------------------- #
# Real command runner: offline run stage with recording sinks (items 3, 30)
# --------------------------------------------------------------------------- #

_RUN_SCRIPT = """
import json, os, smtplib, socket, sys
result = {"home": sorted(os.listdir("/home")), "env": sorted(k for k in os.environ if "SECRET" in k)}
result["ssh"] = os.path.exists(sys.argv[1])
try:
    socket.create_connection(("1.1.1.1", 443), 3).close()
    result["egress"] = "connected"
except OSError as exc:
    result["egress"] = type(exc).__name__
mail = smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"]), timeout=5)
mail.sendmail("app@example.com", ["ops@example.com"], "Subject: x\\r\\n\\r\\nkey " + sys.argv[2])
mail.quit()
print(json.dumps(result))
print("token " + sys.argv[2])
"""


@needs_bwrap
def test_command_runner_runs_offline_with_recording_sinks_and_typed_redacted_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_TEST_CONTROL_PLANE_SECRET", "cp-secret-value")
    (tmp_path / ".lemoncrow").mkdir()
    (tmp_path / ".lemoncrow" / "review.yaml").write_text(
        "version: 1\n"
        "runtimes:\n"
        "  - id: checks\n"
        "    runner: command\n"
        f"    command: [python3, check.py, {str(Path.home() / '.ssh')!r}, {GITHUB_TOKEN}]\n"
        "    timeout: 60\n"
        "surfaces:\n"
        "  - id: checks\n"
        "    provider: service\n"
        "    runtime: checks\n"
        "    services: [tests]\n"
    )
    revision = SimpleNamespace(
        id="revision",
        review_id="review",
        revision_number=1,
        range_mode="commit_range",
        base_sha="base",
        head_sha="head",
        tree_fingerprint="tree",
        source_fingerprint="source",
    )
    context = SurfaceContext(
        tmp_path,
        tmp_path / "store",
        revision,
        ("check.py",),
        load_review_surface_config(tmp_path),  # type: ignore[arg-type]
    )
    surface = ServiceSurfaceProvider().discover(context)[0]

    def materialize(_store: Path, _repo: Path, _revision: object, *, side: str, target: Path, **_kw: object) -> str:
        target.mkdir(parents=True, exist_ok=True)
        (target / "check.py").write_text(_RUN_SCRIPT, encoding="utf-8")
        return side

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.runtimes.materialize_review_side", materialize)

    result = RunnerRegistry((CommandRunner(),)).run(context, surface, side="new")

    assert result.status == "passed", result.output
    assert GITHUB_TOKEN not in result.output
    observed = json.loads(result.output.splitlines()[0])
    # Only the sink's own dummy object-store credential is visible; no host secret is.
    assert observed["home"] == [] and observed["env"] == ["AWS_SECRET_ACCESS_KEY"] and observed["ssh"] is False
    assert observed["egress"] != "connected"
    execution = ExecutionResult.from_wire(result.data["execution"])
    assert execution.executor_class is ExecutorClass.AUTHOR_MACHINE
    assert execution.verification.sinks and execution.recipe.sinks is True
    # The token sits in the trusted command line: the record is redacted, so its
    # recipe no longer hashes to the fingerprint (reproduction states that).
    assert GITHUB_TOKEN not in json.dumps(result.data["execution"])
    assert execution.fingerprint.recipe_digest and execution.fingerprint.recipe_digest != execution.recipe.digest
    assert execution.subject.workspace_digest
    [capture] = [item for item in execution.sink_captures if item.kind == "smtp"]
    assert capture.delivered is False and GITHUB_TOKEN not in capture.payload_preview
    stored = RunArtifactStore.for_store(tmp_path / "store")
    for ref in execution.artifacts:
        assert GITHUB_TOKEN.encode() not in (stored.read("review", ref.digest) or b"")


@needs_bwrap
def test_command_runner_never_shares_host_ipc_or_uts_namespaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".lemoncrow").mkdir()
    (tmp_path / ".lemoncrow" / "review.yaml").write_text(
        "version: 1\n"
        "runtimes:\n  - id: checks\n    runner: command\n    command: [python3, ns.py]\n    timeout: 60\n"
        "surfaces:\n  - id: checks\n    provider: service\n    runtime: checks\n    services: [tests]\n"
    )
    revision = SimpleNamespace(
        id="revision",
        review_id="review",
        revision_number=1,
        range_mode="commit_range",
        base_sha="base",
        head_sha="head",
        tree_fingerprint="tree",
        source_fingerprint="source",
    )
    context = SurfaceContext(
        tmp_path, tmp_path / "store", revision, ("ns.py",), load_review_surface_config(tmp_path)  # type: ignore[arg-type]
    )
    surface = ServiceSurfaceProvider().discover(context)[0]

    def materialize(_store: Path, _repo: Path, _revision: object, *, side: str, target: Path, **_kw: object) -> str:
        target.mkdir(parents=True, exist_ok=True)
        (target / "ns.py").write_text(
            "import json, os\nprint(json.dumps({n: os.readlink(f'/proc/self/ns/{n}') for n in ('ipc', 'uts')}))\n",
            encoding="utf-8",
        )
        return side

    monkeypatch.setattr("lemoncrow.pro.capabilities.review.runtimes.materialize_review_side", materialize)

    result = RunnerRegistry((CommandRunner(),)).run(context, surface, side="new")

    assert result.status == "passed", result.output
    inside = json.loads(result.output.splitlines()[0])
    for name in ("ipc", "uts"):
        assert inside[name] != os.readlink(f"/proc/self/ns/{name}"), name


# --------------------------------------------------------------------------- #
# One-command local reproduction (item 4)
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


_TRUSTED_RUNTIME = "runtimes:\n  - id: checks\n    runner: command\n    command: [python3, check.py]\n"


def _committed_repo(tmp_path: Path, exit_code: int = 0) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "check.py").write_text(
        f"import os\nprint('home', os.listdir('/home'))\nprint('token {GITHUB_TOKEN}')\nraise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    # The trusted base defines the runtime a reproduction may execute.
    (repo / ".lemoncrow").mkdir()
    (repo / ".lemoncrow" / "review.yaml").write_text(_TRUSTED_RUNTIME, encoding="utf-8")
    _git(repo, "add", "check.py", ".lemoncrow/review.yaml")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD").strip()


def _recorded(
    repo: Path, sha: str, tmp_path: Path, *, status: str = "passed", policy: HermeticPolicy | None = None
) -> ExecutionResult:
    tree = tmp_path / "recorded-tree"
    shutil.rmtree(tree, ignore_errors=True)
    materialize_snapshot(repo, ExecutionSubject(snapshot_identity=sha), tree)
    # What a dispatcher records: the effective policy of a trusted base without an execution block.
    recorded_policy = policy or load_hermetic_policy(None)
    return build_execution_result(
        subject=ExecutionSubject(
            review_id="review-one", revision_id="rr-one", snapshot_identity=sha, workspace_digest=workspace_digest(tree)
        ),
        executor_class=ExecutorClass.CUSTOMER_WORKER,
        policy=recorded_policy,
        recipe=ExecutionRecipe(command=("python3", "check.py"), sinks=True, policy=recorded_policy.to_wire()),
        stages=(StageOutcome("run", status, exit_code=0 if status == "passed" else 1),),  # type: ignore[arg-type]
        namespace="head_untrusted",
        content_access="ai_denied",
        tools=("python3",),
        sinks_active=True,
    )


@needs_bwrap
def test_reproduce_replays_a_recorded_commit_and_compares_outcome_and_environment(tmp_path: Path) -> None:
    repo, sha = _committed_repo(tmp_path)
    # The working tree moves on; reproduction still uses the recorded commit.
    (repo / "check.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
    original = _recorded(repo, sha, tmp_path)

    outcome = reproduce_execution(
        load_execution_record({"data": {"execution": original.to_wire()}}), repo=repo, store_root=tmp_path / "store"
    )

    assert outcome.reproduced, outcome.to_wire()
    assert outcome.materialized == f"commit {sha}" and outcome.workspace_match is True
    assert outcome.compared == (("run", "passed", "passed"),)
    assert outcome.environment_differences == ()
    assert outcome.replay.executor_class is ExecutorClass.AUTHOR_MACHINE
    assert outcome.replay.cache_namespace == "head_untrusted"
    run = outcome.replay.stage("run")
    assert run is not None and GITHUB_TOKEN not in run.output and "home []" in run.output

    failed = reproduce_execution(_recorded(repo, sha, tmp_path, status="failed"), repo=repo, store_root=tmp_path)
    assert not failed.reproduced and failed.compared == (("run", "failed", "passed"),)


@needs_bwrap
def test_reproduce_reports_a_different_workspace_and_refuses_scope_escape(tmp_path: Path) -> None:
    repo, sha = _committed_repo(tmp_path)
    original = _recorded(repo, sha, tmp_path)
    working = replace(original, subject=replace(original.subject, snapshot_identity="working-tree"))
    (repo / "check.py").write_text("print('edited')\n", encoding="utf-8")

    outcome = reproduce_execution(working, repo=repo, store_root=tmp_path / "store")
    assert outcome.materialized == "working tree" and outcome.workspace_match is False
    assert not outcome.reproduced

    escaping = replace(original, subject=replace(original.subject, scope="../.."))
    with pytest.raises(ReproductionError, match="escapes"):
        reproduce_execution(escaping, repo=repo, store_root=tmp_path / "store")


@needs_bwrap
def test_lc_review_reproduce_command_exits_by_reproduction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.gateway.cli.commands.review import review_cmd

    repo, sha = _committed_repo(tmp_path)
    record = tmp_path / "record.json"
    record.write_text(json.dumps({"data": {"execution": _recorded(repo, sha, tmp_path).to_wire()}}), encoding="utf-8")
    monkeypatch.chdir(repo)
    runner = CliRunner()

    ok = runner.invoke(
        review_cmd, ["--reproduce", str(record), "--repo-root", str(repo), "--json"], obj={"root": str(tmp_path / "s")}
    )
    assert ok.exit_code == 0, ok.output
    payload = json.loads(ok.output)
    assert payload["reproduced"] is True and payload["identity"] == _recorded(repo, sha, tmp_path).identity

    record.write_text(
        json.dumps(_recorded(repo, sha, tmp_path, status="failed").to_wire()),
        encoding="utf-8",
    )
    mismatch = runner.invoke(review_cmd, ["--reproduce", str(record), "--repo-root", str(repo)], obj={})
    assert mismatch.exit_code == 1, mismatch.output
    assert "NOT reproduced" in mismatch.output and "run: recorded failed -> replayed passed" in mismatch.output

    record.write_text("{}", encoding="utf-8")
    invalid = runner.invoke(review_cmd, ["--reproduce", str(record), "--repo-root", str(repo)], obj={})
    assert invalid.exit_code != 0 and "no typed execution result" in invalid.output


@needs_bwrap
def test_reproduce_never_executes_a_command_or_environment_only_the_record_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.gateway.cli.commands.review import review_cmd

    repo, sha = _committed_repo(tmp_path)
    marker = tmp_path / "workspace-marker"
    original = _recorded(repo, sha, tmp_path)
    record = tmp_path / "record.json"
    monkeypatch.chdir(repo)
    runner = CliRunner()
    args = ["--reproduce", str(record), "--repo-root", str(repo)]

    forged = replace(original, recipe=replace(original.recipe, command=("sh", "-c", f"touch {marker}")))
    record.write_text(json.dumps(forged.to_wire()), encoding="utf-8")
    for extra in ([], ["--use-recorded-policy"]):
        refused = runner.invoke(review_cmd, [*args, *extra], obj={"root": str(tmp_path / "s")})
        assert refused.exit_code != 0
        assert "not a runtime command at the trusted base" in refused.output
    assert not marker.exists()

    injected = replace(original, recipe=replace(original.recipe, env=(("LD_PRELOAD", "/workspace/x.so"),)))
    record.write_text(json.dumps(injected.to_wire()), encoding="utf-8")
    refused = runner.invoke(review_cmd, args, obj={"root": str(tmp_path / "s")})
    assert refused.exit_code != 0 and "LD_PRELOAD" in refused.output


def test_reproduce_materializes_committed_symlinks_as_plain_files(tmp_path: Path) -> None:
    repo, _sha = _committed_repo(tmp_path)
    secret = tmp_path / "outside-secret"
    secret.write_text("host-only\n", encoding="utf-8")
    (repo / "package-lock.json").symlink_to(secret)
    _git(repo, "add", "package-lock.json")
    _git(repo, "commit", "-q", "-m", "link")
    sha = _git(repo, "rev-parse", "HEAD").strip()

    tree = tmp_path / "tree"
    materialize_snapshot(repo, ExecutionSubject(snapshot_identity=sha), tree)

    lock = tree / "package-lock.json"
    assert not lock.is_symlink() and lock.is_file()
    assert lock.read_text(encoding="utf-8") == str(secret)


@needs_bwrap
def test_reproduce_takes_its_policy_from_the_trusted_base_and_fails_on_a_mismatch_unless_told_otherwise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.gateway.cli.commands.review import review_cmd

    repo, first = _committed_repo(tmp_path)
    record = tmp_path / "record.json"
    record.write_text(json.dumps(_recorded(repo, first, tmp_path).to_wire()), encoding="utf-8")
    # The trusted base moves on: its execution policy now differs from the recorded one.
    (repo / ".lemoncrow").mkdir(exist_ok=True)
    (repo / ".lemoncrow" / "review.yaml").write_text(
        _TRUSTED_RUNTIME + "execution:\n  retention_days: 3\n", encoding="utf-8"
    )
    _git(repo, "add", ".lemoncrow/review.yaml")
    _git(repo, "commit", "-q", "-m", "policy")
    monkeypatch.chdir(repo)
    runner = CliRunner()
    args = ["--reproduce", str(record), "--repo-root", str(repo), "--json"]

    refused = runner.invoke(review_cmd, args, obj={"root": str(tmp_path / "s")})
    assert refused.exit_code == 1
    assert "policy mismatch" in refused.stderr and "--use-recorded-policy" in refused.stderr
    assert refused.stdout == ""  # nothing ran

    recorded = runner.invoke(review_cmd, [*args, "--use-recorded-policy"], obj={"root": str(tmp_path / "s")})
    assert recorded.exit_code == 0, recorded.output
    assert "warning:" in recorded.stderr and "RECORDED policy" in recorded.stderr
    payload = json.loads(recorded.stdout)
    assert payload["reproduced"] is True
    assert payload["policy"] == {"source": "recorded", "matches_record": True}

    # The trusted base the record ran under: its committed policy matches, and it governs the replay.
    matched = runner.invoke(review_cmd, [*args, "--trusted-base", first], obj={"root": str(tmp_path / "s")})
    assert matched.exit_code == 0, matched.output
    payload = json.loads(matched.stdout)
    assert payload["policy"] == {"source": f"trusted_base:{first}", "matches_record": True}
    assert payload["scope"] == "full" and payload["compared_outputs"] == ["source tree digest", "run stage outcome"]

    unknown = runner.invoke(review_cmd, [*args, "--trusted-base", "no-such-ref"], obj={})
    assert unknown.exit_code != 0 and "does not resolve" in unknown.output
    stray = runner.invoke(review_cmd, ["--use-recorded-policy", "--repo-root", str(repo)], obj={})
    assert stray.exit_code != 0 and "go with --reproduce" in stray.output


def _preview_record(
    repo: Path, sha: str, tmp_path: Path, *, stages: tuple[StageOutcome, ...], policy: HermeticPolicy, scope: str
) -> ExecutionResult:
    tree = tmp_path / "recorded-preview"
    shutil.rmtree(tree, ignore_errors=True)
    materialize_snapshot(repo, ExecutionSubject(snapshot_identity=sha), tree)
    used_fetch = any(stage.stage == "fetch" and stage.status != "skipped" for stage in stages)
    return build_execution_result(
        subject=ExecutionSubject(
            review_id="review-one",
            revision_id="rr-one",
            snapshot_identity=sha,
            scope=scope,
            workspace_digest=workspace_digest(tree),
        ),
        executor_class=ExecutorClass.AUTHOR_MACHINE,
        policy=policy,
        recipe=ExecutionRecipe(
            command=("preview", "storybook"),
            cwd=scope,
            stages=("fetch", "build", "run") if used_fetch else ("build", "run"),
            policy=policy.to_wire(),
        ),
        stages=stages,
        namespace="head_untrusted",
        content_access="ai_denied",
        tools=("node", "python3"),
    )


_RENDER_STAGES = (
    StageOutcome("build", "passed", detail="storybook exited 0", exit_code=0),
    StageOutcome("run", "passed", detail="chromium exited 0", exit_code=0),
)


def test_a_preview_with_borrowed_dependencies_reproduces_its_source_and_labels_the_render(tmp_path: Path) -> None:
    repo, sha = _committed_repo(tmp_path)
    policy = load_hermetic_policy(None)
    skipped = StageOutcome("fetch", "skipped", detail="reviewed dependency graph equals the trusted checkout")
    original = _preview_record(repo, sha, tmp_path, stages=(skipped, *_RENDER_STAGES), policy=policy, scope=".")

    outcome = reproduce_execution(original, repo=repo, store_root=tmp_path / "store", policy=policy)

    assert outcome.reproduced and outcome.scope == "deterministic"
    assert outcome.compared == () and outcome.compared_outputs == ("source tree digest",)
    assert [(stage, recorded) for stage, recorded, _reason in outcome.not_compared] == [
        ("build", "passed"),
        ("run", "passed"),
    ]
    assert all("not deterministic" in reason for _stage, _recorded, reason in outcome.not_compared)
    assert outcome.replay.stages == ()  # nothing executed: there was nothing deterministic to replay

    (repo / "check.py").write_text("print('moved on')\n", encoding="utf-8")
    working = replace(original, subject=replace(original.subject, snapshot_identity="working-tree"))
    assert not reproduce_execution(working, repo=repo, store_root=tmp_path / "store", policy=policy).reproduced


@needs_bwrap
def test_a_dependency_changing_preview_reproduces_its_fetch_and_build_and_labels_the_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.gateway.cli.commands.review import review_cmd
    from tests.gateway.test_review_hermetic import _project, _Server, _tarball

    registry = _Server()
    try:
        repo = tmp_path / "repo"
        _project(repo / "web", registry, [("left-pad", "1.0.0", _tarball("left-pad", "1.0.0", files={"i.js": "1"}))])
        config = yaml.safe_dump(
            {
                "execution": {
                    "registries": [
                        {"host": "localhost", "scheme": "http", "port": registry.port, "private_network": True}
                    ]
                }
            }
        )
        (repo / ".lemoncrow").mkdir()
        (repo / ".lemoncrow" / "review.yaml").write_text(config, encoding="utf-8")
        _git(repo, "init", "-q")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "preview fixture")
        sha = _git(repo, "rev-parse", "HEAD").strip()
        policy = load_hermetic_policy(config)
        recorded_tree = provision_npm_dependencies(
            repo / "web", policy=policy, cache=DependencyCache.for_store(tmp_path / "recording", "head_untrusted")
        )
        original = _preview_record(
            repo, sha, tmp_path, stages=(*recorded_tree.stages, *_RENDER_STAGES), policy=policy, scope="web"
        )
        record = tmp_path / "preview.json"
        record.write_text(json.dumps(original.to_wire()), encoding="utf-8")
        monkeypatch.chdir(repo)

        result = CliRunner().invoke(
            review_cmd, ["--reproduce", str(record), "--repo-root", str(repo)], obj={"root": str(tmp_path / "s")}
        )
    finally:
        registry.close()

    assert result.exit_code == 0, result.output
    assert "reproduced  " in result.stdout and "NOT reproduced" not in result.stdout
    assert "compared: source tree digest, fetch stage outcome, build stage outcome" in result.stdout
    assert "fetch: recorded passed -> replayed passed" in result.stdout
    assert "build: recorded passed -> replayed passed" in result.stdout
    assert "build: recorded passed, not compared (preview renderer stage" in result.stdout
    assert "run: recorded passed, not compared (preview renderer stage" in result.stdout
    assert "policy: trusted_base:HEAD" in result.stdout


def test_reproduce_refuses_without_bubblewrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review.hermetic import HermeticUnavailable

    repo, sha = _committed_repo(tmp_path)
    original = _recorded(repo, sha, tmp_path)
    monkeypatch.setattr("lemoncrow.pro.capabilities.review.hermetic.shutil.which", lambda _name: None)
    with pytest.raises(HermeticUnavailable, match="refusing to run unsandboxed"):
        reproduce_execution(original, repo=repo, store_root=tmp_path / "store")
