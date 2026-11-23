from dataclasses import dataclass

from lemoncrow.pro.capabilities.review.brief_summary import build_brief_summary
from lemoncrow.pro.capabilities.review.consumer_inventory import consumer_inventory


def projection(items=(), intents=()):
    return {
        "items": list(items),
        "plan": {"intents": list(intents)},
        "coverage": {"file_count": 134, "target_count": 590},
        "questions": [],
        "actions": [],
        "decisions": [],
    }


def explanation(text, **overrides):
    return {
        "id": "item:change",
        "kind": "behavior",
        "rationale_origin": "author",
        "after": text,
        "stale": False,
        **overrides,
    }


def test_summary_distills_context_without_progress_chatter_or_claimed_test_results():
    value = projection(
        [
            explanation("Each branch keeps its review. Additional implementation detail."),
            explanation("", id="agent:progress", title="I'll inspect the implementation."),
            explanation(
                "", id="agent:done", title="Implemented branch-local review identity", rationale="Tests: 5 passed."
            ),
            explanation("", id="agent:conversation", title="Yes, the Brief supports AI annotations."),
        ]
    )
    result = build_brief_summary(value)
    assert result["paragraphs"] == ["Each branch keeps its review.", "Implemented branch-local review identity"]
    assert result["verification"] == "No current verification results recorded."
    assert result["origin"] == "author"


def test_summary_uses_current_recorded_checks_not_stale_or_agent_claims():
    checks = [
        {"name": "unit", "status": "PASS", "state": "current"},
        {"name": "unit", "status": "PASS", "state": "current"},
        {"name": "browser", "status": "FAIL", "state": "current"},
        {"name": "old", "status": "PASS", "state": "stale"},
        {"name": "lint", "status": "NOT_RUN", "state": "current"},
    ]
    result = build_brief_summary(projection(), checks)
    assert result["verification"] == "Recorded checks: 1 passed, 1 failed, 1 unresolved."


def test_summary_is_bounded_and_reports_explanations_beyond_the_bound():
    for count in (3, 4, 50):
        result = build_brief_summary(projection([explanation(f"Change {i}.") for i in range(count)]))
        assert len(result["paragraphs"]) == min(3, count)
        assert result["additional_explanations"] == max(0, count - 3)
    result = build_brief_summary(projection([explanation("Changed " + "long sentence " * 200)]))
    assert len(result["paragraphs"][0]) <= 280


def test_summary_excludes_stale_explanations_and_labels_inferred_text():
    result = build_brief_summary(
        projection(
            [
                explanation("Obsolete implementation.", stale=True),
                explanation("Now preserves review identity.", rationale_origin="inferred"),
            ]
        )
    )
    assert result["paragraphs"] == ["Now preserves review identity."]
    assert result["origin"] == "inferred"


def test_summary_without_ai_or_agent_explanation_states_engine_facts():
    areas = [
        {"kind": "area", "title": name, "primary_target_ids": [name], "rationale_origin": "none"}
        for name in ("Review", "Frontend", "Server", "Docs")
    ]
    packet = {"files": [{"path": f"src/f{i}.py", "status": "modified"} for i in range(5)], "symbols": []}
    result = build_brief_summary(projection(areas), packet=packet)
    assert result["paragraphs"] == [
        "Changed files: src/f0.py, src/f1.py, src/f2.py and 2 more",
        "Not checked: no test or check ran on this revision",
    ]
    assert not any("Changes span" in text for text in result["paragraphs"])
    assert result["origin"] == "observed"
    assert result["scope"] == "134 files · 590 parts"


def test_summary_deduplicates_intent_and_keeps_actionable_counts():
    value = projection(
        [explanation("Keep branch history."), explanation("Fixed review identity.")],
        [{"intent": "Keep branch history."}],
    )
    value["decision_progress"] = {"outstanding": 2}
    value["questions"] = [{"title": "Check migration"}]
    result = build_brief_summary(value)
    assert result["paragraphs"] == ["Keep branch history.", "Fixed review identity."]
    assert result["additional_explanations"] == 0
    assert result["attention"] == "2 open decisions; 1 issue to investigate"


def test_summary_keeps_prioritized_changes_visible_without_a_plan_question():
    result = build_brief_summary(
        projection(
            [
                explanation("Fixed branch identity.", attention="review_first", primary_target_ids=["t-1", "t-2"]),
                explanation("Changed storage.", attention="review_first", primary_target_ids=["t-2"]),
            ]
        )
    )
    assert result["attention"] == "2 parts need attention"


def test_headline_is_the_review_title_with_the_count_line_under_it():
    from types import SimpleNamespace

    def part(target_id, added, removed):
        return SimpleNamespace(
            target_id=target_id,
            kind="hunk",
            path="a.py",
            symbol="",
            start_line=1,
            end_line=2,
            additions=added,
            deletions=removed,
        )

    changed = [part("t-1", 60, 10), part("t-2", 15, 5)]
    value = projection([explanation("Cache invoice lists.")])
    value["coverage"] = {"file_count": 5, "target_count": 14}
    result = build_brief_summary(value, targets=changed, title="  Review browser\nharness fixture change ")
    assert result["headline"] == {
        "title": "Review browser harness fixture change",
        "counts": "5 files \N{MIDDLE DOT} 14 parts \N{MIDDLE DOT} +75 \N{MINUS SIGN}15",
    }
    # One file, one part and no line counts: the count line says so and stops.
    value["coverage"] = {"file_count": 1, "target_count": 1}
    assert build_brief_summary(value, title="Fix it")["headline"]["counts"] == "1 file \N{MIDDLE DOT} 1 part"
    # A Review with no name of its own leads with its first statement, never with nothing.
    assert build_brief_summary(value)["headline"]["title"] == "Cache invoice lists."


def test_empty_intent_does_not_inflate_omitted_explanation_count():
    result = build_brief_summary(
        projection(
            [explanation("Fixed branch identity.")],
            [{"intent": ""}],
        )
    )
    assert result["additional_explanations"] == 0


@dataclass(frozen=True)
class _Target:
    target_id: str
    kind: str
    path: str
    symbol: str
    start_line: int
    end_line: int


_BILLING = (
    _Target("t-doc", "document_section", "lib/cache.ts", "", 1, 1),
    _Target("t-wrap", "symbol", "lib/cache.ts", "wrap", 3, 9),
    _Target("t-clear", "symbol", "lib/cache.ts", "clear", 11, 13),
    _Target("t-hunk", "hunk", "invoices/list.ts", "", 1, 8),
    _Target("t-list", "symbol", "invoices/list.ts", "listInvoices", 4, 8),
)
_BILLING_PACKET = {
    "index_status": "absent",
    "degraded": ["symbol_relations"],
    "files": [{"path": "invoices/list.ts", "status": "modified"}, {"path": "lib/cache.ts", "status": "added"}],
    "symbols": [
        {"symbol_name": "listInvoices", "file_path": "invoices/list.ts", "start_line": 4, "change": "modified"},
        {"symbol_name": "key", "file_path": "invoices/list.ts", "start_line": 5, "change": "modified"},
        {"symbol_name": "wrap", "file_path": "lib/cache.ts", "start_line": 3, "change": "added"},
        {"symbol_name": "clear", "file_path": "lib/cache.ts", "start_line": 11, "change": "added"},
    ],
    "impact": [],
}


def _caller(source: str, line: int) -> dict:
    return {
        "source_path": source,
        "target_path": "invoices/list.ts",
        "kind": "calls",
        "evidence": [{"path": source, "line": line, "symbol": "listInvoices"}],
        "symbols": [{"name": "listInvoices", "count": 1}],
    }


def _billing(current=None, checks=()):
    value = projection()
    value["context"] = consumer_inventory(_BILLING_PACKET, _BILLING, current=current)
    return build_brief_summary(value, checks, packet=_BILLING_PACKET, targets=_BILLING)


def test_headline_names_the_changed_symbol_its_outside_callers_new_files_and_what_was_not_checked():
    index = {
        "edges": [
            _caller("api/v1/invoices.ts", 4),
            _caller("web/Dashboard.tsx", 4),
            _caller("jobs/exportInvoices.ts", 6),
            _caller("invoices/list.test.ts", 7),
        ],
        "degraded": False,
    }
    result = _billing(index)
    assert result["paragraphs"] == [
        "listInvoices (invoices/list.ts) changed · called from "
        "api/v1/invoices.ts, jobs/exportInvoices.ts and web/Dashboard.tsx",
        "New file: lib/cache.ts (wrap, clear)",
        "Not checked: invoices/list.test.ts exercises listInvoices; nothing ran it",
    ]
    assert [fact["kind"] for fact in result["facts"]] == ["reach", "files", "unchecked"]
    assert result["facts"][0]["target_ids"] == ["t-list"] and result["origin"] == "observed"
    assert result["facts"][2]["target_ids"] == ["t-list"]


def test_headline_counts_test_files_only_when_a_check_ran():
    index = {"edges": [_caller("api/v1/invoices.ts", 4), _caller("invoices/list.test.ts", 7)], "degraded": False}
    result = _billing(index, checks=[{"name": "unit", "status": "PASS", "state": "current"}])
    assert result["paragraphs"] == [
        "listInvoices (invoices/list.ts) changed · called from api/v1/invoices.ts, plus 1 test file",
        "New file: lib/cache.ts (wrap, clear)",
    ]


def test_headline_lists_at_most_three_callers_and_counts_the_rest():
    index = {"edges": [_caller(f"app/c{i}.ts", 1) for i in range(5)], "degraded": False}
    assert _billing(index)["paragraphs"][0] == (
        "listInvoices (invoices/list.ts) changed · called from 5 files: app/c0.ts, app/c1.ts, app/c2.ts and 2 more"
    )


def test_headline_never_reports_zero_callers_when_none_were_measured():
    result = _billing(None, checks=[{"name": "unit", "status": "PASS", "state": "current"}])
    assert result["paragraphs"] == [
        "Changed: listInvoices (invoices/list.ts)",
        "New file: lib/cache.ts (wrap, clear)",
        "Not checked: callers of the changed code (no code index)",
    ]


def test_author_explanations_keep_the_title_and_facts_stay_alongside():
    value = projection([explanation("Cache invoice lists.")])
    value["context"] = consumer_inventory(_BILLING_PACKET, _BILLING)
    result = build_brief_summary(value, packet=_BILLING_PACKET, targets=_BILLING)
    assert result["paragraphs"] == ["Cache invoice lists."] and result["origin"] == "author"
    assert [fact["kind"] for fact in result["facts"]] == ["symbols", "files", "unchecked"]


# The walkthrough's "shop" change: an auth check and two order functions, all called from app/views.py.
@dataclass(frozen=True)
class _Priority:
    reasons: tuple = ()
    review_first: bool = False


@dataclass(frozen=True)
class _Ranked(_Target):
    priority: _Priority = _Priority()
    state: str = "unreviewed"


def _shop_targets(extra=()):
    from lemoncrow.pro.capabilities.review.review_priority import QueueReason

    auth = _Priority((QueueReason("sensitive_path_attention", "Access-control code changed"),))
    # Canonical review order: the auth path first, as the Code tab lists it.
    return (
        _Ranked("t-view", "symbol", "app/auth.py", "can_view_order", 1, 5, auth),
        _Ranked("t-total", "symbol", "app/orders.py", "order_total", 1, 5),
        _Ranked("t-discount", "symbol", "app/orders.py", "apply_discount", 7, 12),
        *extra,
    )


def _shop_packet(targets):
    return {
        "index_status": "absent",
        "degraded": ["symbol_relations"],
        "files": [{"path": path, "status": "modified"} for path in dict.fromkeys(item.path for item in targets)],
        "symbols": [
            {"symbol_name": item.symbol, "file_path": item.path, "start_line": item.start_line, "change": "modified"}
            for item in targets
        ],
        "impact": [],
    }


def _uses(source, target_path, *names):
    return {
        "source_path": source,
        "target_path": target_path,
        "kind": "calls",
        "evidence": [{"path": source, "line": index + 3, "symbol": name} for index, name in enumerate(names)],
        "symbols": [{"name": name, "count": 1} for name in names],
    }


def _shop(targets, edges, checks=(), check_command=None):
    packet = _shop_packet(targets)
    value = projection()
    value["context"] = consumer_inventory(packet, targets, current={"edges": edges, "degraded": False})
    return build_brief_summary(value, checks, packet=packet, targets=targets, check_command=check_command)


_SHOP_EDGES = [
    _uses("app/views.py", "app/auth.py", "can_view_order"),
    _uses("app/views.py", "app/orders.py", "order_total", "apply_discount"),
    _uses("tests/test_orders.py", "app/orders.py", "order_total", "apply_discount"),
]


def test_headline_follows_review_order_names_the_sensitive_path_and_the_tests_nothing_ran():
    result = _shop(_shop_targets(), _SHOP_EDGES)
    assert result["paragraphs"] == [
        "can_view_order (app/auth.py) changed · access-control code · called from app/views.py",
        "order_total (app/orders.py) changed · called from app/views.py",
        "apply_discount (app/orders.py) changed · called from app/views.py",
        "Not checked: tests/test_orders.py exercises order_total and apply_discount; nothing ran it",
    ]
    assert [fact["target_ids"] for fact in result["facts"]] == [
        ["t-view"],
        ["t-total"],
        ["t-discount"],
        ["t-total", "t-discount"],
    ]
    assert not any("command" in fact for fact in result["facts"])


def test_headline_keeps_three_lead_lines_and_names_the_rest_instead_of_dropping_them():
    extra = tuple(_Ranked(f"t-x{i}", "symbol", "app/orders.py", f"extra_{i}", 20 + i, 21 + i) for i in range(2))
    edges = [*_SHOP_EDGES, _uses("app/views.py", "app/orders.py", "extra_0", "extra_1")]
    result = _shop(_shop_targets(extra), edges)
    reach = [fact for fact in result["facts"] if fact["kind"] == "reach"]
    assert len(reach) == 4
    assert reach[3]["text"] == "And 2 more parts like these: extra_0 (app/orders.py), extra_1 (app/orders.py)"
    assert reach[3]["target_ids"] == ["t-x0", "t-x1"]
    assert not any(fact["text"].startswith("Changed:") for fact in result["facts"])


def test_sensitive_part_without_outside_callers_still_leads_and_says_why():
    result = _shop(_shop_targets(), _SHOP_EDGES[1:])
    assert result["paragraphs"][0] == "can_view_order (app/auth.py) changed · access-control code"


def test_not_checked_offers_only_a_recorded_command_and_only_when_nothing_ran():
    asked = []

    def command():
        asked.append(True)
        return "uv run pytest -q"

    result = _shop(_shop_targets(), _SHOP_EDGES, check_command=command)
    assert result["facts"][-1]["command"] == "uv run pytest -q"
    assert _shop(_shop_targets(), _SHOP_EDGES, check_command=lambda: "")["facts"][-1].get("command") is None
    asked.clear()
    ran = _shop(_shop_targets(), _SHOP_EDGES, [{"name": "unit", "status": "PASS", "state": "current"}], command)
    assert not any(fact["kind"] == "unchecked" for fact in ran["facts"]) and asked == []


# The demo's real orders.py hunk, as the capture stores it: apply_discount's guard loses `or percent > 100`.
_ORDERS_HUNK = {
    "old_start": 1,
    "old_lines": 11,
    "new_start": 1,
    "new_lines": 12,
    "patch": (
        " def order_total(items):\n"
        "     total = 0\n"
        "-    for i in range(len(items)):\n"
        "+    for i in range(len(items) - 1):\n"
        '         total += items[i]["price"] * items[i]["qty"]\n'
        "     return total\n"
        " \n"
        " \n"
        " def apply_discount(total, percent):\n"
        "-    if percent < 0 or percent > 100:\n"
        '-        raise ValueError("percent must be between 0 and 100")\n'
        "+    # Promo codes can now stack, so percent may exceed 100.\n"
        "+    if percent < 0:\n"
        '+        raise ValueError("percent must not be negative")\n'
        "     return round(total * (100 - percent) / 100, 2)\n"
    ),
}
_AUTH_HUNK = {
    "old_start": 2,
    "old_lines": 4,
    "new_start": 2,
    "new_lines": 7,
    "patch": (
        '     """Only the order\'s owner or an admin may view it."""\n'
        "     if user.is_admin:\n"
        "         return True\n"
        "+    # Guest checkout: orders placed before sign-in have no owner yet.\n"
        "+    if order.owner_id is None:\n"
        "+        return True\n"
        "     return order.owner_id == user.id\n"
    ),
}


def _demo_targets(*, wide_total: bool = False, **states):
    """The demo's parts with the queue's Review first membership: the access-control path and the narrowed guard.

    ``review_first`` is what ``assign_review_priority`` sets (test_review_priority proves it survives a
    judgment); *wide_total* is order_total with outside callers, Review first by ``wide_reach``.
    """

    from lemoncrow.pro.capabilities.review.review_priority import QueueReason

    auth = _Priority((QueueReason("sensitive_path_attention", "Access-control code changed"),), review_first=True)
    # The real queue keeps only "Reviewed; content unchanged" on a reviewed part.
    view = states.get("view", "unreviewed")
    if view == "reviewed":
        auth = _Priority((QueueReason("reviewed", "Reviewed; content unchanged"),), review_first=True)
    return (
        _Ranked("t-view", "symbol", "app/auth.py", "can_view_order", 1, 8, auth, view),
        _Ranked(
            "t-total",
            "symbol",
            "app/orders.py",
            "order_total",
            1,
            5,
            _Priority(review_first=wide_total),
            states.get("total", "unreviewed"),
        ),
        _Ranked(
            "t-disc",
            "symbol",
            "app/orders.py",
            "apply_discount",
            8,
            12,
            _Priority(review_first=True),
            states.get("disc", "unreviewed"),
        ),
    )


def _demo(targets, edges, checks=()):
    packet = _shop_packet(targets)
    packet["files"] = [
        {"path": "app/auth.py", "status": "modified", "hunks": [_AUTH_HUNK]},
        {"path": "app/orders.py", "status": "modified", "hunks": [_ORDERS_HUNK]},
    ]
    value = projection()
    current = {"edges": edges, "degraded": False} if edges is not None else None
    value["context"] = consumer_inventory(packet, targets, current=current)
    return build_brief_summary(value, checks, packet=packet, targets=targets)["risk"]


# The demo: app/views.py calls can_view_order; tests/test_orders.py tests the two order functions.
_DEMO_EDGES = [
    _uses("app/views.py", "app/auth.py", "can_view_order"),
    _uses("tests/test_orders.py", "app/orders.py", "order_total", "apply_discount"),
]


def test_risk_on_the_demo_is_high_with_the_access_control_reason_and_lists_every_fact():
    risk = _demo(_demo_targets(), _DEMO_EDGES)
    assert risk["text"] == "High · can_view_order is access-control code · and 4 more"
    assert [(fact["level"], fact["text"]) for fact in risk["facts"]] == [
        ("high", "can_view_order is access-control code"),
        (
            "high",
            "apply_discount no longer rejects a percent over 100 "
            "(check narrowed from `percent < 0 or percent > 100` to `percent < 0`)",
        ),
        ("high", "can_view_order is called from app/views.py and no check ran on this revision"),
        ("medium", "order_total is exercised by tests/test_orders.py, which did not run"),
        ("medium", "apply_discount is exercised by tests/test_orders.py, which did not run"),
    ]
    assert [fact["target_ids"] for fact in risk["facts"]][:2] == [["t-view"], ["t-disc"]]
    assert risk["not_measured"] == []
    # Only the queue's Review first parts are risky: order_total (unrun tests, Medium) stays in Understand.
    assert risk["risky_target_ids"] == ["t-view", "t-disc"]
    assert risk["understanding"] == {"reviewed": 0, "total": 2, "risky": True, "text": "0 of 2 risky parts reviewed"}


def test_risk_on_the_demo_without_views_counts_the_two_review_first_parts():
    # The test plan's shop: no app/views.py, so only the tests edge exists.
    risk = _demo(_demo_targets(), _DEMO_EDGES[1:])
    assert risk["text"] == "High · can_view_order is access-control code · and 3 more"
    assert [(fact["kind"], fact["target_ids"]) for fact in risk["facts"]] == [
        ("sensitive", ["t-view"]),
        ("removed_safeguard", ["t-disc"]),
        ("untested", ["t-total"]),
        ("untested", ["t-disc"]),
    ]
    assert risk["risky_target_ids"] == ["t-view", "t-disc"]
    assert risk["understanding"]["text"] == "0 of 2 risky parts reviewed"


def test_understanding_counts_reviewed_and_needs_changes_marks_on_the_risky_parts_only():
    # Only the access-control path and the narrowed guard are in Review first.
    risk = _demo(_demo_targets(view="reviewed", total="needs_changes"), [])
    assert risk["risky_target_ids"] == ["t-view", "t-disc"]
    # A Reviewed mark drops the queue's reasons, never its Review first membership: the part stays risky and counts.
    assert risk["facts"][0]["text"] == "can_view_order is access-control code"
    # order_total's Needs changes mark is outside the risky set, so it does not count.
    assert risk["understanding"]["text"] == "1 of 2 risky parts reviewed"
    # A Needs changes mark is a judgment: it counts like Reviewed.
    judged = _demo(_demo_targets(view="reviewed", disc="needs_changes"), [])
    assert judged["understanding"] == {"reviewed": 2, "total": 2, "risky": True, "text": "2 of 2 risky parts reviewed"}
    # A stale mark (changed since review) is not a judgment on this revision.
    stale = _demo(_demo_targets(disc="changed_since_review"), [])
    assert stale["understanding"]["text"] == "0 of 2 risky parts reviewed"


def test_a_check_run_lowers_outside_callers_but_not_a_sensitive_area_or_a_removed_safeguard():
    risk = _demo(_demo_targets(), _DEMO_EDGES, [{"name": "unit", "status": "PASS", "state": "current"}])
    assert risk["level"] == "high"
    assert risk["reason"] == "can_view_order is access-control code · and 2 more"
    assert [(fact["kind"], fact["level"]) for fact in risk["facts"]] == [
        ("sensitive", "high"),
        ("removed_safeguard", "high"),
        ("outside_callers", "medium"),
    ]
    assert risk["facts"][2]["text"] == "can_view_order is called from app/views.py"
    assert risk["risky_target_ids"] == ["t-view", "t-disc"]


def test_outside_callers_with_a_check_run_are_medium_but_still_review_first_and_count():
    # order_total is also called from app/views.py: wide_reach puts it in Review first while its fact is Medium.
    edges = [_uses("app/views.py", "app/orders.py", "order_total"), *_DEMO_EDGES]
    checks = [{"name": "unit", "status": "PASS", "state": "current"}]
    risk = _demo(_demo_targets(wide_total=True, total="needs_changes"), edges, checks)
    total = [fact for fact in risk["facts"] if fact["target_ids"] == ["t-total"]]
    assert [(fact["kind"], fact["level"]) for fact in total] == [("outside_callers", "medium")]
    assert risk["risky_target_ids"] == ["t-view", "t-total", "t-disc"]
    assert risk["understanding"] == {"reviewed": 1, "total": 3, "risky": True, "text": "1 of 3 risky parts reviewed"}


def test_a_single_fact_reason_has_no_and_more_and_no_high_fact_counts_every_part():
    # order_total's unrun tests are its only fact (Medium); README.md has none.
    targets = (
        _Ranked("t-total", "symbol", "app/orders.py", "order_total", 1, 5, state="needs_changes"),
        _Ranked("t-readme", "file", "README.md", "", 1, 3),
    )
    risk = _demo(targets, _DEMO_EDGES[1:])
    assert risk["level"] == "medium"
    assert risk["text"] == "Medium · order_total is exercised by tests/test_orders.py, which did not run"
    assert risk["risky_target_ids"] == []
    assert risk["understanding"] == {"reviewed": 1, "total": 2, "risky": False, "text": "1 of 2 parts reviewed"}


def test_unmeasured_callers_read_not_measured_never_low():
    plain = (_Ranked("t-total", "symbol", "app/orders.py", "order_total", 1, 5),)
    packet = _shop_packet(plain)
    packet["files"] = [{"path": "app/orders.py", "status": "modified", "hunks": [_ORDERS_HUNK]}]
    value = projection()
    value["context"] = consumer_inventory(packet, plain, current=None)
    risk = build_brief_summary(value, packet=packet, targets=plain)["risk"]
    assert risk["level"] == "not_measured" and risk["facts"] == []
    assert risk["text"] == "Not measured · could not measure callers of the changed code (no code index)"
    assert risk["not_measured"] == [
        "callers of the changed code (no code index)",
        "which tests exercise the changed code",
    ]
    assert risk["understanding"]["text"] == "0 of 1 part reviewed"


def test_measured_and_nothing_found_is_low_and_counts_every_part():
    plain = (_Ranked("t-total", "symbol", "app/orders.py", "order_total", 1, 5, state="reviewed"),)
    packet = _shop_packet(plain)
    packet["files"] = [{"path": "app/orders.py", "status": "modified", "hunks": [_ORDERS_HUNK]}]
    value = projection()
    value["context"] = consumer_inventory(packet, plain, current={"edges": [], "degraded": False})
    risk = build_brief_summary(value, packet=packet, targets=plain)["risk"]
    assert risk["level"] == "low"
    assert risk["text"] == "Low · no sensitive code, outside callers, removed safeguards or unrun tests found"
    assert risk["understanding"] == {"reviewed": 1, "total": 1, "risky": False, "text": "1 of 1 part reviewed"}


def test_low_risk_is_scoped_to_this_repository_when_another_repository_depends_on_the_change():
    plain = (_Ranked("t-total", "symbol", "app/orders.py", "order_total", 1, 5),)
    packet = _shop_packet(plain)
    packet["files"] = [{"path": "app/orders.py", "status": "modified", "hunks": [_ORDERS_HUNK]}]
    reached = [{"repo": "storefront", "hops": 1, "direct": True}]
    packet["workspace_impact"] = {"root": "/w", "repo": "shop", "reached": reached}
    value = projection()
    value["context"] = consumer_inventory(packet, plain, current={"edges": [], "degraded": False})
    risk = build_brief_summary(value, packet=packet, targets=plain)["risk"]
    # Workspace reach never sets the level; the Low line just stops reading as "nothing outside".
    assert risk["level"] == "low" and risk["facts"] == []
    assert risk["text"] == (
        "Low · no sensitive code, outside callers, removed safeguards or unrun tests found in this repository"
        " · storefront depends on what changed (see Workspace impact)"
    )
    # A repository that depends on this one only as a whole (no evidence reaches a changed path) does not count.
    reached.append({"repo": "billing", "hops": 1, "direct": False})
    risk = build_brief_summary(value, packet=packet, targets=plain)["risk"]
    assert risk["reason"].endswith(" · storefront depends on what changed (see Workspace impact)")
    reached.append({"repo": "admin", "hops": 2, "direct": True})
    risk = build_brief_summary(value, packet=packet, targets=plain)["risk"]
    assert risk["reason"].endswith(" · 2 other repositories depend on what changed (see Workspace impact)")
