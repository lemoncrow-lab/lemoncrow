"""``insert_relationship_if_absent``: of several writers of one relationship key, exactly one gets its row (P9-1)."""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

from lemoncrow.pro.capabilities.review.session_models import ReviewRelationship, ReviewSession
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_session_id


def _store(root: Path) -> tuple[ReviewStore, str]:
    store = ReviewStore(root)
    store.init()
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="local_change",
            repo_root="/repo/alpha",
            range_mode="working_tree",
            title="t",
            source_ref="",
        )
    )
    return store, session.id


def _claim(review_id: str, who: str) -> ReviewRelationship:
    return ReviewRelationship(
        id="",
        review_id=review_id,
        kind="team_presence",
        relation="claim",
        external_id="claim",
        status="taken",
        source="lemoncrow",
        metadata_json=f'{{"by_id": "{who}"}}',
    )


def test_the_second_writer_of_a_key_gets_nothing_and_leaves_the_first_row_alone(tmp_path: Path) -> None:
    store, review = _store(tmp_path)
    first = store.insert_relationship_if_absent(_claim(review, "usr_sam"))
    assert first is not None and first.status == "taken"
    assert store.insert_relationship_if_absent(_claim(review, "usr_maya")) is None
    [row] = store.list_relationships(review, kind="team_presence")
    assert row.id == first.id and '"usr_sam"' in row.metadata_json
    # A different key is a different row.
    other = store.insert_relationship_if_absent(replace(_claim(review, "usr_maya"), relation="handoff"))
    assert other is not None and len(store.list_relationships(review, kind="team_presence")) == 2


def test_of_several_threads_exactly_one_inserts(tmp_path: Path) -> None:
    store, review = _store(tmp_path)
    results: list[ReviewRelationship | None] = []
    gate = threading.Barrier(6)

    def attempt(who: str) -> None:
        gate.wait()
        results.append(store.insert_relationship_if_absent(_claim(review, who)))

    threads = [threading.Thread(target=attempt, args=(f"usr_{n}",)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sum(item is not None for item in results) == 1 and len(results) == 6
