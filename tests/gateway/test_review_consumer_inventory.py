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
from lemoncrow.pro.capabilities.review.consumer_inventory import MAX_SITES_PER_GROUP, consumer_inventory, outside_reach
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
    ready = context["readiness"]
    # Captured with an index: the packet's callers. Captured without one (a working-tree capture has no head commit),
    # with no index answering now (this app has no code-intelligence provider): the working-tree capture has no head
    # commit to search, so the Reader leaves its callers unmeasured.
    assert ready["source"] == "snapshot_packet"
    assert ready["index_status"] in {"fresh", "stale", "absent"}
    assert isinstance(context["consumers"], list) and context["consumers_omitted"] == 0


# The billing fixture: captured with no code index; the current index knows the callers.
_BILLING_TARGETS = (
    _T("t-wrap", "lib/cache.ts", "wrap", 3, 9),
    _T("t-clear", "lib/cache.ts", "clear", 11, 13),
    _T("t-list", "invoices/list.ts", "listInvoices", 4, 8),
)


def _billing_packet(**extra: Any) -> dict[str, Any]:
    packet: dict[str, Any] = {
        "index_status": "absent",
        "degraded": ["centrality", "symbol_relations"],
        "files": [
            {"path": "invoices/list.ts", "status": "modified"},
            {"path": "lib/cache.ts", "status": "added"},
        ],
        "symbols": [
            {"symbol_name": "listInvoices", "file_path": "invoices/list.ts", "start_line": 4, "change": "modified"},
            {"symbol_name": "wrap", "file_path": "lib/cache.ts", "start_line": 3, "change": "added"},
            {"symbol_name": "clear", "file_path": "lib/cache.ts", "start_line": 11, "change": "added"},
        ],
        "impact": [],
    }
    packet.update(extra)
    return packet


def _calls(source: str, target: str, name: str, line: int, kind: str = "calls") -> dict[str, Any]:
    return {
        "source_path": source,
        "target_path": target,
        "kind": kind,
        "weight": 1,
        "evidence": [{"kind": kind, "path": source, "line": line, "symbol": name}],
        "symbols": [{"name": name, "count": 1}],
    }


_BILLING_INDEX = {
    "edges": [
        _calls("api/v1/invoices.ts", "invoices/list.ts", "listInvoices", 4),
        _calls("web/Dashboard.tsx", "invoices/list.ts", "listInvoices", 4),
        _calls("jobs/exportInvoices.ts", "invoices/list.ts", "listInvoices", 6),
        _calls("invoices/list.test.ts", "invoices/list.ts", "listInvoices", 7),
        # Inside the change: not an outside caller.
        _calls("invoices/list.ts", "lib/cache.ts", "wrap", 6),
        # Changed file to context: the direction is "uses", not "used by".
        _calls("lib/cache.ts", "api/v1/invoices.ts", "get", 4),
        # An import names the module, not a changed symbol.
        _calls("web/Dashboard.tsx", "invoices/list.ts", "invoices/list", 1, kind="imports"),
    ],
    "degraded": False,
    "truncated": False,
}


def test_a_capture_without_an_index_reports_callers_unmeasured_not_zero() -> None:
    out = consumer_inventory(_billing_packet(), _BILLING_TARGETS)
    assert out["readiness"]["impact_pass"] == "no_index"
    assert out["readiness"]["source"] == "snapshot_packet" and out["consumers"] == []

    # A degraded empty answer (index cold or unavailable) is not a measurement either.
    cold = {"edges": [], "degraded": True, "degraded_reason": "link:building", "truncated": False}
    assert (
        consumer_inventory(_billing_packet(), _BILLING_TARGETS, current=cold)["readiness"]["impact_pass"] == "no_index"
    )

    # A clean empty answer is: nothing outside the change calls it.
    clean = consumer_inventory(_billing_packet(), _BILLING_TARGETS, current={"edges": [], "degraded": False})
    assert clean["readiness"]["impact_pass"] == "ran" and clean["consumers"] == []


def test_current_index_callers_fill_a_capture_that_had_no_index() -> None:
    out = consumer_inventory(_billing_packet(), _BILLING_TARGETS, current=_BILLING_INDEX)

    ready = out["readiness"]
    assert ready["impact_pass"] == "ran" and ready["source"] == "current_index"
    assert ready["index_status"] == "current" and "symbol_relations" not in ready["degraded"]
    [group] = out["consumers"]
    assert group["key"] == "invoices/list.ts::listInvoices"
    assert group["join"] == "exact" and group["target_ids"] == ["t-list"] and group["change"] == "modified"
    assert [(site["path"], site["line"]) for site in group["sites"]] == [
        ("api/v1/invoices.ts", 4),
        ("invoices/list.test.ts", 7),
        ("jobs/exportInvoices.ts", 6),
        ("web/Dashboard.tsx", 4),
    ]
    assert {site["exactness"] for site in group["sites"]} == {"current_index"}
    assert {site["kind"] for site in group["sites"]} == {"caller"}

    reach = outside_reach(out)
    assert set(reach) == {"t-list"}
    assert reach["t-list"].files == ("api/v1/invoices.ts", "jobs/exportInvoices.ts", "web/Dashboard.tsx")
    assert reach["t-list"].tests == ("invoices/list.test.ts",) and reach["t-list"].calls_only


def test_current_index_is_ignored_when_the_capture_measured_callers() -> None:
    packet = _billing_packet(index_status="fresh", degraded=[])
    out = consumer_inventory(packet, _BILLING_TARGETS, current=_BILLING_INDEX)
    assert out["readiness"]["source"] == "snapshot_packet" and out["readiness"]["impact_pass"] == "ran"
    assert out["consumers"] == []

    pending = _billing_packet(degraded=["impact_disabled"])
    assert (
        consumer_inventory(pending, _BILLING_TARGETS, current=_BILLING_INDEX)["readiness"]["impact_pass"] == "not_run"
    )


def test_a_truncated_current_answer_is_named() -> None:
    truncated = {**_BILLING_INDEX, "truncated": True}
    ready = consumer_inventory(_billing_packet(), _BILLING_TARGETS, current=truncated)["readiness"]
    assert "caller_queries_truncated" in ready["degraded"]


class _CurrentIndex:
    """The Review Map's provider seam, answering only what the Brief asks."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, ...]] = []

    def file_relationships(self, paths: tuple[str, ...], *, limit: int = 320) -> dict[str, Any]:
        self.asked.append(paths)
        return {"edges": [_calls("src/cli.py", "src/app.py", "load", 3)], "degraded": False, "truncated": False}


def test_projection_ranks_and_summarizes_callers_the_current_index_knows(tmp_path: Path) -> None:
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
    assert build.packet.index_status == "absent"
    session = open_or_create_session(store, root, rng, title=build.packet.title)
    snapshot_revision(store, session, root, rng, store_root=store_root, build=build)
    revision = store.latest_revision(session.id)
    assert revision is not None
    store.publish_snapshot(session.id, revision.id)
    index = _CurrentIndex()
    app = FastAPI()
    register_review_api(
        app, store, auth_dependency=make_token_dependency(_AUTH), repo_root=root, port=PORT, code_intelligence=index
    )
    client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}")
    headers = {"Authorization": f"Bearer {_AUTH}"}

    body = client.get(f"/api/reviews/{session.id}/projection", headers=headers).json()

    assert body["context"]["readiness"]["source"] == "current_index"
    [group] = body["context"]["consumers"]
    assert group["symbol"] == "load" and [site["path"] for site in group["sites"]] == ["src/cli.py"]
    assert body["summary"]["facts"][0]["text"] == "load (src/app.py) changed · called from src/cli.py"
    # No check recipe is configured for this repository, so no command is offered.
    assert "command" not in body["summary"]["facts"][-1]
    targets = client.get(f"/api/reviews/{session.id}/targets", headers=headers).json()["targets"]
    load = next(target for target in targets if target["symbol"] == "load")
    assert load["priority"]["attention"] == "review_first"
    assert load["priority"]["reasons"][0] == {"code": "wide_reach", "detail": "Called from 1 file outside this change"}
    # One answer per Snapshot serves every surface.
    assert index.asked == [("src/app.py",)]


def _review(tmp_path: Path, mode: str) -> tuple[ReviewStore, Path, str]:
    """A capture with no index; src/cli.py (committed) calls the changed ``load``.

    *mode* is ``commit`` (HEAD~1..HEAD), ``working`` (working tree) or ``staged`` (index).
    """

    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    (root / "src/app.py").write_text("def load(x):\n    return x\n", encoding="utf-8")
    (root / "src/cli.py").write_text("from app import load\n\n\ndef main():\n    return load(1)\n", encoding="utf-8")
    repo.index.add_all()
    repo.index.write()
    signature = pygit2.Signature("Fixture", "fixture@example.com", 1700000000, 0)
    base = repo.create_commit("HEAD", signature, signature, "base", repo.index.write_tree(), [])
    (root / "src/app.py").write_text("def load(x):\n    return x or None\n", encoding="utf-8")
    if mode == "commit":
        repo.index.add_all()
        repo.index.write()
        repo.create_commit("HEAD", signature, signature, "change", repo.index.write_tree(), [base])
        rng = resolve_rev_range(root, "HEAD~1..HEAD")
    elif mode == "staged":
        repo.index.add("src/app.py")
        repo.index.write()
        rng = resolve_rev_range(root, None, staged=True)
    else:
        rng = resolve_rev_range(root, None, working_tree=True)
    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    build = build_review_packet_with_blobs(root, rng, store_root=store_root, with_patch_text=True)
    assert bool(build.packet.head_sha) == (mode == "commit")
    session = open_or_create_session(store, root, rng, title=build.packet.title)
    snapshot_revision(store, session, root, rng, store_root=store_root, build=build)
    revision = store.latest_revision(session.id)
    assert revision is not None
    store.publish_snapshot(session.id, revision.id)
    return store, root, session.id


def _projection(store: ReviewStore, scratch: Path, checkout: Path, session_id: str) -> dict[str, Any]:
    scratch.mkdir(exist_ok=True)
    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(_AUTH),
        repo_root=scratch,
        source_repo_root=checkout,
        session_repo_identity=str(checkout.resolve()),
        port=PORT,
    )
    client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}")
    response = client.get(f"/api/reviews/{session_id}/projection", headers={"Authorization": f"Bearer {_AUTH}"})
    assert response.status_code == 200, response.text
    return dict(response.json())


def _searched(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    from lemoncrow.pro.capabilities.review.pr_bot import callers

    calls: list[Path] = []
    real = callers.checkout_callers

    def _spy(root: Path, packet: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(root)
        return real(root, packet, **kwargs)

    monkeypatch.setattr(callers, "checkout_callers", _spy)
    return calls


def test_callers_are_searched_in_the_checkout_not_the_non_git_scratch_root(tmp_path: Path) -> None:
    store, checkout, session_id = _review(tmp_path, "commit")

    context = _projection(store, tmp_path / "scratch", checkout, session_id)["context"]

    assert context["readiness"]["source"] == "checkout_search"
    [group] = context["consumers"]
    assert [site["path"] for site in group["sites"]] == ["src/cli.py"]


def test_a_commit_is_answered_by_its_own_search_not_by_the_name_links_of_the_index(tmp_path: Path) -> None:
    # 2026-11-19 on real commits: the index called LICENSE and BENCHMARKS.md users of `perform` and `pipeline`.
    store, checkout, session_id = _review(tmp_path, "commit")
    (tmp_path / "scratch").mkdir()
    index = _CurrentIndex()
    index.file_relationships = lambda paths, *, limit=320: {  # type: ignore[method-assign]
        "edges": [_calls("LICENSE", "src/app.py", "load", 70)],
        "degraded": False,
        "truncated": False,
    }
    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(_AUTH),
        repo_root=tmp_path / "scratch",
        source_repo_root=checkout,
        session_repo_identity=str(checkout.resolve()),
        port=PORT,
        code_intelligence=index,
    )
    client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}")
    context = client.get(f"/api/reviews/{session_id}/projection", headers={"Authorization": f"Bearer {_AUTH}"}).json()[
        "context"
    ]

    assert context["readiness"]["source"] == "checkout_search"
    [group] = context["consumers"]
    assert [site["path"] for site in group["sites"]] == ["src/cli.py"]


def test_a_commit_capture_is_searched_at_the_commit_after_the_checkout_moved_on(tmp_path: Path) -> None:
    store, checkout, session_id = _review(tmp_path, "commit")
    # The caller is removed from the live tree only; the reviewed commit still has it.
    (checkout / "src/cli.py").write_text("def main():\n    return 1\n", encoding="utf-8")

    context = _projection(store, tmp_path / "scratch", checkout, session_id)["context"]

    assert context["readiness"]["source"] == "checkout_search"
    [group] = context["consumers"]
    assert [site["path"] for site in group["sites"]] == ["src/cli.py"]


@pytest.mark.parametrize("mode", ["working", "staged"])
@pytest.mark.parametrize("moved", [False, True])
def test_a_non_commit_capture_is_never_answered_by_the_live_working_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, moved: bool
) -> None:
    store, checkout, session_id = _review(tmp_path, mode)
    if moved:
        # An unstaged edit drops the real caller; an untracked file is not part of what was reviewed either.
        (checkout / "src/cli.py").write_text("def main():\n    return 1\n", encoding="utf-8")
        (checkout / "src/extra.py").write_text("from app import load\nload(2)\n", encoding="utf-8")
    searched = _searched(monkeypatch)

    body = _projection(store, tmp_path / "scratch", checkout, session_id)

    assert searched == []
    assert body["context"]["readiness"]["source"] != "checkout_search"
    assert body["context"]["consumers"] == []
    assert body["summary"]["risk"]["level"] == "not_measured"
