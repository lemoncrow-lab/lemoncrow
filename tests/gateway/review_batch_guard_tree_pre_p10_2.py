"""The whole-file reading's ``verdict`` as it was before P10-2, frozen to prove the indexed one equal to it.

Frozen here: ``verdict``, ``_ident``, ``_decorators_reordered`` and ``_error_in`` are the old text (``_ident`` calls
``canon_of`` where it called ``canon.of``), and ``canon_of`` is the old ``_Canon.of``, which walked every hunk end for
a line outside the hunks, written as a function over a ``_Canon``'s ``_ends``. Shared with the new module, not frozen:
the parse (``read_file``, ``_untouched``, ``TreeFile``'s ``old``, ``new``, ``canon`` and ``untouched``) and the
chain, difference and abstraction helpers. Those are unchanged by P10-2 except ``_Canon.of``, whose results the
equivalence test compares line by line against ``canon_of``, so the shared ``_Canon`` answers as before everywhere.
Nothing imports this module but that test.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Any

from lemoncrow.pro.capabilities.review.batch_guard_tree import (
    _ABSTRACTED,
    TreeClosed,
    TreeFile,
    _Canon,
    _difference,
    _Unit,
)


def _error_in(errors: Sequence[tuple[int, int]], start: int, end: int) -> bool:
    return any(not (stop < start or first > end) for first, stop in errors)


def canon_of(canon: _Canon, side: str, line: int) -> tuple[Any, ...]:
    table = canon.old if side == "old" else canon.new
    found = table.get(line)
    if found is not None:
        return found
    if side == "old":
        return ("u", line)
    shift = 0
    for end_new, end_old in canon._ends:
        if end_new <= line:
            shift = end_new - end_old
    return ("u", line - shift)


def _ident(canon: _Canon, side: str, unit: _Unit) -> tuple[tuple[Any, ...], ...]:
    return tuple((e.kind, canon_of(canon, side, e.start), e.depth, e.slot) for e in unit.chain)


def verdict(tf: TreeFile, own_old: set[int], own_new: set[int]) -> str:
    """``loop_bound`` | ``block_moved`` | ``""`` for one part's rows; ``TreeClosed`` when the parse cannot be trusted."""

    gone = [u for u in tf.old.units if not u.lines.isdisjoint(own_old)]
    came = [u for u in tf.new.units if not u.lines.isdisjoint(own_new)]
    for errors, own, units in ((tf.old.errors, own_old, gone), (tf.new.errors, own_new, came)):
        if not errors:
            continue
        if any(_error_in(errors, line, line) for line in own):
            raise TreeClosed("the file does not parse where the change is")
        for unit in units:
            for elem in unit.chain:
                if _error_in(errors, elem.start, elem.end):
                    raise TreeClosed("the file does not parse inside a block around the change")
    canon = tf.canon
    found = ""

    def note(loop: bool) -> None:
        nonlocal found
        found = "loop_bound" if (loop or found == "loop_bound") else "block_moved"

    gone_ids = [(u, _ident(canon, "old", u)) for u in gone]
    came_ids = [(u, _ident(canon, "new", u)) for u in came]
    stayed = Counter((u.key, ident) for u, ident in gone_ids) & Counter((u.key, ident) for u, ident in came_ids)
    left_gone: list[tuple[_Unit, Any]] = []
    left_came: list[tuple[_Unit, Any]] = []
    budget = Counter(stayed)
    for u, ident in gone_ids:
        if budget[(u.key, ident)] > 0:
            budget[(u.key, ident)] -= 1
        else:
            left_gone.append((u, ident))
    budget = Counter(stayed)
    for u, ident in came_ids:
        if budget[(u.key, ident)] > 0:
            budget[(u.key, ident)] -= 1
        else:
            left_came.append((u, ident))
    by_key: dict[tuple[str, ...], list[tuple[_Unit, Any]]] = {}
    by_shape: dict[tuple[str, ...], list[tuple[_Unit, Any]]] = {}
    for u, ident in left_came:
        by_key.setdefault(u.key, []).append((u, ident))
        if _ABSTRACTED & set(u.shape) and not u.imported and not u.declaration:
            by_shape.setdefault(u.shape, []).append((u, ident))
    for u, ident in left_gone:
        for twin, twin_ident in by_key.get(u.key, ()):
            note(_difference(ident, twin_ident, u.chain, twin.chain)[2])
        if _ABSTRACTED & set(u.shape) and not u.imported and not u.declaration:
            for twin, twin_ident in by_shape.get(u.shape, ()):
                if twin_ident != ident:
                    note(_difference(ident, twin_ident, u.chain, twin.chain)[2])
    for before, after in tf.untouched:
        ident_before, ident_after = _ident(canon, "old", before), _ident(canon, "new", after)
        if ident_before == ident_after:
            continue
        only_before, only_after, loop = _difference(ident_before, ident_after, before.chain, after.chain)
        explained = any(e.start in own_old or e.end in own_old for e in only_before) or any(
            e.start in own_new or e.end in own_new for e in only_after
        )
        if explained:
            note(loop)
    if not found and _decorators_reordered(tf, gone, came):
        return "block_changed"
    return found


def _decorators_reordered(tf: TreeFile, gone: Sequence[_Unit], came: Sequence[_Unit]) -> bool:
    """Decorators wrap each other in order: the same ones in another order on one definition change what runs."""

    canon = tf.canon
    targets = {_ident(canon, "old", u)[-1] for u in gone if u.decorator} | {
        _ident(canon, "new", u)[-1] for u in came if u.decorator
    }
    if not targets:
        return False

    def order(side: str, units: Sequence[_Unit]) -> dict[Any, list[tuple[str, ...]]]:
        found: dict[Any, list[tuple[str, ...]]] = {}
        for u in units:
            if u.decorator and u.chain:
                found.setdefault(_ident(canon, side, u)[-1], []).append(u.key)
        return found

    before, after = order("old", tf.old.units), order("new", tf.new.units)
    return any(
        before.get(t, []) != after.get(t, []) and sorted(before.get(t, [])) == sorted(after.get(t, [])) for t in targets
    )
