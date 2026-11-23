"""Interactive side-by-side preview through the Review API (Phase 9R item 16).

Deterministic fakes sit at the driver seam; the proxy, exposure (real loopback
listeners), session store, run plan, budget and evidence paths are the real ones.
"""

from __future__ import annotations

import http.client
import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.compute_ledger import ComputeLedger
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.interactive_preview import LoopbackExposure
from lemoncrow.pro.capabilities.review.interactive_preview_api import TOKEN_HEADER, InteractivePreviewBackend
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.preview_runtime import PreviewStartSpec, RunningPreview
from lemoncrow.pro.capabilities.review.preview_sessions import (
    INTERACTIVE_PREVIEW_ENTRY,
    PreviewSessionStore,
    token_digest,
)
from lemoncrow.pro.capabilities.review.run_plan import load_run_plan
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.environment.execution import EnvironmentFingerprint, ExecutorClass
from lemoncrow.pro.environment.preview_proxy import ProxyRequest, ProxyResponse

TOKEN = "-".join(("preview", "api", "fixture"))
PORT = 45997
BASE = f"http://127.0.0.1:{PORT}"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
RECIPE = (
    "interactive_preview:\n"
    "  command: [python3, -m, http.server, '8080', --bind, 127.0.0.1]\n"
    "  port: 8080\n"
    "  ttl_minutes: 15\n"
)


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


class FakeDriver:
    """Deterministic driver seam: records starts/stops, answers per side."""

    executor_class = ExecutorClass.AUTHOR_MACHINE

    def __init__(self, run_on: str = "local", *, fingerprints: Mapping[str, EnvironmentFingerprint] | None = None):
        self.run_on = run_on
        self.problem = ""
        self.started: list[PreviewStartSpec] = []
        self.stopped: list[str] = []
        self.forwarded: list[tuple[str, ProxyRequest]] = []
        same = EnvironmentFingerprint(image_digest="host:sha256:" + "a" * 64, recipe_digest="r")
        self.fingerprints = dict(fingerprints or {"old": same, "new": same})
        self.sink_rows: dict[str, list[dict[str, Any]]] = {"old": [], "new": []}

    def unavailable(self) -> str:
        return self.problem

    def start(self, spec: PreviewStartSpec) -> RunningPreview:
        self.started.append(spec)
        return RunningPreview(spec.session_id, {side: fp.to_wire() for side, fp in self.fingerprints.items()})

    def forward(
        self, running: RunningPreview, side: str, request: ProxyRequest, *, max_bytes: int, timeout: float
    ) -> ProxyResponse:
        self.forwarded.append((side, request))
        return ProxyResponse(200, (("Content-Type", "text/html"),), f"<h1>{side}</h1>".encode())

    def stop(self, running: RunningPreview) -> dict[str, list[dict[str, Any]]]:
        self.stopped.append(running.session_id)
        return self.sink_rows


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _commit(repo: Any, message: str, offset: int) -> None:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    signature = pygit2.Signature("Fixture Tester", "fixture@example.invalid", 1700000000 + offset, 0)
    repo.create_commit("HEAD", signature, signature, message, tree, parents)


def _review(
    tmp_path: Path,
    *,
    base_config: str = RECIPE,
    environment: str = "",
    driver: FakeDriver | None = None,
    viewer: dict[str, str] | None = None,
    unavailable: str = "",
    max_live_previews: int | None = None,
    **register: Any,
) -> SimpleNamespace:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    repo = pygit2.init_repository(str(repo_root), initial_head="main")
    _write(repo_root, "site/index.html", "<h1>base</h1>\n")
    if base_config:
        _write(repo_root, ".lemoncrow/review.yaml", base_config)
    if environment:
        _write(repo_root, ".lemoncrow/environment.toml", environment)
    _commit(repo, "base", 0)
    _write(repo_root, "site/index.html", "<h1>head</h1>\n")
    _commit(repo, "change", 1)

    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, base="HEAD~1", head="HEAD")
    build = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, repo_root, rng, title=build.packet.title)
    revision = snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)
    store.publish_snapshot(session.id, revision.id)
    clock = Clock()
    fake = driver or FakeDriver()
    current = viewer if viewer is not None else {"id": "alice"}
    backend = InteractivePreviewBackend(
        drivers=lambda run_on: fake if run_on == fake.run_on else None,
        exposure=LoopbackExposure(),
        tree_root=tmp_path / "trees",
        frame_ancestors=(BASE,),
        clock=clock,
        unavailable=unavailable,
        **({} if max_live_previews is None else {"max_live_previews": max_live_previews}),
    )
    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=repo_root,
        port=PORT,
        acting_reviewer_id=lambda: current["id"],
        interactive_preview=backend,
        **register,
    )
    client = TestClient(app, base_url=BASE, raise_server_exceptions=False)
    return SimpleNamespace(
        client=client,
        store=store,
        review_id=session.id,
        revision_id=revision.id,
        app=app,
        driver=fake,
        clock=clock,
        viewer=current,
        manager=app.state.review_interactive_preview,
        repo=repo,
        repo_root=repo_root,
        session=session,
        tmp_path=tmp_path,
    )


def _url(review: SimpleNamespace, suffix: str = "") -> str:
    return f"/api/reviews/{review.review_id}/interactive-preview{suffix}"


def _start(review: SimpleNamespace, **body: Any) -> Any:
    return review.client.post(
        _url(review, "/sessions"), headers=HEADERS, json={"expected_revision_id": review.revision_id, **body}
    )


def _enter(review: SimpleNamespace, session_id: str, token: str, side: str = "old", path: str = "/") -> Any:
    return review.client.post(
        _url(review, f"/sessions/{session_id}/enter"),
        headers={**HEADERS, TOKEN_HEADER: token},
        json={"side": side, "path": path},
    )


def _redeem(entry_url: str) -> http.client.HTTPResponse:
    """What the entry page does: POST the fragment's ticket back to its own origin."""

    parts = urlsplit(entry_url)
    ticket = parse_qs(parts.fragment)["ticket"][0]
    conn = http.client.HTTPConnection(parts.hostname or "127.0.0.1", parts.port, timeout=5)
    conn.request(
        "POST",
        parts.path,
        body=f"ticket={ticket}",
        headers={"Host": parts.netloc, "Content-Type": "application/x-www-form-urlencoded"},
    )
    response = conn.getresponse()
    response.read()
    conn.close()
    return response


def _browse(entry_url: str) -> tuple[str, str]:
    """Follow the one-time entry like a browser: returns (origin, capability cookie)."""

    parts = urlsplit(entry_url)
    assert parts.query == "" and parts.fragment.startswith("ticket=")
    answer = _redeem(entry_url)
    assert answer.status == 200, answer.status
    cookie = (answer.getheader("Set-Cookie") or "").split(";", 1)[0]
    assert cookie.startswith("lcp_")
    return f"{parts.scheme}://{parts.netloc}", cookie


def _fetch(origin: str, path: str, cookie: str) -> tuple[int, Mapping[str, str], bytes]:
    parts = urlsplit(origin)
    conn = http.client.HTTPConnection(parts.hostname or "127.0.0.1", parts.port, timeout=5)
    try:
        conn.request("GET", path, headers={"Host": parts.netloc, "Cookie": cookie})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# offer
# --------------------------------------------------------------------------- #


def test_preview_is_not_offered_without_a_previewable_app(tmp_path: Path) -> None:
    review = _review(tmp_path, base_config="")
    offer = review.client.get(_url(review), headers=HEADERS)
    assert offer.status_code == 200, offer.text
    assert offer.json()["state"] == "not_offered"
    entry = offer.json()["entry"]
    assert entry is None or entry["status"] == "inapplicable"
    refused = _start(review)
    assert refused.status_code == 409
    assert review.driver.started == []


def test_truncated_detection_reports_coverage_unknown_not_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review import capability_catalog

    kwdefaults = capability_catalog.detect_capabilities.__kwdefaults__
    assert kwdefaults is not None
    monkeypatch.setitem(kwdefaults, "max_paths", 0)
    review = _review(tmp_path, base_config="")
    offer = review.client.get(_url(review), headers=HEADERS).json()
    assert offer["state"] == "not_available"
    assert offer["reason"].startswith(capability_catalog.COVERAGE_UNKNOWN)
    assert _start(review).status_code == 409 and review.driver.started == []


def test_head_only_recipe_is_never_offered(tmp_path: Path) -> None:
    # The recipe appears only on the head side: the trusted base declares nothing.
    review = _review(tmp_path, base_config="")
    assert review.client.get(_url(review), headers=HEADERS).json()["state"] == "not_offered"


def test_offer_is_available_from_the_trusted_base_recipe(tmp_path: Path) -> None:
    review = _review(tmp_path)
    offer = review.client.get(_url(review), headers=HEADERS).json()
    assert offer["state"] == "available", offer
    # Manual-only: the displayed plan does not run it until a reviewer picks it.
    assert offer["entry"] is None or offer["entry"]["status"] in {"on_request", "skipped"}
    assert offer["admission"]["id"] == INTERACTIVE_PREVIEW_ENTRY
    assert offer["admission"]["status"] == "run"
    assert offer["admission"]["executor"] == "local"
    assert offer["recipe"] == {"mode": "command", "source": "trusted_base", "framework": ""}
    assert offer["opens_from"] == ["behavior_changes", "checks_rail"]
    assert offer["run_on"] == "local"
    assert offer["sessions"] == []


PREVIEWS = """\
version = 1

[previews.web]
run = "npm start"
ready = "/health"

[previews.docs]
run = "make serve"
ready = "/"
"""


def test_every_declared_preview_is_offered_and_the_first_is_the_default(tmp_path: Path) -> None:
    review = _review(tmp_path, base_config="", environment=PREVIEWS)
    offer = review.client.get(_url(review), headers=HEADERS).json()
    assert offer["state"] == "available", offer
    assert (offer["previews"], offer["preview"]) == (["web", "docs"], "web")
    picked = review.client.get(_url(review), headers=HEADERS, params={"preview": "docs"}).json()
    assert (picked["state"], picked["previews"], picked["preview"]) == ("available", ["web", "docs"], "docs")
    # review.yaml's interactive_preview offers no named previews.
    other = _review(tmp_path / "plain")
    plain = other.client.get(_url(other), headers=HEADERS).json()
    assert (plain["previews"], plain["preview"]) == ([], None)


def test_a_start_runs_the_preview_it_names_else_the_first_declared(tmp_path: Path) -> None:
    review = _review(tmp_path, base_config="", environment=PREVIEWS)
    started = _start(review, preview="docs")
    assert started.status_code == 200, started.text
    assert started.json()["session"]["preview"] == "docs"
    assert review.driver.started[-1].recipe.command == ("sh", "-c", "make serve")
    assert review.driver.started[-1].recipe.source == ".lemoncrow/environment.toml previews.docs"
    rows = PreviewSessionStore(review.store).for_review(review.review_id)
    assert [row.preview for row in rows if row.state == "running"] == ["docs"]

    default = _start(review)
    assert default.status_code == 200, default.text
    assert default.json()["session"]["preview"] == "web"
    assert review.driver.started[-1].recipe.command == ("sh", "-c", "npm start")


def test_an_unknown_or_malformed_preview_name_is_refused(tmp_path: Path) -> None:
    review = _review(tmp_path, base_config="", environment=PREVIEWS)
    unknown = _start(review, preview="admin")
    assert unknown.status_code == 422
    assert unknown.json()["detail"] == "no preview named 'admin'; the trusted base declares web, docs"
    assert review.client.get(_url(review), headers=HEADERS, params={"preview": "admin"}).status_code == 422
    assert _start(review, preview=7).status_code == 422
    assert review.driver.started == []
    # Without environment.toml previews, any name is unknown.
    plain = _review(tmp_path / "plain")
    refused = _start(plain, preview="web")
    assert refused.status_code == 422 and "declares no previews" in refused.json()["detail"]


def test_disabled_driver_is_honestly_not_available(tmp_path: Path) -> None:
    driver = FakeDriver()
    driver.problem = "interactive previews need bubblewrap on this machine; refusing to run unsandboxed"
    review = _review(tmp_path, driver=driver)
    offer = review.client.get(_url(review), headers=HEADERS).json()
    assert offer["state"] == "not_available"
    assert "bubblewrap" in offer["reason"]
    refused = _start(review)
    assert refused.status_code == 409
    assert "bubblewrap" in refused.json()["detail"]


def _locus_policy(run_on: str, *, unavailable: str = "") -> Any:
    """A hosted-shaped effective policy placing runtime work on *run_on* (as the Reader bridge does)."""

    def policy(_session: Any, _revision: Any) -> Mapping[str, Any]:
        run_plan: dict[str, Any] = {
            "allowed_run_on": [run_on],
            "default_run_on": run_on,
            "allowed_run_on_by_class": {"ai": [run_on], "runtime": [run_on]},
        }
        if unavailable:
            run_plan["unavailable_run_on_by_class"] = {"runtime": {run_on: unavailable}}
        return {
            "behavior": {"effort": {"effective_value": "auto", "origin": {"layer": "built_in_default"}}},
            "run_plan": run_plan,
        }

    return policy


def test_unwired_locus_plans_not_available_with_its_reason(tmp_path: Path) -> None:
    reason = "managed sandbox execution is not enabled here; Gate E is closed for this deployment"
    review = _review(
        tmp_path,
        driver=FakeDriver("managed"),
        intelligence_policy=_locus_policy("managed", unavailable=reason),
    )
    offer = review.client.get(_url(review), headers=HEADERS).json()
    assert offer["state"] == "not_available"
    assert offer["admission"]["status"] == "not_available"
    assert "Gate E" in offer["reason"]
    refused = _start(review)
    assert refused.status_code == 409
    assert "Gate E" in refused.json()["detail"]
    assert review.driver.started == []


def test_customer_worker_preview_runs_through_planned_execution(tmp_path: Path) -> None:
    driver = FakeDriver("worker")
    driver.executor_class = ExecutorClass.CUSTOMER_WORKER
    review = _review(tmp_path, driver=driver, intelligence_policy=_locus_policy("worker"))
    offer = review.client.get(_url(review), headers=HEADERS).json()
    assert offer["state"] == "available", offer
    assert offer["run_on"] == "worker" and offer["executor_class"] == "customer_worker"
    started = _start(review)
    assert started.status_code == 200, started.text
    assert started.json()["session"]["executor_class"] == "customer_worker"
    plan = load_run_plan(review.store, review.revision_id)
    assert plan is not None
    entry = plan.entry(INTERACTIVE_PREVIEW_ENTRY)
    assert entry is not None and entry.status == "run" and entry.executor == "worker"


def test_no_customer_worker_connected_is_not_available(tmp_path: Path) -> None:
    review = _review(
        tmp_path,
        driver=FakeDriver("worker"),
        intelligence_policy=_locus_policy("worker", unavailable="no customer worker connected"),
    )
    offer = review.client.get(_url(review), headers=HEADERS).json()
    assert offer["state"] == "not_available"
    assert offer["reason"] == "no customer worker connected"
    assert _start(review).status_code == 409
    assert review.driver.started == []


def test_locus_without_a_preview_driver_is_not_available(tmp_path: Path) -> None:
    # The plan runs runtime work on the managed sandbox but no preview driver is wired there.
    review = _review(tmp_path, driver=FakeDriver("local"), intelligence_policy=_locus_policy("managed"))
    offer = review.client.get(_url(review), headers=HEADERS).json()
    assert offer["state"] == "not_available"
    assert "not wired on the managed executor" in offer["reason"]
    assert _start(review).status_code == 409


def test_server_without_a_preview_origin_is_not_available(tmp_path: Path) -> None:
    review = _review(tmp_path, unavailable="interactive previews need a separate preview origin on this server")
    offer = review.client.get(_url(review), headers=HEADERS).json()
    assert offer["state"] == "not_available"
    assert "separate preview origin" in offer["reason"]


# --------------------------------------------------------------------------- #
# start = manual amendment, store round trip, proxy, stop
# --------------------------------------------------------------------------- #


def test_start_admits_a_manual_amendment_and_round_trips_through_the_store(tmp_path: Path) -> None:
    review = _review(tmp_path)
    before = load_run_plan(review.store, review.revision_id)
    shown = before.entry(INTERACTIVE_PREVIEW_ENTRY) if before is not None else None
    assert shown is None or shown.status != "run"

    started = _start(review, ttl_seconds=120)
    assert started.status_code == 200, started.text
    payload = started.json()
    session = payload["session"]
    token = payload["token"]
    assert session["state"] == "running"
    assert session["ttl_seconds"] == 120
    assert session["viewer_id"] == "alice"
    assert session["revision_id"] == review.revision_id
    assert session["comparable"] is True
    assert set(session["origins"]) == {"old", "new"}
    assert session["origins"]["old"] != session["origins"]["new"]
    assert "token_digest" not in session and token not in json.dumps(session)
    assert payload["plan"]["entry"] == INTERACTIVE_PREVIEW_ENTRY

    # Executed == displayed: the persisted plan now runs the entry as a manual amendment.
    amended = load_run_plan(review.store, review.revision_id)
    assert amended is not None
    entry = amended.entry(INTERACTIVE_PREVIEW_ENTRY)
    assert entry is not None and entry.status == "run" and entry.executor == "local"
    assert entry.origin == "manual"
    assert payload["plan"]["digest"] == amended.digest

    # Store round trip: the durable row carries only the token digest.
    rows = PreviewSessionStore(review.store).for_review(review.review_id)
    assert [row.id for row in rows] == [session["id"]]
    assert rows[0].token_digest == token_digest(session["id"], token)
    assert rows[0].plan_digest == amended.digest
    assert token not in json.dumps(rows[0].to_wire(durable=True))

    listed = review.client.get(_url(review), headers=HEADERS).json()["sessions"]
    assert [row["id"] for row in listed] == [session["id"]]
    assert listed[0]["remaining_seconds"] == 120

    # Both sides prepared from the exact Snapshot, then started once.
    assert len(review.driver.started) == 1
    spec = review.driver.started[0]
    trees = {side.side: side.tree for side in spec.sides}
    assert (trees["old"] / "site" / "index.html").read_text() == "<h1>base</h1>\n"
    assert (trees["new"] / "site" / "index.html").read_text() == "<h1>head</h1>\n"


def test_entry_url_opens_each_side_on_its_own_origin_with_hardened_headers(tmp_path: Path) -> None:
    review = _review(tmp_path)
    payload = _start(review).json()
    sid, token = payload["session"]["id"], payload["token"]
    for side in ("old", "new"):
        entered = _enter(review, sid, token, side=side, path="/dashboard?tab=1")
        assert entered.status_code == 200, entered.text
        url = entered.json()["url"]
        assert url.startswith(payload["session"]["origins"][side])
        assert token not in url
        origin, cookie = _browse(url)
        status, headers, body = _fetch(origin, "/", cookie)
        assert status == 200
        assert body == f"<h1>{side}</h1>".encode()
        csp = headers["Content-Security-Policy"]
        assert f"frame-ancestors {BASE}" in csp
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Referrer-Policy"] == "no-referrer"
        # The ticket is single use.
        assert _redeem(url).status == 403
    sides = [side for side, _request in review.driver.forwarded]
    assert sides == ["old", "new"]


def test_token_is_bound_to_viewer_review_and_session(tmp_path: Path) -> None:
    viewer = {"id": "alice"}
    review = _review(tmp_path, viewer=viewer)
    payload = _start(review).json()
    sid, token = payload["session"]["id"], payload["token"]

    assert _enter(review, sid, "not-" + token).status_code == 403
    assert _enter(review, sid, "").status_code == 403
    viewer["id"] = "mallory"
    assert _enter(review, sid, token).status_code == 403
    assert review.client.get(_url(review), headers=HEADERS).json()["sessions"] == []
    stop = review.client.post(_url(review, f"/sessions/{sid}/stop"), headers=HEADERS, json={})
    assert stop.status_code == 403
    viewer["id"] = "alice"
    other = review.client.post(
        f"/api/reviews/r-other/interactive-preview/sessions/{sid}/enter",
        headers={**HEADERS, TOKEN_HEADER: token},
        json={"side": "old"},
    )
    assert other.status_code == 404
    assert _enter(review, sid, token).status_code == 200


def test_proxy_without_the_capability_cookie_is_refused(tmp_path: Path) -> None:
    review = _review(tmp_path)
    payload = _start(review).json()
    origin = payload["session"]["origins"]["old"]
    status, _headers, _body = _fetch(origin, "/", "")
    assert status == 403
    status, _headers, _body = _fetch(origin, "/", "lcp_forged=1")
    assert status == 403
    assert review.driver.forwarded == []


def test_stop_tears_down_and_records_a_human_observation(tmp_path: Path) -> None:
    review = _review(tmp_path)
    payload = _start(review).json()
    sid, token = payload["session"]["id"], payload["token"]
    origin, cookie = _browse(_enter(review, sid, token, path="/account?email=someone@example.com").json()["url"])
    _fetch(origin, "/account?email=someone@example.com", cookie)

    stopped = review.client.post(_url(review, f"/sessions/{sid}/stop"), headers=HEADERS, json={})
    assert stopped.status_code == 200, stopped.text
    record = stopped.json()["session"]
    assert record["state"] == "stopped"
    assert review.driver.stopped == [sid]
    with pytest.raises(OSError):
        _fetch(origin, "/", cookie)
    assert _enter(review, sid, token).status_code == 410

    evidence = [
        row for row in review.store.list_evidence(review.review_id) if row.id == record["recording_evidence_id"]
    ]
    assert len(evidence) == 1
    row = evidence[0]
    assert row.attestation_class == "human_observation"
    assert row.verification_status == ""
    assert row.source == "human"
    assert row.executor_class == ExecutorClass.AUTHOR_MACHINE.value
    document = json.loads((review.store.root / row.artifact_path).read_text())
    assert document["verdict"] is None
    assert document["observation"] == "human_exploration"
    assert "someone@example.com" not in json.dumps(document)
    assert token not in json.dumps(document)


# --------------------------------------------------------------------------- #
# time limit, supersede, close
# --------------------------------------------------------------------------- #


def test_ttl_expiry_on_the_injected_clock_refuses_and_tears_down(tmp_path: Path) -> None:
    review = _review(tmp_path)
    payload = _start(review, ttl_seconds=60).json()
    sid, token = payload["session"]["id"], payload["token"]
    origin, cookie = _browse(_enter(review, sid, token).json()["url"])
    assert _fetch(origin, "/", cookie)[0] == 200

    review.clock.now += 61
    status, _headers, body = _fetch(origin, "/", cookie)
    assert status == 410
    assert b"time limit" in body
    assert review.driver.stopped == [sid]
    rows = PreviewSessionStore(review.store).for_review(review.review_id)
    assert rows[-1].state == "expired"
    assert _enter(review, sid, token).status_code == 410


def test_sweeper_expires_an_idle_session(tmp_path: Path) -> None:
    review = _review(tmp_path)
    sid = _start(review, ttl_seconds=60).json()["session"]["id"]
    review.clock.now += 59
    assert review.manager.sweep() == []
    review.clock.now += 2
    assert review.manager.sweep() == [sid]
    assert review.driver.stopped == [sid]


def test_newer_snapshot_supersedes_the_session(tmp_path: Path) -> None:
    review = _review(tmp_path)
    sid = _start(review).json()["session"]["id"]
    _write(review.repo_root, "site/index.html", "<h1>head 2</h1>\n")
    _commit(review.repo, "change 2", 2)
    rng = resolve_rev_range(review.repo_root, base="HEAD~2", head="HEAD")
    store_root = review.tmp_path / "store"
    build = build_review_packet_with_blobs(review.repo_root, rng, store_root=store_root, with_patch_text=True)
    newer = snapshot_revision(review.store, review.session, review.repo_root, rng, store_root=store_root, build=build)
    assert newer.id != review.revision_id
    assert review.manager.sweep() == [sid]
    rows = PreviewSessionStore(review.store).for_review(review.review_id)
    assert rows[-1].state == "superseded"
    assert review.driver.stopped == [sid]


def test_closing_the_review_tears_the_session_down(tmp_path: Path) -> None:
    review = _review(tmp_path)
    sid = _start(review).json()["session"]["id"]
    review.store.update_session(review.review_id, status="finished")
    assert review.manager.sweep() == [sid]
    assert PreviewSessionStore(review.store).for_review(review.review_id)[-1].state == "closed"
    refused = _start(review)
    assert refused.status_code == 409


def test_a_second_start_replaces_the_viewers_live_session(tmp_path: Path) -> None:
    review = _review(tmp_path)
    first = _start(review).json()["session"]["id"]
    second = _start(review).json()["session"]["id"]
    assert first != second
    assert review.driver.stopped == [first]
    states = {row.id: row.state for row in PreviewSessionStore(review.store).for_review(review.review_id)}
    assert states == {first: "stopped", second: "running"}


# --------------------------------------------------------------------------- #
# concurrency bound (RC12-C): live previews are capped per serving scope (a repository hosted, this machine locally)
# --------------------------------------------------------------------------- #


def _start_as(review: SimpleNamespace, viewer: str) -> Any:
    review.viewer["id"] = viewer
    return _start(review)


def test_live_previews_are_bounded_at_and_past_the_cap(tmp_path: Path) -> None:
    review = _review(tmp_path, max_live_previews=2)
    first = _start_as(review, "alice")
    second = _start_as(review, "bob")
    assert first.status_code == 200 and second.status_code == 200
    # Past the bound: refused with a typed, actionable reason; nothing was started for the third viewer.
    refused = _start_as(review, "carol")
    assert refused.status_code == 429
    assert "2 interactive previews are already running" in refused.json()["detail"]
    assert "Stop one" in refused.json()["detail"]
    assert len(review.driver.started) == 2
    # A viewer replacing their own session never needs a second slot.
    assert _start_as(review, "alice").status_code == 200
    # A freed slot is usable again.
    bob = second.json()["session"]["id"]
    review.viewer["id"] = "bob"
    stopped = review.client.post(_url(review, f"/sessions/{bob}/stop"), headers=HEADERS, json={})
    assert stopped.status_code == 200
    assert _start_as(review, "carol").status_code == 200


def test_a_failed_start_gives_its_slot_back(tmp_path: Path) -> None:
    review = _review(tmp_path, max_live_previews=1)
    review.driver.problem = ""
    original = review.driver.start

    def failing(spec: PreviewStartSpec) -> RunningPreview:
        from lemoncrow.pro.capabilities.review.preview_runtime import PreviewStartFailed

        raise PreviewStartFailed("the app did not answer")

    review.driver.start = failing  # type: ignore[method-assign]
    assert _start_as(review, "alice").status_code == 409
    review.driver.start = original  # type: ignore[method-assign]
    assert _start_as(review, "bob").status_code == 200


def test_the_cap_counts_previews_another_replica_started_through_the_shared_state(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.interactive_preview import InteractivePreviewManager, PreviewRefused
    from lemoncrow.pro.capabilities.review.preview_runtime import PreparedSide, PreviewRecipe

    review = _review(tmp_path, max_live_previews=1)
    first = review.manager
    other_driver = FakeDriver()
    second = InteractivePreviewManager(
        store=review.store,
        exposure=LoopbackExposure(),
        compute=first._compute,
        clock=review.clock,
        state=first._state,
        scope=first._scope,
        max_live=1,
    )
    recipe = PreviewRecipe.from_wire({"mode": "command", "command": ["true"], "port": 8080})

    def start_on_second(viewer: str) -> Any:
        return second.start(
            review_id=review.review_id,
            revision_id=review.revision_id,
            viewer_id=viewer,
            driver=other_driver,
            recipe=recipe,
            prepare=lambda: tuple(PreparedSide(side, tmp_path) for side in ("old", "new")),
            plan_digest="d",
        )

    session_id = _start_as(review, "alice").json()["session"]["id"]
    with pytest.raises(PreviewRefused) as refused:
        start_on_second("bob")
    assert refused.value.status == 429 and refused.value.code == "capacity"
    assert other_driver.started == []
    review.viewer["id"] = "alice"
    assert review.client.post(_url(review, f"/sessions/{session_id}/stop"), headers=HEADERS, json={}).status_code == 200
    record, _token = start_on_second("bob")
    assert record.state == "running"
    second.shutdown()


def test_local_review_apps_share_one_machine_wide_preview_cap(tmp_path: Path) -> None:
    # The local server builds one Review app (and backend) per repository and Review.
    first = InteractivePreviewBackend.local(tmp_path / "repo-a", port=PORT)
    second = InteractivePreviewBackend.local(tmp_path / "repo-b", port=PORT)
    assert first.state is not None and first.state is second.state
    assert first.scope == second.scope


def test_concurrent_starts_never_exceed_the_cap(tmp_path: Path) -> None:
    import threading
    import time as _time

    review = _review(tmp_path, max_live_previews=3)
    original = review.driver.start

    def slow(spec: PreviewStartSpec) -> RunningPreview:
        _time.sleep(0.2)  # every starter holds its reservation while the others try
        return original(spec)

    review.driver.start = slow  # type: ignore[method-assign]
    manager = review.manager
    results: list[int | str] = []
    lock = threading.Lock()

    def start(viewer: str) -> None:
        from lemoncrow.pro.capabilities.review.interactive_preview import PreviewRefused
        from lemoncrow.pro.capabilities.review.preview_runtime import PreparedSide, PreviewRecipe

        recipe = PreviewRecipe.from_wire({"mode": "command", "command": ["true"], "port": 8080})
        try:
            manager.start(
                review_id=review.review_id,
                revision_id=review.revision_id,
                viewer_id=viewer,
                driver=review.driver,
                recipe=recipe,
                prepare=lambda: tuple(PreparedSide(side, tmp_path) for side in ("old", "new")),
                plan_digest="d",
            )
            outcome: int | str = "started"
        except PreviewRefused as exc:
            outcome = exc.status
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=start, args=(f"viewer-{index}",)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(map(str, results)) == ["429"] * 5 + ["started"] * 3
    assert len(review.driver.started) == 3


def _replica_manager(review: SimpleNamespace, **extra: Any) -> Any:
    """A second serving process: its own manager on the same shared state and scope."""

    from lemoncrow.pro.capabilities.review.interactive_preview import InteractivePreviewManager

    first = review.manager
    return InteractivePreviewManager(
        store=review.store,
        exposure=LoopbackExposure(),
        compute=first._compute,
        clock=review.clock,
        state=first._state,
        scope=first._scope,
        max_live=first._max_live,
        **extra,
    )


def _direct_start(manager: Any, review: SimpleNamespace, viewer: str, driver: Any, prepare: Any = None) -> Any:
    from lemoncrow.pro.capabilities.review.preview_runtime import PreparedSide, PreviewRecipe

    recipe = PreviewRecipe.from_wire({"mode": "command", "command": ["true"], "port": 8080})
    return manager.start(
        review_id=review.review_id,
        revision_id=review.revision_id,
        viewer_id=viewer,
        driver=driver,
        recipe=recipe,
        prepare=prepare or (lambda: tuple(PreparedSide(side, review.tmp_path) for side in ("old", "new"))),
        plan_digest="d",
    )


def _outcome(call: Any) -> str:
    from lemoncrow.pro.capabilities.review.interactive_preview import PreviewRefused

    try:
        call()
    except PreviewRefused as exc:
        return str(exc.status)
    return "started"


def test_a_start_in_flight_on_one_replica_holds_its_slot_on_every_replica(tmp_path: Path) -> None:
    """The cap is a reservation in the shared state, taken before prepare(), not a process-local count."""

    import threading

    from lemoncrow.pro.capabilities.review.preview_runtime import PreparedSide

    review = _review(tmp_path, max_live_previews=1)
    second = _replica_manager(review)
    in_prepare, proceed = threading.Event(), threading.Event()

    def slow_prepare() -> tuple[PreparedSide, ...]:
        in_prepare.set()
        assert proceed.wait(timeout=20)
        return tuple(PreparedSide(side, tmp_path) for side in ("old", "new"))

    outcomes: list[str] = []
    starter = threading.Thread(
        target=lambda: outcomes.append(
            _outcome(lambda: _direct_start(review.manager, review, "alice", review.driver, slow_prepare))
        )
    )
    starter.start()
    assert in_prepare.wait(timeout=20)
    other = FakeDriver()
    # Alice's start is inside prepare() on the first replica: nothing is open yet, but her slot is held.
    outcomes.append(_outcome(lambda: _direct_start(second, review, "bob", other)))
    proceed.set()
    starter.join(timeout=20)
    assert sorted(outcomes) == ["429", "started"], outcomes
    assert other.started == [] and len(review.manager._state.live(review.manager._scope_key)) == 1
    second.shutdown()


def test_a_start_whose_slot_lapsed_while_another_filled_the_scope_is_refused_before_it_opens(tmp_path: Path) -> None:
    """A start that outlived its reservation (no renewal landed) re-holds it before opening, or stops."""

    from lemoncrow.pro.capabilities.review.interactive_preview import RESERVATION_SECONDS
    from lemoncrow.pro.capabilities.review.preview_runtime import PreparedSide

    review = _review(tmp_path, max_live_previews=1)
    second, other = _replica_manager(review), FakeDriver()
    outcomes: list[str] = []

    def lapsing_prepare() -> tuple[PreparedSide, ...]:
        # Alice's reservation expires mid-start, and Bob takes the only slot on another replica.
        review.clock.now += RESERVATION_SECONDS + 1
        outcomes.append(_outcome(lambda: _direct_start(second, review, "bob", other)))
        return tuple(PreparedSide(side, tmp_path) for side in ("old", "new"))

    outcomes.append(_outcome(lambda: _direct_start(review.manager, review, "alice", review.driver, lapsing_prepare)))
    assert outcomes == ["started", "429"], outcomes
    # Alice's sides started, then were stopped: they never ran outside the count.
    assert len(review.driver.started) == 1 and len(review.driver.stopped) == 1
    assert len(review.manager._state.live(review.manager._scope_key)) == 1


def test_replacing_your_own_preview_at_capacity_keeps_the_slot_from_racing_starts(tmp_path: Path) -> None:
    """The viewer's new start holds its slot before the old session is torn down."""

    import threading

    from lemoncrow.pro.capabilities.review.preview_runtime import PreparedSide

    review = _review(tmp_path, max_live_previews=1)
    first_id = _start_as(review, "alice").json()["session"]["id"]
    second = _replica_manager(review)
    in_prepare, proceed = threading.Event(), threading.Event()

    def slow_prepare() -> tuple[PreparedSide, ...]:
        in_prepare.set()
        assert proceed.wait(timeout=20)
        return tuple(PreparedSide(side, tmp_path) for side in ("old", "new"))

    outcomes: list[str] = []
    restart = threading.Thread(
        target=lambda: outcomes.append(
            _outcome(lambda: _direct_start(review.manager, review, "alice", review.driver, slow_prepare))
        )
    )
    restart.start()
    assert in_prepare.wait(timeout=20)
    assert review.driver.stopped == [first_id]  # the old session is gone, its slot is not up for grabs
    other = FakeDriver()
    outcomes.append(_outcome(lambda: _direct_start(second, review, "bob", other)))
    proceed.set()
    restart.join(timeout=20)
    assert sorted(outcomes) == ["429", "started"], outcomes
    assert other.started == []
    states = {
        row.id: (row.viewer_id, row.state) for row in PreviewSessionStore(review.store).for_review(review.review_id)
    }
    assert states.pop(first_id) == ("alice", "stopped")
    assert list(states.values()) == [("alice", "running")]  # the viewer kept a preview
    second.shutdown()


def test_a_refused_replacement_keeps_the_viewers_running_preview(tmp_path: Path) -> None:
    review = _review(tmp_path, max_live_previews=1)
    first_id = _start_as(review, "alice").json()["session"]["id"]
    second = _replica_manager(review)
    # Another start holds alice's replaced slot already (same viewer, concurrent): only one may use it.
    held = review.manager.reserve(review_id=review.review_id, viewer_id="alice")
    assert _outcome(lambda: _direct_start(second, review, "alice", FakeDriver())) == "429"
    assert review.driver.stopped == []  # refused before tearing anything down
    review.manager.release(held)
    rows = {row.id: row.state for row in PreviewSessionStore(review.store).for_review(review.review_id)}
    assert rows[first_id] == "running"
    second.shutdown()


def test_a_start_refused_at_capacity_leaves_no_plan_amendment_and_starts_the_sweeper(tmp_path: Path) -> None:
    review = _review(tmp_path, max_live_previews=1)
    second = _replica_manager(review)
    _direct_start(second, review, "bob", FakeDriver())  # the scope is full, started on another replica
    assert review.manager._sweeper is None
    refused = _start_as(review, "alice")
    assert refused.status_code == 429, refused.text
    plan = load_run_plan(review.store, review.revision_id)
    entry = plan.entry(INTERACTIVE_PREVIEW_ENTRY) if plan is not None else None
    assert entry is None or entry.origin != "manual"  # no amendment was persisted for the refused start
    # A replica refused at capacity still reaps the scope (its sweeper runs from the first attempt).
    assert review.manager._sweeper is not None and review.manager._sweeper.is_alive()
    review.manager.shutdown()
    second.shutdown()


@pytest.mark.parametrize("failing", ["exposure", "state"])
def test_a_failure_after_the_sides_started_stops_them_and_frees_the_slot(tmp_path: Path, failing: str) -> None:
    review = _review(tmp_path, max_live_previews=1)
    manager = review.manager
    if failing == "exposure":
        original_open = manager._exposure.open

        def broken_open(core: Any) -> str:
            if core.side == "new":
                raise RuntimeError("no preview origin")
            return original_open(core)

        manager._exposure.open = broken_open
    else:
        state_open = manager._state.open

        def broken_state(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("coordination store unavailable")

        manager._state.open = broken_state
    with pytest.raises(RuntimeError):
        _direct_start(manager, review, "alice", review.driver)
    (spec,) = review.driver.started
    assert review.driver.stopped == [spec.session_id]  # the sandboxed apps are not left running uncounted
    assert manager._state.live(manager._scope_key) == []
    rows = PreviewSessionStore(review.store).for_review(review.review_id)
    assert [(row.id, row.state) for row in rows] == [(spec.session_id, "failed")]
    if failing == "exposure":
        manager._exposure.open = original_open
    else:
        manager._state.open = state_open
    record, _token = _direct_start(manager, review, "bob", review.driver)
    assert record.state == "running"  # the slot was given back
    manager.shutdown()


# --------------------------------------------------------------------------- #
# budget and compute
# --------------------------------------------------------------------------- #


def test_exhausted_budget_is_limited_by_budget(tmp_path: Path) -> None:
    review = _review(tmp_path, base_config=RECIPE + "review:\n  budget:\n    per_review_minutes: 1\n")
    offer = review.client.get(_url(review), headers=HEADERS).json()
    assert offer["state"] == "limited_by_budget"
    assert offer["reason"].startswith("Limited by budget")
    refused = _start(review)
    assert refused.status_code == 409
    assert refused.json()["detail"].startswith("Limited by budget")
    assert review.driver.started == []


def test_budget_caps_the_session_lifetime(tmp_path: Path) -> None:
    review = _review(tmp_path, base_config=RECIPE + "review:\n  budget:\n    per_review_minutes: 11\n")
    payload = _start(review, ttl_seconds=900)
    assert payload.status_code == 200, payload.text
    session = payload.json()["session"]
    # 11 minutes for two sides: 330 seconds of preview.
    assert session["ttl_seconds"] == 330
    assert session["limited_by_budget"] is True


def test_compute_is_recorded_per_minute_and_at_teardown(tmp_path: Path) -> None:
    review = _review(tmp_path)
    sid = _start(review, ttl_seconds=300).json()["session"]["id"]
    review.clock.now += 61
    review.manager.sweep()
    run_rows = [
        row
        for row in ComputeLedger(review.store).rows([review.revision_id])
        if row.entry_id == INTERACTIVE_PREVIEW_ENTRY and row.stage == "run"
    ]
    assert len(run_rows) == 1 and run_rows[0].seconds == pytest.approx(122.0)
    review.clock.now += 30
    review.client.post(_url(review, f"/sessions/{sid}/stop"), headers=HEADERS, json={})
    run_rows = [
        row
        for row in ComputeLedger(review.store).rows([review.revision_id])
        if row.entry_id == INTERACTIVE_PREVIEW_ENTRY and row.stage == "run"
    ]
    assert run_rows[0].seconds == pytest.approx(182.0)
    record = PreviewSessionStore(review.store).for_review(review.review_id)[-1]
    assert record.compute_seconds == pytest.approx(182.0)


def test_preview_compute_counts_against_the_one_visible_budget(tmp_path: Path) -> None:
    review = _review(tmp_path, base_config=RECIPE + "review:\n  budget:\n    per_review_minutes: 12\n")
    sid = _start(review, ttl_seconds=300).json()["session"]["id"]
    review.clock.now += 300
    review.manager.sweep()
    assert PreviewSessionStore(review.store).for_review(review.review_id)[-1].id == sid
    # Five minutes on two sides used 10 of the 12 compute minutes.
    offer = review.client.get(_url(review), headers=HEADERS).json()
    assert offer["budget"]["remaining_minutes"] == pytest.approx(2.0, abs=0.05)


def test_not_comparable_fingerprints_are_labeled(tmp_path: Path) -> None:
    driver = FakeDriver(
        fingerprints={
            "old": EnvironmentFingerprint(image_digest="host:sha256:" + "a" * 64, lockfile_digest="x"),
            "new": EnvironmentFingerprint(image_digest="host:sha256:" + "a" * 64, lockfile_digest="y"),
        }
    )
    review = _review(tmp_path, driver=driver)
    session = _start(review).json()["session"]
    assert session["comparable"] is False
    assert "lockfile" in session["comparability"]


def test_a_preview_never_runs_without_the_sidecars_its_trusted_base_declares(tmp_path: Path) -> None:
    config = RECIPE + "execution:\n  sidecars:\n    - {id: db, command: [postgres], port: 5432}\n"
    review = _review(tmp_path, base_config=config)
    refused = _start(review)
    assert refused.status_code == 409, refused.text
    assert "sidecars are not supported" in refused.json()["detail"]
    assert review.driver.started == []
    assert not list((tmp_path / "trees").glob("ips-*"))


def test_unknown_body_fields_fail_closed(tmp_path: Path) -> None:
    review = _review(tmp_path)
    refused = _start(review, command=["sh", "-c", "id"])
    assert refused.status_code == 422
    assert review.driver.started == []
    missing = review.client.post(_url(review, "/sessions"), headers=HEADERS, json={})
    assert missing.status_code == 400
