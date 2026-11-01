"""Known callers/consumers per changed target and impact readiness in the Brief projection (F7)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.consumer_inventory import MAX_SITES_PER_GROUP, consumer_inventory
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore


@dataclass(frozen=True)
class _T:
    target_id: str
    path: str
    symbol: str
    start_line: int
    end_line: int


_TARGETS = (
    _T("t-load", "src/svc.py", "Service.load", 10, 20),
    _T("t-save", "src/svc.py", "save", 30, 40),
    _T("t-other", "src/other.py", "helper", 1, 5),
)


def _packet(**extra: Any) -> dict[str, Any]:
    packet: dict[str, Any] = {
        "index_status": "stale",
        "degraded": ["symbol_relations", "ambiguous_symbol_counts"],
        "symbols": [
            {"symbol_name": "load", "file_path": "src/svc.py", "start_line": 10, "caller_count": 5},
        ],
        "impact": [
            {
                "kind": "untouched_caller",
                "path": "src/api.py:L42",
                "old": "load",
                "new": "modified",
                "snippet": "api.handle",
                "in_patch": False,
                "source_path": "src/svc.py",
            },
            {
                "kind": "untouched_caller",
                "path": "src/cli.py",
                "old": "load",
                "new": "modified",
                "snippet": "cli.main",
                "in_patch": False,
                "source_path": "src/svc.py",
            },
            {
                "kind": "contract_literal",
                "path": "web/app.ts:L7",
                "old": "user_id",
                "new": None,
                "snippet": "fetch(user_id)",
                "in_patch": False,
                "source_path": "src/svc.py",
                "uncertainty": "2 definitions named user_id",
            },
            {"kind": "contract_literal", "path": "docs/x.md:L1", "old": "k", "snippet": "k", "source_path": ""},
        ],
    }
    packet.update(extra)
    return packet


def test_callers_join_exactly_one_target_and_count_unlisted() -> None:
    out = consumer_inventory(_packet(), _TARGETS)

    by_key = {group["key"]: group for group in out["consumers"]}
    load = by_key["src/svc.py::load"]
    assert load["join"] == "exact" and load["target_ids"] == ["t-load"]
    assert [(site["path"], site["line"], site["symbol"]) for site in load["sites"]] == [
        ("src/api.py", 42, "api.handle"),
        ("src/cli.py", 0, "cli.main"),
    ]
    assert {site["exactness"] for site in load["sites"]} == {"snapshot"}
    assert load["known_callers"] == 5 and load["unlisted_callers"] == 3
    # A detector site the packet attributes only to a file stays at file level.
    literal = by_key["src/svc.py::"]
    assert literal["join"] == "file" and literal["target_ids"] == ["t-load", "t-save"]
    assert literal["sites"][0]["kind"] == "consumer" and literal["sites"][0]["uncertainty"]
    # A site no changed file owns stays at Review level.
    assert by_key["::"]["join"] == "review" and by_key["::"]["target_ids"] == []


def test_readiness_names_index_state_and_degradation() -> None:
    ready = consumer_inventory(_packet(), _TARGETS)["readiness"]
    assert ready["source"] == "snapshot_packet" and ready["impact_pass"] == "ran"
    assert ready["index_status"] == "stale"
    assert ready["degraded"] == ["ambiguous_symbol_counts", "symbol_relations"]
    assert ready["sites_recorded"] == 4

    disabled = consumer_inventory(_packet(degraded=["impact_disabled"], impact=[]), _TARGETS)
    assert disabled["readiness"]["impact_pass"] == "not_run" and disabled["consumers"] == []

    missing = consumer_inventory(None, _TARGETS)
    assert missing["readiness"]["source"] == "unavailable" and missing["consumers"] == []


def test_sites_past_the_bound_are_counted() -> None:
    impact = [
        {
            "kind": "untouched_caller",
            "path": f"src/c{i:03d}.py:L1",
            "old": "load",
            "snippet": f"c{i}",
            "source_path": "src/svc.py",
        }
        for i in range(MAX_SITES_PER_GROUP + 7)
    ]
    [group] = consumer_inventory(_packet(impact=impact), _TARGETS)["consumers"]
    assert len(group["sites"]) == MAX_SITES_PER_GROUP and group["omitted"] == 7
    assert consumer_inventory(_packet(impact=impact), _TARGETS) == consumer_inventory(_packet(impact=impact), _TARGETS)


_AUTH = "fixture-" + "bearer-value"
PORT = 45997


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    monkeypatch.setenv("LEMONCROW_LIVE_REVIEW_PREWARM", "0")


def test_projection_serves_context_block(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    (root / "src/app.py").write_text("def load(x):\n    return x\n", encoding="utf-8")
    repo.index.add_all()
    repo.index.write()
    signature = pygit2.Signature("Fixture", "fixture@example.com", 1700000000, 0)
    repo.create_commit("HEAD", signature, signature, "base", repo.index.write_tree(), [])
    (root / "src/app.py").write_text("def load(x):\n    return x or None\n", encoding="utf-8")
    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(root, None, working_tree=True)
    build = build_review_packet_with_blobs(root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, root, rng, title=build.packet.title)
    snapshot_revision(store, session, root, rng, store_root=store_root, build=build)
    revision = store.latest_revision(session.id)
    assert revision is not None
    store.publish_snapshot(session.id, revision.id)
    app = FastAPI()
    register_review_api(app, store, auth_dependency=make_token_dependency(_AUTH), repo_root=root, port=PORT)
    client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}")

    response = client.get(f"/api/reviews/{session.id}/projection", headers={"Authorization": f"Bearer {_AUTH}"})

    assert response.status_code == 200, response.text
    context = response.json()["context"]
    assert context["readiness"]["source"] == "snapshot_packet"
    assert context["readiness"]["index_status"] in {"fresh", "stale", "absent"}
    assert isinstance(context["consumers"], list) and context["consumers_omitted"] == 0
