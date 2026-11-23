"""Phase 4: bounded, validated AI synthesis (spec §7, §8, §10)."""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pygit2

from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.guidance import build_guidance_view
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.review_plan import (
    Claim,
    ReviewQuestion,
    apply_synthesis,
    build_review_plan,
    question_reasons,
)
from lemoncrow.pro.capabilities.review.review_priority import assign_review_priority
from lemoncrow.pro.capabilities.review.session_models import ReviewSynthesisRow
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.synthesis import (
    SYNTHESIS_JSON_SCHEMA,
    SynthesisBudget,
    build_synthesis_request,
    item_input_hash,
    run_synthesis,
    synthesis_key,
    synthesis_messages,
    validate_synthesis,
)
from lemoncrow.pro.capabilities.review.targets import ReviewTarget, TargetSpan

A = "src/a.py"
B = "src/b.py"
C = "src/c.py"
SOURCES = {A: "def alpha():\n    return 1\n", B: "def beta():\n    return 2\n", C: "def gamma():\n    return 3\n"}


def _target(target_id: str, path: str, *, state: str = "unreviewed") -> ReviewTarget:
    return ReviewTarget(
        target_id=target_id,
        unit_key=f"sym:{target_id}",
        kind="symbol",
        path=path,
        label=target_id,
        symbol=target_id,
        start_line=1,
        end_line=2,
        hunk_ordinals=(0,),
        spans=(TargetSpan("new", 1, 2, 0),),
        state=state,
        changed_since_mark=False,
        reviewed_revision_id="",
        attention_rank=0,
        attention_level="normal",
        reasons=(),
        additions=2,
        deletions=0,
        fingerprint_method="exact",
    )


TARGETS = assign_review_priority((_target("t-a", A), _target("t-b", B), _target("t-c", C)))
CHAPTERS = (
    {"key": "intent:a", "label": "Alpha", "targets": [{"target_id": "t-a"}]},
    {"key": "intent:b", "label": "Beta", "targets": [{"target_id": "t-b"}]},
    {"key": "intent:c", "label": "Gamma", "targets": [{"target_id": "t-c"}]},
)
CHECKS = (
    {"name": "unit tests", "source_ref": "", "status": "FAIL", "state": "current", "snapshot_number": 0, "path": ""},
    {"name": "lint", "source_ref": "", "status": "PASS", "state": "current", "snapshot_number": 0, "path": ""},
)


def _plan(targets: Any = TARGETS) -> Any:
    view = build_guidance_view((), targets=targets, units=(), packet={"files": []})
    return build_review_plan(
        targets=targets, guidance=view, checks=CHECKS, chapters=CHAPTERS, routine_proofs={}, snapshot_number=0
    )


def _request(budget: SynthesisBudget | None = None, targets: Any = TARGETS) -> Any:
    return build_synthesis_request(_plan(targets), targets, CHECKS, SOURCES.get, budget or SynthesisBudget())


def test_review_synthesis_request_contract() -> None:
    """case: test_request_only_covers_unexplained_groups_in_queue_order"""
    request = _request()
    assert [item.item_id for item in request.items] == ["area:intent:a", "area:intent:b", "area:intent:c"]
    assert request.items[0].snippets == (("t-a", "def alpha():\n    return 1"),)
    assert {check.key for check in request.checks} == {"unit tests", "lint"}
    assert request.skipped_item_ids == () and request.truncated_target_ids == ()
    "case: test_content_access_filters_targets_before_source_is_read"
    reads: list[str] = []

    def read_new(path: str) -> str | None:
        reads.append(path)
        if path == B:
            raise AssertionError("denied target bytes must never be read for synthesis")
        return SOURCES.get(path)

    request = build_synthesis_request(
        _plan(), TARGETS, CHECKS, read_new, SynthesisBudget(), allowed_target_ids={"t-a", "t-c"}
    )
    assert B not in reads
    assert {target_id for item in request.items for target_id in item.target_ids} == {"t-a", "t-c"}
    assert "t-b" in request.skipped_target_ids
    "case: test_budget_truncation_is_reported_not_dropped"
    request = _request(SynthesisBudget(max_items=2))
    assert [item.item_id for item in request.items] == ["area:intent:a", "area:intent:b"]
    assert request.skipped_item_ids == ("area:intent:c",)
    validated = validate_synthesis({"explanations": []}, request)
    assert set(validated.unsynthesized_target_ids) == {"t-a", "t-b", "t-c"}
    tight = _request(SynthesisBudget(max_chars=30))
    assert tight.truncated_target_ids, "a snippet past the character budget is reported"
    "case: test_key_ignores_marks_and_tracks_evidence"
    request = _request()
    marked = assign_review_priority((_target("t-a", A, state="reviewed"), _target("t-b", B), _target("t-c", C)))
    assert synthesis_key(_request(targets=marked), revision_id="rev-1", model="m") == synthesis_key(
        request, revision_id="rev-1", model="m"
    )
    assert synthesis_key(request, revision_id="rev-2", model="m") != synthesis_key(
        request, revision_id="rev-1", model="m"
    )
    assert synthesis_key(request, revision_id="rev-1", model="other") != synthesis_key(
        request, revision_id="rev-1", model="m"
    )
    more = replace(request, checks=(*request.checks, replace(request.checks[0], key="e2e")))
    assert synthesis_key(more, revision_id="rev-1", model="m") != synthesis_key(request, revision_id="rev-1", model="m")
    "case: test_fabricated_references_are_rejected_individually"
    request = _request()
    validated = validate_synthesis(
        {
            "explanations": [
                {"item_id": "area:intent:a", "summary": "Alpha now returns one.", "target_ids": ["t-a"]},
                {"item_id": "area:intent:b", "summary": "Beta.", "target_ids": ["t-ghost"]},
                {"item_id": "area:nope", "summary": "Invented.", "target_ids": ["t-c"]},
            ],
            "relevance": [{"claim_id": "claim:nope", "check_key": "unit tests", "why": "x"}],
            "questions": [
                {"kind": "author_observation_mismatch", "title": "Q", "detail": "D", "target_ids": ["t-zzz"]}
            ],
        },
        request,
    )
    assert list(validated.explanations) == ["area:intent:a"]
    assert validated.explanations["area:intent:a"].target_ids == ("t-a",)
    assert len(validated.rejected) == 4
    assert validated.questions == ()
    assert set(validated.unsynthesized_target_ids) == {"t-b", "t-c"}
    "case: test_model_cannot_assert_check_results"
    request = _request()
    validated = validate_synthesis(
        {
            "explanations": [
                {
                    "item_id": "area:intent:a",
                    "summary": "Alpha changed and the unit tests passed.",
                    "target_ids": ["t-a"],
                },
                {"item_id": "area:intent:b", "summary": "Beta returns two now.", "target_ids": ["t-b"]},
            ]
        },
        request,
    )
    assert list(validated.explanations) == ["area:intent:b"]
    assert any("check result" in reason for reason in validated.rejected)


def test_review_synthesis_question_contract() -> None:
    """case: test_planner_decisions_become_questions_at_tier_three"""
    request = _request()
    validated = validate_synthesis(
        {
            "questions": [
                {
                    "kind": "implementation_choice_unrecorded",
                    "title": "Retention appears indefinite",
                    "detail": "The implementation appears to keep checkpoints forever; guidance records no such decision.",
                    "target_ids": ["t-b"],
                    "tier": 1,
                },
                {"kind": "accept_this", "title": "x", "detail": "y", "target_ids": ["t-b"]},
            ],
            "decisions": [{"question": "Keep forever?", "target_ids": ["t-c"]}],
        },
        request,
    )
    assert [(q.reason, q.tier, q.target_ids) for q in validated.questions] == [
        ("implementation_choice_unrecorded", 3, ("t-b",)),
        ("implementation_choice_unrecorded", 3, ("t-c",)),
    ]
    assert all(not q.review_level for q in validated.questions)
    assert any("accept_this" in reason for reason in validated.rejected)
    "case: test_marks_never_change_which_groups_are_synthesized"
    budget = SynthesisBudget(max_items=1)
    marked = assign_review_priority((_target("t-a", A, state="reviewed"), _target("t-b", B), _target("t-c", C)))
    fresh, after = (_request(budget), _request(budget, targets=marked))
    assert [item.item_id for item in after.items] == [item.item_id for item in fresh.items]
    assert synthesis_key(after, revision_id="rev-1", model="m") == synthesis_key(fresh, revision_id="rev-1", model="m")
    "case: test_overlay_labels_inferred_explanations_and_reads_recorded_results"
    claim = Claim(
        id="claim:a",
        statement="Alpha returns one.",
        source="claude:s",
        target_ids=("t-a",),
        limitations=(),
        evidence=(),
        summary="none",
        result_line="",
    )
    plan = replace(_plan(), claims=(claim,))
    request = build_synthesis_request(plan, TARGETS, CHECKS, SOURCES.get, SynthesisBudget())
    validated = validate_synthesis(
        {
            "explanations": [{"item_id": "area:intent:a", "summary": "Alpha now returns one.", "target_ids": ["t-a"]}],
            "relevance": [{"claim_id": "claim:a", "check_key": "unit tests", "why": "it passed"}],
            "questions": [
                {
                    "kind": "implementation_choice_unrecorded",
                    "title": "Unrecorded choice: return value",
                    "detail": "Beta appears to pick a new constant.",
                    "target_ids": ["t-b"],
                }
            ],
        },
        request,
    )
    overlaid = apply_synthesis(plan, validated, CHECKS)
    items = {item.id: item for item in overlaid.items}
    explained = items["area:intent:a"]
    assert (explained.rationale, explained.rationale_origin, explained.source_label) == (
        "Alpha now returns one.",
        "inferred",
        "AI explanation",
    )
    assert "Intent inferred from the changed code; not stated by the author." in explained.limitations
    assert items["area:intent:b"].rationale_origin == "none"
    (link,) = overlaid.claims[0].evidence
    assert (link.key, link.status, link.mapping) == (
        "unit tests",
        "FAIL",
        "inferred",
    ), "recorded result, never the model's"
    assert overlaid.claims[0].summary == "inferred_failing"
    (question,) = [item for item in overlaid.questions if item.origin == "ai"]
    assert (question.reason, question.tier, question.target_ids) == ("implementation_choice_unrecorded", 3, ("t-b",))
    assert overlaid.coverage.synthesized == 1
    assert set(overlaid.coverage.unsynthesized_target_ids) == {"t-b", "t-c"}
    "case: test_the_prompt_names_the_answer_shape"
    system = synthesis_messages(_request())[0]["content"]
    for key in (
        "explanations",
        "item_id",
        "summary",
        "target_ids",
        "relevance",
        "claim_id",
        "check_key",
        "questions",
        "kind",
    ):
        assert f'"{key}"' in system, key
    "case: test_relevance_and_questions_cannot_carry_results"
    claim = Claim(
        id="claim:a",
        statement="Alpha returns one.",
        source="claude:s",
        target_ids=("t-a",),
        limitations=(),
        evidence=(),
        summary="none",
        result_line="",
    )
    plan = replace(_plan(), claims=(claim,))
    request = build_synthesis_request(plan, TARGETS, CHECKS, SOURCES.get, SynthesisBudget())
    validated = validate_synthesis(
        {
            "relevance": [
                {"claim_id": "claim:a", "check_key": "unit tests", "why": "it passed"},
                {"claim_id": "claim:a", "check_key": "lint", "why": "green in CI"},
            ],
            "questions": [
                {
                    "kind": "claim_evidence_scope_mismatch",
                    "title": "The unit tests passed but miss legacy rows",
                    "detail": "Legacy rows are not exercised.",
                    "target_ids": ["t-a"],
                }
            ],
        },
        request,
    )
    assert [link.why for link in validated.relevance["claim:a"]] == ["", ""]
    assert validated.questions == ()
    overlaid = apply_synthesis(plan, validated, CHECKS)
    assert all("pass" not in link.relevance.lower() for link in overlaid.claims[0].evidence)
    "case: test_ai_question_reasons_are_labelled"
    question = ReviewQuestion(
        id="q:ai:x:0",
        reason="implementation_choice_unrecorded",
        title="Unrecorded choice: retention",
        detail="",
        item_ids=(),
        target_ids=("t-a",),
        actions=(),
        origin="ai",
    )
    (reason,) = question_reasons((question,))["t-a"]
    assert reason.detail == "AI question: Unrecorded choice: retention"


def test_review_synthesis_source_contract() -> None:
    """case: test_unreadable_code_is_never_explained"""
    unreadable = {A: SOURCES[A], C: SOURCES[C]}
    request = build_synthesis_request(_plan(), TARGETS, CHECKS, unreadable.get, SynthesisBudget())
    assert [item.item_id for item in request.items] == ["area:intent:a", "area:intent:c"]
    validated = validate_synthesis({"explanations": []}, request)
    assert "t-b" in validated.unsynthesized_target_ids
    "case: test_each_item_is_explained_from_its_own_code"
    from lemoncrow.pro.capabilities.review.synthesis import PROMPT_VERSION

    system = synthesis_messages(_request())[0]["content"]
    assert "only from its own changed_source" in system
    assert PROMPT_VERSION >= 3


def _captured(tmp_path: Path) -> tuple[ReviewStore, Any, Any]:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src/a.py").write_text("def alpha():\n    return 0\n", encoding="utf-8")
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.invalid"
    repo.index.add_all()
    repo.index.write()
    signature = pygit2.Signature("Fixture Tester", "fixture@example.invalid", 1700000000, 0)
    repo.create_commit("HEAD", signature, signature, "base", repo.index.write_tree(), [])
    (root / "src/a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(root, working_tree=True)
    build = build_review_packet_with_blobs(root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, root, rng, title="synthesis")
    revision = snapshot_revision(store, session, root, rng, store_root=store_root, build=build)
    return (store, session, revision)


def _row(session: Any, revision: Any, key: str = "syn-1") -> ReviewSynthesisRow:
    return ReviewSynthesisRow(
        review_id=session.id,
        id="",
        revision_id=revision.id,
        synthesis_key=key,
        schema_version=1,
        prompt_version=1,
        model="m",
        status="pending",
    )


def test_claim_coalesces_and_completion_is_cas(tmp_path: Path) -> None:
    store, session, revision = _captured(tmp_path)
    first, claimed = store.claim_synthesis(_row(session, revision))
    assert claimed and first.status == "pending" and first.id
    again, claimed_again = store.claim_synthesis(_row(session, revision))
    assert (again.id, claimed_again) == (first.id, False)
    assert store.complete_synthesis(session.id, first.id, status="ready", payload_json='{"explanations": {}}')
    assert not store.complete_synthesis(session.id, first.id, status="failed", error="late"), "only pending changes"
    stored = store.get_synthesis(session.id, "syn-1")
    assert stored is not None and (stored.status, stored.payload_json) == ("ready", '{"explanations": {}}')
    assert stored.completed_at


def test_a_failed_synthesis_can_be_claimed_again(tmp_path: Path) -> None:
    store, session, revision = _captured(tmp_path)
    first, _ = store.claim_synthesis(_row(session, revision))
    store.complete_synthesis(session.id, first.id, status="failed", error="timeout")
    retry, claimed = store.claim_synthesis(_row(session, revision))
    assert claimed and retry.id != first.id and (retry.status == "pending")


def test_an_abandoned_pending_claim_is_reclaimed_after_the_window(tmp_path: Path) -> None:
    store, session, revision = _captured(tmp_path)
    first, _ = store.claim_synthesis(_row(session, revision))
    held, claimed = store.claim_synthesis(_row(session, revision), stale_after_s=3600)
    assert (held.id, claimed) == (first.id, False)
    retry, claimed = store.claim_synthesis(_row(session, revision), stale_after_s=0)
    assert claimed and retry.id != first.id
    assert not store.complete_synthesis(
        session.id, first.id, status="ready", payload_json="{}"
    ), "the abandoned claim cannot publish"


def test_prune_keeps_bounded_history_and_cascade_deletes(tmp_path: Path) -> None:
    store, session, revision = _captured(tmp_path)
    ids = []
    for index in range(5):
        row, _ = store.claim_synthesis(_row(session, revision, key=f"syn-{index}"))
        store.complete_synthesis(session.id, row.id, status="ready", payload_json="{}")
        ids.append(row.id)
    pending, _ = store.claim_synthesis(_row(session, revision, key="syn-pending"))
    assert store.prune_syntheses(session.id, keep=2) == 3
    kept = {row.id for row in store.list_syntheses(session.id)}
    assert kept == {ids[3], ids[4], pending.id}
    with store._transaction() as conn:
        conn.execute("DELETE FROM review_sessions WHERE id = ?", (session.id,))
    assert store.list_syntheses(session.id) == ()


_ANSWER = {
    "explanations": [{"item_id": "area:intent:a", "summary": "Alpha now returns one.", "target_ids": ["t-a"]}],
    "relevance": [],
    "questions": [],
}


class _Model:
    def __init__(self, answer: Any = _ANSWER, *, gate: threading.Event | None = None) -> None:
        self.answer = answer
        self.gate = gate
        self.calls: list[Any] = []
        self.entered = threading.Event()

    def __call__(self, messages: Any, *, json_schema: Any = None) -> Any:
        self.calls.append((messages, json_schema))
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(10)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def _run(store: Any, session: Any, revision_id: str, model: Any, **kwargs: Any) -> Any:
    return run_synthesis(
        store,
        review_id=session.id,
        revision_id=revision_id,
        request=kwargs.pop("request", _request()),
        model="m",
        chat_fn=model,
        budget=kwargs.pop("budget", SynthesisBudget()),
    )


def test_second_request_while_pending_makes_no_model_call(tmp_path: Path) -> None:
    store, session, revision = _captured(tmp_path)
    gate = threading.Event()
    model = _Model(gate=gate)
    results: list[Any] = []
    worker = threading.Thread(target=lambda: results.append(_run(store, session, revision.id, model)))
    worker.start()
    assert model.entered.wait(10)
    second = _run(store, session, revision.id, model)
    gate.set()
    worker.join(10)
    assert second.status == "pending" and second.synthesis is None
    assert len(model.calls) == 1
    assert results[0].status == "ready"
    assert model.calls[0][1] is SYNTHESIS_JSON_SCHEMA


def test_ready_result_is_reused_without_a_call(tmp_path: Path) -> None:
    store, session, revision = _captured(tmp_path)
    model = _Model()
    first = _run(store, session, revision.id, model)
    again = _run(store, session, revision.id, model)
    assert len(model.calls) == 1
    assert first.status == again.status == "ready"
    assert again.synthesis is not None
    assert again.synthesis.explanations["area:intent:a"].summary == "Alpha now returns one."
    assert again.row_id == first.row_id


def test_model_failure_is_stored_and_honest(tmp_path: Path) -> None:
    store, session, revision = _captured(tmp_path)
    failed = _run(store, session, revision.id, _Model(RuntimeError("backend down")))
    assert failed.status == "failed" and failed.synthesis is None
    assert "backend down" in failed.error
    stored = store.get_synthesis(session.id, store.list_syntheses(session.id)[0].synthesis_key)
    assert stored is not None and stored.status == "failed" and ("backend down" in stored.error)
    slow = _run(store, session, revision.id, _Model(gate=threading.Event()), budget=SynthesisBudget(timeout_s=0.05))
    assert slow.status == "failed" and "did not answer" in slow.error
    garbage = _run(store, session, revision.id, _Model("not json"))
    assert garbage.status == "failed"


def test_completion_for_an_old_snapshot_never_serves_the_new_one(tmp_path: Path) -> None:
    store, session, revision = _captured(tmp_path)
    gate = threading.Event()
    old_model = _Model(gate=gate)
    results: list[Any] = []
    worker = threading.Thread(target=lambda: results.append(_run(store, session, "rev-old", old_model)))
    worker.start()
    assert old_model.entered.wait(10)
    new_model = _Model(
        {**_ANSWER, "explanations": [{"item_id": "area:intent:b", "summary": "Beta.", "target_ids": ["t-b"]}]}
    )
    current = _run(store, session, revision.id, new_model)
    gate.set()
    worker.join(10)
    assert len(new_model.calls) == 1, "a pending claim for another revision does not answer this one"
    assert current.synthesis is not None and list(current.synthesis.explanations) == ["area:intent:b"]
    reread = _run(store, session, revision.id, new_model)
    assert reread.synthesis is not None and list(reread.synthesis.explanations) == ["area:intent:b"]
    assert len(new_model.calls) == 1


def test_find_synthesis_by_snapshot_inputs(tmp_path: Path) -> None:
    store, session, revision = _captured(tmp_path)
    row, _ = store.claim_synthesis(replace(_row(session, revision), inputs_digest="sin-1"))
    found = store.find_synthesis(session.id, revision_id=revision.id, inputs_digest="sin-1")
    assert found is not None and found.id == row.id
    assert store.find_synthesis(session.id, revision_id=revision.id, inputs_digest="sin-2") is None
    assert store.find_synthesis(session.id, revision_id="rev-other", inputs_digest="sin-1") is None


def test_an_answer_without_the_expected_shape_is_failed_not_ready(tmp_path: Path) -> None:
    store, session, revision = _captured(tmp_path)
    wrong = {"items": [{"item_id": "area:intent:a", "explanation": "Alpha returns one."}]}
    outcome = _run(store, session, revision.id, _Model(wrong))
    assert outcome.status == "failed" and "expected" in outcome.error


def test_a_ready_synthesis_is_found_under_a_new_inputs_digest(tmp_path: Path) -> None:
    store, session, revision = _captured(tmp_path)
    model = _Model()
    common = {
        "review_id": session.id,
        "revision_id": revision.id,
        "request": _request(),
        "model": "m",
        "chat_fn": model,
        "budget": SynthesisBudget(),
    }
    assert run_synthesis(store, inputs_digest="sin-1", **common).status == "ready"
    again = run_synthesis(store, inputs_digest="sin-2", **common)
    assert again.status == "ready" and len(model.calls) == 1
    assert store.find_synthesis(session.id, revision_id=revision.id, inputs_digest="sin-2") is not None


class _Explainer:
    """Explains exactly the items it is shown; records what it was sent."""

    def __init__(self) -> None:
        self.sent: list[list[str]] = []

    def __call__(self, messages: Any, *, json_schema: Any = None) -> Any:
        import json as _json

        payload = _json.loads(messages[-1]["content"])
        self.sent.append([item["item_id"] for item in payload["items"]])
        return {
            "explanations": [
                {
                    "item_id": item["item_id"],
                    "summary": f"{item['title']} changes.",
                    "target_ids": [row["target_id"] for row in item["targets"]],
                }
                for item in payload["items"]
            ],
            "relevance": [],
            "questions": [],
        }


def test_unchanged_items_are_reused_and_only_changed_items_are_sent(tmp_path: Path) -> None:
    store, session, revision = _captured(tmp_path)
    model = _Explainer()
    first = _run(store, session, revision.id, model)
    assert first.status == "ready" and model.sent == [["area:intent:a", "area:intent:b", "area:intent:c"]]
    edited = {**SOURCES, B: "def beta():\n    return 20\n"}
    request = build_synthesis_request(
        _plan(), TARGETS, (*CHECKS, {**CHECKS[1], "name": "e2e"}), edited.get, SynthesisBudget()
    )
    assert item_input_hash(request.items[1]) != item_input_hash(_request().items[1])
    assert item_input_hash(request.items[0]) == item_input_hash(_request().items[0])
    second = _run(store, session, revision.id, model, request=request)
    assert second.status == "ready" and model.sent[-1] == ["area:intent:b"], "only the changed item is sent"
    assert second.synthesis is not None
    assert set(second.synthesis.explanations) == {"area:intent:a", "area:intent:b", "area:intent:c"}


def test_everything_reused_makes_no_call(tmp_path: Path) -> None:
    store, session, revision = _captured(tmp_path)
    model = _Explainer()
    assert _run(store, session, revision.id, model).status == "ready"
    again = _run(store, session, "rev-identical", model)
    assert again.status == "ready" and len(model.sent) == 1
    assert again.synthesis is not None and len(again.synthesis.explanations) == 3


def test_a_reused_explanation_is_still_validated(tmp_path: Path) -> None:
    store, session, revision = _captured(tmp_path)
    model = _Explainer()
    assert _run(store, session, revision.id, model).status == "ready"
    moved = assign_review_priority((_target("t-a2", A), _target("t-b", B), _target("t-c", C)))
    chapters = ({**CHAPTERS[0], "targets": [{"target_id": "t-a2"}]}, *CHAPTERS[1:])
    view = build_guidance_view((), targets=moved, units=(), packet={"files": []})
    plan = build_review_plan(
        targets=moved, guidance=view, checks=CHECKS, chapters=chapters, routine_proofs={}, snapshot_number=0
    )
    request = build_synthesis_request(plan, moved, CHECKS, SOURCES.get, SynthesisBudget())
    outcome = _run(store, session, "rev-moved", model, request=request)
    assert outcome.synthesis is not None
    assert outcome.synthesis.explanations["area:intent:a"].target_ids == ("t-a2",)
    assert model.sent[-1] == ["area:intent:a"], "a changed target set is a changed input"


def _claimed(statement: str) -> Any:
    claim = Claim(
        id="claim:a",
        statement=statement,
        source="claude:s",
        target_ids=("t-a",),
        limitations=(),
        evidence=(),
        summary="none",
        result_line="",
    )
    plan = replace(_plan(), claims=(claim,))
    return build_synthesis_request(plan, TARGETS, CHECKS, SOURCES.get, SynthesisBudget())


def test_claim_dependent_questions_are_not_reused_when_claims_change(tmp_path: Path) -> None:
    store, session, revision = _captured(tmp_path)
    mismatch = {
        **_ANSWER,
        "explanations": [
            {"item_id": item, "summary": "Changes.", "target_ids": [target]}
            for item, target in (("area:intent:a", "t-a"), ("area:intent:b", "t-b"), ("area:intent:c", "t-c"))
        ],
        "questions": [
            {
                "kind": "author_observation_mismatch",
                "title": "Alpha returns 1, not 2 as claimed",
                "detail": "The claim and the code disagree.",
                "target_ids": ["t-a"],
            }
        ],
    }
    first = _run(store, session, revision.id, _Model(mismatch), request=_claimed("Alpha returns 2."))
    assert first.synthesis is not None and len(first.synthesis.questions) == 1
    fixed = _run(
        store, session, "rev-2", _Model({"relevance": [], "questions": []}), request=_claimed("Alpha returns 1.")
    )
    assert fixed.synthesis is not None and fixed.synthesis.questions == (), "a claim-dependent question is stale"


def test_a_model_question_on_code_it_was_not_shown_is_rejected(tmp_path: Path) -> None:
    store, session, revision = _captured(tmp_path)
    everything = {
        "explanations": [
            {"item_id": item, "summary": "Changes.", "target_ids": [target]}
            for item, target in (("area:intent:a", "t-a"), ("area:intent:b", "t-b"), ("area:intent:c", "t-c"))
        ],
        "relevance": [],
        "questions": [],
    }
    assert _run(store, session, revision.id, _Model(everything)).status == "ready"
    edited = {**SOURCES, B: "def beta():\n    return 20\n"}
    request = build_synthesis_request(_plan(), TARGETS, CHECKS, edited.get, SynthesisBudget())
    unseen = {
        "explanations": [{"item_id": "area:intent:b", "summary": "Beta returns twenty.", "target_ids": ["t-b"]}],
        "relevance": [],
        "questions": [
            {
                "kind": "implementation_choice_unrecorded",
                "title": "Unrecorded choice: alpha constant",
                "detail": "Alpha appears to pick a constant.",
                "target_ids": ["t-a"],
            }
        ],
    }
    outcome = _run(store, session, "rev-2", _Model(unseen), request=request)
    assert outcome.synthesis is not None and outcome.synthesis.questions == ()


def test_a_review_stops_spending_once_its_call_budget_is_used(tmp_path: Path) -> None:
    """Spec §10: a cumulative bound per Review, not only per call; pruning never resets it."""
    store, session, revision = _captured(tmp_path)
    model = _Model(RuntimeError("upstream 500"))
    budget = SynthesisBudget(max_calls=2)
    first = _run(store, session, revision.id, model, budget=budget)
    second = _run(store, session, revision.id, model, budget=budget)
    assert (first.status, second.status, len(model.calls)) == ("failed", "failed", 2)
    third = _run(store, session, revision.id, model, budget=budget)
    assert len(model.calls) == 2, "no call past the Review's budget"
    assert third.status == "failed" and "budget" in third.error.lower()
    assert store.synthesis_calls(session.id) == 2
    store.prune_syntheses(session.id, keep=0)
    assert store.synthesis_calls(session.id) == 2
    with store._transaction() as conn:
        conn.execute("DELETE FROM review_sessions WHERE id = ?", (session.id,))
    assert store.synthesis_calls(session.id) == 0, "the Change's deletion owns the counter"


def test_the_call_budget_reads_the_environment(monkeypatch: Any) -> None:
    assert SynthesisBudget.from_env().max_calls == SynthesisBudget().max_calls > 0
    monkeypatch.setenv("LEMONCROW_REVIEW_SYNTHESIS_MAX_CALLS", "5")
    assert SynthesisBudget.from_env().max_calls == 5
    monkeypatch.setenv("LEMONCROW_REVIEW_SYNTHESIS_MAX_CALLS", "0")
    assert SynthesisBudget.from_env().max_calls == 0, "zero means no model calls for any Review"
