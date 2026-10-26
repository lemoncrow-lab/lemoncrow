"""What a pull request review looks at: every changed code target, the diff of each, and calls sized to fit a prompt.

Unit tests for the pure pieces (``ai_coverage``, ``ai_review.coverage_units``, ``not_worth_a_look``, the relaxed
hypothesis and finding validation) and end-to-end tests through the pull request bot with a fake model, which read the
exact prompt the investigator was shown.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import ai_coverage
from lemoncrow.pro.capabilities.review.ai_review import (
    ReviewCandidate,
    _direct_hypothesis,
    _validate_hypotheses,
    coverage_units,
    general_review_targets,
    generate_candidates,
    not_worth_a_look,
    pr_candidates,
    run_independent_review,
)
from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallBudget, ModelCallUsage, StructuredExecution
from lemoncrow.pro.capabilities.review.review_priority import QueueReason, TargetPriority
from lemoncrow.pro.capabilities.review.targets import ReviewTarget
from tests.gateway.pr_bot_support import BASE, World, build_world
from tests.gateway.test_review_ai_review import _store, _target

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def target(target_id: str, path: str, *, size: int = 3, **changes: Any) -> ReviewTarget:
    base = _target(target_id=target_id, path=path, level="normal")  # "high" attention is a risk signal of its own
    return dataclasses.replace(base, symbol=f"fn_{target_id}", additions=size, deletions=0, **changes)


def with_reason(item: ReviewTarget, code: str) -> ReviewTarget:
    priority = TargetPriority(
        tier=item.priority.tier,
        attention=item.priority.attention,
        outstanding=True,
        reasons=(QueueReason(code, code.replace("_", " ")),),
    )
    return dataclasses.replace(
        item, priority=priority, structural_level="mechanical" if code.startswith("routine_") else "normal"
    )


def rows(tmp: dict[str, str]) -> dict[str, dict[str, Any]]:
    return {target_id: {"text": text} for target_id, text in tmp.items()}


# --------------------------------------------------------------------------- #
# what is worth a look
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "kind", "binary", "status", "category", "why"),
    [
        ("docs/guide.md", "hunk", False, "modified", "docs", "docs"),
        ("vendor/lib/x.go", "hunk", False, "modified", "vendor", "vendor"),
        ("api/user.pb.go", "hunk", False, "modified", "generated", "generated"),
        ("uv.lock", "dependency", False, "modified", "generated", "a dependency lockfile"),
        ("assets/logo.png", "file", True, "modified", "production", "binary"),
        ("src/old.py", "file", False, "deleted", "production", "deleted"),
        ("web/__snapshots__/app.test.tsx.snap", "hunk", False, "modified", "production", "generated"),
        ("go.sum", "hunk", False, "modified", "production", "generated"),
    ],
)
def test_generated_vendored_documentation_binary_and_deleted_files_are_not_worth_a_call(
    path: str, kind: str, binary: bool, status: str, category: str, why: str
) -> None:
    item = dataclasses.replace(target("t", path), kind=kind)  # type: ignore[arg-type]
    row = ai_coverage.FileFacts(path=path, status=status, category=category, binary=binary)
    assert not_worth_a_look(item, {path: row}) == why
    assert general_review_targets([item], {path: row}) == ()


@pytest.mark.parametrize("code", ["routine_formatting_only", "routine_comment_only", "generated_or_vendor"])
def test_a_formatting_only_or_comment_only_change_is_not_worth_a_call(code: str) -> None:
    item = with_reason(target("t", "src/pricing.py"), code)
    assert not_worth_a_look(item) != ""
    assert general_review_targets([item]) == ()


def test_code_that_only_removes_lines_in_a_file_that_stays_is_worth_a_look() -> None:
    """A removed guard leaves nothing at the head, and is exactly what a reviewer is there to notice."""

    removed = dataclasses.replace(
        target("t", "src/pricing.py"), spans=(dataclasses.replace(target("t", "x").spans[0], side="old"),)
    )
    assert not_worth_a_look(removed) == ""
    assert general_review_targets([removed]) == (removed,)


def test_a_keyword_in_the_name_of_a_lockfile_or_a_document_is_not_a_risk_but_a_rule_still_applies() -> None:
    """``package-lock.json`` holds the word ``lock``; before this it was asked a concurrency question."""

    lock = dataclasses.replace(target("l", "package-lock.json"), kind="dependency")
    doc = target("d", "docs/auth.md")
    code = target("c", "src/auth.py")
    file_facts = {
        "package-lock.json": ai_coverage.FileFacts("package-lock.json", category="generated"),
        "docs/auth.md": ai_coverage.FileFacts("docs/auth.md", category="docs"),
    }
    plain = generate_candidates([lock, doc, code])
    assert {t for c in plain for t in c.target_ids} == {"l", "d", "c"}, "the Reader's triage is unchanged"
    kept = pr_candidates([lock, doc, code], facts=file_facts)
    assert {t for c in kept for t in c.target_ids} == {"c"}


@pytest.mark.parametrize(
    ("path", "category"),
    [
        ("src/pricing.py", "production"),
        ("lib/pricing.rb", "production"),
        ("server/Pricing.java", "production"),
        ("pkg/pricing/pricing.go", "production"),
        ("web/pricing.ts", "production"),
        ("tests/test_pricing.py", "test"),
        ("deploy/values.yaml", "config"),
    ],
)
def test_ordinary_changed_code_tests_and_configuration_are_worth_a_look(path: str, category: str) -> None:
    item = target("t", path)
    row = ai_coverage.FileFacts(path=path, category=category)
    assert not_worth_a_look(item, {path: row}) == ""
    assert general_review_targets([item], {path: row}) == (item,)


def test_general_targets_are_the_changed_code_that_has_no_risk_signal_of_its_own() -> None:
    signal = target("a", "src/auth.py")
    plain = target("b", "src/pricing.py")
    candidates = generate_candidates([signal, plain])
    assert {t for c in candidates for t in c.target_ids} == {"a"}
    assert general_review_targets([signal, plain], exclude={"a"}) == (plain,)


# --------------------------------------------------------------------------- #
# the diff
# --------------------------------------------------------------------------- #

GUARD_HUNK = ai_coverage.HunkText(
    header="@@ -10,6 +10,5 @@ def withdraw(account, amount):",
    body=" def withdraw(account, amount):\n-    if amount > account.balance:\n-        raise InsufficientFunds()\n"
    "     account.balance -= amount\n     return account.balance\n",
    old_start=10,
    old_lines=6,
    new_start=10,
    new_lines=5,
)


def test_the_diff_row_carries_the_removed_lines_of_the_hunks_a_target_owns() -> None:
    other = ai_coverage.HunkText(header="@@ -90,2 +89,2 @@", body="-x = 1\n+x = 2\n", new_start=89, new_lines=2)
    view = ai_coverage.DiffView({"src/bank.py": ai_coverage.FileFacts("src/bank.py", hunks=(GUARD_HUNK, other))})
    owner = dataclasses.replace(target("t", "src/bank.py"), hunk_ordinals=(0,))
    (row,) = view.rows([owner])
    assert row["path"] == "src/bank.py" and row["status"] == "modified"
    assert "-    if amount > account.balance:" in row["diff"] and "-        raise InsufficientFunds()" in row["diff"]
    assert "@@ -10,6 +10,5 @@" in row["diff"]
    assert "x = 2" not in row["diff"], "a hunk the target does not own is not shown"
    # A file-level target owns every hunk.
    whole = dataclasses.replace(owner, hunk_ordinals=())
    assert "x = 2" in view.rows([whole])[0]["diff"]


def test_an_added_file_has_no_diff_row_text_and_a_missing_one_says_so() -> None:
    view = ai_coverage.DiffView(
        {"src/new.py": ai_coverage.FileFacts("src/new.py", status="added", hunks=(GUARD_HUNK,))}
    )
    assert view.rows([target("t", "src/new.py")]) == [{"path": "src/new.py", "status": "added", "diff": ""}]
    assert view.rows([target("t", "src/other.py")])[0]["diff"] == "(the diff is not available)"


def test_a_diff_is_cut_at_a_size_and_says_how_many_hunks_were_left_out() -> None:
    big = [
        ai_coverage.HunkText(header=f"@@ -{n},1 +{n},1 @@", body="+" + "x" * 6000 + "\n", new_start=n, new_lines=1)
        for n in range(1, 6)
    ]
    view = ai_coverage.DiffView({"src/big.py": ai_coverage.FileFacts("src/big.py", hunks=tuple(big))})
    text = view.rows([dataclasses.replace(target("t", "src/big.py"), hunk_ordinals=())])[0]["diff"]
    assert len(text) <= ai_coverage.MAX_DIFF_CHARS + 200
    assert "more hunks of this file not shown" in text


def test_a_file_whose_hunks_carry_no_body_gets_a_diff_computed_from_its_two_sides() -> None:
    bare = ai_coverage.HunkText(header="@@ -1,3 +1,2 @@", body="", old_start=1, old_lines=3, new_start=1, new_lines=2)

    def texts(path: str, side: str) -> str | None:
        return {"old": "a = 1\nif ok:\n    run()\n", "new": "a = 1\nrun()\n"}[side]

    view = ai_coverage.DiffView({"src/x.py": ai_coverage.FileFacts("src/x.py", hunks=(bare,))}, texts=texts)
    text = view.rows([target("t", "src/x.py")])[0]["diff"]
    assert "-if ok:" in text and "-    run()" in text and "+run()" in text


def test_the_window_of_a_shown_diff_is_the_new_side_of_its_hunks() -> None:
    view = ai_coverage.DiffView({"src/bank.py": ai_coverage.FileFacts("src/bank.py", hunks=(GUARD_HUNK,))})
    assert view.window("src/bank.py", [dataclasses.replace(target("t", "src/bank.py"), hunk_ordinals=(0,))]) == (10, 14)
    assert view.window("src/none.py", [target("t", "src/none.py")]) is None


# --------------------------------------------------------------------------- #
# batches
# --------------------------------------------------------------------------- #


def test_chunks_keep_order_and_never_exceed_the_room_except_for_one_big_item() -> None:
    cost = lambda items: sum(items)  # noqa: E731
    assert ai_coverage.chunks([4, 4, 4, 4, 4], cost, 10) == [[4, 4], [4, 4], [4]]
    assert ai_coverage.chunks([4, 30, 4], cost, 10) == [[4], [30], [4]], "one big item travels alone"
    assert ai_coverage.chunks([], cost, 10) == []


def test_pack_places_groups_first_fit_in_the_order_given() -> None:
    cost = lambda groups: sum(len(g) for g in groups)  # noqa: E731
    bins = ai_coverage.pack([[1] * 6, [1] * 6, [1] * 3, [1] * 3], cost, 10)
    assert [[len(g) for g in b] for b in bins] == [[6, 3], [6, 3]]


def test_rounds_run_the_first_wave_whole_and_stop_at_the_first_clean_round_after_it() -> None:
    ran: list[int] = []

    def run(item: int) -> int:
        ran.append(item)
        return item

    clean = lambda _result: True  # noqa: E731
    results = ai_coverage.run_rounds(list(range(20)), run, empty=clean)
    assert results == list(range(9)), "six together, then one round of three that found nothing"
    assert sorted(ran) == list(range(9)), "nothing below the clean round was started"
    # Fewer items than a wave are run whole, whatever they find.
    assert ai_coverage.run_rounds(list(range(4)), run, empty=clean) == [0, 1, 2, 3]
    assert ai_coverage.run_rounds([], run, empty=clean) == []
    assert ai_coverage.run_rounds(list(range(7)), run, empty=clean) == list(range(7)), "a short last round ends it"


def test_a_round_with_one_result_that_is_not_empty_goes_on_to_the_next() -> None:
    found = {7}  # the second round holds the only finding
    results = ai_coverage.run_rounds(list(range(20)), lambda item: item, empty=lambda result: result not in found)
    assert results == list(range(12)), "round two (6 7 8) had a finding, round three (9 10 11) had none"
    everything = ai_coverage.run_rounds(list(range(20)), lambda item: item, empty=lambda _result: False)
    assert everything == list(range(20)), "while every round finds something the whole change is covered"


def test_the_rounds_never_stop_when_told_not_to_and_leave_the_protected_items_to_the_first_wave() -> None:
    assert ai_coverage.run_rounds(
        list(range(20)), lambda item: item, empty=lambda _r: True, stop_on_clean=False
    ) == list(range(20))
    # Eight leading items are not to be skipped (a repository's own rules): they all run before anything is judged.
    results = ai_coverage.run_rounds(list(range(30)), lambda item: item, empty=lambda _r: True, protected=8)
    assert results == list(range(11))


def test_rounds_run_the_first_wave_together_then_three_at_a_time_in_the_callers_context() -> None:
    import contextvars
    import threading
    import time

    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="unset")
    marker.set("caller")
    lock = threading.Lock()
    live = 0
    peak: list[int] = []
    seen: list[str] = []

    def run(_item: int) -> int:
        nonlocal live
        with lock:
            live += 1
            peak.append(live)
            seen.append(marker.get())
        time.sleep(0.05)
        with lock:
            live -= 1
        return 0

    ai_coverage.run_rounds(list(range(12)), run, empty=lambda _r: False)
    assert max(peak) == ai_coverage.FIRST_WAVE, "the first wave runs together; no round runs more than a wave"
    assert set(seen) == {"caller"}, "each call runs in a copy of the caller's context"
    peak.clear()
    ai_coverage.run_rounds(list(range(3)), run, empty=lambda _r: False, first=0, size=3)
    assert max(peak) == 3


def test_room_leaves_space_for_a_context_pack_but_never_less_than_a_floor() -> None:
    assert ai_coverage.room_tokens() == ai_coverage.BATCH_INPUT_TOKENS - ai_coverage.PROMPT_OVERHEAD_TOKENS
    assert ai_coverage.room_tokens(context_tokens=3000) == ai_coverage.room_tokens() - 3000
    assert ai_coverage.room_tokens(context_tokens=10**6) == ai_coverage.MIN_ROOM_TOKENS


def units_for(
    specs: Sequence[tuple[str, str, int]],
    *,
    candidates: Sequence[ReviewCandidate] = (),
    category: dict[str, str] | None = None,
    room: int = 4000,
) -> tuple[ReviewCandidate, ...]:
    """``specs`` are ``(target id, path, characters of code)``; the candidates are the risk signals among them."""

    targets = {tid: target(tid, path, size=max(n // 100, 1)) for tid, path, n in specs}
    source = rows({tid: "x" * n for tid, _path, n in specs})
    paths = {tid: path for tid, path, _n in specs}
    categories = category or {}
    file_facts = {
        path: ai_coverage.FileFacts(path=path, category=categories.get(path, "production"))
        for path in set(paths.values())
    }
    general = general_review_targets(
        list(targets.values()), file_facts, exclude={t for c in candidates for t in c.target_ids}
    )
    return coverage_units(
        candidates,
        general,
        targets=targets,
        source=source,
        diffs=ai_coverage.DiffView(file_facts),
        facts=file_facts,
        room=room,
    )


def signal(category: str, *ids: str, score: int = 100, rule_refs: tuple[str, ...] = ()) -> ReviewCandidate:
    return ReviewCandidate(category, ids, score, ("a reason",), ", ".join(ids), rule_refs)  # type: ignore[arg-type]


def test_several_small_files_travel_in_one_call_and_a_big_one_is_cut_into_several() -> None:
    small = units_for([(f"s{i}", f"src/small{i}.py", 400) for i in range(6)])
    assert len(small) == 1 and small[0].general and len(small[0].target_ids) == 6
    big = units_for([(f"b{i}", "src/big.py", 3000) for i in range(5)])
    assert len(big) > 1 and all(unit.general for unit in big)
    assert [t for unit in big for t in unit.target_ids] == [f"b{i}" for i in range(5)], "line order is kept"
    assert len({t for unit in big for t in unit.target_ids}) == 5, "no target is lost or repeated"


def test_a_risk_signal_keeps_its_own_question_and_plain_code_fills_the_room_of_its_call() -> None:
    units = units_for(
        [("a", "src/auth.py", 300), ("b", "src/auth.py", 300), ("c", "src/pricing.py", 300)],
        candidates=[signal("security", "a")],
    )
    (unit,) = units
    assert (unit.category, set(unit.target_ids), unit.general) == ("security", {"a", "b", "c"}, False)
    assert unit.target_ids[:2] == ("a", "b"), "the file's own plain code comes right after the signal"
    # Too big to share: the plain file gets a general call of its own, ranked after the signal's.
    apart = units_for(
        [("a", "src/auth.py", 300), ("c", "src/pricing.py", 6000)], candidates=[signal("security", "a")], room=1000
    )
    assert [(unit.target_ids, unit.general) for unit in apart] == [(("a",), False), (("c",), True)]


def test_small_files_with_a_risk_signal_share_a_call_whatever_the_signal_and_the_call_names_them_all() -> None:
    candidates = [
        signal("security", "a", score=100),
        signal("security", "b", score=100),
        signal("security", "c", score=100),
        signal("performance", "d", score=72),
    ]
    units = units_for(
        [
            ("a", "src/auth.py", 300),
            ("b", "src/token.py", 300),
            ("c", "src/crypto.py", 300),
            ("d", "src/cache.py", 300),
        ],
        candidates=candidates,
    )
    (unit,) = units
    assert set(unit.target_ids) == {"a", "b", "c", "d"} and not unit.general
    assert (unit.category, unit.also, unit.score) == ("security", ("performance",), 100)
    hypothesis = _direct_hypothesis(unit, whole_change=True)
    assert hypothesis.also == ("performance",) and hypothesis.to_wire()["also_categories"] == ["performance"]
    assert "performance" in hypothesis.failure_mode.lower() or "hot/query path" in hypothesis.failure_mode
    # A rule's question is never merged into it.
    ruled = units_for(
        [("a", "src/auth.py", 300), ("b", "src/token.py", 300)],
        candidates=[signal("critical_rule", "a", rule_refs=("rule:critical:r",)), signal("security", "b")],
    )
    assert sorted(unit.target_ids for unit in ruled) == [("a",), ("b",)]


def test_signal_files_too_big_to_share_a_call_get_one_each() -> None:
    candidates = [signal("security", "a"), signal("security", "b")]
    units = units_for([("a", "src/auth.py", 14000), ("b", "src/token.py", 14000)], candidates=candidates, room=4000)
    assert [unit.target_ids for unit in units] == [("a",), ("b",)]


def test_a_rule_question_is_never_diluted_with_plain_code() -> None:
    rule = signal("critical_rule", "a", rule_refs=("rule:r",))
    units = units_for([("a", "src/auth.py", 300), ("b", "src/auth.py", 300)], candidates=[rule])
    assert next(unit for unit in units if not unit.general).target_ids == ("a",)


def test_the_order_spends_a_short_budget_on_production_code_before_test_code() -> None:
    """Rule, then a risk signal in production code, then plain production code, then configuration, then tests."""

    candidates = [
        signal("critical_rule", "r", rule_refs=("rule:critical:r",)),
        signal("security", "s", score=100),
        signal("security", "ts", score=100),
    ]
    units = units_for(
        [
            ("r", "src/ruled.py", 100),
            ("s", "src/auth.py", 100),
            ("ts", "tests/test_auth.py", 100),
            ("g", "src/pricing.py", 100),
            ("c", "deploy/values.yaml", 100),
            ("t", "tests/test_pricing.py", 100),
        ],
        candidates=candidates,
        category={"tests/test_auth.py": "test", "tests/test_pricing.py": "test", "deploy/values.yaml": "config"},
        room=200,  # one tiny target fills a call, so the order of the calls is the order of the work
    )
    assert [unit.target_ids for unit in units] == [("r",), ("s",), ("g",), ("c",), ("ts",), ("t",)]
    assert [unit.general for unit in units] == [False, False, True, True, False, True]


def test_the_larger_change_is_looked_at_first_within_a_tier() -> None:
    bigger = dataclasses.replace(target("big", "src/b.py"), additions=50)
    smaller = dataclasses.replace(target("small", "src/a.py"), additions=2)
    only = coverage_units(
        [],
        [smaller, bigger],
        targets={"small": smaller, "big": bigger},
        source=rows({"small": "x" * 4000, "big": "x" * 4000}),
        diffs=ai_coverage.DiffView({}),
        facts={},
        room=1500,  # one of them fills a call
    )
    assert [unit.target_ids for unit in only] == [("big",), ("small",)]


def test_a_target_with_two_signals_costs_one_call_and_targets_of_one_file_share_it() -> None:
    candidates = [signal("security", "a", score=100), signal("correctness", "a", score=80), signal("security", "b")]
    units = units_for([("a", "src/auth.py", 100), ("b", "src/auth.py", 100)], candidates=candidates)
    assert [(unit.category, unit.target_ids) for unit in units] == [("security", ("a", "b"))]


# --------------------------------------------------------------------------- #
# what the model proposes and answers
# --------------------------------------------------------------------------- #


def hypothesis_row(category: str, *ids: str) -> dict[str, Any]:
    return {
        "category": category,
        "target_ids": list(ids),
        "failure_mode": "Money can be taken twice",
        "investigation_rationale": "the balance check changed",
        "priority_rationale": "it moves money",
        "evidence_needed": "trace the withdrawal",
        "rule_refs": [],
    }


def test_a_proposed_hypothesis_in_another_category_on_a_changed_target_is_kept_for_a_pull_request() -> None:
    """On #50 the model proposed categories the triage had not raised for those targets, all were dropped, and the
    fallback spent the review on test functions."""

    candidates = [signal("security", "a"), signal("performance", "b", score=72)]
    raw = {"hypotheses": [hypothesis_row("correctness", "a"), hypothesis_row("concurrency_state", "b", "c")]}
    assert (
        _validate_hypotheses(raw, candidates, limit=4) == ()
    ), "the Reader still holds the model to the triage's pairing"
    kept = _validate_hypotheses(raw, candidates, limit=4, open_targets={"a", "b", "c"})
    assert [(h.category, h.target_ids) for h in kept] == [("correctness", ("a",)), ("concurrency_state", ("b", "c"))]
    # The target must still be a real changed target of this review, and a rule hypothesis needs its rule.
    assert (
        _validate_hypotheses(
            {"hypotheses": [hypothesis_row("correctness", "ghost")]}, candidates, limit=4, open_targets={"a"}
        )
        == ()
    )
    assert (
        _validate_hypotheses(
            {"hypotheses": [hypothesis_row("critical_rule", "c")]}, candidates, limit=4, open_targets={"c"}
        )
        == ()
    )


# --------------------------------------------------------------------------- #
# through run_independent_review (the investigator's own prompt)
# --------------------------------------------------------------------------- #


def investigator(seen: list[dict[str, Any]], **answer: Any):  # type: ignore[no-untyped-def]
    def execute(**kwargs: Any) -> StructuredExecution:
        import json

        seen.append(
            {"system": kwargs["system"], "prompt": json.loads(kwargs["prompt"]), "schema": kwargs["output_schema"]}
        )
        result = {
            "status": "not_confirmed",
            "category": "correctness",
            "target_ids": [],
            "title": "",
            "explanation": "",
            "consequence": "",
            "evidence_refs": [],
            "rule_refs": [],
            "severity": "low",
            "lines": None,
            "replacement": None,
            "other_findings": [],
            "prior_findings": [],
            **answer,
        }
        return StructuredExecution(result=result, driver="openai", model="review-model", usage=ModelCallUsage(calls=1))  # type: ignore[arg-type]

    return execute


def test_quick_looks_at_the_highest_ranked_work_with_one_call_and_leaves_the_rest_pending(tmp_path: Path) -> None:
    store, revision = _store(tmp_path)
    seen: list[dict[str, Any]] = []
    result = run_independent_review(
        store=store,
        revision=revision,
        targets=(
            _target(target_id="target-plain", path="src/plain.py", level="normal"),
            _target(target_id="target-auth", path="src/auth.py"),
        ),
        repo_root=tmp_path,
        effort="quick",
        driver="openai",
        model="review-model",
        budget=ModelCallBudget(max_calls=1, max_review_calls=20),
        structured_executor=investigator(seen),
        pr_fields=True,
    )
    assert len(seen) == 1, "quick is one call"
    assert result.checked_target_ids == ("target-auth", "target-plain"), "both were set out to be looked at"
    first = seen[0]["prompt"]["hypothesis"]["value"]["target_ids"]
    assert first[0] == "target-auth", "the risk signal ranks first, and the plain code rides in its call"


def test_the_investigators_prompt_carries_the_diff_and_the_pr_rules_for_every_pr_call(tmp_path: Path) -> None:
    store, revision = _store(tmp_path)
    seen: list[dict[str, Any]] = []
    run_independent_review(
        store=store,
        revision=revision,
        targets=(_target(target_id="target-plain", path="src/plain.py", level="normal"),),
        repo_root=tmp_path,
        effort="auto",
        driver="openai",
        model="review-model",
        budget=ModelCallBudget(max_calls=1, max_review_calls=20),
        structured_executor=investigator(seen),
        pr_fields=True,
    )
    (call,) = seen
    assert "other_findings" in call["schema"]["properties"]
    assert "general review of changed code" in call["system"] and "other_findings" in call["system"]
    hypothesis = call["prompt"]["hypothesis"]["value"]
    assert hypothesis["general"] is True and hypothesis["category"] == "correctness"


# --------------------------------------------------------------------------- #
# the whole pull request, end to end
# --------------------------------------------------------------------------- #

BANK_OLD = (
    "def withdraw(balance, amount):\n"
    "    if amount > balance:\n"
    "        raise ValueError('insufficient funds')\n"
    "    return balance - amount\n"
)
BANK_NEW = "def withdraw(balance, amount):\n    return balance - amount\n"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return build_world(tmp_path, monkeypatch, {**BASE, "src/bank.py": BANK_OLD})


def test_ordinary_changed_code_with_no_keyword_is_investigated_and_what_is_found_in_it_is_reported(
    world: World,
) -> None:
    """Before this, ``bank.py`` matched no marker and the review said "No must-fix findings" having asked nothing."""

    world.origin.push({"src/bank.py": BANK_NEW + "# BUG: lets a withdrawal overdraw the account\n"})
    report = world.review()
    (call,) = world.model.calls
    assert call["paths"] == ["src/bank.py"] and call["general"] is True
    assert [finding.title for finding in report.findings] == ["lets a withdrawal overdraw the account"]
    assert report.findings[0].severity == "p1", "a finding only the model stands behind is at most should-fix"


def test_a_removed_guard_is_in_the_diff_the_investigator_is_shown(world: World) -> None:
    """A guard the change took away is not in the new text. Only the diff shows it."""

    world.origin.push(
        {"src/bank.py": "def withdraw(balance, amount):\n    fee = 1\n    return balance - amount - fee\n"}
    )
    world.review()
    (call,) = world.model.calls
    (row,) = call["diff"]
    assert row["path"] == "src/bank.py" and row["status"] == "modified"
    assert "-    if amount > balance:" in row["diff"]
    assert "-        raise ValueError('insufficient funds')" in row["diff"]
    assert "+    fee = 1" in row["diff"]
    shown = call["prompt"]["snapshot_source"]["value"][call["targets"][0]]
    assert shown["side"] == "new" and "ValueError" not in shown["text"], "the new text has no trace of the guard"
    assert "change_diff is this pull request's own unified diff" in call["system"]
    assert "general review of changed code" in call["system"]


def test_a_change_that_only_removes_a_guard_is_investigated_with_the_removed_code_and_its_diff(world: World) -> None:
    world.origin.push({"src/bank.py": BANK_NEW})
    world.review()
    (call,) = world.model.calls
    shown = call["prompt"]["snapshot_source"]["value"][call["targets"][0]]
    assert shown["side"] == "old" and "ValueError" in shown["text"], "the removed code is what the target is"
    assert "-        raise ValueError('insufficient funds')" in call["diff"][0]["diff"]
    assert 'A snapshot_source entry whose side is "old" is code the change removes' in call["system"]


def test_a_finding_the_model_files_under_another_category_than_the_question_is_kept(world: World) -> None:
    """A question about a cache that turns up a wrong result is a wrong result, not an "unknown"."""

    world.origin.push(
        {
            "src/cache.py": "CACHE = {}\n\n\ndef remember(key, value):\n    CACHE[key] = [value]  # BUG: drops the old entry\n"
        }
    )
    world.model.category["drops the old entry"] = "correctness"
    report = world.review()
    call = next(call for call in world.model.calls if "src/cache.py" in call["paths"])
    assert call["category"] == "performance" and not call["general"], "the question was the cache marker's"
    assert [(finding.title, finding.category) for finding in report.findings] == [
        ("drops the old entry", "correctness")
    ]


def test_a_second_defect_in_the_same_targets_is_reported_as_a_finding_of_its_own(world: World) -> None:
    world.origin.push(
        {"src/bank.py": BANK_NEW + "# BUG: lets a withdrawal overdraw the account\n# BUG: accepts a negative amount\n"}
    )
    report = world.review()
    assert len(world.model.calls) == 1
    assert sorted(finding.title for finding in report.findings) == [
        "accepts a negative amount",
        "lets a withdrawal overdraw the account",
    ]


def test_user_auth_py_is_asked_a_security_question_like_auth_py(world: World) -> None:
    world.origin.push({"src/user_auth.py": "def login(user):\n    return user is not None\n"})
    world.review()
    (call,) = world.model.calls
    assert (call["paths"], call["category"], call["general"]) == (["src/user_auth.py"], "security", False)


def test_small_plain_files_share_one_call_and_every_one_of_them_is_shown(world: World) -> None:
    files = {f"src/pkg/m{i}.py": f"def f{i}(x):\n    return x + {i}\n" for i in range(8)}
    world.origin.push(files)
    report = world.review()
    assert len(world.model.calls) == 1, "eight small files are one call, not eight"
    assert world.model.files_seen() == sorted(files)
    assert {row["path"] for row in world.model.calls[0]["diff"]} == set(files)
    assert report.verdict is not None and report.verdict.headline == "No must-fix findings"


def test_documentation_lockfiles_and_generated_code_cost_no_call(world: World) -> None:
    world.origin.push(
        {
            "README.md": "# Widgets\n\nNow with more widgets.\n",
            "docs/guide.md": "Use the widgets.\n",
            "package-lock.json": '{"lockfileVersion": 3, "packages": {}}\n',
            "api/service.pb.go": "// Code generated by protoc-gen-go. DO NOT EDIT.\npackage api\n",
            "vendor/lib/x.py": "VALUE = 2\n",
        }
    )
    report = world.review()
    assert world.model.calls == []
    assert report.verdict is not None and report.verdict.headline == "No must-fix findings"


def test_a_target_whose_content_did_not_change_is_not_asked_again_on_the_next_push(world: World) -> None:
    world.origin.push({"src/bank.py": BANK_NEW, "src/pkg/a.py": "def a():\n    return 1\n"})
    world.review()
    assert world.model.files_seen() == ["src/bank.py", "src/pkg/a.py"]
    world.model.reset()
    world.origin.push({"src/pkg/b.py": "def b():\n    return 2\n"}, "another file")
    world.review()
    assert world.model.files_seen() == ["src/pkg/b.py"], "the files reviewed before are carried, not asked again"


def test_a_short_budget_is_spent_on_production_code_before_test_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_REVIEW_INTELLIGENCE_MAX_CALLS", "1")
    world = build_world(
        tmp_path, monkeypatch, {**BASE, "src/bank.py": BANK_OLD, "tests/test_bank.py": "def test_a():\n    assert 1\n"}
    )
    world.origin.push({"tests/test_bank.py": "def test_a():\n    assert 2\n", "src/bank.py": BANK_NEW})
    report = world.review()
    assert world.model.files_seen() == ["src/bank.py"]
    assert report.verdict is not None and report.verdict.headline == "Partially reviewed: 1 of 2 changed areas examined"


def test_a_risk_signal_is_a_lens_on_the_general_question_in_a_pull_request_and_the_whole_question_in_the_reader(
    world: World,
) -> None:
    """A model asked only about performance answers about performance, and a wrong result it noticed on the way never
    reaches the page. In a pull request the specific concern is where to look first; the question stays the general one.
    """

    world.origin.push({"src/cache.py": "CACHE = {}\n\n\ndef remember(key, value):\n    CACHE[key] = [value]\n"})
    world.review()
    call = next(call for call in world.model.calls if "src/cache.py" in call["paths"])
    hypothesis = call["prompt"]["hypothesis"]["value"]
    assert call["category"] == "performance" and not call["general"]
    assert "may introduce a defect" in hypothesis["failure_mode"], "the general question first"
    assert "specific concern" in hypothesis["failure_mode"] and "unbounded" in hypothesis["failure_mode"]
    assert "also read the change as a careful reviewer would" in call["system"].replace("\n", " ")
    # The Reader's hypothesis for the same candidate is exactly the specific one it always was.
    candidate = ReviewCandidate("performance", ("t",), 72, ("a reason",), "src/cache.py:1-2")
    reader = _direct_hypothesis(candidate)
    assert "may introduce a defect" not in reader.failure_mode and reader.failure_mode.startswith("The identifiable")
    assert _direct_hypothesis(candidate, whole_change=True).id != reader.id


def test_files_with_different_risk_signals_share_a_call_and_each_risk_still_reads_examined_for_its_own_category(
    world: World,
) -> None:
    world.origin.push(
        {
            "src/auth.py": "def authorize(user):\n    return bool(user) and user.is_admin\n",
            "src/cache.py": "CACHE = {}\n\n\ndef remember(key, value):\n    CACHE[key] = (value,)\n",
        }
    )
    report = world.review()
    (call,) = world.model.calls
    assert sorted(call["paths"]) == ["src/auth.py", "src/cache.py"] and call["category"] == "security"
    assert call["prompt"]["hypothesis"]["value"]["also_categories"] == ["performance"]
    body = world.github.summary_comments()[0]["body"]
    assert report.verdict is not None and report.verdict.headline == "No must-fix findings"
    assert "Risks examined: 2 of 2" in body and "not investigated yet" not in body
    rows = [row for row in body.split("\n") if row.startswith("- **Examined, nothing found**")]
    assert any("`src/auth.py" in row and "security" in row for row in rows)
    assert any("`src/cache.py" in row and "performance" in row for row in rows)


def test_plain_production_code_rides_in_a_call_that_has_room_and_only_otherwise_gets_one_of_its_own() -> None:
    with_signal = units_for(
        [("s", "src/auth.py", 300), ("p", "src/pricing.py", 300)], candidates=[signal("security", "s")]
    )
    assert [(set(unit.target_ids), unit.general) for unit in with_signal] == [({"s", "p"}, False)]
    without = units_for([("p", "src/pricing.py", 300), ("q", "src/billing.py", 300)])
    assert [(set(unit.target_ids), unit.general) for unit in without] == [({"p", "q"}, True)]
    # Configuration and test code never ride in a production call: they are ranked after it.
    mixed = units_for(
        [("s", "src/auth.py", 300), ("c", "deploy/values.yaml", 300), ("t", "tests/test_x.py", 300)],
        candidates=[signal("security", "s")],
        category={"deploy/values.yaml": "config", "tests/test_x.py": "test"},
    )
    assert [set(unit.target_ids) for unit in mixed] == [{"s"}, {"c"}, {"t"}]
