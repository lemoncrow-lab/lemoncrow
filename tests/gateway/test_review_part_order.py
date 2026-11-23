"""P12-1 (SPEC 7.1, OWNER-16): in a shell or Make file a part whose order changed reopens, and only that part.

A definition that keeps its body but moves above its first use can change what a shell script or a Makefile does, and its
content fingerprint cannot see it. ``revisions._order_moved`` reads the order of the parts the two revisions share; a
part that only shifted because lines were added or removed elsewhere keeps its place and its mark.

Three layers, cheapest first:

* the pure rule through :func:`reconcile`, and the path predicate;
* a seeded randomized differential on synthetic units whose oracle is written from the rule and not from the
  implementation: for every pair of parts whose relative order flipped, at least one reopens (**fail-open == 0**), a part
  that reopens is part of some flipped pair, and a revision that only inserts, deletes or re-spaces reopens nothing;
* the same oracle over the real pipeline (a git repository, ``refresh``, real marks, ``compute_frontier``), for shell and
  Make. ``LEMONCROW_PART_ORDER_PROGRAMS`` raises the number of real programs for a larger local run.
"""

from __future__ import annotations

import os
import random
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pygit2
import pytest

from lemoncrow.pro.capabilities.review import revisions
from lemoncrow.pro.capabilities.review import units as units_module
from lemoncrow.pro.capabilities.review.gitdiff import MAX_BLOB_BYTES, resolve_rev_range
from lemoncrow.pro.capabilities.review.revisions import compute_frontier, orders_parts, reconcile
from lemoncrow.pro.capabilities.review.session_models import ReviewMark, ReviewUnit
from lemoncrow.pro.capabilities.review.since_review import since_review_proofs
from lemoncrow.pro.capabilities.review.sources.local import (
    mark_unit,
    open_or_create_session,
    read_packet_json,
    refresh,
)
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.targets import ReviewTarget, derive_review_targets
from lemoncrow.pro.capabilities.review.units import (
    ANCHOR_MOVED,
    bare_fingerprint,
    is_unanchored,
    split_anchor,
    symbol_fingerprint,
    unit_key,
    with_anchor,
)
from lemoncrow.pro.capabilities.review.validity import REASONS, mark_event_reason

SEEDS = (20261116, 20261117, 20261118)
PROGRAMS_PER_SEED = 300
REAL_SEED = 20261119
REAL_PROGRAMS = int(os.environ.get("LEMONCROW_PART_ORDER_PROGRAMS", "10"))
ORDER_REASON = "part_order_changed"
PLACE_REASON = "part_place_unverifiable"


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


# --------------------------------------------------------------------------------------------------------------------
# Synthetic units: a part is a name, a kind and a length; a layout puts them in order with gaps between.
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Part:
    name: str
    kind: str  # "symbol" or "hunk"
    length: int = 1


def _key(path: str, part: _Part) -> str:
    if part.kind == "hunk":
        return unit_key("hunk", path, "", int(part.name[1:]))
    return unit_key("symbol", path, part.name, 0)


def _fingerprint(part: _Part) -> str:
    return symbol_fingerprint("function", part.name, f"{part.name}() {{ :; }}\n")


def _layout(path: str, parts: list[_Part], rng: random.Random, revision: str) -> list[ReviewUnit]:
    """One unit per part, in order, with a random run of untouched lines before each: nothing overlaps."""

    line = rng.randint(1, 4)
    out: list[ReviewUnit] = []
    for part in parts:
        start, end = line, line + part.length - 1
        line = end + 1 + rng.randint(1, 4)
        out.append(
            ReviewUnit(
                revision_id=revision,
                unit_key=_key(path, part),
                kind=part.kind,  # type: ignore[arg-type]
                path=path,
                content_fingerprint=_fingerprint(part),
                fingerprint_method="hunk_patch_sha256" if part.kind == "hunk" else "symbol_body_sha256",
                symbol="" if part.kind == "hunk" else part.name,
                ordinal=int(part.name[1:]) if part.kind == "hunk" else 0,
                start_line=start,
                end_line=end,
            )
        )
    return out


def _mark(unit: ReviewUnit, state: str = "reviewed") -> ReviewMark:
    return ReviewMark(
        review_id="rev-1",
        unit_key=unit.unit_key,
        state=state,  # type: ignore[arg-type]
        reviewed_revision_id=unit.revision_id,
        content_fingerprint=unit.content_fingerprint,
    )


def _run(path: str, before: list[ReviewUnit], after: list[ReviewUnit]) -> tuple[set[str], dict[str, str]]:
    """``(keys reopened for a changed place, reason code by key)`` for a reviewer who marked every unit of *before*."""

    result = reconcile(before, after, [_mark(unit) for unit in before])
    reasons = {key: code for _reviewer, key, code in result.reasons}
    return {key for key, code in reasons.items() if code == ORDER_REASON}, reasons


def _symbols(*names: str) -> list[_Part]:
    return [_Part(name, "symbol", 2) for name in names]


# --------------------------------------------------------------------------------------------------------------------
# The rule, one case at a time.
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("build.sh", True),
        ("scripts/ci.bash", True),
        ("x.zsh", True),
        ("x.ksh", True),
        ("Makefile", True),
        ("sub/dir/Makefile", True),
        ("GNUmakefile", True),
        ("makefile", True),
        ("rules/common.mk", True),
        ("A.SH", True),
        ("app.py", False),
        ("main.go", False),
        ("src/lib.rs", False),
        ("Makefile.md", False),
        ("notes.make.txt", False),
        ("script", False),
        ("shell.pyc", False),
    ],
)
def test_only_shell_and_make_files_order_their_parts(path: str, expected: bool) -> None:
    assert orders_parts(path) is expected


def test_the_new_reason_is_registered_as_a_reopened_reason() -> None:
    assert REASONS[ORDER_REASON].state == "reopened"
    assert mark_event_reason(ORDER_REASON)


@pytest.mark.parametrize("path", ["run.sh", "Makefile", "mk/rules.mk", "GNUmakefile", "t.ksh"])
def test_a_definition_moved_above_the_others_reopens_with_the_new_reason(path: str) -> None:
    rng = random.Random(1)
    before = _layout(path, _symbols("a", "b", "c", "d"), rng, "r1")
    after = _layout(path, _symbols("c", "a", "b", "d"), rng, "r2")
    result = reconcile(before, after, [_mark(unit) for unit in before])
    moved = unit_key("symbol", path, "c", 0)
    assert [mark.unit_key for mark in result.reopened] == [moved]
    assert result.reopened[0].state == "changed_since_review"
    assert {key: code for _r, key, code in result.reasons}[moved] == ORDER_REASON
    assert len(result.carried) == 3, "the three parts that kept their order keep their mark"


def test_the_same_move_in_another_language_is_carried() -> None:
    rng = random.Random(1)
    before = _layout("a.py", _symbols("a", "b", "c", "d"), rng, "r1")
    after = _layout("a.py", _symbols("c", "a", "b", "d"), rng, "r2")
    result = reconcile(before, after, [_mark(unit) for unit in before])
    assert result.reopened == () and len(result.carried) == 4
    assert {code for _r, _k, code in result.reasons} == {"content_unchanged"}


def test_a_swap_reopens_both_parts_because_either_could_be_the_one_that_moved() -> None:
    rng = random.Random(2)
    before = _layout("Makefile", _symbols("a", "b", "c"), rng, "r1")
    after = _layout("Makefile", _symbols("b", "a", "c"), rng, "r2")
    moved, _reasons = _run("Makefile", before, after)
    assert moved == {unit_key("symbol", "Makefile", "a", 0), unit_key("symbol", "Makefile", "b", 0)}


def test_lines_added_or_removed_elsewhere_move_no_part() -> None:
    rng = random.Random(3)
    before = _layout("run.sh", _symbols("a", "b", "c", "d"), rng, "r1")
    # a new definition in the middle, one deleted at the end, every line number below the insertion shifted
    after = _layout("run.sh", [*_symbols("a", "b"), _Part("fresh", "symbol", 9), *_symbols("c")], rng, "r2")
    moved, reasons = _run("run.sh", before, after)
    assert moved == set()
    assert {code for key, code in reasons.items() if key != unit_key("symbol", "run.sh", "fresh", 0)} <= {
        "content_unchanged"
    }


def test_a_changed_definition_keeps_the_content_reason_and_a_moved_one_that_also_changed_is_not_double_counted() -> (
    None
):
    rng = random.Random(4)
    before = _layout("run.sh", _symbols("a", "b", "c"), rng, "r1")
    after = _layout("run.sh", _symbols("c", "a", "b"), rng, "r2")
    edited = [replace(unit, content_fingerprint="changed") if unit.symbol == "c" else unit for unit in after]
    _moved, reasons = _run("run.sh", before, edited)
    assert reasons[unit_key("symbol", "run.sh", "c", 0)] == "content_changed"


def test_a_request_for_changes_is_never_withdrawn_by_a_move() -> None:
    rng = random.Random(5)
    before = _layout("run.sh", _symbols("a", "b", "c"), rng, "r1")
    after = _layout("run.sh", _symbols("c", "a", "b"), rng, "r2")
    mark = _mark(next(unit for unit in before if unit.symbol == "c"), "needs_changes")
    result = reconcile(before, after, [mark])
    assert result.reopened == () and result.carried == (mark,)


def test_a_part_with_an_unknown_fingerprint_is_left_out_of_the_comparison() -> None:
    rng = random.Random(6)
    before = _layout("run.sh", _symbols("a", "b", "c"), rng, "r1")
    after = _layout("run.sh", _symbols("c", "a", "b"), rng, "r2")
    unknown = [replace(unit, fingerprint_method="unknown") if unit.symbol == "c" else unit for unit in after]
    assert revisions._order_moved(before, unknown, {}) == frozenset()


def test_a_renamed_file_keeps_the_order_check() -> None:
    rng = random.Random(7)
    before = _layout("old.sh", _symbols("a", "b", "c"), rng, "r1")
    after = _layout("new.sh", _symbols("c", "a", "b"), rng, "r2")
    result = reconcile(before, after, [_mark(unit) for unit in before], renames=[("old.sh", "new.sh")])
    # a renamed file moves every mark to a new key, so all three are written back; only one of them reopens
    assert {mark.unit_key: mark.state for mark in result.reopened} == {
        unit_key("symbol", "new.sh", "c", 0): "changed_since_review",
        unit_key("symbol", "new.sh", "a", 0): "reviewed",
        unit_key("symbol", "new.sh", "b", 0): "reviewed",
    }
    assert {key: code for _r, key, code in result.reasons}[unit_key("symbol", "new.sh", "c", 0)] == ORDER_REASON
    assert [code for _r, _k, code in result.reasons].count(ORDER_REASON) == 1


def test_a_block_between_definitions_moves_only_when_a_definition_that_stayed_crosses_it() -> None:
    rng = random.Random(8)
    block = _Part("h0", "hunk", 3)
    before = _layout("run.sh", [*_symbols("a", "b"), block, *_symbols("c")], rng, "r1")
    # ``b`` stays between ``a`` and ``c``; the block moves from after ``b`` to before ``a``: it crossed two parts that stayed
    after = _layout("run.sh", [block, *_symbols("a", "b", "c")], rng, "r2")
    moved, _reasons = _run("run.sh", before, after)
    assert moved == {_key("run.sh", block)}
    # the same block, left where it was, with lines added above it
    after = _layout("run.sh", [_Part("new", "symbol", 7), *_symbols("a", "b"), block, *_symbols("c")], rng, "r2")
    assert _run("run.sh", before, after)[0] == set()


def test_a_block_beside_a_single_definition_is_checked_too() -> None:
    rng = random.Random(9)
    block = _Part("h0", "hunk", 3)
    before = _layout("Makefile", [_Part("only", "symbol", 2), block], rng, "r1")
    after = _layout("Makefile", [block, _Part("only", "symbol", 2)], rng, "r2")
    assert _run("Makefile", before, after)[0] == {_key("Makefile", block)}


# --------------------------------------------------------------------------------------------------------------------
# The differential. The oracle is the definition of the requirement, not of the algorithm: look at every pair of parts and
# ask whether their relative order flipped.
# --------------------------------------------------------------------------------------------------------------------


def _flipped_pairs(before: list[str], after: list[str], kinds: dict[str, str]) -> set[frozenset[str]]:
    """Pairs of surviving parts whose order differs, ignoring two blocks (those are told apart by their ordinal)."""

    common = [name for name in before if name in set(after)]
    place = {name: index for index, name in enumerate(after)}
    out: set[frozenset[str]] = set()
    for index, first in enumerate(common):
        for second in common[index + 1 :]:
            if place[first] > place[second] and not (kinds[first] == "hunk" and kinds[second] == "hunk"):
                out.add(frozenset((first, second)))
    return out


def _program(rng: random.Random, path: str, *, reorder: bool) -> tuple[list[_Part], list[_Part]]:
    """A file as a list of parts and the next revision of it: re-spaced, with parts added and removed, maybe reordered."""

    names = [_Part(f"d{i}", "symbol", rng.randint(1, 5)) for i in range(rng.randint(2, 12))]
    blocks = [_Part(f"h{i}", "hunk", rng.randint(1, 6)) for i in range(rng.randint(0, 3))]
    before = [*names, *blocks]
    rng.shuffle(before)
    after = [replace(part, length=max(1, part.length + rng.choice((0, 0, 0, 1, -1, 3)))) for part in before]
    for index in range(rng.randint(0, 3)):
        after.insert(rng.randint(0, len(after)), _Part(f"new{index}", "symbol", rng.randint(1, 6)))
    for _ in range(rng.randint(0, 3)):
        # a block is never added or removed: a changed hunk count resets hunk marks (``hunks_renumbered``), another rule
        symbols = [index for index, part in enumerate(after) if part.kind == "symbol"]
        if len(symbols) > 2:
            after.pop(rng.choice(symbols))
    if reorder:
        common = [index for index, part in enumerate(after) if not part.name.startswith("new")]
        if len(common) >= 2:
            mode = rng.choice(("move", "swap", "shuffle", "reverse"))
            picked = [after[index] for index in common]
            original = [part.name for part in picked]
            if mode == "move":
                part = picked.pop(rng.randrange(len(picked)))
                picked.insert(rng.randrange(len(picked) + 1), part)
            elif mode == "swap":
                left, right = rng.sample(range(len(picked)), 2)
                picked[left], picked[right] = picked[right], picked[left]
            elif mode == "shuffle":
                rng.shuffle(picked)
            else:
                picked.reverse()
            if [part.name for part in picked] != original:
                for index, part in zip(common, picked, strict=True):
                    after[index] = part
    return before, after


@dataclass
class _Tally:
    programs: int = 0
    reordered: int = 0
    flipped_pairs: int = 0
    fail_open: int = 0
    reopened: int = 0
    unexplained: int = 0
    shifted_reopened: int = 0
    notes: Counter[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.notes = Counter()


def _differential(path: str, seed: int, programs: int) -> _Tally:
    tally = _Tally()
    for index in range(programs):
        rng = random.Random(f"{seed}:{path}:{index}")
        reorder = index % 2 == 0
        before_parts, after_parts = _program(rng, path, reorder=reorder)
        before = _layout(path, before_parts, rng, "r1")
        after = _layout(path, after_parts, rng, "r2")
        moved, _reasons = _run(path, before, after)
        kinds = {part.name: part.kind for part in (*before_parts, *after_parts)}
        by_name = {_key(path, part): part.name for part in (*before_parts, *after_parts)}
        flips = _flipped_pairs([p.name for p in before_parts], [p.name for p in after_parts], kinds)
        reopened = {by_name[key] for key in moved}
        tally.programs += 1
        tally.reopened += len(reopened)
        if flips:
            tally.reordered += 1
            tally.flipped_pairs += len(flips)
        # fail-open: a flipped pair of which neither part reopened is an order change the reviewer is never told about
        tally.fail_open += sum(1 for pair in flips if not pair & reopened)
        # a part reopened for its place is part of some flipped pair; one that is not was reopened for nothing
        involved = set().union(*flips) if flips else set()
        tally.unexplained += len(reopened - involved)
        if not flips:
            tally.shifted_reopened += len(reopened)
    return tally


@pytest.mark.parametrize("path", ["scripts/build.sh", "Makefile", "mk/common.mk"])
@pytest.mark.parametrize("seed", SEEDS)
def test_differential_no_reorder_is_missed_and_no_shift_reopens(path: str, seed: int) -> None:
    tally = _differential(path, seed, PROGRAMS_PER_SEED)
    assert tally.reordered > PROGRAMS_PER_SEED // 4, "the generator must actually reorder"
    assert tally.flipped_pairs > 0 and tally.reopened > 0
    assert tally.fail_open == 0, tally
    assert tally.unexplained == 0, tally
    assert tally.shifted_reopened == 0, tally


@pytest.mark.parametrize("seed", SEEDS)
def test_differential_other_languages_never_reopen_for_order(seed: int) -> None:
    for path in ("src/app.py", "main.go", "lib/x.rb", "Makefile.md"):
        tally = _differential(path, seed, 120)
        assert tally.reopened == 0, (path, tally)


def test_the_differential_has_teeth(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the order check turned off the same oracle reports misses: it measures the behaviour, not the algorithm."""

    monkeypatch.setattr(revisions, "_order_moved", lambda *_args, **_kwargs: frozenset())
    assert _differential("Makefile", SEEDS[0], 100).fail_open > 0
    # and a check that reopens whatever moved by line number is caught by the shift rule
    monkeypatch.setattr(
        revisions,
        "_order_moved",
        lambda before, after, _aliases: frozenset(
            unit.unit_key
            for old, unit in zip(
                sorted(before, key=lambda u: u.unit_key), sorted(after, key=lambda u: u.unit_key), strict=False
            )
            if old.start_line != unit.start_line
        ),
    )
    assert _differential("Makefile", SEEDS[0], 100).shifted_reopened > 0


# --------------------------------------------------------------------------------------------------------------------
# The real pipeline: a git repository, real captures, real marks.
# --------------------------------------------------------------------------------------------------------------------


class _Repo:
    def __init__(self, root: Path, store_root: Path, base: dict[str, str] | None = None) -> None:
        """A repository whose baseline commit holds the README and *base* (the code the branch starts from)."""

        self.root, self.store_root = root, store_root
        root.mkdir(parents=True, exist_ok=True)
        self.git = pygit2.init_repository(str(root), initial_head="main")
        self.git.config["user.name"], self.git.config["user.email"] = "Fixture Tester", "fixture@example.com"
        self._commit({"README": "base\n", **(base or {})}, [])
        self.store = ReviewStore(store_root)
        self.session = open_or_create_session(self.store, root, resolve_rev_range(root))

    def _commit(self, files: dict[str, str], parents: list[Any]) -> None:
        for path, text in files.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            self.git.index.add(path)  # only these paths: the branch's own work stays out of the commit
        self.git.index.write()
        signature = pygit2.Signature("Fixture Tester", "fixture@example.com", 1700000000 + len(parents), 0)
        self.git.create_commit("HEAD", signature, signature, "base", self.git.index.write_tree(), parents)

    def rebase_onto(self, files: dict[str, str]) -> None:
        """Upstream moved: a new base commit holding *files*, with the branch's own work still in the working tree."""

        self._commit(files, [self.git.head.target])

    def advance(self, files: dict[str, str]) -> Any:
        for path, text in files.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return refresh(
            self.store,
            self.session,
            self.root,
            store_root=self.store_root,
            rng=resolve_rev_range(self.root),
            limit=500,
        )

    def targets(self, revision: Any) -> tuple[ReviewTarget, ...]:
        units = self.store.list_units(revision.id)
        marks = self.store.list_marks(self.session.id, reviewer_id="local")
        frontier = compute_frontier(self.session.id, "local", revision, units, marks)
        return derive_review_targets(units, frontier.entries, read_packet_json(self.store, revision))

    def mark_everything(self, revision: Any) -> None:
        by_key = {unit.unit_key: unit for unit in self.store.list_units(revision.id)}
        for target in self.targets(revision):
            mark_unit(self.store, self.session, revision, by_key[target.unit_key], state="reviewed")


def _shell_defs(names: list[str]) -> str:
    return "".join(
        f"{name}() {{\n  echo {name}\n}}\n\n" if name.startswith("fn") else f"{name}={len(name)}\n\n" for name in names
    )


def _make_defs(names: list[str]) -> str:
    return "".join(
        f"{name}:\n\t@echo {name}\n\n" if name.startswith("tgt") else f"{name} ?= {name.lower()}\n\n" for name in names
    )


# shell variable assignments get no symbol unit today (only functions do), so a shell program is functions only
_REAL = {"run.sh": ("fn", "fn", _shell_defs), "Makefile": ("tgt", "VAR", _make_defs)}


def _real_program(rng: random.Random, path: str, *, reorder: bool) -> tuple[list[str], list[str]]:
    function_prefix, variable_prefix, _render = _REAL[path]
    names = [
        f"{function_prefix if rng.random() < 0.5 else variable_prefix}_{index}" for index in range(rng.randint(4, 10))
    ]
    names = [name.replace("fn_", "fn").replace("tgt_", "tgt") for name in names]
    after = list(names)
    if reorder:
        mode = rng.choice(("move", "swap", "shuffle"))
        if mode == "move":
            name = after.pop(rng.randrange(len(after)))
            after.insert(rng.choice([i for i in range(len(after) + 1) if i != names.index(name)]), name)
        elif mode == "swap":
            left, right = rng.sample(range(len(after)), 2)
            after[left], after[right] = after[right], after[left]
        else:
            while after == names:
                rng.shuffle(after)
    else:
        for index in range(rng.randint(1, 3)):
            after.insert(rng.randint(0, len(after)), f"{function_prefix}extra{index}")
        if len(after) > 3 and rng.random() < 0.5:
            after.pop(rng.randrange(len(after)))
    return names, after


def _real_differential(tmp_path: Path, path: str, programs: int) -> _Tally:
    tally = _Tally()
    render = _REAL[path][2]
    for index in range(programs):
        rng = random.Random(f"{REAL_SEED}:{path}:{index}")
        reorder = index % 2 == 0
        names, after = _real_program(rng, path, reorder=reorder)
        repo = _Repo(tmp_path / f"{path.replace('.', '_')}-{index}" / "repo", tmp_path / f"{path}-{index}" / "store")
        first = repo.advance({path: render(names)})
        repo.mark_everything(first.revision)
        second = repo.advance({path: render(after)})
        states = {t.symbol: t.state for t in repo.targets(second.revision) if t.path == path and t.kind == "symbol"}
        assert set(after) <= set(states), (path, index, sorted(states), after)
        names_by_key = {unit.unit_key: unit.symbol for unit in repo.store.list_units(second.revision.id)}
        by_order = {
            names_by_key[event.unit_key]
            for event in repo.store.list_mark_events(repo.session.id)
            if event.revision_id == second.revision.id and event.reason == mark_event_reason(ORDER_REASON)
        }
        # reopened for any reason: that is what a reviewer is told. Reopened for its place: that is what the rule decided.
        reopened = {name for name, state in states.items() if state != "reviewed" and name in names}
        flips = _flipped_pairs(names, after, dict.fromkeys(names + after, "symbol"))
        tally.programs += 1
        tally.reopened += len(by_order)
        if flips:
            tally.reordered += 1
            tally.flipped_pairs += len(flips)
        tally.fail_open += sum(1 for pair in flips if not pair & reopened)
        tally.unexplained += len(by_order - (set().union(*flips) if flips else set()))
        if not flips:
            tally.shifted_reopened += len(by_order)
    return tally


@pytest.mark.parametrize("path", sorted(_REAL))
def test_real_pipeline_reorder_reopens_and_an_insertion_or_deletion_elsewhere_does_not(
    tmp_path: Path, path: str
) -> None:
    tally = _real_differential(tmp_path, path, REAL_PROGRAMS)
    assert tally.programs == REAL_PROGRAMS
    assert tally.reordered >= REAL_PROGRAMS // 3, tally
    assert tally.fail_open == 0, tally
    assert tally.unexplained == 0, tally
    assert tally.shifted_reopened == 0, tally


def test_real_pipeline_the_same_reorder_in_python_is_carried(tmp_path: Path) -> None:
    first_text = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
    second_text = "def b():\n    return 2\n\n\ndef a():\n    return 1\n"
    repo = _Repo(tmp_path / "repo", tmp_path / "store")
    first = repo.advance({"m.py": first_text})
    repo.mark_everything(first.revision)
    second = repo.advance({"m.py": second_text})
    states = {t.symbol: t.state for t in repo.targets(second.revision) if t.path == "m.py" and t.kind == "symbol"}
    assert states == {"a": "reviewed", "b": "reviewed"}


# --------------------------------------------------------------------------------------------------------------------
# Review round 1: a part that crosses lines of the BASE file (the part-to-part order cannot see those), a hunk that moves
# across identical context, stickiness, the classification of extensionless scripts, and the paths that skipped the check.
# --------------------------------------------------------------------------------------------------------------------


def _label(unit: ReviewUnit) -> tuple[str, str]:
    return unit.path, unit.symbol or f"{unit.kind}#{unit.ordinal}"


def _order_reopened(repo: _Repo, revision: Any, reason: str = ORDER_REASON) -> set[tuple[str, str]]:
    """The parts the order rule reopened on *revision* for *reason*, by ``(path, name)``: read from the mark events."""

    units = {unit.unit_key: unit for unit in repo.store.list_units(revision.id)}
    return {
        _label(units[event.unit_key])
        for event in repo.store.list_mark_events(repo.session.id)
        if event.revision_id == revision.id
        and mark_event_reason(reason) in event.reason  # a moved symbol's reason also names where it came from
        and event.unit_key in units
    }


def _target_states(repo: _Repo, revision: Any, path: str) -> dict[tuple[str, str], str]:
    units = {unit.unit_key: unit for unit in repo.store.list_units(revision.id)}
    return {_label(units[t.unit_key]): t.state for t in repo.targets(revision) if t.path == path}


_SHELL_BASE = "#!/bin/sh\nset -e\necho start\nmain\n"
_SHELL_ABOVE = "#!/bin/sh\nset -e\nmain() { echo hi; }\necho start\nmain\n"
_SHELL_BELOW = "#!/bin/sh\nset -e\necho start\nmain\nmain() { echo hi; }\n"
_MAKE_BASE = "all: $(TOOL)\n\t@echo $(TOOL)\n"
_MAKE_ABOVE = "TOOL := x\nall: $(TOOL)\n\t@echo $(TOOL)\n"
_MAKE_BELOW = "all: $(TOOL)\n\t@echo $(TOOL)\nTOOL := x\n"

_ACROSS_BASE = [
    ("run.sh", _SHELL_BASE, _SHELL_ABOVE, _SHELL_BELOW, "main"),
    ("Makefile", _MAKE_BASE, _MAKE_ABOVE, _MAKE_BELOW, "TOOL"),
]


@pytest.mark.parametrize(("path", "base", "first", "second", "name"), _ACROSS_BASE)
def test_a_definition_that_moves_across_base_lines_reopens_and_staying_put_does_not(
    tmp_path: Path, path: str, base: str, first: str, second: str, name: str
) -> None:
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: base})
    one = repo.advance({path: first})
    repo.mark_everything(one.revision)
    assert _target_states(repo, one.revision, path)[(path, name)] == "reviewed"
    # lines added elsewhere in the file: the definition did not move
    quiet = repo.advance({path: first + ("echo tail\n" if path.endswith("sh") else "$(info tail)\n")})
    assert _target_states(repo, quiet.revision, path)[(path, name)] == "reviewed"
    assert _order_reopened(repo, quiet.revision) == set()
    two = repo.advance({path: second})
    assert _order_reopened(repo, two.revision) == {(path, name)}
    assert _target_states(repo, two.revision, path)[(path, name)] != "reviewed"


# A line inserted into a run of identical base lines: moved along the run it has the same patch (same context either side).
_STEPS = {"run.sh": "echo step\n", "bin/run": "echo step\n", "rules.mk": "$(info step)\n", "Makefile": "$(info step)\n"}
_HEAD = {"run.sh": "#!/bin/sh\n", "bin/run": "#!/bin/bash\n", "rules.mk": "", "Makefile": ""}
_INSERTED = {"run.sh": "set -e\n", "bin/run": "set -e\n", "rules.mk": "$(info moved)\n", "Makefile": "$(info moved)\n"}


def _steps(path: str, *, at: int | None = None, total: int = 12) -> str:
    rows = [_STEPS[path]] * total
    if at is not None:
        rows.insert(at, _INSERTED[path])
    return _HEAD[path] + "".join(rows)


@pytest.mark.parametrize("path", sorted(_STEPS))
def test_a_hunk_that_moves_across_identical_context_reopens(tmp_path: Path, path: str) -> None:
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: _steps(path)})
    one = repo.advance({path: _steps(path, at=4)})
    patches = {
        unit.content_fingerprint.partition("@")[0]
        for unit in repo.store.list_units(one.revision.id)
        if unit.kind == "hunk"
    }
    repo.mark_everything(one.revision)
    assert _target_states(repo, one.revision, path) == {(path, "hunk#0"): "reviewed"}
    two = repo.advance({path: _steps(path, at=8)})
    # the same patch with the same context: only the base lines above it differ
    assert patches == {
        unit.content_fingerprint.partition("@")[0]
        for unit in repo.store.list_units(two.revision.id)
        if unit.kind == "hunk"
    }
    assert _order_reopened(repo, two.revision) == {(path, "hunk#0")}
    assert _target_states(repo, two.revision, path)[(path, "hunk#0")] != "reviewed"
    back = repo.advance({path: _steps(path, at=4)})
    assert _target_states(repo, back.revision, path)[(path, "hunk#0")] == "reviewed"  # back where the reviewer saw it


@pytest.mark.parametrize("path", sorted(_STEPS))
def test_a_base_only_shift_moves_no_part(tmp_path: Path, path: str) -> None:
    """A rebase that only adds upstream lines above (and in the middle of) the file moves no anchor."""

    upstream = "# upstream one\n# upstream two\n"
    shifted_base = _HEAD[path] + upstream + _steps(path)[len(_HEAD[path]) :]
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: _steps(path)})
    one = repo.advance({path: _steps(path, at=4)})
    repo.mark_everything(one.revision)
    repo.rebase_onto({path: shifted_base})
    two = repo.advance({path: shifted_base.replace(_STEPS[path] * 4, _STEPS[path] * 4 + _INSERTED[path], 1)})
    assert _order_reopened(repo, two.revision) == set()
    assert _target_states(repo, two.revision, path)[(path, "hunk#0")] == "reviewed"


# --------------------------------------------------------------------------------------------------------------------
# The differential over BASE code: the baseline commit holds lines of its own and the branch adds definitions among them.
# Oracle (written from the rule, not from the implementation): a definition crossed base lines when the number of base
# lines above it changed; a pair of definitions flipped when their relative order changed. Every crossing leaves the
# crossing definition reopened, for whatever reason (fail-open == 0; its text can also have changed with what follows it); every flipped pair reopens at least one of the two; a revision that only inserts
# or deletes definitions reopens nothing.
# --------------------------------------------------------------------------------------------------------------------

BASE_SEED = 20261120
BASE_PROGRAMS = int(os.environ.get("LEMONCROW_PART_ORDER_BASE_PROGRAMS", "12"))


@dataclass
class _BaseTally:
    programs: int = 0
    crossed: int = 0
    flipped_pairs: int = 0
    fail_open: int = 0
    unexplained: int = 0
    shifted_reopened: int = 0


def _base_lines(path: str, count: int) -> list[str]:
    if path == "run.sh":
        return ["#!/bin/sh"] + [f"echo base{index}" for index in range(1, count)]
    return [f"$(info base{index})" for index in range(count)]


def _render_gapped(path: str, base: list[str], gaps: dict[int, list[str]]) -> str:
    render = _REAL[path][2]
    out = []
    for index, line in enumerate(base):
        if index in gaps:
            out.append(render(gaps[index]))
        out.append(line + "\n")
    if len(base) in gaps:
        out.append(render(gaps[len(base)]))
    return "".join(out)


def _base_program(
    rng: random.Random, path: str, base: list[str], *, reorder: bool
) -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    function_prefix, variable_prefix, _ = _REAL[path]
    count = rng.randint(3, 7)
    names = [(function_prefix if rng.random() < 0.5 else variable_prefix) + f"_{n}" for n in range(count)]
    names = [name.replace("fn_", "fn").replace("tgt_", "tgt") for name in names]
    first: dict[int, list[str]] = {}
    for name in names:
        first.setdefault(rng.randint(1, len(base) - 1), []).append(name)
    after = {gap: list(items) for gap, items in first.items()}
    if reorder:
        moved = rng.sample(names, rng.randint(1, max(1, count // 2)))
        for name in moved:
            for items in after.values():
                if name in items:
                    items.remove(name)
        for name in moved:
            after.setdefault(rng.randint(1, len(base) - 1), []).insert(rng.randint(0, 1), name)
    else:
        for extra in range(rng.randint(1, 3)):
            after.setdefault(rng.randint(1, len(base) - 1), []).append(f"{function_prefix}extra{extra}")
        victim = rng.choice(names)
        for items in after.values():
            if victim in items and len(names) > 3:
                items.remove(victim)
    return first, {gap: items for gap, items in after.items() if items}


def _place(gaps: dict[int, list[str]]) -> tuple[dict[str, int], list[str]]:
    """The base lines above each definition, and the definitions in file order."""

    above, order = {}, []
    for gap in sorted(gaps):
        for name in gaps[gap]:
            above[name] = gap
            order.append(name)
    return above, order


def _base_differential(tmp_path: Path, path: str, programs: int) -> _BaseTally:
    tally = _BaseTally()
    for index in range(programs):
        rng = random.Random(f"{BASE_SEED}:{path}:{index}")
        reorder = index % 2 == 0
        base = _base_lines(path, rng.randint(6, 10))
        first, after = _base_program(rng, path, base, reorder=reorder)
        repo = _Repo(
            tmp_path / f"{path.replace('.', '_')}-{index}" / "repo",
            tmp_path / f"{path}-{index}" / "store",
            {path: _render_gapped(path, base, {})},
        )
        one = repo.advance({path: _render_gapped(path, base, first)})
        repo.mark_everything(one.revision)
        two = repo.advance({path: _render_gapped(path, base, after)})
        states = {t.symbol: t.state for t in repo.targets(two.revision) if t.path == path and t.kind == "symbol"}
        before_above, before_order = _place(first)
        after_above, after_order = _place(after)
        kept = [name for name in before_order if name in after_above]
        assert set(after_order) <= set(states), (path, index, sorted(states), after_order)
        by_order = {
            label[1] for label in _order_reopened(repo, two.revision) if label[0] == path and label[1] in states
        }
        reopened = {name for name, state in states.items() if state != "reviewed" and name in before_above}
        crossed = {name for name in kept if before_above[name] != after_above[name]}
        flips = _flipped_pairs(
            kept, [name for name in after_order if name in before_above], dict.fromkeys(kept, "symbol")
        )
        tally.programs += 1
        tally.crossed += len(crossed)
        tally.flipped_pairs += len(flips)
        tally.fail_open += len(crossed - reopened) + sum(1 for pair in flips if not pair & reopened)
        tally.unexplained += len(by_order - crossed - (set().union(*flips) if flips else set()))
        if not crossed and not flips:
            tally.shifted_reopened += len(by_order)
    return tally


@pytest.mark.parametrize("path", sorted(_REAL))
def test_real_pipeline_base_code_crossings_reopen_with_no_fail_open(tmp_path: Path, path: str) -> None:
    tally = _base_differential(tmp_path, path, BASE_PROGRAMS)
    assert tally.programs == BASE_PROGRAMS
    assert tally.crossed >= BASE_PROGRAMS // 3, tally  # the generator really crosses base lines
    assert tally.fail_open == 0, tally
    assert tally.unexplained == 0, tally
    assert tally.shifted_reopened == 0, tally


def test_the_base_differential_has_teeth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the anchor comparison the same programs go fail-open: the oracle is not vacuous."""

    monkeypatch.setattr(units_module._PartAnchors, "at_line", lambda self, new_line: "top")
    tally = _base_differential(tmp_path, "run.sh", 12)
    assert tally.fail_open > 0, tally


# --------------------------------------------------------------------------------------------------------------------
# Classification (m2): by name, and by a shell shebang for a file with no telling name.
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "text", "expected"),
    [
        ("bin/run", "#!/bin/sh\nset -e\n", True),
        ("bin/run", "#!/bin/bash\n", True),
        ("bin/run", "#!/usr/bin/zsh -f\n", True),
        ("bin/run", "#!/bin/ksh\n", True),
        ("bin/run", "#!/bin/dash\n", True),
        ("bin/run", "#!/usr/bin/env sh\n", True),
        ("bin/run", "#!/usr/bin/env bash\n", True),
        ("bin/run", "#! /usr/bin/env -S bash -e\n", True),
        ("bin/run", "#!/usr/bin/python3\nimport os\n", False),
        ("bin/run", "#!/usr/bin/env python3\n", False),
        ("bin/run", "#!/usr/bin/env node\n", False),
        ("bin/run", "#!/bin/shellcheck\n", False),
        ("bin/run", "echo hi\n#!/bin/sh\n", False),
        ("bin/run", "", False),
        (
            "tool.py",
            "#!/bin/sh\n",
            True,
        ),  # what the first line says wins over a language guess: it is run by that shell
        ("rules.mak", "", True),
        ("Makefile.am", "", True),
        ("Makefile.inc", "", True),
        ("makefile.win", "", True),
        ("GNUmakefile.local", "", True),
        ("Makefile.md", "", False),
        ("Makefile.json", "", False),
        ("makefile.txt", "", False),
    ],
)
def test_classification_reads_the_shebang_and_the_make_name_variants(path: str, text: str, expected: bool) -> None:
    assert orders_parts(path, text) is expected


def test_anchor_helpers_round_trip_and_the_bare_fingerprint_ignores_the_anchor() -> None:
    fingerprint = symbol_fingerprint("function", "a", "a() { :; }\n")
    assert split_anchor(fingerprint) == (fingerprint, "")
    anchored = with_anchor(fingerprint, "abc123")
    assert split_anchor(anchored) == (fingerprint, "abc123")
    assert with_anchor(anchored, ANCHOR_MOVED) == f"{fingerprint}@{ANCHOR_MOVED}"
    assert bare_fingerprint(anchored) == bare_fingerprint(fingerprint)


# --------------------------------------------------------------------------------------------------------------------
# Stickiness: a part reopened for its place stays reopened until a person answers it again.
# --------------------------------------------------------------------------------------------------------------------


def test_a_part_reopened_for_its_place_stays_reopened_through_revisions_that_move_nothing(tmp_path: Path) -> None:
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {"run.sh": _SHELL_BASE})
    one = repo.advance({"run.sh": _SHELL_ABOVE})
    repo.mark_everything(one.revision)
    two = repo.advance({"run.sh": _SHELL_BELOW})
    assert _order_reopened(repo, two.revision) == {("run.sh", "main")}
    for tail in ("echo a\n", "echo a\necho b\n", ""):
        later = repo.advance({"run.sh": _SHELL_BELOW + tail})
        assert _target_states(repo, later.revision, "run.sh")[("run.sh", "main")] == "changed_since_review", tail
    unit = next(u for u in repo.store.list_units(later.revision.id) if u.symbol == "main")
    mark_unit(repo.store, repo.session, later.revision, unit, state="reviewed")
    last = repo.advance({"run.sh": _SHELL_BELOW + "echo c\n"})
    assert _target_states(repo, last.revision, "run.sh")[("run.sh", "main")] == "reviewed"
    assert _order_reopened(repo, last.revision) == set()


# --------------------------------------------------------------------------------------------------------------------
# A verdict made before anchors existed carries (nothing says it moved) and is anchored where the part is now.
# --------------------------------------------------------------------------------------------------------------------


def _anchored(unit: ReviewUnit, anchor: str) -> ReviewUnit:
    return replace(unit, content_fingerprint=with_anchor(unit.content_fingerprint, anchor))


def test_a_verdict_without_an_anchor_is_read_once_and_never_carried() -> None:
    """A verdict from before places were kept cannot say where its part sat, so it reopens (and is not trusted on first use)."""

    rng = random.Random(11)
    before = _layout("run.sh", _symbols("a", "b"), rng, "r1")
    after = [
        _anchored(unit, f"p{index}") for index, unit in enumerate(_layout("run.sh", _symbols("a", "b"), rng, "r2"))
    ]
    result = reconcile(before, after, [_mark(unit) for unit in before])
    assert result.carried == ()
    assert result.restamped == ()
    assert {mark.unit_key for mark in result.reopened} == {unit.unit_key for unit in after}
    assert {mark.state for mark in result.reopened} == {"changed_since_review"}
    assert {code for _r, _k, code in result.reasons} == {PLACE_REASON}
    assert REASONS[PLACE_REASON].state == "reopened"
    # the reopened verdict keeps what it judged (no place); a person's new answer records the place, and that holds
    again = reconcile(
        after,
        after,
        [replace(_mark(unit), content_fingerprint=unit.content_fingerprint) for unit in after],
    )
    assert again.reopened == ()
    assert {code for _r, _k, code in again.reasons} == {"content_unchanged"}


def test_a_definition_moved_below_its_base_call_with_anchorless_verdicts_is_read() -> None:
    """The reviewer's probe: ``main()`` moved below the call, every verdict from before anchors existed."""

    rng = random.Random(15)
    before = _layout("run.sh", _symbols("main"), rng, "r1")
    after = [_anchored(unit, "below-the-call") for unit in _layout("run.sh", _symbols("main"), rng, "r2")]
    result = reconcile(before, after, [_mark(unit) for unit in before])
    assert result.carried == ()
    assert all(mark.state == "changed_since_review" for mark in result.reopened) and result.reopened


def test_a_changed_anchor_reopens_with_the_place_reason_and_the_same_anchor_carries() -> None:
    rng = random.Random(12)
    before = [_anchored(u, "x") for u in _layout("run.sh", _symbols("a", "b"), rng, "r1")]
    after = _layout("run.sh", _symbols("a", "b"), rng, "r2")
    after = [_anchored(after[0], "x"), _anchored(after[1], "elsewhere")]
    result = reconcile(before, after, [_mark(unit) for unit in before])
    assert {mark.unit_key for mark in result.reopened} == {after[1].unit_key}
    assert {key: code for _r, key, code in result.reasons} == {
        after[0].unit_key: "content_unchanged",
        after[1].unit_key: ORDER_REASON,
    }
    # the verdict keeps the place it judged, which no longer matches, so it stays reopened without needing a tombstone
    assert split_anchor(result.reopened[0].content_fingerprint)[1] == "x"


# --------------------------------------------------------------------------------------------------------------------
# The two paths that skipped the check (m5).
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "anchors", "expected"),
    [
        ("run.sh", ("p0", "p0"), "fingerprint_restored"),  # identity knowable again, nothing about the place changed
        ("Makefile", ("p0", "p0"), "fingerprint_restored"),
        ("run.sh", ("p0", "elsewhere"), ORDER_REASON),  # the place did change: the honest reason, with the tombstone
        ("a.py", ("", ""), "fingerprint_restored"),
    ],
)
def test_a_restored_verdict_is_read_again_only_when_its_place_changed(
    path: str, anchors: tuple[str, str], expected: str
) -> None:
    rng = random.Random(13)
    judged_anchor, now_anchor = anchors
    before = [replace(unit, fingerprint_method="unknown") for unit in _layout(path, _symbols("a", "b"), rng, "r1")]
    after = _layout(path, _symbols("a", "b"), rng, "r2")
    if now_anchor:
        after = [_anchored(unit, now_anchor) for unit in after]
    judged = [
        replace(
            _mark(unit, "unknown"),
            content_fingerprint=(
                with_anchor(unit.content_fingerprint, judged_anchor) if judged_anchor else unit.content_fingerprint
            ),
        )
        for unit in after
    ]
    result = reconcile(before, after, judged)
    assert {code for _r, _k, code in result.reasons} == {expected}
    assert bool(result.reopened) is True  # the downgraded mark is written back either way
    reopens = expected == ORDER_REASON
    assert {mark.state for mark in result.reopened} == ({"changed_since_review"} if reopens else {"reviewed"})
    if reopens:
        assert {split_anchor(mark.content_fingerprint)[1] for mark in result.reopened} == {
            judged_anchor
        }  # it keeps the place it judged, which no longer matches


# --------------------------------------------------------------------------------------------------------------------
# The base-only door keeps the place check (m1).
# --------------------------------------------------------------------------------------------------------------------


def test_a_base_only_rebase_keeps_an_anchored_verdict_whose_anchor_moved_only_with_the_base() -> None:
    rng = random.Random(21)
    before = [_anchored(u, "old-base") for u in _layout("run.sh", _symbols("a", "b"), rng, "r1")]
    after = [_anchored(u, "new-base") for u in _layout("run.sh", _symbols("a", "b"), rng, "r2")]
    result = reconcile(before, after, [_mark(u) for u in before], base_only=frozenset(u.unit_key for u in after))
    assert {code for _r, _k, code in result.reasons} == {"base_only_equivalent"}
    assert result.reopened == ()
    assert {m.content_fingerprint for m in result.rebased} == {u.content_fingerprint for u in after}


def test_a_base_only_rebase_still_reads_a_verdict_from_before_places_were_kept_once() -> None:
    rng = random.Random(22)
    before = _layout("run.sh", _symbols("a", "b"), rng, "r1")
    after = [_anchored(u, "new-base") for u in _layout("run.sh", _symbols("a", "b"), rng, "r2")]
    result = reconcile(before, after, [_mark(u) for u in before], base_only=frozenset(u.unit_key for u in after))
    assert result.rebased == ()
    assert result.carried == ()
    assert {code for _r, _k, code in result.reasons} == {PLACE_REASON}
    assert {m.state for m in result.reopened} == {"changed_since_review"}


def test_a_base_only_rebase_of_a_file_that_orders_no_parts_keeps_the_verdict() -> None:
    rng = random.Random(23)
    before = _layout("a.py", _symbols("a", "b"), rng, "r1")
    after = [
        replace(u, content_fingerprint=u.content_fingerprint + "~x")
        for u in _layout("a.py", _symbols("a", "b"), rng, "r2")
    ]
    result = reconcile(before, after, [_mark(u) for u in before], base_only=frozenset(u.unit_key for u in after))
    assert {code for _r, _k, code in result.reasons} == {"base_only_equivalent"}


def test_a_definition_moved_to_another_file_is_read_again_when_that_file_orders_its_parts(tmp_path: Path) -> None:
    cases = {
        "shell": (
            ("a.sh", "greet() {\n  echo hi\n}\n\nkeep() {\n  echo keep\n}\n", "keep() {\n  echo keep\n}\n"),
            ("b.sh", "base() {\n  echo base\n}\n", "base() {\n  echo base\n}\n\ngreet() {\n  echo hi\n}\n"),
            True,
        ),
        "python": (
            ("a.py", "def greet():\n    return 'hi'\n\n\ndef keep():\n    return 1\n", "def keep():\n    return 1\n"),
            ("b.py", "def base():\n    return 0\n", "def base():\n    return 0\n\n\ndef greet():\n    return 'hi'\n"),
            False,
        ),
    }
    for language, ((source, first_a, second_a), (target, first_b, second_b), reopens) in cases.items():
        repo = _Repo(tmp_path / language / "repo", tmp_path / language / "store", {source: "", target: ""})
        one = repo.advance({source: first_a, target: first_b})
        repo.mark_everything(one.revision)
        two = repo.advance({source: second_a, target: second_b})
        states = {(t.path, t.symbol): t.state for t in repo.targets(two.revision) if t.kind == "symbol"}
        if reopens:
            assert states[(target, "greet")] == "changed_since_review", (language, states)
            assert (target, "greet") in _order_reopened(repo, two.revision)
        else:
            assert states[(target, "greet")] == "reviewed", (language, states)
            assert _order_reopened(repo, two.revision) == set()


# --------------------------------------------------------------------------------------------------------------------
# m3: the crossing test is a sweep, not a pair loop.
# --------------------------------------------------------------------------------------------------------------------


def _span_unit(key: str, start: int, end: int) -> ReviewUnit:
    return ReviewUnit(
        revision_id="r",
        unit_key=key,
        kind="hunk",
        path="x.sh",
        content_fingerprint="f",
        fingerprint_method="hunk_patch_sha256",
        start_line=start,
        end_line=end,
    )


def _brute_crossed(spans: list[tuple[ReviewUnit, ReviewUnit]], fixed: list[tuple[ReviewUnit, ReviewUnit]]) -> set[str]:
    crossed = set()
    for before, after in spans:
        for old, new in fixed:
            above_then_below = old.end_line < before.start_line and new.start_line > after.end_line
            below_then_above = old.start_line > before.end_line and new.end_line < after.start_line
            if above_then_below or below_then_above:
                crossed.add(after.unit_key)
                break
    return crossed


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
def test_the_crossing_sweep_equals_the_pair_loop_on_random_layouts(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(200):

        def pairs(prefix: str, count: int) -> list[tuple[ReviewUnit, ReviewUnit]]:
            out = []
            for index in range(count):
                a = rng.randint(1, 60)
                b = rng.randint(1, 60)
                out.append(
                    (
                        _span_unit(f"{prefix}{index}", a, a + rng.randint(0, 6)),
                        _span_unit(f"{prefix}{index}", b, b + rng.randint(0, 6)),
                    )
                )
            return out

        spans, fixed = pairs("s", rng.randint(0, 12)), pairs("f", rng.randint(0, 12))
        assert revisions._crossed_spans(spans, fixed) == _brute_crossed(spans, fixed)


def test_the_crossing_sweep_handles_a_large_file_in_well_under_the_pair_loop_time() -> None:
    import time

    rng = random.Random(7)
    count = 20000
    fixed = []
    for index in range(count):
        line = index * 3 + 1
        fixed.append((_span_unit(f"f{index}", line, line + 1), _span_unit(f"f{index}", line + 7, line + 8)))
    spans = []
    for index in range(5000):
        line = rng.randint(1, count * 3)
        spans.append((_span_unit(f"s{index}", line, line), _span_unit(f"s{index}", line, line)))
    started = time.perf_counter()
    revisions._crossed_spans(spans, fixed)
    assert time.perf_counter() - started < 2.0  # the pair loop took 13.7 s on this size


# --------------------------------------------------------------------------------------------------------------------
# m4: the reading says the part moved, not that nothing was reviewed before.
# --------------------------------------------------------------------------------------------------------------------


def test_the_since_review_reading_of_a_moved_part_says_it_moved_even_when_it_was_also_edited(tmp_path: Path) -> None:
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {"run.sh": _SHELL_BASE})
    one = repo.advance({"run.sh": _SHELL_ABOVE})
    repo.mark_everything(one.revision)
    moved_and_commented = _SHELL_BELOW.replace("main() {", "# says hello\nmain() {")
    two = repo.advance({"run.sh": moved_and_commented})
    targets = [t for t in repo.targets(two.revision) if t.state == "changed_since_review"]
    assert [t.symbol for t in targets if t.kind == "symbol"] == ["main"]
    proofs = since_review_proofs(
        repo.store,
        repo.session.id,
        "local",
        two.revision.id,
        targets,
        lambda path: (repo.root / path).read_text(encoding="utf-8"),
    )
    proof = proofs[next(t.target_id for t in targets if t.symbol == "main")]
    assert proof.routine is False
    assert "position among the unchanged lines of an order-sensitive file changed" in proof.detail
    assert "moved" not in proof.detail.lower()
    assert "no earlier review" not in proof.detail.lower()


# --------------------------------------------------------------------------------------------------------------------
# MA1: a file whose place cannot be computed keeps its marks while it does not change, and is read when it does.
# --------------------------------------------------------------------------------------------------------------------


def _oversized_base() -> str:
    """A shell script over ``MAX_BLOB_BYTES`` (an autoconf ``configure``), which the anchor cannot read the base of."""

    base = "#! /bin/sh\n" + "".join(f"echo pad {index}\n" for index in range(40000)) + "echo start\nmain\n"
    assert len(base) > MAX_BLOB_BYTES
    return base


def _oversized_head(base: str, tail: str = "") -> str:
    return base.replace("echo start\n", "main() { echo hi; }\necho start\n", 1) + tail


def test_an_oversized_ordered_file_that_does_not_change_keeps_its_verdicts_across_revisions(tmp_path: Path) -> None:
    base = _oversized_base()
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {"run.sh": base})
    one = repo.advance({"run.sh": _oversized_head(base)})
    parts = [unit for unit in repo.store.list_units(one.revision.id) if unit.path == "run.sh" and unit.kind != "file"]
    assert parts and all(is_unanchored(split_anchor(unit.content_fingerprint)[1]) for unit in parts)
    repo.mark_everything(one.revision)
    marked = _target_states(repo, one.revision, "run.sh")
    assert set(marked.values()) == {"reviewed"}
    for step, other in enumerate(("1\n", "2\n", "3\n"), start=2):
        later = repo.advance({"run.sh": _oversized_head(base), "other.txt": other})
        assert _target_states(repo, later.revision, "run.sh") == marked, f"revision {step}"
        assert _order_reopened(repo, later.revision) == set()
        assert _order_reopened(repo, later.revision, PLACE_REASON) == set()


def test_an_oversized_ordered_file_that_changes_reopens_its_parts_as_unverifiable_not_moved(tmp_path: Path) -> None:
    base = _oversized_base()
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {"run.sh": base})
    # two hunks: one far above the part (the branch edits a pad line) and the part itself, which never changes
    first = _oversized_head(base).replace("echo pad 5\n", "echo pad five\n", 1)
    one = repo.advance({"run.sh": first})
    repo.mark_everything(one.revision)
    assert set(_target_states(repo, one.revision, "run.sh")) == {("run.sh", "hunk#0"), ("run.sh", "hunk#1")}
    two = repo.advance({"run.sh": first.replace("echo pad five\n", "echo pad six\n", 1)})
    states = _target_states(repo, two.revision, "run.sh")
    assert states[("run.sh", "hunk#0")] != "reviewed"  # its own text changed
    # the part's text and hunk are as they were, but the file is not: its place cannot be checked, so it is read
    assert states[("run.sh", "hunk#1")] != "reviewed"
    assert _order_reopened(repo, two.revision, PLACE_REASON) == {("run.sh", "hunk#1")}
    assert _order_reopened(repo, two.revision) == set()  # it does not claim a move
    # nothing about the file changes next: it stays read, until a person answers
    second = first.replace("echo pad five\n", "echo pad six\n", 1)
    three = repo.advance({"run.sh": second, "other.txt": "x\n"})
    assert "reviewed" not in _target_states(repo, three.revision, "run.sh").values()
    repo.mark_everything(three.revision)
    four = repo.advance({"run.sh": second, "other.txt": "y\n"})
    assert set(_target_states(repo, four.revision, "run.sh").values()) == {"reviewed"}


def test_the_fallback_anchor_is_a_digest_of_the_file_and_not_random() -> None:
    from lemoncrow.pro.capabilities.review.models import ChangedFile

    def anchor(text: str) -> str:
        item = ChangedFile(path="run.sh", old_path=None, status="modified")
        return units_module._PartAnchors(item, text, None).at_line(3)

    assert anchor("a\nb\nc\n") == anchor("a\nb\nc\n")
    assert anchor("a\nb\nc\n") != anchor("a\nb\nd\n")
    assert is_unanchored(anchor("a\nb\nc\n"))


# --------------------------------------------------------------------------------------------------------------------
# m3: the since-review reading compares places whatever the body did.
# --------------------------------------------------------------------------------------------------------------------


def _reading(repo: _Repo, revision: Any) -> Any:
    targets = [t for t in repo.targets(revision) if t.state == "changed_since_review" and t.kind == "symbol"]
    proofs = since_review_proofs(
        repo.store,
        repo.session.id,
        "local",
        revision.id,
        targets,
        lambda path: (repo.root / path).read_text(encoding="utf-8"),
    )
    assert [t.symbol for t in targets] == ["main"]
    return proofs[targets[0].target_id]


def test_the_since_review_reading_of_a_part_that_was_edited_and_moved_says_its_place_changed(tmp_path: Path) -> None:
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {"run.sh": _SHELL_BASE})
    one = repo.advance({"run.sh": _SHELL_ABOVE})
    repo.mark_everything(one.revision)
    two = repo.advance({"run.sh": _SHELL_BELOW.replace("{ echo hi; }", "{ echo hello; }")})  # body edited, then moved
    proof = _reading(repo, two.revision)
    assert proof.routine is False
    assert "position among the unchanged lines of an order-sensitive file changed" in proof.detail


def test_the_since_review_reading_of_a_part_whose_place_cannot_be_checked_says_so(tmp_path: Path) -> None:
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {"run.sh": _SHELL_BASE})
    one = repo.advance({"run.sh": _SHELL_ABOVE})
    repo.mark_everything(one.revision)
    for mark in repo.store.list_marks(repo.session.id, reviewer_id="local"):  # a verdict from before places were kept
        repo.store.set_mark(replace(mark, content_fingerprint=split_anchor(mark.content_fingerprint)[0]))
    two = repo.advance({"run.sh": _SHELL_ABOVE.replace("{ echo hi; }", "{ echo hello; }"), "other.txt": "1\n"})
    proof = _reading(repo, two.revision)
    assert proof.routine is False
    assert "cannot be checked against your review" in proof.detail
    assert "position among the unchanged lines" not in proof.detail
