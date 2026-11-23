from __future__ import annotations

from copy import deepcopy

from lemoncrow.pro.capabilities.review.hosted import _finalize_detail
from lemoncrow.pro.capabilities.review.tour import (
    apply_tour_synthesis,
    build_change_tour,
    tour_synthesis_context,
    validate_tour_synthesis,
)


def _target(
    target_id: str,
    path: str,
    *,
    symbol: str = "",
    start_line: int = 1,
    structural_level: str = "semantic",
    fail: int = 0,
    unknown: int = 0,
) -> dict[str, object]:
    return {
        "target_id": target_id,
        "path": path,
        "symbol": symbol,
        "start_line": start_line,
        "end_line": start_line + 2,
        "unit_key": "unit:" + target_id,
        "structural_level": structural_level,
        "verification": {"fail": fail, "unknown": unknown},
        "reasons": [],
    }


def _projection() -> dict[str, object]:
    return {
        "items": [
            {
                "id": "item:behavior",
                "key": "behavior:public-api",
                "kind": "behavior",
                "title": "Public API changes",
                "before": "Clients sent one field.",
                "after": "Clients now send two fields.",
                "rationale": "",
                "rationale_origin": "author",
                "primary_target_ids": ["t-api"],
                "related_target_ids": [],
                "claim_ids": ["claim:api"],
                "concerns": [],
                "position": 4,
                "stale": False,
            },
            {
                "id": "item:failing",
                "key": "area:validation",
                "kind": "area",
                "title": "Validation path",
                "before": "",
                "after": "",
                "rationale": "",
                "rationale_origin": "none",
                "primary_target_ids": ["t-validation"],
                "related_target_ids": [],
                "claim_ids": [],
                "concerns": [],
                "position": 2,
                "stale": False,
            },
            {
                "id": "item:mechanical",
                "key": "area:generated",
                "kind": "mechanical",
                "title": "Generated output",
                "before": "",
                "after": "",
                "rationale": "",
                "rationale_origin": "none",
                "primary_target_ids": ["t-generated"],
                "related_target_ids": [],
                "claim_ids": [],
                "concerns": [],
                "position": 1,
                "stale": False,
            },
        ],
        "claims": [
            {
                "id": "claim:api",
                "evidence": [
                    {
                        "key": "contract-test",
                        "title": "Contract test",
                        "status": "PASS",
                        "state": "current",
                        "snapshot_number": 2,
                    }
                ],
            }
        ],
        "decisions": [
            {"key": "decision:api-shape", "target_ids": ["t-api"]},
        ],
        "delta": {
            "changed": ["t-api", "t-validation"],
            "added": [],
            "removed": 0,
            "stale_evidence": [],
            "reopened_decisions": ["decision:api-shape"],
        },
    }


def _targets() -> list[dict[str, object]]:
    return [
        _target("t-api", "src/api.py", symbol="handle", start_line=40),
        _target("t-validation", "src/validation.py", symbol="validate", start_line=10, fail=1),
        _target("t-generated", "generated/schema.json", structural_level="mechanical"),
    ]


def test_finalize_detail_never_calls_a_failed_enrichment_ready_context() -> None:
    assert _finalize_detail(superseded=False, failure="") == "source and available review context are ready"
    assert "not applied: conflict" in _finalize_detail(superseded=False, failure="conflict")
    assert "superseded" in _finalize_detail(superseded=True, failure="source changed")


def test_signals_match_whole_words_and_the_fallback_names_what_changed() -> None:
    # 2026-11-19 on a real change: client/src/.../usage.py was a "public contract" because "client" contains "cli".
    item = {"key": "area:usage", "id": "item-usage", "title": "Usage", "primary_target_ids": ["t-usage", "t-auth"]}
    targets = [
        {"target_id": "t-usage", "path": "client/src/usage.py", "symbol": "", "additions": 4, "deletions": 1},
        {"target_id": "t-auth", "path": "client/src/author_names.py", "symbol": "estimate", "additions": 2},
    ]

    step = build_change_tour({"items": [item]}, targets)["steps"][0]
    assert not {"public_contract", "security_trust_boundary", "state_persistence"} & set(step["signals"])
    assert step["why"] == "Changes usage.py, estimate (+6 \u22121)."
    assert step["synthesis_needed"] is True

    worded = {"target_id": "t-oauth", "path": "src/oauthTokens.py", "symbol": "PublicApi"}
    step = build_change_tour({"items": [{**item, "primary_target_ids": ["t-oauth"]}]}, [worded])["steps"][0]
    assert {"public_contract", "security_trust_boundary"} <= set(step["signals"])


def test_deterministic_tour_orders_consequential_steps_and_suppresses_mechanical_noise() -> None:
    tour = build_change_tour(_projection(), _targets())

    assert [row["semantic_key"] for row in tour["steps"]] == [
        "behavior:public-api",
        "area:validation",
    ]
    first = tour["steps"][0]
    assert first["id"] == "tour:behavior:public-api"
    assert first["change_type"] == "reopened"
    assert {"reopened_judgment", "human_decision", "behavior_contract", "public_contract"} <= set(first["signals"])
    assert first["implementation"] == {
        "target_id": "t-api",
        "path": "src/api.py",
        "symbol": "handle",
        "start_line": 40,
        "end_line": 42,
    }
    assert first["evidence_refs"][0]["claim_id"] == "claim:api"


def test_snapshot_delta_recomposes_content_without_changing_semantic_step_identity() -> None:
    projection = _projection()
    first = build_change_tour(projection, _targets())
    step_id = first["steps"][0]["id"]

    changed = deepcopy(projection)
    changed["delta"] = {
        "changed": [],
        "added": ["t-api"],
        "removed": 0,
        "stale_evidence": [],
        "reopened_decisions": [],
    }
    changed["decisions"] = []
    second = build_change_tour(changed, _targets())
    second_step = next(row for row in second["steps"] if row["id"] == step_id)
    assert second_step["id"] == step_id
    assert second_step["change_type"] == "added"
    assert "reopened_judgment" not in second_step["signals"]


def test_stale_targets_and_evidence_are_explicit_not_silently_remapped() -> None:
    projection = _projection()
    projection["items"][0]["related_target_ids"] = ["t-old"]
    projection["claims"][0]["evidence"][0]["state"] = "stale"
    projection["claims"][0]["evidence"][0]["snapshot_number"] = 1

    tour = build_change_tour(projection, _targets())
    step = tour["steps"][0]

    assert step["stale"]
    assert step["stale_target_ids"] == ["t-old"]
    assert step["evidence_refs"][0]["state"] == "stale"
    assert step["target_ids"] == ["t-api"], "stale identity is never redirected to a different current target"


def test_tour_works_without_ai_and_only_requests_bounded_gap_wording() -> None:
    targets = _targets()
    targets[1]["verification"] = {"fail": 0, "unknown": 0}
    tour = build_change_tour(_projection(), targets)
    assert tour["steps"]
    context = tour_synthesis_context(tour)

    assert [row["step_id"] for row in context["steps"]] == ["tour:area:validation"]
    assert context["steps"][0]["target_ids"] == ["t-validation"]


def test_tour_synthesis_cannot_invent_steps_or_targets() -> None:
    tour = build_change_tour(_projection(), _targets())
    raw = {
        "steps": [
            {
                "step_id": "tour:area:validation",
                "title": "Validate the new input contract",
                "why": "This target determines which inputs are accepted.",
                "target_ids": ["t-validation"],
            },
            {
                "step_id": "tour:not-real",
                "title": "Invented",
                "why": "Invented",
                "target_ids": ["t-validation"],
            },
            {
                "step_id": "tour:area:validation",
                "title": "Wrong target",
                "why": "Wrong target",
                "target_ids": ["t-api"],
            },
        ]
    }

    validated = validate_tour_synthesis(raw, tour)
    assert list(validated) == ["tour:area:validation"]
    refined = apply_tour_synthesis(tour, raw)
    step = next(row for row in refined["steps"] if row["id"] == "tour:area:validation")
    assert step["title"] == "Validate the new input contract"
    assert step["why_origin"] == "inferred"
    assert step["target_ids"] == ["t-validation"]


def _area(key: str, target_id: str, position: int) -> dict[str, object]:
    return {
        "id": "item:" + key,
        "key": key,
        "kind": "area",
        "title": key,
        "rationale_origin": "none",
        "primary_target_ids": [target_id],
        "related_target_ids": [],
        "claim_ids": [],
        "concerns": [],
        "position": position,
    }


def _re_review_case() -> tuple[dict[str, object], list[dict[str, object]]]:
    # Position order (the first-review order) is deliberately the reverse of the re-review order.
    items = [
        _area("area:kept", "t-kept", 1),
        _area("area:pending", "t-pending", 2),
        _area("area:new", "t-new", 3),
        _area("area:reopened", "t-reopened", 4),
    ]
    targets = [
        {**_target("t-kept", "lib/cache.ts", symbol="clear"), "state": "reviewed"},
        {**_target("t-pending", "lib/format.ts", symbol="format"), "state": "unreviewed"},
        {**_target("t-new", "invoices/export.ts", symbol="exportCsv"), "state": "unreviewed"},
        {**_target("t-reopened", "invoices/list.ts", symbol="listInvoices"), "state": "changed_since_review"},
    ]
    delta = {"changed": ["t-reopened"], "added": ["t-new"], "removed": 0, "reopened_decisions": []}
    return {"items": items, "claims": [], "decisions": [], "delta": delta}, targets


def test_re_review_tour_starts_with_reopened_then_new_then_unreviewed_and_kept_last() -> None:
    projection, targets = _re_review_case()
    tour = build_change_tour(projection, targets)

    assert [row["semantic_key"] for row in tour["steps"]] == [
        "area:reopened",
        "area:new",
        "area:pending",
        "area:kept",
    ]
    assert tour["steps"][0]["change_type"] == "reopened"
    assert "reopened_judgment" in tour["steps"][0]["signals"]


def test_first_review_tour_order_ignores_review_state() -> None:
    projection, targets = _re_review_case()
    projection["delta"] = None
    tour = build_change_tour(projection, [{**target, "state": "unreviewed"} for target in targets])
    with_marks = build_change_tour(projection, [{**target, "state": "reviewed"} for target in targets])

    expected = ["area:kept", "area:pending", "area:new", "area:reopened"]
    assert [row["semantic_key"] for row in tour["steps"]] == expected
    assert [row["semantic_key"] for row in with_marks["steps"]] == expected


def test_building_or_visiting_a_tour_has_no_review_state_to_mutate() -> None:
    projection = _projection()
    targets = _targets()
    before_projection = deepcopy(projection)
    before_targets = deepcopy(targets)

    tour = build_change_tour(projection, targets)
    _ = tour["steps"][0]

    assert projection == before_projection
    assert targets == before_targets
    assert all("reviewed" not in row and "visited" not in row for row in tour["steps"])
