"""D7: browser-reported exposure, origin-scoped API, and signed record v2."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.record_verify import parse_key_set, verify_record
from lemoncrow.pro.capabilities.review.review_record import PREDICATE_TYPE, PREDICATE_TYPE_V2, validate_statement
from lemoncrow.pro.capabilities.review.seen_coverage import SeenCoverageRefused, coverage_index, validate_seen_payload
from lemoncrow.pro.capabilities.review.session_models import ReviewActivityEvent
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore
from tests.gateway.test_review_api import BASE as _API_BASE  # noqa: F401
from tests.gateway.test_review_api import Workspace, _commit, _init_repo, _write
from tests.gateway.test_review_record import (  # noqa: F401
    BASE,
    PORT,
    TOKEN,
    RecordWorkspace,
    record_workspace,
)


@dataclass
class Unit:
    unit_key: str = "part"
    path: str = "src/app.py"
    content_fingerprint: str = "sha256:abc"
    fingerprint_method: str = "symbol_source"
    start_line: int = 10
    end_line: int = 20


def _span(side: str = "new", start: int = 11, end: int = 13, hunk: int = 1) -> dict[str, object]:
    return {"side": side, "start_line": start, "end_line": end, "hunk_ordinal": hunk}


def _row(unit: str, path: str, spans: list[dict[str, object]]) -> dict[str, object]:
    return {"unit_key": unit, "path": path, "content_fingerprint": "sha256:abc", "spans": spans}


ROWS = [
    _row("part", "src/app.py", [_span("new", 11, 13), _span("old", 4, 8, 1), _span("new", 15, 54, 2)]),
    _row("other", "src/other.py", [_span("new", 1, 2, 0)]),
    _row("bare", "src/bare.py", []),
    _row("broken", "src/broken.py", [_span("new", 0, 0)]),
]
INDEX = coverage_index(ROWS)
UNITS = {
    "part": Unit(),
    "other": Unit(unit_key="other", path="src/other.py"),
    "bare": Unit(unit_key="bare", path="src/bare.py"),
}


def seen_body(*, revision: str = "revision", elapsed: int = 800) -> dict[str, object]:
    return {
        "revision_id": revision,
        "action": "seen",
        "unit_key": "part",
        "block_key": "part:src/app.py:sha256:abc:new:1:11:13",
        "side": "new",
        "start": 11,
        "end": 13,
        "hunk": 1,
        "source": "dwell",
        "elapsed_ms": elapsed,
    }


def _validate(body: dict[str, object]) -> dict[str, object]:
    return validate_seen_payload(body, units=UNITS, revision_id="revision", index=INDEX)


def test_seen_payload_accepts_only_the_exact_real_block() -> None:
    accepted = _validate(seen_body())
    assert accepted["basis"] == "reviewer_reported_not_independent_evidence"
    assert accepted["content_fingerprint"] == "sha256:abc"
    old = {**seen_body(), "block_key": "part:src/app.py:sha256:abc:old:1:4:8", "side": "old", "start": 4, "end": 8}
    assert _validate(old)["side"] == "old"
    # The required time comes from the real block: 40 changed lines need 1600 ms.
    long_block = {
        **seen_body(),
        "block_key": "part:src/app.py:sha256:abc:new:2:15:54",
        "start": 15,
        "end": 54,
        "hunk": 2,
    }
    assert _validate({**long_block, "elapsed_ms": 1600})["end"] == 54
    with pytest.raises(SeenCoverageRefused, match="1600"):
        _validate({**long_block, "elapsed_ms": 1000})


@pytest.mark.parametrize(
    "forged",
    [
        pytest.param(seen_body(revision="stale"), id="stale-revision"),
        pytest.param({**seen_body(), "unit_key": "other"}, id="block-of-another-unit"),
        pytest.param({**seen_body(), "unit_key": "missing"}, id="unknown-unit"),
        pytest.param({**seen_body(), "start": 13, "end": 13, "elapsed_ms": 600}, id="shrunk-start"),
        pytest.param({**seen_body(), "end": 11, "elapsed_ms": 600}, id="shrunk-end"),
        pytest.param({**seen_body(), "hunk": 2}, id="wrong-hunk"),
        pytest.param({**seen_body(), "side": "old"}, id="wrong-side"),
        pytest.param({**seen_body(), "side": "other"}, id="bad-side"),
        pytest.param({**seen_body(), "elapsed_ms": 500}, id="too-quick"),
        pytest.param({**seen_body(), "hunk": -1}, id="negative-hunk"),
        pytest.param({**seen_body(), "unit_key": ""}, id="empty-unit"),
        pytest.param({**seen_body(), "block_key": "other:src/other.py:sha256:abc:new:0:1:2"}, id="key-of-another-unit"),
        pytest.param(
            {**seen_body(), "block_key": "part:src/app.py:sha256:abc:new:1:11:1000000", "end": 1_000_000},
            id="invented-span",
        ),
        pytest.param(
            {
                **seen_body(),
                "block_key": "part:src/app.py:sha256:abc:old:1:4:900",
                "side": "old",
                "start": 4,
                "end": 900,
            },
            id="old-side-out-of-range",
        ),
        pytest.param(
            {**seen_body(), "block_key": "part:src/app.py:sha256:abc:old:1:4:8", "side": "old", "start": 4, "end": 99},
            id="old-side-key-with-wider-body",
        ),
        pytest.param(
            {**seen_body(), "unit_key": "bare", "block_key": "bare:src/bare.py:sha256:abc:unmeasured"},
            id="unmeasured-key",
        ),
        pytest.param(
            {**seen_body(), "unit_key": "broken", "block_key": "broken:src/broken.py:sha256:abc:unmeasured"},
            id="unmeasured-key-of-invalid-spans",
        ),
    ],
)
def test_seen_payload_refuses_every_forged_span(forged: dict[str, object]) -> None:
    with pytest.raises(SeenCoverageRefused):
        _validate(forged)


def test_skipped_payload_is_bounded_to_real_blocks_and_unmeasured_parts() -> None:
    keys = ["part:src/app.py:sha256:abc:new:1:11:13", "bare:src/bare.py:sha256:abc:unmeasured"]
    skipped = _validate({"action": "skipped", "revision_id": "revision", "block_keys": [*keys, keys[0]]})
    assert skipped["block_keys"] == sorted(keys)
    # A part whose every span is unusable is unmeasured on the server exactly as on the client.
    assert _validate(
        {"action": "skipped", "revision_id": "revision", "block_keys": ["broken:src/broken.py:sha256:abc:unmeasured"]}
    )["block_keys"]
    with pytest.raises(SeenCoverageRefused):
        _validate({"action": "skipped", "revision_id": "revision", "block_keys": ["forged:other-part"]})
    with pytest.raises(SeenCoverageRefused):
        _validate({"action": "skipped", "revision_id": "revision", "block_keys": "not-a-list"})


def test_seen_coverage_api_and_signed_record_v2(record_workspace: RecordWorkspace) -> None:  # noqa: F811
    ws = record_workspace
    revision = ws.latest_id()
    targets = ws.workspace.get(f"/api/reviews/{ws.review_id}/targets").json()["targets"]
    candidate = next(
        (target for target in targets if target["spans"] and target["fingerprint_method"] != "unknown"), None
    )
    assert candidate is not None, "fixture should include a fingerprinted changed target"
    span = next((row for row in candidate["spans"] if row["side"] == "new"), candidate["spans"][0])
    body = {
        "revision_id": revision,
        "action": "seen",
        "unit_key": candidate["unit_key"],
        "block_key": f"{candidate['unit_key']}:{candidate['path']}:{candidate['content_fingerprint']}:{span['side']}:{span['hunk_ordinal']}:{span['start_line']}:{span['end_line']}",
        "side": span["side"],
        "start": span["start_line"],
        "end": span["end_line"],
        "hunk": span["hunk_ordinal"],
        "elapsed_ms": 800,
        "source": "dwell",
    }
    route = f"/api/reviews/{ws.review_id}/seen-coverage"
    stale = ws.workspace.post(route, json={**body, "revision_id": "stale"})
    assert stale.status_code == 422
    assert ws.workspace.post(route, json={**body, "block_key": "forged:other-part"}).status_code == 422
    assert ws.workspace.post(route, json={**body, "source": "focus_answer"}).status_code == 422
    response = ws.workspace.post(route, json=body)
    assert response.status_code == 201, response.text
    assert response.json()["revision_id"] == revision
    listing = ws.workspace.get(route)
    assert listing.status_code == 200
    assert listing.json()["seen"][0]["block_key"] == body["block_key"]
    omitted = ws.workspace.post(
        route, json={"action": "skipped", "revision_id": revision, "block_keys": [body["block_key"]]}
    )
    assert omitted.status_code == 201, omitted.text

    ws.approve()
    ws.opt_in()
    signed = ws.export()
    assert signed.status_code == 201, signed.text
    result = signed.json()
    assert result["verification"]["status"] == "verified", result["verification"]
    assert result["matches_store"] is True, result["store"]
    assert result["store"]["seen_coverage"] == "match"
    statement = json.loads(base64.b64decode(result["envelope"]["payload"]))
    assert statement["predicateType"] == PREDICATE_TYPE_V2
    assert statement["predicate"]["schema_version"] == 2
    coverage = statement["predicate"]["seen_coverage"]
    assert coverage["basis"] == "reviewer_reported_not_independent_evidence"
    assert coverage["revision_id"] == revision
    assert len(coverage["seen"]) == len(coverage["skipped"]) == 1
    assert coverage["seen"][0]["content_fingerprint"]
    assert validate_statement(statement) == []
    altered = json.loads(json.dumps(statement))
    altered["predicate"]["seen_coverage"]["seen"][0]["unit_key"] = "tampered"
    assert altered != statement


# --------------------------------------------------------------------------- #
# API: forged spans, lifecycle, record binding
# --------------------------------------------------------------------------- #


def _real_blocks(ws: RecordWorkspace) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    targets = ws.workspace.get(f"/api/reviews/{ws.review_id}/targets").json()["targets"]
    return [
        (target, span) for target in targets if target["fingerprint_method"] != "unknown" for span in target["spans"]
    ]


def _body(ws: RecordWorkspace, target: dict[str, Any], span: dict[str, Any], **override: Any) -> dict[str, Any]:
    fingerprint = target["content_fingerprint"] or "fingerprint-unavailable"
    body = {
        "revision_id": ws.latest_id(),
        "action": "seen",
        "unit_key": target["unit_key"],
        "block_key": f"{target['unit_key']}:{target['path']}:{fingerprint}:{span['side']}:{span['hunk_ordinal']}:{span['start_line']}:{span['end_line']}",
        "side": span["side"],
        "start": span["start_line"],
        "end": span["end_line"],
        "hunk": span["hunk_ordinal"],
        "elapsed_ms": 3000,
        "source": "dwell",
    }
    body.update(override)
    return body


def _route(ws: RecordWorkspace) -> str:
    return f"/api/reviews/{ws.review_id}/seen-coverage"


def test_api_refuses_forged_spans_and_unmeasured_seen(record_workspace: RecordWorkspace) -> None:  # noqa: F811
    ws = record_workspace
    (target, span), *_ = _real_blocks(ws)
    good = _body(ws, target, span)
    other_unit = {**good, "unit_key": "file:src/not-a-part.py"}
    # (Shrunk spans need a multi-line block; the unit tests above cover them. This fixture's block is one line.)
    wider = {**good, "end": span["end_line"] + 40, "elapsed_ms": 3000}
    old_out_of_range = {**good, "side": "old", "start": 1, "end": 90_000}
    unmeasured = {
        **good,
        "block_key": f"{target['unit_key']}:{target['path']}:{target['content_fingerprint']}:unmeasured",
    }
    for forged in (other_unit, wider, old_out_of_range, unmeasured):
        refused = ws.workspace.post(_route(ws), json=forged)
        assert refused.status_code == 422, (forged, refused.text)
    assert ws.workspace.get(_route(ws)).json()["seen"] == []
    assert ws.workspace.post(_route(ws), json=good).status_code == 201


def test_a_repeated_report_appends_no_second_row(record_workspace: RecordWorkspace) -> None:  # noqa: F811
    ws = record_workspace
    (target, span), *_ = _real_blocks(ws)
    body = _body(ws, target, span)
    first = ws.workspace.post(_route(ws), json=body).json()
    again = ws.workspace.post(_route(ws), json=body)
    assert again.status_code == 201 and first["event_id"] and again.json()["event_id"] == ""
    skip = {"action": "skipped", "revision_id": ws.latest_id(), "block_keys": [body["block_key"]]}
    assert ws.workspace.post(_route(ws), json=skip).json()["event_id"]
    assert ws.workspace.post(_route(ws), json=skip).json()["event_id"] == ""
    rows = ws.workspace.store.list_coverage_events(
        ws.review_id,
        revision_id=ws.latest_id(),
        actor_id="local",
        kinds=("review.coverage_seen", "review.coverage_skipped"),
    )
    assert [row.kind for row in rows] == ["review.coverage_seen", "review.coverage_skipped"]


def test_a_discarded_review_takes_no_coverage_and_deleting_it_removes_the_rows(
    record_workspace: RecordWorkspace,  # noqa: F811
) -> None:
    ws = record_workspace
    (target, span), *_ = _real_blocks(ws)
    kinds = ("review.coverage_seen", "review.coverage_skipped")
    revision_id = ws.latest_id()
    assert ws.workspace.post(_route(ws), json=_body(ws, target, span)).status_code == 201
    store = ws.workspace.store
    assert len(store.list_coverage_events(ws.review_id, revision_id=revision_id, actor_id="local", kinds=kinds)) == 1
    store.update_session(ws.review_id, status="archived")
    refused = ws.workspace.post(_route(ws), json=_body(ws, target, span, elapsed_ms=3001))
    assert refused.status_code == 409 and "read-only" in refused.json()["detail"]
    # Retention: the rows are activity rows of the Review, so the Review's own deletion removes them.
    assert store.delete_archived_session(ws.review_id)
    assert store.list_coverage_events(ws.review_id, revision_id=revision_id, actor_id="local", kinds=kinds) == ()


def _signed_v2(ws: RecordWorkspace) -> dict[str, Any]:
    (target, span), *_ = _real_blocks(ws)
    assert ws.workspace.post(_route(ws), json=_body(ws, target, span)).status_code == 201
    ws.approve()
    ws.opt_in()
    exported = ws.export()
    assert exported.status_code == 201, exported.text
    body: dict[str, Any] = exported.json()
    assert body["store"]["seen_coverage"] == "match"
    return body


def test_a_valid_v2_record_still_verifies_after_later_coverage_events(
    record_workspace: RecordWorkspace,  # noqa: F811
) -> None:
    ws = record_workspace
    record_id = _signed_v2(ws)["record"]["record_id"]
    (target, span), *_ = _real_blocks(ws)
    store = ws.workspace.store
    # More reports than any fixed read window, all written after the record was issued.
    for number in range(5_100):
        store.record_activity(
            ReviewActivityEvent(
                id="",
                review_id=ws.review_id,
                revision_id=ws.latest_id(),
                kind="review.coverage_seen",
                actor_id="local",
                actor_type="human",
                detail_json=json.dumps({"block_key": f"later:{number}", "unit_key": "x"}),
            )
        )
    assert ws.workspace.post(_route(ws), json=_body(ws, target, span, elapsed_ms=3001)).status_code == 201
    again = ws.workspace.get(f"/api/reviews/{ws.review_id}/records/{record_id}/verify").json()
    assert again["store"]["seen_coverage"] == "match", again["store"]
    assert again["verification"]["status"] == "verified"


def test_a_record_verified_by_someone_other_than_its_signer_keeps_the_signers_coverage(
    record_workspace: RecordWorkspace,  # noqa: F811
) -> None:
    ws = record_workspace
    record_id = _signed_v2(ws)["record"]["record_id"]
    app = FastAPI()
    register_review_api(
        app,
        ws.workspace.store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=ws.workspace.repo_root,
        port=PORT,
        acting_reviewer_id=lambda: "someone-else",
    )
    viewer = TestClient(app, base_url=BASE)
    verified = viewer.get(
        f"/api/reviews/{ws.review_id}/records/{record_id}/verify", headers={"Authorization": f"Bearer {TOKEN}"}
    ).json()
    assert verified["store"]["seen_coverage"] == "match", verified["store"]
    # Their own view of coverage is theirs: they have reported nothing.
    mine = viewer.get(_route(ws), headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert mine["seen"] == []


def test_a_v1_record_signed_before_seen_coverage_still_verifies() -> None:
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "review_record" / "v1_signed_before_p3_2.json").read_text()
    )
    statement = json.loads(base64.b64decode(fixture["envelope"]["payload"]))
    assert statement["predicateType"] == PREDICATE_TYPE and statement["predicate"]["schema_version"] == 1
    assert "seen_coverage" not in statement["predicate"]
    assert validate_statement(statement) == []
    assert verify_record(fixture["envelope"], keys=parse_key_set(fixture["keys"])).status == "verified"


def test_a_focus_answer_needs_the_full_time_and_a_decision(record_workspace: RecordWorkspace) -> None:  # noqa: F811
    ws = record_workspace
    (target, span), *_ = _real_blocks(ws)
    # This fixture's only part is a quick check: One at a time shows it whole, but it is not a decision.
    assert target["answer"]["category"] == "quick_check"
    marked = ws.workspace.post(
        f"/api/reviews/{ws.review_id}/marks",
        json={"unit_key": target["unit_key"], "state": "reviewed", "expected_revision_id": ws.latest_id()},
    )
    assert marked.status_code == 200, marked.text
    # Answered, but a focus answer cannot be reported with 0 ms: it is held to the same D7 time.
    zero = ws.workspace.post(_route(ws), json=_body(ws, target, span, source="focus_answer", elapsed_ms=0))
    assert zero.status_code == 422 and "requires at least" in zero.json()["detail"]
    # Full time and answered, yet not a decision: refused rather than counted as seen.
    quick = ws.workspace.post(_route(ws), json=_body(ws, target, span, source="focus_answer", elapsed_ms=600))
    assert quick.status_code == 422 and "decision" in quick.json()["detail"]
    assert ws.workspace.get(_route(ws)).json()["seen"] == []


@pytest.fixture
def decision_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RecordWorkspace:
    """A Review that adds a symlink: a real changed block that is a decision by structure, whatever the person marks."""

    settings_root = tmp_path / "lemoncrow-root"
    settings_root.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(settings_root))
    repo_root = tmp_path / "repo"
    repo = _init_repo(repo_root)
    _write(repo_root, "target.txt", "one\n")
    _commit(repo, "base", 0)
    (repo_root / "link.txt").symlink_to("target.txt")
    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, None, working_tree=True)
    build = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, repo_root, rng, title=build.packet.title)
    snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)
    revision = store.latest_revision(session.id)
    assert revision is not None
    store.publish_snapshot(session.id, revision.id)
    app = FastAPI()
    register_review_api(app, store, auth_dependency=make_token_dependency(TOKEN), repo_root=repo_root, port=PORT)
    workspace = Workspace(TestClient(app, base_url=BASE), store, store_root, repo_root, session.id)
    return RecordWorkspace(workspace, app, settings_root)


@pytest.mark.parametrize("state", ["reviewed", "needs_changes"])
def test_a_saved_decision_answer_with_the_full_time_is_accepted_as_seen(
    decision_workspace: RecordWorkspace, state: str
) -> None:
    ws = decision_workspace
    (target, span), *_ = _real_blocks(ws)
    assert target["answer"]["category"] == "decision"
    report = _body(ws, target, span, source="focus_answer", elapsed_ms=3000)
    # Not yet answered: refused, and nothing is stored.
    unsaved = ws.workspace.post(_route(ws), json=report)
    assert unsaved.status_code == 422 and "must be saved" in unsaved.json()["detail"]
    assert ws.workspace.get(_route(ws)).json()["seen"] == []
    marked = ws.workspace.post(
        f"/api/reviews/{ws.review_id}/marks",
        json={"unit_key": target["unit_key"], "state": state, "expected_revision_id": ws.latest_id()},
    )
    assert marked.status_code == 200, marked.text
    accepted = ws.workspace.post(_route(ws), json=report)
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["event_id"]
    seen = ws.workspace.get(_route(ws)).json()["seen"]
    assert [(row["block_key"], row["source"]) for row in seen] == [(report["block_key"], "focus_answer")]
    rows = ws.workspace.store.list_coverage_events(
        ws.review_id, revision_id=ws.latest_id(), actor_id="local", kinds=("review.coverage_seen",)
    )
    assert len(rows) == 1
