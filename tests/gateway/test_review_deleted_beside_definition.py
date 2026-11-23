"""P11-6: deleted base lines beside a reviewed, unchanged definition are shown, never carried under its mark.

Git aligns a deletion with the added lines next to it, so the base lines a later revision deletes can land in the same
replacement block as the lines of a definition the reviewer already marked. ``derive_review_targets`` gives that block's
old lines to the definition, whose fingerprint used to hash only its new-side body: the body did not change, so the
reviewer's mark carried and the deletion was never shown (P11-5 found it in mattermost/desktop#759, r5 to r6).

Every test here runs the real pipeline: a git repository, ``refresh`` (``derive_units`` and ``reconcile``), real marks,
``compute_frontier`` and ``derive_review_targets``. The oracle is not the implementation: the deleted lines come from the
program's own model of the files, and a line counts as shown only when a part the reviewer still has to read (its state is
not ``reviewed``) lists it in an old-side span. ``LEMONCROW_DELETED_BESIDE_PROGRAMS`` raises the number of programs per
language for a larger local run.
"""

from __future__ import annotations

import os
import random
import re
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pygit2
import pytest

from lemoncrow.pro.capabilities.review import packet as packet_module
from lemoncrow.pro.capabilities.review import units as units_module
from lemoncrow.pro.capabilities.review.gitdiff import (
    MAX_BLOB_BYTES,
    BlobPair,
    collect_diff,
    load_old_blobs,
    resolve_rev_range,
    wants_old_blob,
)
from lemoncrow.pro.capabilities.review.models import review_packet_from_dict
from lemoncrow.pro.capabilities.review.packet import PacketBuild
from lemoncrow.pro.capabilities.review.revisions import compute_frontier, reconcile
from lemoncrow.pro.capabilities.review.session_models import ReviewMark, ReviewUnit
from lemoncrow.pro.capabilities.review.sources.local import (
    carry_recorded_anchors,
    enrich_revision_projection,
    mark_unit,
    open_or_create_session,
    read_packet_json,
    refresh,
)
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.targets import ReviewTarget, derive_review_targets

PROGRAMS = int(os.environ.get("LEMONCROW_DELETED_BESIDE_PROGRAMS", "30"))
SEED = 20261115
WHY: list[str] = []


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


# --------------------------------------------------------------------------------------------------------------------
# A real repository, a real Review session.
# --------------------------------------------------------------------------------------------------------------------


class _Repo:
    def __init__(self, root: Path, store_root: Path, base: dict[str, str]) -> None:
        self.root, self.store_root = root, store_root
        root.mkdir(parents=True, exist_ok=True)
        repo = pygit2.init_repository(str(root), initial_head="main")
        repo.config["user.name"], repo.config["user.email"] = "Fixture Tester", "tester@example.com"
        self.write(base)
        repo.index.add_all()
        repo.index.write()
        signature = pygit2.Signature("Fixture Tester", "tester@example.com", 1700000000, 0)
        repo.create_commit("HEAD", signature, signature, "baseline", repo.index.write_tree(), [])
        self.store = ReviewStore(store_root)
        self.session = open_or_create_session(self.store, root, resolve_rev_range(root))

    def write(self, files: dict[str, str]) -> None:
        for path, text in files.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def advance(self, files: dict[str, str]) -> Any:
        """Write the working tree to *files* and capture it as the next revision."""

        self.write(files)
        return refresh(
            self.store,
            self.session,
            self.root,
            store_root=self.store_root,
            rng=resolve_rev_range(self.root),
            limit=500,
        )

    def rebase(self, files: dict[str, str]) -> None:
        """Move the base: commit *files* on top of HEAD without touching the working tree, as an upstream merge does."""

        repo = pygit2.Repository(str(self.root))
        for path, text in files.items():
            repo.index.add(pygit2.IndexEntry(path, repo.create_blob(text.encode()), pygit2.enums.FileMode.BLOB))
        repo.index.write()
        signature = pygit2.Signature("Fixture Tester", "tester@example.com", 1700000100, 0)
        repo.create_commit("HEAD", signature, signature, "upstream", repo.index.write_tree(), [repo.head.target])

    def targets(self, revision: Any) -> tuple[tuple[ReviewUnit, ...], tuple[ReviewTarget, ...]]:
        units = self.store.list_units(revision.id)
        frontier = compute_frontier(
            self.session.id,
            "local",
            revision,
            units,
            self.store.list_marks(self.session.id, reviewer_id="local"),
        )
        return units, derive_review_targets(units, frontier.entries, read_packet_json(self.store, revision))

    def mark_everything(self, revision: Any) -> int:
        """The reviewer marks every part of *revision* ``reviewed``; returns how many parts there were."""

        units, targets = self.targets(revision)
        by_key = {unit.unit_key: unit for unit in units}
        for target in targets:
            mark_unit(self.store, self.session, revision, by_key[target.unit_key], state="reviewed")
        return len(targets)


def _to_read(targets: tuple[ReviewTarget, ...]) -> list[ReviewTarget]:
    return [target for target in targets if target.state != "reviewed"]


def _shows_old_line(targets: tuple[ReviewTarget, ...], path: str, line: int) -> bool:
    """Whether a part the reviewer still has to read lists base line *line* of *path* in an old-side span."""

    return any(
        target.path == path
        and target.state != "reviewed"
        and any(span.side == "old" and span.start_line <= line <= span.end_line for span in target.spans)
        for target in targets
    )


def _all_old_lines(targets: tuple[ReviewTarget, ...], path: str) -> set[int]:
    return {
        line
        for target in targets
        if target.path == path
        for span in target.spans
        if span.side == "old"
        for line in range(span.start_line, span.end_line + 1)
    }


# --------------------------------------------------------------------------------------------------------------------
# The reviewer's probes and the mattermost transition, each as base -> r1 -> r2.
# --------------------------------------------------------------------------------------------------------------------

_PY_BASE = "import os\n\nREQUIRE_AUTH = True\nALLOWED = ['admin']\n\ndef keep():\n    return 1\n"
_PY_R1 = "import os\n\nREQUIRE_AUTH = True\nALLOWED = ['admin']\n\ndef added(x):\n    return x + 1\n\ndef keep():\n    return 1\n"
_PY_R2 = "import os\n\ndef added(x):\n    return x + 1\n\ndef keep():\n    return 1\n"

_UMASK_BASE = "import os\n\nos.umask(0o077)\nsetup_audit()\n\ndef keep():\n    return 1\n"
_UMASK_R1 = (
    "import os\n\nos.umask(0o077)\nsetup_audit()\n\ndef added(x):\n    return x + 1\n\ndef keep():\n    return 1\n"
)
_UMASK_R2 = "import os\n\ndef added(x):\n    return x + 1\n\ndef keep():\n    return 1\n"

_TS_BASE = "import x from 'x';\n\nlet a: A | undefined;\nlet b: B | undefined;\n\ntest('t', () => {\n  a = 1;\n});\n"
_TS_R1 = (
    "import x from 'x';\n\nlet a: A | undefined;\nlet b: B | undefined;\n\n"
    "async function setup(): Promise<A> {\n  return run();\n}\n\ntest('t', () => {\n  a = 1;\n});\n"
)
_TS_R2 = "import x from 'x';\n\nasync function setup(): Promise<A> {\n  return run();\n}\n\ntest('t', () => {\n  a = 1;\n});\n"

_PROBES = {
    "python settings above an added function": ("a.py", _PY_BASE, _PY_R1, _PY_R2, (3, 4)),
    "python umask and audit calls above an added function": ("a.py", _UMASK_BASE, _UMASK_R1, _UMASK_R2, (3, 4)),
    "typescript module lets above an added function": ("a.ts", _TS_BASE, _TS_R1, _TS_R2, (3, 4)),
}


@pytest.mark.parametrize("name", sorted(_PROBES))
def test_deleted_lines_beside_an_added_unchanged_definition_are_shown(tmp_path: Path, name: str) -> None:
    path, base, r1, r2, deleted = _PROBES[name]
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: base})
    first = repo.advance({path: r1})
    assert repo.mark_everything(first.revision) >= 1
    assert _to_read(repo.targets(first.revision)[1]) == [], "the reviewer marked everything"
    second = repo.advance({path: r2})
    _units, targets = repo.targets(second.revision)

    for line in deleted:
        assert _shows_old_line(targets, path, line), (name, line, [(t.kind, t.symbol, t.state) for t in targets])


def test_the_mattermost_transition_shows_the_deleted_module_lets(tmp_path: Path) -> None:
    """r5 to r6 of mattermost/desktop#759: the module-level lets above ``setupMattermost`` leave; the function does not change."""

    path = "src/main/app.test.ts"
    head = "import { app } from 'electron';\nimport { start } from './start';\n\n"
    tail = "\ntest('starts', async () => {\n  await setupMattermost();\n  expect(start()).toBe(true);\n});\n"
    lets = "let a: A | undefined;\nlet b: B | undefined;\n"
    fn = "\nasync function setupMattermost(): Promise<void> {\n  a = await boot();\n  b = await connect(a);\n}\n"
    base = head + lets + tail.replace("  await setupMattermost();\n", "  a = await boot();\n")
    r5 = head + lets + fn + tail  # an earlier revision added the function
    r6 = head + fn.lstrip("\n") + tail  # this one deletes the lets and leaves the function exactly as it was

    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: base})
    five = repo.advance({path: r5})
    repo.mark_everything(five.revision)
    six = repo.advance({path: r6})
    _units, targets = repo.targets(six.revision)

    deleted = [index + 1 for index, text in enumerate(base.splitlines()) if text.startswith("let ")]
    assert len(deleted) == 2
    for line in deleted:
        assert _shows_old_line(targets, path, line), (line, [(t.kind, t.symbol, t.state) for t in targets])


# --------------------------------------------------------------------------------------------------------------------
# What must not change.
# --------------------------------------------------------------------------------------------------------------------


def test_a_revision_identical_to_the_last_one_reopens_nothing(tmp_path: Path) -> None:
    path = "a.py"
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: _PY_BASE})
    first = repo.advance({path: _PY_R1})
    count = repo.mark_everything(first.revision)
    # A new revision is captured (another file changed); the reviewed file is byte-identical to r1.
    second = repo.advance({path: _PY_R1, "other.py": "VALUE = 1\n"})
    _units, targets = repo.targets(second.revision)
    assert [target for target in targets if target.path == path and target.state != "reviewed"] == []
    assert len([target for target in targets if target.path == path]) == count


def test_a_definition_modified_only_inside_its_own_body_is_one_part_that_shows_its_deleted_line(tmp_path: Path) -> None:
    path = "m.py"
    base = "import os\n\nLIMIT = 3\n\ndef keep(x):\n    y = x + 1\n    return y\n\ndef other():\n    return 2\n"
    r1 = base.replace("    y = x + 1\n", "    y = x + 2\n")
    r2 = r1.replace("    return y\n", "    return y * 2\n")
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: base})
    first = repo.advance({path: r1})
    _units, once = repo.targets(first.revision)
    assert [target.symbol for target in once if target.path == path] == ["keep"]
    assert not [target for target in once if target.kind == "hunk"], "an ordinary edit must not add a hunk part"
    keep = next(target for target in once if target.symbol == "keep")
    assert {span.side for span in keep.spans} == {"old", "new"}, "the definition shows its own deleted line"
    repo.mark_everything(first.revision)

    second = repo.advance({path: r2})
    _units, targets = repo.targets(second.revision)
    assert [target.symbol for target in targets if target.path == path] == ["keep"]
    assert [target.symbol for target in _to_read(targets)] == ["keep"]
    assert not [target for target in targets if target.kind == "hunk"]
    assert _shows_old_line(targets, path, 6), "the deleted line of its own body is still in its part"


def test_a_renamed_definition_keeps_its_old_lines_in_the_hunk_part_and_the_new_one_in_its_own(tmp_path: Path) -> None:
    """Old ``foo`` lines aligned with new ``bar``: the reader shows the same parts before and after P11-6.

    ``foo`` is a deleted definition, so the existing ``_foreign_definition_start`` rule gives its lines to the hunk part
    and ``bar`` shows only its new side. The fix changes none of that; it only makes sure ``bar``'s mark covers what it owns.
    """

    path = "r.py"
    base = "import os\n\ndef foo(x):\n    a = x + 1\n    return a\n\ndef keep():\n    return 1\n"
    r1 = base.replace("def foo(x):\n    a = x + 1\n    return a\n", "def bar(x):\n    b = x * 3\n    return b\n")
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: base})
    first = repo.advance({path: r1})
    _units, targets = repo.targets(first.revision)
    assert sorted(target.symbol for target in targets if target.path == path) == ["", "bar"]
    assert {3, 4, 5} <= _all_old_lines(targets, path), "foo's lines are shown"
    repo.mark_everything(first.revision)

    # The same content again: the mark carries.
    again = repo.advance({path: r1, "other.py": "V = 1\n"})
    assert [t for t in repo.targets(again.revision)[1] if t.path == path and t.state != "reviewed"] == []

    # One more base line deleted beside it: the part reopens and shows that line.
    third = repo.advance({path: r1.replace("import os\n\n", "")})
    _units, after = repo.targets(third.revision)
    assert _shows_old_line(after, path, 1)


def test_a_pure_deletion_with_no_added_line_beside_it_goes_to_the_hunk(tmp_path: Path) -> None:
    path = "p.py"
    base = "LIMIT = 3\nOTHER = 4\n\ndef keep():\n    return 1\n"
    r1 = base + "\ndef added():\n    return 2\n"
    r2 = "OTHER = 4\n\ndef keep():\n    return 1\n\ndef added():\n    return 2\n"
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: base})
    first = repo.advance({path: r1})
    repo.mark_everything(first.revision)
    second = repo.advance({path: r2})
    _units, targets = repo.targets(second.revision)
    assert _shows_old_line(targets, path, 1)
    assert [target.symbol for target in targets if target.kind == "symbol" and target.state != "reviewed"] == []


def _symbol_states(targets: tuple[ReviewTarget, ...], path: str) -> dict[str, str]:
    return {target.symbol: target.state for target in targets if target.path == path and target.kind == "symbol"}


def test_swapping_two_reviewed_modified_definitions_keeps_both_verdicts(tmp_path: Path) -> None:
    """Each part shows only its own, identical new lines; the foreign deleted definition belongs to the hunk part.

    The owner's fingerprint hashes the rows the Reader gives it (``owned_old_count``), not the whole replaced block.
    """

    path = "src/mod_1.py"
    base = '"""Module 1."""\n\n\ndef handler_1(value):\n    return value\n\n\ndef other_1(value):\n    return value\n'
    handler = "def handler_1(value):\n    total = value + 1\n    return total\n"
    other = "def other_1(value):\n    return value * 2\n"
    r1 = '"""Module 1."""\n\n\n' + handler + "\n\n" + other
    r2 = '"""Module 1."""\n\n\n' + other + "\n\n" + handler
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: base})
    first = repo.advance({path: r1})
    repo.mark_everything(first.revision)
    second = repo.advance({path: r2})
    units, targets = repo.targets(second.revision)
    assert _symbol_states(targets, path) == {"handler_1": "reviewed", "other_1": "reviewed"}
    assert not any(units_module.owns_deleted_text(unit.content_fingerprint) for unit in units if unit.kind == "symbol")
    # The deleted definitions are still in a part to read.
    assert {4, 5, 8, 9} <= {
        line
        for t in _to_read(targets)
        for s in t.spans
        if s.side == "old"
        for line in range(s.start_line, s.end_line + 1)
    }
    # Verdicts survive: a part that stopped owning deleted text is restamped, never reopened.
    assert {reason for _who, _key, reason in second.reconciliation.reasons} <= {
        "content_unchanged",
        "deleted_text_no_longer_shown",
    }
    assert not second.reconciliation.reopened


_UPSTREAM = "".join(f"# upstream {i}\n" for i in range(10))
_PAD = "".join(f"P{i} = {i}\n" for i in range(8)) + "\n"
_REBASE_BASE = (
    "import os\n\n{pad}LIMIT = 3\n\ndef keep(x):\n    y = x + 1\n    return y\n\ndef other():\n    return 2\n"
)
_REBASE_R1 = (
    "import os\n\n{pad}def added(x):\n    return x + 1\n\ndef keep(x):\n    y = x + 2\n    return y\n\n"
    "def other():\n    return 2\n"
)


@pytest.mark.parametrize(
    ("pad", "authored_elsewhere"),
    [("", False), ("", True), (_PAD, False), (_PAD, True)],
    ids=["R1 upstream lines above", "R2 plus a change elsewhere", "R5 lines far above", "R6 far above plus a change"],
)
def test_a_rebase_that_only_shifts_the_base_lines_keeps_the_verdict(
    tmp_path: Path, pad: str, authored_elsewhere: bool
) -> None:
    """Upstream lines above the deleted text move its base line numbers, not what the reviewer judged."""

    path = "m.py"
    base, r1 = _REBASE_BASE.format(pad=pad), _REBASE_R1.format(pad=pad)
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: base})
    first = repo.advance({path: r1})
    units, _ = repo.targets(first.revision)
    assert units_module.owns_deleted_text(next(u for u in units if u.symbol == "added").content_fingerprint)
    repo.mark_everything(first.revision)
    repo.rebase({path: _UPSTREAM + base})
    r2 = _UPSTREAM + r1
    second = repo.advance({path: r2.replace("    return 2\n", "    return 3\n") if authored_elsewhere else r2})
    _units, targets = repo.targets(second.revision)
    states = _symbol_states(targets, path)
    assert states["added"] == states["keep"] == "reviewed"
    assert not second.reconciliation.reopened
    assert {reason for _who, key, reason in second.reconciliation.reasons if "added" in key or "keep" in key} <= {
        "content_unchanged"
    }


def test_deleting_the_next_definition_keeps_the_verdict_on_the_one_above_it(tmp_path: Path) -> None:
    """The neighbour's deletion joins the owner's block in git's diff; only the owner's own text is its to own."""

    path = "src/mod_1.py"
    base = '"""Module 1."""\n\n\ndef handler_1(value):\n    return value\n\n\ndef other_1(value):\n    return value\n'
    handler = "def handler_1(value):\n    total = value + 1\n    return total\n"
    other = "def other_1(value):\n    return value * 2\n"
    r1 = '"""Module 1."""\n\n\n' + handler + "\n\n" + other
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: base})
    first = repo.advance({path: r1})
    repo.mark_everything(first.revision)
    second = repo.advance({path: '"""Module 1."""\n\n\n' + handler})
    _units, targets = repo.targets(second.revision)
    assert _symbol_states(targets, path) == {"handler_1": "reviewed"}
    assert not second.reconciliation.reopened
    # The deleted neighbour is still there to read.
    assert {8, 9} <= {
        line
        for t in _to_read(targets)
        for s in t.spans
        if s.side == "old"
        for line in range(s.start_line, s.end_line + 1)
    }


def test_the_same_text_deleted_from_a_different_place_in_the_file_does_not_carry(tmp_path: Path) -> None:
    path = "a.py"
    base = (
        "import os\n\nregister(handler)\n\ndef keep():\n    return 1\n\n"
        "def tail():\n    return 2\n\nregister(handler)\n\ndef last():\n    return 3\n"
    )
    added = "def added(x):\n    return x + 1\n"
    r1 = base.replace("import os\n\nregister(handler)\n", "import os\n\n" + added, 1)
    r2 = (
        "import os\n\nregister(handler)\n\ndef keep():\n    return 1\n\n"
        "def tail():\n    return 2\n\n" + added + "\ndef last():\n    return 3\n"
    )
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: base})
    first = repo.advance({path: r1})
    repo.mark_everything(first.revision)
    second = repo.advance({path: r2})
    _units, targets = repo.targets(second.revision)
    second_register = 11
    assert _shows_old_line(targets, path, second_register), _symbol_states(targets, path)


def test_the_same_text_deleted_from_another_file_does_not_carry_to_a_moved_definition(tmp_path: Path) -> None:
    """Only the base path tells the two files apart: the rows above the deleted line are the same in both."""

    a_base = "import os\n\nregister(handler)\n\ndef keep():\n    return 1\n"
    b_base = "import os\n\nregister(handler)\n\ndef other():\n    return 2\n"
    added = "def added(x):\n    return x + 1\n"
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {"a.py": a_base, "b.py": b_base})
    first = repo.advance({"a.py": a_base.replace("register(handler)\n", added)})
    repo.mark_everything(first.revision)
    second = repo.advance({"a.py": a_base, "b.py": b_base.replace("register(handler)\n", added)})
    _units, targets = repo.targets(second.revision)
    assert _shows_old_line(targets, "b.py", 3), _symbol_states(targets, "b.py")


_COPIES = (
    "import os\n\ndef keep():\n    return 1\n\nregister(handler)\n\n"
    "def tail():\n    return 1\n\nregister(handler)\n\ndef last():\n    return 3\n"
)
_ADDED = "def added(x):\n    return x + 1\n"


def test_the_same_text_between_the_same_rows_at_a_second_place_does_not_carry(tmp_path: Path) -> None:
    """P1: two copies of one run of base rows (the two rows above and the deleted line), told apart by occurrence.

    r1 replaces the first copy, r2 the second; the definition's body is the same, so only what sat beside it moved.
    """

    path = "m.py"
    r1 = _COPIES.replace("    return 1\n\nregister(handler)\n\ndef tail", "    return 1\n\n" + _ADDED + "\ndef tail", 1)
    r2 = (
        "import os\n\ndef keep():\n    return 1\n\nregister(handler)\n\n"
        "def tail():\n    return 1\n\n" + _ADDED + "\ndef last():\n    return 3\n"
    )
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: _COPIES})
    first = repo.advance({path: r1})
    repo.mark_everything(first.revision)
    second = repo.advance({path: r2})
    _units, targets = repo.targets(second.revision)
    assert _symbol_states(targets, path)["added"] != "reviewed", _symbol_states(targets, path)
    assert _shows_old_line(targets, path, 11), "the second copy's deleted line is in a part to read"


def test_a_base_file_that_is_not_at_hand_gives_the_same_fingerprint_on_every_derive_and_never_the_anchored_one(
    tmp_path: Path,
) -> None:
    """Without the base file the occurrence cannot be counted; it is a digest of the patch's own text, so it is stable.

    P13-3 (OWNER-25): it used to be a random token, so such a part reopened on every refresh and could never stay
    reviewed. It still never equals the anchored fingerprint, so a base that becomes readable reopens the part once.
    """

    path = "m.py"
    r1 = _COPIES.replace("    return 1\n\nregister(handler)\n\ndef tail", "    return 1\n\n" + _ADDED + "\ndef tail", 1)
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: _COPIES})
    first = repo.advance({path: r1})
    packet_units = {u.symbol: u for u in repo.store.list_units(first.revision.id)}
    assert units_module.owns_deleted_text(packet_units["added"].content_fingerprint)
    # The same packet, derived twice with no base file / with one over the bound / with the matching one.
    payload = read_packet_json(repo.store, first.revision)
    assert payload is not None
    packet = review_packet_from_dict(payload)
    new_blobs = {path: r1}
    anchored = units_module.derive_units(packet, new_blobs, old_blobs={path: _COPIES})
    again = units_module.derive_units(packet, new_blobs, old_blobs={path: _COPIES})
    assert {u.content_fingerprint for u in anchored if u.symbol == "added"} == {
        u.content_fingerprint for u in again if u.symbol == "added"
    }
    for old_blobs in (None, {}, {path: _COPIES + "#" * (MAX_BLOB_BYTES + 1)}, {path: "not the base\n"}):
        one = units_module.derive_units(packet, new_blobs, old_blobs=old_blobs)
        two = units_module.derive_units(packet, new_blobs, old_blobs=old_blobs)
        fps = [u.content_fingerprint for u in (*one, *two) if u.symbol == "added"]
        assert all(units_module.owns_deleted_text(fp) for fp in fps)
        assert len(set(fps)) == 1, "the same on every derive of the same revision"
        assert set(fps).isdisjoint({u.content_fingerprint for u in anchored if u.symbol == "added"})


_RUN_PATCH = " import os\n-register(handler)\n+def added(x):\n+    return x + 1\n"


def _two_runs(*, second_rows: str = _RUN_PATCH, second_old: int = 40) -> list[dict[str, Any]]:
    return [
        {"patch": _RUN_PATCH, "old_start": 2, "new_start": 2},
        {"patch": second_rows, "old_start": second_old, "new_start": second_old + 1},
    ]


@pytest.mark.parametrize(
    "old_text", [None, "#" * (MAX_BLOB_BYTES + 1), "not the base\n"], ids=["none", "oversized", "other"]
)
def test_an_owned_run_with_no_readable_base_has_a_digest_anchor_that_tells_identical_runs_apart(
    old_text: str | None,
) -> None:
    """P13-3: path, rows and base line make the anchor; two identical runs of one file are two places."""

    raw = _two_runs()
    owned = units_module._OwnedOldText(raw, "m.py", old_text)
    first, second = owned.within(3, 4), owned.within(42, 43)
    assert first and second
    assert units_module.UNANCHORED in first and units_module.UNANCHORED in second
    assert first != second, "identical runs at two places in one file do not collide"
    # The same on every refresh of the same revision, whatever instance computes it.
    again = units_module._OwnedOldText(raw, "m.py", old_text)
    assert (again.within(3, 4), again.within(42, 43)) == (first, second)
    # Different when what it owns differs: the deleted text, the rows above it, the base path.
    other_text = units_module._OwnedOldText(
        _two_runs(second_rows=_RUN_PATCH.replace("register(handler)", "register(other)")), "m.py", old_text
    )
    assert other_text.within(3, 4) == first and other_text.within(42, 43) != second
    other_above = units_module._OwnedOldText(
        _two_runs(second_rows=_RUN_PATCH.replace(" import os", " import sys")), "m.py", old_text
    )
    assert other_above.within(3, 4) == first and other_above.within(42, 43) != second
    assert units_module._OwnedOldText(raw, "n.py", old_text).within(3, 4) != first
    # Not a line shift elsewhere in the file: the base line is what places the run, the new-side line is not.
    moved_new = [raw[0], {**raw[1], "new_start": raw[1]["new_start"] + 7}]
    assert units_module._OwnedOldText(moved_new, "m.py", old_text).within(49, 50) == second


@pytest.mark.parametrize("old_text", [None, "#" * (MAX_BLOB_BYTES + 1)], ids=["none", "oversized"])
def test_the_digest_of_a_run_with_no_readable_base_covers_the_rows_as_the_patch_prints_them(
    old_text: str | None,
) -> None:
    """A last row that ends the file with no newline is printed with a trailing ``>`` by one tool and bare by another.

    Only the base file says which is the row's text (:func:`_row_text`), and here there is none. Both readings give the
    same source row, so the digest also covers the rows as printed: the same revision patched by the two tools is two
    texts and reopens once, never carries.
    """

    marked = " import os\n x = 1\n-LABEL = 'abc'>\n\\ No newline at end of file\n+def added(x):\n+    return x + 1\n"
    bare = marked.replace("'abc'>", "'abc'")
    assert bare != marked
    one, two = ({"patch": patch, "old_start": 1, "new_start": 1} for patch in (marked, bare))
    for raw in (one, two):
        (block,) = units_module.change_blocks(raw)
        assert block.old_text == ("LABEL = 'abc'",), block.old_text
    first = units_module._OwnedOldText([one], "m.py", old_text).within(3, 4)
    second = units_module._OwnedOldText([two], "m.py", old_text).within(3, 4)
    assert first and second and units_module.UNANCHORED in first and units_module.UNANCHORED in second
    assert first != second
    # Each reading is stable across refreshes.
    assert units_module._OwnedOldText([one], "m.py", old_text).within(3, 4) == first
    assert units_module._OwnedOldText([two], "m.py", old_text).within(3, 4) == second


def test_an_anchored_run_is_not_affected_by_the_unanchored_digest() -> None:
    """With the base file at hand the occurrence is still the count of earlier identical runs."""

    base = "".join(f"row {n}\n" for n in range(1, 8)) + "import os\nregister(handler)\n"
    patch = " row 7\n import os\n-register(handler)\n+def added(x):\n+    return x + 1\n"
    raw = [{"patch": patch, "old_start": 7, "new_start": 7}]
    anchored = units_module._OwnedOldText(raw, "m.py", base).within(9, 10)
    assert anchored and units_module.UNANCHORED not in anchored
    assert units_module._OwnedOldText(raw, "m.py", None).within(9, 10) != anchored


def _without_a_readable_base(monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    """Capture as if the base file were not at hand: absent from the blob pair, or over ``MAX_BLOB_BYTES``."""

    real = packet_module.load_blobs

    def load(*args: Any, **kwargs: Any) -> BlobPair:
        pair = real(*args, **kwargs)
        old = {} if how == "absent" else {name: text + "#" * (MAX_BLOB_BYTES + 1) for name, text in pair.old.items()}
        return replace(pair, old=old)

    monkeypatch.setattr(packet_module, "load_blobs", load)


_NO_BASE = pytest.mark.parametrize("how", ["absent", "oversized"])


@_NO_BASE
def test_a_part_with_no_readable_base_stays_reviewed_across_a_revision_that_changes_another_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """P13-3: the owned deleted text of an unreadable base used to be a fresh random id, so it reopened every time."""

    _without_a_readable_base(monkeypatch, how)
    path = "m.py"
    r1 = _COPIES.replace("    return 1\n\nregister(handler)\n\ndef tail", "    return 1\n\n" + _ADDED + "\ndef tail", 1)
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: _COPIES})
    first = repo.advance({path: r1})
    owned = {u.symbol: u for u in repo.store.list_units(first.revision.id)}["added"]
    assert units_module.owns_deleted_text(owned.content_fingerprint)
    repo.mark_everything(first.revision)
    second = repo.advance({path: r1, "z.txt": "1\n"})
    _units, targets = repo.targets(second.revision)
    assert _symbol_states(targets, path)["added"] == "reviewed", _symbol_states(targets, path)
    assert not second.reconciliation.reopened
    carried = {u.symbol: u for u in repo.store.list_units(second.revision.id)}["added"]
    assert carried.content_fingerprint == owned.content_fingerprint
    # Lines added elsewhere in the file move the new side, not the base line the run sat on.
    third = repo.advance({path: r1.replace("import os\n", "import os\nimport sys\n", 1), "z.txt": "2\n"})
    _units, targets = repo.targets(third.revision)
    assert _symbol_states(targets, path)["added"] == "reviewed", _symbol_states(targets, path)


@_NO_BASE
def test_a_part_with_no_readable_base_reopens_as_changed_when_its_owned_text_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """The owned text is in the fingerprint, so a changed part is a content change, not a ``part_place_unverifiable``."""

    _without_a_readable_base(monkeypatch, how)
    path = "m.py"
    base = "import os\n\ndef other():\n    return 2\n\nLIMIT = 3\nOTHER = 4\n"
    r1 = "import os\n\ndef other():\n    return 2\n\ndef added(x):\n    return x + 1\nOTHER = 4\n"
    r2 = "import os\n\ndef other():\n    return 2\n\ndef added(x):\n    return x + 1\n"
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: base})
    first = repo.advance({path: r1})
    key = {u.symbol: u for u in repo.store.list_units(first.revision.id)}["added"].unit_key
    repo.mark_everything(first.revision)
    second = repo.advance({path: r2})
    _units, targets = repo.targets(second.revision)
    assert _symbol_states(targets, path)["added"] != "reviewed", _symbol_states(targets, path)
    assert _shows_old_line(targets, path, 6) and _shows_old_line(targets, path, 7)
    assert [reason for _who, unit_key, reason in second.reconciliation.reasons if unit_key == key] == [
        "content_changed"
    ]


@_NO_BASE
def test_the_same_text_at_a_second_place_does_not_carry_when_the_base_is_not_at_hand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """The base line is what tells two identical runs of one file apart once the occurrence cannot be counted."""

    _without_a_readable_base(monkeypatch, how)
    path = "m.py"
    r1 = _COPIES.replace("    return 1\n\nregister(handler)\n\ndef tail", "    return 1\n\n" + _ADDED + "\ndef tail", 1)
    r2 = (
        "import os\n\ndef keep():\n    return 1\n\nregister(handler)\n\n"
        "def tail():\n    return 1\n\n" + _ADDED + "\ndef last():\n    return 3\n"
    )
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: _COPIES})
    first = repo.advance({path: r1})
    repo.mark_everything(first.revision)
    second = repo.advance({path: r2})
    _units, targets = repo.targets(second.revision)
    assert _symbol_states(targets, path)["added"] != "reviewed", _symbol_states(targets, path)
    assert _shows_old_line(targets, path, 11), "the second copy's deleted line is in a part to read"


@_NO_BASE
def test_a_rebase_that_shifts_the_base_reopens_a_part_whose_base_is_not_at_hand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """Fail-safe, and the cost of having no base: a shifted run cannot be told from a second copy, so it is read."""

    _without_a_readable_base(monkeypatch, how)
    path = "m.py"
    base, r1 = _REBASE_BASE.format(pad=""), _REBASE_R1.format(pad="")
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: base})
    first = repo.advance({path: r1})
    repo.mark_everything(first.revision)
    repo.rebase({path: _UPSTREAM + base})
    second = repo.advance({path: _UPSTREAM + r1})
    _units, targets = repo.targets(second.revision)
    assert _symbol_states(targets, path)["added"] != "reviewed", _symbol_states(targets, path)


_NO_NEWLINE_BASE = "import os\n\ndef other():\n    return 2\n\nLIMIT = 3"
_NO_NEWLINE_R1 = "import os\n\ndef other():\n    return 2\n\ndef added(x):\n    return x + 1"


def test_a_deleted_last_line_with_no_newline_at_the_end_of_the_file_keeps_its_verdict(tmp_path: Path) -> None:
    """P7: libgit2's end-of-file marker is not part of the deleted row, so the base blob still anchors it.

    The captured patch reads ``-LIMIT = 3>`` there; compared as written it never matched the base file, and every
    later revision that touched the file reopened a part nothing had changed.
    """

    path = "m.py"
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: _NO_NEWLINE_BASE})
    first = repo.advance({path: _NO_NEWLINE_R1})
    owned = {u.symbol: u for u in repo.store.list_units(first.revision.id)}["added"]
    assert units_module.owns_deleted_text(owned.content_fingerprint)
    assert "unanchored" not in owned.content_fingerprint
    repo.mark_everything(first.revision)
    elsewhere = repo.advance({path: _NO_NEWLINE_R1.replace("return 2", "return 3")})
    _units, targets = repo.targets(elsewhere.revision)
    assert _symbol_states(targets, path)["added"] == "reviewed", _symbol_states(targets, path)


def test_a_deleted_last_line_with_no_newline_still_reopens_when_the_owned_text_changes(tmp_path: Path) -> None:
    """P7, the other direction: the marker is stripped for the comparison only, so more deleted text still reads."""

    path = "m.py"
    base = "import os\n\ndef other():\n    return 2\n\nLIMIT = 3\nOTHER = 4"
    r1 = "import os\n\ndef other():\n    return 2\n\ndef added(x):\n    return x + 1\nOTHER = 4"
    r2 = "import os\n\ndef other():\n    return 2\n\ndef added(x):\n    return x + 1"
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: base})
    first = repo.advance({path: r1})
    repo.mark_everything(first.revision)
    second = repo.advance({path: r2})
    _units, targets = repo.targets(second.revision)
    assert _symbol_states(targets, path)["added"] != "reviewed", _symbol_states(targets, path)
    assert _shows_old_line(targets, path, 6) and _shows_old_line(targets, path, 7)


def _enriched_with_no_base_blobs(repo: _Repo, revision: Any, *, edit: tuple[str, str] | None = None) -> None:
    """The server's enrichment: the packet and the frozen new-side text only, no base blobs."""

    payload = read_packet_json(repo.store, revision)
    assert payload is not None
    frozen = repo.store.read_blob_artifact(repo.session.id, revision.id)
    assert frozen is not None
    if edit is not None:
        frozen = {name: text.replace(*edit) for name, text in frozen.items()}
    enrich_revision_projection(
        repo.store,
        repo.session,
        revision,
        PacketBuild(packet=review_packet_from_dict(payload), blobs=BlobPair(old={}, new=frozen, degraded=())),
    )


def test_an_enrichment_without_base_blobs_keeps_the_fingerprints_capture_recorded(tmp_path: Path) -> None:
    """The enrichment has no base blob, so it carries capture's fingerprint for an owned-text part, never a fresh marker."""

    path = "m.py"
    base = "import os\n\nLIMIT = 3\n\ndef keep(x):\n    y = x + 1\n    return y\n\ndef other():\n    return 2\n"
    r1 = (
        "import os\n\ndef added(x):\n    return x + 1\n\ndef keep(x):\n    y = x + 2\n    return y\n\n"
        "def other():\n    return 2\n"
    )
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: base})
    first = repo.advance({path: r1})
    before = {u.unit_key: u.content_fingerprint for u in repo.store.list_units(first.revision.id)}
    owning = [key for key, fp in before.items() if units_module.owns_deleted_text(fp)]
    assert owning, "the fixture has a part that owns deleted text"
    _enriched_with_no_base_blobs(repo, first.revision)
    after = {u.unit_key: u.content_fingerprint for u in repo.store.list_units(first.revision.id)}
    assert after == before
    assert not any("unanchored" in fp for fp in after.values())
    repo.mark_everything(repo.store.get_revision(first.revision.id))
    unchanged = repo.advance({path: r1, "z.txt": "1\n"})
    assert not unchanged.reconciliation.reopened
    _units, targets = repo.targets(unchanged.revision)
    assert all(state == "reviewed" for path_, state in _symbol_states(targets, path).items() if path_ in {"added"})
    edited = repo.advance({path: r1.replace("return 2", "return 3"), "z.txt": "2\n"})
    _units, targets = repo.targets(edited.revision)
    states = _symbol_states(targets, path)
    assert states["added"] == "reviewed", states
    # The carry is only for the same body: derived from other text, the recorded fingerprint is not kept.
    stored = repo.store.list_units(edited.revision.id)
    owner = next(unit for unit in stored if unit.symbol == "added")
    other_body = replace(
        owner,
        content_fingerprint=units_module.symbol_fingerprint("function", "added", "def added(x):\n    pass", "old"),
    )
    assert carry_recorded_anchors([other_body], stored, {}) == [other_body]
    assert carry_recorded_anchors([other_body], stored, {path: base}) == [other_body], "a base blob at hand wins"


@pytest.mark.parametrize("call_first", [True, False])
def test_a_block_that_adds_a_module_line_beside_the_definition_is_not_the_definitions_to_own(
    tmp_path: Path, call_first: bool
) -> None:
    """Ownership needs EVERY added line of the block inside the definition, as the Reader decides it.

    The block's added lines are a module-level call and the definition, in either order. The Reader gives the deleted
    rows to the hunk part, so the definition's verdict neither covers them nor reopens when they change.
    """

    path = "m.py"
    base = "import os\nLIMIT = 3\nOTHER = 4\n\ndef keep():\n    return 1\n"
    new = ["configure()", "def added(x):", "    return x + 1"]
    if not call_first:
        new = [*new[1:], new[0]]
    added = "\n".join(new) + "\n"
    r1 = "import os\n" + added + "OTHER = 4\n\ndef keep():\n    return 1\n"
    r2 = "import os\n" + added + "\ndef keep():\n    return 1\n"
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: base})
    first = repo.advance({path: r1})
    repo.mark_everything(first.revision)
    second = repo.advance({path: r2})
    units, targets = repo.targets(second.revision)
    assert _symbol_states(targets, path)["added"] == "reviewed", _symbol_states(targets, path)
    unit = next(unit for unit in units if unit.symbol == "added")
    assert not units_module.owns_deleted_text(unit.content_fingerprint)
    assert _shows_old_line(targets, path, 2) and _shows_old_line(targets, path, 3), "still in a part to read"


def test_restoring_the_deleted_lines_keeps_the_verdict_as_a_restamp_not_a_reopening(tmp_path: Path) -> None:
    """The part now shows less than the reviewer judged: kept, counted and audited as its own outcome."""

    path = "s.py"
    base = "import os\n\nLIMIT = 3\n\ndef keep():\n    return 1\n"
    r1 = "import os\n\ndef added(x):\n    return x + 1\n\ndef keep():\n    return 1\n"
    r2 = base + "\ndef added(x):\n    return x + 1\n"
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: base})
    first = repo.advance({path: r1})
    units, _targets = repo.targets(first.revision)
    assert units_module.owns_deleted_text(next(u for u in units if u.symbol == "added").content_fingerprint)
    repo.mark_everything(first.revision)
    second = repo.advance({path: r2})
    result = second.reconciliation
    added_key = next(u.unit_key for u in repo.targets(second.revision)[0] if u.symbol == "added")
    assert [mark.unit_key for mark in result.restamped] == [added_key]
    assert added_key not in {mark.unit_key for mark in result.reopened}
    assert ("local", added_key, "deleted_text_no_longer_shown") in result.reasons
    events = [
        e for e in repo.store.list_mark_events(repo.session.id, unit_key=added_key) if e.event_kind == "reconciled"
    ]
    assert events and "no longer shows deleted lines" in events[-1].reason
    assert "base-only" not in events[-1].reason
    assert _symbol_states(repo.targets(second.revision)[1], path)["added"] == "reviewed"


# --------------------------------------------------------------------------------------------------------------------
# Randomized programs per language.
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Lang:
    name: str
    ext: str
    prelude: tuple[str, ...]
    data: tuple[str, ...]  # module-level data lines; {i} is the element id and {k} its value
    call: tuple[str, ...]  # a module-level statement
    define: tuple[str, ...]  # one definition; every line carries {i} so git alignment is unambiguous


LANGS: tuple[Lang, ...] = (
    Lang(
        "python",
        "py",
        ("import os",),
        ("SETTING_{i}a = {k}", "SETTING_{i}b = {k}"),
        ("configure_{i}({k})",),
        ("def fn_{i}(v):", "    total_{i} = v + {k}", "    return total_{i}"),
    ),
    Lang(
        "typescript",
        "ts",
        ("import x from 'x';",),
        ("let v{i}a: number | undefined;", "const c{i}b = {k};"),
        ("configure_{i}({k});",),
        (
            "function fn_{i}(v: number): number {{",
            "  const total_{i} = v + {k};",
            "  return total_{i};",
            "}} // end {i}",
        ),
    ),
    Lang(
        "javascript",
        "js",
        ("import x from 'x';",),
        ("let v{i}a;", "var c{i}b = {k};"),
        ("configure_{i}({k});",),
        ("function fn_{i}(v) {{", "  const total_{i} = v + {k};", "  return total_{i};", "}} // end {i}"),
    ),
    Lang(
        "go",
        "go",
        ("package a",),
        ("var v{i}a = {k}", "const c{i}b = {k}"),
        ("var w{i} = {k}",),
        ("func fn_{i}(v int) int {{", "\ttotal_{i} := v + {k}", "\treturn total_{i}", "}} // end {i}"),
    ),
    Lang(
        "rust",
        "rs",
        ("use std::fmt;",),
        ("const C{i}A: i32 = {k};", "static S{i}B: i32 = {k};"),
        ("const D{i}: i32 = {k};",),
        ("fn fn_{i}(v: i32) -> i32 {{", "    let total_{i} = v + {k};", "    total_{i}", "}} // end {i}"),
    ),
    Lang(
        "ruby",
        "rb",
        ("require 'set'",),
        ("SETTING_{i}A = {k}", "SETTING_{i}B = {k}"),
        ("configure_{i}({k})",),
        ("def fn_{i}(v)", "  total_{i} = v + {k}", "  total_{i}", "end # end {i}"),
    ),
)

# An element of a program's model: (kind, ident, shape); ``kind`` is "data", "call" or "def".
Elem = tuple[str, str, int]


def _lines(lang: Lang, elem: Elem) -> list[str]:
    kind, ident, shape = elem
    template = {"data": lang.data, "call": lang.call, "def": lang.define, "twin": lang.call}[kind]
    count = len(template)
    if kind == "twin":
        ident, shape = "t", 1  # the same text wherever a twin stands: only its position tells two of them apart
    if kind == "data" and ident[1:].isdigit() and int(ident[1:]) % 2 == 0:
        count = 1  # runs of one line and of two
    return [line.format(i=ident, k=shape) for line in template[:count]]


def _render(lang: Lang, elems: list[Elem]) -> tuple[str, dict[str, list[int]]]:
    """The file text and, per element ident, the 1-based line numbers of its lines."""

    lines = [*lang.prelude, ""]
    where: dict[str, list[int]] = {}
    for elem in elems:
        body = _lines(lang, elem)
        where[elem[1]] = list(range(len(lines) + 1, len(lines) + 1 + len(body)))
        lines.extend(body)
        lines.append("")
    return "\n".join(lines) + "\n", where


def _program(rng: random.Random) -> tuple[list[Elem], list[Elem], list[Elem], set[str], Counter[str]]:
    """base, r1, r2 as element lists; the idents of base elements r2 deletes that r1 left alone; per-program facts."""

    facts: Counter[str] = Counter()
    base: list[Elem] = []
    for index in range(rng.randint(5, 9)):
        kind = rng.choice(("data", "data", "call", "def", "def", "def"))
        base.append((kind, f"b{index}", rng.randrange(1, 9)))
    if not any(kind == "def" for kind, _i, _k in base):
        base.append(("def", "b99", 1))
    if not any(kind != "def" for kind, _i, _k in base):
        base.insert(0, ("data", "b98", 1))

    # Sometimes two statements with byte-identical text at different places ("twins"), so that the same text can be
    # deleted from two places: a verdict that covered the text deleted at one must not carry to the other.
    twinned = rng.random() < 0.4
    if twinned:
        low, high = sorted(rng.sample(range(len(base) + 1), 2))
        base.insert(high, ("twin", "t1", 1))
        base.insert(low, ("twin", "t0", 1))

    # r1: add definitions, rewrite some base definitions, sometimes delete a module-level base element.
    r1 = list(base)
    twin_swap = twinned and rng.random() < 0.7
    if twin_swap:
        # The first twin is replaced in place by an added definition, which the reviewer will read with the deleted twin.
        r1[r1.index(("twin", "t0", 1))] = ("def", "nt", rng.randrange(1, 9))
    for added in range(rng.randint(1, 2)):
        r1.insert(rng.randrange(len(r1) + 1), ("def", f"n{added}", rng.randrange(1, 9)))
    for position, (kind, ident, shape) in enumerate(r1):
        if kind == "def" and ident.startswith("b") and rng.random() < 0.25:
            r1[position] = (kind, ident, shape + 100)
            facts["r1_rewrites"] += 1
    if not twinned and rng.random() < 0.3:
        victims = [elem for elem in r1 if elem[0] != "def" and elem[1].startswith("b")]
        if len(victims) > 1:
            r1.remove(rng.choice(victims))
            facts["r1_deletes"] += 1

    # r2: delete base elements that r1 left byte-identical (the deletions the property is about), and sometimes make
    # other edits: rewrite or add a definition.
    base_by_ident = {ident: (kind, ident, shape) for kind, ident, shape in base}
    untouched = [elem for elem in r1 if base_by_ident.get(elem[1]) == elem]
    r2 = list(r1)
    deleted: set[str] = set()
    if twin_swap:
        # Restore the first twin where it was and delete the second, putting the added definition in its place: the
        # same text is deleted again, from a different place.
        facts["twin_swaps"] += 1
        nt = next(elem for elem in r2 if elem[1] == "nt")
        r2[r2.index(nt)] = ("twin", "t0", 1)
        r2[r2.index(("twin", "t1", 1))] = nt
        deleted.add("t1")
    else:
        for elem in untouched:
            if rng.random() < 0.4:
                r2.remove(elem)
                deleted.add(elem[1])
    if rng.random() < 0.5:
        for position, (kind, ident, shape) in enumerate(r2):
            if kind == "def" and rng.random() < 0.3:
                r2[position] = (kind, ident, shape + 200)
                facts["r2_rewrites"] += 1
    if rng.random() < 0.4:
        r2.insert(rng.randrange(len(r2) + 1), ("def", "n9", rng.randrange(1, 9)))
        facts["r2_adds"] += 1
    return base, r1, r2, deleted, facts


def _beside_an_added_definition(r1: list[Elem], deleted: set[str]) -> bool:
    """Whether a deleted base element sits directly next to a definition r1 added (the shape of the defect)."""

    for position, elem in enumerate(r1):
        if elem[1] in deleted:
            neighbours = [r1[index] for index in (position - 1, position + 1) if 0 <= index < len(r1)]
            if any(kind == "def" and ident.startswith("n") for kind, ident, _shape in neighbours):
                return True
    return False


def _diff_deleted_lines(old_text: str, new_text: str, path: str) -> set[int]:
    """Base lines a plain git diff of *old_text* against *new_text* deletes, read from the diff and not from any target."""

    patch = pygit2.Patch.create_from(old_text.encode(), new_text.encode(), old_as_path=path, new_as_path=path)
    if patch is None:
        return set()
    return {line.old_lineno for hunk in patch.hunks for line in hunk.lines if line.origin == "-"}


def _run_program(tmp: Path, lang: Lang, index: int, counts: Counter[str]) -> None:
    seed = SEED + index * 7919 + sum(map(ord, lang.name))
    base, r1, r2, deleted, facts = _program(random.Random(seed))
    counts.update(facts)
    path = f"src/mod.{lang.ext}"
    base_text, _ = _render(lang, base)
    r1_text, _ = _render(lang, r1)
    r2_text, _ = _render(lang, r2)
    repo = _Repo(tmp / f"{lang.name}-{index}", tmp / f"store-{lang.name}-{index}", {path: base_text})
    first = repo.advance({path: r1_text})
    repo.mark_everything(first.revision)
    _units, after_first = repo.targets(first.revision)
    assert _to_read(after_first) == [], (lang.name, index, [(t.label, t.state) for t in _to_read(after_first)])
    counts["programs"] += 1
    if not deleted:
        return

    second = repo.advance({path: r2_text})
    _units, targets = repo.targets(second.revision)
    counts["with_deletions"] += 1
    if _beside_an_added_definition(r1, deleted):
        counts["beside_added"] += 1
    # The deleted base lines are what a git diff says they are, not what the model or the implementation's own spans
    # say: with byte-identical statements at two places git decides which of them a revision deleted. A line that no
    # span shows is therefore still checked.
    newly_deleted = _diff_deleted_lines(base_text, r2_text, path) - _diff_deleted_lines(base_text, r1_text, path)
    for line in sorted(newly_deleted):
        counts["deleted_lines"] += 1
        if not _shows_old_line(targets, path, line):
            counts["fail_open"] += 1
            WHY.append(
                f"{lang.name}#{index} seed={seed} base line {line} is not in a part to read\n"
                f"base:\n{base_text}\nr1:\n{r1_text}\nr2:\n{r2_text}\n"
                f"targets: {[(t.kind, t.symbol, t.state, [(s.side, s.start_line, s.end_line) for s in t.spans]) for t in targets]}"
            )
    # Anti-noise: a later revision with r2's content reopens nothing in this file.
    repo.mark_everything(second.revision)
    again = repo.advance({path: r2_text, "other.txt": f"{index}\n"})
    _units, settled = repo.targets(again.revision)
    if [t for t in _to_read(settled) if t.path == path]:
        counts["noise"] += 1
        WHY.append(f"{lang.name}#{index} noise: {[(t.label, t.state) for t in _to_read(settled)]}")


def test_deleting_base_lines_next_to_an_unchanged_reviewed_definition_never_carries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    totals: dict[str, Counter[str]] = {}
    for lang in LANGS:
        counts: Counter[str] = Counter()
        for index in range(PROGRAMS):
            _run_program(tmp_path, lang, index, counts)
        totals[lang.name] = counts
    with capsys.disabled():
        print(f"\nP11-6 differential, seed {SEED}, {PROGRAMS} programs per language:")
        for name, counts in totals.items():
            print(f"  {name:11s} {dict(sorted(counts.items()))}")
    for name, counts in totals.items():
        assert counts["programs"] == PROGRAMS, name
        assert counts["fail_open"] == 0, (name, dict(counts), WHY[:2])
        assert counts["noise"] == 0, (name, dict(counts), WHY[:2])
        # Not vacuous: deleted lines beside an added definition, the defect's shape, actually occurred.
        assert counts["with_deletions"] >= PROGRAMS // 2, (name, dict(counts))
        assert counts["beside_added"] >= 3, (name, dict(counts))
        assert counts["twin_swaps"] >= 2, (name, dict(counts))
        assert counts["deleted_lines"] >= PROGRAMS, (name, dict(counts))


def test_the_differential_sees_the_defect_when_the_owned_old_text_is_not_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: with the deleted text left out of the fingerprint the same programs fail open."""

    monkeypatch.setattr(units_module._OwnedOldText, "within", lambda self, start, end, group=False: "")
    counts: Counter[str] = Counter()
    for lang in LANGS[:2]:
        for index in range(PROGRAMS):
            _run_program(tmp_path, lang, index, counts)
    assert counts["fail_open"] >= 1, dict(counts)


def test_symbol_fingerprint_keeps_its_legacy_value_without_owned_old_text() -> None:
    legacy = units_module.symbol_fingerprint("function", "f", "def f():\n    return 1\n")
    assert units_module.symbol_fingerprint("function", "f", "def f():\n    return 1\n", "") == legacy
    assert units_module.symbol_fingerprint("function", "f", "def f():\n    return 1\n", "gone") != legacy


# --------------------------------------------------------------------------------------------------------------------
# ``reconcile``: what a fingerprint that covers deleted text does to a verdict that stays, moves or loses its neighbour.
# --------------------------------------------------------------------------------------------------------------------

_BODY = "def keep(x):\n    return x + 1\n"


def _unit_at(path: str, old: str = "", *, name: str = "keep", revision: str = "rrv-1") -> ReviewUnit:
    return ReviewUnit(
        revision_id=revision,
        unit_key=units_module.unit_key("symbol", path, name, 0),
        kind="symbol",
        path=path,
        content_fingerprint=units_module.symbol_fingerprint("function", name, _BODY, old),
        fingerprint_method="symbol_body_sha256",
        symbol=name,
        start_line=1,
        end_line=2,
    )


def _verdict(unit: ReviewUnit, state: str = "reviewed") -> ReviewMark:
    return ReviewMark(
        review_id="rev-1",
        unit_key=unit.unit_key,
        state=state,  # type: ignore[arg-type]
        reviewed_revision_id="rrv-1",
        content_fingerprint=unit.content_fingerprint,
    )


def _states(result: Any) -> dict[str, tuple[str, str]]:
    return {mark.unit_key: (mark.state, mark.content_fingerprint) for mark in result.marks}


def test_the_same_definition_with_more_deleted_text_beside_it_reopens() -> None:
    before, after = _unit_at("a.py", "LIMIT = 3"), _unit_at("a.py", "LIMIT = 3\nOTHER = 4", revision="rrv-2")
    assert before.content_fingerprint != after.content_fingerprint
    result = reconcile([before], [after], [_verdict(before)])
    assert _states(result)[after.unit_key][0] == "changed_since_review"


def test_the_same_definition_with_the_same_deleted_text_beside_it_keeps_its_verdict() -> None:
    before, after = _unit_at("a.py", "LIMIT = 3"), _unit_at("a.py", "LIMIT = 3", revision="rrv-2")
    result = reconcile([before], [after], [_verdict(before)])
    assert _states(result)[after.unit_key] == ("reviewed", after.content_fingerprint)
    assert result.carried and not result.reopened


def test_a_definition_that_now_shows_less_deleted_text_keeps_its_verdict_stamped_to_what_is_on_screen() -> None:
    before, after = _unit_at("a.py", "LIMIT = 3"), _unit_at("a.py", "", revision="rrv-2")
    result = reconcile([before], [after], [_verdict(before)])
    assert _states(result)[after.unit_key] == ("reviewed", after.content_fingerprint)
    assert after.content_fingerprint == units_module.bare_fingerprint(before.content_fingerprint)
    # Its own outcome: not a reopening, not a base-only carry, and its reason says what happened.
    assert [mark.unit_key for mark in result.restamped] == [after.unit_key]
    assert not result.reopened and not result.rebased and not result.carried
    assert [code for _who, _key, code in result.reasons] == ["deleted_text_no_longer_shown"]


def test_a_verdict_that_was_already_stale_is_not_freshened_by_showing_less() -> None:
    before, after = _unit_at("a.py", "LIMIT = 3"), _unit_at("a.py", "", revision="rrv-2")
    stale = ReviewMark(
        review_id="rev-1",
        unit_key=before.unit_key,
        state="reviewed",
        reviewed_revision_id="rrv-0",
        content_fingerprint=_unit_at("a.py", "older").content_fingerprint,
    )
    assert _states(reconcile([before], [after], [stale]))[after.unit_key][0] == "changed_since_review"


def test_a_move_carries_to_a_destination_that_shows_no_deleted_text_and_not_to_one_that_does() -> None:
    source = _unit_at("a.py", "LIMIT = 3")
    clean = _unit_at("b.py", "", revision="rrv-2")
    noisy = _unit_at("b.py", "OTHER = 4", revision="rrv-2")
    headers = {source.unit_key: "", clean.unit_key: ""}
    moved = reconcile([source], [clean], [_verdict(source)], headers=headers)
    assert _states(moved)[clean.unit_key] == ("reviewed", clean.content_fingerprint)
    assert [(m.kind, m.old_key, m.new_key) for m in moved.moves] == [("moved", source.unit_key, clean.unit_key)]

    refused = reconcile([source], [noisy], [_verdict(source)], headers={source.unit_key: "", noisy.unit_key: ""})
    assert not any(state == "reviewed" for state, _fp in _states(refused).values())
    assert not refused.moves, "text the verdict never covered is not a move"


def test_a_file_rename_carries_the_verdict_to_a_definition_that_shows_no_deleted_text() -> None:
    source, dest = _unit_at("a.py", "LIMIT = 3"), _unit_at("b.py", "", revision="rrv-2")
    result = reconcile([source], [dest], [_verdict(source)], renames=[("a.py", "b.py")])
    assert _states(result)[dest.unit_key] == ("reviewed", dest.content_fingerprint)
    renamed_with_text = _unit_at("b.py", "OTHER = 4", revision="rrv-2")
    result = reconcile([source], [renamed_with_text], [_verdict(source)], renames=[("a.py", "b.py")])
    assert _states(result)[renamed_with_text.unit_key][0] == "changed_since_review"


# --------------------------------------------------------------------------------------------------------------------
# Which base files a capture without base blobs reads.
# --------------------------------------------------------------------------------------------------------------------


def test_load_old_blobs_reads_only_the_base_text_a_deleted_row_or_a_manifest_needs(tmp_path: Path) -> None:
    body = "".join(f"line {n}\n" for n in range(1, 21))
    manifest = '{\n  "name": "x",\n  "dependencies": {\n    "a": "1",\n    "z": "9"\n  }\n}\n'
    repo = _Repo(
        tmp_path / "repo",
        tmp_path / "store",
        {
            "old_name.py": body,
            "moved_only.py": "".join(f"keep {n}\n" for n in range(1, 21)),
            "adds_only.py": "a = 1\n",
            "mode_only.sh": "echo hi\n",
            "package.json": manifest,
            "deletes.py": "one\ntwo\nthree\n",
        },
    )
    (repo.root / "old_name.py").unlink()
    (repo.root / "moved_only.py").rename(repo.root / "kept_name.py")
    repo.write(
        {
            "new_name.py": body.replace("line 7\n", ""),
            "adds_only.py": "a = 1\nb = 2\n",
            "package.json": manifest.replace('"a": "1",\n', '"a": "1",\n    "b": "2",\n'),
            "deletes.py": "one\nthree\n",
        }
    )
    (repo.root / "mode_only.sh").chmod(0o755)
    git = pygit2.Repository(str(repo.root))
    git.index.add_all()
    git.index.write()
    signature = pygit2.Signature("Fixture Tester", "tester@example.invalid", 1700000200, 0)
    git.create_commit("HEAD", signature, signature, "head", git.index.write_tree(), [git.head.target])

    rng = resolve_rev_range(repo.root, "HEAD~1..HEAD")
    files = {item.path: item for item in collect_diff(repo.root, rng).files}
    assert files["new_name.py"].old_path == "old_name.py" and files["new_name.py"].status == "renamed"
    assert files["kept_name.py"].old_path == "moved_only.py" and files["kept_name.py"].hunks == ()
    for added_only in ("adds_only.py", "package.json"):
        assert files[added_only].hunks and all(hunk.removed == 0 for hunk in files[added_only].hunks), added_only
    assert "mode_only.sh" in files and files["mode_only.sh"].hunks == ()

    blobs = load_old_blobs(repo.root, rng, tuple(files.values()))

    assert blobs == {
        "new_name.py": body,  # keyed by the new path, read at the old one
        "deletes.py": "one\ntwo\nthree\n",
        "package.json": manifest,  # a manifest that only adds lines: its dependency side needs the base text
    }
    assert {path for path, item in files.items() if wants_old_blob(item)} == set(blobs)


# --------------------------------------------------------------------------------------------------------------------
# The end-of-file marker, and a deleted row that merely ends like one.
# --------------------------------------------------------------------------------------------------------------------

_MARKER_ENDINGS = ("# </html>", "# less <", "# assign =")


@pytest.mark.parametrize("last", _MARKER_ENDINGS)
def test_a_standard_unified_diff_and_libgits_patch_anchor_a_last_row_with_no_newline_to_the_same_fingerprint(
    tmp_path: Path, last: str
) -> None:
    """GitHub prints a row that ends the file with no newline as it is; libgit2's rebuilt patch appends a marker to it.

    A last row that really ends in ``<``, ``>`` or ``=`` (``</html>``) made the two indistinguishable by text alone:
    stripping a character from the standard shape read ``-</html>`` as ``</html``. Each row is checked against the base
    blob, so both shapes hash the base row and the real character stays.
    """

    path = "m.py"
    base = f"import os\n\ndef other():\n    return 2\n\n{last}"
    r1 = "import os\n\ndef other():\n    return 2\n\ndef added(x):\n    return x + 1"
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: base})
    first = repo.advance({path: r1})
    payload = read_packet_json(repo.store, first.revision)
    assert payload is not None
    libgit_patches = [hunk["patch"] for item in payload["files"] for hunk in item["hunks"]]
    assert any(f"-{last}" in patch and "\n\\ No newline" in patch for patch in libgit_patches)

    def derived(shape: str) -> str:
        changed = review_packet_from_dict(payload)
        files = []
        for item in changed.files:
            hunks = []
            for hunk in item.hunks:
                patch = hunk.patch
                if shape == "standard":
                    # The marker character libgit2 appends to a row that precedes a "\ No newline" line, removed.
                    patch = re.sub(r"(?m)^([-+ ].*)[<>=](?=\n\\ No newline)", r"\1", patch)
                hunks.append(replace(hunk, patch=patch))
            files.append(replace(item, hunks=tuple(hunks)))
        units = units_module.derive_units(replace(changed, files=tuple(files)), {path: r1}, old_blobs={path: base})
        return next(unit for unit in units if unit.symbol == "added").content_fingerprint

    one, two = derived("libgit2"), derived("standard")
    assert units_module.owns_deleted_text(one)
    assert one == two, "the same deleted row, read from either tool's patch, is the same part"
    assert one == derived("libgit2"), "anchored: nothing random in it"
    assert derived("standard") == two


@pytest.mark.parametrize("last", ("# markup </div>", "# less <", "# assign ="))
def test_a_deleted_row_that_ends_in_a_marker_character_beside_a_reviewed_definition_keeps_its_verdict(
    tmp_path: Path, last: str
) -> None:
    """A deleted row in the middle of the file is printed as it is, so its last character is the row's own."""

    path = "m.py"
    base = f"import os\n\n{last}\n\ndef keep():\n    return 1\n"
    r1 = "import os\n\ndef added(x):\n    return x + 1\n\ndef keep():\n    return 1\n"
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: base})
    first = repo.advance({path: r1})
    owned = {u.symbol: u for u in repo.store.list_units(first.revision.id)}["added"]
    assert units_module.owns_deleted_text(owned.content_fingerprint), "the fixture has a part that owns the row"
    repo.mark_everything(first.revision)
    elsewhere = repo.advance({path: r1.replace("return 1", "return 2")})
    _units, targets = repo.targets(elsewhere.revision)
    assert _symbol_states(targets, path)["added"] == "reviewed", _symbol_states(targets, path)


def test_the_deleted_text_digest_is_checked_when_the_body_digest_matches(tmp_path: Path) -> None:
    """``carry_recorded_anchors`` keeps a recorded fingerprint only where the body is the same AND no base blob is at hand."""

    path = "m.py"
    base = "import os\n\nLIMIT = 3\n\ndef keep(x):\n    y = x + 1\n    return y\n"
    r1 = "import os\n\ndef added(x):\n    return x + 1\n\ndef keep(x):\n    y = x + 2\n    return y\n"
    repo = _Repo(tmp_path / "repo", tmp_path / "store", {path: base})
    first = repo.advance({path: r1})
    stored = repo.store.list_units(first.revision.id)
    owner = next(unit for unit in stored if unit.symbol == "added")
    assert units_module.owns_deleted_text(owner.content_fingerprint)
    full, bare, _owned = owner.content_fingerprint.split("~")
    # The same body, other deleted text: the bare digests agree and only the owned digest differs.
    other_text = replace(owner, content_fingerprint=f"{full}~{bare}~{'0' * 64}")
    assert units_module.bare_fingerprint(other_text.content_fingerprint) == units_module.bare_fingerprint(
        owner.content_fingerprint
    )
    assert other_text.content_fingerprint != owner.content_fingerprint
    # Without a base blob the recorded fingerprint is the one that was recorded for that part.
    assert carry_recorded_anchors([other_text], stored, {}) == [owner]
    # With the base file at hand the derived fingerprint stands: the path check, not the body check, decides.
    assert carry_recorded_anchors([other_text], stored, {path: base}) == [other_text]


def test_a_deleted_row_that_is_only_a_marker_character_is_not_read_as_blank_by_the_ownership_rule() -> None:
    """The text ``owned_old_count`` reads must keep a mid-file row whole: a deleted TSX ``  >`` is code, not a blank.

    Stripped of its last character it would be blank, the foreign ``function Foreign() {`` below it would take it as
    its own prelude, and the definition would own two of the deleted rows instead of three.
    """

    patch = " import x\n-const a = 1;\n-const b = 2;\n-  >\n-function Foreign() {\n-}\n+function added() {\n+}\n"
    blocks = units_module.change_blocks({"patch": patch, "old_start": 1, "new_start": 1})
    assert blocks is not None and len(blocks) == 1
    assert blocks[0].old_text == ("const a = 1;", "const b = 2;", "  >", "function Foreign() {", "}")
    source = {2: "function added() {", 3: "}"}
    assert units_module.owned_old_count(blocks[0], source, 2, 3, owner_is_group=False) == 3
