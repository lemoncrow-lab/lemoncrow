"""P7-2: presence, claim and handoff routes, with a fake multi-person authority (hosted supplies the real one)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.second_reviewer import OwnerRef
from lemoncrow.pro.capabilities.review.session_models import ReviewSession
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.team_presence_api import (
    PRESENCE_KEEP_SECONDS,
    PRESENCE_TTL_SECONDS,
    TEAM_KIND,
    Away,
    RequestMove,
)

TOKEN = "team-presence-test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
SAM, MAYA, LEO = "usr_sam", "usr_maya", "usr_leo"
NAMES = {SAM: "Sam Rivera", MAYA: "Maya Chen", LEO: "Leo Park"}


class FakeTeam:
    def __init__(self) -> None:
        self.who = SAM
        self.away_of: dict[str, Away] = {}
        self.told: list[tuple[tuple[str, ...], str, bool]] = []
        self.reachable = {MAYA, LEO}
        self.is_human = True
        self.barred: set[str] = set()
        self.removed: set[str] = set()
        self.untold: list[tuple[str, ...]] = []
        self.moved: list[tuple[str, str]] = []
        self.move_fails: str | None = None
        self.restored: list[dict[str, str]] = []
        self.record_nothing = False
        self.mates = tuple(pid for pid in NAMES)

    def viewer_id(self) -> str:
        return self.who

    def person(self, session: ReviewSession, principal_id: str) -> OwnerRef:
        return OwnerRef(principal_id, "", NAMES.get(principal_id, principal_id))

    def teammates(self, session: ReviewSession) -> Sequence[OwnerRef]:
        return tuple(self.person(session, pid) for pid in self.mates if pid != self.who and pid not in self.removed)

    def human(self) -> bool:
        return self.is_human

    def may_take(self, session: ReviewSession, principal_id: str) -> bool:
        return principal_id not in self.barred

    def gone(self, session: ReviewSession, principal_id: str) -> bool:
        return principal_id in self.removed

    def untell(self, session: ReviewSession, principal_ids: Sequence[str]) -> None:
        self.untold.append(tuple(principal_ids))

    def move_request(self, session: ReviewSession, giver: str, target: str) -> RequestMove:
        self.moved.append((giver, target))
        if self.move_fails is not None:
            return RequestMove(failure=self.move_fails, code="version_conflict")
        if self.record_nothing:
            return RequestMove()  # nothing to move (or a handoff from before the move was kept)
        return RequestMove(
            record={"kind": "delegated", "giver_request": f"req-{giver}", "target_request": f"req-{target}"}
        )

    def restore_request(self, session: ReviewSession, record: Any) -> str | None:
        self.restored.append(dict(record))
        return None

    def away(self, principal_id: str) -> Away | None:
        return self.away_of.get(principal_id)

    def tell(self, session: ReviewSession, principal_ids: Sequence[str], reason: str, *, assign: bool) -> Sequence[str]:
        self.told.append((tuple(principal_ids), reason, assign))
        return tuple(pid for pid in principal_ids if pid in self.reachable or assign)


def _repo(tmp_path: Path) -> tuple[Path, ReviewStore, ReviewSession, str]:
    root = tmp_path / "repo"
    root.mkdir()
    repo = pygit2.init_repository(str(root), initial_head="main")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    repo.index.add_all()
    repo.index.write()
    sig = pygit2.Signature("T", "t@example.invalid", 1700000000, 0)
    repo.create_commit("HEAD", sig, sig, "base", repo.index.write_tree(), [])
    (root / "a.py").write_text("x = 2\n", encoding="utf-8")
    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(root, working_tree=True)
    built = build_review_packet_with_blobs(root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, root, rng, title=built.packet.title)
    first = snapshot_revision(store, session, root, rng, store_root=store_root, build=built)
    store.publish_snapshot(session.id, first.id)
    return root, store, session, first.id


TeamFixture = tuple[TestClient, str, str, FakeTeam, ReviewStore]


@pytest.fixture
def world(tmp_path: Path) -> TeamFixture:
    root, store, session, revision = _repo(tmp_path)
    team = FakeTeam()
    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=root,
        port=48080,
        acting_reviewer_id=lambda: team.who,
        team=team,
    )
    return TestClient(app, base_url="http://127.0.0.1:48080"), session.id, revision, team, store


def _get(client: TestClient, review: str, path: str = "") -> dict[str, Any]:
    response = client.get(f"/api/reviews/{review}/team{path}", headers=HEADERS)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _post(client: TestClient, review: str, path: str, body: dict[str, Any] | None = None) -> Any:
    return client.post(f"/api/reviews/{review}/team{path}", headers=HEADERS, json=body or {})


def _as(team: FakeTeam, who: str) -> None:
    team.who = who


def _row(store: ReviewStore, review: str, relation: str, key: str) -> Any:
    return next(
        r for r in store.list_relationships(review, kind=TEAM_KIND) if r.relation == relation and r.external_id == key
    )


def _age(store: ReviewStore, review: str, who: str, seconds: float) -> None:
    """Pretend this person's last beat (and start) was ``seconds`` ago."""

    row = _row(store, review, "reviewing", who)
    then = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    meta = {**json.loads(row.metadata_json), "seen_at": then, "started_at": then}
    store.upsert_relationship(replace(row, metadata_json=json.dumps(meta)))  # same natural key: an update in place


def test_a_local_review_has_no_team_route_or_field(tmp_path: Path) -> None:
    root, store, session, _revision = _repo(tmp_path)
    app = FastAPI()
    register_review_api(app, store, auth_dependency=make_token_dependency(TOKEN), repo_root=root, port=48080)
    client = TestClient(app, base_url="http://127.0.0.1:48080")
    assert client.get(f"/api/reviews/{session.id}/team", headers=HEADERS).status_code == 404
    assert client.post(f"/api/reviews/{session.id}/team/presence", headers=HEADERS, json={}).status_code == 404
    assert client.post(f"/api/reviews/{session.id}/team/claim", headers=HEADERS, json={}).status_code == 404
    assert client.post(f"/api/reviews/{session.id}/team/handoff", headers=HEADERS, json={}).status_code == 404
    assert store.list_relationships(session.id, kind=TEAM_KIND) == ()


def test_a_beat_shows_the_person_to_others_and_never_to_themselves(world: TeamFixture) -> None:
    client, review, _rev, team, _store = world
    assert _post(client, review, "/presence").json()["reviewing"] == []  # Sam alone: nobody else
    _as(team, MAYA)
    seen = _get(client, review)
    assert [(p["id"], p["name"]) for p in seen["reviewing"]] == [(SAM, "Sam Rivera")]
    assert seen["reviewing"][0]["started_at"]
    assert seen["viewer"] == MAYA
    assert seen["reviewing"][0]["sent_notes"] == 0  # nothing sent yet


def test_presence_expires_and_the_next_beat_starts_a_new_stay(world: TeamFixture) -> None:
    client, review, _rev, team, store = world
    _post(client, review, "/presence")
    _age(store, review, SAM, PRESENCE_TTL_SECONDS + 5)
    _as(team, MAYA)
    assert _get(client, review)["reviewing"] == []  # three missed beats: no longer in it
    _as(team, SAM)
    _post(client, review, "/presence")
    _as(team, MAYA)
    started = _get(client, review)["reviewing"][0]["started_at"]
    assert datetime.fromisoformat(started) > datetime.now(UTC) - timedelta(seconds=30)  # not the aged start


def test_a_beat_inside_the_window_keeps_the_start(world: TeamFixture) -> None:
    client, review, _rev, team, store = world
    _post(client, review, "/presence")
    _age(store, review, SAM, PRESENCE_TTL_SECONDS - 80)
    _post(client, review, "/presence")
    _as(team, MAYA)
    started = datetime.fromisoformat(_get(client, review)["reviewing"][0]["started_at"])
    assert started < datetime.now(UTC) - timedelta(seconds=90)


def test_the_next_write_deletes_presence_nobody_reads_any_more(world: TeamFixture) -> None:
    client, review, _rev, team, store = world
    _post(client, review, "/presence")
    _age(store, review, SAM, PRESENCE_KEEP_SECONDS + 60)
    _as(team, MAYA)
    _post(client, review, "/presence")
    assert {r.external_id for r in store.list_relationships(review, kind=TEAM_KIND)} == {MAYA}


def test_taking_it_tells_nobody_until_their_own_beat_picks_it_up(world: TeamFixture) -> None:
    client, review, _rev, team, _store = world
    _as(team, MAYA)
    _post(client, review, "/presence")
    _as(team, SAM)
    taken = _post(client, review, "/claim").json()
    assert taken["claim"]["by_me"] is True
    assert taken["claim"]["by"]["name"] == "Sam Rivera"
    assert taken["claim"]["told"] == []  # nothing has been delivered yet
    assert [p["name"] for p in taken["claim"]["waiting"]] == ["Maya Chen"]
    assert team.told == []  # no lasting attention reason is left behind by a claim
    _as(team, MAYA)
    seen = _get(client, review)  # a read is not a delivery
    assert seen["notices"] == [{"kind": "taken", "by": {"id": SAM, "name": "Sam Rivera"}, "at": seen["claim"]["at"]}]
    assert seen["claim"]["told"] == []
    beat = _post(client, review, "/presence").json()  # her beat carries the notice and records that she got it
    assert [n["kind"] for n in beat["notices"]] == ["taken"]
    assert [p["name"] for p in beat["claim"]["told"]] == ["Maya Chen"]
    assert beat["claim"]["waiting"] == []
    assert _get(client, review)["notices"] == []  # delivered once
    _as(team, SAM)
    assert [p["name"] for p in _get(client, review)["claim"]["told"]] == ["Maya Chen"]


def test_a_second_claim_is_refused_and_the_holders_repeat_is_harmless(world: TeamFixture) -> None:
    client, review, _rev, team, _store = world
    assert _post(client, review, "/claim").status_code == 200
    assert _post(client, review, "/claim").json()["claim"]["by"]["id"] == SAM
    _as(team, MAYA)
    refused = _post(client, review, "/claim")
    assert refused.status_code == 409
    assert "Sam Rivera already took this review" in refused.text


def test_a_claim_ends_when_its_holder_decides(world: TeamFixture) -> None:
    client, review, revision, _team, _store = world
    _post(client, review, "/claim")
    assert _get(client, review)["claim"] is not None
    decided = client.post(
        f"/api/reviews/{review}/outcome",
        headers=HEADERS,
        json={"outcome": "lgtm", "summary": "", "expected_revision_id": revision},
    )
    assert decided.status_code == 200, decided.text
    assert _get(client, review)["claim"] is None


def test_handing_off_names_the_teammate_keeps_the_note_and_lets_go_of_a_claim(world: TeamFixture) -> None:
    client, review, _rev, team, _store = world
    _post(client, review, "/claim")
    handed = _post(client, review, "/handoff", {"to": MAYA, "note": "Start at the verifier."}).json()
    assert handed["claim"] is None
    assert handed["handoff"]["to"] == {"id": MAYA, "name": "Maya Chen"}
    assert handed["handoff"]["note"] == "Start at the verifier."
    assert handed["handoff"]["away"] is None
    assert team.told[-1] == ((MAYA,), "Sam Rivera handed this review to you: Start at the verifier.", True)
    _as(team, MAYA)
    seen = _get(client, review)
    assert seen["handoff"]["to_me"] is True
    assert [n["kind"] for n in seen["notices"]] == ["handed_off"]
    assert seen["notices"][0]["note"] == "Start at the verifier."


def test_handing_to_someone_away_goes_to_who_they_named_until_they_are_back(world: TeamFixture) -> None:
    client, review, _rev, team, _store = world
    team.away_of[LEO] = Away((datetime.now(UTC) + timedelta(days=2)).isoformat(), MAYA)
    listed = _get(client, review, "/teammates")["teammates"]
    leo = next(p for p in listed if p["id"] == LEO)
    assert leo["away"]["reassign_to"] == {"id": MAYA, "name": "Maya Chen"}
    handed = _post(client, review, "/handoff", {"to": LEO, "note": ""}).json()
    assert handed["handoff"]["to"]["id"] == MAYA
    assert handed["handoff"]["away"]["name"] == "Leo Park"
    assert handed["handoff"]["away"]["until"]
    # The away period ended: a later handoff to Leo is not rerouted.
    team.away_of[LEO] = Away((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), MAYA)
    again = _post(client, review, "/handoff", {"to": LEO, "note": ""}).json()
    assert again["handoff"]["to"]["id"] == LEO
    assert again["handoff"]["away"] is None
    assert next(p for p in _get(client, review, "/teammates")["teammates"] if p["id"] == LEO)["away"] is None


def test_a_stand_in_who_is_not_on_the_review_gets_nothing_and_the_handoff_goes_to_the_one_asked(
    world: TeamFixture,
) -> None:
    client, review, _rev, team, _store = world
    team.away_of[LEO] = Away((datetime.now(UTC) + timedelta(days=2)).isoformat(), "usr_stranger_outside_repo")
    handed = _post(client, review, "/handoff", {"to": LEO, "note": "n"}).json()
    assert handed["handoff"]["to"]["id"] == LEO
    assert handed["handoff"]["away"]["rerouted"] is False
    assert team.told[-1][0] == (LEO,)
    listed = _get(client, review, "/teammates")["teammates"]
    assert next(p for p in listed if p["id"] == LEO)["away"]["reassign_to"] is None


def test_a_stand_in_who_was_taken_off_the_review_is_not_brought_back(world: TeamFixture) -> None:
    client, review, _rev, team, _store = world
    team.away_of[LEO] = Away((datetime.now(UTC) + timedelta(days=2)).isoformat(), MAYA)
    team.removed.add(MAYA)
    handed = _post(client, review, "/handoff", {"to": LEO, "note": ""}).json()
    assert handed["handoff"]["to"]["id"] == LEO
    assert team.told[-1][0] == (LEO,)


def test_the_handoff_note_is_for_the_two_people_it_is_between(world: TeamFixture) -> None:
    client, review, _rev, team, _store = world
    _post(client, review, "/handoff", {"to": MAYA, "note": "I approved; the guard is fine"})
    _as(team, LEO)  # a third person, e.g. a second reviewer who has not answered
    seen = _get(client, review)
    assert seen["handoff"]["to"]["id"] == MAYA
    assert seen["handoff"]["note"] == ""
    assert seen["handoff"]["by"] == {"id": "", "name": ""}
    _as(team, MAYA)
    assert _get(client, review)["handoff"]["note"] == "I approved; the guard is fine"
    _as(team, SAM)
    assert _get(client, review)["handoff"]["note"] == "I approved; the guard is fine"


def test_the_reassignment_line_ends_with_the_away_period(world: TeamFixture) -> None:
    client, review, _rev, team, store = world
    team.away_of[LEO] = Away((datetime.now(UTC) + timedelta(days=2)).isoformat(), MAYA)
    _post(client, review, "/handoff", {"to": LEO, "note": ""})
    assert _get(client, review)["handoff"]["away"] is not None
    row = _row(store, review, "handoff", "handoff")
    meta = {**json.loads(row.metadata_json), "away_until": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}
    store.upsert_relationship(replace(row, metadata_json=json.dumps(meta)))
    assert _get(client, review)["handoff"]["away"] is None
    assert _get(client, review)["handoff"]["to"]["id"] == MAYA  # the handoff itself stands


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"to": SAM, "note": ""}, 422),  # yourself
        ({"to": "usr_nobody", "note": ""}, 422),  # not on the team
        ({"to": MAYA, "note": "x" * 301}, 422),  # a note past the bound
        ({"to": MAYA, "note": "x" * 300}, 200),  # at the bound
        ({"note": ""}, 422),
    ],
)
def test_a_handoff_names_a_real_teammate_and_keeps_the_note_short(
    world: TeamFixture, body: dict[str, Any], status: int
) -> None:
    client, review, _rev, _team, _store = world
    assert _post(client, review, "/handoff", body).status_code == status


def test_nobody_hands_off_a_review_someone_else_took(world: TeamFixture) -> None:
    client, review, _rev, team, _store = world
    _as(team, MAYA)
    _post(client, review, "/claim")
    _as(team, SAM)
    refused = _post(client, review, "/handoff", {"to": LEO, "note": ""})
    assert refused.status_code == 409
    assert "Maya Chen took this review" in refused.text


def test_a_third_person_cannot_override_a_handoff(world: TeamFixture) -> None:
    client, review, _rev, team, _store = world
    _post(client, review, "/handoff", {"to": MAYA, "note": ""})
    _as(team, LEO)
    refused = _post(client, review, "/handoff", {"to": SAM, "note": ""})
    assert refused.status_code == 409
    assert "was handed to Maya Chen" in refused.text
    _as(team, MAYA)  # the one it was handed to may hand it on
    assert _post(client, review, "/handoff", {"to": LEO, "note": ""}).status_code == 200


def test_the_holder_lets_go_and_nobody_else_can(world: TeamFixture) -> None:
    client, review, _rev, team, _store = world
    _post(client, review, "/claim")
    _as(team, MAYA)
    assert _post(client, review, "/release").status_code == 409
    _as(team, SAM)
    assert _post(client, review, "/release").json()["claim"] is None
    assert _post(client, review, "/release").status_code == 409  # nothing left to let go of
    _as(team, MAYA)
    assert _post(client, review, "/claim").status_code == 200  # free again


def test_the_author_and_an_agent_cannot_take_a_review(world: TeamFixture) -> None:
    client, review, _rev, team, _store = world
    team.barred.add(SAM)
    assert _post(client, review, "/claim").status_code == 403


def test_a_claim_whose_holder_was_taken_off_the_review_is_over(world: TeamFixture) -> None:
    client, review, _rev, team, _store = world
    _post(client, review, "/claim")
    team.removed.add(SAM)
    _as(team, MAYA)
    assert _get(client, review)["claim"] is None
    assert _post(client, review, "/claim").status_code == 200


def test_a_service_token_is_not_a_person_in_the_review(world: TeamFixture) -> None:
    client, review, _rev, team, store = world
    team.is_human = False
    assert _post(client, review, "/presence").status_code == 200
    assert store.list_relationships(review, kind=TEAM_KIND) == ()  # nothing written
    assert _post(client, review, "/claim").status_code == 403
    assert _post(client, review, "/handoff", {"to": MAYA, "note": ""}).status_code == 403
    assert _post(client, review, "/release").status_code == 403


def test_two_people_taking_it_at_the_same_moment_do_not_both_get_it(tmp_path: Path) -> None:
    import threading

    from lemoncrow.pro.capabilities.review import team_presence_api as T

    _root, store, session, _rev = _repo(tmp_path)
    sam, maya = FakeTeam(), FakeTeam()
    maya.who = MAYA
    results: list[str] = []
    gate = threading.Barrier(2)

    def run(team: FakeTeam) -> None:
        deps = T.TeamDeps(store=store, authority=team, session=store.get_session, mutable_session=store.get_session)
        gate.wait()
        try:
            T.take(deps, session.id)
            results.append("ok")
        except T.TeamRefused as exc:
            results.append(str(exc.status))

    threads = [threading.Thread(target=run, args=(t,)) for t in (sam, maya)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["409", "ok"]


def test_a_claim_that_lands_between_the_holder_check_and_the_write_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two servers: Maya passes the holder check, then Sam takes the review completely, then Maya writes."""

    from lemoncrow.pro.capabilities.review import team_presence_api as T

    class NoLock:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr(T, "_CLAIM_LOCK", NoLock())
    _root, store, session, _rev = _repo(tmp_path)
    sam, maya = FakeTeam(), FakeTeam()
    maya.who = MAYA

    def deps(team: FakeTeam) -> Any:
        return T.TeamDeps(store=store, authority=team, session=store.get_session, mutable_session=store.get_session)

    real_prune, fired = T.prune, []

    def prune(st: Any, review_id: str, rows: Any, now: Any) -> int:
        if not fired:  # Maya's request is between its holder check and its write
            fired.append(1)
            T.take(deps(sam), session.id)
        return real_prune(st, review_id, rows, now)

    monkeypatch.setattr(T, "prune", prune)
    with pytest.raises(T.TeamRefused) as refused:
        T.take(deps(maya), session.id)
    assert refused.value.status == 409
    claims = [row for row in T.rows_of(store, session.id) if row.relation == T.REL_CLAIM]
    assert [(row.status, row.meta["by_id"]) for row in claims] == [("taken", SAM)]


class _NoLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> None:
        return None


def _two_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, ReviewStore, ReviewSession, Any, Any, Any]:
    """Two servers on one store (no in-process lock between them): Sam's and Maya's."""

    from lemoncrow.pro.capabilities.review import team_presence_api as T

    monkeypatch.setattr(T, "_CLAIM_LOCK", _NoLock())
    _root, store, session, _rev = _repo(tmp_path)
    sam, maya = FakeTeam(), FakeTeam()
    maya.who = MAYA

    def deps(team: FakeTeam) -> Any:
        return T.TeamDeps(store=store, authority=team, session=store.get_session, mutable_session=store.get_session)

    return T, store, session, deps(sam), deps(maya), None


def _team_state(T: Any, store: ReviewStore, review: str) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {"claim": [], "handoff": []}
    for row in T.rows_of(store, review):
        if row.relation in out:
            who = row.meta.get("by_id") if row.relation == "claim" else row.meta.get("to_id")
            out[row.relation].append((row.status, str(who)))
    return out


def test_a_handoff_that_lands_between_a_takers_reads_is_not_overwritten_by_her_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Maya's take read no handoff; Sam then hands the review to Leo; Maya must be refused, not claim over it."""

    T, store, session, sam, maya, _ = _two_servers(tmp_path, monkeypatch)
    real_prune, fired = T.prune, []

    def prune(st: Any, review_id: str, rows: Any, now: Any) -> int:
        if not fired:  # Maya's request is between its first read and its fresh read
            fired.append(1)
            T.hand_off(sam, session.id, to=LEO, note="")
        return real_prune(st, review_id, rows, now)

    monkeypatch.setattr(T, "prune", prune)
    with pytest.raises(T.TeamRefused) as refused:
        T.take(maya, session.id)
    assert refused.value.status == 409 and "handed to Leo Park" in refused.value.detail
    assert _team_state(T, store, session.id) == {"claim": [], "handoff": [("active", LEO)]}


def test_a_claim_that_lands_between_a_givers_reads_is_not_overwritten_by_the_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sam's hand-off read no claim; Maya then takes the review; Sam must be refused, not hand it on over her."""

    T, store, session, sam, maya, _ = _two_servers(tmp_path, monkeypatch)
    real_prune, fired = T.prune, []

    def prune(st: Any, review_id: str, rows: Any, now: Any) -> int:
        if not fired:  # Sam's request is between its first read and its fresh read
            fired.append(1)
            T.take(maya, session.id)
        return real_prune(st, review_id, rows, now)

    monkeypatch.setattr(T, "prune", prune)
    with pytest.raises(T.TeamRefused) as refused:
        T.hand_off(sam, session.id, to=LEO, note="")
    assert refused.value.status == 409 and "Maya Chen took this review" in refused.value.detail
    assert _team_state(T, store, session.id) == {"claim": [("taken", MAYA)], "handoff": []}


def test_a_handoff_written_after_a_takers_fresh_read_takes_her_claim_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sam checked, Maya inserted her claim, Sam wrote: Maya reads after her write, sees it and gives her claim back."""

    T, store, session, _sam, maya, _ = _two_servers(tmp_path, monkeypatch)
    real_insert, fired = store.insert_relationship_if_absent, []

    def insert(relationship: Any) -> Any:
        stored = real_insert(relationship)
        if relationship.relation == "claim" and not fired:
            fired.append(1)  # Sam's handoff row lands right after Maya's claim row
            T._write(
                store,
                session.id,
                T.REL_HANDOFF,
                T.HANDOFF_KEY,
                "active",
                {"by_id": SAM, "to_id": LEO, "to_name": "Leo Park", "at": T.utc_now()},
            )
        return stored

    monkeypatch.setattr(store, "insert_relationship_if_absent", insert)
    with pytest.raises(T.TeamRefused) as refused:
        T.take(maya, session.id)
    assert refused.value.status == 409
    assert _team_state(T, store, session.id) == {"claim": [("released", MAYA)], "handoff": [("active", LEO)]}


def test_a_claim_written_after_a_givers_fresh_read_takes_his_handoff_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Maya's claim lands after Sam's checks and before his write: Sam reads after his write, sees it and withdraws."""

    T, store, session, sam, maya, _ = _two_servers(tmp_path, monkeypatch)
    real_insert, fired = store.insert_relationship_if_absent, []

    def insert(relationship: Any) -> Any:
        if relationship.relation == "handoff" and not fired:
            fired.append(1)
            T.take(maya, session.id)
        return real_insert(relationship)

    monkeypatch.setattr(store, "insert_relationship_if_absent", insert)
    with pytest.raises(T.TeamRefused) as refused:
        T.hand_off(sam, session.id, to=LEO, note="")
    assert refused.value.status == 409
    assert _team_state(T, store, session.id) == {"claim": [("taken", MAYA)], "handoff": [("ended", LEO)]}
    assert sam.authority.told == []  # the target was never told about a handoff that did not happen


def test_a_handoff_moves_the_givers_request_to_the_one_it_went_to(world: TeamFixture) -> None:
    client, review, _rev, team, _store = world
    assert _post(client, review, "/handoff", {"to": MAYA, "note": ""}).status_code == 200
    assert team.moved == [(SAM, MAYA)]
    away = Away(until=(datetime.now(UTC) + timedelta(days=2)).isoformat(), reassign_to=MAYA)
    team.away_of[LEO] = away
    assert _post(client, review, "/handoff", {"to": LEO, "note": ""}).status_code == 200
    assert team.moved[-1] == (SAM, MAYA)  # an away teammate's stand-in is the one who is asked
    assert _post(client, review, "/handoff", {"to": SAM, "note": ""}).status_code == 422
    assert len(team.moved) == 2  # a refused handoff moves nothing


def _fail_first_compare_and_set(
    store: ReviewStore, monkeypatch: pytest.MonkeyPatch, status: str, relation: str
) -> list[int]:
    """The first compare-and-set to ``status`` on a ``relation`` row loses, as if a presence beat had written into the
    row after it was read (the row changes, with the same words, so a retry on a fresh read can win)."""

    real, fired = store.update_relationship_if_unchanged, []

    def update(expected: Any, *, status: str, metadata_json: str) -> Any:
        if expected.relation == relation and status == want and not fired:
            fired.append(1)
            real(expected, status=expected.status, metadata_json=expected.metadata_json)  # the beat
        return real(expected, status=status, metadata_json=metadata_json)

    want = status
    monkeypatch.setattr(store, "update_relationship_if_unchanged", update)
    return fired


def test_a_claim_given_back_after_a_conflict_is_given_back_even_when_a_beat_wrote_into_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    T, store, session, _sam, maya, _ = _two_servers(tmp_path, monkeypatch)
    real_insert, fired = store.insert_relationship_if_absent, []

    def insert(relationship: Any) -> Any:
        stored = real_insert(relationship)
        if relationship.relation == "claim" and not fired:
            fired.append(1)
            T._write(
                store,
                session.id,
                T.REL_HANDOFF,
                T.HANDOFF_KEY,
                "active",
                {"by_id": SAM, "to_id": LEO, "to_name": "Leo Park", "at": T.utc_now()},
            )
        return stored

    monkeypatch.setattr(store, "insert_relationship_if_absent", insert)
    lost = _fail_first_compare_and_set(store, monkeypatch, "released", "claim")
    with pytest.raises(T.TeamRefused):
        T.take(maya, session.id)
    assert lost == [1]  # the first give-back lost its compare-and-set
    assert _team_state(T, store, session.id) == {"claim": [("released", MAYA)], "handoff": [("active", LEO)]}


def test_a_handoff_withdrawn_after_a_conflict_is_withdrawn_even_when_a_beat_wrote_into_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    T, store, session, sam, maya, _ = _two_servers(tmp_path, monkeypatch)
    real_insert, fired = store.insert_relationship_if_absent, []

    def insert(relationship: Any) -> Any:
        if relationship.relation == "handoff" and not fired:
            fired.append(1)
            T.take(maya, session.id)
        return real_insert(relationship)

    monkeypatch.setattr(store, "insert_relationship_if_absent", insert)
    lost = _fail_first_compare_and_set(store, monkeypatch, "ended", "handoff")
    with pytest.raises(T.TeamRefused):
        T.hand_off(sam, session.id, to=LEO, note="")
    assert lost == [1]
    assert _team_state(T, store, session.id) == {"claim": [("taken", MAYA)], "handoff": [("ended", LEO)]}


def test_a_handoff_that_loses_a_conflict_leaves_the_handoff_that_stood_before_it_standing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sam handed it to Maya; Maya hands it on to Leo; Sam takes it back in between. Maya is refused and Sam's
    handoff to her is not ended by her failed one."""

    T, store, session, sam, maya, _ = _two_servers(tmp_path, monkeypatch)
    T.hand_off(sam, session.id, to=MAYA, note="the guard first")
    standing = next(r for r in T.rows_of(store, session.id) if r.relation == "handoff")
    real_update, fired = store.update_relationship_if_unchanged, []

    def update(expected: Any, *, status: str, metadata_json: str) -> Any:
        if expected.relation == "handoff" and status == "active" and not fired:
            fired.append(1)
            T.take(sam, session.id)  # the giver of the standing handoff takes it back, after Maya's checks
        return real_update(expected, status=status, metadata_json=metadata_json)

    monkeypatch.setattr(store, "update_relationship_if_unchanged", update)
    with pytest.raises(T.TeamRefused) as refused:
        T.hand_off(maya, session.id, to=LEO, note="")
    assert refused.value.status == 409
    now = next(r for r in T.rows_of(store, session.id) if r.relation == "handoff")
    assert (now.status, now.relationship.metadata_json) == (standing.status, standing.relationship.metadata_json)
    assert _team_state(T, store, session.id)["claim"] == [("taken", SAM)]


def test_a_withdrawal_does_not_overwrite_a_status_only_change_made_meanwhile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handoff that stood is being restored when a prune ends the row: the end stands, it is not undone."""

    T, store, session, sam, maya, _ = _two_servers(tmp_path, monkeypatch)
    T.hand_off(sam, session.id, to=MAYA, note="the guard first")
    real_update, calls = store.update_relationship_if_unchanged, []

    def update(expected: Any, *, status: str, metadata_json: str) -> Any:
        if expected.relation == "handoff":
            calls.append(status)
            if len(calls) == 1:
                T.take(sam, session.id)  # the conflict: the giver takes it back after Maya's checks
            elif len(calls) == 2:  # the withdrawal: the row is ended (words unchanged) before it gets there
                real_update(expected, status="ended", metadata_json=expected.metadata_json)
        return real_update(expected, status=status, metadata_json=metadata_json)

    monkeypatch.setattr(store, "update_relationship_if_unchanged", update)
    with pytest.raises(T.TeamRefused):
        T.hand_off(maya, session.id, to=LEO, note="")
    assert _team_state(T, store, session.id)["handoff"] == [("ended", LEO)]


def test_a_handoff_says_whether_its_request_moved_and_why_not(world: TeamFixture) -> None:
    client, review, _rev, team, _store = world
    moved = _post(client, review, "/handoff", {"to": MAYA, "note": ""})
    assert moved.status_code == 200 and moved.json()["request_moved"] is True
    assert "request_move_error" not in moved.json()
    team.move_fails = "The review request changed at the same moment."
    kept = _post(client, review, "/handoff", {"to": LEO, "note": ""})
    assert kept.status_code == 200  # the handoff itself stands
    assert kept.json()["request_moved"] is False
    assert kept.json()["request_move_error"] == "The review request changed at the same moment."
    assert kept.json()["handoff"]["to"]["id"] == LEO


def test_the_giver_taking_the_review_back_takes_the_request_back_and_the_target_taking_it_does_not(
    world: TeamFixture,
) -> None:
    client, review, _rev, team, _store = world
    assert _post(client, review, "/handoff", {"to": MAYA, "note": ""}).status_code == 200
    assert team.moved == [(SAM, MAYA)]
    taken = _post(client, review, "/claim")  # Sam, the giver, takes it back
    assert taken.status_code == 200 and taken.json()["request_moved"] is True
    # exactly what the handoff recorded is reversed, nothing else
    assert team.restored == [{"kind": "delegated", "giver_request": f"req-{SAM}", "target_request": f"req-{MAYA}"}]
    assert team.moved == [(SAM, MAYA)]
    assert _post(client, review, "/release").status_code == 200
    assert _post(client, review, "/handoff", {"to": MAYA, "note": ""}).status_code == 200
    _as(team, MAYA)
    team.moved.clear()
    team.restored.clear()
    taken_by_target = _post(client, review, "/claim")
    assert taken_by_target.status_code == 200 and "request_moved" not in taken_by_target.json()
    assert team.moved == [] and team.restored == []  # the one it was handed to keeps the request


def test_a_handoff_that_recorded_no_move_is_not_reversed_by_a_take_back(world: TeamFixture) -> None:
    client, review, _rev, team, _store = world
    team.record_nothing = True  # as a handoff made before the move was kept, or one that moved nothing
    assert _post(client, review, "/handoff", {"to": MAYA, "note": ""}).status_code == 200
    taken = _post(client, review, "/claim")
    assert taken.status_code == 200 and "request_moved" not in taken.json()
    assert team.restored == []


def test_a_taker_who_did_not_give_the_review_moves_no_request(world: TeamFixture) -> None:
    client, review, _rev, team, _store = world
    assert _post(client, review, "/claim").status_code == 200
    assert team.moved == []


def test_a_finished_review_keeps_no_rows_and_a_person_who_is_gone_leaves_none(world: TeamFixture) -> None:
    from lemoncrow.pro.capabilities.review import team_presence_api as T

    client, review, _rev, team, store = world
    _post(client, review, "/presence")
    _post(client, review, "/claim")
    _as(team, MAYA)
    _post(client, review, "/presence")
    assert T.end_for_person(store, review, SAM) == 2  # their presence goes, their claim is released
    rows = {(r.relation, r.external_id, r.status) for r in store.list_relationships(review, kind=TEAM_KIND)}
    assert ("claim", "claim", "released") in rows and ("reviewing", SAM, "active") not in rows
    assert T.clear_review(store, review) == 2
    assert store.list_relationships(review, kind=TEAM_KIND) == ()


def test_a_handed_off_review_cannot_be_taken_by_a_third_person(world: TeamFixture) -> None:
    client, review, _rev, team, _store = world
    _post(client, review, "/handoff", {"to": MAYA, "note": ""})
    _as(team, LEO)
    assert _post(client, review, "/claim").status_code == 409
    _as(team, MAYA)
    assert _post(client, review, "/claim").status_code == 200
    seen = _get(client, review)  # her claim is the newer word: the handoff no longer shows
    assert seen["claim"]["by_me"] is True
    assert seen["handoff"] is None


def test_a_finished_review_shows_no_claim_and_takes_none(world: TeamFixture) -> None:
    client, review, _rev, team, store = world
    _post(client, review, "/claim")
    store.update_session(review, status="finished")
    assert _get(client, review)["claim"] is None
    _as(team, MAYA)
    assert _post(client, review, "/claim").status_code == 409


def test_an_unanswered_second_reviewer_never_reads_the_first_reviewers_handoff_note(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review import team_presence_api as T

    _root, store, session, _rev = _repo(tmp_path)
    team = FakeTeam()
    # Maya is a second reviewer who has not answered: Sam's words are hidden from her. The note was for Leo.
    hidden = lambda _s: frozenset({SAM}) if team.who == MAYA else frozenset()  # noqa: E731
    deps = T.TeamDeps(
        store=store, authority=team, session=store.get_session, mutable_session=store.get_session, hidden=hidden
    )
    T.hand_off(deps, session.id, to=LEO, note="I approved; the guard is fine")
    team.who = MAYA
    seen = T.build_status(deps, session.id)
    assert seen["handoff"]["to"]["id"] == LEO
    assert seen["handoff"]["note"] == ""
    assert seen["handoff"]["by"]["name"] == ""
    team.who = LEO
    assert T.build_status(deps, session.id)["handoff"]["note"] == "I approved; the guard is fine"


def test_finishing_a_review_deletes_its_presence_claim_and_handoff_rows(world: TeamFixture) -> None:
    client, review, revision, _team, store = world
    _post(client, review, "/presence")
    _post(client, review, "/claim")
    _post(client, review, "/handoff", {"to": MAYA, "note": ""})
    assert len(store.list_relationships(review, kind=TEAM_KIND)) == 3
    done = client.post(
        f"/api/reviews/{review}/finish",
        headers=HEADERS,
        json={"status": "finished", "expected_revision_id": revision},
    )
    assert done.status_code == 200, done.text
    assert store.list_relationships(review, kind=TEAM_KIND) == ()


def _deps_for(store: ReviewStore, team: FakeTeam, hidden: Any = None) -> Any:
    from lemoncrow.pro.capabilities.review import team_presence_api as T

    return T.TeamDeps(
        store=store, authority=team, session=store.get_session, mutable_session=store.get_session, hidden=hidden
    )


def test_a_handoff_to_someone_taken_off_the_review_blocks_nobody(world: TeamFixture) -> None:
    client, review, _rev, team, _store = world
    _post(client, review, "/handoff", {"to": MAYA, "note": ""})
    team.removed.add(MAYA)
    _as(team, LEO)
    assert _get(client, review)["handoff"] is None
    assert _post(client, review, "/claim").status_code == 200
    _post(client, review, "/release")
    assert _post(client, review, "/handoff", {"to": SAM, "note": ""}).status_code == 200


def test_a_handoff_by_someone_taken_off_the_review_is_over_too(world: TeamFixture) -> None:
    client, review, _rev, team, _store = world
    _post(client, review, "/handoff", {"to": MAYA, "note": ""})
    team.removed.add(SAM)
    _as(team, LEO)
    assert _get(client, review)["handoff"] is None


def test_a_second_reviewer_who_is_the_target_reads_no_words_of_the_first_reviewer(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review import team_presence_api as T
    from lemoncrow.pro.capabilities.review.second_reviewer_api import request_ask

    _root, store, session, revision = _repo(tmp_path)
    team = FakeTeam()
    answered = {"yes": False}
    # Maya owes a second signature on Sam's approval: Sam's words are hidden from her until she has answered.
    hidden = lambda _s: frozenset({SAM}) if team.who == MAYA and not answered["yes"] else frozenset()  # noqa: E731
    request_ask(
        store,
        review_id=session.id,
        revision_id=revision,
        first_outcome_id="out-1",
        first_reviewer_id=SAM,
        second_reviewer_id=MAYA,
        area="a",
        unit_keys=["u"],
    )
    deps = _deps_for(store, team, hidden)
    T.hand_off(deps, session.id, to=MAYA, note="I approved; the guard is fine")
    assert "guard" not in team.told[-1][1]  # the reason is stored: it never carried the words
    assert team.told[-1][1] == "Sam Rivera handed this review to you"
    team.who = MAYA
    seen = T.build_status(deps, session.id)
    assert seen["handoff"]["to_me"] is True
    assert seen["handoff"]["note"] == "" and seen["handoff"]["by"]["name"] == ""
    assert all(not n.get("note") for n in seen["notices"])
    assert [n["kind"] for n in seen["notices"]] == ["handed_off"]
    answered["yes"] = True  # once she has answered nothing is hidden from her
    assert T.build_status(deps, session.id)["handoff"]["note"] == "I approved; the guard is fine"


def test_an_unanswered_second_reviewer_is_not_told_the_first_reviewer_is_in_the_review(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review import team_presence_api as T
    from lemoncrow.pro.capabilities.review.second_reviewer_api import request_ask

    _root, store, session, revision = _repo(tmp_path)
    team = FakeTeam()
    answered = {"yes": False}
    hidden = lambda _s: frozenset({SAM}) if team.who == MAYA and not answered["yes"] else frozenset()  # noqa: E731
    request_ask(
        store,
        review_id=session.id,
        revision_id=revision,
        first_outcome_id="out-1",
        first_reviewer_id=SAM,
        second_reviewer_id=MAYA,
        area="a",
        unit_keys=["u"],
    )
    deps = _deps_for(store, team, hidden)
    T.beat(deps, session.id)  # Sam is in the review now
    team.who = MAYA
    assert T.build_status(deps, session.id)["reviewing"] == []
    answered["yes"] = True
    assert [person["id"] for person in T.build_status(deps, session.id)["reviewing"]] == [SAM]


def test_a_target_who_hides_nobody_gets_the_note_in_the_reason(world: TeamFixture) -> None:
    client, review, _rev, team, _store = world
    _post(client, review, "/handoff", {"to": MAYA, "note": "Start at the verifier."})
    assert team.told[-1][1] == "Sam Rivera handed this review to you: Start at the verifier."


def test_a_discarded_review_keeps_no_team_rows(world: TeamFixture) -> None:
    client, review, revision, _team, store = world
    _post(client, review, "/presence")
    _post(client, review, "/handoff", {"to": MAYA, "note": "secret words"})
    assert len(store.list_relationships(review, kind=TEAM_KIND)) == 2
    done = client.post(
        f"/api/reviews/{review}/finish", headers=HEADERS, json={"status": "archived", "expected_revision_id": revision}
    )
    assert done.status_code == 200, done.text
    assert store.list_relationships(review, kind=TEAM_KIND) == ()


def test_the_giver_taking_the_review_back_takes_back_the_targets_reason(world: TeamFixture) -> None:
    client, review, _rev, team, _store = world
    _post(client, review, "/handoff", {"to": MAYA, "note": "pls look"})
    team.untold.clear()
    assert _post(client, review, "/claim").status_code == 200
    assert team.untold == [(MAYA,)]


def test_deprovisioning_the_giver_takes_back_the_targets_reason(world: TeamFixture) -> None:
    from lemoncrow.pro.capabilities.review import team_presence_api as T

    client, review, _rev, _team, store = world
    _post(client, review, "/handoff", {"to": MAYA, "note": "look at auth"})
    taken_back: list[str] = []
    T.end_for_person(store, review, SAM, taken_back.append)
    assert taken_back == [MAYA]


def test_an_unanswered_second_reviewer_is_refused_a_claim_without_the_first_reviewers_name(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review import team_presence_api as T
    from lemoncrow.pro.capabilities.review.second_reviewer_api import request_ask

    _root, store, session, revision = _repo(tmp_path)
    team = FakeTeam()
    answered = {"yes": False}
    hidden = lambda _s: frozenset({SAM}) if team.who == MAYA and not answered["yes"] else frozenset()  # noqa: E731
    request_ask(
        store,
        review_id=session.id,
        revision_id=revision,
        first_outcome_id="out-1",
        first_reviewer_id=SAM,
        second_reviewer_id=MAYA,
        area="a",
        unit_keys=["u"],
    )
    deps = _deps_for(store, team, hidden)
    T.take(deps, session.id)  # Sam takes it
    team.who = MAYA
    with pytest.raises(T.TeamRefused) as refused:
        T.take(deps, session.id)
    assert refused.value.status == 409 and refused.value.detail == "Someone already took this review"
    assert "Sam" not in refused.value.detail and SAM not in refused.value.detail
    answered["yes"] = True  # once she has answered, she is told who
    with pytest.raises(T.TeamRefused) as named:
        T.take(deps, session.id)
    assert named.value.detail == "Sam Rivera already took this review"


def _hidden_scene(tmp_path: Path) -> tuple[Any, Any, FakeTeam, Any, dict[str, bool]]:
    from lemoncrow.pro.capabilities.review.second_reviewer_api import request_ask

    _root, store, session, revision = _repo(tmp_path)
    team = FakeTeam()
    answered = {"yes": False}
    hidden = lambda _s: frozenset({SAM}) if team.who == MAYA and not answered["yes"] else frozenset()  # noqa: E731
    request_ask(
        store,
        review_id=session.id,
        revision_id=revision,
        first_outcome_id="out-1",
        first_reviewer_id=SAM,
        second_reviewer_id=MAYA,
        area="a",
        unit_keys=["u"],
    )
    return store, session, team, _deps_for(store, team, hidden), answered


def _claim_by(store: Any, review_id: str) -> str:
    from lemoncrow.pro.capabilities.review import team_presence_api as T

    row = next((r for r in T.rows_of(store, review_id) if r.relation == T.REL_CLAIM and r.status == "taken"), None)
    return "" if row is None else str(row.meta.get("by_id"))


def test_a_hidden_second_reviewer_cannot_hand_off_a_review_the_first_reviewer_holds(tmp_path: Path) -> None:
    """R8-1: a hand-off decides on the whole team state. Sam's claim is hidden from her, not absent."""

    from lemoncrow.pro.capabilities.review import team_presence_api as T

    store, session, team, deps, _answered = _hidden_scene(tmp_path)
    T.take(deps, session.id)  # Sam takes it
    team.who = MAYA
    for target in (SAM, LEO):
        with pytest.raises(T.TeamRefused) as refused:
            T.hand_off(deps, session.id, to=target, note="mine now")
        assert refused.value.status == 409 and refused.value.detail == "Someone took this review", target
        assert _claim_by(store, session.id) == SAM  # his claim stands, and nothing was handed over
        assert not [r for r in T.rows_of(store, session.id) if r.relation == T.REL_HANDOFF and r.status == "active"]


def test_the_review_is_handed_off_by_a_reader_who_may_read_the_holder(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review import team_presence_api as T

    _store, session, team, deps, answered = _hidden_scene(tmp_path)
    T.take(deps, session.id)
    team.who = MAYA
    answered["yes"] = True  # nothing is hidden from her any more: the refusal names him
    with pytest.raises(T.TeamRefused) as refused:
        T.hand_off(deps, session.id, to=LEO, note="mine now")
    assert refused.value.detail == "Sam Rivera took this review"


def test_a_first_reviewer_who_is_in_the_review_is_told_when_a_hidden_second_reviewer_takes_it(tmp_path: Path) -> None:
    """R8-2: who was present is decided on the whole state. She is not shown his name in it."""

    from lemoncrow.pro.capabilities.review import team_presence_api as T

    _store, session, team, deps, _answered = _hidden_scene(tmp_path)
    T.beat(deps, session.id)  # Sam is in the review
    team.who = MAYA
    taken = T.take(deps, session.id)
    assert taken["claim"]["by_me"] is True
    assert SAM not in repr(taken["claim"]) and "Sam" not in repr(taken["claim"])  # nothing of him in her view
    team.who = SAM
    told = T.build_status(deps, session.id)
    assert [n["kind"] for n in told["notices"]] == ["taken"]  # he is told
    assert [p["id"] for p in told["claim"]["waiting"]] == [SAM]
    after = T.beat(deps, session.id)
    assert after["claim"]["waiting"] == [] and [p["id"] for p in after["claim"]["told"]] == [SAM]
