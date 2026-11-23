"""Home and queue (P5-1) over the local server: snooze, Undo and the saved place, through the real routes and rows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from test_local_review_inbox_views import REPO, _Local, _ok, _resnapshot, _row, _run
from test_local_review_inbox_views import _review as _inbox_review


def _review(store: Any, title: str, **kwargs: Any) -> Any:
    # The route resolves a Review through its repository's own scope: local:<repo id>.
    return _inbox_review(store, title, repo_root=f"local:{REPO}", **kwargs)


def _in(hours: float) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _queue(path: str) -> str:
    return f"/api/reviews/{path}/queue-state"


async def _put(local: _Local, review_id: str, document: dict[str, Any], status: int = 200) -> Any:
    response = await local.send("PUT", _queue(review_id), document)
    if status == 200:
        return await _ok(response)
    assert response.status == status, await response.text()
    return await response.json()


def test_snooze_then_undo_round_trips_through_the_route_and_the_inbox_row(tmp_path: Path) -> None:
    async def body(local: _Local) -> None:
        review_id, _ = _review(local.store(), "billing")
        until = _in(2)
        assert (await _put(local, review_id, {"snoozed_until": until}))["snoozed_until"] == until
        row = _row(await local.inbox(), review_id)
        assert row["queue"] == {"snoozed_until": until, "resume": None}
        assert (await _ok(await local.get(_queue(review_id))))["snoozed_until"] == until
        assert (await _put(local, review_id, {"snoozed_until": None})) == {"snoozed_until": "", "resume": None}
        assert _row(await local.inbox(), review_id)["queue"] == {"snoozed_until": "", "resume": None}

    _run(tmp_path, body)


def test_a_newer_snapshot_ends_the_snooze_in_the_row(tmp_path: Path) -> None:
    async def body(local: _Local) -> None:
        store = local.store()
        review_id, _ = _review(store, "payments", units=2)
        await _put(local, review_id, {"snoozed_until": _in(48)})
        assert _row(await local.inbox(), review_id)["queue"]["snoozed_until"] != ""
        _resnapshot(store, review_id, "payments", changed=1, units=2)
        assert _row(await local.inbox(), review_id)["queue"]["snoozed_until"] == ""

    _run(tmp_path, body)


def test_the_saved_place_comes_back_on_the_row_and_survives_unsnoozing(tmp_path: Path) -> None:
    async def body(local: _Local) -> None:
        review_id, _ = _review(local.store(), "shop", units=2)
        place = {"screen": "one", "part": "can_view_order", "position": 2, "total": 2}
        await _put(local, review_id, {"resume": place})
        await _put(local, review_id, {"snoozed_until": _in(1)})
        await _put(local, review_id, {"snoozed_until": None})
        assert _row(await local.inbox(), review_id)["queue"] == {"snoozed_until": "", "resume": place}
        await _put(local, review_id, {"resume": None})
        assert _row(await local.inbox(), review_id)["queue"]["resume"] is None

    _run(tmp_path, body)


def test_the_route_refuses_what_the_rules_refuse(tmp_path: Path) -> None:
    async def body(local: _Local) -> None:
        store = local.store()
        open_id, _ = _review(store, "open")
        done_id, _ = _review(store, "done", status="finished")
        await _put(local, open_id, {"snoozed_until": _in(-1)}, 422)
        await _put(local, open_id, {"snoozed_until": _in(24 * 40)}, 422)
        await _put(local, open_id, {"resume": {"screen": "overview"}}, 422)
        await _put(local, open_id, {"snooze": _in(1)}, 422)
        await _put(local, open_id, {}, 422)
        # Only an open review is snoozed; a finished one can still forget its place.
        await _put(local, done_id, {"snoozed_until": _in(1)}, 409)
        await _put(local, done_id, {"resume": None})

    _run(tmp_path, body)


def test_a_row_carries_how_many_notes_are_not_sent(tmp_path: Path) -> None:
    async def body(local: _Local) -> None:
        from lemoncrow.pro.capabilities.review.session_models import Annotation, AnnotationAnchor

        store = local.store()
        review_id, revision_id = _review(store, "notes")
        assert _row(await local.inbox(), review_id)["unsent_count"] == 0
        store.add_annotation(
            Annotation(
                id="",
                review_id=review_id,
                revision_id=revision_id,
                anchor=AnnotationAnchor(path="notes-0.py", side="new", start_line=1, end_line=1),
                body="Guests should not see this.",
            )
        )
        assert _row(await local.inbox(), review_id)["unsent_count"] == 1

    _run(tmp_path, body)


def test_a_row_counts_decisions_and_quick_checks_for_the_time_range(tmp_path: Path) -> None:
    async def body(local: _Local) -> None:
        review_id, _ = _review(local.store(), "counts", units=3)
        progress = _row(await local.inbox(), review_id)["progress"]
        assert progress["target_count"] == 3
        assert progress["decision"] + progress["quick_check"] + progress["accepted_by_rule"] == 3

    _run(tmp_path, body)


def _review_at(store: Any, paths: list[str]) -> str:
    from lemoncrow.pro.capabilities.review.models import SCHEMA_VERSION
    from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession, ReviewUnit

    session = store.create_session(
        ReviewSession(
            id="", subject_type="local_change", repo_root=f"local:{REPO}", range_mode="working_tree", title="auth"
        )
    )
    revision = store.add_revision(
        ReviewRevision(
            id="",
            review_id=session.id,
            revision_number=0,
            range_mode="working_tree",
            tree_fingerprint="tree-auth",
            packet_schema_version=SCHEMA_VERSION,
        ),
        [
            ReviewUnit(
                revision_id="",
                unit_key=f"file:{path}",
                kind="file",
                path=path,
                content_fingerprint=f"fp-{path}",
                fingerprint_method="blob_sha256",
            )
            for path in paths
        ],
    )
    store.set_current_revision(session.id, revision.id)
    return str(session.id)


def test_home_overview_and_brief_count_the_same_decisions(tmp_path: Path) -> None:
    # A sensitive path is a decision only once the Reader's plan and policy inputs are applied: Home must count it
    # from the same targets the Overview and the Brief read (``/targets``), not from the units alone.
    async def body(local: _Local) -> None:
        review_id = _review_at(local.store(), ["auth/login.py", "docs/readme.py", "util/strings.py"])
        reader = await _ok(await local.get(f"/api/reviews/{review_id}/targets"))
        categories = [target["answer"]["category"] for target in reader["targets"]]
        decisions = categories.count("decision")
        quick_checks = categories.count("quick_check")
        assert decisions >= 1, categories
        progress = _row(await local.inbox(), review_id)["progress"]
        assert (progress["decision"], progress["quick_check"]) == (decisions, quick_checks)
        assert (progress["decision_open"], progress["quick_check_open"]) == (decisions, quick_checks)
        assert (reader["progress"]["decision"], reader["progress"]["quick_check"]) == (decisions, quick_checks)

    _run(tmp_path, body)


def test_open_counts_drop_as_parts_are_answered_while_totals_stay(tmp_path: Path) -> None:
    from test_local_review_inbox_views import _review_everything

    async def body(local: _Local) -> None:
        store = local.store()
        review_id, revision_id = _review(store, "answered", units=3)
        before = _row(await local.inbox(), review_id)["progress"]
        assert before["decision_open"] == before["decision"] and before["quick_check_open"] == before["quick_check"]
        assert before["decision_open"] + before["quick_check_open"] > 0
        _review_everything(store, review_id, revision_id)
        after = _row(await local.inbox(), review_id)["progress"]
        assert (after["decision_open"], after["quick_check_open"]) == (0, 0)
        assert (after["decision"], after["quick_check"]) == (before["decision"], before["quick_check"])

    _run(tmp_path, body)
