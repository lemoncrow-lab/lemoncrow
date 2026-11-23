"""P9-6: a request's repeated reads are answered once, and never from another request or after a write."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import request_reads
from lemoncrow.pro.capabilities.review.models import SCHEMA_VERSION
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession, ReviewUnit
from lemoncrow.pro.capabilities.review.store import DerivedResult, ReviewStore
from tests.gateway.test_review_api import draft_workspace, workspace  # noqa: F401  (a real store and repository)


def _store(tmp_path: Path) -> ReviewStore:
    return ReviewStore(tmp_path / "store")


def _session(store: ReviewStore) -> str:
    return store.create_session(
        ReviewSession(id="", subject_type="local_change", repo_root="/srv/x", range_mode="working_tree", title="t")
    ).id


def _revision(store: ReviewStore, review_id: str, tag: str) -> str:
    revision = store.add_revision(
        ReviewRevision(
            id="",
            review_id=review_id,
            revision_number=0,
            range_mode="working_tree",
            tree_fingerprint=f"tree-{tag}",
            packet_schema_version=SCHEMA_VERSION,
        ),
        [ReviewUnit(revision_id="", unit_key=f"file:{tag}.py", kind="file", path=f"{tag}.py", content_fingerprint=tag)],
    )
    return revision.id


class _Counting:
    """A store stand-in whose epoch the test moves by hand."""

    def __init__(self) -> None:
        self.write_epoch = 0
        self.reads = 0

    def read(self, value: str = "a") -> str:
        self.reads += 1
        return f"{value}{self.reads}"


def test_outside_a_request_every_question_is_read_again() -> None:
    fake = _Counting()
    assert request_reads.facts_once(fake, ("k",), fake.read) == "a1"
    assert request_reads.facts_once(fake, ("k",), fake.read) == "a2"
    assert request_reads.read_once(("k",), fake.read) == "a3"
    assert request_reads.read_once(("k",), fake.read) == "a4"


def test_inside_a_request_a_question_is_read_once_until_the_store_writes() -> None:
    fake = _Counting()
    with request_reads.reads_once():
        assert request_reads.facts_once(fake, ("k",), fake.read) == "a1"
        assert request_reads.facts_once(fake, ("k",), fake.read) == "a1"
        fake.write_epoch += 1  # a write in this very request
        assert request_reads.facts_once(fake, ("k",), fake.read) == "a2"
        assert request_reads.facts_once(fake, ("k",), fake.read) == "a2"
        assert request_reads.facts_once(fake, ("other",), fake.read) == "a3"


def test_a_write_that_lands_during_a_read_leaves_that_answer_out_of_date() -> None:
    fake = _Counting()

    def racing() -> str:
        fake.write_epoch += 1
        return fake.read()

    with request_reads.reads_once():
        assert request_reads.facts_once(fake, ("k",), racing) == "a1"
        assert request_reads.facts_once(fake, ("k",), fake.read) == "a2"  # not served: the epoch moved mid-read


def test_a_store_without_a_write_epoch_is_never_memoized() -> None:
    reads: list[int] = []
    with request_reads.reads_once():
        for _ in range(3):
            request_reads.facts_once(object(), ("k",), lambda: reads.append(1))
    assert len(reads) == 3


def test_each_caller_gets_its_own_copy_when_asked() -> None:
    fake = _Counting()
    with request_reads.reads_once():
        first = request_reads.facts_once(fake, ("k",), lambda: {"a": [1]}, copy=lambda value: {"a": list(value["a"])})
        first["a"].append(2)
        assert request_reads.facts_once(fake, ("k",), lambda: {"a": [9]}) == {"a": [1]}


def test_two_requests_never_share_an_answer_even_on_the_same_store_at_the_same_time() -> None:
    fake = _Counting()
    inside = threading.Barrier(2, timeout=10)
    seen: dict[str, tuple[str, str]] = {}

    def request(viewer: str) -> None:
        with request_reads.reads_once():
            first = request_reads.facts_once(fake, ("policy",), lambda: f"for {viewer}")
            inside.wait()  # both requests are open on the one store before either reads again
            seen[viewer] = (first, request_reads.facts_once(fake, ("policy",), lambda: "must not be asked"))

    threads = [threading.Thread(target=request, args=(name,)) for name in ("maya", "sam")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert seen == {"maya": ("for maya", "for maya"), "sam": ("for sam", "for sam")}
    with request_reads.reads_once():  # and a later request starts empty
        assert request_reads.facts_once(fake, ("policy",), lambda: "fresh") == "fresh"


def test_the_revision_list_a_request_holds_is_fresh_after_the_request_adds_a_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    review_id = _session(store)
    _revision(store, review_id, "one")
    with request_reads.reads_once():
        assert len(store.list_revisions(review_id)) == 1
        store.list_revisions(review_id)
        _revision(store, review_id, "two")
        assert len(store.list_revisions(review_id)) == 2


def test_cache_rows_do_not_move_the_epoch_but_action_rows_and_evidence_do(tmp_path: Path) -> None:
    store = _store(tmp_path)
    review_id = _session(store)
    revision_id = _revision(store, review_id, "one")
    before = store.write_epoch
    store.put_derived_results(
        [DerivedResult(review_id=review_id, revision_id=revision_id, kind="title", scope_key="", stamp="s", payload={})]
    )
    store.touch_derived_result(revision_id, "title", "")
    assert store.write_epoch == before  # a stamped, recomputable row is not a fact a memo depends on
    created, _stamp, _payload = store.insert_derived_result(
        DerivedResult(
            review_id=review_id, revision_id=revision_id, kind="review_action", scope_key="a1", stamp="s", payload={}
        )
    )
    assert created and store.write_epoch > before  # the action ledger is one: the plan's paths read it
    after = store.write_epoch
    store.list_revisions(review_id)
    assert store.write_epoch == after  # a read never moves it


def test_a_request_that_appends_an_action_sees_the_plan_paths_question_asked_again(tmp_path: Path) -> None:
    store = _store(tmp_path)
    review_id = _session(store)
    revision_id = _revision(store, review_id, "one")
    asked: list[int] = []

    def plan() -> int:
        asked.append(1)
        return len(asked)

    with request_reads.reads_once():
        assert request_reads.facts_once(store, ("plan", revision_id), plan) == 1
        assert request_reads.facts_once(store, ("plan", revision_id), plan) == 1
        store.insert_derived_result(
            DerivedResult(
                review_id=review_id,
                revision_id=revision_id,
                kind="review_action",
                scope_key="a1",
                stamp="s",
                payload={},
            )
        )
        assert request_reads.facts_once(store, ("plan", revision_id), plan) == 2


def test_digest_listings_are_good_only_for_the_epoch_they_were_read_at() -> None:
    with request_reads.reads_once():
        scope = request_reads.digest_scope(("org", "repo"))
        assert scope is not None
        scope.add("a/b/", 3, {"a/b/x": "d1"})
        assert scope.lookup("a/b/x", 3) == (True, "d1")
        assert scope.lookup("a/b/missing", 3) == (True, None)  # listed prefix: absent means absent
        assert scope.lookup("a/b/x", 4) == (False, None)  # a write since: ask the database
        assert scope.lookup("a/c/x", 3) == (False, None)  # outside the listed prefix
        other = request_reads.digest_scope(("org", "other-repo"))
        assert other is not None and other.lookup("a/b/x", 3) == (False, None)
    assert request_reads.digest_scope(("org", "repo")) is None


@pytest.mark.parametrize("nested", [False, True])
def test_a_nested_block_joins_the_outer_request(nested: bool) -> None:
    fake = _Counting()
    with request_reads.reads_once():
        request_reads.facts_once(fake, ("k",), fake.read)
        if nested:
            with request_reads.reads_once():
                assert request_reads.facts_once(fake, ("k",), fake.read) == "a1"
        assert request_reads.facts_once(fake, ("k",), fake.read) == "a1"


# -- where the policy and trusted-base memos save work: a local Review (hosted prefetches their artifacts) -------------


def test_a_request_asks_for_the_operator_policy_once_per_snapshot_and_a_new_request_asks_again(
    workspace: Any,  # noqa: F811
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
    from tests.gateway.test_review_api import BASE, PORT, TOKEN

    asked: list[str] = []

    origin = {"layer": "built_in_default", "source": "built_in_default", "scope_id": ""}

    def setting(value: Any) -> dict[str, Any]:
        return {"effective_value": value, "origin": origin}

    def policy(_session: Any, revision: Any) -> dict[str, Any]:
        asked.append(str(revision.id))
        return {
            "behavior": {
                "enabled": setting(True),
                "triggers": {
                    name: setting(name in {"open_or_ready", "manual"})
                    for name in ("open_or_ready", "push", "rebase", "drafts", "manual")
                },
                "effort": setting("auto"),
                "precedence": [
                    "built_in_default",
                    "organization",
                    "team",
                    "repository",
                    "trusted_base_repository",
                    "directory",
                    "review_request",
                    "personal_presentation",
                    "platform_safety",
                ],
                "trusted_base_required": True,
            },
            "requirements": {
                "configured": False,
                "source": "built_in_default",
                "settings": {"required_checks": {"mode": "inherit", "effective_value": [], "origin": origin}},
                "effective": {"required_checks": []},
            },
        }

    app = FastAPI()
    register_review_api(
        app,
        ReviewStore(workspace.store_root),
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=workspace.repo_root,
        port=PORT,
        intelligence_policy=policy,
    )
    client = TestClient(app, base_url=BASE)
    client.headers.update({"Authorization": f"Bearer {TOKEN}"})
    for path in ("projection", "readiness"):
        asked.clear()
        assert client.get(f"/api/reviews/{workspace.review_id}/{path}").status_code == 200
        assert asked, path
        assert len(set(asked)) == len(asked), f"{path}: the policy was asked twice for one Snapshot: {asked}"
        before = len(asked)
        assert client.get(f"/api/reviews/{workspace.review_id}/{path}").status_code == 200
        assert len(asked) > before, "a second request must ask for itself, not reuse the first request's answer"


def test_content_access_answers_are_held_for_a_request_and_not_beyond_it() -> None:
    from lemoncrow.pro.capabilities.review.rules import content_access_for_path

    first = content_access_for_path("pkg/a.py", (), route_trust="external")
    with request_reads.reads_once():
        held = content_access_for_path("pkg/a.py", (), route_trust="external")
        assert content_access_for_path("pkg/a.py", (), route_trust="external") is held
        assert content_access_for_path("pkg/a.py", (), route_trust="customer_local") is not held
        assert content_access_for_path("pkg/b.py", (), route_trust="external") is not held
    with request_reads.reads_once():
        assert (
            content_access_for_path("pkg/a.py", (), route_trust="external") is not held
        )  # nothing survived the request
    assert held == first and held.allowed


def test_a_write_made_directly_on_an_atomic_transitions_connection_moves_the_epoch(tmp_path: Path) -> None:
    """The attention and outbox writers insert on the yielded connection, not through a store method."""

    store = _store(tmp_path)
    review_id = _session(store)
    before = store.write_epoch
    with store.atomic_review_transition() as conn:
        conn.execute("UPDATE review_sessions SET title = 'moved' WHERE id = ?", (review_id,))
    assert store.write_epoch > before
    after = store.write_epoch
    with store.atomic_review_transition() as conn:
        conn.execute("SELECT id FROM review_sessions").fetchall()
    assert store.write_epoch == after, "a read-only scope moves nothing"
    with store.cache_write(), store.atomic_review_transition() as conn:
        conn.execute("UPDATE review_sessions SET title = 'again' WHERE id = ?", (review_id,))
    assert store.write_epoch == after, "a cache write does not move it"
