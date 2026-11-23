"""RR-BATCHPERF: the batch guard reads a large hunk once, not once per part, and gives the same verdicts as before.

The guard used to compare a hunk's whole token stream with ``difflib`` once per part of that hunk, with two
quadratic scans on top, so a 300-function change took more than 28 minutes to open. This file pins the shape, not
the clock (the work is counted, never timed), and proves the verdicts against ``review_batch_guard_pre_perf.py``,
the frozen guard from before the change:

* a hunk is compared once however many parts share it, and a file is read once per side;
* over randomized Python, TypeScript and JavaScript programs (moved statements, decorators, string edits, wrapped
  calls), every part gets the same guard as before; with the comparison budget cut to almost nothing, every
  divergence is ``fail-safe`` (a part the old guard offered is now held) and there is no ``fail-open`` one;
* at the budget a comparison finishes, one step past it the part is held for individual review.
"""

from __future__ import annotations

import random
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import batch_guard as new_module
from lemoncrow.pro.capabilities.review import batch_guard_tree as tree
from lemoncrow.pro.capabilities.review.batch_guard import UNVERIFIABLE, batch_guards
from lemoncrow.pro.capabilities.review.batch_guard import guard_for_target as new_guard
from tests.gateway import review_batch_guard_pre_perf as old_module
from tests.gateway import review_batch_guard_tree_pre_p10_2 as old_tree
from tests.gateway.test_review_batch_guard import _hunks, _spans, _target
from tests.gateway.test_review_batch_guard_differential import SEED, _body, _clone, _diff_hunks, _lines, _mutate

SAMPLES = 260  # per language
PARTS_PER_PROGRAM = 6  # single changed lines, each owned by a part of its own, plus one part owning everything


def _function(index: int) -> str:
    return f"def fn_{index}(value):\n    return value + {index}\n\n\n"


def _logger(index: int) -> str:
    return f"def fn_{index}(value):\n    log('start {index}')\n    return value\n\n\n"


def _large_hunk(count: int, arithmetic: bool = True) -> tuple[list[dict[str, Any]], str, str]:
    """``count`` look-alike functions, every one changed: one hunk. ``arithmetic`` or only a message edited."""

    base = "".join((_function if arithmetic else _logger)(i) for i in range(count))
    head = base.replace("return value + ", "return value * ") if arithmetic else base.replace("start", "begin")
    return _diff_hunks(base.split("\n"), head.split("\n")), base, head


def _one_part_per_change(hunks: list[dict[str, Any]], path: str) -> list[Any]:
    base = _target(hunks, path)
    return [replace(base, target_id=f"target:{path}:{n}", spans=(span,)) for n, span in enumerate(_spans(hunks))]


# ----- the work is counted, not timed ----- #


def _matchers_built(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    built: list[int] = []
    original = new_module._BudgetMatcher.__init__

    def counting(self: Any, old: Any, new: Any) -> None:
        built.append(len(old))
        original(self, old, new)

    monkeypatch.setattr(new_module._BudgetMatcher, "__init__", counting)
    return built


def test_a_hunk_is_compared_once_however_many_parts_share_it(monkeypatch: pytest.MonkeyPatch) -> None:
    built = _matchers_built(monkeypatch)
    counts = {}
    for functions in (8, 24):
        hunks, _, _ = _large_hunk(functions, arithmetic=False)
        assert len(hunks) == 1, "the generated change is one hunk"
        parts = _one_part_per_change(hunks, "app/large.py")
        assert len(parts) >= 2 * functions
        before = len(built)
        entry = {"path": "app/large.py", "status": "modified", "hunks": hunks}
        cache: dict[str, Any] = {}
        with new_module._scope():
            for part in parts:
                new_guard(part, entry, None, cache)
        counts[functions] = len(built) - before
    # Parts tripled; the comparisons did not grow with them (the old guard made one per part and per stage).
    assert counts[8] == counts[24] <= 3, counts


def test_each_file_is_read_once_per_side_however_many_parts_it_has() -> None:
    hunks, base, head = _large_hunk(12, arithmetic=False)
    parts = [
        replace(part, answer=type("A", (), {"category": "quick_check"})())
        for part in _one_part_per_change(hunks, "a.py")
    ]
    reads: list[tuple[str, str]] = []

    def read(path: str, side: str) -> str:
        reads.append((path, side))
        return base if side == "old" else head

    packet = {"files": [{"path": "a.py", "status": "modified", "hunks": hunks}]}
    verdicts = batch_guards(packet, parts, read)
    assert len(verdicts) == len(parts) >= 24
    assert Counter(reads) == {("a.py", "old"): 1, ("a.py", "new"): 1}


def test_a_logical_line_is_built_once_however_many_parts_read_it() -> None:
    hunks, _, _ = _large_hunk(10, arithmetic=False)
    entry = {"path": "a.py", "status": "modified", "hunks": hunks}
    cache: dict[str, Any] = {}
    parts = _one_part_per_change(hunks, "a.py")
    with new_module._scope() as run:
        new_guard(parts[0], entry, None, cache)
        built = len(run.lines)
        for part in parts[1:]:
            new_guard(part, entry, None, cache)
        # Only the lines a part owns differ between parts: a handful of new keys per part, not a copy of the hunk.
        assert len(run.lines) - built <= 8 * len(parts)


def test_the_large_review_guard_is_bounded_on_a_medium_input() -> None:
    """A generous clock on top of the counts: 120 look-alike functions took minutes before, well under 30 s now."""

    hunks, base, head = _large_hunk(120)
    entry = {"path": "a.py", "status": "modified", "hunks": hunks}
    parts = _one_part_per_change(hunks, "a.py")
    started = time.perf_counter()
    cache: dict[str, Any] = {}
    with new_module._scope():
        for part in parts:
            new_guard(part, entry, lambda _path, side: base if side == "old" else head, cache)
    assert time.perf_counter() - started < 30


# ----- the bound, at and past ----- #


def _big_pair(count: int) -> tuple[list[str], list[str]]:
    hunks, _, _ = _large_hunk(count)
    old = [
        tok
        for tok in new_module._flatten(
            new_module._side([(t[1:], False) for t in hunks[0]["patch"].split("\n") if t[:1] in " -"], "python")
        )
    ]
    new = [
        tok
        for tok in new_module._flatten(
            new_module._side([(t[1:], False) for t in hunks[0]["patch"].split("\n") if t[:1] in " +"], "python")
        )
    ]
    return [t.key for t in old], [t.key for t in new]


def test_a_comparison_at_the_budget_finishes_and_one_step_past_it_holds_the_part(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old, new = _big_pair(30)
    matcher = new_module._BudgetMatcher(old, new)
    expected = matcher.get_opcodes()
    spent = matcher._spent
    assert spent > 1000
    monkeypatch.setattr(new_module, "_MATCH_BUDGET", spent)
    assert new_module._opcodes_of(tuple(old), tuple(new)) == tuple(expected), "the same blocks difflib finds"
    monkeypatch.setattr(new_module, "_MATCH_BUDGET", spent - 1)
    assert new_module._opcodes_of(tuple(old), tuple(new)) is None
    with pytest.raises(new_module._Unresolved):
        new_module._opcodes(old, new)


def test_a_part_in_a_hunk_too_large_to_compare_is_held_not_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    hunks, base, head = _large_hunk(30)
    entry = {"path": "a.py", "status": "modified", "hunks": hunks}
    part = _one_part_per_change(hunks, "a.py")[0]
    monkeypatch.setattr(new_module, "_MATCH_BUDGET", 1000)
    guard = new_guard(part, entry, lambda _path, side: base if side == "old" else head, {})
    assert guard.guarded and guard.code == UNVERIFIABLE
    assert "too large" in guard.reason and "stays out of the batch" in guard.reason
    # The same part with the default budget is read, and its change is the arithmetic one.
    monkeypatch.undo()
    read = new_guard(part, entry, lambda _path, side: base if side == "old" else head, {})
    assert read.guarded and read.code == "arithmetic"


# ----- differential: the guard before this change against the guard now ----- #


def _program(
    rng: random.Random, python: bool, tmp_path: Path, *, git: bool
) -> tuple[str, str, str, list[dict[str, Any]]] | None:
    tsx = not python and rng.random() < 0.5
    path = "app/prog.py" if python else ("app/prog.tsx" if tsx else "app/prog.ts")
    body = _body(rng, python, 0, tsx)
    for _ in range(rng.randint(0, 2)):  # a bigger program: several bodies one after another
        body += _body(rng, python, 0, tsx)
    after = _clone(body)
    for _ in range(rng.randint(1, 4)):
        _mutate(rng, after, python, [])
    old_text, new_text = _lines(body, python), _lines(after, python)
    if old_text == new_text:
        return None
    old_src, new_src = "\n".join(old_text) + "\n", "\n".join(new_text) + "\n"
    hunks = _hunks(tmp_path, path, old_src, new_src) if git else _diff_hunks(old_text, new_text)
    return (path, old_src, new_src, hunks) if hunks else None


def _verdict_pair(
    path: str, hunks: list[dict[str, Any]], spans: tuple[Any, ...], texts: tuple[str, str] | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    target = replace(_target(hunks, path), spans=spans)
    entry = {"path": path, "status": "modified", "hunks": hunks}
    reader = None if texts is None else (lambda _path, side: texts[0] if side == "old" else texts[1])
    return (
        old_module.guard_for_target(target, entry, reader, {}).payload(),
        new_guard(target, entry, reader, {}).payload(),
    )


def _tally(rng: random.Random, python: bool, tmp_path: Path, programs: int) -> Counter[str]:
    tally: Counter[str] = Counter()
    for sample in range(programs):
        made = _program(rng, python, tmp_path, git=sample % 12 == 0)
        if made is None:
            continue
        path, old_src, new_src, hunks = made
        spans = _spans(hunks)
        picks = [spans, *(tuple([span]) for span in rng.sample(spans, min(PARTS_PER_PROGRAM, len(spans))))]
        with new_module._scope():  # one call: the parts of a program share their memo, as they do in a review
            for owned in picks:
                for texts in (None, (old_src, new_src)):
                    before, now = _verdict_pair(path, hunks, owned, texts)
                    tally["parts"] += 1
                    if before == now:
                        tally["same"] += 1
                    elif now["guarded"] and not before["guarded"]:
                        tally["fail-safe"] += 1
                    elif now["guarded"] and before["guarded"]:
                        tally["both-held"] += 1  # the same part is held, for another reason
                    else:
                        tally["fail-open"] += 1
                        tally["open-example"] = tally["open-example"] or sample + 1
    return tally


@pytest.mark.parametrize("python", [True, False], ids=["python", "typescript-javascript"])
def test_the_guard_gives_the_same_verdict_as_before_on_random_programs(python: bool, tmp_path: Path) -> None:
    tally = _tally(random.Random(f"{SEED}:perf:{python}"), python, tmp_path, SAMPLES)
    print(f"\nbatch guard perf differential [{'python' if python else 'script'}]: {dict(tally)}")
    assert tally["parts"] > 800
    assert tally["fail-open"] == 0, tally
    assert tally["fail-safe"] == 0 and tally["both-held"] == 0, "the caches change no verdict"
    assert tally["same"] == tally["parts"]


@pytest.mark.parametrize("python", [True, False], ids=["python", "typescript-javascript"])
def test_with_the_budget_cut_every_divergence_holds_more_and_none_offers_more(
    python: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(new_module, "_MATCH_BUDGET", 300)  # a few lines' worth: most hunks are now refused
    tally = _tally(random.Random(f"{SEED}:perf-budget:{python}"), python, tmp_path, SAMPLES)
    print(f"\nbatch guard budget differential [{'python' if python else 'script'}]: {dict(tally)}")
    assert tally["parts"] > 800
    assert tally["fail-open"] == 0, tally
    assert tally["fail-safe"] + tally["both-held"] > 50, "the budget did refuse comparisons"


@pytest.mark.parametrize("python", [True, False], ids=["python", "typescript-javascript"])
def test_with_the_read_budget_cut_every_divergence_holds_more_and_none_offers_more(
    python: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(new_module, "_ROW_STEPS", 200_000)  # reading a few rows costs a hunk its whole budget
    monkeypatch.setattr(new_module, "_HUNK_BUDGET", 3_000_000)
    monkeypatch.setattr(new_module, "_CALL_BUDGET", 9_000_000)
    tally = _tally(random.Random(f"{SEED}:perf-read:{python}"), python, tmp_path, SAMPLES)
    print(f"\nbatch guard read-budget differential [{'python' if python else 'script'}]: {dict(tally)}")
    assert tally["parts"] > 800
    assert tally["fail-open"] == 0, tally
    assert tally["fail-safe"] + tally["both-held"] > 50, "the read budget did refuse parts"


# ----- review round 2: the charge is an upper bound, a refused hunk is refused once, nothing outlives a call ----- #


def test_the_charge_is_what_the_inner_loop_can_cost_not_an_average() -> None:
    """A long run of tokens the other side lacks must not dilute the density of the dense region it sits beside."""

    absent = [f"p{i}" for i in range(30_000)]
    dense_old, dense_new = ["x"] * 1500, ["x", "w"] * 3000
    matcher = new_module._BudgetMatcher(absent + dense_old, dense_new)
    # One whole-range search walks every occurrence in b of each x: 1500 * 3000, however many absent tokens come first.
    with pytest.raises(new_module._TooLarge):
        matcher.find_longest_match(0, len(absent) + len(dense_old), 0, len(dense_new))
    assert matcher._spent == 0, "a refused search charges nothing"
    just_the_dense = new_module._BudgetMatcher(dense_old, dense_new)
    with pytest.raises(new_module._TooLarge):
        just_the_dense.find_longest_match(0, len(dense_old), 0, len(dense_new))
    small = new_module._BudgetMatcher(absent[:10] + ["x"] * 4, ["x", "w"] * 5)
    small.find_longest_match(10, 14, 0, 10)
    assert small._spent == 4 + 10 + 4 * 5, "span + the other side's range + one step per occurrence of each token"


def test_a_comparison_crafted_to_dilute_the_average_is_refused_within_the_budget() -> None:
    old = [f"p{i}" for i in range(200_000)] + ["x"] * 400
    new = ["x", "w"] * 5000
    matcher = new_module._BudgetMatcher(old, new)
    with pytest.raises(new_module._TooLarge):
        matcher.get_opcodes()
    assert matcher._spent <= new_module._MATCH_BUDGET
    assert new_module._opcodes_of(tuple(old), tuple(new)) is None
    # Counted, not timed: the work refused was cut off after one search, not after the whole recursion.
    searches: list[int] = []
    original = new_module._BudgetMatcher.find_longest_match

    def counting(self: Any, *args: Any) -> Any:
        searches.append(1)
        return original(self, *args)

    new_module._BudgetMatcher.find_longest_match = counting  # type: ignore[method-assign]
    try:
        with pytest.raises(new_module._TooLarge):
            new_module._BudgetMatcher(old, new).get_opcodes()
    finally:
        new_module._BudgetMatcher.find_longest_match = original  # type: ignore[method-assign]
    assert len(searches) <= 3, searches


def _look_alikes(count: int) -> tuple[list[str], list[str]]:
    """Functions alike but for their number, each with a message edited and two statements swapped."""

    def fn(index: int, changed: bool) -> list[str]:
        pair = ["    left = value", "    right = value"]
        return [
            f"def fn_{index}(value):",
            f"    log('{'begin' if changed else 'start'} {index}')",
            *(pair[::-1] if changed else pair),
            "    return left",
            "",
            "",
        ]

    return [row for i in range(count) for row in fn(i, False)], [row for i in range(count) for row in fn(i, True)]


def _part_per_function(hunks: list[dict[str, Any]], count: int) -> list[Any]:
    target, spans = _target(hunks, "a.py"), _spans(hunks)
    parts = []
    for i in range(count):
        own = tuple(span for span in spans if i * 6 + 1 <= span.start_line <= i * 6 + 6)
        if own:
            parts.append(replace(target, target_id=f"p{i}", spans=own))
    return parts


def test_a_hunk_refused_for_one_part_is_refused_once_when_every_part_moves_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each part cuts its own moved lines out of the token stream, so the streams differ part to part: the refusal is
    remembered by the hunk's text, not by the stream."""

    built = _matchers_built(monkeypatch)
    monkeypatch.setattr(new_module, "_MATCH_BUDGET", 20_000)
    for count in (20, 40):
        old, new = _look_alikes(count)
        hunks = _diff_hunks(old, new)
        assert len(hunks) == 1
        parts = _part_per_function(hunks, count)
        assert len(parts) == count
        entry = {"path": "a.py", "status": "modified", "hunks": hunks}
        built.clear()
        with new_module._scope():
            guards = [new_guard(part, entry, None, {}) for part in parts]
        assert {g.code for g in guards} == {UNVERIFIABLE} and all(g.guarded for g in guards)
        assert len(built) <= 3, (count, len(built))


def test_a_refusal_never_offers_a_part_the_old_guard_held_and_does_not_depend_on_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sibling's refusal may hold a part that would have fit alone (stricter), never release one the old guard held."""

    count = 12
    old, new = _look_alikes(count)
    hunks = _diff_hunks(old, new)
    parts = _part_per_function(hunks, count)
    entry = {"path": "a.py", "status": "modified", "hunks": hunks}
    before = [old_module.guard_for_target(part, entry, None, {}).payload() for part in parts]
    monkeypatch.setattr(new_module, "_MATCH_BUDGET", 1500)
    with new_module._scope():
        forward = [new_guard(part, entry, None, {}).payload() for part in parts]
    with new_module._scope():
        backward = [new_guard(part, entry, None, {}).payload() for part in reversed(parts)][::-1]
    alone = [new_guard(part, entry, None, {}).payload() for part in parts]
    for verdicts in (forward, backward, alone):
        for was, got in zip(before, verdicts, strict=True):
            assert got["guarded"] or not was["guarded"], "fail-open"
    assert forward == backward, "the verdicts do not depend on which part came first"


def test_nothing_a_call_memoized_outlives_it() -> None:
    hunks, _, _ = _large_hunk(6, arithmetic=False)
    entry = {"path": "a.py", "status": "modified", "hunks": hunks}
    parts = [
        replace(part, answer=type("A", (), {"category": "quick_check"})())
        for part in _one_part_per_change(hunks, "a.py")
    ]
    packet = {"files": [entry]}
    runs: list[Any] = []
    original = new_module._Run.__init__

    def tracking(self: Any) -> None:
        original(self)
        runs.append(self)

    new_module._Run.__init__ = tracking  # type: ignore[method-assign]
    try:
        first = batch_guards(packet, parts)
        assert new_module._RUN.get() is None, "no scope is left open"
        second = batch_guards(packet, parts)
    finally:
        new_module._Run.__init__ = original  # type: ignore[method-assign]
    assert first == second
    assert len(runs) == 2 and runs[0] is not runs[1], "each call has a memo of its own"
    assert runs[0].lines and runs[1].lines


def test_two_concurrent_calls_do_not_share_or_clear_each_others_memo() -> None:
    import threading

    seen: dict[str, Any] = {}
    gate = threading.Barrier(2)

    def work(name: str) -> None:
        with new_module._scope() as run:
            run.tokens[(name, "python")] = ()
            gate.wait(timeout=10)
            seen[name] = (run, new_module._run() is run, set(run.tokens))
            gate.wait(timeout=10)

    threads = [threading.Thread(target=work, args=(name,)) for name in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert seen["one"][0] is not seen["two"][0]
    assert seen["one"][1] and seen["two"][1]
    assert seen["one"][2] == {("one", "python")} and seen["two"][2] == {("two", "python")}


# ----- review round 3: one part per function with string-only edits ----- #


def _handlers(count: int, python: bool = True) -> tuple[list[str], list[str], str]:
    """``count`` 3-line handlers whose log text changes case: token keys do not change, so every part cuts its own
    edited line out of the stream as a moved one and compares a stream of its own."""

    def py(i: int, new: bool) -> list[str]:
        word = "Handling" if new else "handling"
        return [
            f"def handler_{i}(request):",
            f"    log.info('{word} request {i}')",
            f"    return dispatch_{i}(request)",
            "",
        ]

    def ts(i: int, new: bool) -> list[str]:
        word = "Handling" if new else "handling"
        return [
            f"export function handle{i}(req: Request) {{",
            f"  logger.info('{word} {i}');",
            f"  return dispatch{i}(req);",
            "}",
        ]

    make = py if python else ts
    return (
        [row for i in range(count) for row in make(i, False)],
        [row for i in range(count) for row in make(i, True)],
        "a.py" if python else "a.ts",
    )


def _handler_parts(hunks: list[dict[str, Any]], path: str, count: int) -> list[Any]:
    target, spans = _target(hunks, path), _spans(hunks)
    return [
        replace(target, target_id=f"p{i}", spans=own)
        for i in range(count)
        if (own := tuple(s for s in spans if i * 4 + 1 <= s.start_line <= i * 4 + 4))
    ]


def _run_handlers(count: int, python: bool) -> tuple[Any, list[Any]]:
    old, new, path = _handlers(count, python)
    hunks = _diff_hunks(old, new)
    assert len(hunks) == 1
    parts = _handler_parts(hunks, path, count)
    assert len(parts) == count
    entry = {"path": path, "status": "modified", "hunks": hunks}
    with new_module._scope() as run:
        guards = [new_guard(part, entry, None, {}) for part in parts]
    return run, guards


@pytest.mark.parametrize("python", [True, False], ids=["python", "typescript"])
def test_a_hunk_refused_in_the_row_comparison_is_refused_once_not_once_per_part(python: bool) -> None:
    """The refusal comes from the block check, after the part's own token comparison: it is remembered by the hunk's
    text all the same, so the parts after it are refused before any comparison."""

    run, guards = _run_handlers(220, python)
    assert all(g.guarded and g.code == UNVERIFIABLE for g in guards)
    assert run.matchers <= 4, run.matchers
    assert run.spent <= 3 * new_module._MATCH_BUDGET, run.spent


def test_the_work_on_one_hunk_is_bounded_whatever_the_number_of_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    """At the hunk's budget every part is still read; past it the parts not yet read are held, and the steps charged
    never pass the budget by more than one comparison."""

    monkeypatch.setattr(new_module, "_MATCH_BUDGET", 1_000_000)
    monkeypatch.setattr(new_module, "_HUNK_BUDGET", 10**12)
    run, guards = _run_handlers(30, True)
    total = run.spent
    assert total > 100_000 and not any(g.guarded for g in guards), "unbounded: every part is read and is safe"
    assert run.matchers >= 30, "each part compares a stream of its own"

    monkeypatch.setattr(new_module, "_HUNK_BUDGET", total)
    at, at_guards = _run_handlers(30, True)
    assert at.spent == total and [g.payload() for g in at_guards] == [g.payload() for g in guards], "at the bound"

    monkeypatch.setattr(new_module, "_HUNK_BUDGET", total // 2)
    past, past_guards = _run_handlers(30, True)
    held = [g for g in past_guards if g.guarded]
    assert held and all(g.code == UNVERIFIABLE for g in held), "past the bound the unread parts are held"
    assert len(held) < 30, "the parts read before it are not"
    assert past.spent <= total // 2 + new_module._MATCH_BUDGET, past.spent
    assert past.matchers < run.matchers


# ----- review round 4: a bound on the whole call, not only on each hunk ----- #


def _dense_hunks(blocks: int, per: int) -> tuple[list[str], list[str]]:
    """``blocks`` runs of ``per`` handlers with string-only edits, set apart by constants: one dense hunk each."""

    def block(index: int, new: bool) -> list[str]:
        word = "Handling" if new else "handling"
        gap = [f"# section {index}", *(f"CONST_{index}_{j} = {j}" for j in range(10)), ""]
        return gap + [
            row
            for i in range(per)
            for row in (
                f"def handler_{index}_{i}(request):",
                f"    log.info('{word} request {index} {i}')",
                f"    return dispatch_{index}_{i}(request)",
                "",
            )
        ]

    return (
        [row for b in range(blocks) for row in block(b, False)],
        [row for b in range(blocks) for row in block(b, True)],
    )


def _run_dense(
    blocks: int, per: int, versus_old: bool = False
) -> tuple[Any, list[Any], list[Any], list[dict[str, Any]]]:
    old, new = _dense_hunks(blocks, per)
    hunks = _diff_hunks(old, new)
    assert len(hunks) == blocks
    parts = _one_part_per_change(hunks, "a.py")
    entry = {"path": "a.py", "status": "modified", "hunks": hunks}
    with new_module._scope() as run:
        guards = [new_guard(part, entry, None, {}) for part in parts]
    before = [old_module.guard_for_target(part, entry, None, {}).payload() for part in parts] if versus_old else []
    return run, parts, guards, before


def test_the_work_on_a_whole_review_is_bounded_whatever_the_number_of_hunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(new_module, "_MATCH_BUDGET", 1_000_000)
    monkeypatch.setattr(new_module, "_HUNK_BUDGET", 10**12)
    monkeypatch.setattr(new_module, "_CALL_BUDGET", 10**12)
    run, _parts, guards, _ = _run_dense(4, 20)
    total = run.spent
    assert total > 100_000 and not any(g.guarded for g in guards), "unbounded: every hunk is read and is safe"
    per_hunk = sorted(run.hunk_spent.values())
    fixed = total - sum(
        per_hunk
    )  # a part's own cost before it reads (its view, scope, payload) is the call's, not a hunk's
    assert len(per_hunk) == 4 and 0 <= fixed < len(_parts) * (
        new_module._PART_STEPS + 10_000
    ), "charged to the hunk read"

    monkeypatch.setattr(new_module, "_CALL_BUDGET", total)
    at, _, at_guards, _ = _run_dense(4, 20)
    assert at.spent == total and not any(g.guarded for g in at_guards), "at the bound nothing is held"

    cap = total // 2  # two hunks' worth
    monkeypatch.setattr(new_module, "_CALL_BUDGET", cap)
    past, _past_parts, past_guards, before = _run_dense(4, 20, versus_old=True)
    assert past.spent <= cap + new_module._MATCH_BUDGET, "the call never passes its budget by more than a comparison"
    held = [g.guarded and g.code == UNVERIFIABLE for g in past_guards]
    assert held[0] is False and held[-1] is True, "the hunks read first keep their verdicts, the later ones are held"
    first_held = held.index(True)
    assert all(held[first_held:]), "once the budget is gone every later part is held"
    for was, got in zip(before, past_guards, strict=True):
        assert got.guarded or not was["guarded"], "fail-open"


# ----- review round 5: reading is charged too, so the budgets bound a call's time and not only its comparisons ----- #


def test_the_work_of_reading_the_rows_is_charged_and_bounded_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every part reads the hunk's rows again (its own rows flagged), which no comparison counts. Reading is charged
    per row and character to the hunk and the call, so past either budget the parts not yet read are held."""

    monkeypatch.setattr(new_module, "_MATCH_BUDGET", 1_000_000)
    monkeypatch.setattr(new_module, "_ROW_STEPS", 20_000)  # reading dominates the comparisons
    monkeypatch.setattr(new_module, "_HUNK_BUDGET", 10**15)
    monkeypatch.setattr(new_module, "_CALL_BUDGET", 10**15)
    run, parts, guards, _ = _run_dense(4, 12)
    reads = run.rows * new_module._ROW_STEPS + run.chars * new_module._CHAR_STEPS
    assert not any(g.guarded for g in guards) and run.rows > 40 * len(parts), "every part reads its hunk's rows"
    assert run.spent > reads > run.spent // 2, "the reads are in the total, and are most of it here"
    per_part = run.spent - sum(
        run.hunk_spent.values()
    )  # the fixed cost of a part and of its view: the call's, not a hunk's
    assert 0 <= per_part < len(parts) * (new_module._PART_STEPS + 10_000), "reads are charged to the hunk they read"
    part_reads = reads // len(parts)  # parts are alike in a dense hunk: one part's reads, give or take

    cap = run.spent // 2
    monkeypatch.setattr(new_module, "_CALL_BUDGET", cap)
    past, _, past_guards, before = _run_dense(4, 12, versus_old=True)
    assert past.spent <= cap + new_module._MATCH_BUDGET + 2 * part_reads, "a call stops reading when its budget is gone"
    held = [g.guarded and g.code == UNVERIFIABLE for g in past_guards]
    assert held[0] is False and held[-1] is True and all(held[held.index(True) :]), "the unread parts are held"
    for was, got in zip(before, past_guards, strict=True):
        assert got.guarded or not was["guarded"], "fail-open"

    monkeypatch.setattr(new_module, "_CALL_BUDGET", 10**15)
    monkeypatch.setattr(new_module, "_HUNK_BUDGET", max(run.hunk_spent.values()) // 2)
    again, _, again_guards, before = _run_dense(4, 12, versus_old=True)
    assert max(again.hunk_spent.values()) <= new_module._HUNK_BUDGET + new_module._MATCH_BUDGET + 2 * part_reads
    assert any(
        g.guarded and g.code == UNVERIFIABLE for g in again_guards
    ), "a hunk past its budget holds its later parts"
    assert not all(g.guarded for g in again_guards), "and the parts read before it keep their verdicts"
    for was, got in zip(before, again_guards, strict=True):
        assert got.guarded or not was["guarded"], "fail-open"


# ----- review round 6: what a part costs before it reads a row, and the hunks a part tests its spans against ----- #


def test_every_part_is_charged_what_it_costs_before_it_reads_a_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Many small parts: the work per part is mostly fixed (its view, scope, lookups, payload), which no row or
    comparison counts. Each part read is charged ``_PART_STEPS``, so the call budget bounds their number; the parts
    after it are held, and a part refused is not charged."""

    monkeypatch.setattr(new_module, "_HUNK_BUDGET", 10**15)
    monkeypatch.setattr(new_module, "_CALL_BUDGET", 10**15)
    base, guards = _run_handlers(24, True)
    assert not any(g.guarded for g in guards)

    monkeypatch.setattr(new_module, "_PART_STEPS", 10**9)  # one part's fixed cost dwarfs everything else it reads
    monkeypatch.setattr(new_module, "_CALL_BUDGET", int(5.5 * 10**9))
    run, guards = _run_handlers(24, True)
    read = [not (g.guarded and g.code == UNVERIFIABLE) for g in guards]
    # The sixth part starts inside the budget, is charged, and finds it used up when it builds its view: held, not read.
    assert read.count(True) == 5 and read == sorted(read, reverse=True), "five parts are read, the rest held, in order"
    assert (
        6 * 10**9 <= run.spent < 6 * 10**9 + 2 * base.spent
    ), "each part started is charged its fixed cost, a refusal is not"


def test_a_part_is_charged_for_testing_every_row_against_every_one_of_its_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old, new, path = _handlers(40, True)
    hunks = _diff_hunks(old, new)
    entry = {"path": path, "status": "modified", "hunks": hunks}
    parts = _handler_parts(hunks, path, 40)
    one, many = parts[0], replace(parts[0], spans=tuple(s for p in parts for s in p.spans))
    spent = []
    for target in (one, many):
        with new_module._scope() as run:
            new_module._hunk_views(entry, target.spans)
            spent.append(run.spent)
    rows = sum(len(side) for side in new_module._hunk_views(entry, one.spans)[0]) // 2 or 1
    assert spent[1] > 10 * spent[0], "the span count multiplies the rows tested"
    assert spent[0] >= new_module._VIEW_STEPS * rows * 2

    monkeypatch.setattr(new_module, "_CALL_BUDGET", spent[1] // 2)
    with new_module._scope() as run:
        run.spent = spent[1] // 2 + 1
        with pytest.raises(new_module._TooBig):
            new_module._hunk_views(entry, many.spans)  # refused before the view is built


def test_a_statement_moved_between_hunks_is_found_as_before_with_one_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    rng = random.Random(f"{SEED}:between-hunks")
    for _ in range(300):
        views = []
        for _hunk in range(rng.randint(1, 6)):
            old = [
                (f"v{rng.randint(0, 9)} = {rng.randint(0, 3)}", rng.random() < 0.5) for _ in range(rng.randint(0, 4))
            ]
            new = [
                (f"v{rng.randint(0, 9)} = {rng.randint(0, 3)}", rng.random() < 0.5) for _ in range(rng.randint(0, 4))
            ]
            views.append((old, new))
        outcomes = []
        for module in (old_module, new_module):
            try:
                module._moved_between_hunks(views, "python")
                outcomes.append("")
            except Exception as exc:
                outcomes.append(type(exc).__name__)
        assert outcomes[0] == outcomes[1], views


def test_the_whole_file_reading_is_charged_to_the_call_too(monkeypatch: pytest.MonkeyPatch) -> None:
    old, new, path = _handlers(6, True)
    hunks = _diff_hunks(old, new)
    entry = {"path": path, "status": "modified", "hunks": hunks}
    parts = _handler_parts(hunks, path, 6)
    texts = ("\n".join(old) + "\n", "\n".join(new) + "\n")

    def read(_path: str, side: str) -> str:
        return texts[0 if side == "old" else 1]

    with new_module._scope() as plain:
        [new_guard(p, entry, None, {}) for p in parts]
    with new_module._scope() as whole:
        guards = [new_guard(p, entry, read, {}) for p in parts]
    assert not any(g.guarded and g.code == UNVERIFIABLE for g in guards)
    parse = new_module._TREE_CHAR_STEPS * (len(texts[0]) + len(texts[1]))
    assert whole.spent - plain.spent >= parse + len(parts) * new_module._TREE_PART_STEPS, "parsed text and each verdict"


# ----- a part reads only its own hunks (P10-2) ----- #


class _CountedSpan:
    """A span that counts every read of its hunk ordinal: the scan of a file's hunks tests this once per hunk."""

    def __init__(self, span: Any, reads: list[int]) -> None:
        self._span, self._reads = span, reads
        self.side, self.start_line, self.end_line = span.side, span.start_line, span.end_line

    @property
    def hunk_ordinal(self) -> int:
        self._reads.append(1)
        return int(self._span.hunk_ordinal)


class _CountedEntry(dict):  # type: ignore[type-arg]
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.hunk_lists = 0

    def get(self, key: Any, default: Any = None) -> Any:
        self.hunk_lists += key == "hunks"
        return super().get(key, default)


def _separate_hunks(count: int) -> list[dict[str, Any]]:
    old: list[str] = []
    new: list[str] = []
    for i in range(count):
        body = [f"K_{i}_{j} = {j}" for j in range(8)]
        old += [f"def f_{i}(x):", f"    log.info('a {i}')", "    return x", *body]
        new += [f"def f_{i}(x):", f"    log.info('B {i}')", "    return x", *body]
    hunks = _diff_hunks(old, new)
    assert len(hunks) == count, "every change is a hunk of its own"
    return hunks


def _hunk_reads(count: int) -> tuple[int, int, int, int]:
    """(ordinal reads, parts, lists of hunks built, a part's verdicts that are not guarded) for one file of ``count`` hunks."""

    hunks = _separate_hunks(count)
    entry = _CountedEntry({"path": "app/big.py", "status": "modified", "hunks": hunks})
    reads: list[int] = []
    base = replace(_target(hunks, "app/big.py"), answer=type("A", (), {"category": "quick_check"})())
    parts = [
        replace(base, target_id=f"p{n}", spans=(_CountedSpan(span, reads),))
        for n, span in enumerate(_spans(hunks))
        if span.side == "new"
    ]
    reads.clear()  # the parts' own construction is not the guard's work
    with new_module._scope():
        guards = [new_guard(part, entry, None, {}) for part in parts]
    return len(reads), len(parts), entry.hunk_lists, sum(not g.guarded for g in guards)


def test_a_part_reads_only_its_own_hunks_not_every_hunk_of_the_file() -> None:
    small, large = _hunk_reads(30), _hunk_reads(90)
    for (reads, parts, lists, offered), count in ((small, 30), (large, 90)):
        assert parts == count and offered == count, "a part in each hunk, each one checked and offered"
        # Each part has one span in one hunk: the span is grouped once. The scan of the file's hunks read it once per
        # hunk of the file, so this was count * count.
        assert reads == parts, (reads, parts)
        assert lists == 1, "the file's hunks are listed once per call, not once per part"


def test_the_hunk_list_of_a_file_is_not_taken_from_another_entry_of_the_same_id() -> None:
    first = {"path": "a.py", "hunks": _separate_hunks(2)}
    second = {"path": "a.py", "hunks": _separate_hunks(3)}
    with new_module._scope() as run:
        assert len(new_module._hunks_of(first)[0]) == 2
        run.hunks[id(second)] = (first, [], False)  # what a reused id would leave behind
        assert len(new_module._hunks_of(second)[0]) == 3, "the memo checks the entry it holds is this one"
        assert run.hunks[id(first)][0] is first, "the entry is held, so its id is its own for the whole call"
    assert new_module._RUN.get() is None, "no scope, so no memo, outlives the call"


def test_a_part_gets_the_same_views_as_when_every_hunk_was_tested_against_its_spans() -> None:
    from lemoncrow.pro.capabilities.review.targets import TargetSpan

    rng = random.Random(f"{SEED}:own-hunks")
    hunks = _separate_hunks(12)
    hunks[3] = {**hunks[3], "patch": ""}  # a hunk with no diff text
    entries = [
        {"path": "a.py", "hunks": hunks},
        {"path": "b.py", "hunks": [*hunks[:5], "not a hunk", None, *hunks[5:]]},  # skipped entries do not count
        {"path": "c.py", "hunks": [{"path": "x"}]},  # no diff text at all
        {"path": "d.py"},
    ]
    for _ in range(400):
        spans = tuple(
            TargetSpan(rng.choice(("old", "new")), line, line + rng.randint(0, 2), rng.randint(-2, 15))
            for line in (rng.randint(1, 120) for _ in range(rng.randint(0, 6)))
        )
        for entry in entries:
            outcomes = []
            for module in (old_module, new_module):
                with new_module._scope():  # the frozen module has no scope; the new one memoizes inside this one
                    try:
                        outcomes.append(module._hunk_views(entry, spans))
                    except Exception as exc:
                        outcomes.append(type(exc).__name__)
            assert outcomes[0] == outcomes[1], (entry["path"], spans)


# ----- the whole-file reading: a part reads only its own lines (P10-2, round 2) ----- #


def _tree_outcome(function: Any, loaded: Any, own_old: set[int], own_new: set[int]) -> tuple[str, str]:
    try:
        return "ok", function(loaded, own_old, own_new)
    except tree.TreeClosed as exc:
        return "closed", str(exc)


@pytest.mark.parametrize("python", [True, False], ids=["python", "typescript-javascript"])
def test_the_whole_file_verdict_is_what_it_was_when_every_part_scanned_every_unit(python: bool, tmp_path: Path) -> None:
    rng = random.Random(f"{SEED}:tree-index:{python}")
    verdicts: Counter[str] = Counter()
    for sample in range(SAMPLES):
        made = _program(rng, python, tmp_path, git=sample % 12 == 0)
        if made is None:
            continue
        path, old_src, new_src, hunks = made
        entry = {"path": path, "status": "modified", "hunks": hunks}
        try:
            texts = {"old": old_src, "new": new_src}
            loaded = tree.read_file(entry, path, lambda _p, side, texts=texts: texts[side])
        except tree.TreeClosed:
            verdicts["unread"] += 1
            continue
        for side, table in (("old", loaded.canon.old), ("new", loaded.canon.new)):
            for line in range(0, max(table, default=0) + 40):
                assert loaded.canon.of(side, line) == old_tree.canon_of(loaded.canon, side, line), (side, line)
        spans = _spans(hunks)
        picks = [spans, *(rng.sample(spans, rng.randint(1, min(5, len(spans)))) for _ in range(PARTS_PER_PROGRAM))]
        for owned in picks:
            own_old, own_new = tree.own_lines(owned)
            before = _tree_outcome(old_tree.verdict, loaded, own_old, own_new)
            now = _tree_outcome(tree.verdict, loaded, own_old, own_new)
            assert before == now, (path, sorted(own_old), sorted(own_new))
            verdicts[before[1] or before[0]] += 1
    assert verdicts["loop_bound"] + verdicts["block_moved"] > 20, verdicts  # the holds were exercised, not only "ok"
    assert sum(verdicts.values()) > 800, verdicts


def test_a_line_outside_every_hunk_is_shifted_by_the_last_hunk_before_it_whatever_their_order() -> None:
    rng = random.Random(f"{SEED}:canon-shift")
    for _ in range(300):
        hunks = [
            {
                "old_start": rng.randint(1, 60),
                "old_lines": rng.randint(0, 3),
                "new_start": rng.randint(1, 60),
                "new_lines": rng.randint(0, 3),
            }
            for _ in range(
                rng.randint(0, 8)
            )  # not sorted, and overlapping: the old walk took the last one in list order
        ]
        canon = tree._Canon(hunks)
        for side in ("old", "new"):
            for line in range(-2, 80):
                assert canon.of(side, line) == old_tree.canon_of(canon, side, line), (hunks, side, line)


def _tree_hunks_work(count: int) -> tuple[int, int]:
    """(units looked at, unchanged pairs looked at) by ``verdict`` over one part per hunk of a file of ``count`` hunks."""

    hunks = _separate_hunks(count)
    old = [
        line
        for i in range(count)
        for line in (
            f"def f_{i}(x):",
            f"    log.info('a {i}')",
            "    return x",
            *(f"K_{i}_{j} = {j}" for j in range(8)),
        )
    ]
    new = [line.replace("'a ", "'B ") for line in old]
    texts = ("\n".join(old) + "\n", "\n".join(new) + "\n")
    entry = {"path": "app/big.py", "status": "modified", "hunks": hunks}
    loaded = tree.read_file(entry, "app/big.py", lambda _p, side: texts[0] if side == "old" else texts[1])
    looked: list[int] = []

    class Counted(list):  # type: ignore[type-arg]
        def __iter__(self) -> Any:
            looked.append(len(self))
            return super().__iter__()

    loaded.old.units, loaded.new.units = Counted(loaded.old.units), Counted(loaded.new.units)
    loaded.untouched = Counted(loaded.untouched)
    calls = 0
    for span in _spans(hunks):
        if span.side == "new":
            own_old, own_new = tree.own_lines((span,))
            tree.verdict(loaded, own_old, own_new)
            calls += 1
    assert calls == count
    return sum(looked), calls


def test_a_part_reads_only_its_own_lines_in_the_whole_file_reading_not_every_unit_of_the_file() -> None:
    small, large = _tree_hunks_work(20), _tree_hunks_work(60)
    for (looked, calls), count in ((small, 20), (large, 60)):
        # Each call used to iterate both sides' units and the unchanged pairs: three lists of the file's size.
        assert looked <= calls, (looked, calls, count)


@pytest.mark.parametrize(
    ("ordinals", "views"),
    [
        ([0, 1.0], 2),  # 1.0 is hunk 1 under ==: read, not dropped
        ([1.0], 1),
        ([True], 1),  # True == 1
        ([None, 0], 1),  # None is no hunk
        (["1", 0], 1),
        ([float("nan"), 0], 1),
        ([float("inf"), 1.5, 0], 1),
        ([None], 0),
    ],
    ids=str,
)
def test_a_span_whose_ordinal_is_not_an_int_is_read_as_before_not_dropped(ordinals: list[Any], views: int) -> None:
    hunks = _separate_hunks(3)
    by_ordinal = {o: [s for s in _spans(hunks) if s.hunk_ordinal == o and s.side == "new"] for o in range(3)}
    # the first new line of hunk 0, 1 or 2 as asked, whatever number the span carries for it
    spans = tuple(
        replace(by_ordinal[int(o) if isinstance(o, (int, float)) and o == o and o in (0, 1) else 0][0], hunk_ordinal=o)
        for o in ordinals
    )
    entry = {"path": "a.py", "hunks": hunks}
    with new_module._scope():
        now = new_module._hunk_views(entry, spans)
    assert now == old_module._hunk_views(entry, spans)
    assert len(now or ()) == views, "the part is offered the hunks it owns, and no hunk is dropped"


@pytest.mark.parametrize("python", [True, False], ids=["python", "typescript-javascript"])
def test_the_whole_file_verdict_is_what_it_was_when_the_file_does_not_parse(python: bool, tmp_path: Path) -> None:
    rng = random.Random(f"{SEED}:tree-errors:{python}")
    closed = 0
    for _sample in range(SAMPLES // 2):
        made = _program(rng, python, tmp_path, git=False)
        if made is None:
            continue
        path, old_src, new_src, _ = made
        old_lines, new_lines = old_src.split("\n"), new_src.split("\n")
        for lines in (old_lines, new_lines):
            for _ in range(rng.randint(0, 3)):
                lines.insert(rng.randrange(len(lines)), rng.choice(["((( ]]", "} } else {", "def (:", "x = = 1"]))
        hunks = _diff_hunks(old_lines, new_lines)
        if not hunks:
            continue
        texts = {"old": "\n".join(old_lines), "new": "\n".join(new_lines)}
        entry = {"path": path, "status": "modified", "hunks": hunks}
        try:
            loaded = tree.read_file(entry, path, lambda _p, side, texts=texts: texts[side])
        except tree.TreeClosed:
            continue
        spans = _spans(hunks)
        for owned in [
            spans,
            *(rng.sample(spans, rng.randint(1, min(4, len(spans)))) for _ in range(PARTS_PER_PROGRAM)),
        ]:
            own_old, own_new = tree.own_lines(owned)
            before = _tree_outcome(old_tree.verdict, loaded, own_old, own_new)
            assert before == _tree_outcome(tree.verdict, loaded, own_old, own_new), (path, sorted(own_old))
            closed += before[0] == "closed"
    assert closed > 50, "the parse errors were hit"


_DECORATORS = ["@a", "@b", "@c(1)", "@d.e"]


def _decorated(rng: random.Random, python: bool) -> tuple[list[str], list[str], str]:
    """Stacked decorators on Python functions or TypeScript methods, then some reordered, reversed, moved or edited."""

    members: list[tuple[list[str], list[str]]] = []
    for i in range(rng.randint(2, 7)):
        decorators = rng.sample(_DECORATORS, rng.randint(0, 3))
        loop = rng.random() < 0.4
        if python:
            body = [f"    x = {i}", *(["    for k in ks:", f"        x += {i}"] if loop else []), "    return x"]
            members.append((decorators, [f"def f{i}(x):", *body, ""]))
        else:
            body = [
                f"    x = {i};",
                *(["    for (const k of ks) {", f"      x += {i};", "    }"] if loop else []),
                "    return x;",
            ]
            members.append(([f"  {d}" for d in decorators], [f"  m{i}(x) {{", *body, "  }", ""]))

    def render(group: list[tuple[list[str], list[str]]]) -> list[str]:
        lines = [] if python else ["class C {"]
        for decorators, body in group:
            lines += [*decorators, *body]
        return lines if python else [*lines, "}"]

    changed = [(list(d), list(b)) for d, b in members]
    for _ in range(rng.randint(1, 3)):
        k, move = rng.randrange(len(changed)), rng.random()
        if move < 0.4 and len(changed[k][0]) > 1:
            rng.shuffle(changed[k][0])  # the same decorators in another order
        elif move < 0.6:
            j = rng.randrange(len(changed))
            changed[k], changed[j] = changed[j], changed[k]
        elif move < 0.8:
            changed[k][1].insert(1, "    y = 2" + ("" if python else ";"))
        else:
            changed[k][0].reverse()
    return render(members), render(changed), "a.py" if python else "a.ts"


@pytest.mark.parametrize("python", [True, False], ids=["python", "typescript-javascript"])
def test_the_whole_file_verdict_is_what_it_was_with_stacked_decorators_reordered(python: bool) -> None:
    rng = random.Random(f"{SEED}:tree-decorators:{python}")
    verdicts: Counter[str] = Counter()
    for _ in range(300):
        old_lines, new_lines, path = _decorated(rng, python)
        hunks = _diff_hunks(old_lines, new_lines) if old_lines != new_lines else []
        if not hunks:
            continue
        if rng.random() < 0.3:
            rng.shuffle(hunks)  # the hunks of a file need not arrive in order
        texts = {"old": "\n".join(old_lines) + "\n", "new": "\n".join(new_lines) + "\n"}
        entry = {"path": path, "status": "modified", "hunks": hunks}
        try:
            loaded = tree.read_file(entry, path, lambda _p, side, texts=texts: texts[side])
        except tree.TreeClosed:
            verdicts["unread"] += 1
            continue
        spans = _spans(hunks)
        for owned in [spans, *([span] for span in spans)]:
            own_old, own_new = tree.own_lines(owned)
            before = _tree_outcome(old_tree.verdict, loaded, own_old, own_new)
            assert before == _tree_outcome(tree.verdict, loaded, own_old, own_new), (path, old_lines, new_lines)
            verdicts[before[1] or before[0]] += 1
    assert verdicts["block_changed"] > 20, verdicts  # decorators in another order were hit, not only moves and edits
