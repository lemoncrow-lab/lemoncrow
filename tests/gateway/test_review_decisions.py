"""Phase 3b: HumanDecision events, fingerprints, validity and queue.

Spec: docs/planning/2026-09-28-evidence-backed-review-design.md §5, §7, §12.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pygit2
import pytest

from lemoncrow.pro.capabilities.review.contracts import UNAVAILABLE, contract_fingerprint
from lemoncrow.pro.capabilities.review.decisions import (
    DecisionImplementation,
    DecisionScope,
    canonical_json,
    check_outcomes,
    compare,
    decision_actions,
    decision_event,
    decision_reasons,
    derive_decision_states,
    implementation_document,
    implementation_fingerprint,
    scope_document,
    semantic_fingerprint,
)
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.guidance import (
    GuidanceConflict,
    GuidanceDecision,
    GuidanceDefinition,
    GuidanceOption,
    GuidanceTargetRef,
    GuidanceView,
    RefResolution,
)
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.session_models import (
    Annotation,
    AnnotationAnchor,
    ReviewDecisionEvent,
    ReviewUnit,
)
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.targets import ReviewTarget, TargetSpan


def _git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src/store.py").write_text("def prune():\n    return []\n", encoding="utf-8")
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.invalid"
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    signature = pygit2.Signature("Fixture Tester", "fixture@example.invalid", 1700000000, 0)
    repo.create_commit("HEAD", signature, signature, "base", tree, [])
    (root / "src/store.py").write_text("def prune():\n    return [1]\n", encoding="utf-8")
    return root


def _capture(store_root: Path, repo_root: Path) -> tuple[ReviewStore, Any, Any]:
    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, working_tree=True)
    build = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, repo_root, rng, title="decisions")
    revision = snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)
    return (store, session, revision)


def _event(
    session: Any, revision: Any, action: str, disposition: str, *, key: str = "retention"
) -> ReviewDecisionEvent:
    return ReviewDecisionEvent(
        id="",
        review_id=session.id,
        decision_key=key,
        reviewer_id=session.reviewer_id,
        revision_id=revision.id,
        action=action,
        disposition=disposition,
        option_key="indefinite" if action == "accept" else "",
        semantic_fingerprint="sem",
        implementation_fingerprint="impl",
    )


def test_decision_events_append_in_order_and_cascade(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    store, session, revision = _capture(tmp_path / "store", repo)
    first = store.append_decision_event(_event(session, revision, "accept", "accepted"), expected_prior_event_id="")
    second = store.append_decision_event(
        _event(session, revision, "defer", "deferred"), expected_prior_event_id=first.id
    )
    other = store.append_decision_event(
        _event(session, revision, "accept", "accepted", key="other"), expected_prior_event_id=""
    )
    assert first.id and first.created_at and (second.prior_event_id == first.id)
    assert second.created_at > first.created_at
    assert [event.id for event in store.list_decision_events(session.id, decision_key="retention")] == [
        first.id,
        second.id,
    ]
    assert [event.id for event in store.list_decision_events(session.id)] == [first.id, second.id, other.id]
    assert store.list_decision_events(session.id, reviewer_id="someone-else") == ()
    assert store.list_decision_events(session.id, decision_key="retention")[0] == first
    with store._transaction() as conn:
        conn.execute("DELETE FROM review_sessions WHERE id = ?", (session.id,))
    assert store.list_decision_events(session.id) == ()


def test_decision_event_rejects_stale_prior_and_stale_snapshot(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    store_root = tmp_path / "store"
    store, session, revision = _capture(store_root, repo)
    first = store.append_decision_event(_event(session, revision, "accept", "accepted"), expected_prior_event_id="")
    with pytest.raises(ValueError, match="changed since"):
        store.append_decision_event(_event(session, revision, "defer", "deferred"), expected_prior_event_id="")
    (repo / "src/store.py").write_text("def prune():\n    return [2]\n", encoding="utf-8")
    _, _, newer = _capture(store_root, repo)
    assert newer.id != revision.id
    with pytest.raises(ValueError, match="Revision"):
        store.append_decision_event(_event(session, revision, "reopen", "open"), expected_prior_event_id=first.id)
    assert [event.id for event in store.list_decision_events(session.id)] == [first.id]


def _scope(**overrides: Any) -> DecisionScope:
    base: dict[str, Any] = {
        "decision_key": "retention",
        "selected_option_key": "indefinite",
        "positions": (),
        "assumptions": (("branch-stability", "stable-within-session"),),
        "contracts": (("src/store.py::prune", "sha256:a"),),
        "checks": (("browser:checkout", "PASS"),),
    }
    base.update(overrides)
    return DecisionScope(**base)


def _impl(**targets: str) -> DecisionImplementation:
    return DecisionImplementation(targets=tuple(sorted((targets or {"sym:src/store.py:prune": "fp-1"}).items())))


def _judge(
    scope: DecisionScope, impl: DecisionImplementation, *, then: DecisionScope, then_impl: DecisionImplementation
) -> Any:
    return compare(scope_document(then), implementation_document(then_impl), scope, impl)


def test_decision_fingerprint_and_lineage_contract() -> None:
    """case: test_rewording_changes_nothing"""
    from lemoncrow.pro.capabilities.review.decisions import scope_from_decision
    from lemoncrow.pro.capabilities.review.guidance import GuidanceAssumption, GuidanceDecision, GuidanceOption

    first = GuidanceDecision(
        key="retention",
        question="How long should checkpoints be retained?",
        options=(GuidanceOption("indefinite", "Retain indefinitely"), GuidanceOption("ninety", "90 days")),
        selected_option="indefinite",
        reason_human_required="Unspecified requirement.",
        assumptions=(GuidanceAssumption("branch-stability", "stable-within-session", "Branch names are stable."),),
    )
    reworded = GuidanceDecision(
        key="retention",
        question="What retention policy applies to committed checkpoints?",
        options=(GuidanceOption("indefinite", "Keep forever"), GuidanceOption("ninety", "Expire after ninety days")),
        selected_option="indefinite",
        reason_human_required="The product brief is silent.",
        assumptions=(GuidanceAssumption("branch-stability", "stable-within-session", "A branch keeps its name."),),
    )
    one = scope_from_decision(first, contracts={}, checks=())
    two = scope_from_decision(reworded, contracts={}, checks=())
    assert semantic_fingerprint(one) == semantic_fingerprint(two)
    assert compare(scope_document(one), implementation_document(_impl()), two, _impl()) == ("current", ())
    "case: test_option_assumption_contract_and_outcome_changes_need_reconsideration"
    accepted = _scope()
    for changed, needle in (
        (_scope(selected_option_key="ninety"), "option"),
        (_scope(assumptions=(("branch-stability", "may-change"),)), "branch-stability"),
        (_scope(contracts=(("src/store.py::prune", "sha256:b"),)), "src/store.py::prune"),
        (_scope(checks=(("browser:checkout", "FAIL"),)), "browser:checkout"),
    ):
        validity, inputs = _judge(changed, _impl(), then=accepted, then_impl=_impl())
        assert validity == "needs_reconsideration"
        assert any(needle in line for line in inputs), inputs
    "case: test_newer_passing_run_and_missing_run_change_nothing"
    for before, after in (("PASS", "PASS"), ("PASS", "unobserved"), ("unobserved", "PASS"), ("FAIL", "unobserved")):
        validity, _ = _judge(
            _scope(checks=(("browser:checkout", after),)),
            _impl(),
            then=_scope(checks=(("browser:checkout", before),)),
            then_impl=_impl(),
        )
        assert validity == "current", (before, after)
    "case: test_body_only_change_is_implementation_changed"
    validity, inputs = _judge(_scope(), _impl(**{"sym:src/store.py:prune": "fp-2"}), then=_scope(), then_impl=_impl())
    assert validity == "implementation_changed_since_acceptance"
    assert inputs and "sym:src/store.py:prune" in inputs[0]
    "case: test_unavailable_contract_is_unverified_never_reconsideration"
    unavailable = _scope(contracts=(("src/store.py::prune", UNAVAILABLE),))
    assert _judge(unavailable, _impl(), then=_scope(), then_impl=_impl())[0] == "unverified"
    also_option = _scope(contracts=(("src/store.py::prune", UNAVAILABLE),), selected_option_key="ninety")
    assert _judge(also_option, _impl(), then=_scope(), then_impl=_impl())[0] == "needs_reconsideration"
    body_unknown = _impl(**{"sym:src/store.py:prune": UNAVAILABLE})
    assert _judge(_scope(), body_unknown, then=_scope(), then_impl=_impl())[0] == "unverified"
    "case: test_target_set_change_is_ambiguous_lineage"
    renamed = _impl(**{"sym:src/store.py:prune_all": "fp-9"})
    validity, inputs = _judge(_scope(), renamed, then=_scope(), then_impl=_impl())
    assert validity == "needs_reconsideration"
    assert any("implementation targets" in line for line in inputs)
    more = _impl(**{"sym:src/store.py:prune": "fp-1", "sym:src/store.py:keep": "fp-9"})
    validity, inputs = _judge(_scope(), more, then=_scope(), then_impl=_impl())
    assert validity == "implementation_changed_since_acceptance"
    assert any("sym:src/store.py:keep" in line for line in inputs)
    "case: test_a_moved_target_with_identical_content_keeps_its_acceptance"
    moved = _impl(**{"sym:src/stores.py:prune": "fp-1"})
    assert _judge(_scope(), moved, then=_scope(), then_impl=_impl()) == ("current", ())
    moved_and_grown = _impl(**{"sym:src/stores.py:prune": "fp-1", "sym:src/stores.py:keep": "fp-7"})
    validity, inputs = _judge(_scope(), moved_and_grown, then=_scope(), then_impl=_impl())
    assert validity == "implementation_changed_since_acceptance"
    assert any("sym:src/stores.py:keep" in line for line in inputs)
    "case: test_two_identical_candidates_stay_ambiguous"
    twins = _impl(**{"sym:src/a.py:prune": "fp-1", "sym:src/b.py:prune": "fp-1"})
    validity, inputs = _judge(_scope(), twins, then=_scope(), then_impl=_impl())
    assert validity == "needs_reconsideration"
    assert any("lineage is ambiguous" in line for line in inputs)
    "case: test_a_moved_and_edited_target_stays_ambiguous"
    moved = _impl(**{"sym:src/stores.py:prune": "fp-2"})
    assert _judge(_scope(), moved, then=_scope(), then_impl=_impl())[0] == "needs_reconsideration"
    unknown = _impl(**{"sym:src/stores.py:prune": UNAVAILABLE})
    blind = compare(
        scope_document(_scope()),
        implementation_document(_impl(**{"sym:src/store.py:prune": UNAVAILABLE})),
        _scope(),
        unknown,
    )
    assert blind[0] == "needs_reconsideration", "an unanalyzable fingerprint never proves identity"
    "case: test_lineage_never_rebinds_onto_a_copy_elsewhere_in_the_snapshot"
    accepted = implementation_document(_impl())

    def judge(now: Any, snapshot: dict[str, str]) -> Any:
        return compare(scope_document(_scope()), accepted, _scope(), now, snapshot_units=snapshot)

    both = {"sym:src/store.py:prune": "fp-1", "sym:src/b.py:prune": "fp-1"}
    assert judge(_impl(**{"sym:src/b.py:prune": "fp-1"}), both)[0] == "needs_reconsideration"
    copies = {"sym:src/a.py:prune": "fp-1", "sym:src/b.py:prune": "fp-1"}
    assert judge(_impl(**{"sym:src/a.py:prune": "fp-1"}), copies)[0] == "needs_reconsideration"
    assert judge(_impl(**{"sym:src/stores.py:prune": "fp-1"}), {"sym:src/stores.py:prune": "fp-1"}) == ("current", ())
    "case: test_fingerprints_exclude_display_text"
    raw = canonical_json(scope_document(_scope()))
    assert raw == canonical_json(scope_document(_scope()))
    assert " " not in raw.replace("stable-within-session", "")
    assert semantic_fingerprint(_scope()).startswith("sha256:")
    assert implementation_fingerprint(_impl()).startswith("sha256:")
    assert '"v":1' in raw and '"positions"' not in raw
    assert '"positions"' in canonical_json(scope_document(_scope(positions=("a", "b"))))
    "case: test_contract_fingerprint_signature_not_body"
    files = {
        "a.py": "class Store:\n    def prune(self, a):\n        return 1\n\ndef f(a):\n    return 1\n",
        "b.py": "class Store:\n    def prune(self, a):\n        return 2\n\ndef f(a):\n    return 2\n",
        "c.py": "class Store:\n    def prune(self, a, b):\n        return 1\n\ndef f(a, b):\n    return 1\n",
        "a.go": "package p\n\nfunc F(a int) int {\n\treturn 1\n}\n",
        "b.go": "package p\n\nfunc F(a int) int {\n\treturn 2\n}\n",
        "c.go": "package p\n\nfunc F(a int, b int) int {\n\treturn 1\n}\n",
        "a.ts": "export interface Shape { id: string }\n",
        "b.ts": "export interface Shape { id: string; name: string }\n",
    }
    read = files.get

    def fp(ref: str) -> str:
        return contract_fingerprint(ref, read)

    assert fp("a.py::f") == fp("b.py::f") != fp("c.py::f")
    assert fp("a.py::Store.prune") == fp("b.py::Store.prune") != fp("c.py::Store.prune")
    assert fp("a.py#f") == fp("a.py::f")
    assert fp("a.go::F") == fp("b.go::F") != fp("c.go::F")
    assert fp("a.ts::Shape") != fp("b.ts::Shape"), "an interface body is the contract"
    assert fp("a.py::missing") == "absent"
    assert fp("x.xyz::f") == UNAVAILABLE
    assert fp("gone.py::f") == UNAVAILABLE
    assert fp("public REST API") == "unanalyzed:public REST API"
    assert fp("a.py::f").startswith("sha256:")


def test_decision_check_lifecycle_contract() -> None:
    """case: test_relied_check_lifecycle_from_unobserved"""
    accepted = _scope(checks=(("browser:checkout", "unobserved"),))
    passed = _scope(checks=(("browser:checkout", "PASS"),))
    assert _judge(passed, _impl(), then=accepted, then_impl=_impl())[0] == "current"
    failed = _scope(checks=(("browser:checkout", "FAIL"),))
    assert _judge(failed, _impl(), then=passed, then_impl=_impl())[0] == "needs_reconsideration"
    "case: test_check_outcomes_use_last_observed_pass_or_fail"
    rows = [
        {"source_ref": "browser:checkout", "status": "PASS", "state": "stale"},
        {"source_ref": "browser:login", "status": "NOT_RUN", "state": "current"},
    ]
    assert check_outcomes(["browser:checkout", "browser:login", "browser:gone"], rows) == (
        ("browser:checkout", "PASS"),
        ("browser:gone", "unobserved"),
        ("browser:login", "unobserved"),
    )
    "case: test_relied_browser_checks_read_real_run_rows"
    rows = [
        _browser_run_row("FAIL", "current", 2, "390x844"),
        _browser_run_row("PASS", "current", 2, "1280x800"),
        _browser_run_row("PASS", "stale", 1),
    ]
    assert check_outcomes(["browser:checkout"], rows) == (("browser:checkout", "FAIL"),)
    newest_pass = [_browser_run_row("PASS", "current", 2), _browser_run_row("FAIL", "stale", 1)]
    assert check_outcomes(["browser:checkout"], newest_pass) == (("browser:checkout", "PASS"),)
    owed = {"kind": "browser", "source_ref": "browser:checkout", "status": "NOT_RUN", "state": "current"}
    assert check_outcomes(["browser:checkout"], [owed]) == (("browser:checkout", "unobserved"),)
    reported = {"name": "unit tests", "status": "FAIL", "state": "session"}
    assert check_outcomes(["unit tests"], [reported]) == (("unit tests", "unobserved"),), "agent-reported"
    "case: test_a_check_available_trigger_fires_on_a_real_browser_run"
    view = _gview(_definition(relied=("browser:checkout",)))
    (state,) = _states(view)
    waiting = _decide(state, "defer", reason="needs the browser run", trigger="check_available:browser:checkout")
    (ran,) = _states(view, (waiting,), checks=(_browser_run_row("FAIL", "current", 0),))
    assert ran.trigger_fired is True
    "case: test_unanalyzed_inputs_are_never_current"
    unanalyzed = _scope(contracts=(("src/store.py::prune", UNAVAILABLE),))
    validity, inputs = _judge(unanalyzed, _impl(), then=unanalyzed, then_impl=_impl())
    assert validity == "unverified" and "could not be analyzed on this revision" in inputs[0]
    validity, inputs = _judge(_scope(), _impl(), then=unanalyzed, then_impl=_impl())
    assert validity == "unverified" and "when it was accepted" in inputs[0]
    body = _impl(**{"sym:src/store.py:prune": UNAVAILABLE})
    assert _judge(_scope(), body, then=_scope(), then_impl=body)[0] == "unverified"
    "case: test_reconfirm_records_an_input_that_was_unanalyzed_at_acceptance"
    definition = _definition()
    assert definition.decision is not None
    definition = replace(definition, decision=replace(definition.decision, contract_scope=("src/store.py::prune",)))
    view = _gview(definition)
    (state,) = _states(view, contracts=lambda ref: UNAVAILABLE)
    accepted = _decide(state, "accept", option_key="indefinite")
    (now,) = _states(view, (accepted,), contracts=lambda ref: UNAVAILABLE)
    assert (now.validity, "reconfirm" in now.available_actions) == ("unverified", False)
    (later,) = _states(view, (accepted,), contracts=lambda ref: "sha256:c")
    assert later.validity == "unverified" and "reconfirm" in later.available_actions


def test_decision_state_contract() -> None:
    """case: test_open_decision_is_review_first_on_its_targets_and_one_action"""
    (state,) = _states(_gview(_definition()))
    assert (state.disposition, state.reason_code, state.tier, state.outstanding, state.navigable) == (
        "open",
        "decision_open",
        3,
        True,
        True,
    )
    assert set(state.available_actions) == {"accept", "request_change", "ask", "defer"}
    reasons = decision_reasons((state,))
    assert list(reasons) == ["target:a"] and reasons["target:a"][0].code == "decision_open"
    (action,) = decision_actions((state,))
    assert (action["id"], action["tier"], action["reason"], action["target_ids"]) == (
        "decision:retention",
        3,
        "decision_open",
        ["target:a"],
    )
    "case: test_accept_current_settles_and_body_change_needs_reconfirm"
    view = _gview(_definition())
    (state,) = _states(view)
    accepted = _decide(state, "accept", option_key="indefinite")
    (settled,) = _states(view, (accepted,))
    assert (settled.disposition, settled.validity, settled.outstanding, settled.reason_code) == (
        "accepted",
        "current",
        False,
        "",
    )
    assert decision_reasons((settled,)) == {} and decision_actions((settled,)) == []
    changed_units = (replace(_UNITS3[0], content_fingerprint="fp-a2"), _UNITS3[1])
    (again,) = _states(view, (accepted,), units=changed_units)
    assert (again.validity, again.reason_code, again.tier) == (
        "implementation_changed_since_acceptance",
        "decision_implementation_changed",
        2,
    )
    assert "reconfirm" in again.available_actions
    (moved,) = _states(_gview(_definition(selected="ninety")), (accepted,))
    assert (moved.validity, moved.reason_code, moved.tier) == ("needs_reconsideration", "decision_scope_changed", 2)
    assert "reconfirm" not in moved.available_actions
    "case: test_deferred_manual_leaves_next_but_stays_outstanding"
    view = _gview(_definition())
    (state,) = _states(view)
    deferred = _decide(state, "defer", reason="Waiting on product", trigger="manual")
    (after,) = _states(view, (deferred,))
    assert (after.disposition, after.outstanding, after.navigable, after.trigger_fired) == (
        "deferred",
        True,
        False,
        False,
    )
    assert decision_reasons((after,)) == {} and decision_actions((after,)) == []
    "case: test_new_snapshot_and_check_available_triggers_fire"
    view = _gview(_definition(relied=("browser:checkout",)))
    (state,) = _states(view)
    later = _decide(state, "defer", reason="after next push", trigger="new_snapshot")
    (same,) = _states(view, (later,))
    assert same.trigger_fired is False
    (fired,) = _states(view, (later,), current_revision_id="rev-2")
    assert (fired.trigger_fired, fired.reason_code, fired.navigable) == (True, "decision_deferred_trigger_fired", True)
    waiting = _decide(state, "defer", reason="needs the browser run", trigger="check_available:browser:checkout")
    assert (waiting.trigger_kind, waiting.trigger_check_key) == ("check_available", "browser:checkout")
    (idle,) = _states(
        view, (waiting,), checks=({"source_ref": "browser:checkout", "status": "NOT_RUN", "state": "current"},)
    )
    assert idle.trigger_fired is False
    (ran,) = _states(
        view, (waiting,), checks=({"source_ref": "browser:checkout", "status": "PASS", "state": "current"},)
    )
    assert ran.trigger_fired is True


def test_decision_history_and_feedback_contract() -> None:
    """case: test_a_deleted_contract_file_is_absent_not_unavailable"""
    from lemoncrow.pro.capabilities.review.contracts import ABSENT, contract_fingerprint

    assert contract_fingerprint("src/gone.py::prune", lambda path: None, deleted_paths={"src/gone.py"}) == ABSENT
    assert contract_fingerprint("src/here.py::prune", lambda path: None, deleted_paths={"src/gone.py"}) == UNAVAILABLE
    "case: test_a_decision_follows_its_moved_target"
    (state,) = _states(_gview(_definition(targets=("target:a",))))
    accepted = _decide(state, "accept", option_key="indefinite")
    moved_target = replace(_TARGETS3[0], target_id="target:a2", unit_key="sym:a-moved", path="src/stores.py")
    moved_unit = replace(_UNITS3[0], unit_key="sym:a-moved", path="src/stores.py")
    (after,) = _states(
        _gview(_definition(targets=("target:a2",))),
        (accepted,),
        targets=(moved_target, _TARGETS3[1]),
        units=(moved_unit, _UNITS3[1]),
    )
    assert (after.validity, after.outstanding, after.changed_inputs) == ("current", False, ())
    edited_unit = replace(moved_unit, content_fingerprint="fp-a9")
    (edited,) = _states(
        _gview(_definition(targets=("target:a2",))),
        (accepted,),
        targets=(moved_target, _TARGETS3[1]),
        units=(edited_unit, _UNITS3[1]),
    )
    assert edited.validity == "needs_reconsideration", "moved and edited is not provable lineage"
    (copied,) = _states(
        _gview(_definition(targets=("target:a2",))),
        (accepted,),
        targets=(moved_target, *_TARGETS3),
        units=(moved_unit, *_UNITS3),
    )
    assert copied.validity == "needs_reconsideration", "a copy is not the accepted code"
    "case: test_free_text_trigger_maps_to_manual_and_keeps_the_note"
    (state,) = _states(_gview(_definition()))
    event = _decide(state, "defer", reason="later", trigger="when legal replies")
    assert (event.trigger_kind, event.trigger_note) == ("manual", "when legal replies")
    "case: test_conflicting_producers_one_card_and_accept_settles_conflict"
    first = _definition(producer="claude:cl-1", selected="indefinite", record="gdn-1")
    second = _definition(producer="codex:cx-1", selected="ninety", record="gdn-2")
    conflict = GuidanceConflict(kind="decision", key="retention", definitions=(first, second))
    view = _gview(conflicts=(conflict,))
    (state,) = _states(view)
    assert state.conflict and [p.producer for p in state.positions] == ["claude:cl-1", "codex:cx-1"]
    assert (state.reason_code, state.tier) == ("author_guidance_conflict", 3)
    accepted = _decide(state, "accept", option_key="ninety")
    (settled,) = _states(view, (accepted,))
    assert (settled.disposition, settled.validity, settled.outstanding) == ("accepted", "current", False)
    from lemoncrow.pro.capabilities.review.review_plan import review_questions

    assert any(q.id == "q:conflict:decision:retention" for q in review_questions(_TARGETS3, view, ()))
    assert not any(
        q.id == "q:conflict:decision:retention"
        for q in review_questions(_TARGETS3, view, (), handled_decisions={"retention"})
    )
    "case: test_withdrawn_decision_is_kept_with_history"
    (state,) = _states(_gview(_definition()))
    accepted = _decide(state, "accept", option_key="indefinite")
    (withdrawn,) = _states(_gview(), (accepted,))
    assert (withdrawn.key, withdrawn.withdrawn, withdrawn.disposition, withdrawn.outstanding) == (
        "retention",
        True,
        "accepted",
        False,
    )
    assert withdrawn.events == (accepted,) and withdrawn.available_actions == ()
    "case: test_change_requested_uses_linked_request_action"
    view = _gview(_definition())
    (state,) = _states(view)
    requested = _decide(state, "request_change", feedback_annotation_id="ann-1")
    (open_request,) = _states(view, (requested,), annotations=(_annotation(),))
    assert (open_request.disposition, open_request.outstanding, open_request.reason_code) == (
        "change_requested",
        True,
        "",
    )
    assert decision_actions((open_request,)) == []
    (resolved,) = _states(view, (requested,), annotations=(_annotation("resolved"),))
    assert (resolved.reason_code, resolved.tier) == ("decision_open", 3)
    "case: test_historical_states_offer_no_writes"
    (state,) = _states(_gview(_definition()), historical=True)
    assert state.available_actions == ("ask",)
    "case: test_agent_addressed_feedback_never_accepts_a_decision"
    view = _gview(_definition())
    (state,) = _states(view)
    requested = _decide(state, "request_change", feedback_annotation_id="ann-1")
    addressed = replace(_annotation(), author_response="addressed")
    (after,) = _states(view, (requested,), annotations=(addressed,))
    assert (after.disposition, after.validity, after.outstanding) == ("change_requested", None, True)


_REV = "rev-1"


def _t(target_id: str, unit_key: str, path: str = "src/store.py") -> ReviewTarget:
    return ReviewTarget(
        target_id=target_id,
        unit_key=unit_key,
        kind="symbol",
        path=path,
        label=unit_key,
        symbol=unit_key,
        start_line=1,
        end_line=2,
        hunk_ordinals=(0,),
        spans=(TargetSpan("new", 1, 2, 0),),
        state="unreviewed",
        changed_since_mark=False,
        reviewed_revision_id="",
        attention_rank=0,
        attention_level="normal",
        reasons=(),
        additions=1,
        deletions=0,
        fingerprint_method="exact",
    )


_TARGETS3 = (_t("target:a", "sym:a"), _t("target:b", "sym:b"))
_UNITS3 = (
    ReviewUnit(
        revision_id=_REV,
        unit_key="sym:a",
        kind="symbol",
        path="src/store.py",
        content_fingerprint="fp-a",
        fingerprint_method="exact",
    ),
    ReviewUnit(
        revision_id=_REV,
        unit_key="sym:b",
        kind="symbol",
        path="src/store.py",
        content_fingerprint="fp-b",
        fingerprint_method="exact",
    ),
)


def _decision(
    key: str = "retention", *, selected: str = "indefinite", relied: tuple[str, ...] = ()
) -> GuidanceDecision:
    return GuidanceDecision(
        key=key,
        question="How long should checkpoints be retained?",
        options=(GuidanceOption("indefinite", "Forever"), GuidanceOption("ninety", "90 days")),
        selected_option=selected,
        reason_human_required="Retention is unspecified.",
        targets=(GuidanceTargetRef(path="src/store.py"),),
        relied_checks=relied,
    )


def _definition(
    key: str = "retention",
    *,
    producer: str = "claude:cl-1",
    selected: str = "indefinite",
    targets: tuple[str, ...] = ("target:a",),
    record: str = "gdn-1",
    relied: tuple[str, ...] = (),
) -> GuidanceDefinition:
    return GuidanceDefinition(
        kind="decision",
        key=key,
        record_id=record,
        producer=producer,
        attribution="exact",
        created_at="2026-09-28T00:00:00+00:00",
        stale=False,
        item=None,
        decision=_decision(key, selected=selected, relied=relied),
        resolutions=(RefResolution(GuidanceTargetRef(path="src/store.py"), "resolved", targets),),
    )


def _gview(*definitions: GuidanceDefinition, conflicts: tuple[GuidanceConflict, ...] = ()) -> GuidanceView:
    return GuidanceView(records=(), items=(), decisions=definitions, conflicts=conflicts, superseded=(), limitations=())


def _states(view: GuidanceView, events: tuple[ReviewDecisionEvent, ...] = (), **kwargs: Any) -> Any:
    params: dict[str, Any] = {
        "guidance": view,
        "targets": _TARGETS3,
        "units": _UNITS3,
        "events": events,
        "checks": (),
        "contracts": lambda ref: "sha256:c",
        "annotations": (),
        "current_revision_id": _REV,
        "historical": False,
    }
    params.update(kwargs)
    return derive_decision_states(**params)


def _decide(state: Any, action: str, *, revision_id: str = _REV, **kwargs: Any) -> ReviewDecisionEvent:
    event = decision_event(state, action=action, reviewer_id="local", revision_id=revision_id, **kwargs)
    return replace(
        event, id=f"dec-{action}-{len(kwargs)}", review_id="rev-review", created_at="2026-09-28T01:00:00+00:00"
    )


def _browser_run_row(
    status: str, state: str, snapshot: int, viewport: str = "default", observed: str = "2026-09-28T01:00:00+00:00"
) -> dict[str, Any]:
    return {
        "kind": "browser",
        "name": "Checkout",
        "source_ref": f"browser:checkout:{viewport}:new",
        "status": status,
        "state": state,
        "snapshot_number": snapshot,
        "observed_at": observed,
        "browser": {"scenario": "checkout"},
    }


def _annotation(state: str = "open") -> Annotation:
    return Annotation(
        id="ann-1",
        review_id="rev-review",
        revision_id=_REV,
        anchor=AnnotationAnchor(path="src/store.py", side="new", start_line=0, end_line=0),
        body="Keep 90 days",
        kind="request_change",
        state=state,
    )
