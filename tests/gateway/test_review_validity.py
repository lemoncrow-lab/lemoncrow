"""Trust T1: one deterministic validity vocabulary over the existing Review authorities.

``validity.py`` explains and never decides. These tests pin three things:

* every reopen, reuse, reverify and expiry an existing authority decides carries a
  registered reason code;
* the code alone decides the explanatory state, and an unknown code is refused;
* the same inputs give the same reason chain.

The plan's mutation-style cases (Trust plan section 28) are here: an unchanged target
preserves, a changed body reopens, a base-only move preserves with an explicit reason, an
unknown fingerprint never preserves, a check carries only with the exact key, each
changed carry-key component explains the refusal, external evidence expires
independently of the Snapshot, and a policy generation change re-evaluates readiness.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.check_ingest import (
    CI_EQUIVALENCE_FLAG,
    EquivalenceKey,
    ci_equivalence_enabled,
    explain_equivalent,
    find_equivalent,
)
from lemoncrow.pro.capabilities.review.freshness import FreshnessPolicy, assess_freshness, format_timestamp
from lemoncrow.pro.capabilities.review.incremental import (
    _COMPONENT_REASONS,
    CARRY_FLAG,
    CarryKey,
    carry_enabled,
    explain_carry,
    find_carry,
    record_carry,
)
from lemoncrow.pro.capabilities.review.readiness import (
    ConditionRegistry,
    EvidenceRef,
    PolicyDecisionFacts,
    ReadinessCondition,
    ReadinessInputs,
    RegisteredCondition,
    built_in_policy_layer,
    policy_generation,
)
from lemoncrow.pro.capabilities.review.revisions import reconcile
from lemoncrow.pro.capabilities.review.runtime_validation import RUNTIME_VALIDATION_MEDIA_TYPE
from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence, ReviewMark, ReviewRevision, ReviewUnit
from lemoncrow.pro.capabilities.review.store import DerivedResult
from lemoncrow.pro.capabilities.review.validity import (
    REASONS,
    VALIDITY_STATES,
    ValidityResult,
    carry_result,
    condition_results,
    equivalence_result,
    freshness_result,
    mark_event_reason,
    mark_results,
    result,
)
from lemoncrow.pro.environment.execution import (
    EXECUTOR_FINGERPRINT_VERSION,
    EnvironmentFingerprint,
    ExecutableIdentity,
    ExecutionRecipe,
    ExecutionResult,
)

# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #


def test_every_code_implies_one_known_state_and_every_state_is_reachable() -> None:
    assert {reason.state for reason in REASONS.values()} == set(VALIDITY_STATES)
    for code, reason in REASONS.items():
        assert reason.code == code and reason.summary
        assert result(code, "subject").state == reason.state


def test_an_unregistered_code_or_a_contradicting_state_is_refused() -> None:
    with pytest.raises(ValueError, match="unregistered"):
        result("looks_fine", "subject")
    with pytest.raises(ValueError, match="implies 'reopened'"):
        ValidityResult(state="preserved", reason_code="content_changed", subject_ref="subject")
    with pytest.raises(ValueError, match="causes"):
        result("content_changed", "subject", causes=("looks_fine",))
    with pytest.raises(ValueError, match="subject"):
        result("content_changed", "")


def test_the_wire_is_the_chain_and_the_detail_defaults_to_the_registered_sentence() -> None:
    wire = result("carry_policy_changed", "check:unit#1", causes=("carry_inputs_changed",)).to_wire()
    assert wire == {
        "state": "reverify",
        "reason_code": "carry_policy_changed",
        "chain": ["carry_policy_changed", "carry_inputs_changed"],
        "subject_ref": "check:unit#1",
        "source_revision": "",
        "current_revision": "",
        "evidence_refs": [],
        "detail": REASONS["carry_policy_changed"].summary,
    }


def test_every_carry_component_explains_with_a_registered_reverify_code() -> None:
    for _name, code in _COMPONENT_REASONS:
        assert REASONS[code].state == "reverify"
    assert [name for name, _code in _COMPONENT_REASONS] == [
        "policy",
        "recipe",
        "executor",
        "scope",
        "attempt",
        "inputs",
    ]


# --------------------------------------------------------------------------- #
# human judgments (revisions.reconcile)
# --------------------------------------------------------------------------- #


def _unit(
    key: str,
    fingerprint: str,
    *,
    method: str = "blob_sha256",
    kind: str = "file",
    path: str = "src/a.py",
    ordinal: int = 0,
) -> ReviewUnit:
    return ReviewUnit(
        revision_id="rrv-1",
        unit_key=key,
        kind=kind,  # type: ignore[arg-type]
        path=path,
        ordinal=ordinal,
        content_fingerprint=fingerprint,
        fingerprint_method=method,  # type: ignore[arg-type]
    )


def _mark(key: str, fingerprint: str, state: str = "reviewed") -> ReviewMark:
    return ReviewMark(
        review_id="rev-1",
        unit_key=key,
        state=state,  # type: ignore[arg-type]
        reviewed_revision_id="rrv-1",
        content_fingerprint=fingerprint,
        reviewer_id="alice",
    )


_UNKNOWN = _unit("k", "fp-x", method="unknown")

_RECONCILE_CASES = {
    # name: (previous units, next units, mark, base_only, code)
    "unchanged target preserves": (
        [_unit("k", "fp-a")],
        [_unit("k", "fp-a")],
        _mark("k", "fp-a"),
        (),
        "content_unchanged",
    ),
    "changed body reopens": ([_unit("k", "fp-a")], [_unit("k", "fp-b")], _mark("k", "fp-a"), (), "content_changed"),
    "base-only move preserves": (
        [_unit("k", "fp-a")],
        [_unit("k", "fp-b")],
        _mark("k", "fp-a"),
        ("k",),
        "base_only_equivalent",
    ),
    "unknown fingerprint cannot preserve": (
        [_unit("k", "fp-a")],
        [_UNKNOWN],
        _mark("k", "fp-a"),
        (),
        "fingerprint_unknown",
    ),
    "unknown stays unknown": ([_UNKNOWN], [_UNKNOWN], _mark("k", "fp-a", "unknown"), (), "fingerprint_unknown"),
    "identity restored": ([_UNKNOWN], [_unit("k", "fp-a")], _mark("k", "fp-a", "unknown"), (), "fingerprint_restored"),
    "restored but changed": ([_UNKNOWN], [_unit("k", "fp-b")], _mark("k", "fp-a", "unknown"), (), "content_changed"),
    "a request is kept": (
        [_unit("k", "fp-a")],
        [_UNKNOWN],
        _mark("k", "fp-a", "needs_changes"),
        (),
        "human_request_kept",
    ),
    "no attestation": (
        [_unit("k", "fp-a")],
        [_unit("k", "fp-b")],
        _mark("k", "fp-a", "unreviewed"),
        (),
        "no_attestation",
    ),
    "hunks renumbered": (
        [_unit("hun:0", "fp-a", kind="hunk")],
        [_unit("hun:0", "fp-a", kind="hunk"), _unit("hun:1", "fp-c", kind="hunk", ordinal=1)],
        _mark("hun:0", "fp-a"),
        (),
        "hunks_renumbered",
    ),
    "behavior judgment kept": ([], [], _mark("beh:login", "fp-a"), (), "behavior_mark_kept"),
}


@pytest.mark.parametrize("case", sorted(_RECONCILE_CASES))
def test_each_reconciliation_rule_names_itself(case: str) -> None:
    previous, following, mark, base_only, code = _RECONCILE_CASES[case]
    decided = reconcile(previous, following, [mark], base_only=frozenset(base_only))
    [explained] = mark_results(decided, current_revision="rrv-2")
    assert explained.reason_code == code
    assert explained.state == REASONS[code].state
    assert (explained.source_revision, explained.current_revision) == ("rrv-1", "rrv-2")
    # Deterministic: the same inputs give the same chain.
    assert mark_results(reconcile(previous, following, [mark], base_only=frozenset(base_only))) == (
        replace(explained, current_revision=""),
    )


def test_reviewed_never_explains_as_preserved_across_changed_or_unknown_content() -> None:
    for following in ([_unit("k", "fp-b")], [_UNKNOWN]):
        [explained] = mark_results(reconcile([_unit("k", "fp-a")], following, [_mark("k", "fp-a")]))
        assert explained.state != "preserved"


def test_a_removed_unit_is_invalid_and_a_renamed_one_says_it_moved() -> None:
    removed = mark_results(reconcile([_unit("k", "fp-a")], [_unit("j", "fp-b")], [_mark("k", "fp-a")]))
    assert [(item.subject_ref, item.reason_code, item.state) for item in removed] == [
        ("mark:alice:k", "unit_removed", "invalid")
    ]

    old = _unit("sym:src/a.py::f", "fp-a", kind="symbol")
    old = replace(old, symbol="f")
    from lemoncrow.pro.capabilities.review.units import unit_key

    old = replace(old, unit_key=unit_key("symbol", "src/a.py", "f", 0))
    new = replace(old, path="src/b.py", unit_key=unit_key("symbol", "src/b.py", "f", 0))
    moved = mark_results(reconcile([old], [new], [_mark(old.unit_key, "fp-a")], renames=[("src/a.py", "src/b.py")]))
    # Only the surviving mark explains; the old key is the migrated row, not a removal.
    assert [(item.subject_ref, item.chain) for item in moved] == [
        (f"mark:alice:{new.unit_key}", ("content_unchanged", "renamed"))
    ]


def test_every_mark_that_still_applies_carries_exactly_one_reason() -> None:
    units = [_unit(f"k{i}", f"fp-{i}") for i in range(4)]
    changed = [replace(units[0], content_fingerprint="fp-new"), *units[1:3], _UNKNOWN]
    marks = [_mark(f"k{i}", f"fp-{i}") for i in range(3)] + [_mark("k", "fp-x", "needs_changes")]
    decided = reconcile([*units, _UNKNOWN], changed, marks)
    keys = [(reviewer, key) for reviewer, key, _code in decided.reasons]
    assert sorted(keys) == sorted((mark.reviewer_id, mark.unit_key) for mark in decided.marks)
    assert len(keys) == len(set(keys))


def test_the_persisted_mark_event_reason_is_the_registered_sentence() -> None:
    assert mark_event_reason("content_changed") == (
        "revision reconciliation: the content differs from what the reviewer judged"
    )
    assert mark_event_reason("fingerprint_unknown", renamed=True).startswith("part identity migrated across rename; ")
    with pytest.raises(ValueError):
        mark_event_reason("looks_fine")


# --------------------------------------------------------------------------- #
# evidence freshness
# --------------------------------------------------------------------------- #

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


def _evidence(kind: str, *, created_at: str, detail: str = "") -> ReviewEvidence:
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
    )


def test_each_freshness_rule_names_itself() -> None:
    hours_ago = format_timestamp(NOW - timedelta(hours=2))
    cases = {
        "snapshot_bound": assess_freshness(_evidence("sbom", created_at=hours_ago), now=NOW),
        "observation_current": assess_freshness(_evidence("vulnerability_scan", created_at=hours_ago), now=NOW),
        "observation_expired": assess_freshness(
            _evidence("vulnerability_scan", created_at=hours_ago),
            now=NOW,
            policy=FreshnessPolicy(external_max_age_seconds=3600),
        ),
        "freshness_policy_unreadable": assess_freshness(
            _evidence("vulnerability_scan", created_at=hours_ago),
            now=NOW,
            policy=FreshnessPolicy(external_max_age_seconds=0, error="freshness section is invalid"),
        ),
        "observation_time_unknown": assess_freshness(_evidence("vulnerability_scan", created_at=""), now=NOW),
        "valid_until_unreadable": assess_freshness(
            _evidence("vulnerability_scan", created_at=hours_ago, detail=json.dumps({"valid_until": "soon"})),
            now=NOW,
        ),
    }
    for code, assessed in cases.items():
        explained = freshness_result(assessed)
        assert (explained.reason_code, explained.state) == (code, REASONS[code].state), code
        assert explained.evidence_refs == (assessed.evidence_id,)
    # The explanation agrees with the decision it explains.
    assert {code: assessed.state for code, assessed in cases.items()} == {
        "snapshot_bound": "current",
        "observation_current": "current",
        "observation_expired": "expired",
        "freshness_policy_unreadable": "expired",
        "observation_time_unknown": "unknown",
        "valid_until_unreadable": "unknown",
    }


def test_external_evidence_expires_on_the_same_snapshot() -> None:
    scan = _evidence("vulnerability_scan", created_at=format_timestamp(NOW - timedelta(hours=2)))
    before = freshness_result(assess_freshness(scan, now=NOW))
    after = freshness_result(assess_freshness(scan, now=NOW + timedelta(days=2)))
    assert (before.state, after.state) == ("preserved", "expired")
    assert after.reason_code == "observation_expired"
    # Nothing about the Snapshot moved: the same row on the same revision expired by time alone.
    assert before.subject_ref == after.subject_ref == f"evidence:{scan.id}"
    sbom = _evidence("sbom", created_at=format_timestamp(NOW - timedelta(days=400)))
    assert freshness_result(assess_freshness(sbom, now=NOW + timedelta(days=2))).reason_code == "snapshot_bound"


# --------------------------------------------------------------------------- #
# cross-Snapshot check carry (incremental.explain_carry)
# --------------------------------------------------------------------------- #


@dataclass
class _CarryStore:
    """The four store reads and two writes the carry path uses, in memory."""

    revisions: list[Any]
    derived: dict[tuple[str, str, str], DerivedResult] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, bytes] = field(default_factory=dict)

    def list_revisions(self, _review_id: str) -> list[Any]:
        return list(self.revisions)

    def insert_derived_result(self, row: DerivedResult) -> None:
        self.derived.setdefault((row.kind, row.revision_id, row.scope_key), row)

    def put_derived_results(self, rows: list[DerivedResult]) -> None:
        for row in rows:
            self.derived[(row.kind, row.revision_id, row.scope_key)] = row

    def derived_results(self, kind: str, revision_ids: list[str]) -> dict[tuple[str, str], tuple[str, dict[str, Any]]]:
        return {
            (revision, scope): (row.stamp, dict(row.payload))
            for (row_kind, revision, scope), row in self.derived.items()
            if row_kind == kind and revision in revision_ids
        }

    def get_evidence(self, evidence_id: str) -> Any:
        return self.evidence.get(evidence_id)

    def read_evidence_artifact(self, evidence: Any) -> bytes | None:
        return self.artifacts.get(evidence.id)


_FIRST = SimpleNamespace(id="rr-1", review_id="rev-1", revision_number=1, created_at="2026-10-01T10:00:00Z")
_SECOND = SimpleNamespace(id="rr-2", review_id="rev-1", revision_number=2, created_at="2026-10-01T11:00:00Z")
_THIRD = SimpleNamespace(id="rr-3", review_id="rev-1", revision_number=3, created_at="2026-10-01T12:00:00Z")


_PYTHON = ExecutableIdentity("python3", "/usr/bin/python3.13", "Python 3.13.1", "sha256:" + "a" * 64)


def _execution(
    *,
    image_digest: str = "sha256:img",
    toolchain: tuple[tuple[str, str], ...] = (("python3", "Python 3.13"),),
    sandbox: str = "bubblewrap",
    kernel: str = "6.8.0-49-generic",
    executables: tuple[ExecutableIdentity, ...] = (_PYTHON,),
    **overrides: Any,
) -> Any:
    """A recorded run in a fully known executor environment (fingerprint version 2), with overrides."""

    from tests.gateway.test_review_execution_contract import _result

    environment = EnvironmentFingerprint(
        image_digest=image_digest,
        toolchain=toolchain,
        sandbox=sandbox,
        version=EXECUTOR_FINGERPRINT_VERSION,
        kernel=kernel,
        executables=executables,
    )
    return _result(environment=environment, **overrides)


_ENV = _execution().fingerprint.executor_digest
_KEY = CarryKey(
    recipe_digest="sha256:recipe",
    policy_digest="sha256:policy",
    executor_class="author_machine",
    scope_digest="sha256:scope",
    attempt=1,
    input_digest="sha256:inputs",
    check="unit",
    environment=_ENV,
)


def _index_run(
    store: _CarryStore, revision: Any, key: CarryKey, evidence_id: str, *, execution: dict[str, Any] | None
) -> None:
    data: dict[str, Any] = {
        "recipe_digest": key.recipe_digest,
        "attempt": key.attempt,
        "input_digest": key.input_digest,
    }
    if execution is not None:
        data["execution"] = execution
    record = {"status": "PASS", "duration_ms": 4000, "data": data}
    payload = json.dumps(record).encode("utf-8")
    evidence = SimpleNamespace(
        id=evidence_id,
        review_id="rev-1",
        revision_id=revision.id,
        mime_type=RUNTIME_VALIDATION_MEDIA_TYPE,
        content_hash=hashlib.sha256(payload).hexdigest(),
    )
    store.evidence[evidence.id] = evidence
    store.artifacts[evidence.id] = payload
    assert record_carry(
        store, revision=revision, key=key, evidence=evidence, record=record, side="new", variant="run", overlay=False
    )


def _ran_on_first(*keys: CarryKey, legacy: bool = False, execution: bool = True) -> _CarryStore:
    """Snapshot 1 ran each key's attempt (default: ``_KEY``) in ``_ENV`` and indexed it for carry."""

    store = _CarryStore(revisions=[_FIRST, _SECOND])
    for index, key in enumerate(keys or (_KEY,)):
        wire = _execution().to_wire() if execution else None
        _index_run(store, _FIRST, key, "ev-run" if index == 0 else f"ev-run-{index}", execution=wire)
    if legacy:
        for row in store.derived.values():
            row.payload.pop("key")
    return store


def test_a_check_carries_only_with_the_exact_key() -> None:
    store = _ran_on_first()
    decision = explain_carry(store, _SECOND, _KEY)
    assert decision.carried is not None and decision.reasons == ("carry_exact_key",)
    assert find_carry(store, _SECOND, _KEY) == decision.carried
    explained = carry_result(decision, subject_ref="check:unit#1", current_revision=_SECOND.id)
    assert (explained.state, explained.source_revision, explained.evidence_refs) == ("reused", "rr-1", ("ev-run",))


_COMPONENT_CHANGES: dict[str, dict[str, Any]] = {
    "carry_policy_changed": {"policy_digest": "sha256:tightened"},
    "carry_recipe_changed": {"recipe_digest": "sha256:new-recipe"},
    "carry_executor_changed": {"executor_class": "managed_sandbox"},
    "carry_scope_changed": {"scope_digest": "sha256:narrowed"},
    "carry_attempt_differs": {"attempt": 2},
    "carry_inputs_changed": {"input_digest": "sha256:edited"},
}


@pytest.mark.parametrize("code", sorted(_COMPONENT_CHANGES))
def test_each_changed_carry_key_component_explains_the_refusal(code: str) -> None:
    store = _ran_on_first()
    key = replace(_KEY, **_COMPONENT_CHANGES[code])
    decision = explain_carry(store, _SECOND, key)
    assert decision.carried is None and find_carry(store, _SECOND, key) is None
    assert decision.reasons == (code,)
    assert carry_result(decision, subject_ref="check:unit#1").state == "reverify"


def test_several_changed_components_explain_in_a_fixed_order() -> None:
    key = replace(_KEY, input_digest="sha256:edited", policy_digest="sha256:tightened")
    explained = carry_result(explain_carry(_ran_on_first(), _SECOND, key), subject_ref="check:unit#1")
    assert explained.chain == ("carry_policy_changed", "carry_inputs_changed")


def test_a_miss_is_explained_against_the_same_attempt_first() -> None:
    # Snapshot 1 ran attempt 1 on today's inputs and attempt 2 on older ones. Attempt 2
    # now differs from its own earlier run by inputs, not from attempt 1 by attempt.
    store = _ran_on_first(_KEY, replace(_KEY, attempt=2, input_digest="sha256:older"))
    assert explain_carry(store, _SECOND, replace(_KEY, attempt=2)).reasons == ("carry_inputs_changed",)


def test_a_carry_miss_without_a_comparable_earlier_run_says_so() -> None:
    assert explain_carry(_ran_on_first(), _FIRST, _KEY).reasons == ("carry_no_earlier_snapshot",)
    assert explain_carry(_ran_on_first(), _SECOND, replace(_KEY, input_digest="")).reasons == ("carry_no_input_digest",)
    empty = _CarryStore(revisions=[_FIRST, _SECOND])
    assert explain_carry(empty, _SECOND, _KEY).reasons == ("carry_no_prior_run",)
    # Another check's run never explains this one.
    assert explain_carry(_ran_on_first(), _SECOND, replace(_KEY, check="lint", input_digest="x")).reasons == (
        "carry_no_prior_run",
    )
    # A row written before carry keys were recorded cannot name the component.
    legacy = _ran_on_first(legacy=True)
    assert explain_carry(legacy, _SECOND, replace(_KEY, input_digest="sha256:edited")).reasons == (
        "carry_prior_key_unrecorded",
    )
    assert explain_carry(legacy, _SECOND, _KEY).reasons == ("carry_exact_key",)


def test_an_exact_key_whose_evidence_does_not_verify_is_not_carried() -> None:
    store = _ran_on_first()
    store.artifacts["ev-run"] = b'{"status": "PASS", "tampered": true}'
    decision = explain_carry(store, _SECOND, _KEY)
    assert decision.carried is None and decision.reasons == ("carry_evidence_unverifiable",)


def test_carry_needs_the_current_executor_environment_known_and_equal_to_the_earlier_runs() -> None:
    """Trust T3 item 1: the earlier run's own execution record decides where it ran."""

    # Unknown before the run (a hosted worker reports it only with its result).
    decision = explain_carry(_ran_on_first(), _SECOND, replace(_KEY, environment=""))
    assert decision.carried is None and decision.reasons == ("carry_environment_unknown",)
    assert carry_result(decision, subject_ref="check:unit#1").state == "reverify"
    # Another image, kernel, executable (path, version or bytes), toolchain version or sandbox:
    # the run executes again.
    for elsewhere in (
        _execution(image_digest="sha256:other-image"),
        _execution(kernel="6.8.0-50-generic"),
        _execution(executables=(replace(_PYTHON, version="Python 3.13.2"),)),
        _execution(executables=(replace(_PYTHON, sha256="sha256:" + "b" * 64),)),
        _execution(executables=(replace(_PYTHON, path="/usr/local/bin/python3.13"),)),
        _execution(executables=(_PYTHON, ExecutableIdentity("node", "/usr/bin/node", "v22", "sha256:" + "c" * 64))),
        _execution(toolchain=(("python3", "Python 3.14"),)),
        _execution(toolchain=(("node", "v22"), ("python3", "Python 3.13"))),
        _execution(sandbox="seatbelt"),
    ):
        key = replace(_KEY, environment=elsewhere.fingerprint.executor_digest)
        assert key.environment and key.environment != _ENV
        decision = explain_carry(_ran_on_first(), _SECOND, key)
        assert decision.carried is None and decision.reasons == ("carry_environment_changed",)
    # An earlier run that recorded no typed execution cannot show where it ran.
    decision = explain_carry(_ran_on_first(execution=False), _SECOND, _KEY)
    assert decision.carried is None and decision.reasons == ("carry_environment_unknown",)
    # The recipe and lockfile parts of a fingerprint belong to the carry key, not the environment.
    other_recipe = _execution(recipe=ExecutionRecipe(command=("python3", "other.py")), lockfile_digest="sha256:lock")
    assert other_recipe.fingerprint.executor_digest == _ENV


_UNKNOWN_COMPONENTS: dict[str, dict[str, Any]] = {
    "image-empty": {"image_digest": ""},
    "image-partial": {"image_digest": "host-partial:sha256:abc"},
    "kernel": {"kernel": ""},
    "sandbox": {"sandbox": ""},
    "no-executables": {"executables": ()},
    "executable-unresolved": {"executables": (ExecutableIdentity("pyrun"),)},
    "executable-unversioned": {"executables": (replace(_PYTHON, version=""),)},
    "executable-version-unavailable": {"executables": (replace(_PYTHON, version="unavailable"),)},
    "executable-version-unknown": {"executables": (replace(_PYTHON, version="unknown"),)},
    "executable-undigested": {"executables": (replace(_PYTHON, sha256=""),)},
    "toolchain-unavailable": {"toolchain": (("python3", "unavailable"),)},
    "toolchain-absent": {"toolchain": (("node", "absent"), ("python3", "Python 3.13"))},
}


@pytest.mark.parametrize("component", sorted(_UNKNOWN_COMPONENTS))
def test_an_unknown_environment_component_never_compares_equal(component: str) -> None:
    """Trust T3 item 1 fix: any unknown component makes the whole environment unknown, even against itself."""

    unknown = _execution(**_UNKNOWN_COMPONENTS[component])
    assert unknown.fingerprint.executor_digest == ""
    # Recorded and current alike unknown: never a carry, whatever else matches.
    store = _CarryStore(revisions=[_FIRST, _SECOND])
    _index_run(store, _FIRST, _KEY, "ev-unknown", execution=unknown.to_wire())
    current = replace(_KEY, environment=unknown.fingerprint.executor_digest)
    assert explain_carry(store, _SECOND, current).reasons == ("carry_environment_unknown",)
    # A known current environment against an unknown recorded one: still unknown.
    assert explain_carry(store, _SECOND, _KEY).reasons == ("carry_environment_unknown",)


def test_a_version_one_fingerprint_never_matches_a_version_two_environment() -> None:
    from tests.gateway.test_review_execution_contract import _result

    old = _result()  # the version-1 fingerprint: image, toolchain and sandbox only
    assert old.fingerprint.version == 1 and old.fingerprint.executor_digest == ""
    assert ExecutionResult.from_wire(old.to_wire()).identity == old.identity
    store = _CarryStore(revisions=[_FIRST, _SECOND])
    _index_run(store, _FIRST, _KEY, "ev-v1", execution=old.to_wire())
    assert explain_carry(store, _SECOND, _KEY).reasons == ("carry_environment_unknown",)
    # A version-2 record round-trips exactly, so its identity still verifies.
    current = _execution()
    assert ExecutionResult.from_wire(current.to_wire()).identity == current.identity
    assert ExecutionResult.from_wire(current.to_wire()).fingerprint == current.fingerprint


def test_the_carry_kill_switch_carries_nothing_reads_no_rows_and_turns_back_on_without_a_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate T-J item 2: ``off``, or any value but ``on``, carries nothing (fail-safe)."""

    store = _ran_on_first()

    def unread(*_args: Any) -> Any:
        raise AssertionError("a disabled carry read a carry row")

    for value in ("off", "OFF", " off ", "0", "false", "of", "enabled"):
        monkeypatch.setenv(CARRY_FLAG, value)
        assert not carry_enabled()
        with monkeypatch.context() as patched:
            patched.setattr(store, "derived_results", unread)
            patched.setattr(store, "list_revisions", unread)
            # The switch explains itself ahead of every other refusal.
            for revision, key in ((_FIRST, _KEY), (_SECOND, replace(_KEY, input_digest="")), (_SECOND, _KEY)):
                decision = explain_carry(store, revision, key)
                assert decision.carried is None and decision.reasons == ("carry_disabled",)
            assert find_carry(store, _SECOND, _KEY) is None
        explained = carry_result(decision, subject_ref="check:unit#1", current_revision=_SECOND.id)
        assert (explained.state, explained.source_revision, explained.evidence_refs) == ("reverify", "", ())
    # Back on: the rows recorded before carry was turned off carry again, unchanged.
    for value in ("on", " On ", ""):
        monkeypatch.setenv(CARRY_FLAG, value)
        assert carry_enabled() and explain_carry(store, _SECOND, _KEY).reasons == ("carry_exact_key",)
    monkeypatch.delenv(CARRY_FLAG)
    assert carry_enabled() and find_carry(store, _SECOND, _KEY) is not None


def test_an_older_run_in_the_current_environment_still_carries() -> None:
    # Snapshot 1 ran in today's environment; Snapshot 2 ran the same key on another image.
    store = _CarryStore(revisions=[_FIRST, _SECOND, _THIRD])
    _index_run(store, _FIRST, _KEY, "ev-one", execution=_execution().to_wire())
    _index_run(store, _SECOND, _KEY, "ev-two", execution=_execution(image_digest="sha256:other").to_wire())
    decision = explain_carry(store, _THIRD, _KEY)
    assert decision.carried is not None and decision.evidence_id == "ev-one"
    assert decision.origin_revision_id == _FIRST.id


# --------------------------------------------------------------------------- #
# typed check equivalence (check_ingest.explain_equivalent)
# --------------------------------------------------------------------------- #

HEAD = "a" * 40
_REVISION = ReviewRevision(
    id="rr-1",
    review_id="rev-1",
    revision_number=1,
    range_mode="commit_range",
    tree_fingerprint="tree",
    packet_schema_version=2,
    head_sha=HEAD,
)
_REQUIRED = EquivalenceKey("github:actions:unit-tests", "checks.test", HEAD)


def _typed(*, status: str = "PASS", klass: str = "provider_ci", head: str = HEAD, scope: str = "") -> ReviewEvidence:
    return ReviewEvidence(
        id=f"ev-{klass}-{status}-{head[:4]}",
        review_id="rev-1",
        revision_id=_REVISION.id,
        kind="verification",
        title="unit tests",
        source="ci",
        verification_status=status,
        created_at="2026-10-01T10:00:00Z",
        attestation_class=klass,
        stable_key="unit-tests",
        provider="github",
        integration_id="actions",
        provider_revision=head,
        attestation_scope=scope,
    )


def test_each_equivalence_rule_names_itself() -> None:
    match, reasons = explain_equivalent([_typed()], _REVISION, _REQUIRED)
    assert match is not None and reasons == ()
    assert equivalence_result(reasons, subject_ref="entry:checks.test", evidence_id=match.id).state == "reused"
    assert find_equivalent([_typed()], _REVISION, _REQUIRED) == match

    pinned = replace(_REQUIRED, config_digest="sha256:cfg", recipe_digest="sha256:recipe")
    cases = {
        ("snapshot_not_reusable",): explain_equivalent([_typed()], replace(_REVISION, dirty=True), _REQUIRED),
        ("equivalence_head_changed",): explain_equivalent([_typed()], _REVISION, replace(_REQUIRED, head_sha="c" * 40)),
        ("no_current_result",): explain_equivalent([], _REVISION, _REQUIRED),
        ("result_not_completed",): explain_equivalent([_typed(status="NOT_RUN")], _REVISION, _REQUIRED),
        ("attestation_class_insufficient",): explain_equivalent(
            [_typed(klass="trusted_security_feed")], _REVISION, replace(_REQUIRED, tool="github:actions:unit-tests")
        ),
        ("equivalence_recipe_digest_differs", "equivalence_config_differs"): explain_equivalent(
            [_typed()], _REVISION, pinned
        ),
    }
    for expected, (found, reasons) in cases.items():
        assert found is None and reasons == expected, expected
        explained = equivalence_result(reasons, subject_ref="entry:checks.test")
        assert explained.chain == expected
        assert explained.state == REASONS[expected[0]].state


def test_the_ci_equivalence_kill_switch_lets_no_result_stand_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate T-J item 2: ``off``, or any value but ``on``, satisfies nothing (fail-safe)."""

    for value in ("off", "OFF", "0", "false", "enabled"):
        monkeypatch.setenv(CI_EQUIVALENCE_FLAG, value)
        assert not ci_equivalence_enabled()
        # The switch explains itself ahead of every other refusal.
        for revision in (_REVISION, replace(_REVISION, dirty=True)):
            assert explain_equivalent([_typed()], revision, _REQUIRED) == (None, ("equivalence_disabled",))
        assert find_equivalent([_typed()], _REVISION, _REQUIRED) is None
        explained = equivalence_result(("equivalence_disabled",), subject_ref="entry:checks.test")
        assert explained.state == "reverify"
    for value in ("on", " On ", ""):
        monkeypatch.setenv(CI_EQUIVALENCE_FLAG, value)
        assert ci_equivalence_enabled() and find_equivalent([_typed()], _REVISION, _REQUIRED) is not None


# --------------------------------------------------------------------------- #
# readiness conditions
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
    return lambda _inputs: conditions


def test_a_policy_generation_change_re_evaluates_readiness_and_says_why() -> None:
    older = PolicyDecisionFacts(landing_generation="sha256:older", approval_generations=("sha256:older",))
    unmet = ReadinessCondition(id="security", label="Security review", state="pending", detail="required.")
    met = replace(unmet, state="pass", detail="done.")

    def evaluate(condition: ReadinessCondition) -> ReadinessCondition:
        registry = ConditionRegistry((RegisteredCondition("security", 1, _fixed(condition)),))
        outcome = registry.evaluate(_inputs(facts={"policy_decisions": older}))
        return next(item for item in outcome.conditions if item.id == "policy-generation")

    [blocked] = condition_results(evaluate(unmet), current_revision="rr-1")
    assert (blocked.reason_code, blocked.state, blocked.subject_ref) == (
        "policy_generation_changed",
        "reverify",
        "condition:policy-generation",
    )
    [kept] = condition_results(evaluate(met))
    assert (kept.reason_code, kept.state) == ("policy_generation_revalidated", "preserved")

    # Decisions under the current generation: nothing to re-evaluate, nothing to explain.
    current = ConditionRegistry((RegisteredCondition("security", 1, _fixed(met)),)).evaluate(_inputs())
    assert all(not condition_results(item) for item in current.conditions)


def test_a_pass_on_expired_or_missing_evidence_names_each_cited_row() -> None:
    scan = _evidence("vulnerability_scan", created_at=format_timestamp(NOW - timedelta(hours=30)))
    sbom = _evidence("sbom", created_at=format_timestamp(NOW - timedelta(days=400)))
    assessed = {item.id: assess_freshness(item, now=NOW) for item in (scan, sbom)}
    cites = ReadinessCondition(
        id="supply-chain",
        label="Supply chain",
        state="pass",
        detail="passed.",
        evidence_refs=tuple(EvidenceRef(evidence_id=value) for value in (sbom.id, scan.id, "ev-gone")),
    )
    [evaluated] = (
        ConditionRegistry((RegisteredCondition("supply", 1, _fixed(cites)),))
        .evaluate(_inputs(evidence=assessed))
        .conditions
    )
    assert (evaluated.state, evaluated.reason) == ("unknown", "evidence_expired")

    head, *rows = condition_results(evaluated)
    assert head.reason_code == "condition_evidence_expired" and head.state == "expired"
    assert head.chain == ("condition_evidence_expired", "evidence_not_on_snapshot", "observation_expired")
    assert set(head.evidence_refs) == {scan.id, "ev-gone"}
    assert [(row.subject_ref, row.reason_code) for row in rows] == [
        (f"evidence:{sbom.id}", "snapshot_bound"),
        (f"evidence:{scan.id}", "observation_expired"),
        ("evidence:ev-gone", "evidence_not_on_snapshot"),
    ]


def test_the_resolved_reason_code_never_moves_the_conditions_digest() -> None:
    ref = EvidenceRef(evidence_id="ev-1", state="current")
    assert ref.to_wire() == replace(ref, reason_code="observation_current").to_wire()
    assert "reason_code" not in ref.to_wire()
    unresolved = ReadinessCondition(id="x", label="X", state="pass", detail="ok", evidence_refs=(ref,))
    assert condition_results(unresolved) == ()
