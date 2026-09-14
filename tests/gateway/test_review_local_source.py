"""The local review source: reopen, don't re-create; re-snapshot, don't duplicate.

Phase 1's acceptance criterion is one sentence -- *stop and reopen, and every
review mark survives* -- and it is asserted here against a real SQLite file
through a second ``ReviewStore`` instance, which is what a restart actually
looks like from the store's point of view
(:func:`test_a_mark_survives_a_restart`).

The two invariants underneath it get their own tests because breaking either
one loses marks silently rather than loudly: a second ``lc review`` that
manufactured a rival session would strand the first one's marks, and a
re-snapshot of an untouched tree that manufactured a second revision would
strand the marks made against the first.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pygit2
import pytest

from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.models import ReviewPacket
from lemoncrow.pro.capabilities.review.packet import build_review_packet, build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.session_models import ReviewSession
from lemoncrow.pro.capabilities.review.sources.local import (
    coerce_mark_state,
    effective_mark_state,
    encode_packet,
    mark_downgrade_note,
    mark_unit,
    open_or_create_session,
    range_for_session,
    read_packet_json,
    refresh,
    resolve_mark_units,
    resolve_unit,
    snapshot_revision,
    source_ref,
)
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.units import file_fingerprint

_BASE = '''"""Session management."""


class SessionManager:
    def __init__(self, store):
        self.store = store

    def refresh(self, user):
        record = self.store.get(user)
        if record is None:
            return {"status": "expired"}
        return {"status": "ok", "user": user}
'''

_CHANGED = _BASE.replace('"expired"', '"invalid"')

_HELPER = "def helper(value):\n    return value + 1\n"


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the impact detectors from bootstrapping an ast-grep binary per repo."""

    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


def _init_repo(root: Path) -> Any:
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.test"
    return repo


def _commit(repo: Any, message: str, offset: int) -> str:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    signature = pygit2.Signature("Fixture Tester", "fixture@example.test", 1700000000 + offset, 0)
    return str(repo.create_commit("HEAD", signature, signature, message, tree, parents))


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _dirty_repo(tmp_path: Path) -> tuple[Path, Any]:
    """A committed baseline plus an uncommitted edit -- the reviewed moment."""

    root = tmp_path / "repo"
    repo = _init_repo(root)
    _write(root, "src/session.py", _BASE)
    _write(root, "src/helper.py", _HELPER)
    _commit(repo, "baseline", 0)
    _write(root, "src/session.py", _CHANGED)
    return root, repo


def _store(tmp_path: Path) -> ReviewStore:
    return ReviewStore(tmp_path / "store")


def _named_range_repo(tmp_path: Path) -> tuple[Path, Any]:
    """main at a baseline and feature one commit ahead, both as named refs."""

    root = tmp_path / "named-repo"
    repo = _init_repo(root)
    _write(root, "src/session.py", _BASE)
    _commit(repo, "baseline", 0)
    repo.branches.local.create("feature", repo[repo.head.target])
    repo.checkout("refs/heads/feature")
    _write(root, "src/session.py", _CHANGED)
    _commit(repo, "feature one", 60)
    return root, repo


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #


def test_open_or_create_session_twice_returns_the_same_session(tmp_path: Path) -> None:
    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    store = _store(tmp_path)

    first = open_or_create_session(store, root, rng)
    second = open_or_create_session(store, root, rng)
    assert first.id == second.id
    assert len(store.list_sessions()) == 1


def test_a_reopened_session_is_found_by_a_fresh_store_instance(tmp_path: Path) -> None:
    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    first = open_or_create_session(_store(tmp_path), root, rng)
    second = open_or_create_session(_store(tmp_path), root, rng)
    assert first.id == second.id


def test_discarded_session_is_read_only_until_explicitly_restored(tmp_path: Path) -> None:
    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)
    store.update_session(session.id, status="archived")
    discarded = store.get_session(session.id)
    assert discarded is not None
    discarded_at = discarded.updated_at

    with pytest.raises(ValueError, match="discarded and read-only"):
        open_or_create_session(store, root, rng)

    restored = open_or_create_session(store, root, rng, restore_archived=True)
    assert restored.id == session.id
    assert restored.status == "open"
    assert restored.updated_at >= discarded_at
    assert store.prune(older_than_days=0) == 0


def test_a_working_tree_session_has_no_source_ref_and_a_commit_range_has_shas(tmp_path: Path) -> None:
    root, repo = _dirty_repo(tmp_path)
    assert source_ref(resolve_rev_range(root)) == ""

    _commit(repo, "second", 60)
    rng = resolve_rev_range(root, "HEAD~1")
    assert source_ref(rng) == f"{rng.base_sha}..{rng.head_sha}"
    assert "HEAD" not in source_ref(rng)


def test_a_working_tree_and_a_commit_range_are_different_sessions(tmp_path: Path) -> None:
    root, repo = _dirty_repo(tmp_path)
    store = _store(tmp_path)
    working = open_or_create_session(store, root, resolve_rev_range(root))
    _commit(repo, "second", 60)
    ranged = open_or_create_session(store, root, resolve_rev_range(root, "HEAD~1"))
    assert working.id != ranged.id


def test_named_branch_range_follows_branch_head_in_one_session(tmp_path: Path) -> None:
    root, repo = _named_range_repo(tmp_path)
    store = _store(tmp_path)
    first_range = resolve_rev_range(root, "main...feature")
    first = open_or_create_session(store, root, first_range)

    assert source_ref(first_range, repo_root=root) == "named:refs/heads/main...refs/heads/feature"

    _write(root, "src/session.py", _CHANGED.replace('"invalid"', '"newer"'))
    _commit(repo, "feature two", 120)
    second_range = resolve_rev_range(root, "main...feature")
    second = open_or_create_session(store, root, second_range)

    assert second.id == first.id
    assert second.source_ref == first.source_ref
    assert first_range.head_sha != second_range.head_sha
    assert range_for_session(root, second).head_sha == second_range.head_sha


def test_relative_commit_range_remains_snapshot_isolated(tmp_path: Path) -> None:
    root, repo = _named_range_repo(tmp_path)
    store = _store(tmp_path)
    first_range = resolve_rev_range(root, "HEAD~1..HEAD")
    first = open_or_create_session(store, root, first_range)

    _write(root, "src/session.py", _CHANGED.replace('"invalid"', '"newer"'))
    _commit(repo, "feature two", 120)
    second_range = resolve_rev_range(root, "HEAD~1..HEAD")
    second = open_or_create_session(store, root, second_range)

    assert first.id != second.id
    assert first.source_ref == f"{first_range.base_sha}..{first_range.head_sha}"
    assert second.source_ref == f"{second_range.base_sha}..{second_range.head_sha}"


def test_named_range_adopts_legacy_session_with_human_progress(tmp_path: Path) -> None:
    root, repo = _named_range_repo(tmp_path)
    store = _store(tmp_path)
    first_range = resolve_rev_range(root, "main...feature")
    resolved_root = str(root.resolve())

    reviewed = store.create_session(
        ReviewSession(
            id="",
            subject_type="commit_range",
            repo_root=resolved_root,
            range_mode="commit_range",
            source_ref=f"{first_range.base_sha}..{first_range.head_sha}",
        )
    )
    first_revision = snapshot_revision(store, reviewed, root, first_range, store_root=tmp_path / "store")
    unit = resolve_unit(store.list_units(first_revision.id), "src/session.py")
    assert unit is not None
    mark_unit(store, reviewed, first_revision, unit, state="reviewed")

    _write(root, "src/session.py", _CHANGED.replace('"invalid"', '"newer"'))
    _commit(repo, "feature two", 120)
    second_range = resolve_rev_range(root, "main...feature")
    empty_newer = store.create_session(
        ReviewSession(
            id="",
            subject_type="commit_range",
            repo_root=resolved_root,
            range_mode="commit_range",
            source_ref=f"{second_range.base_sha}..{second_range.head_sha}",
        )
    )
    snapshot_revision(store, empty_newer, root, second_range, store_root=tmp_path / "store")

    _write(root, "src/session.py", _CHANGED.replace('"invalid"', '"newest"'))
    _commit(repo, "feature three", 180)
    current_range = resolve_rev_range(root, "main...feature")
    adopted = open_or_create_session(store, root, current_range)

    assert adopted.id == reviewed.id
    assert adopted.source_ref == "named:refs/heads/main...refs/heads/feature"
    assert [mark.state for mark in store.list_marks(adopted.id)] == ["reviewed"]

    current_revision = snapshot_revision(store, adopted, root, current_range, store_root=tmp_path / "store")
    assert current_revision.revision_number == 2


def test_a_brand_new_named_range_never_adopts_a_discarded_session(tmp_path: Path) -> None:
    """Adoption must not hand a range its first reviewer never opened a tombstone.

    Re-keying an ``archived`` row happens *before* ``_usable`` refuses it, so
    the reviewer is told a review they have never opened is discarded -- and
    every retry repeats it, because the new named key now resolves to the
    stolen row while the discarded review is no longer reachable under its own.
    """

    root, repo = _named_range_repo(tmp_path)
    store = _store(tmp_path)

    # A SHA-keyed review of the branch's first commit, then discarded.
    legacy_range = resolve_rev_range(root, "HEAD~1..HEAD")
    legacy = open_or_create_session(store, root, legacy_range, title="stack one")
    snapshot_revision(store, legacy, root, legacy_range, store_root=tmp_path / "store")
    store.update_session(legacy.id, status="archived")
    legacy_ref = legacy.source_ref

    # A different, never-before-opened named range stacked on top of it.
    _write(root, "src/helper.py", _HELPER)
    _commit(repo, "feature two", 120)
    named_range = resolve_rev_range(root, "main..feature")
    assert source_ref(named_range, repo_root=root) == "named:refs/heads/main..refs/heads/feature"

    opened = open_or_create_session(store, root, named_range, title="stack two")
    assert opened.id != legacy.id
    assert opened.status == "open"
    assert opened.source_ref == "named:refs/heads/main..refs/heads/feature"

    # The discarded review is untouched and still reachable under its own key.
    still_discarded = store.get_session(legacy.id)
    assert still_discarded is not None
    assert still_discarded.status == "archived"
    assert still_discarded.source_ref == legacy_ref

    # And the new review keeps opening instead of being refused forever.
    assert open_or_create_session(store, root, named_range).id == opened.id


def test_a_range_based_on_the_empty_tree_reopens_and_refreshes(tmp_path: Path) -> None:
    """A single-commit (or ``--depth 1``) repo stores its base as ``"..<sha>"``.

    Splitting that on ``".."`` yields an empty base, and reopening it as
    ``base=None`` makes ``resolve_rev_range`` ask git for ``"<sha>~1"`` -- the
    one commit that provably does not exist -- so every ``POST
    /reviews/{id}/refresh`` on the session 409s for the life of the review.
    """

    root = tmp_path / "single-commit"
    repo = _init_repo(root)
    _write(root, "src/session.py", _BASE)
    _commit(repo, "only commit", 0)
    store_root = tmp_path / "store"
    store = ReviewStore(store_root)

    rng = resolve_rev_range(root)
    assert rng.mode == "commit_range"
    assert rng.base_sha == ""
    session = open_or_create_session(store, root, rng, title="only commit")
    assert session.source_ref == f"..{rng.head_sha}"
    refresh(store, session, root, store_root=store_root, rng=rng)

    reopened = range_for_session(root, session)
    assert reopened.base_sha == ""
    assert reopened.head_sha == rng.head_sha
    assert reopened.base_rev == "(empty tree)"
    # The no-range form is the one the refresh endpoint uses.
    result = refresh(store, session, root, store_root=store_root)
    assert result.created is False
    assert len(store.list_revisions(session.id)) == 1


# --------------------------------------------------------------------------- #
# revisions
# --------------------------------------------------------------------------- #


def test_snapshotting_an_unchanged_tree_creates_no_second_revision(tmp_path: Path) -> None:
    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)

    first = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")
    second = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")

    assert first.id == second.id
    assert first.tree_fingerprint == second.tree_fingerprint
    assert len(store.list_revisions(session.id)) == 1


def test_changing_the_tree_creates_a_second_dense_revision(tmp_path: Path) -> None:
    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)

    first = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")
    _write(root, "src/session.py", _CHANGED.replace("user}", "user, 'v': 2}"))
    second = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")

    assert second.id != first.id
    assert [revision.revision_number for revision in store.list_revisions(session.id)] == [1, 2]


def test_an_oversized_text_change_cannot_reuse_a_revision_with_placeholder_file_identity(tmp_path: Path) -> None:
    root = tmp_path / "large-repo"
    repo = _init_repo(root)
    # Keep the file over gitdiff.MAX_BLOB_BYTES while making each edit tiny, so
    # the file unit is `unknown` but the hunk itself remains fingerprintable.
    lines = [f"value_{index} = {index}\n" for index in range(30000)]
    _write(root, "big.py", "".join(lines))
    _commit(repo, "baseline", 0)

    lines[100] = "value_100 = 1000\n"
    _write(root, "big.py", "".join(lines))
    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)
    first = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")
    first_units = store.list_units(first.id)
    assert next(unit for unit in first_units if unit.kind == "file").fingerprint_method == "unknown"

    lines[200] = "value_200 = 2000\n"
    _write(root, "big.py", "".join(lines))
    second = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")

    assert second.id != first.id
    assert second.revision_number == 2


def test_a_revision_carries_units_for_every_changed_file(tmp_path: Path) -> None:
    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)
    revision = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")

    units = store.list_units(revision.id)
    files = {unit.path for unit in units if unit.kind == "file"}
    assert files == {"src/session.py"}
    assert any(unit.kind == "hunk" for unit in units)
    assert all(unit.revision_id == revision.id for unit in units)


def test_hunk_units_are_content_fingerprinted_because_patch_text_is_captured(tmp_path: Path) -> None:
    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)
    revision = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")

    hunks = [unit for unit in store.list_units(revision.id) if unit.kind == "hunk"]
    assert hunks
    assert all(unit.fingerprint_method == "hunk_patch_sha256" for unit in hunks)


def test_the_packet_artifact_round_trips_with_its_degraded_tuple_verbatim(tmp_path: Path) -> None:
    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)

    build = build_review_packet_with_blobs(root, rng, store_root=tmp_path / "store", with_patch_text=True)
    packet = build.packet
    revision = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store", build=build)

    raw = store.read_packet_artifact(revision)
    assert raw is not None
    assert raw[:2] == b"\x1f\x8b", "the store names the file .gz; it must actually be one"
    restored = read_packet_json(store, revision)
    assert restored is not None
    assert restored["degraded"] == list(packet.degraded)
    assert restored["schema_version"] == packet.schema_version
    assert revision.packet_bytes == len(raw)
    # The revision's own degraded set is the packet's plus the blob loader's, so
    # it may know more -- it may never know less.
    assert set(packet.degraded) <= set(revision.degraded)


def test_the_stored_packet_carries_hunk_bodies(tmp_path: Path) -> None:
    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)
    revision = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")

    restored = read_packet_json(store, revision)
    assert restored is not None
    patches = [hunk["patch"] for item in restored["files"] for hunk in item["hunks"]]
    assert any('+            return {"status": "invalid"}' in patch for patch in patches)


# --------------------------------------------------------------------------- #
# marks -- Phase 1 acceptance
# --------------------------------------------------------------------------- #


def test_a_mark_survives_a_restart(tmp_path: Path) -> None:
    """Phase 1 acceptance: stop and reopen, and the mark is still there."""

    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)

    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)
    revision = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")
    unit = resolve_unit(store.list_units(revision.id), "src/session.py")
    assert unit is not None
    mark_unit(store, session, revision, unit)

    # A second process: a brand-new store over the same file, nothing shared.
    reopened_store = _store(tmp_path)
    reopened_session = open_or_create_session(reopened_store, root, rng)
    assert reopened_session.id == session.id
    reopened_revision = snapshot_revision(reopened_store, reopened_session, root, rng, store_root=tmp_path / "store")
    assert reopened_revision.id == revision.id

    marks = reopened_store.list_marks(reopened_session.id)
    assert [(mark.unit_key, mark.state) for mark in marks] == [(unit.unit_key, "reviewed")]
    assert marks[0].content_fingerprint == unit.content_fingerprint


def test_a_mark_records_the_fingerprint_it_was_made_against(tmp_path: Path) -> None:
    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)
    revision = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")
    unit = resolve_unit(store.list_units(revision.id), "src/session.py")
    assert unit is not None
    mark = mark_unit(store, session, revision, unit)

    _write(root, "src/session.py", _CHANGED.replace("user}", "user, 'v': 2}"))
    next_revision = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")
    next_unit = resolve_unit(store.list_units(next_revision.id), "src/session.py")
    assert next_unit is not None

    # The row survives the new revision; its fingerprint no longer matches, which
    # is what makes "reviewed" impossible to preserve silently across a rewrite.
    assert store.list_marks(session.id)[0].content_fingerprint == mark.content_fingerprint
    assert next_unit.content_fingerprint != mark.content_fingerprint
    assert next_unit.unit_key == mark.unit_key


def test_snapshotting_a_rewrite_reopens_the_mark_without_being_asked(tmp_path: Path) -> None:
    """There is no way to record a revision and skip the reconciliation.

    ``snapshot_revision`` is what every surface but ``--since-my-review`` used
    to call, and it wrote a revision row and stopped. That left the store one
    revision ahead of the reconciliation forever: the next reconciling call saw
    the latest revision already equal to the one it was about to take and
    concluded nothing had changed, so a ``reviewed`` mark over rewritten code
    could never be reopened again by anything.

    Pinned at the seam rather than at each call site, because the defect was
    never in any one caller -- it was in there being two operations to choose
    between.
    """

    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)
    revision = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")
    unit = resolve_unit(store.list_units(revision.id), "src/session.py")
    assert unit is not None
    mark_unit(store, session, revision, unit)
    assert [mark.state for mark in store.list_marks(session.id)] == ["reviewed"]

    _write(root, "src/session.py", _CHANGED.replace("user}", "user, 'v': 2}"))
    snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")

    (reopened,) = [mark for mark in store.list_marks(session.id) if mark.unit_key == unit.unit_key]
    assert reopened.state == "changed_since_review"

    # And a second look is a look, not a write: no third revision, no change of
    # verdict because the developer ran the command twice.
    snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")
    assert [item.revision_number for item in store.list_revisions(session.id)] == [1, 2]
    assert [mark.state for mark in store.list_marks(session.id)] == ["changed_since_review"]


def test_only_the_reconciling_seam_can_write_a_revision() -> None:
    """There is one door, so a future caller cannot reintroduce the split.

    The defect was never in any one call site: it was that recording a revision
    and reconciling the reviewer's marks onto it were two operations, and every
    surface but one called only the first. Sprinkling the second call at each
    site would fix today's callers and none of tomorrow's, so the guarantee is
    structural -- ``ReviewStore.add_revision`` has exactly one production caller
    and it is the function that also reconciles.
    """

    import ast

    source_root = Path(__file__).resolve().parents[2] / "src" / "lemoncrow"
    seam = source_root / "pro" / "capabilities" / "review" / "sources" / "local.py"

    call_sites: list[tuple[Path, int]] = []
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_revision":
                call_sites.append((path, node.lineno))

    assert [path for path, _ in call_sites] == [seam], call_sites

    ((_, lineno),) = call_sites
    (recorder,) = [
        node
        for node in ast.walk(ast.parse(seam.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef) and node.name == "record_revision"
    ]
    assert recorder.lineno < lineno <= (recorder.end_lineno or lineno)


def test_marking_a_unit_whose_fingerprint_is_unknown_never_records_reviewed(tmp_path: Path) -> None:
    root, repo = _dirty_repo(tmp_path)
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "assets" / "logo.bin").write_bytes(bytes(range(256)) * 8)
    _commit(repo, "add binary", 60)
    (root / "assets" / "logo.bin").write_bytes(bytes(reversed(range(256))) * 8)

    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)
    revision = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")
    unit = resolve_unit(store.list_units(revision.id), "assets/logo.bin")
    assert unit is not None
    assert unit.fingerprint_method == "unknown"

    mark = mark_unit(store, session, revision, unit, state="reviewed")
    assert mark.state == "unknown"


def test_marks_are_per_unit_not_per_file_path(tmp_path: Path) -> None:
    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)
    revision = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")
    units = store.list_units(revision.id)

    file_unit = resolve_unit(units, "src/session.py")
    hunk_unit = next(unit for unit in units if unit.kind == "hunk")
    assert file_unit is not None
    mark_unit(store, session, revision, file_unit)
    mark_unit(store, session, revision, hunk_unit, state="needs_changes")

    states = {mark.unit_key: mark.state for mark in store.list_marks(session.id)}
    assert states[file_unit.unit_key] == "reviewed"
    assert states[hunk_unit.unit_key] == "needs_changes"


# --------------------------------------------------------------------------- #
# resolution helpers
# --------------------------------------------------------------------------- #


def test_resolve_unit_accepts_a_path_or_a_unit_key_and_refuses_anything_else(tmp_path: Path) -> None:
    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)
    revision = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")
    units = store.list_units(revision.id)

    by_path = resolve_unit(units, "src/session.py")
    assert by_path is not None
    assert by_path.kind == "file"
    assert resolve_unit(units, by_path.unit_key) is by_path
    assert resolve_unit(units, "src/never_touched.py") is None


def test_resolving_a_path_picks_the_file_unit_not_a_hunk_inside_it(tmp_path: Path) -> None:
    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)
    revision = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")
    unit = resolve_unit(store.list_units(revision.id), "src/session.py")
    assert unit is not None and unit.kind == "file"


def test_resolving_a_mark_target_expands_a_path_onto_the_targets_it_covers(tmp_path: Path) -> None:
    """The file unit a path resolves to is not a key any counter reads.

    ``resolve_unit`` is right that "I read this file" is the claim a path makes,
    but progress and closure are denominated in :class:`ReviewTarget` rows keyed
    to symbols or hunks, so a verdict parked on the file unit moved nothing at
    all. Marking has to expand the path onto the judgments it actually covers.
    """

    from lemoncrow.pro.capabilities.review.targets import derive_review_targets

    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)
    revision = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")
    units = store.list_units(revision.id)
    targets = derive_review_targets(units, (), read_packet_json(store, revision))

    covered = resolve_mark_units(units, targets, "src/session.py").units
    assert {unit.unit_key for unit in covered} == {
        target.unit_key for target in targets if target.path == "src/session.py"
    }
    assert covered, "the path derived no target at all"
    file_unit = resolve_unit(units, "src/session.py")
    assert file_unit is not None and file_unit.unit_key not in {unit.unit_key for unit in covered}

    # A unit key is the precise form: it is never widened, because expanding it
    # would store verdicts the reviewer did not type.
    hunk_unit = next(unit for unit in units if unit.kind == "hunk")
    assert resolve_mark_units(units, targets, hunk_unit.unit_key).units == (hunk_unit,)

    # And a path outside the revision stays a refusal, so the caller can say so
    # rather than record silence.
    assert resolve_mark_units(units, targets, "src/never_touched.py").units == ()


def test_resolving_a_mark_target_widens_the_file_key_exactly_as_it_widens_the_path(
    tmp_path: Path,
) -> None:
    """``src/session.py`` and its ``fil:`` key are one claim typed two ways.

    The file key is printed by ``lc review --units`` and documented in the
    walkthrough beside the path, so leaving it unwidened left the documented
    sibling parked on a unit no target counter reads -- the same defect, reached
    by the other spelling.
    """

    from lemoncrow.pro.capabilities.review.targets import derive_review_targets

    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)
    revision = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")
    units = store.list_units(revision.id)
    targets = derive_review_targets(units, (), read_packet_json(store, revision))

    file_unit = resolve_unit(units, "src/session.py")
    assert file_unit is not None and file_unit.kind == "file"
    assert resolve_mark_units(units, targets, file_unit.unit_key) == resolve_mark_units(
        units, targets, "src/session.py"
    )
    assert file_unit.unit_key not in {
        unit.unit_key for unit in resolve_mark_units(units, targets, file_unit.unit_key).units
    }


def test_resolving_a_mark_target_accepts_the_label_a_surface_printed(tmp_path: Path) -> None:
    """Reader spec §29.3: accept target labels cleanly.

    The label is what the terminal disclosure, the browser and the feedback
    bundle all print for a target, so it has to be typeable back. One label
    names one judgment, so it is never widened.
    """

    from lemoncrow.pro.capabilities.review.targets import derive_review_targets

    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)
    revision = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")
    units = store.list_units(revision.id)
    targets = derive_review_targets(units, (), read_packet_json(store, revision))

    for item in targets:
        resolution = resolve_mark_units(units, targets, item.label)
        assert [unit.unit_key for unit in resolution.units] == [item.unit_key], (item.label, resolution)
        # No repository file is spelled like this label, so nothing lost to it
        # and there is nothing for the caller to disclose.
        assert resolution.shadowed == (), (item.label, resolution)

    assert resolve_mark_units(units, targets, "src/session.py::never_written").units == ()


def test_resolving_a_mark_target_falls_back_to_the_file_unit_without_a_target_view(
    tmp_path: Path,
) -> None:
    """A caller with no targets to expand onto still gets a unit, never silence.

    Expansion needs the revision's derived targets, and a caller that holds none
    -- or holds a list derived against some other revision -- must not have its
    path turned into a refusal: a path this revision contains is a match, and a
    match may never be a no-op.
    """

    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)
    revision = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")
    units = store.list_units(revision.id)

    file_unit = resolve_unit(units, "src/session.py")
    assert file_unit is not None
    assert resolve_mark_units(units, (), "src/session.py").units == (file_unit,)
    assert resolve_mark_units(units, (), file_unit.unit_key).units == (file_unit,)
    assert resolve_mark_units(units, (), "src/never_touched.py").units == ()


def test_a_real_path_outranks_a_target_label_spelled_the_same_way(tmp_path: Path) -> None:
    """Target labels are path-shaped, so a repository can contain one as a file.

    ``src/app.py#0`` and ``src/app.py::two`` are labels *and* legal file names.
    Matching the label first meant a changed file whose own path was spelled
    like some other file's label had every verdict on it redirected into that
    other file -- and silently, because the widening disclosure fires only when
    the resolution widened, which by then it had not. The file the repository
    actually contains wins, and the label it beat comes back so the caller can
    name what it did not do.
    """

    from lemoncrow.pro.capabilities.review.targets import derive_review_targets

    root, _repo = _dirty_repo(tmp_path)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, resolve_rev_range(root))
    first = snapshot_revision(store, session, root, resolve_rev_range(root), store_root=tmp_path / "store")
    labels = derive_review_targets(store.list_units(first.id), (), read_packet_json(store, first))
    shadowed_label = next(item.label for item in labels if item.path == "src/session.py" and item.label != item.path)

    # ... and now a real file turns up spelled exactly like that label.
    _write(root, shadowed_label, "a file whose name reads like somebody else's label\n")
    rng = resolve_rev_range(root)
    revision = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")
    units = store.list_units(revision.id)
    targets = derive_review_targets(units, (), read_packet_json(store, revision))

    resolution = resolve_mark_units(units, targets, shadowed_label)
    assert resolution.units, "the file in the repository matched nothing at all"
    assert {unit.path for unit in resolution.units} == {shadowed_label}, resolution.units
    assert {unit.unit_key for unit in resolution.shadowed} == {
        item.unit_key for item in targets if item.label == shadowed_label and item.path != shadowed_label
    }, resolution.shadowed
    assert resolution.shadowed, "the label the path outranked was dropped without a word"


def test_incremental_reuse_carries_the_partial_scan_marker_it_reuses(tmp_path: Path) -> None:
    """A capped scan stays capped when the next revision reuses its sites.

    ``contract_literal_scan_truncated`` is recorded when the batched ast-grep
    pass found more matches than one pass materialises, so an empty result from
    it means "not looked at everywhere", not "not present". Incremental reuse
    copies that pass's sites into the next packet; classified in neither the
    blocking set nor the carry set, it copied the sites and dropped the caveat,
    presenting a truncated scan as exhaustive -- the one claim the token exists
    to prevent. ``contract_literal_unparsed`` rides along here as the sibling
    with identical semantics, so the two cannot drift apart again.
    """

    from lemoncrow.pro.capabilities.review.sources.local import _incremental_impact_reuse
    from lemoncrow.pro.capabilities.tool_supervision.edit_impact import (
        LITERAL_SCAN_TRUNCATED,
        LITERAL_SCAN_UNPARSED,
    )

    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)
    previous = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")

    # The revision as recorded, re-stamped with the two partial-pass tokens.
    # Whether this fixture's own scan happens to cap is not the subject; how a
    # capped scan is classified on the way into the next revision is.
    packet = read_packet_json(store, previous)
    assert packet is not None
    packet["degraded"] = [LITERAL_SCAN_TRUNCATED, LITERAL_SCAN_UNPARSED]
    store.write_packet_artifact(
        session.id,
        previous.id,
        gzip.compress(json.dumps(packet).encode("utf-8"), mtime=0),
    )

    current = build_review_packet_with_blobs(
        root,
        rng,
        store_root=tmp_path / "store",
        with_impact=False,
        with_patch_text=True,
        unbounded_patch_text=True,
    )
    reuse = _incremental_impact_reuse(store, previous, current, rng)
    assert reuse is not None, "an unchanged tree carrying only partial-pass tokens must still reuse"
    assert set(reuse.carried_degraded) == {LITERAL_SCAN_TRUNCATED, LITERAL_SCAN_UNPARSED}


def test_coerce_mark_state_refuses_an_unknown_name(tmp_path: Path) -> None:
    assert coerce_mark_state("reviewed") == "reviewed"
    with pytest.raises(ValueError, match="unknown mark state"):
        coerce_mark_state("approved")


def test_encoding_the_same_packet_twice_produces_the_same_bytes(tmp_path: Path) -> None:
    """``packet_sha256`` must identify the packet, not the second it was written."""

    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    packet = build_review_packet(root, rng, store_root=tmp_path / "store", with_patch_text=True)
    assert encode_packet(packet) == encode_packet(packet)


def test_read_packet_json_returns_none_for_a_corrupt_artifact(tmp_path: Path) -> None:
    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)
    revision = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")

    (tmp_path / "store" / revision.packet_path).write_bytes(b"not a gzip stream")
    assert read_packet_json(store, revision) is None


# --------------------------------------------------------------------------- #
# the record and the fingerprints must describe the same bytes
# --------------------------------------------------------------------------- #


def test_the_stored_packet_and_its_fingerprints_describe_the_same_bytes(tmp_path: Path) -> None:
    """The invariant the whole persisting path exists to hold.

    The packet is built at T and stored verbatim. If the fingerprints were
    computed from a *second* read of the working tree, a save landing between
    the two reads would store content A under fingerprints describing content
    B, and the reviewer would be marking a packet they were never shown.

    The save is simulated by rewriting the file between the build and the
    snapshot -- the exact window that used to be open.
    """

    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)

    build = build_review_packet_with_blobs(root, rng, store_root=tmp_path / "store", with_patch_text=True)
    shown = build.blobs.new["src/session.py"]

    # The developer saves again while the impact pass is still running.
    _write(root, "src/session.py", _CHANGED.replace('"invalid"', '"revoked-after-the-build"'))

    revision = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store", build=build)

    restored = read_packet_json(store, revision)
    assert restored is not None
    stored_patches = "\n".join(hunk["patch"] for item in restored["files"] for hunk in item["hunks"])
    assert "revoked-after-the-build" not in stored_patches, "the stored artifact must be the packet that was built"

    file_unit = next(
        unit for unit in store.list_units(revision.id) if unit.kind == "file" and unit.path == "src/session.py"
    )
    assert file_unit.content_fingerprint == file_fingerprint("src/session.py", shown)
    assert file_unit.content_fingerprint != file_fingerprint("src/session.py", (root / "src/session.py").read_text())


def test_a_build_carries_the_blobs_even_with_the_impact_pass_off(tmp_path: Path) -> None:
    """``--no-impact --track`` must still fingerprint real content.

    The blob read used to be unconditional in the snapshot; moving it into the
    build must not make a diff-only packet fall back to ``unknown`` file
    fingerprints.
    """

    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    build = build_review_packet_with_blobs(
        root, rng, store_root=tmp_path / "store", with_impact=False, with_patch_text=True
    )
    assert build.blobs.new["src/session.py"] == _CHANGED

    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)
    revision = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store", build=build)
    file_units = [unit for unit in store.list_units(revision.id) if unit.kind == "file"]
    assert file_units
    assert all(unit.fingerprint_method == "blob_sha256" for unit in file_units)


def test_build_review_packet_still_returns_a_bare_packet(tmp_path: Path) -> None:
    """The un-widened entry point keeps its contract for the display-only path."""

    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    packet = build_review_packet(root, rng, store_root=tmp_path / "store")
    build = build_review_packet_with_blobs(root, rng, store_root=tmp_path / "store")
    assert isinstance(packet, ReviewPacket)
    assert [item.path for item in packet.files] == [item.path for item in build.packet.files]
    assert packet.degraded == build.packet.degraded


# --------------------------------------------------------------------------- #
# a downgraded mark is said out loud
# --------------------------------------------------------------------------- #


def test_a_unit_without_a_fingerprint_downgrades_and_explains_itself(tmp_path: Path) -> None:
    root, _repo = _dirty_repo(tmp_path)
    rng = resolve_rev_range(root)
    store = _store(tmp_path)
    session = open_or_create_session(store, root, rng)
    revision = snapshot_revision(store, session, root, rng, store_root=tmp_path / "store")

    unit = next(unit for unit in store.list_units(revision.id) if unit.kind == "file")
    unknown = replace(unit, fingerprint_method="unknown")

    assert effective_mark_state(unknown, "reviewed") == "unknown"
    assert effective_mark_state(unknown, "needs_changes") == "needs_changes"
    assert effective_mark_state(unit, "reviewed") == "reviewed"

    note = mark_downgrade_note(unknown, "reviewed", effective_mark_state(unknown, "reviewed"))
    assert "not reviewed" in note
    assert "could not be" in note
    assert mark_downgrade_note(unit, "reviewed", "reviewed") == ""

    recorded = mark_unit(store, session, revision, unknown, state="reviewed")
    assert recorded.state == "unknown"


# --------------------------------------------------------------------------- #
# a verdict a rename carried is not a verdict that was thrown away
# --------------------------------------------------------------------------- #


def test_a_rename_that_carried_a_verdict_is_not_reported_as_a_discard() -> None:
    """``Reconciliation.dropped`` carries two different events under one name.

    A mark whose unit left the review, and the stale old-key row of a mark a
    ``git mv`` just re-keyed and re-inserted under the new path. Only the first
    is a discarded verdict, and announcing the second as one would tell a
    reviewer their approval was deleted in the single case where it survived.

    Pure, and independent of git's rename detection -- which ``collect_diff``
    does not enable today, which is exactly why this guard needs a test of its
    own rather than an end-to-end one that cannot reach it.
    """

    from lemoncrow.pro.capabilities.review.session_models import ReviewUnit
    from lemoncrow.pro.capabilities.review.sources.local import _rename_migrations
    from lemoncrow.pro.capabilities.review.units import unit_key

    def _symbol(path: str, name: str) -> ReviewUnit:
        return ReviewUnit(
            revision_id="",
            unit_key=unit_key("symbol", path, name, 0),
            kind="symbol",
            path=path,
            symbol=name,
            content_fingerprint=f"fp-{name}",
        )

    before = (_symbol("src/a.py", "kept"), _symbol("src/a.py", "deleted_in_the_move"))
    after = (_symbol("src/b.py", "kept"),)
    renames = (("src/a.py", "src/b.py"),)

    migrated = _rename_migrations(before, after, renames)
    assert migrated == frozenset({before[0].unit_key})
    # The symbol deleted *during* the rename genuinely left the review, so its
    # verdict really is discarded and must still be announced.
    assert before[1].unit_key not in migrated
    # No rename reported, nothing migrated: every dropped mark is a real loss.
    assert _rename_migrations(before, after, ()) == frozenset()
