"""Revision reconciliation and the review frontier -- "what changed since I looked?".

The headline is :func:`test_phase_two_acceptance_agent_rewrites_a_reviewed_change`,
which is the plan's Phase 2 gate encoded verbatim: an agent rewrites a
previously reviewed 20-file change, and LemonCrow must surface **only** the
units that genuinely require re-review while every valid mark and comment
survives.

That sentence has two halves and both are asserted, because each half alone is
trivially satisfiable by a broken implementation:

* preserve nothing and everything reopens -- safe, useless, and the reason
  :func:`test_reopening_everything_would_fail_the_acceptance_test` exists as its
  own named test rather than as a comment inside another one;
* preserve everything and nothing reopens -- a stale ``reviewed`` mark, which is
  worse than no review state at all, because no review state makes a reader look
  and a stale one tells them not to.

The fixtures are real git repositories with real diffs, real fingerprints and a
real SQLite store. Nothing here is a mock, because the failure mode being
guarded against is precisely that the real pipeline disagrees with a pure
function that looked correct in isolation.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pygit2
import pytest

from lemoncrow.pro.capabilities.review.anchors import build_anchor
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.revisions import (
    compare_revision_units,
    compute_frontier,
    file_revision_delta,
    group_frontier,
    reconcile,
)
from lemoncrow.pro.capabilities.review.session_models import (
    Annotation,
    ReviewMark,
    ReviewRevision,
    ReviewUnit,
)
from lemoncrow.pro.capabilities.review.sources.local import mark_unit, open_or_create_session, refresh
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.units import unit_key

# --------------------------------------------------------------------------- #
# fixture repository
# --------------------------------------------------------------------------- #

_BASE = '''"""Module {i}."""


def handler_{i}(value):
    return value
'''

# The change under review: every one of the 20 files gains a body.
#  1 docstring   2 blank   3 blank   4 def   5 total   6 logged   7 return
_REVIEWED = '''"""Module {i}."""


def handler_{i}(value):
    total = value + {i}
    logged = total
    return logged
'''

_COMMENTED_LINE = 6
"""``    logged = total`` -- the line all five annotations are attached to."""


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the impact detectors from bootstrapping an ast-grep binary per repo."""

    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


def _init_repo(root: Path) -> Any:
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.invalid"
    return repo


def _commit(repo: Any, message: str, offset: int) -> str:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    signature = pygit2.Signature("Fixture Tester", "fixture@example.invalid", 1700000000 + offset, 0)
    return str(repo.create_commit("HEAD", signature, signature, message, tree, parents))


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _rel(index: int) -> str:
    return f"src/mod_{index:02d}.py"


def _repo(tmp_path: Path, files: int = 20) -> tuple[Path, Any]:
    """A committed baseline plus an uncommitted change across *files* files."""

    root = tmp_path / "repo"
    repo = _init_repo(root)
    for index in range(files):
        _write(root, _rel(index), _BASE.format(i=index))
    _commit(repo, "baseline", 0)
    for index in range(files):
        _write(root, _rel(index), _REVIEWED.format(i=index))
    return root, repo


def _session(tmp_path: Path, root: Path) -> tuple[ReviewStore, Any, Any]:
    store = ReviewStore(tmp_path / "store")
    rng = resolve_rev_range(root)
    return store, open_or_create_session(store, root, rng), rng


def _refresh(store: ReviewStore, session: Any, root: Path, tmp_path: Path) -> Any:
    return refresh(store, session, root, store_root=tmp_path / "store", rng=resolve_rev_range(root), limit=500)


def _by_kind(units: tuple[ReviewUnit, ...], kind: str) -> dict[str, ReviewUnit]:
    """Units of one kind, keyed by path (or ``path::symbol`` for symbols)."""

    if kind == "symbol":
        return {f"{unit.path}::{unit.symbol}": unit for unit in units if unit.kind == kind}
    return {unit.path: unit for unit in units if unit.kind == kind}


def _states(store: ReviewStore, review_id: str) -> dict[str, str]:
    return {mark.unit_key: mark.state for mark in store.list_marks(review_id)}


def _anchor_events(store: ReviewStore, revision_id: str) -> dict[str, list[tuple[str, str]]]:
    """``annotation_id -> [(method, status)]`` recorded against one revision."""

    conn = sqlite3.connect(store.db_path)
    try:
        rows = conn.execute(
            "SELECT annotation_id, method, status FROM annotation_anchor_events WHERE revision_id = ?",
            (revision_id,),
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, list[tuple[str, str]]] = {}
    for annotation_id, method, status in rows:
        out.setdefault(str(annotation_id), []).append((str(method), str(status)))
    return out


def _revision() -> ReviewRevision:
    return ReviewRevision(
        id="rrv-2",
        review_id="rev-1",
        revision_number=2,
        range_mode="working_tree",
        tree_fingerprint="tree",
        packet_schema_version=2,
    )


# --------------------------------------------------------------------------- #
# case 1 -- idempotency
# --------------------------------------------------------------------------- #


def test_a_refresh_with_no_content_change_creates_no_revision_and_moves_no_mark(tmp_path: Path) -> None:
    """Running the command twice must not cost the reviewer their marks.

    The tree fingerprints to the revision that is already stored, so there is no
    second revision to reconcile against and nothing to reopen. Without this,
    every invocation would strand the marks made by the previous one.
    """

    root, _repo_handle = _repo(tmp_path, files=3)
    store, session, _rng = _session(tmp_path, root)

    first = _refresh(store, session, root, tmp_path)
    for unit in store.list_units(first.revision.id):
        mark_unit(store, session, first.revision, unit)

    second = _refresh(store, session, root, tmp_path)
    assert second.created is False
    assert second.revision.id == first.revision.id
    assert len(store.list_revisions(session.id)) == 1
    assert second.reconciliation.reopened == ()
    assert second.reconciliation.dropped == ()
    assert set(_states(store, session.id).values()) == {"reviewed"}


# --------------------------------------------------------------------------- #
# case 2 -- one edited function in a 20-file change
# --------------------------------------------------------------------------- #


def test_editing_one_function_reopens_that_symbol_and_its_file_and_nothing_else(tmp_path: Path) -> None:
    root, _repo_handle = _repo(tmp_path)
    store, session, _rng = _session(tmp_path, root)

    first = _refresh(store, session, root, tmp_path)
    for unit in store.list_units(first.revision.id):
        mark_unit(store, session, first.revision, unit)

    _write(root, _rel(7), _REVIEWED.format(i=7).replace("    logged = total", "    logged = total * 2"))
    second = _refresh(store, session, root, tmp_path)
    assert second.created is True

    states = _states(store, session.id)
    files = _by_kind(store.list_units(second.revision.id), "file")
    symbols = _by_kind(store.list_units(second.revision.id), "symbol")

    assert states[files[_rel(7)].unit_key] == "changed_since_review"
    assert states[symbols[f"{_rel(7)}::handler_7"].unit_key] == "changed_since_review"
    for index in range(20):
        if index == 7:
            continue
        assert states[files[_rel(index)].unit_key] == "reviewed", _rel(index)
        assert states[symbols[f"{_rel(index)}::handler_{index}"].unit_key] == "reviewed"


# --------------------------------------------------------------------------- #
# case 3 -- an insertion above a reviewed function
# --------------------------------------------------------------------------- #


def test_inserting_lines_above_a_reviewed_function_leaves_that_function_reviewed(tmp_path: Path) -> None:
    """The single most common agent edit, and the one a line-keyed tool loses.

    Ten lines above a function move every line below them. ``unit_key`` contains
    no line number and ``content_fingerprint`` covers the body only, so the
    function's verdict survives and exactly the file -- whose content genuinely
    did change -- reopens.
    """

    root, _repo_handle = _repo(tmp_path, files=3)
    store, session, _rng = _session(tmp_path, root)

    first = _refresh(store, session, root, tmp_path)
    for unit in store.list_units(first.revision.id):
        mark_unit(store, session, first.revision, unit)

    padding = "".join(f"CONSTANT_{n} = {n}\n" for n in range(10))
    _write(root, _rel(1), _REVIEWED.format(i=1).replace('"""Module 1."""\n', f'"""Module 1."""\n{padding}'))
    second = _refresh(store, session, root, tmp_path)

    states = _states(store, session.id)
    units = store.list_units(second.revision.id)
    assert states[_by_kind(units, "symbol")[f"{_rel(1)}::handler_1"].unit_key] == "reviewed"
    assert states[_by_kind(units, "file")[_rel(1)].unit_key] == "changed_since_review"


_WIDE_BASE = '''"""Wide module."""

{filler}

def handler(value):
    return value
'''

_WIDE_REVIEWED = '''"""Wide module."""

{filler}

def handler(value):
    total = value + 1
    logged = total
    return logged
'''

_FILLER = "\n".join(f"CONSTANT_{n} = {n}" for n in range(20))


def test_a_re_hunked_file_resets_its_hunk_marks_and_says_why(tmp_path: Path) -> None:
    """Hunk keys are ordinal-derived, which is the one key a re-hunking breaks.

    Rather than silently reassigning hunk 2's verdict to hunk 3, the hunk marks
    for that file are reset -- and the reason is carried in ``notes`` so a
    surface can explain it instead of looking broken. File and symbol marks,
    whose keys are position-independent, reconcile normally underneath.
    """

    root = tmp_path / "repo"
    repo_handle = _init_repo(root)
    _write(root, "src/wide.py", _WIDE_BASE.format(filler=_FILLER))
    _commit(repo_handle, "baseline", 0)
    _write(root, "src/wide.py", _WIDE_REVIEWED.format(filler=_FILLER))

    store = ReviewStore(tmp_path / "store")
    session = open_or_create_session(store, root, resolve_rev_range(root))
    first = _refresh(store, session, root, tmp_path)
    units = store.list_units(first.revision.id)
    assert len([unit for unit in units if unit.kind == "hunk"]) == 1
    for unit in units:
        mark_unit(store, session, first.revision, unit)

    # A second, distant edit at the top of the file: same content below, one
    # more hunk above it.
    _write(root, "src/wide.py", "HEADER = True\n" + _WIDE_REVIEWED.format(filler=_FILLER))
    second = _refresh(store, session, root, tmp_path)

    states = _states(store, session.id)
    after = store.list_units(second.revision.id)
    hunks = [unit for unit in after if unit.kind == "hunk"]
    assert len(hunks) == 2
    assert {states.get(unit.unit_key) for unit in hunks} <= {"unreviewed", None}
    assert any("hunk count changed 1 -> 2" in note for note in second.reconciliation.notes)
    # The position-independent keys are untouched by the re-hunking.
    assert states[_by_kind(after, "symbol")["src/wide.py::handler"].unit_key] == "reviewed"


# --------------------------------------------------------------------------- #
# case 4 -- a deleted file
# --------------------------------------------------------------------------- #


def test_deleting_a_reviewed_file_makes_its_comment_obsolete_and_never_leaves_it_reviewed(
    tmp_path: Path,
) -> None:
    """``obsolete`` and ``orphaned`` are different facts and must not be merged.

    The comment did not lose its footing -- the thing it was about is gone. A
    reviewer told "orphaned" would go looking for where it moved to.

    The file's own unit survives the deletion, because a deletion is a change
    the reviewer still has to look at; what it cannot survive is reading as
    ``reviewed``. With no new-side content there is nothing left to fingerprint,
    so the mark drops to ``unknown`` rather than attesting to a file nobody can
    show you.
    """

    root, _repo_handle = _repo(tmp_path, files=3)
    store, session, _rng = _session(tmp_path, root)

    first = _refresh(store, session, root, tmp_path)
    units = store.list_units(first.revision.id)
    for unit in units:
        mark_unit(store, session, first.revision, unit)
    doomed = _by_kind(units, "file")[_rel(2)]
    annotation = store.add_annotation(
        Annotation(
            id="",
            review_id=session.id,
            revision_id=first.revision.id,
            anchor=build_anchor(
                path=_rel(2),
                text=_REVIEWED.format(i=2),
                start_line=_COMMENTED_LINE,
                end_line=_COMMENTED_LINE,
                unit_key=doomed.unit_key,
                symbol_qualified_name="handler_2",
            ),
            body="this whole file looks redundant",
        )
    )

    (root / _rel(2)).unlink()
    _refresh(store, session, root, tmp_path)

    states = _states(store, session.id)
    assert states[doomed.unit_key] == "unknown"
    assert states[doomed.unit_key] != "reviewed"
    stored = store.list_annotations(session.id)[0]
    assert stored.id == annotation.id
    assert stored.state == "obsolete"
    assert stored.anchor_method == "removed"
    assert stored.state != "orphaned"


def test_a_unit_that_leaves_the_change_set_takes_its_mark_with_it(tmp_path: Path) -> None:
    """Reverting a file removes it from the diff, and its verdicts go with it.

    A mark left behind would be a verdict on something the review no longer
    contains, and it would keep counting toward "how much is done".
    """

    root, _repo_handle = _repo(tmp_path, files=3)
    store, session, _rng = _session(tmp_path, root)

    first = _refresh(store, session, root, tmp_path)
    units = store.list_units(first.revision.id)
    for unit in units:
        mark_unit(store, session, first.revision, unit)
    reverted = [unit.unit_key for unit in units if unit.path == _rel(2)]
    assert reverted

    _write(root, _rel(2), _BASE.format(i=2))
    second = _refresh(store, session, root, tmp_path)

    assert set(reverted) <= set(second.reconciliation.removed)
    assert {mark.unit_key for mark in second.reconciliation.dropped} == set(reverted)
    states = _states(store, session.id)
    assert all(key not in states for key in reverted)


# --------------------------------------------------------------------------- #
# case 5 -- a rename
# --------------------------------------------------------------------------- #


def test_a_rename_git_reports_as_delete_plus_add_never_claims_the_marks_survived(tmp_path: Path) -> None:
    """Today ``collect_diff`` does not run git's rename detection.

    Enabling it would change ``lc review``'s output, which is out of scope here,
    so a ``git mv`` currently arrives as one deletion and one addition. What
    matters is that nothing lies about it: the new path's units are *new* and
    read as unreviewed, rather than inheriting a verdict nobody gave them.

    :func:`test_a_mark_on_a_renamed_unit_moves_to_its_new_key_and_leaves_no_duplicate`
    covers the reconciliation side, so switching rename detection on later is a
    change to the diff layer alone and carries the marks across without loss.
    """

    root, repo_handle = _repo(tmp_path, files=3)
    _commit(repo_handle, "the reviewed change", 60)
    store_root = tmp_path / "store"
    rng = resolve_rev_range(root, "HEAD~1")
    store = ReviewStore(store_root)
    session = open_or_create_session(store, root, rng)

    first = refresh(store, session, root, store_root=store_root, rng=rng, limit=500)
    for unit in store.list_units(first.revision.id):
        mark_unit(store, session, first.revision, unit)

    (root / _rel(1)).rename(root / "src/moved.py")
    _commit(repo_handle, "rename", 120)
    second = refresh(
        store,
        session,
        root,
        store_root=store_root,
        rng=resolve_rev_range(root, "HEAD~2"),
        limit=500,
    )

    states = _states(store, session.id)
    after = store.list_units(second.revision.id)
    moved = _by_kind(after, "file")["src/moved.py"]
    assert moved.unit_key in second.reconciliation.added
    assert states.get(moved.unit_key) is None
    # The two files that were genuinely untouched keep their verdicts.
    assert states[_by_kind(after, "file")[_rel(0)].unit_key] == "reviewed"
    assert states[_by_kind(after, "file")[_rel(2)].unit_key] == "reviewed"


# --------------------------------------------------------------------------- #
# case 6 -- the Phase 2 acceptance test
# --------------------------------------------------------------------------- #

_SPARE_4 = """    return logged * scale


def spare_4(value):
    total = value + 4
    logged = total
    return logged
"""


def _agent_rewrite(root: Path) -> None:
    """A scripted agent rewrite touching exactly four of the twenty files.

    Each edit is chosen to exercise a different rung of the anchor ladder, so
    the acceptance test proves the comments survive *for a stated reason* rather
    than by luck:

    * ``mod_03`` gains imports above the commented line -> rung 2;
    * ``mod_04`` gains an import **and** a signature change plus a second copy of
      the commented text elsewhere in the file -> rung 3, the only rung that can
      still tell the two copies apart;
    * ``mod_05`` and ``mod_06`` are rewritten with no comment on them at all.
    """

    _write(
        root,
        _rel(3),
        _REVIEWED.format(i=3).replace('"""Module 3."""\n', '"""Module 3."""\nimport os\nimport sys\n'),
    )
    _write(
        root,
        _rel(4),
        _REVIEWED.format(i=4)
        .replace('"""Module 4."""\n', '"""Module 4."""\nimport os\n')
        .replace("def handler_4(value):", "def handler_4(value, scale=1):")
        .replace("    return logged\n", _SPARE_4),
    )
    _write(root, _rel(5), _REVIEWED.format(i=5).replace("    total = value + 5", "    total = value * 5"))
    _write(root, _rel(6), _REVIEWED.format(i=6).replace("    return logged", "    return logged or 0"))


def test_phase_two_acceptance_agent_rewrites_a_reviewed_change(tmp_path: Path) -> None:
    """*An agent rewrites a previously reviewed 20-file change.*

    LemonCrow must surface only the units that actually require human
    re-review, while preserving valid comments and marks. All four clauses of
    the gate are asserted:

    (a) exactly the units whose fingerprints changed are ``changed_since_review``;
    (b) the other 16 files remain ``reviewed``;
    (c) all 5 annotations still resolve -- 3 by rung 1, 1 by rung 2, 1 by rung 3;
    (d) **zero** annotations moved to a different line without a rung recorded in
        ``annotation_anchor_events``.

    Clause (d) is the negative one and it is the point: a relocation nobody can
    audit after the fact is indistinguishable from a fabrication.
    """

    root, _repo_handle = _repo(tmp_path)
    store, session, _rng = _session(tmp_path, root)

    first = _refresh(store, session, root, tmp_path)
    units_before = store.list_units(first.revision.id)
    assert len({unit.path for unit in units_before}) == 20
    for unit in units_before:
        mark_unit(store, session, first.revision, unit)
    assert set(_states(store, session.id).values()) == {"reviewed"}

    files_before = _by_kind(units_before, "file")
    annotated = (0, 1, 2, 3, 4)
    annotations = [
        store.add_annotation(
            Annotation(
                id="",
                review_id=session.id,
                revision_id=first.revision.id,
                anchor=build_anchor(
                    path=_rel(index),
                    text=_REVIEWED.format(i=index),
                    start_line=_COMMENTED_LINE,
                    end_line=_COMMENTED_LINE,
                    unit_key=files_before[_rel(index)].unit_key,
                    symbol_qualified_name=f"handler_{index}",
                ),
                body=f"is this shadowing anything, module {index}?",
            )
        )
        for index in annotated
    ]
    assert len(annotations) == 5

    _agent_rewrite(root)
    second = _refresh(store, session, root, tmp_path)
    assert second.created is True

    units_after = store.list_units(second.revision.id)
    states = _states(store, session.id)

    # --- (a) exactly the units whose fingerprints changed reopened ---------- #
    before_fp = {unit.unit_key: unit.content_fingerprint for unit in units_before}
    genuinely_changed = {
        unit.unit_key
        for unit in units_after
        if unit.unit_key in before_fp and before_fp[unit.unit_key] != unit.content_fingerprint
    }
    reopened = {key for key, state in states.items() if state == "changed_since_review"}
    assert reopened == genuinely_changed
    assert reopened, "the rewrite must have reopened something"

    # --- (b) the other 16 files remain reviewed ----------------------------- #
    files_after = _by_kind(units_after, "file")
    untouched = [index for index in range(20) if index not in (3, 4, 5, 6)]
    assert len(untouched) == 16
    for index in untouched:
        assert states[files_after[_rel(index)].unit_key] == "reviewed", _rel(index)
    for index in (3, 4, 5, 6):
        assert states[files_after[_rel(index)].unit_key] == "changed_since_review", _rel(index)

    # --- (c) all five annotations still resolve, by a named rung ------------ #
    stored = {item.id: item for item in store.list_annotations(session.id)}
    assert len(stored) == 5
    methods = {item.anchor.path: item.anchor_method for item in stored.values()}
    assert methods[_rel(0)] == "identical_blob"
    assert methods[_rel(1)] == "identical_blob"
    assert methods[_rel(2)] == "identical_blob"
    assert methods[_rel(3)] == "exact_text_context"
    assert methods[_rel(4)] == "symbol_text"
    assert all(item.state == "open" for item in stored.values())

    # Each surviving comment landed on the line it is actually about.
    for item in stored.values():
        text = (root / item.anchor.path).read_text(encoding="utf-8").splitlines()
        assert text[item.anchor.start_line - 1] == "    logged = total"

    # --- (d) nothing moved without a recorded rung -------------------------- #
    events = _anchor_events(store, second.revision.id)
    assert set(events) == set(stored)
    for move in second.moves:
        assert move.annotation_id in events
        assert move.method in {method for method, _status in events[move.annotation_id]}
        assert move.method != ""
    moved = [move for move in second.moves if move.moved]
    assert moved, "the rewrite shifted at least one comment; a test where none moved proves nothing"
    for move in moved:
        assert (move.method, move.status) in events[move.annotation_id]
        assert move.status == "relocated"


def test_reopening_everything_would_fail_the_acceptance_test(tmp_path: Path) -> None:
    """The negative clause, as its own test rather than as a hopeful comment.

    A reconciliation that reopens every unit on every revision is not
    conservative, it is useless: a reviewer asked to re-read sixteen untouched
    files because an agent edited four others stops reading any of them. This
    fails loudly if the carry-forward path is ever removed "for safety".
    """

    root, _repo_handle = _repo(tmp_path)
    store, session, _rng = _session(tmp_path, root)

    first = _refresh(store, session, root, tmp_path)
    for unit in store.list_units(first.revision.id):
        mark_unit(store, session, first.revision, unit)

    _agent_rewrite(root)
    second = _refresh(store, session, root, tmp_path)

    frontier = compute_frontier(
        session.id,
        "local",
        second.revision,
        store.list_units(second.revision.id),
        store.list_marks(session.id),
        annotations=store.list_annotations(session.id),
    )
    groups = group_frontier(frontier, second.reconciliation.added)
    assert len(groups.unchanged_reviewed) > len(groups.changed_since_review)
    assert len(groups.changed_since_review) < len(frontier.entries) / 2
    assert {entry.path for entry in groups.changed_since_review} == {_rel(3), _rel(4), _rel(5), _rel(6)}


# --------------------------------------------------------------------------- #
# the pure reconciliation rules
# --------------------------------------------------------------------------- #


def _unit(key: str, fingerprint: str, *, method: str = "blob_sha256", kind: str = "file") -> ReviewUnit:
    return ReviewUnit(
        revision_id="rrv-1",
        unit_key=key,
        kind=kind,  # type: ignore[arg-type]
        path="src/a.py",
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
    )


def test_file_revision_delta_preserves_only_exact_known_file_identity() -> None:
    before = [
        replace(_unit("fil:a", "same"), path="src/a.py"),
        replace(_unit("fil:b", "old"), path="src/b.py"),
        replace(_unit("fil:gone", "gone"), path="src/gone.py"),
        replace(_unit("fil:old-name", "moved"), path="src/old-name.py"),
        replace(_unit("fil:opaque", "placeholder", method="unknown"), path="src/opaque.bin"),
    ]
    after = [
        replace(_unit("fil:a", "same"), path="src/a.py"),
        replace(_unit("fil:b", "new"), path="src/b.py"),
        replace(_unit("fil:new", "new"), path="src/new.py"),
        replace(_unit("fil:new-name", "moved"), path="src/new-name.py"),
        replace(_unit("fil:opaque", "placeholder", method="unknown"), path="src/opaque.bin"),
    ]

    delta = file_revision_delta(before, after, renames=[("src/old-name.py", "src/new-name.py")])

    assert delta.preserved == ("src/a.py",)
    assert delta.changed == ("src/b.py", "src/opaque.bin")
    assert delta.added == ("src/new.py",)
    assert delta.removed == ("src/gone.py",)
    assert delta.renamed == (("src/old-name.py", "src/new-name.py"),)

    base_moved = file_revision_delta(before, before, same_diff_base=False)
    assert base_moved.preserved == ()
    assert base_moved.changed == (
        "src/a.py",
        "src/b.py",
        "src/gone.py",
        "src/old-name.py",
        "src/opaque.bin",
    )


def test_reviewed_is_never_silently_preserved_across_changed_content() -> None:
    """The sentence the whole module exists for, asserted as one line."""

    result = reconcile([_unit("k", "fp-a")], [_unit("k", "fp-b")], [_mark("k", "fp-a")])
    assert result.carried == ()
    assert [mark.state for mark in result.reopened] == ["changed_since_review"]


def test_an_unchanged_reviewed_unit_keeps_the_revision_the_human_actually_looked_at() -> None:
    """``reviewed_revision_id`` is not advanced; it records when a human looked.

    Advancing it would erase the only evidence of how stale a still-valid
    verdict is.
    """

    result = reconcile([_unit("k", "fp-a")], [_unit("k", "fp-a")], [_mark("k", "fp-a")])
    assert result.reopened == ()
    assert result.carried[0].reviewed_revision_id == "rrv-1"


def test_changed_since_review_stays_reopened_across_later_unchanged_revisions() -> None:
    """A -> B -> B stays reopened until the human explicitly reviews B."""

    original_mark = _mark("k", "fp-a")
    changed = reconcile([_unit("k", "fp-a")], [_unit("k", "fp-b")], [original_mark])
    reopened = changed.reopened[0]
    assert reopened.state == "changed_since_review"

    unchanged_after_change = reconcile([_unit("k", "fp-b")], [_unit("k", "fp-b")], [reopened])
    assert unchanged_after_change.reopened == ()
    assert unchanged_after_change.carried == (reopened,)
    assert unchanged_after_change.carried[0].state == "changed_since_review"
    assert unchanged_after_change.carried[0].reviewed_revision_id == "rrv-1"
    assert unchanged_after_change.carried[0].content_fingerprint == "fp-a"


def test_content_that_changed_and_came_back_is_reviewed_again() -> None:
    """An exact return to the fingerprint the human reviewed is valid again."""

    mark = _mark("k", "fp-a", state="changed_since_review")
    result = reconcile([_unit("k", "fp-b")], [_unit("k", "fp-a")], [mark])
    assert [item.state for item in result.reopened] == ["reviewed"]


def test_an_unfingerprintable_unit_becomes_unknown_but_recovers_when_identity_returns() -> None:
    mark = _mark("k", "fp-a")
    unknown = _unit("k", "fp-x", method="unknown")
    degraded = reconcile([_unit("k", "fp-a")], [unknown], [mark])
    assert [item.state for item in degraded.reopened] == ["unknown"]
    downgraded = degraded.reopened[0]
    assert downgraded.content_fingerprint == "fp-a"

    restored = reconcile([unknown], [_unit("k", "fp-a")], [downgraded])
    assert [item.state for item in restored.reopened] == ["reviewed"]

    changed = reconcile([unknown], [_unit("k", "fp-b")], [downgraded])
    assert [item.state for item in changed.reopened] == ["changed_since_review"]


def test_needs_changes_survives_a_rehunk_instead_of_being_withdrawn() -> None:
    mark = _mark("hun:k", "fp-a", state="needs_changes")
    before = ReviewUnit(
        revision_id="", unit_key="hun:k", kind="hunk", path="a.py", ordinal=0, content_fingerprint="fp-a"
    )
    after = ReviewUnit(
        revision_id="", unit_key="hun:k", kind="hunk", path="a.py", ordinal=0, content_fingerprint="fp-b"
    )
    extra = ReviewUnit(
        revision_id="", unit_key="hun:j", kind="hunk", path="a.py", ordinal=1, content_fingerprint="fp-c"
    )
    result = reconcile([before], [after, extra], [mark])
    assert result.carried == (mark,)
    assert result.reopened == ()


def test_a_needs_changes_verdict_is_a_human_request_and_survives_intact() -> None:
    """Reconciliation may invalidate an attestation; it may not withdraw a request.

    Downgrading ``needs_changes`` to ``unknown`` because a file turned binary
    would delete the reviewer's own input, which is strictly worse than the
    staleness the ``unknown`` rule guards against.
    """

    mark = _mark("k", "fp-a", state="needs_changes")
    result = reconcile([_unit("k", "fp-a")], [_unit("k", "fp-x", method="unknown")], [mark])
    assert result.carried == (mark,)
    assert result.reopened == ()


def test_a_unit_that_left_the_review_takes_its_mark_with_it() -> None:
    result = reconcile([_unit("k", "fp-a")], [_unit("j", "fp-b")], [_mark("k", "fp-a")])
    assert result.removed == ("k",)
    assert result.added == ("j",)
    assert [mark.unit_key for mark in result.dropped] == ["k"]
    assert result.carried == ()
    assert result.reopened == ()


def test_a_mark_on_a_renamed_unit_moves_to_its_new_key_and_leaves_no_duplicate() -> None:
    """Pure-function proof of the rename path, independent of git's detection."""

    old = replace(
        _unit(unit_key("symbol", "src/a.py", "f", 0), "fp-a", kind="symbol"),
        path="src/a.py",
        symbol="f",
    )
    new = replace(old, path="src/b.py", unit_key=unit_key("symbol", "src/b.py", "f", 0))

    result = reconcile([old], [new], [_mark(old.unit_key, "fp-a")], renames=[("src/a.py", "src/b.py")])
    assert [mark.unit_key for mark in result.reopened] == [new.unit_key]
    assert [mark.state for mark in result.reopened] == ["reviewed"]
    assert [mark.unit_key for mark in result.dropped] == [old.unit_key]
    assert result.added == ()
    assert result.removed == ()


def test_revision_compare_reports_only_semantic_changes_and_preserves_judgment_context() -> None:
    before = (
        replace(_unit("stable", "fp-stable"), path="src/a.py", start_line=3),
        replace(_unit("changed", "fp-old"), path="src/a.py", start_line=10),
        replace(_unit("removed", "fp-removed"), path="src/old.py", start_line=4),
    )
    after = (
        replace(_unit("stable", "fp-stable"), path="src/a.py", start_line=5),
        replace(_unit("changed", "fp-new"), path="src/a.py", start_line=12),
        replace(_unit("added", "fp-added"), path="src/new.py", start_line=8),
    )
    result = compare_revision_units(
        before,
        after,
        previous_marks=[_mark("changed", "fp-old", state="reviewed")],
        next_marks=[_mark("changed", "fp-old", state="changed_since_review")],
    )

    assert (result.added, result.changed, result.removed, result.unchanged) == (1, 1, 1, 1)
    assert [(item.unit_key, item.status, item.from_state, item.to_state) for item in result.units] == [
        ("added", "added", "unreviewed", "unreviewed"),
        ("changed", "changed", "reviewed", "changed_since_review"),
        ("removed", "removed", "unreviewed", "unreviewed"),
    ]
    assert [(item.path, item.status, item.added, item.changed, item.removed) for item in result.files] == [
        ("src/a.py", "changed", 0, 1, 0),
        ("src/new.py", "added", 1, 0, 0),
        ("src/old.py", "removed", 0, 0, 1),
    ]


def test_revision_compare_never_calls_unknown_fingerprints_unchanged() -> None:
    before = [replace(_unit("opaque", "placeholder", method="unknown"), path="opaque.bin")]
    after = [replace(_unit("opaque", "placeholder", method="unknown"), path="opaque.bin")]
    result = compare_revision_units(before, after)
    assert result.changed == 1
    assert result.unchanged == 0
    assert [(item.path, item.status) for item in result.files] == [("opaque.bin", "changed")]


def test_the_frontier_is_derived_and_cannot_disagree_with_the_marks() -> None:
    """Every entry's state comes from the mark rows, with no cached second copy."""

    revision = _revision()
    units = (_unit("k", "fp-b"), _unit("j", "fp-c"))
    frontier = compute_frontier("rev-1", "local", revision, units, [_mark("k", "fp-a")])
    by_key = {entry.unit_key: entry for entry in frontier.entries}
    assert by_key["k"].state == "reviewed"
    assert by_key["k"].changed_since_mark is True
    assert by_key["j"].state == "unreviewed"
    assert frontier.last_seen_revision == revision.id
    assert frontier.reviewed_unit_fingerprints == (("k", "fp-a"),)


def test_group_frontier_puts_what_an_agent_rewrote_first_and_never_double_counts() -> None:
    revision = _revision()
    units = (
        _unit("changed", "fp-1"),
        _unit("fresh", "fp-2"),
        _unit("asked", "fp-3"),
        _unit("done", "fp-4"),
        _unit("todo", "fp-5"),
    )
    marks = [
        _mark("changed", "fp-0", state="changed_since_review"),
        _mark("asked", "fp-3", state="needs_changes"),
        _mark("done", "fp-4"),
    ]
    groups = group_frontier(compute_frontier("rev-1", "local", revision, units, marks), ["fresh"])
    assert [entry.unit_key for entry in groups.changed_since_review] == ["changed"]
    assert [entry.unit_key for entry in groups.new] == ["fresh"]
    assert [entry.unit_key for entry in groups.unresolved] == ["asked"]
    assert [entry.unit_key for entry in groups.unchanged_reviewed] == ["done"]
    assert [entry.unit_key for entry in groups.not_yet_reviewed] == ["todo"]
    total = sum(
        len(bucket)
        for bucket in (
            groups.changed_since_review,
            groups.new,
            groups.unresolved,
            groups.unchanged_reviewed,
            groups.not_yet_reviewed,
        )
    )
    assert total == len(units)
