"""Annotations: where a comment is attached, and what happens when the code moves.

The thing under test is not "can a comment be stored" -- it is the promise that
makes stored comments worth anything: **a comment either follows its content or
says out loud that it could not.** So the assertions come in pairs. Every
relocation is checked against the rung that produced it, and every ambiguity is
checked to have produced an orphan rather than the nearest plausible line.

The anchor is always built server-side, from the file's own bytes. A test that
let the caller supply an anchor would be testing a design this project rejects:
the moment a viewer's line number becomes the record, "what changed since I
reviewed?" has no answer (spec SS5.4).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review.anchors import resolve_anchor
from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.feedback import build_bundle, render_markdown
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.session_models import ANNOTATION_KINDS
from lemoncrow.pro.capabilities.review.sources.local import (
    annotate,
    annotation_counts,
    coerce_annotation_kind,
    open_or_create_session,
    owning_symbol_unit,
    parse_line_target,
    refresh,
    snapshot_revision,
)
from lemoncrow.pro.capabilities.review.store import ReviewStore

TOKEN = "annotation-token"
PORT = 45998
BASE = f"http://127.0.0.1:{PORT}"

_BASE_SOURCE = '''"""Session management."""


class SessionManager:
    def __init__(self, store):
        self.store = store

    def refresh(self, user):
        record = self.store.get(user)
        if record is None:
            return {"status": "expired"}
        return {"status": "ok", "user": user}
'''

_CHANGED_SOURCE = _BASE_SOURCE.replace('"expired"', '"invalid"')

# The commented-on line in `_CHANGED_SOURCE`, and the line it moves to once six
# lines of imports are inserted above it.
_COMMENT_LINE = 10
_PREAMBLE = "from __future__ import annotations\n\nimport logging\n\nLOG = logging.getLogger(__name__)\n\n"
_MOVED_LINE = _COMMENT_LINE + _PREAMBLE.count("\n")

# An exact copy of the three lines above the comment, the commented line, and
# the two below it. Both copies then have identical context, which is precisely
# the case rung 2 must refuse to choose between.
_DUPLICATE = (
    "\n"
    "    def refresh(self, user):\n"
    "        record = self.store.get(user)\n"
    "        if record is None:\n"
    '            return {"status": "invalid"}\n'
    '        return {"status": "ok", "user": user}\n'
)


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


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


class Review:
    """A real repository, a real store, and the session that spans them."""

    def __init__(self, store: ReviewStore, repo_root: Path, store_root: Path) -> None:
        self.store = store
        self.repo_root = repo_root
        self.store_root = store_root
        self.session = self._open()
        self.revision = self._snapshot()

    def _range(self) -> Any:
        return resolve_rev_range(self.repo_root, None, working_tree=True)

    def _build(self) -> Any:
        return build_review_packet_with_blobs(
            self.repo_root,
            self._range(),
            store_root=self.store_root,
            with_patch_text=True,
        )

    def _open(self) -> Any:
        return open_or_create_session(self.store, self.repo_root, self._range(), title="annotations")

    def _snapshot(self) -> Any:
        self.build = self._build()
        return snapshot_revision(
            self.store,
            self.session,
            self.repo_root,
            self._range(),
            store_root=self.store_root,
            build=self.build,
        )

    def text(self, rel: str = "src/session.py") -> str:
        return (self.repo_root / rel).read_text(encoding="utf-8")

    def comment(self, line: int, body: str = "look at this", **kwargs: Any) -> Any:
        text = kwargs.pop("new_text", self.build.blobs.new.get("src/session.py"))
        return annotate(
            self.store,
            self.session,
            self.revision,
            path="src/session.py",
            start_line=line,
            end_line=kwargs.pop("end_line", line),
            body=body,
            new_text=text,
            **kwargs,
        )

    def rewrite(self, text: str) -> Any:
        _write(self.repo_root, "src/session.py", text)
        return refresh(self.store, self.session, self.repo_root, store_root=self.store_root)


@pytest.fixture
def review(tmp_path: Path) -> Review:
    repo_root = tmp_path / "repo"
    repo = _init_repo(repo_root)
    _write(repo_root, "src/session.py", _BASE_SOURCE)
    _commit(repo, "base", 0)
    _write(repo_root, "src/session.py", _CHANGED_SOURCE)
    return Review(ReviewStore(tmp_path / "store"), repo_root, tmp_path / "store")


@pytest.fixture
def client(review: Review) -> TestClient:
    app = FastAPI()
    register_review_api(
        app,
        review.store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=review.repo_root,
        port=PORT,
    )
    return TestClient(app, base_url=BASE)


def _post(client: TestClient, path: str, payload: dict[str, Any]) -> Any:
    return client.post(path, json=payload, headers={"Authorization": f"Bearer {TOKEN}"})


def _patch(client: TestClient, path: str, payload: dict[str, Any]) -> Any:
    return client.patch(path, json=payload, headers={"Authorization": f"Bearer {TOKEN}"})


def _get(client: TestClient, path: str) -> Any:
    return client.get(path, headers={"Authorization": f"Bearer {TOKEN}"})


def _anchor_methods(store: ReviewStore, annotation_id: str) -> list[str]:
    """Every rung recorded for one annotation, oldest first.

    Read straight out of the append-only table rather than through a helper,
    because the claim under test is that the *row* exists -- an accessor could
    be right about a history nobody wrote down.
    """

    conn = sqlite3.connect(store.db_path)
    try:
        rows = conn.execute(
            "SELECT method FROM annotation_anchor_events WHERE annotation_id = ? ORDER BY created_at, id",
            (annotation_id,),
        ).fetchall()
    finally:
        conn.close()
    return [str(row[0]) for row in rows]


# --------------------------------------------------------------------------- #
# 1. the anchor is captured, not supplied
# --------------------------------------------------------------------------- #


def test_a_selected_range_becomes_a_full_anchor(review: Review) -> None:
    """Spec test 1: hashes, both contexts and the owning definition, all server-side."""

    annotation = review.comment(_COMMENT_LINE)
    anchor = annotation.anchor

    assert anchor.selected_text_hash != ""
    assert anchor.before_context_hash != ""
    assert anchor.after_context_hash != ""
    assert anchor.blob_sha != ""
    assert anchor.selected_text.strip() == "if record is None:"
    # Qualified, not bare: a second class in this file with its own ``refresh``
    # would otherwise produce two anchors, two units and two rows all reading
    # ``src/session.py::refresh``.
    assert anchor.symbol_qualified_name == "SessionManager.refresh"
    assert anchor.symbol_fingerprint != ""
    assert anchor.unit_key != ""
    assert annotation.anchor_method == "identical_blob"


def test_a_range_spanning_a_symbol_boundary_names_no_symbol_and_still_resolves(review: Review) -> None:
    """Spec test 2. No symbol is a real answer, not a lookup that failed.

    Lines 1..5 run from the module docstring into the body of ``__init__``: no
    single definition contains the selection, so the anchor carries no symbol
    and leans on rung 1, which needs none.
    """

    annotation = review.comment(1, end_line=5)
    assert annotation.anchor.symbol_qualified_name == ""

    resolution = resolve_anchor(annotation.anchor, new_text=review.text())
    assert (resolution.method, resolution.status) == ("identical_blob", "unchanged")


def test_the_owning_symbol_is_the_tightest_one_not_the_first(review: Review) -> None:
    """``refresh`` owns the line, not the class that also contains it.

    Recording the class would hand rung 3 a body big enough to hold the selected
    text twice, turning a locatable comment into an orphan on the next revision.
    """

    units = review.store.list_units(review.revision.id)
    symbols = {unit.symbol for unit in units if unit.kind == "symbol"}
    assert {"SessionManager.refresh", "SessionManager"} <= symbols

    owner = owning_symbol_unit(units, "src/session.py", _COMMENT_LINE, _COMMENT_LINE)
    assert owner is not None
    assert owner.symbol == "SessionManager.refresh"


def test_a_line_past_the_end_of_the_file_is_refused(review: Review) -> None:
    """Never a comment silently pinned to the last line it could reach."""

    with pytest.raises(ValueError, match="past the end of the file"):
        review.comment(9_000)


def test_a_path_outside_the_review_is_refused(review: Review) -> None:
    with pytest.raises(ValueError, match="not a file in revision"):
        annotate(
            review.store,
            review.session,
            review.revision,
            path="src/never.py",
            start_line=1,
            end_line=1,
            body="x",
            new_text="x\n",
        )


def test_a_comment_with_no_readable_text_is_kept_and_never_orphaned(review: Review) -> None:
    """A file we could not read is a reason we did not look, not a lost comment.

    The words are the valuable part; refusing to store them because the bytes
    around them could not be hashed would lose the review. The anchor says so,
    and the ladder keeps saying ``unresolved`` rather than promoting the silence
    to an orphan.
    """

    annotation = review.comment(_COMMENT_LINE, new_text=None)
    assert annotation.anchor_method == "unresolved"
    assert annotation.anchor.selected_text == ""
    assert annotation.anchor.start_line == _COMMENT_LINE

    resolution = resolve_anchor(annotation.anchor, new_text=review.text())
    assert resolution.status == "unresolved"
    assert resolution.method != "orphaned"
    assert "never anchored" in resolution.detail


# --------------------------------------------------------------------------- #
# 2. the four kinds, and no fifth
# --------------------------------------------------------------------------- #


def test_all_four_kinds_round_trip(review: Review) -> None:
    for index, kind in enumerate(ANNOTATION_KINDS):
        annotation = review.comment(_COMMENT_LINE, body=f"note {index}", kind=kind)
        assert annotation.kind == kind
        assert review.store.get_annotation(annotation.id) is not None


def test_non_human_annotations_never_inflate_outstanding_comment_counts(review: Review) -> None:
    human = review.comment(_COMMENT_LINE, body="human judgment")
    system = annotate(
        review.store,
        review.session,
        review.revision,
        path="src/session.py",
        start_line=0,
        end_line=0,
        body="7 known callers",
        created_by="lemoncrow",
        created_by_actor="unknown",
        source="lemoncrow",
        source_id="attention:test",
        new_text=review.text(),
    )
    assert system.source == "lemoncrow"
    assert annotation_counts([human, system]) == {"open": 1}


def test_a_fifth_kind_is_refused_by_the_model_and_by_the_api(review: Review, client: TestClient) -> None:
    """Spec test 3's negative half. Plan SS5.5 caps the set at four."""

    with pytest.raises(ValueError, match="unknown annotation kind"):
        coerce_annotation_kind("blocker")

    response = _post(
        client,
        f"/api/reviews/{review.session.id}/annotations",
        {"path": "src/session.py", "start_line": _COMMENT_LINE, "body": "no", "kind": "blocker"},
    )
    assert response.status_code == 422
    assert "blocker" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# 3. threads
# --------------------------------------------------------------------------- #


def test_a_reply_resolves_its_parent_and_shares_its_position(review: Review) -> None:
    parent = review.comment(_COMMENT_LINE, body="why is this invalid?")
    reply = review.comment(_COMMENT_LINE, body="because the store lost it", parent_id=parent.id)

    assert reply.parent_id == parent.id
    assert reply.anchor.start_line == parent.anchor.start_line


def test_a_reply_to_a_comment_in_another_review_is_refused(review: Review, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="to reply to"):
        review.comment(_COMMENT_LINE, parent_id="ann-does-not-exist")


def test_deleting_a_parent_takes_its_thread_with_it(review: Review) -> None:
    """Spec test 4. A reply that outlives the comment it answers is an orphan
    nobody asked for, and one whose anchor is still perfectly valid -- which is
    worse, because it looks trustworthy."""

    parent = review.comment(_COMMENT_LINE, body="root")
    child = review.comment(_COMMENT_LINE, body="reply", parent_id=parent.id)
    grandchild = review.comment(_COMMENT_LINE, body="reply to the reply", parent_id=child.id)
    bystander = review.comment(_COMMENT_LINE, body="unrelated")

    assert review.store.delete_annotation(parent.id) == 3
    assert review.store.get_annotation(parent.id) is None
    assert review.store.get_annotation(child.id) is None
    assert review.store.get_annotation(grandchild.id) is None
    assert review.store.get_annotation(bystander.id) is not None


def test_a_deleted_body_leaves_the_search_index(review: Review) -> None:
    """The contentless FTS table has to be un-indexed by hand; prove it happened."""

    annotation = review.comment(_COMMENT_LINE, body="phlogiston")
    assert review.store.search_annotations(review.session.id, "phlogiston")
    review.store.delete_annotation(annotation.id)
    assert review.store.search_annotations(review.session.id, "phlogiston") == ()


# --------------------------------------------------------------------------- #
# 4. search
# --------------------------------------------------------------------------- #


def test_search_finds_a_body_and_a_prefix(review: Review) -> None:
    """Spec test 6."""

    annotation = review.comment(_COMMENT_LINE, body="the invalid branch swallows a real failure")
    review.comment(11, body="unrelated wording entirely")

    found = review.store.search_annotations(review.session.id, "swallows")
    assert [item.id for item in found] == [annotation.id]
    assert [item.id for item in review.store.search_annotations(review.session.id, "swall")] == [annotation.id]


# --------------------------------------------------------------------------- #
# 5. relocation and orphaning across a revision
# --------------------------------------------------------------------------- #


def test_a_comment_follows_its_line_when_the_line_moves(review: Review) -> None:
    """The whole point. Text unchanged, position changed, comment carried."""

    annotation = review.comment(_COMMENT_LINE)
    result = review.rewrite(_PREAMBLE + _CHANGED_SOURCE)

    moved = review.store.get_annotation(annotation.id)
    assert moved is not None
    assert moved.state == "open"
    assert moved.anchor.start_line == _MOVED_LINE
    assert moved.anchor_method == "exact_text_context"

    (move,) = [item for item in result.moves if item.annotation_id == annotation.id]
    assert (move.from_line, move.to_line, move.status) == (_COMMENT_LINE, _MOVED_LINE, "relocated")


def test_every_relocation_leaves_an_audit_row(review: Review) -> None:
    """A move nobody can explain afterwards is a move nobody can check."""

    annotation = review.comment(_COMMENT_LINE)
    review.rewrite(_PREAMBLE + _CHANGED_SOURCE)

    methods = _anchor_methods(review.store, annotation.id)
    # Creation is itself an anchoring: the history starts where the comment did.
    assert methods[0] == "identical_blob"
    assert methods[-1] == "exact_text_context"


def test_two_identical_locations_orphan_rather_than_guess(review: Review) -> None:
    """The hard rule (spec SS5.1): if more than one location fits, do not guess.

    The duplicated block gives the selected text two homes with byte-identical
    context. A tool that picked one would be right half the time and unfalsifiable
    the rest, which is worse than admitting it does not know.
    """

    annotation = review.comment(_COMMENT_LINE)
    review.rewrite(_PREAMBLE + _CHANGED_SOURCE)
    result = review.rewrite(_PREAMBLE + _CHANGED_SOURCE + _DUPLICATE)

    orphan = review.store.get_annotation(annotation.id)
    assert orphan is not None
    assert orphan.state == "orphaned"
    assert orphan.anchor_method == "orphaned"
    assert "2 candidate locations" in orphan.anchor_detail
    # The last known line is kept for display, and claims nothing.
    assert orphan.anchor.start_line == _MOVED_LINE

    (move,) = [item for item in result.moves if item.annotation_id == annotation.id]
    assert move.status == "orphaned"
    assert annotation.id in result.frontier.orphaned_annotation_ids


def test_an_orphan_that_is_found_again_reopens(review: Review) -> None:
    """An objection buried in the ignore list is an objection that gets shipped."""

    annotation = review.comment(_COMMENT_LINE)
    review.rewrite(_PREAMBLE + _CHANGED_SOURCE + _DUPLICATE)
    assert (review.store.get_annotation(annotation.id) or annotation).state == "orphaned"

    review.rewrite(_PREAMBLE + _CHANGED_SOURCE)
    found = review.store.get_annotation(annotation.id)
    assert found is not None
    assert found.state == "open"
    assert found.anchor.start_line == _MOVED_LINE


# --------------------------------------------------------------------------- #
# 6. the feedback bundle
# --------------------------------------------------------------------------- #


def test_the_export_is_byte_stable_across_two_runs(review: Review) -> None:
    """Spec test 5. A bundle that changes when nothing changed cannot be diffed."""

    review.comment(11, body="second", kind="request_change")
    review.comment(_COMMENT_LINE, body="first")
    units = review.store.list_units(review.revision.id)

    def render() -> str:
        bundle = build_bundle(
            review.session,
            review.revision,
            review.store.list_annotations(review.session.id),
            units,
            context=("impacted caller: jobs/cleanup.py:L82", "focused tests: NOT_RUN"),
        )
        return render_markdown(bundle)

    assert render() == render()


def test_the_export_reads_like_the_plan_and_names_its_orphans(review: Review) -> None:
    review.comment(_COMMENT_LINE, body="this hides an invalid session", kind="request_change")
    lost = review.comment(11, body="and this one will get lost")
    review.store.update_annotation(lost.id, state="orphaned", anchor_detail="2 candidate locations")

    bundle = build_bundle(
        review.session,
        review.revision,
        review.store.list_annotations(review.session.id),
        review.store.list_units(review.revision.id),
        context=("impacted caller: jobs/cleanup.py:L82",),
    )
    text = render_markdown(bundle)

    assert text.startswith("## Review feedback")
    assert "### src/session.py:L10 — request change" in text
    assert "this hides an invalid session" in text
    assert "### Comments that lost their anchor" in text
    assert "2 candidate locations" in text
    assert "### Review context" in text
    assert "Human review REQUIRED" in text
    # An orphan is never rendered as a locatable comment.
    assert "### src/session.py:L11 — comment" not in text


def test_replies_render_under_their_parent_not_as_their_own_sections(review: Review) -> None:
    parent = review.comment(_COMMENT_LINE, body="why?")
    reply = review.comment(_COMMENT_LINE, body="because the store lost it", parent_id=parent.id)
    review.comment(_COMMENT_LINE, body="and the fallback still fails", parent_id=reply.id)

    text = render_markdown(
        build_bundle(review.session, review.revision, review.store.list_annotations(review.session.id))
    )
    assert text.count("### src/session.py:L10") == 1
    assert "> because the store lost it" in text
    assert "> and the fallback still fails" in text


# --------------------------------------------------------------------------- #
# 7. the HTTP surface
# --------------------------------------------------------------------------- #


def test_the_annotation_routes_refuse_an_unauthenticated_request(review: Review, client: TestClient) -> None:
    """These routes read and write a private repository's review. No token, nothing."""

    review_id = review.session.id
    calls = [
        ("GET", f"/api/reviews/{review_id}/annotations"),
        ("POST", f"/api/reviews/{review_id}/annotations"),
        ("POST", f"/api/reviews/{review_id}/feedback/export"),
        ("PATCH", "/api/annotations/ann-whatever"),
    ]
    for method, path in calls:
        response = client.request(method, path, json={"path": "src/session.py", "start_line": 1, "body": "x"})
        assert response.status_code == 403, f"{method} {path} answered {response.status_code}"


def test_the_api_builds_the_anchor_from_a_selected_line_range(review: Review, client: TestClient) -> None:
    """The browser sends a range; everything else is computed here.

    ``@pierre/diffs`` spells a side ``additions``; the stored anchor spells it
    ``new``. Translating at the door is what keeps one viewer's vocabulary out
    of durable review state.
    """

    response = _post(
        client,
        f"/api/reviews/{review.session.id}/annotations",
        {
            "path": "src/session.py",
            "range": {"start": _COMMENT_LINE, "end": _COMMENT_LINE, "side": "additions"},
            "body": "this hides an invalid session",
            "kind": "request_change",
        },
    )
    assert response.status_code == 201
    payload = response.json()["annotation"]
    assert payload["kind"] == "request_change"
    assert payload["side"] == "new"
    assert payload["start_line"] == _COMMENT_LINE
    assert payload["symbol"] == "SessionManager.refresh"
    assert payload["anchored"] is True

    stored = review.store.get_annotation(payload["id"])
    assert stored is not None
    assert stored.anchor.selected_text_hash != ""
    assert stored.anchor.blob_sha != ""


def test_the_api_refuses_an_empty_body_and_a_missing_path(review: Review, client: TestClient) -> None:
    url = f"/api/reviews/{review.session.id}/annotations"
    assert _post(client, url, {"path": "src/session.py", "start_line": 1, "body": "   "}).status_code == 400
    assert _post(client, url, {"start_line": 1, "body": "x"}).status_code == 400
    assert _post(client, url, {"path": "src/session.py", "body": "x"}).status_code == 400


def test_the_api_refuses_a_path_that_is_not_in_the_review(review: Review, client: TestClient) -> None:
    response = _post(
        client,
        f"/api/reviews/{review.session.id}/annotations",
        {"path": "src/untouched.py", "start_line": 1, "body": "x"},
    )
    assert response.status_code == 422
    assert "not a file in revision" in response.json()["detail"]


def test_the_listing_carries_the_reason_each_anchor_is_where_it_is(review: Review, client: TestClient) -> None:
    annotation = review.comment(_COMMENT_LINE)
    body = _get(client, f"/api/reviews/{review.session.id}/annotations").json()

    (row,) = [item for item in body["annotations"] if item["id"] == annotation.id]
    assert row["anchor_method"] == "identical_blob"
    assert row["anchor_detail"] != ""
    assert body["counts"] == {"open": 1}


def test_patch_edits_the_text_and_the_state_but_never_the_position(review: Review, client: TestClient) -> None:
    annotation = review.comment(_COMMENT_LINE, body="first draft")

    edited = _patch(client, f"/api/annotations/{annotation.id}", {"body": "second draft", "kind": "suggestion"})
    assert edited.status_code == 200
    assert edited.json()["annotation"]["body"] == "second draft"
    assert edited.json()["annotation"]["kind"] == "suggestion"

    resolved = _patch(client, f"/api/annotations/{annotation.id}", {"state": "resolved"})
    assert resolved.json()["annotation"]["state"] == "resolved"

    # There is no route that moves an anchor, and the position survives editing.
    stored = review.store.get_annotation(annotation.id)
    assert stored is not None
    assert stored.anchor.start_line == _COMMENT_LINE
    assert _patch(client, f"/api/annotations/{annotation.id}", {"start_line": 3}).status_code == 400


def test_patch_refuses_an_unknown_comment_and_an_unknown_state(review: Review, client: TestClient) -> None:
    annotation = review.comment(_COMMENT_LINE)
    assert _patch(client, "/api/annotations/ann-nope", {"body": "x"}).status_code == 404
    assert _patch(client, f"/api/annotations/{annotation.id}", {"state": "cancelled"}).status_code == 422


def test_the_export_route_renders_and_delivers_nothing(review: Review, client: TestClient) -> None:
    review.comment(_COMMENT_LINE, body="this hides an invalid session", kind="request_change")
    response = _post(client, f"/api/reviews/{review.session.id}/feedback/export", {})

    assert response.status_code == 200
    payload = response.json()
    assert payload["open"] == 1
    assert payload["orphaned"] == 0
    assert "## Review feedback" in payload["markdown"]
    assert "src/session.py:L10" in payload["markdown"]
    # Nothing was handed anywhere: no delivery row exists.
    annotations = review.store.list_annotations(review.session.id)
    assert all(review.store.list_deliveries(item.id) == () for item in annotations)


# --------------------------------------------------------------------------- #
# 8. the terminal spelling of a selection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("src/app.py:L10", ("src/app.py", 10, 10)),
        ("src/app.py:L10-L20", ("src/app.py", 10, 20)),
        ("src/app.py:L10-20", ("src/app.py", 10, 20)),
        ("a/b/c.ts:L1", ("a/b/c.ts", 1, 1)),
    ],
)
def test_parse_line_target_accepts_the_spellings_a_reviewer_types(target: str, expected: tuple[str, int, int]) -> None:
    assert parse_line_target(target) == expected


@pytest.mark.parametrize("target", ["src/app.py", "src/app.py:10", "src/app.py:L20-L10", "", "L10"])
def test_parse_line_target_refuses_anything_it_would_have_to_guess_at(target: str) -> None:
    """A bare path is refused rather than defaulted to line 1: a comment silently
    attached to the top of a file is a comment about something nobody read."""

    with pytest.raises(ValueError, match=r"line target|before it starts"):
        parse_line_target(target)


def test_annotation_counts_name_every_state_present(review: Review) -> None:
    first = review.comment(_COMMENT_LINE, body="one")
    review.comment(11, body="two")
    review.store.update_annotation(first.id, state="resolved")

    counts = annotation_counts(review.store.list_annotations(review.session.id))
    assert counts == {"open": 1, "resolved": 1}


def test_nonhuman_annotations_do_not_become_feedback_or_outstanding_human_comments(review: Review) -> None:
    human = review.comment(_COMMENT_LINE, body="please change this", kind="request_change")
    author = review.comment(
        _COMMENT_LINE,
        body="I changed this for background refreshes",
        created_by="claude",
        created_by_actor="agent",
        source="author",
        source_id="sess-1",
        title="Author rationale",
    )
    ai = review.comment(
        _COMMENT_LINE,
        body="possible stale cache",
        created_by="correctness",
        created_by_actor="agent",
        source="ai_review",
        source_id="correctness",
        title="Possible stale-cache behavior",
        confidence=0.72,
    )

    bundle = build_bundle(review.session, review.revision, review.store.list_annotations(review.session.id))
    assert [item.annotation_id for item in bundle.items] == [human.id]
    assert author.id not in {item.annotation_id for item in bundle.items}
    assert ai.id not in {item.annotation_id for item in bundle.items}

    from lemoncrow.pro.capabilities.review.sources.local import set_review_status

    closure = set_review_status(review.store, review.session, review.revision)
    assert closure.open_comments == 1
