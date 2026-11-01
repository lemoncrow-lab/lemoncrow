"""The deterministic readiness core (Phase 10 items 7, 12-17; Phase 14 items 20, 28, 29).

Readiness is recomputed from durable state at one instant against one policy
generation. These tests pin what may never happen: a model call while
deciding readiness, an expired external observation counted as PASS, a policy
change that silently keeps (or creates) an armed landing, and human evidence
standing in for a provider/CI/LemonCrow result by copying its title.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.freshness import (
    DEFAULT_EXTERNAL_MAX_AGE_SECONDS,
    FreshnessPolicy,
    assess_freshness,
    format_timestamp,
    parse_freshness_policy,
)
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.landing import (
    landing_metadata,
    landing_relationship,
    reconcile_local_github_landing,
    record_landing_state,
)
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.readiness import (
    DEFAULT_CONDITIONS,
    ConditionRegistry,
    EvidenceRef,
    ReadinessCondition,
    ReadinessInputs,
    RegisteredCondition,
    built_in_policy_layer,
    policy_generation,
    policy_layer,
)
from lemoncrow.pro.capabilities.review.runtime_validation import trusted_policy_digest
from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence, ReviewSession
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore
from tests.gateway.test_review_api import (
    _BASE_SOURCE,
    _CHANGED_SOURCE,
    BASE,
    PORT,
    TOKEN,
    Workspace,
    _advance_snapshot_without_rerunning_checks,
    _commit,
    _init_repo,
    _no_astgrep_download,
    _review_all_current_targets,
    _write,
    draft_workspace,
    workspace,
)
from tests.gateway.test_review_landing import _armed_pull_request, _FakeGitHub, _use

__all__ = ["_no_astgrep_download", "draft_workspace", "workspace"]

_DIGEST = "sha256:" + "a" * 64
_SPDX = json.dumps(
    {"spdxVersion": "SPDX-2.3", "packages": [{"name": "cryptography", "versionInfo": "46.0.0"}]}
).encode()
_SCAN = json.dumps([{"package": "cryptography", "advisories": []}]).encode()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


@dataclass
class _Clock:
    now: datetime

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Clock]:
    """Controls ``store.utc_now``: every stored timestamp and the readiness instant."""

    from lemoncrow.pro.capabilities.review import store as store_module

    state = _Clock(datetime.now(UTC).replace(microsecond=0))
    real = store_module.datetime

    class _Frozen(real):  # type: ignore[misc,valid-type]
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return state.now.astimezone(tz) if tz is not None else state.now.replace(tzinfo=None)

    monkeypatch.setattr(store_module, "datetime", _Frozen)
    yield state


def _client(
    workspace: Workspace,
    *,
    delegate: Any = None,
    extension: Any = None,
    conditions: Iterable[RegisteredCondition] = (),
    hook: Any = None,
) -> Workspace:
    store = ReviewStore(workspace.store_root)
    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=workspace.repo_root,
        port=PORT,
        landing_delegate=delegate,
        readiness_extension=extension,
        readiness_conditions=tuple(conditions),
        policy_change_hook=hook,
    )
    return Workspace(
        TestClient(app, base_url=BASE), store, workspace.store_root, workspace.repo_root, workspace.review_id
    )


def _recording_delegate(calls: list[tuple[str, str]]) -> Any:
    def delegate(session: ReviewSession, revision: Any, *, action: str) -> dict[str, Any]:
        calls.append((revision.id, action))
        return {
            "supported": True,
            "state": "delegated" if action == "arm" else "cancelled",
            "provider": "github",
            "detail": "GitHub auto-merge delegated" if action == "arm" else "GitHub auto-merge disabled",
            "url": "https://github.com/acme/widgets/pull/7",
            "mechanism": "auto_merge_or_merge_queue",
        }

    return delegate


def _link_pull_request(workspace: Workspace) -> None:
    linked = workspace.post(
        f"/api/reviews/{workspace.review_id}/relationships",
        json={
            "kind": "scm",
            "relation": "pull_request",
            "provider": "github",
            "external_id": "acme/widgets#7",
            "url": "https://github.com/acme/widgets/pull/7",
        },
    )
    assert linked.status_code == 200, linked.text


def _readiness(workspace: Workspace) -> dict[str, Any]:
    response = workspace.get(f"/api/reviews/{workspace.review_id}/readiness")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _condition(body: dict[str, Any], condition_id: str) -> dict[str, Any]:
    return next(row for row in body["conditions"] if row["id"] == condition_id)


def _arm(workspace: Workspace, inspected: dict[str, Any]) -> Any:
    return workspace.post(
        f"/api/reviews/{workspace.review_id}/landing/arm",
        json={
            "armed": True,
            "expected_revision_id": inspected["snapshot_id"],
            "expected_tree_fingerprint": inspected["tree_fingerprint"],
            "expected_conditions_hash": inspected["conditions_hash"],
        },
    )


def _upload_supply_chain(
    workspace: Workspace,
    kind: str,
    status: str,
    *,
    observation_source: str,
    payload: bytes,
    **extra: str,
) -> dict[str, Any]:
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    response = workspace.post(
        f"/api/reviews/{workspace.review_id}/evidence/upload",
        params={
            "kind": kind,
            "revision_id": revision.id,
            "title": f"{observation_source} {kind}",
            "filename": f"{kind}.json",
            "mime_type": "application/json",
            "source": "ci",
            "verification_status": status,
            "artifact_digest": _DIGEST,
            "observation_source": observation_source,
            **extra,
        },
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 201, response.text
    evidence: dict[str, Any] = response.json()["evidence"]
    return evidence


def _feed_supply_chain(
    workspace: Workspace,
    kind: str,
    status: str,
    *,
    stable_key: str = "osv-scan",
    observed_at: datetime | None = None,
    source_revision: str = "",
    valid_until: datetime | None = None,
) -> ReviewEvidence:
    """A typed ``trusted_security_feed`` supply-chain verdict (the only kind that can satisfy the policy)."""

    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    metadata: dict[str, Any] = {"artifact_digest": _DIGEST, "observation_source": "osv"}
    if observed_at is not None:
        metadata["source_timestamp"] = format_timestamp(observed_at)
    if source_revision:
        metadata["source_revision"] = source_revision
    if valid_until is not None:
        metadata["valid_until"] = format_timestamp(valid_until)
    return workspace.store.add_evidence(
        ReviewEvidence(
            id="",
            review_id=workspace.review_id,
            revision_id=revision.id,
            kind=kind,  # type: ignore[arg-type]
            title=f"osv {kind}",
            source="external",
            source_ref="osv",
            verification_status=status,
            detail=json.dumps(metadata, sort_keys=True),
            attestation_class="trusted_security_feed",
            stable_key=stable_key,
            provider="osv",
            integration_id="osv-feed",
            observed_at=format_timestamp(observed_at) if observed_at is not None else "",
        )
    )


def _typed_check(
    workspace: Workspace,
    revision_id: str,
    *,
    status: str,
    title: str = "CI / unit tests",
    stable_key: str = "unit-tests",
    attestation_class: str = "provider_ci",
) -> ReviewEvidence:
    return workspace.store.add_evidence(
        ReviewEvidence(
            id="",
            review_id=workspace.review_id,
            revision_id=revision_id,
            kind="verification",
            title=title,
            source="ci",
            source_ref="github-actions",
            verification_status=status,
            attestation_class=attestation_class,
            stable_key=stable_key,
            provider="github",
            integration_id="actions",
        )
    )


def _evidence(kind: str, *, created_at: str, detail: str = "", attestation_class: str = "") -> ReviewEvidence:
    return ReviewEvidence(
        id=f"ev-{kind}",
        review_id="review-1",
        revision_id="rr-1",
        kind=kind,  # type: ignore[arg-type]
        title=kind,
        source="ci",
        verification_status="PASS",
        detail=detail,
        created_at=created_at,
        attestation_class=attestation_class,
        stable_key="feed" if attestation_class else "",
    )


# --------------------------------------------------------------------------- #
# core contract (pure)
# --------------------------------------------------------------------------- #


def _inputs(*, facts: dict[str, object] | None = None, evidence: dict[str, Any] | None = None) -> ReadinessInputs:
    return ReadinessInputs(
        review_id="review-1",
        snapshot_id="rr-1",
        snapshot_number=1,
        evaluated_at="2026-10-01T12:00:00Z",
        policy=policy_generation([built_in_policy_layer()]),
        evidence=evidence or {},
        facts=facts or {},
    )


def _fixed(*conditions: ReadinessCondition) -> Any:
    return lambda inputs: conditions


def test_registry_rejects_an_empty_or_duplicate_registration() -> None:
    entry = RegisteredCondition("downstream", 76, _fixed())
    with pytest.raises(ValueError):
        ConditionRegistry((entry, entry))
    with pytest.raises(ValueError):
        ConditionRegistry((RegisteredCondition("", 1, _fixed()),))
    with pytest.raises(ValueError):
        DEFAULT_CONDITIONS.with_conditions(RegisteredCondition("verification", 75, _fixed()))
    with pytest.raises(ValueError):
        DEFAULT_CONDITIONS.with_conditions(RegisteredCondition("premerge", 75, _fixed()))
    extended = DEFAULT_CONDITIONS.with_conditions(entry)
    keys = [item.key for item in extended.entries]
    assert keys.index("supply_chain") < keys.index("premerge") < keys.index("downstream") < keys.index("approval")
    assert "downstream" not in [item.key for item in DEFAULT_CONDITIONS.entries]


def test_one_failed_condition_blocks_and_a_broken_rule_never_passes() -> None:
    passing = ReadinessCondition(id="a", label="A", state="pass", detail="ok")
    registry = ConditionRegistry(
        (
            RegisteredCondition("a", 1, _fixed(passing)),
            RegisteredCondition(
                "b", 2, _fixed(ReadinessCondition(id="b", label="B", state="fail", detail="error-mode"))
            ),
        )
    )
    result = registry.evaluate(_inputs())
    assert [item.id for item in result.blockers] == ["b"]
    assert result.ready is False and result.armable is False

    def explode(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
        raise RuntimeError("rule crashed")

    def broken_gather(sources: Any) -> object:
        raise OSError("store unreadable")

    broken = ConditionRegistry(
        (
            RegisteredCondition("a", 1, _fixed(passing)),
            RegisteredCondition("crash", 2, explode),
            RegisteredCondition("io", 3, _fixed(passing), gather=broken_gather),
        )
    )
    facts = broken.gather(object(), {})  # type: ignore[arg-type]
    result = broken.evaluate(_inputs(facts=facts))
    states = {item.id: item.state for item in result.conditions}
    assert states == {"a": "pass", "readiness-error:crash": "unknown", "readiness-error:io": "unknown"}
    assert "rule crashed" in _condition(result.to_wire(), "readiness-error:crash")["detail"]
    assert result.ready is False
    # Vocabulary a rule does not know is a blocker, never a pass.
    assert ReadinessCondition(id="x", label="X", state="green", detail="").state == "pending"


def test_a_pass_that_cites_expired_or_missing_evidence_becomes_unknown() -> None:
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    scan = _evidence("vulnerability_scan", created_at=format_timestamp(now - timedelta(hours=30)))
    sbom = _evidence("sbom", created_at=format_timestamp(now - timedelta(days=400)))
    evidence = {item.id: assess_freshness(item, now=now) for item in (scan, sbom)}

    def cites(*ids: str) -> ReadinessCondition:
        return ReadinessCondition(
            id="-".join(ids),
            label="Supply chain",
            state="pass",
            detail="passed.",
            evidence_refs=tuple(EvidenceRef(evidence_id=value) for value in ids),
        )

    registry = ConditionRegistry(
        (
            RegisteredCondition("intrinsic", 1, _fixed(cites(sbom.id))),
            RegisteredCondition("external", 2, _fixed(cites(scan.id))),
            RegisteredCondition("missing", 3, _fixed(cites("ev-gone"))),
        )
    )
    result = registry.evaluate(_inputs(evidence=evidence))
    by_id = {item.id: item for item in result.conditions}
    assert by_id[sbom.id].state == "pass"
    assert by_id[sbom.id].evidence_refs[0].freshness_class == "snapshot_intrinsic"
    assert by_id[scan.id].state == "unknown"
    assert by_id[scan.id].reason == "evidence_expired"
    assert "never counted as passing" in by_id[scan.id].detail
    assert by_id[scan.id].evidence_refs[0].state == "expired"
    assert by_id["ev-gone"].state == "unknown"
    assert result.ready is False
    assert result.valid_until == ""  # no satisfied condition relies on time-limited evidence

    fresh = {scan.id: assess_freshness(scan, now=now - timedelta(hours=10))}
    current = ConditionRegistry((RegisteredCondition("external", 2, _fixed(cites(scan.id))),)).evaluate(
        _inputs(evidence=fresh)
    )
    assert current.ready is True
    assert current.valid_until == format_timestamp(now - timedelta(hours=6))


def test_freshness_classes_expiry_and_post_dating() -> None:
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    created = format_timestamp(now - timedelta(hours=2))
    assert assess_freshness(_evidence("sbom", created_at=created), now=now).state == "current"
    assert assess_freshness(_evidence("provenance", created_at=created), now=now).state == "current"
    assert assess_freshness(_evidence("verification", created_at=created), now=now).freshness_class == (
        "execution_bound"
    )
    feed = assess_freshness(
        _evidence("verification", created_at=created, attestation_class="trusted_security_feed"), now=now
    )
    assert (feed.freshness_class, feed.state) == ("external_observation", "current")
    one_hour = FreshnessPolicy(external_max_age_seconds=3600)
    assert assess_freshness(_evidence("vulnerability_scan", created_at=created), now=now, policy=one_hour).state == (
        "expired"
    )
    # An explicit valid_until earlier than max-age wins.
    bounded = _evidence(
        "vulnerability_scan",
        created_at=created,
        detail=json.dumps({"valid_until": format_timestamp(now - timedelta(minutes=1)), "source_revision": "db@7"}),
    )
    expired = assess_freshness(bounded, now=now)
    assert (expired.state, expired.source_revision) == ("expired", "db@7")
    # A writer cannot extend validity by post-dating the observation.
    post_dated = _evidence(
        "vulnerability_scan",
        created_at=format_timestamp(now - timedelta(hours=25)),
        detail=json.dumps({"source_timestamp": format_timestamp(now + timedelta(days=30))}),
    )
    assert assess_freshness(post_dated, now=now).state == "expired"
    # No readable source time: unknown, never current.
    assert assess_freshness(_evidence("vulnerability_scan", created_at=""), now=now).state == "unknown"
    unreadable = _evidence("vulnerability_scan", created_at=created, detail=json.dumps({"valid_until": "soon"}))
    assert assess_freshness(unreadable, now=now).state == "unknown"


def test_freshness_policy_only_tightens_and_an_invalid_section_fails_closed() -> None:
    assert parse_freshness_policy("version: 1\n", source="base").external_max_age_seconds == (
        DEFAULT_EXTERNAL_MAX_AGE_SECONDS
    )
    tight = parse_freshness_policy("evidence_freshness:\n  external_observation_max_age: 1h\n", source="base")
    assert (tight.external_max_age_seconds, tight.source, tight.error) == (3600, "base", "")
    # A longer max-age than the built-in default never loosens it.
    loose = parse_freshness_policy("evidence_freshness:\n  external_observation_max_age: 30d\n", source="base")
    assert loose.external_max_age_seconds == DEFAULT_EXTERNAL_MAX_AGE_SECONDS
    for text in (
        "evidence_freshness:\n  external_observation_max_age: forever\n",
        "evidence_freshness:\n  external_observation_max_age: 400d\n",
        "evidence_freshness:\n  sbom_max_age: 1h\n",
        "evidence_freshness: 12h\n",
    ):
        broken = parse_freshness_policy(text, source="base")
        assert broken.external_max_age_seconds == 0 and broken.error, text
    assert tight.stricter(FreshnessPolicy(external_max_age_seconds=60, source="org")).source == "org"
    assert tight.stricter(FreshnessPolicy(external_max_age_seconds=7200, source="org")).source == "base"


def test_policy_generation_is_a_content_address_over_its_layers() -> None:
    base = policy_layer("trusted_base", "base", {"version": 1})
    org = policy_layer("operator_review_policy", "org", {"required": ["CI"]})
    first = policy_generation([built_in_policy_layer(), base, org])
    assert first.digest.startswith("policy:") and len(first.short) == 12
    assert first == policy_generation([built_in_policy_layer(), base, org])
    tightened = policy_generation(
        [built_in_policy_layer(), base, policy_layer("operator_review_policy", "org", {"required": ["CI", "SAST"]})]
    )
    assert tightened.digest != first.digest
    provisional = policy_generation([built_in_policy_layer(), policy_layer("trusted_base", "b", {}, provisional=True)])
    assert provisional.provisional and provisional.to_wire()["authority"] == "provisional"

    passing = (ReadinessCondition(id="a", label="A", state="pass", detail="ok"),)
    registry = ConditionRegistry((RegisteredCondition("a", 1, _fixed(*passing)),))
    same_conditions = [
        registry.evaluate(
            ReadinessInputs(
                review_id="r", snapshot_id="s", snapshot_number=1, evaluated_at="", policy=generation, facts={}
            )
        )
        for generation in (first, tightened)
    ]
    # The same visible conditions under another policy are another decision.
    assert same_conditions[0].conditions_hash != same_conditions[1].conditions_hash


def test_a_provisional_policy_authority_blocks_readiness() -> None:
    provisional = policy_generation([built_in_policy_layer(), policy_layer("trusted_base", "b", {}, provisional=True)])
    result = DEFAULT_CONDITIONS.evaluate(
        ReadinessInputs(
            review_id="r", snapshot_id="s", snapshot_number=1, evaluated_at="", policy=provisional, facts={}
        )
    )
    authority = next(item for item in result.conditions if item.id == "policy-authority")
    assert authority.state == "unknown"
    assert result.armable is False

    # A final operator policy governs a composition whose provider event carries
    # no base tree; the provisional repository layer stays named, not blocking.
    governed = policy_generation([*provisional.layers, policy_layer("operator_review_policy", "org", {})])
    result = DEFAULT_CONDITIONS.evaluate(
        ReadinessInputs(review_id="r", snapshot_id="s", snapshot_number=1, evaluated_at="", policy=governed, facts={})
    )
    assert governed.provisional
    assert not any(item.id == "policy-authority" for item in result.conditions)


# --------------------------------------------------------------------------- #
# API: recompute without a model, policy generation named
# --------------------------------------------------------------------------- #


def test_readiness_recomputes_identically_without_any_model_call(
    clock: _Clock, workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review import ai_review, intelligence_runtime

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("readiness must never call a model")

    for module in (intelligence_runtime, ai_review):
        for name in ("execute_structured", "run_intelligence", "resolve_driver_model"):
            monkeypatch.setattr(module, name, forbidden, raising=False)

    _review_all_current_targets(workspace)
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    _typed_check(workspace, revision.id, status="PASS")
    _upload_supply_chain(workspace, "vulnerability_scan", "PASS", observation_source="osv", payload=_SCAN)

    first = _readiness(workspace)
    second = _readiness(workspace)
    fresh_instance = _readiness(_client(workspace))
    assert first == second == fresh_instance
    assert first["ready"] is True
    assert first["evaluated_at"] == clock.now.isoformat()
    generation = first["policy_generation"]
    assert generation["digest"].startswith("policy:")
    assert generation["short"] == generation["digest"].removeprefix("policy:")[:12]
    assert generation["authority"] == "final"
    assert [layer["layer"] for layer in generation["layers"]] == ["built_in", "trusted_base"]
    # The base has no review config: the layer still names that exact state.
    assert generation["layers"][1]["digest"] == trusted_policy_digest("", base_known=True)
    for row in first["conditions"]:
        assert {"id", "label", "state", "detail", "owner", "category", "reason", "evidence_refs"} <= set(row)


def test_a_registered_condition_reaches_the_api_and_a_broken_one_blocks(workspace: Workspace) -> None:
    seen: list[str] = []

    def gather(sources: Any) -> object:
        seen.append(sources.revision.id)
        return len(sources.store.list_annotations(sources.session.id))

    def evaluate(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
        count = inputs.fact("downstream")
        return (
            ReadinessCondition(
                id="downstream",
                label="Downstream checks",
                state="pass" if count == 0 else "fail",
                detail=f"{count} annotation(s).",
                category="downstream",
            ),
        )

    def broken(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
        raise KeyError("downstream plan missing")

    _review_all_current_targets(workspace)
    registered = _client(workspace, conditions=(RegisteredCondition("downstream", 76, evaluate, gather=gather),))
    body = _readiness(registered)
    ids = [row["id"] for row in body["conditions"]]
    assert ids.index("supply-chain-policy") < ids.index("downstream") < ids.index("human-approval-state")
    assert _condition(body, "downstream")["state"] == "pass"
    assert seen == [body["snapshot_id"]]
    assert body["armable"] is True

    failing = _readiness(_client(workspace, conditions=(RegisteredCondition("downstream", 76, broken),)))
    assert _condition(failing, "readiness-error:downstream")["state"] == "unknown"
    assert failing["armable"] is False


# --------------------------------------------------------------------------- #
# Phase 14 item 20: evidence age on an unchanged Snapshot
# --------------------------------------------------------------------------- #


def test_an_external_observation_expires_on_an_unchanged_snapshot_while_the_sbom_stays_current(
    clock: _Clock, workspace: Workspace
) -> None:
    _review_all_current_targets(workspace)
    observed = clock.now
    # A CI-uploaded SBOM is snapshot-intrinsic evidence; with a self-declared
    # source it supports review but is never a policy verdict.
    sbom = _upload_supply_chain(workspace, "sbom", "PASS", observation_source="BuildKit", payload=_SPDX)
    clock.advance(seconds=1)
    scan = {
        "id": _feed_supply_chain(
            workspace, "vulnerability_scan", "PASS", observed_at=observed, source_revision="advisory-db@100"
        ).id
    }

    fresh = _readiness(workspace)
    supply = _condition(fresh, "supply-chain-policy")
    assert supply["state"] == "pass"
    refs = {ref["evidence_id"]: ref for ref in supply["evidence_refs"]}
    assert sbom["id"] not in refs
    assert refs[scan["id"]]["freshness_class"] == "external_observation"
    assert refs[scan["id"]]["source_revision"] == "advisory-db@100"
    expires = format_timestamp(observed + timedelta(seconds=DEFAULT_EXTERNAL_MAX_AGE_SECONDS))
    assert refs[scan["id"]]["expires_at"] == expires
    assert fresh["valid_until"] == expires
    assert fresh["armable"] is True

    # Nothing about the Change moves; only the world the scan observed ages.
    clock.advance(hours=25)
    aged = _readiness(workspace)
    assert aged["snapshot_id"] == fresh["snapshot_id"]
    supply = _condition(aged, "supply-chain-policy")
    assert supply["state"] == "unknown"
    assert supply["reason"] == "evidence_expired"
    refs = {ref["evidence_id"]: ref for ref in supply["evidence_refs"]}
    assert refs[scan["id"]]["state"] == "expired"
    assert aged["armable"] is False and aged["ready"] is False
    verification = workspace.get(f"/api/reviews/{workspace.review_id}/verification").json()
    artifact = next(row for row in verification["artifacts"] if row["id"] == scan["id"])
    assert artifact["freshness_class"] == "external_observation"
    assert artifact["source_revision"] == "advisory-db@100"
    intrinsic = next(row for row in verification["artifacts"] if row["id"] == sbom["id"])
    assert intrinsic["freshness_class"] == "snapshot_intrinsic"
    assert intrinsic["freshness"]["state"] == "current"  # the SBOM itself never ages

    # A feed re-scan of the same source on the same Snapshot supersedes the expired one.
    clock.advance(seconds=1)
    _feed_supply_chain(workspace, "vulnerability_scan", "PASS", source_revision="db@101")
    assert _condition(_readiness(workspace), "supply-chain-policy")["state"] == "pass"

    # The advisory database changed: the newest observation now fails.
    clock.advance(seconds=1)
    _feed_supply_chain(workspace, "vulnerability_scan", "FAIL", source_revision="db@102")
    changed = _readiness(workspace)
    assert _condition(changed, "supply-chain-policy")["state"] == "fail"
    assert changed["snapshot_id"] == fresh["snapshot_id"]


def test_reading_readiness_withdraws_an_armed_landing_whose_evidence_expired(
    clock: _Clock, workspace: Workspace
) -> None:
    calls: list[tuple[str, str]] = []
    landing = _client(workspace, delegate=_recording_delegate(calls))
    _link_pull_request(landing)
    _review_all_current_targets(landing)
    _feed_supply_chain(landing, "vulnerability_scan", "PASS")
    inspected = _readiness(landing)
    armed = _arm(landing, inspected)
    assert armed.status_code == 200, armed.text
    record = landing_relationship(landing.store, workspace.review_id)
    assert record is not None
    assert landing_metadata(record)["readiness_valid_until"] == inspected["valid_until"] != ""

    clock.advance(hours=25)
    after = _readiness(landing)
    assert calls[-1] == (inspected["snapshot_id"], "cancel")
    assert [action for _, action in calls].count("arm") == 1
    assert after["landing"]["armed"] is False
    assert after["landing"]["state"] == "blocked"
    assert "Dependency / supply-chain policy" in after["landing"]["detail"]


def test_the_landing_watch_never_merges_after_the_readiness_it_relied_on_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session = _armed_pull_request(tmp_path)
    landing = landing_relationship(store, session.id)
    assert landing is not None
    record_landing_state(
        store, landing, status="armed", metadata_updates={"readiness_valid_until": "2026-10-01T12:00:00Z"}
    )
    fake = _use(monkeypatch, _FakeGitHub(mergeStateStatus="CLEAN"))

    reconcile_local_github_landing(store, session, now=datetime(2026, 10, 1, 12, tzinfo=UTC))

    settled = landing_relationship(store, session.id)
    assert settled is not None
    assert settled.status == "blocked"
    assert "expired at 2026-10-01T12:00:00Z" in str(landing_metadata(settled)["detail"])
    assert fake.merges() == []


def test_the_landing_watch_still_merges_before_the_readiness_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session = _armed_pull_request(tmp_path)
    landing = landing_relationship(store, session.id)
    assert landing is not None
    record_landing_state(
        store, landing, status="armed", metadata_updates={"readiness_valid_until": "2026-10-01T12:00:00Z"}
    )
    fake = _use(monkeypatch, _FakeGitHub(mergeStateStatus="CLEAN"))

    reconcile_local_github_landing(store, session, now=datetime(2026, 10, 1, 11, 59, tzinfo=UTC))

    settled = landing_relationship(store, session.id)
    assert settled is not None and settled.status == "merged"
    assert len(fake.merges()) == 1


# --------------------------------------------------------------------------- #
# trusted-base authority: a Change cannot weaken the policy that reviews it
# --------------------------------------------------------------------------- #


def _config_workspace(tmp_path: Path, *, base_config: str, head_config: str | None) -> Workspace:
    repo_root = tmp_path / "repo"
    repo = _init_repo(repo_root)
    _write(repo_root, "src/session.py", _BASE_SOURCE)
    _write(repo_root, ".lemoncrow/review.yaml", base_config)
    _commit(repo, "base", 0)
    _write(repo_root, "src/session.py", _CHANGED_SOURCE)
    if head_config is None:
        (repo_root / ".lemoncrow/review.yaml").unlink()
    else:
        _write(repo_root, ".lemoncrow/review.yaml", head_config)
    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, None, working_tree=True)
    build = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, repo_root, rng, title=build.packet.title)
    snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)
    revision = store.latest_revision(session.id)
    assert revision is not None
    store.publish_snapshot(session.id, revision.id)
    app = FastAPI()
    register_review_api(app, store, auth_dependency=make_token_dependency(TOKEN), repo_root=repo_root, port=PORT)
    return Workspace(TestClient(app, base_url=BASE), store, store_root, repo_root, session.id)


_TIGHT_BASE = "evidence_freshness:\n  external_observation_max_age: 1h\n"


@pytest.mark.parametrize(
    "head_config",
    ["evidence_freshness:\n  external_observation_max_age: 90d\n", "version: 1\n", None],
    ids=["loosened", "section-removed", "file-deleted"],
)
def test_a_change_to_review_policy_cannot_weaken_its_own_review(
    clock: _Clock, tmp_path: Path, head_config: str | None
) -> None:
    workspace = _config_workspace(tmp_path, base_config=_TIGHT_BASE, head_config=head_config)
    _review_all_current_targets(workspace)
    body = _readiness(workspace)
    assert body["freshness_policy"]["external_max_age_seconds"] == 3600
    trusted = next(layer for layer in body["policy_generation"]["layers"] if layer["layer"] == "trusted_base")
    assert trusted["digest"] == trusted_policy_digest(_TIGHT_BASE, base_known=True)

    _feed_supply_chain(workspace, "vulnerability_scan", "PASS")
    assert _condition(_readiness(workspace), "supply-chain-policy")["state"] == "pass"
    clock.advance(hours=2)
    assert _condition(_readiness(workspace), "supply-chain-policy")["state"] == "unknown"


# --------------------------------------------------------------------------- #
# Phase 14 item 28: policy-generation race
# --------------------------------------------------------------------------- #


@dataclass
class _OperatorPolicy:
    """A mutable org/repo policy the composition reads on every evaluation."""

    document: dict[str, Any]
    conditions: list[dict[str, Any]]

    def extension(self, session: ReviewSession, revision: Any) -> dict[str, Any]:
        return {
            "policy": [
                {"layer": "operator_review_policy", "source": "organization acme", "document": dict(self.document)}
            ],
            "conditions": [dict(item) for item in self.conditions],
        }


def _security_review(state: str) -> dict[str, Any]:
    return {
        "id": "policy:security-review",
        "label": "Security review required",
        "state": state,
        "detail": "Organization policy requires a security review.",
        "owner": "lemoncrow",
        "category": "policy",
    }


def test_tightening_policy_while_armed_cancels_the_landing_and_loosening_never_rearms(workspace: Workspace) -> None:
    calls: list[tuple[str, str]] = []
    policy = _OperatorPolicy(document={"required": []}, conditions=[])
    landing = _client(workspace, delegate=_recording_delegate(calls), extension=policy.extension)
    _link_pull_request(landing)
    _review_all_current_targets(landing)
    loose = _readiness(landing)
    assert loose["armable"] is True
    loose_generation = loose["policy_generation"]["digest"]
    assert [layer["layer"] for layer in loose["policy_generation"]["layers"]][-1] == "operator_review_policy"
    armed = _arm(landing, loose)
    assert armed.status_code == 200, armed.text
    assert calls == [(loose["snapshot_id"], "arm")]
    assert _readiness(landing)["landing"]["policy_generation"] == loose_generation

    # The organization tightens its policy while the landing is armed.
    policy.document = {"required": ["security-review"]}
    policy.conditions = [_security_review("pending")]
    tight = _readiness(landing)
    tight_generation = tight["policy_generation"]["digest"]
    assert tight_generation != loose_generation
    assert tight["snapshot_id"] == loose["snapshot_id"]
    assert _condition(tight, "policy:security-review")["state"] == "pending"
    assert tight["armable"] is False
    assert calls[-1] == (loose["snapshot_id"], "cancel")
    assert tight["landing"]["armed"] is False
    assert tight["landing"]["state"] == "blocked"
    # The stale decision stays visible: which generation it was armed under.
    assert tight["landing"]["policy_generation"] == loose_generation
    assert "Review policy changed after arming" in tight["landing"]["detail"]
    assert "Security review required" in tight["landing"]["detail"]
    stored = landing_relationship(landing.store, workspace.review_id)
    assert stored is not None
    assert landing_metadata(stored)["policy_generation_at_cancel"] == tight_generation

    # Weakening back never re-arms on its own, and a view taken under the
    # tight generation cannot arm under the loose one.
    policy.document = {"required": []}
    policy.conditions = []
    reverted = _readiness(landing)
    assert reverted["policy_generation"]["digest"] == loose_generation  # content-addressed
    assert reverted["armable"] is True
    assert reverted["landing"]["state"] == "blocked"
    assert [action for _, action in calls] == ["arm", "cancel"]
    assert _arm(landing, tight).status_code == 409
    assert [action for _, action in calls] == ["arm", "cancel"]


def test_loosening_policy_while_armed_revalidates_without_arming_again(workspace: Workspace) -> None:
    calls: list[tuple[str, str]] = []
    policy = _OperatorPolicy(document={"required": ["security-review"]}, conditions=[_security_review("pass")])
    landing = _client(workspace, delegate=_recording_delegate(calls), extension=policy.extension)
    _link_pull_request(landing)
    _review_all_current_targets(landing)
    tight = _readiness(landing)
    assert _arm(landing, tight).status_code == 200

    policy.document = {"required": []}
    policy.conditions = []
    loose = _readiness(landing)
    assert loose["policy_generation"]["digest"] != tight["policy_generation"]["digest"]
    assert loose["landing"]["armed"] is True
    assert loose["landing"]["state"] == "delegated"
    assert loose["landing"]["policy_generation"] == loose["policy_generation"]["digest"]
    assert loose["landing"]["policy_stale"] is False
    assert "revalidated" in loose["landing"]["detail"]
    stored = landing_relationship(landing.store, workspace.review_id)
    assert stored is not None
    assert landing_metadata(stored)["policy_revalidated_from"] == tight["policy_generation"]["digest"]
    assert calls == [(tight["snapshot_id"], "arm")]  # weakening never arms or merges


def test_a_policy_change_with_identical_conditions_still_refuses_a_stale_arm(
    clock: _Clock, workspace: Workspace
) -> None:
    calls: list[tuple[str, str]] = []
    policy = _OperatorPolicy(document={"required": []}, conditions=[])
    landing = _client(workspace, delegate=_recording_delegate(calls), extension=policy.extension)
    _link_pull_request(landing)
    _review_all_current_targets(landing)
    before = _readiness(landing)
    policy.document = {"required": [], "audit": "quarterly"}
    after = _readiness(landing)
    assert [row["state"] for row in before["conditions"]] == [row["state"] for row in after["conditions"]]
    assert before["conditions_hash"] != after["conditions_hash"]
    stale = _arm(landing, before)
    assert stale.status_code == 409, stale.text
    assert calls == []
    assert _arm(landing, after).status_code == 200
    record = landing_relationship(landing.store, workspace.review_id)
    assert record is not None
    metadata = landing_metadata(record)
    # Durable record of the decision: generation, its layers and when it was taken.
    assert metadata["policy_generation"] == after["policy_generation"]["digest"]
    assert [layer["layer"] for layer in metadata["policy_layers"]] == [
        "built_in",
        "trusted_base",
        "operator_review_policy",
    ]
    assert metadata["evaluated_at"] == after["evaluated_at"]
    reopened = landing_relationship(ReviewStore(workspace.store_root), workspace.review_id)
    assert reopened is not None and landing_metadata(reopened) == metadata


# --------------------------------------------------------------------------- #
# Phase 14 item 29 / item 16-17: typed identity and required-check spoofing
# --------------------------------------------------------------------------- #


def _human_result(workspace: Workspace, revision_id: str, status: str, name: str = "CI / unit tests") -> None:
    posted = workspace.post(
        f"/api/reviews/{workspace.review_id}/verification",
        json={"revision_id": revision_id, "name": name, "status": status, "source": "human"},
    )
    assert posted.status_code == 201, posted.text


@pytest.mark.parametrize("attestation_class", ["provider_ci", "lemoncrow_verification", "trusted_security_feed"])
def test_a_human_pass_titled_like_an_attested_failure_never_replaces_it(
    clock: _Clock, workspace: Workspace, attestation_class: str
) -> None:
    _review_all_current_targets(workspace)
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    _typed_check(workspace, revision.id, status="FAIL", attestation_class=attestation_class)
    clock.advance(seconds=1)
    _human_result(workspace, revision.id, "PASS")

    body = _readiness(workspace)
    assert _condition(body, "verification")["state"] == "fail"
    rows = [
        row
        for row in workspace.get(f"/api/reviews/{workspace.review_id}/verification").json()["checks"]
        if row["name"] == "CI / unit tests"
    ]
    assert sorted(row["authority"] for row in rows) == ["attested", "human"]
    assert body["armable"] is False


def test_a_human_pass_on_a_later_snapshot_never_clears_a_carried_provider_failure(
    clock: _Clock, workspace: Workspace
) -> None:
    first = workspace.store.latest_revision(workspace.review_id)
    assert first is not None
    _typed_check(workspace, first.id, status="FAIL")
    clock.advance(seconds=1)
    _advance_snapshot_without_rerunning_checks(workspace)
    second = workspace.store.latest_revision(workspace.review_id)
    assert second is not None and second.id != first.id
    clock.advance(seconds=1)
    _human_result(workspace, second.id, "PASS")

    verification = _condition(_readiness(workspace), "verification")
    assert verification["state"] == "pending"
    assert "CI / unit tests" in verification["detail"]
    assert "human observation" in verification["detail"] or "have not reported" in verification["detail"]


def test_a_human_pass_never_counts_while_the_attested_check_is_still_pending(
    clock: _Clock, workspace: Workspace
) -> None:
    _review_all_current_targets(workspace)
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    _typed_check(workspace, revision.id, status="NOT_RUN", attestation_class="lemoncrow_verification")
    clock.advance(seconds=1)
    _human_result(workspace, revision.id, "PASS")
    assert _condition(_readiness(workspace), "verification")["state"] == "pending"

    # Alone, a human PASS supports review but is never counted as verification.
    clock.advance(seconds=1)
    _human_result(workspace, revision.id, "PASS", name="Manual smoke")
    _typed_check(workspace, revision.id, status="PASS", attestation_class="lemoncrow_verification")
    verification = _condition(_readiness(workspace), "verification")
    assert verification["state"] == "pass"
    assert "1 current check(s) passed" in verification["detail"]
    assert "human observation(s) recorded PASS" in verification["detail"]


def test_two_attested_checks_sharing_a_title_are_two_checks(clock: _Clock, workspace: Workspace) -> None:
    _review_all_current_targets(workspace)
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    _typed_check(workspace, revision.id, status="FAIL", title="build", stable_key="linux")
    clock.advance(seconds=1)
    _typed_check(workspace, revision.id, status="PASS", title="build", stable_key="macos")
    assert _condition(_readiness(workspace), "verification")["state"] == "fail"


def test_a_title_waiver_cannot_waive_an_attested_check_but_its_check_key_can(
    clock: _Clock, workspace: Workspace
) -> None:
    first = workspace.store.latest_revision(workspace.review_id)
    assert first is not None
    _typed_check(workspace, first.id, status="FAIL", title="build", stable_key="linux")
    _typed_check(workspace, first.id, status="FAIL", title="build", stable_key="macos")
    clock.advance(seconds=1)
    _advance_snapshot_without_rerunning_checks(workspace)
    second = workspace.store.latest_revision(workspace.review_id)
    assert second is not None and second.id != first.id

    # A legacy title-only waiver row names no typed identity: it waives nothing typed.
    workspace.store.add_evidence(
        ReviewEvidence(
            id="",
            review_id=workspace.review_id,
            revision_id=second.id,
            kind="waiver",
            title="build",
            source="human",
            detail="looks fine to me",
            attestation_class="human_waiver",
            stable_key="build",
        )
    )
    assert _condition(_readiness(workspace), "verification")["state"] == "pending"

    url = f"/api/reviews/{workspace.review_id}/verification/waivers"
    ambiguous = workspace.post(url, json={"revision_id": second.id, "name": "build", "reason": "retired"})
    assert ambiguous.status_code == 409, ambiguous.text
    assert "check_key" in ambiguous.json()["detail"]

    checks = workspace.get(f"/api/reviews/{workspace.review_id}/verification").json()["checks"]
    linux = next(row for row in checks if row["name"] == "build" and row["stable_key"] == "linux")
    waived = workspace.post(
        url, json={"revision_id": second.id, "check_key": linux["check_key"], "reason": "linux runner retired"}
    )
    assert waived.status_code == 201, waived.text
    assert waived.json()["evidence"]["waived_requirement_key"] == f"attested:{linux['check_key']}"
    rows = {
        row["stable_key"]: row
        for row in workspace.get(f"/api/reviews/{workspace.review_id}/verification").json()["checks"]
        if row["name"] == "build"
    }
    assert rows["linux"]["waiver"]["reason"] == "linux runner retired"
    assert "waiver" not in rows["macos"]
    assert _condition(_readiness(workspace), "verification")["state"] == "pending"

    macos = rows["macos"]["check_key"]
    assert (
        workspace.post(
            url, json={"revision_id": second.id, "check_key": macos, "reason": "macos runner retired"}
        ).status_code
        == 201
    )
    verification = _condition(_readiness(workspace), "verification")
    assert verification["state"] == "waived"  # shown as waived, never as passed


def test_a_human_or_self_declared_supply_chain_pass_never_satisfies_the_policy(
    clock: _Clock, workspace: Workspace
) -> None:
    _review_all_current_targets(workspace)

    # The upload endpoint's human source: evidence, never a policy verdict.
    _upload_supply_chain(
        workspace, "vulnerability_scan", "PASS", observation_source="osv", payload=_SCAN, source="human"
    )
    supply = _condition(_readiness(workspace), "supply-chain-policy")
    assert (supply["state"], supply["label"]) == ("not_applicable", "Dependency / supply-chain policy")
    assert "1 PASS verdict(s) without a trusted security feed attestation" in supply["detail"]
    assert supply["evidence_refs"] == []

    # A typed trusted-security-feed attestation is the verdict that satisfies it.
    clock.advance(seconds=1)
    feed = _feed_supply_chain(workspace, "vulnerability_scan", "PASS")
    supply = _condition(_readiness(workspace), "supply-chain-policy")
    assert supply["state"] == "pass"
    assert [ref["evidence_id"] for ref in supply["evidence_refs"]] == [feed.id]

    # Spoof: a failing scan, then a PASS that merely claims the same CI source
    # and observation source, and a human PASS on top. Neither clears it.
    clock.advance(seconds=1)
    _upload_supply_chain(workspace, "vulnerability_scan", "FAIL", observation_source="trivy", payload=_SCAN)
    clock.advance(seconds=1)
    _upload_supply_chain(workspace, "vulnerability_scan", "PASS", observation_source="trivy", payload=_SCAN)
    clock.advance(seconds=1)
    _upload_supply_chain(
        workspace, "vulnerability_scan", "PASS", observation_source="trivy", payload=_SCAN, source="human"
    )
    body = _readiness(workspace)
    supply = _condition(body, "supply-chain-policy")
    assert supply["state"] == "fail"
    assert "1 current supply-chain policy result(s) failed." in supply["detail"]
    assert body["armable"] is False


def test_an_explicit_human_waiver_unblocks_the_supply_chain_policy_but_never_an_attested_failure(
    clock: _Clock, workspace: Workspace
) -> None:
    _review_all_current_targets(workspace)
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    _upload_supply_chain(workspace, "vulnerability_scan", "FAIL", observation_source="trivy", payload=_SCAN)
    blocked = _condition(_readiness(workspace), "supply-chain-policy")
    assert (blocked["state"], blocked["requirement_key"], blocked["waivable"]) == (
        "fail",
        "supply-chain-policy",
        True,
    )

    url = f"/api/reviews/{workspace.review_id}/verification/waivers"
    waived = workspace.post(
        url,
        json={
            "revision_id": revision.id,
            "requirement_key": "supply-chain-policy",
            "reason": "scanner false positive on a vendored test fixture",
        },
    )
    assert waived.status_code == 201, waived.text
    evidence = waived.json()["evidence"]
    assert (evidence["attestation_class"], evidence["waived_requirement_key"]) == (
        "human_waiver",
        "supply-chain-policy",
    )
    body = _readiness(workspace)
    supply = _condition(body, "supply-chain-policy")
    assert (supply["state"], supply["reason"], supply["waivable"]) == ("waived", "waived", False)
    assert "scanner false positive" in supply["detail"]
    assert "1 current supply-chain policy result(s) failed." in supply["detail"]  # observed verdict unchanged
    assert body["armable"] is True

    # A current attested FAIL outranks any waiver and cannot itself be waived.
    clock.advance(seconds=1)
    _feed_supply_chain(workspace, "vulnerability_scan", "FAIL")
    supply = _condition(_readiness(workspace), "supply-chain-policy")
    assert (supply["state"], supply["waivable"]) == ("fail", False)
    refused = workspace.post(
        url, json={"revision_id": revision.id, "requirement_key": "supply-chain-policy", "reason": "ship it"}
    )
    assert refused.status_code == 409, refused.text


# --------------------------------------------------------------------------- #
# store/API round-trips
# --------------------------------------------------------------------------- #


def test_evidence_upload_round_trips_source_time_revision_and_validity(clock: _Clock, workspace: Workspace) -> None:
    observed = clock.now - timedelta(hours=1)
    valid_until = clock.now + timedelta(hours=3)
    uploaded = _upload_supply_chain(
        workspace,
        "vulnerability_scan",
        "PASS",
        observation_source="osv",
        payload=_SCAN,
        observed_at=observed.isoformat(),
        source_revision="advisory-db@100",
        valid_until=valid_until.isoformat(),
    )
    stored = ReviewStore(workspace.store_root).get_evidence(uploaded["id"])
    assert stored is not None
    assert stored.observed_at == format_timestamp(observed)
    metadata = json.loads(stored.detail)
    assert metadata["source_timestamp"] == format_timestamp(observed)
    assert metadata["source_revision"] == "advisory-db@100"
    assert metadata["valid_until"] == format_timestamp(valid_until)
    _review_all_current_targets(workspace)
    # An upload with a self-declared source is never relied on by readiness;
    # the same validity on a typed feed attestation bounds it.
    assert _readiness(workspace)["valid_until"] == ""
    _feed_supply_chain(workspace, "vulnerability_scan", "PASS", observed_at=observed, valid_until=valid_until)
    assert _readiness(workspace)["valid_until"] == format_timestamp(valid_until)

    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    for params in ({"observed_at": "yesterday"}, {"valid_until": "2026-10-01T12:00:00"}):
        refused = workspace.post(
            f"/api/reviews/{workspace.review_id}/evidence/upload",
            params={
                "kind": "vulnerability_scan",
                "revision_id": revision.id,
                "source": "ci",
                "artifact_digest": _DIGEST,
                **params,
            },
            content=_SCAN,
        )
        assert refused.status_code == 422, refused.text


# --------------------------------------------------------------------------- #
# readiness side effects: idempotent, audited, never on a transient failure
# --------------------------------------------------------------------------- #


def _activity(workspace: Workspace, kind: str) -> list[Any]:
    return [row for row in workspace.store.list_activity_events(workspace.review_id) if row.kind == kind]


def _armed(
    workspace: Workspace, *, before_arm: Any = None, **client: Any
) -> tuple[Workspace, list[tuple[str, str]], dict[str, Any]]:
    calls: list[tuple[str, str]] = []
    landing = _client(workspace, delegate=_recording_delegate(calls), **client)
    _link_pull_request(landing)
    _review_all_current_targets(landing)
    if before_arm is not None:
        before_arm(landing)
    inspected = _readiness(landing)
    armed = _arm(landing, inspected)
    assert armed.status_code == 200, armed.text
    assert calls == [(inspected["snapshot_id"], "arm")]
    return landing, calls, inspected


def test_a_transient_readiness_failure_reads_unknown_and_never_withdraws_an_armed_landing(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky = {"broken": False}

    def gather(sources: Any) -> object:
        if flaky["broken"]:
            raise TimeoutError("attestation store unavailable")
        return 0

    def evaluate(inputs: ReadinessInputs) -> Iterable[ReadinessCondition]:
        return (ReadinessCondition(id="downstream", label="Downstream checks", state="pass", detail="ok."),)

    landing, calls, inspected = _armed(
        workspace, conditions=(RegisteredCondition("downstream", 76, evaluate, gather=gather),)
    )

    # A rule input that cannot be read is unknown: it blocks arming, but the
    # armed decision is kept, and reading again neither cancels nor re-audits.
    flaky["broken"] = True
    for _ in range(3):
        body = _readiness(landing)
        failed = _condition(body, "readiness-error:downstream")
        assert failed["state"] == "unknown" and failed["reason"] == "evaluation_error"
        assert body["armable"] is False
        assert body["landing"]["armed"] is True and body["landing"]["state"] == "delegated"
    assert calls == [(inspected["snapshot_id"], "arm")]
    deferred = _activity(landing, "landing.revalidation_deferred")
    assert len(deferred) == 1
    assert json.loads(deferred[0].detail_json) == {"origin": "read", "cause": "Readiness condition unavailable"}

    # The store fails mid-read: every fact is unknown, still nothing is withdrawn.
    flaky["broken"] = False

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise OSError("database is locked")

    with monkeypatch.context() as patch:
        patch.setattr(ReviewStore, "list_annotations", unavailable)
        body = _readiness(landing)
    blocking = [row for row in body["conditions"] if row["state"] not in {"pass", "not_applicable", "waived"}]
    assert blocking and {row["reason"] for row in blocking} == {"evaluation_error"}
    assert body["landing"]["armed"] is True
    assert calls == [(inspected["snapshot_id"], "arm")]

    # Only a definite unsatisfied evaluation withdraws it.
    assert _readiness(landing)["armable"] is True
    posted = landing.post(
        f"/api/reviews/{workspace.review_id}/annotations",
        json={
            "path": "src/session.py",
            "side": "new",
            "start_line": 8,
            "end_line": 8,
            "body": "This must not merge yet.",
            "kind": "request_change",
        },
    )
    assert posted.status_code == 201, posted.text
    assert calls[-1] == (inspected["snapshot_id"], "cancel")
    assert _readiness(landing)["landing"]["state"] == "blocked"


def test_a_definite_regression_is_withdrawn_and_audited_exactly_once(clock: _Clock, workspace: Workspace) -> None:
    from lemoncrow.pro.capabilities.review.landing import claim_readiness_withdrawal

    landing, calls, inspected = _armed(
        workspace,
        before_arm=lambda client: _feed_supply_chain(client, "vulnerability_scan", "PASS"),
    )
    armed_record = landing_relationship(landing.store, workspace.review_id)
    assert armed_record is not None

    clock.advance(hours=25)  # the scan expires; nothing about the Change moves
    revalidate = landing.client.app.state.review_landing_revalidator  # type: ignore[attr-defined]
    outcomes = [revalidate(workspace.review_id, origin="sweep")]
    for _ in range(3):
        body = _readiness(landing)
        outcomes.append(revalidate(workspace.review_id, origin="sweep"))
    assert outcomes == ["withdrawn", "none", "none", "none"]
    assert [action for _, action in calls] == ["arm", "cancel"]
    assert body["landing"]["state"] == "blocked" and body["landing"]["armed"] is False
    assert "Provider landing was cancelled" in body["landing"]["detail"]
    [withdrawn] = _activity(landing, "landing.withdrawn")
    detail = json.loads(withdrawn.detail_json)
    assert detail["origin"] == "sweep"
    assert detail["blocked_by"] == [
        {"id": "supply-chain-policy", "label": "Dependency / supply-chain policy passed", "reason": "evidence_expired"}
    ]
    assert detail["generation"] == detail["previous_generation"] == inspected["policy_generation"]["digest"]

    # The claim is compare-and-set: a second pass holding the intent as it
    # was read before the first withdrawal can never claim (or cancel) again.
    assert (
        claim_readiness_withdrawal(
            landing.store, armed_record, detail="late", blocked_by=["x"], policy_generation="policy:late"
        )
        is None
    )


# --------------------------------------------------------------------------- #
# item 13: an expired PASS is never counted as passing anywhere
# --------------------------------------------------------------------------- #


def test_the_verification_summary_never_counts_an_expired_pass(clock: _Clock, workspace: Workspace) -> None:
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    feed = _typed_check(
        workspace,
        revision.id,
        status="PASS",
        title="Advisories",
        stable_key="advisories",
        attestation_class="trusted_security_feed",
    )

    def verification() -> dict[str, Any]:
        response = workspace.get(f"/api/reviews/{workspace.review_id}/verification")
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    fresh = verification()
    row = next(item for item in fresh["checks"] if item["evidence_id"] == feed.id)
    assert row["freshness"]["state"] == "current"
    assert (fresh["summary"]["pass"], fresh["summary"]["unknown"]) == (1, 0)

    clock.advance(hours=25)  # the feed observation ages out on the unchanged Snapshot
    aged = verification()
    row = next(item for item in aged["checks"] if item["evidence_id"] == feed.id)
    assert row["status"] == "PASS"  # the observation itself is never rewritten
    assert row["freshness"]["state"] == "expired"
    assert (aged["summary"]["pass"], aged["summary"]["unknown"]) == (0, 1)
    assert _condition(_readiness(workspace), "verification")["state"] == "unknown"


# --------------------------------------------------------------------------- #
# item 15: a policy generation change is visible, audited and routed
# --------------------------------------------------------------------------- #


def _approve(workspace: Workspace) -> None:
    revision = workspace.store.latest_revision(workspace.review_id)
    assert revision is not None
    approved = workspace.post(
        f"/api/reviews/{workspace.review_id}/outcome",
        json={"outcome": "lgtm", "summary": "ship it", "expected_revision_id": revision.id},
    )
    assert approved.status_code == 200, approved.text


def test_tightening_policy_names_the_generation_change_and_labels_older_approvals(workspace: Workspace) -> None:
    from lemoncrow.pro.capabilities.review.readiness import PolicyChangeNotice, short_generation

    calls: list[tuple[str, str]] = []
    notices: list[PolicyChangeNotice] = []
    policy = _OperatorPolicy(document={"required": []}, conditions=[])
    landing = _client(workspace, delegate=_recording_delegate(calls), extension=policy.extension, hook=notices.append)
    _link_pull_request(landing)
    _review_all_current_targets(landing)
    _approve(landing)
    loose = _readiness(landing)
    assert not any(row["id"] == "policy-generation" for row in loose["conditions"])
    assert "approved under policy" not in _condition(loose, "human-approval-state")["detail"]
    assert _arm(landing, loose).status_code == 200
    reviewer = landing.store.latest_outcomes(workspace.review_id)[0].reviewer_id
    old, old_short = loose["policy_generation"]["digest"], loose["policy_generation"]["short"]

    policy.document = {"required": ["security-review"]}
    policy.conditions = [_security_review("pending")]
    tight = _readiness(landing)
    new = tight["policy_generation"]["digest"]

    # A visible blocker, listed first, naming the old and the new generation.
    changed = tight["conditions"][0]
    assert changed["id"] == "policy-generation" and changed["state"] == "pending"
    assert changed["reason"] == "policy_changed"
    assert f"generation {old_short} -> {short_generation(new)}" in changed["detail"]
    assert "Security review required" in changed["detail"]
    assert changed["previous_generations"] == [old] and changed["generation"] == new
    # The approval still counts, shown as given under the older policy.
    approval = _condition(tight, "human-approval-state")
    assert approval["state"] == "pass"
    assert f"{reviewer} approved under policy {old_short}" in approval["detail"]
    assert tight["landing"]["state"] == "blocked"

    # Audited once, and handed once to the typed attention hook (P9-ATTN).
    _readiness(landing)
    events = _activity(landing, "policy.generation_changed")
    assert len(events) == 1
    detail = json.loads(events[0].detail_json)
    assert (detail["previous_generations"], detail["generation"], detail["tightened"]) == ([old], new, True)
    assert detail["blocked_by"] == ["Security review required"]
    assert detail["approved_under_previous"] == [reviewer]
    assert notices == [
        PolicyChangeNotice(
            review_id=workspace.review_id,
            snapshot_id=tight["snapshot_id"],
            previous_generations=(old,),
            generation=new,
            tightened=True,
            blocked_by=("Security review required",),
            approved_under_previous=(reviewer,),
        )
    ]
    [withdrawn] = _activity(landing, "landing.withdrawn")
    assert json.loads(withdrawn.detail_json)["policy_changed"] is True

    # Satisfying the new policy: the change stays named (the decisions predate
    # it) but no longer blocks.
    policy.conditions = [_security_review("pass")]
    satisfied = _readiness(landing)
    assert _condition(satisfied, "policy-generation")["state"] == "pass"
    assert satisfied["armable"] is True


def test_the_landing_watch_withdraws_when_the_readiness_rules_changed_since_arming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review import readiness as readiness_module

    store, session = _armed_pull_request(tmp_path)
    landing = landing_relationship(store, session.id)
    assert landing is not None
    layers = [built_in_policy_layer(), policy_layer("trusted_base", "trusted base", {"version": 1})]
    armed_under = policy_generation(layers)
    record_landing_state(
        store,
        landing,
        status="armed",
        metadata_updates={
            "policy_generation": armed_under.digest,
            "policy_layers": [layer.to_wire() for layer in layers],
        },
    )
    fake = _use(monkeypatch, _FakeGitHub(mergeStateStatus="CLEAN"))
    monkeypatch.setattr(
        readiness_module, "READINESS_RULES_VERSION", readiness_module.READINESS_RULES_VERSION + 1
    )  # a future upgrade

    # Wired to the Reader, the watch hands the intent to readiness revalidation
    # and merges nothing in this pass.
    revalidated: list[str] = []
    reconcile_local_github_landing(store, session, revalidate=lambda: revalidated.append(session.id))
    assert revalidated == [session.id] and fake.merges() == []
    current = landing_relationship(store, session.id)
    assert current is not None and current.status == "armed"

    # Without a way to re-evaluate readiness, it withdraws instead of merging.
    reconcile_local_github_landing(store, session)
    settled = landing_relationship(store, session.id)
    assert settled is not None and settled.status == "blocked"
    assert f"generation {armed_under.short} ->" in str(landing_metadata(settled)["detail"])
    assert fake.merges() == []


def test_the_landing_watch_merges_when_the_recorded_generation_is_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session = _armed_pull_request(tmp_path)
    landing = landing_relationship(store, session.id)
    assert landing is not None
    layers = [built_in_policy_layer(), policy_layer("trusted_base", "trusted base", {"version": 1})]
    record_landing_state(
        store,
        landing,
        status="armed",
        metadata_updates={
            "policy_generation": policy_generation(layers).digest,
            "policy_layers": [layer.to_wire() for layer in layers],
        },
    )
    fake = _use(monkeypatch, _FakeGitHub(mergeStateStatus="CLEAN"))
    reconcile_local_github_landing(store, session, revalidate=lambda: pytest.fail("nothing changed"))
    settled = landing_relationship(store, session.id)
    assert settled is not None and settled.status == "merged"
    assert len(fake.merges()) == 1


def test_operator_policy_is_one_core_condition_supplied_by_a_gatherer() -> None:
    from lemoncrow.pro.capabilities.review.readiness import ApprovalRecord, OrgCheckFact, OrgPolicyFacts

    with pytest.raises(ValueError, match="no readiness condition is registered for nope"):
        DEFAULT_CONDITIONS.with_gatherers({"nope": lambda sources: None})
    assert not [row for row in DEFAULT_CONDITIONS.evaluate(_inputs()).conditions if row.id.startswith("org-policy")]

    older = "policy:" + "0" * 64
    facts = OrgPolicyFacts(
        minimum_reviewers=2,
        required_reviewers=("alice",),
        approvals=(ApprovalRecord("alice", older), ApprovalRecord("bob")),
        required_checks=(OrgCheckFact(stable_key="advisories", requirement_key="feed", satisfied=False, expired=True),),
    )
    registry = DEFAULT_CONDITIONS.with_gatherers({"org_policy": lambda sources: facts})
    gathered = registry.gather(object(), {})  # type: ignore[arg-type]
    rows = {row.id: row for row in registry.evaluate(_inputs(facts=gathered)).conditions}
    assert rows["org-policy:minimum-approvals"].state == "pass"
    assert "alice approved under policy 000000000000." in rows["org-policy:minimum-approvals"].detail
    assert rows["org-policy:reviewer:alice"].state == "pass"
    check = rows["org-policy:check:feed"]
    assert (check.state, check.reason) == ("unknown", "evidence_expired")
    assert check.extra["waivable"] is False


def test_the_core_accepts_a_supplied_trusted_base_layer_in_place_of_the_derived_one(
    workspace: Workspace,
) -> None:
    # A fake hosted trusted-base layer: provider-proven provenance the core
    # cannot derive from the Snapshot's base tree.
    supplied: dict[str, Any] = {
        "layer": "trusted_base",
        "source": "fake provider trusted base",
        "document": {"commit_sha": "a" * 40, "blob_sha": "blob-1", "policy_state": "final"},
        "state": "final",
    }
    _review_all_current_targets(workspace)
    client = _client(workspace, extension=lambda session, revision: {"policy": [supplied]})

    body = _readiness(client)
    generation = body["policy_generation"]
    fake = policy_layer("trusted_base", "fake provider trusted base", supplied["document"])
    assert generation["layers"] == [built_in_policy_layer().to_wire(), fake.to_wire()]
    assert generation["digest"] == policy_generation([built_in_policy_layer(), fake]).digest
    assert generation["authority"] == "final"
    assert not any(row["id"] == "policy-authority" for row in body["conditions"])
    assert body["armable"] is True

    # A new trusted-base commit with the same config is a new generation.
    supplied["document"] = {**supplied["document"], "commit_sha": "c" * 40}
    moved = _readiness(client)["policy_generation"]
    assert moved["digest"] != generation["digest"]

    # An invalid trusted base blocks, whatever else governs; an unknown state
    # string fails closed as invalid.
    for state in ("invalid", "trust-me"):
        supplied["state"] = state
        blocked = _readiness(client)
        assert blocked["policy_generation"]["authority"] == "invalid"
        condition = _condition(blocked, "policy-authority")
        assert (condition["state"], condition["reason"]) == ("fail", "invalid_policy")
        assert "fake provider trusted base" in condition["detail"]
        assert blocked["armable"] is False

    # Provisional alone (no final operator layer) blocks as unknown, as before.
    supplied["state"] = "provisional"
    provisional = _condition(_readiness(client), "policy-authority")
    assert (provisional["state"], provisional["reason"]) == ("unknown", "provisional_policy")


def test_required_run_plan_work_that_will_not_run_is_a_core_condition_never_a_pass() -> None:
    from lemoncrow.pro.capabilities.review.readiness import RunPlanBlocker, RunPlanFacts, definite_blockers

    def evaluated(facts: RunPlanFacts | None) -> Any:
        return DEFAULT_CONDITIONS.evaluate(_inputs(facts={} if facts is None else {"run_plan": facts}))

    def run_plan(facts: RunPlanFacts | None) -> list[ReadinessCondition]:
        return [row for row in evaluated(facts).conditions if row.id == "run-plan-required"]

    assert "run_plan" in [item.key for item in DEFAULT_CONDITIONS.entries]
    assert run_plan(None) == []  # a composition without a run planner
    assert run_plan(RunPlanFacts(snapshot_number=2)) == []  # every required entry runs: absent, never PASS

    blockers = (
        RunPlanBlocker("check:security:sast", "security:sast", "not_available"),
        RunPlanBlocker("ai.investigators", "", "limited_by_budget"),
    )
    [blocked] = run_plan(RunPlanFacts(snapshot_number=2, blockers=blockers))
    assert (blocked.state, blocked.label, blocked.category, blocked.reason) == (
        "pending",
        "Required checks planned",
        "verification",
        "not_planned",
    )
    assert blocked.detail == (
        "Required on Snapshot 2 but not run: security:sast (no executor), ai.investigators (Limited by budget)."
    )
    assert blocked.extra["required_blockers"] == ["check:security:sast", "ai.investigators"]
    assert [row["id"] for row in definite_blockers([blocked.to_wire()])] == ["run-plan-required"]
    assert evaluated(RunPlanFacts(snapshot_number=2, blockers=blockers)).armable is False

    # A plan that cannot be computed fails closed: pending (never armable),
    # but a planning failure alone defers rather than withdraws a landing.
    failed_facts = RunPlanFacts(snapshot_number=2, error="usage ledger offline")
    [failed] = run_plan(failed_facts)
    assert (failed.state, failed.reason) == ("pending", "evaluation_error")
    assert failed.detail == "The effective run plan could not be computed: usage ledger offline."
    assert definite_blockers([failed.to_wire()]) == []
    assert evaluated(failed_facts).armable is False
