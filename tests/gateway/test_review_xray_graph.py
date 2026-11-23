"""The X-Ray graph document (``docs/reference/xray-graph.md``): index, recorded runs, optional explicit AI.

The producer is checked on in-memory revisions (determinism, stable ids, grounding, the evidence rules, caps,
folding and redaction) and through the Review API on a real repository and Review store (ready, pending,
unavailable, the 409 when AI is off, and the AI path replayed from a recorded fixture: no live model call).
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review import xray_graph_ai as ai
from lemoncrow.pro.capabilities.review import xray_graph_api
from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.evaluation.fixtures import (
    CaseFixtures,
    FixtureReplayer,
    RecordingExecutor,
    load_case_fixtures,
    write_case_fixtures,
)
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallUsage, StructuredExecution
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.runtime_validation import RUNTIME_VALIDATION_MEDIA_TYPE
from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_evidence_id
from lemoncrow.pro.capabilities.review.xray_graph import (
    MAX_DEGRADED,
    MAX_EDGES,
    MAX_FLOWS,
    MAX_LANES,
    MAX_NODES,
    MAX_SAMPLE_BYTES,
    MAX_STEP_LABEL,
    MAX_TOUR,
    MAX_VIEWS,
    MIN_TOUR,
    AnchorIndex,
    ChangedFile,
    ChangedSymbol,
    RecordedCall,
    RecordedRun,
    ResponseChange,
    XRayInputs,
    _return_label,
    build_index_graph,
    canonical_json,
    finalize_graph,
    graph_digest,
    redact_sample,
    route_path_matches,
    stable_id,
    string_literals,
)

TOKEN = "xray-test-token"
PORT = 45998
BASE_URL = f"http://127.0.0.1:{PORT}"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "xray_graph"
FIXTURE_CASE = "ai_enrichment"
RECORD_ENV = "LEMONCROW_XRAY_RECORD_FIXTURES"
EMAIL = "owner@example.com"

APP_BASE = '''"""Demo API."""

import json
from http.server import BaseHTTPRequestHandler


def plans():
    return {"plans": [{"name": "Team", "monthly": 49}]}


def review_summary():
    return {"open": 3, "closed": 7}


ROUTES = {
    "/api/plans": plans,
    "/api/reviews/summary": review_summary,
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = ROUTES.get(self.path)
        body = json.dumps(route() if route else {}).encode()
        self.send_response(200 if route else 404)
        self.end_headers()
        self.wfile.write(body)
'''
APP_HEAD = APP_BASE.replace('"monthly": 49}', '"monthly": 59}, {"name": "Enterprise", "monthly": 99}').replace(
    '"open": 3, "closed": 7', '"open": 4, "closed": 9, "stale": 1'
)
BRU_BASE = """meta {
  name: List plans
  type: http
  seq: 1
}

get {
  url: {{baseUrl}}/api/plans
  body: none
  auth: none
}
"""
BRU_HEAD = BRU_BASE + """
assert {
  res.status: eq 200
}
"""
API_JS_BASE = 'export function loadPlans() {\n  return fetch("/api/plans").then((r) => r.json());\n}\n'
API_JS_HEAD = API_JS_BASE.replace("r.json()", "r.ok ? r.json() : []")
BASE_FILES = {"backend/app.py": APP_BASE, "bruno/Plans.bru": BRU_BASE, "frontend/src/api.js": API_JS_BASE}
HEAD_FILES = {"backend/app.py": APP_HEAD, "bruno/Plans.bru": BRU_HEAD, "frontend/src/api.js": API_JS_HEAD}

BASE_BODY = {"plans": [{"name": "Team", "monthly": 49, "owner": EMAIL}]}
HEAD_BODY = {"plans": [{"name": "Team", "monthly": 59, "owner": EMAIL}, {"name": "Enterprise", "monthly": 99}]}


# --------------------------------------------------------------------------- #
# in-memory revisions
# --------------------------------------------------------------------------- #


def _ranges(old: str, new: str) -> tuple[tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]]:
    head: list[tuple[int, int]] = []
    base: list[tuple[int, int]] = []
    matcher = difflib.SequenceMatcher(a=old.splitlines(), b=new.splitlines(), autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            base.append((i1 + 1, i2))
        if j2 > j1:
            head.append((j1 + 1, j2))
    return tuple(head), tuple(base)


def _revision(
    base: Mapping[str, str], head: Mapping[str, str], **extra: Any
) -> tuple[XRayInputs, Callable[[str, str], str | None]]:
    files = []
    for path in sorted(set(base) | set(head)):
        old, new = base.get(path), head.get(path)
        if old == new:
            continue
        status = "added" if old is None else "removed" if new is None else "modified"
        head_ranges, base_ranges = _ranges(old or "", new or "")
        files.append(
            ChangedFile(
                path,
                status,
                "",
                sum(end - start + 1 for start, end in head_ranges),
                sum(end - start + 1 for start, end in base_ranges),
                "",
                head_ranges,
                base_ranges,
            )
        )

    def read(side: str, path: str) -> str | None:
        return (base if side == "base" else head).get(path)

    inputs = XRayInputs("review-1", "revision-1", "", "2026-11-19T10:00:00Z", tuple(files), **extra)
    return inputs, read


def _recorded(base_body: Any = BASE_BODY, head_body: Any = HEAD_BODY) -> dict[str, Any]:
    recipe = "bruno/Plans.bru"
    runs = (
        RecordedRun("ev-base", recipe, "base", "2026-11-19T10:01:00Z"),
        RecordedRun("ev-head", recipe, "head", "2026-11-19T10:02:00Z"),
    )
    calls = (
        RecordedCall("ev-base", recipe, "base", recipe, "List plans", "GET", "/api/plans", 200, base_body, True),
        RecordedCall("ev-head", recipe, "head", recipe, "List plans", "GET", "/api/plans", 200, head_body, True),
    )
    responses = (
        ResponseChange(
            "api_response:bruno/Plans.bru",
            recipe,
            "List plans",
            "Team 49 to 59; Enterprise added",
            "ev-base",
            "ev-head",
        ),
    )
    return {"runs": runs, "calls": calls, "responses": responses}


def _node(graph: Mapping[str, Any], label: str) -> dict[str, Any]:
    return next(node for node in graph["nodes"] if node["label"] == label)


def _views(graph: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    stack = list(graph["views"])
    while stack:
        view = stack.pop()
        out.append(view)
        stack.extend(view.get("children") or ())
    return out


def _assert_contract_bounds(graph: Mapping[str, Any]) -> None:
    assert 1 <= len(graph["lanes"]) <= MAX_LANES
    assert len(graph["nodes"]) <= MAX_NODES and len(graph["edges"]) <= MAX_EDGES
    assert len(graph["flows"]) <= MAX_FLOWS and len(_views(graph)) <= MAX_VIEWS
    if "walkthrough" in graph:  # optional; 2..12 steps when present
        assert MIN_TOUR <= len(graph["walkthrough"]["steps"]) <= MAX_TOUR
    assert len(graph["degraded"]) <= MAX_DEGRADED
    assert graph["digest"] == graph_digest(graph)
    assert sum(1 for edge in graph["edges"] if edge.get("hero")) <= 1


def test_the_index_graph_is_deterministic_and_its_ids_are_stable() -> None:
    inputs, read = _revision(BASE_FILES, HEAD_FILES)
    first = build_index_graph(inputs, read)
    shuffled = build_index_graph(replace(inputs, files=tuple(reversed(inputs.files))), read)
    assert canonical_json(first) == canonical_json(shuffled)
    assert finalize_graph(first, AnchorIndex(read)) == first  # finalizing is idempotent
    _assert_contract_bounds(first)
    assert first["provenance"]["producer"] == "index" and "model" not in first["provenance"]

    route = _node(first, "GET /api/plans")
    assert route["id"] == stable_id("n_", "route", "GET", "/api/plans")
    assert _node(first, "plans()")["delta"] == "modified"
    for edge in first["edges"]:
        assert edge["id"] == stable_id("e_", edge["from"], edge["to"], edge["kind"])
    # Ids name components, not the revision: the next revision of the same change keeps them.
    later = build_index_graph(replace(inputs, review_id="review-2", revision_id="revision-2"), read)
    assert [node["id"] for node in later["nodes"]] == [node["id"] for node in first["nodes"]]
    assert [edge["id"] for edge in later["edges"]] == [edge["id"] for edge in first["edges"]]
    assert later["digest"] != first["digest"]


def test_a_recorded_run_backs_every_recorded_edge_step_and_payload() -> None:
    inputs, read = _revision(BASE_FILES, HEAD_FILES, **_recorded())
    graph = build_index_graph(inputs, read)
    _assert_contract_bounds(graph)
    refs = {run["ref"] for run in graph["provenance"]["recorded_runs"]}
    assert refs == {"ev-base", "ev-head"}
    recorded = [edge for edge in graph["edges"] if edge["evidence"] == "recorded"]
    assert recorded and all(edge["recorded"]["ref"] in refs for edge in recorded)
    assert next(edge for edge in graph["edges"] if edge.get("hero")) in recorded

    step = next(step for flow in graph["flows"] for step in flow["steps"] if step.get("payload"))
    response = step["payload"]["response"]
    assert step["evidence"] == "recorded" and response["evidence"] == "recorded"
    assert response["evidence_ref"] in refs and response["behavior_key"] == "api_response:bruno/Plans.bru"
    assert response["after"]["plans"][1] == {"name": "Enterprise", "monthly": 99}
    assert response["status"] == {"before": 200, "after": 200}

    # A payload naming a run this revision does not hold is dropped.
    unknown = copy.deepcopy(graph)
    for flow in unknown["flows"]:
        for row in flow["steps"]:
            if row.get("payload"):
                row["payload"]["response"]["evidence_ref"] = "ev-elsewhere"
    assert not any(
        row.get("payload") for flow in finalize_graph(unknown, AnchorIndex(read))["flows"] for row in flow["steps"]
    )

    # Without its runs nothing stays recorded, and the downgrade is said.
    orphaned = copy.deepcopy(graph)
    orphaned["provenance"]["recorded_runs"] = []
    out = finalize_graph(orphaned, AnchorIndex(read))
    assert all(edge["evidence"] != "recorded" and "recorded" not in edge for edge in out["edges"])
    assert all(row["evidence"] != "recorded" and not row.get("payload") for f in out["flows"] for row in f["steps"])
    assert any(note.startswith("Shown as static") for note in out["degraded"])


def test_a_changed_response_of_an_endpoint_outside_the_change_gets_a_two_step_flow() -> None:
    team = BRU_BASE.replace("List plans", "Team").replace("/api/plans", "/api/team")
    recipe = "bruno/Team.bru"
    inputs, read = _revision(
        BASE_FILES,
        {**HEAD_FILES, recipe: team},
        runs=(
            RecordedRun("ev-team-base", recipe, "base", "2026-11-19T10:01:00Z"),
            RecordedRun("ev-team-head", recipe, "head", "2026-11-19T10:02:00Z"),
        ),
        calls=(
            RecordedCall("ev-team-base", recipe, "base", recipe, "Team", "GET", "/api/team", 200, {"n": 1}, True),
            RecordedCall("ev-team-head", recipe, "head", recipe, "Team", "GET", "/api/team", 200, {"n": 2}, True),
        ),
        responses=(
            ResponseChange("api_response:bruno/Team.bru", recipe, "Team", "n 1 to 2", "ev-team-base", "ev-team-head"),
        ),
    )
    graph = build_index_graph(inputs, read)
    _assert_contract_bounds(graph)
    endpoint = _node(graph, "GET /api/team")
    assert endpoint["kind"] == "external" and endpoint["source"] == "recorded" and endpoint["anchors"] == []
    edge = next(edge for edge in graph["edges"] if edge["to"] == endpoint["id"])
    assert edge["evidence"] == "recorded" and "payload" not in edge
    flow = next(flow for flow in graph["flows"] if {part["node"] for part in flow["participants"]} >= {edge["from"]})
    assert [(step["from"], step["to"], step["kind"]) for step in flow["steps"]] == [
        (edge["from"], endpoint["id"], "sync"),
        (endpoint["id"], edge["from"], "return"),
    ]
    assert flow["steps"][1]["payload"]["response"]["evidence_ref"] == "ev-team-head"


def _team_recorded() -> dict[str, Any]:
    """``_recorded()`` plus a second recipe (Team), whose endpoint is outside the change."""

    plans, recipe = _recorded(), "bruno/Team.bru"
    return {
        "runs": (
            *plans["runs"],
            RecordedRun("ev-team-base", recipe, "base", "2026-11-19T10:03:00Z"),
            RecordedRun("ev-team-head", recipe, "head", "2026-11-19T10:04:00Z"),
        ),
        "calls": (
            *plans["calls"],
            RecordedCall("ev-team-base", recipe, "base", recipe, "Team", "GET", "/api/team", 200, {"n": 1}, True),
            RecordedCall("ev-team-head", recipe, "head", recipe, "Team", "GET", "/api/team", 200, {"n": 2}, True),
        ),
        "responses": (
            *plans["responses"],
            ResponseChange("api_response:bruno/Team.bru", recipe, "Team", "n 1 to 2", "ev-team-base", "ev-team-head"),
        ),
    }


def test_step_ids_are_unique_in_the_graph_and_each_recorded_step_names_its_own_run() -> None:
    team = BRU_BASE.replace("List plans", "Team").replace("/api/plans", "/api/team")
    inputs, read = _revision(BASE_FILES, {**HEAD_FILES, "bruno/Team.bru": team}, **_team_recorded())
    graph = build_index_graph(inputs, read)
    _assert_contract_bounds(graph)
    steps = [(flow, step) for flow in graph["flows"] for step in flow["steps"]]
    assert len({step["id"] for _flow, step in steps}) == len(steps)
    assert all(step["id"].startswith(flow["id"] + ".s") for flow, step in steps)
    runs = {run["ref"]: run["recipe"] for run in graph["provenance"]["recorded_runs"]}
    by_title = {flow["title"]: flow for flow in graph["flows"]}
    for title, ref in (("List plans response", "ev-head"), ("Team response", "ev-team-head")):
        recorded = [step for step in by_title[title]["steps"] if step["evidence"] == "recorded"]
        # Each recorded step names the run of its own request (both sides of it), not every run of the revision.
        assert recorded and all(step["recorded"] == {"ref": ref, "count": 2} for step in recorded)
        assert {runs[step["recorded"]["ref"]] for step in recorded} == {runs[ref]}
    assert all("recorded" not in step for _flow, step in steps if step["evidence"] != "recorded")
    for tour in graph["walkthrough"]["steps"]:
        if tour["stage"]["kind"] == "flow":
            owned = {step["id"] for flow, step in steps if flow["id"] == tour["stage"]["flow"]}
            assert set(tour["focus"].get("messages") or ()) <= owned
    # A step that claims a run it cannot show is drawn as static.
    forged = copy.deepcopy(graph)
    static = next(step for flow in forged["flows"] for step in flow["steps"] if step["label"] == "plans()")
    static.update(evidence="recorded", recorded={"ref": "ev-nowhere", "count": 1})
    cleaned = finalize_graph(forged, AnchorIndex(read))
    again = next(step for flow in cleaned["flows"] for step in flow["steps"] if step["id"] == static["id"])
    assert again["evidence"] == "static" and "recorded" not in again


def test_a_recorded_return_says_which_field_changed_and_keeps_the_body_change_in_its_note() -> None:
    inputs, read = _revision(BASE_FILES, HEAD_FILES, **_recorded())
    graph = build_index_graph(inputs, read)
    flow = next(flow for flow in graph["flows"] if flow["title"] == "List plans response")
    assert [step["label"] for step in flow["steps"]] == [
        "GET /api/plans",
        "plans()",
        "returns plans",
        "200 · Team monthly 49 → 59, +1 more",
    ]
    assert flow["steps"][-1]["note"] == "Team 49 to 59; Enterprise added"
    summary = ({"open": 4, "reviewed": 11, "stale_marks": 1}, {"open": 2, "reviewed": 13, "stale_marks": 3})
    assert _return_label(200, 200, *summary) == "200 · open 4 → 2, +2 more"
    assert _return_label(200, 500, {"ok": True}, {"ok": False}) == "200 → 500 · ok true → false"
    assert _return_label(200, 404, {"a": 1}, {"a": 1}) == "HTTP 200 → 404"
    assert _return_label(200, 200, None, {"a": 1}) == "200 · new body"
    long = _return_label(
        200,
        200,
        {"preview_timeout_seconds_for_every_surface": 120},
        {"preview_timeout_seconds_for_every_surface": 180, "x": 1},
    )
    assert len(long) <= MAX_STEP_LABEL and long.startswith("200 · ") and long.endswith(", +1 more")


PRICING_BASE = """import { API } from "./config.js";
import content from "./content.json";

// fetch("/api/reviews/summary") was the old badge
export default function Pricing() {
  const load = () => fetch(`${API}/api/plans`);
  return <a href="/api/reviews/summary">summary</a>;
}
"""
PRICING_HEAD = PRICING_BASE.replace("fetch(`${API}/api/plans`)", "fetch(`${API}/api/plans`).then((r) => r.json())")
CLIENT_BASE = {
    **BASE_FILES,
    "frontend/src/Pricing.jsx": PRICING_BASE,
    "frontend/src/urls.js": 'export const SUMMARY_URL = "/api/reviews/summary";\n',
    "frontend/src/content.json": '{"title": "Pricing"}\n',
    "frontend/src/Pricing.test.jsx": 'it("loads", () => fetch("/api/reviews/summary"));\n',
}
CLIENT_HEAD = {
    **HEAD_FILES,
    "frontend/src/Pricing.jsx": PRICING_HEAD,
    "frontend/src/urls.js": 'export const SUMMARY_URL = "/api/reviews/summary"; // the badge\n',
    "frontend/src/content.json": '{"title": "Plans and pricing"}\n',
    "frontend/src/Pricing.test.jsx": 'it("loads", () => fetch("/api/reviews/summary").then(Boolean));\n',
}


def test_client_code_that_names_a_route_path_calls_it_but_comments_links_and_tests_do_not() -> None:
    inputs, read = _revision(CLIENT_BASE, CLIENT_HEAD)
    graph = build_index_graph(inputs, read)
    _assert_contract_bounds(graph)
    plans_route, summary_route = _node(graph, "GET /api/plans"), _node(graph, "GET /api/reviews/summary")
    pricing = _node(graph, "Pricing")
    urls = next(node for node in graph["nodes"] if node["anchors"][0]["path"] == "frontend/src/urls.js")

    def http_into(route: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        return {edge["from"]: edge for edge in graph["edges"] if edge["to"] == route["id"] and edge["kind"] == "http"}

    # A template literal with a base-URL prefix, anchored at its own line.
    edge = http_into(plans_route)[pricing["id"]]
    assert edge["label"] == "GET /api/plans" and edge["evidence"] == "static" and edge["delta"] == "modified"
    assert edge["anchors"] == [{"path": "frontend/src/Pricing.jsx", "side": "head", "start_line": 6, "end_line": 6}]
    # Importing a changed data file reads it.
    reads = next(edge for edge in graph["edges"] if edge["from"] == pricing["id"] and edge["kind"] == "data")
    assert reads["to"] == _node(graph, "content.json")["id"] and reads["label"] == "reads content.json"
    assert reads["anchors"] == [{"path": "frontend/src/Pricing.jsx", "side": "head", "start_line": 2, "end_line": 2}]
    # A bare URL constant counts; the commented-out fetch, the link and the test file do not.
    assert set(http_into(summary_route)) == {urls["id"]}
    assert http_into(summary_route)[urls["id"]]["label"] == "GET /api/reviews/summary"

    assert route_path_matches("/api/plans/{plan_id}", "/api/plans/${}", loose=True)
    assert not route_path_matches("/api/plans/{plan_id}", "/api/plans/${}", loose=False)
    assert not route_path_matches("/api/plans/:id", "/api/team/7", loose=True)
    assert not route_path_matches("/", "/pricing", loose=True)
    js = 'const a = "x"; // "y"\n/* "z"\n */ const b = `p${ q("w") }s`;\n'
    assert [(item.value, item.line) for item in string_literals("a.js", js)] == [("x", 1), ("p${}s", 3)]
    py = "# 'no'\nx = 'a'\n'''doc\nmore'''\ny = \"b\"\n"
    assert [(item.value, item.line) for item in string_literals("a.py", py)] == [("a", 2), ("doc\nmore", 3), ("b", 5)]


def _lines(text: str, needle: str, count: int) -> tuple[int, int]:
    start = next(number for number, line in enumerate(text.splitlines(), start=1) if needle in line)
    return start, start + count - 1


APP_CSV_BASE = APP_BASE.replace(
    "import json\n",
    'import json\nfrom pathlib import Path\n\nPLANS_CSV = Path(__file__).parent.parent / "data" / "plans.csv"\n',
).replace(
    'def plans():\n    return {"plans": [{"name": "Team", "monthly": 49}]}',
    'def plans():\n    rows = PLANS_CSV.read_text().splitlines()\n    return {"plans": rows}',
)
APP_CSV_HEAD = (
    APP_CSV_BASE.replace("read_text().splitlines()", "read_text().strip().splitlines()")
    .replace(
        'def review_summary():\n    return {"open": 3, "closed": 7}',
        'def review_summary():\n    # summary.csv is no longer read here\n    return {"open": 4, "closed": 9}',
    )
    .replace(
        "\n\nROUTES = {",
        '\n\ndef export_plans(text):\n    Path("data/plans.csv").write_text(text)\n\n\nROUTES = {',
    )
)
DATA_BASE = {"data/plans.csv": "name,monthly\nTeam,49\n", "data/summary.csv": "open\n3\n"}
DATA_HEAD = {"data/plans.csv": "name,monthly\nTeam,59\nEnterprise,99\n", "data/summary.csv": "open\n4\n"}
TEST_APP = 'def test_plans():\n    assert open("data/plans.csv").read()\n'


def test_a_path_literal_in_a_handler_reads_that_file_and_the_response_flow_shows_it() -> None:
    inputs, read = _revision(
        {**BASE_FILES, "backend/app.py": APP_CSV_BASE, **DATA_BASE, "backend/tests/test_app.py": TEST_APP},
        {
            **HEAD_FILES,
            "backend/app.py": APP_CSV_HEAD,
            **DATA_HEAD,
            "backend/tests/test_app.py": TEST_APP + "# more\n",
        },
        symbols=(
            ChangedSymbol(
                "export_plans", "function", "backend/app.py", *_lines(APP_CSV_HEAD, "def export_plans", 2), "added"
            ),
        ),
        **_recorded(),
    )
    graph = build_index_graph(inputs, read)
    _assert_contract_bounds(graph)
    plans_csv, summary_csv = _node(graph, "plans.csv"), _node(graph, "summary.csv")
    data = {(edge["from"], edge["to"]): edge for edge in graph["edges"] if edge["kind"] == "data"}
    head_line = APP_CSV_HEAD.splitlines().index('PLANS_CSV = Path(__file__).parent.parent / "data" / "plans.csv"') + 1
    # Through the module-level constant plans() uses, anchored at the path literal.
    reads = data[(_node(graph, "plans()")["id"], plans_csv["id"])]
    assert reads["label"] == "reads plans.csv" and reads["evidence"] == "static"
    assert reads["anchors"] == [
        {"path": "backend/app.py", "side": "head", "start_line": head_line, "end_line": head_line}
    ]
    writes = data[(_node(graph, "export_plans()")["id"], plans_csv["id"])]
    assert writes["label"] == "writes plans.csv"
    # A comment naming a file and a test file reading it are not data edges.
    assert not [key for key in data if key[1] == summary_csv["id"]]
    assert {source for source, target in data if target == plans_csv["id"]} == {
        _node(graph, "plans()")["id"],
        _node(graph, "export_plans()")["id"],
    }

    flow = next(flow for flow in graph["flows"] if flow["title"] == "List plans response")
    assert [part["node"] for part in flow["participants"]] == [
        _node(graph, "List plans")["id"],
        _node(graph, "GET /api/plans")["id"],
        _node(graph, "plans()")["id"],
        plans_csv["id"],
    ]
    assert [step["label"] for step in flow["steps"]] == [
        "GET /api/plans",
        "plans()",
        "reads plans.csv",
        "returns plans",
        "200 · Team monthly 49 → 59, +1 more",
    ]
    assert flow["steps"][2]["anchors"] == reads["anchors"] and flow["steps"][2]["evidence"] == "static"


def test_the_digest_is_the_contract_hash_of_the_graph_without_it() -> None:
    inputs, read = _revision(BASE_FILES, HEAD_FILES, **_recorded())
    graph = build_index_graph(inputs, read)
    body = {key: value for key, value in graph.items() if key != "digest"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert graph["digest"] == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_an_element_whose_code_is_not_on_this_revision_is_dropped_with_a_note() -> None:
    inputs, read = _revision(BASE_FILES, HEAD_FILES)
    graph = build_index_graph(inputs, read)
    template = _node(graph, "plans()")
    tampered = copy.deepcopy(graph)
    tampered["nodes"].append(
        {**template, "id": "n_ghost", "anchors": [{"path": "src/ghost.py", "side": "head", "start_line": 1}]}
    )
    tampered["nodes"].append(
        {
            **template,
            "id": "n_past_end",
            "anchors": [{"path": "backend/app.py", "side": "head", "start_line": 900, "end_line": 910}],
        }
    )
    tampered["edges"].append({**graph["edges"][0], "id": "e_dangling", "to": "n_ghost"})
    out = finalize_graph(tampered, AnchorIndex(read))
    ids = {node["id"] for node in out["nodes"]}
    assert "n_ghost" not in ids and "n_past_end" not in ids
    assert all(edge["to"] != "n_ghost" for edge in out["edges"])
    assert any("could not be found on this revision" in note for note in out["degraded"])
    _assert_contract_bounds(out)


def test_large_changes_fold_into_groups_and_every_list_stays_bounded() -> None:
    head = {f"src/pkg_{index % 9}/mod_{index:03}.py": f"def f{index}():\n    return {index}\n" for index in range(150)}
    inputs, read = _revision({}, head)
    graph = build_index_graph(inputs, read)
    _assert_contract_bounds(graph)
    folded = [node for node in graph["nodes"] if node["label"].startswith("+") and node["label"].endswith("more")]
    assert folded and all(node["id"] == stable_id("n_", "fold", node["lane"]) for node in folded)
    assert graph["degraded"] == ["Folded 91 components into '+N more' groups to stay within 60"]
    assert build_index_graph(inputs, read) == graph

    noisy = build_index_graph(replace(inputs, degraded=tuple(f"note {index}" for index in range(9))), read)
    _assert_contract_bounds(noisy)
    assert noisy["degraded"][-1] == "and 6 more notes"


def test_recorded_bodies_are_redacted_and_bounded() -> None:
    sample, masked = redact_sample({"owner": EMAIL, "rows": list(range(5000)), "note": "x" * 1000})
    encoded = canonical_json(sample)
    assert masked and EMAIL not in encoded and len(encoded.encode()) <= MAX_SAMPLE_BYTES

    inputs, read = _revision(BASE_FILES, HEAD_FILES, **_recorded())
    graph = build_index_graph(inputs, read)
    response = next(row for f in graph["flows"] for row in f["steps"] if row.get("payload"))["payload"]["response"]
    assert response["redacted"] is True
    assert EMAIL not in canonical_json(graph)
    assert "monthly" in response["shape"] and response["type"].endswith("Response")


# --------------------------------------------------------------------------- #
# AI enrichment: grounded, never recorded
# --------------------------------------------------------------------------- #


def _skeleton(prompt: str) -> dict[str, Any]:
    return dict(json.loads(prompt)["skeleton"]["value"])


def _answer(skeleton: Mapping[str, Any]) -> dict[str, Any]:
    """A deterministic model answer: grounded relabels plus a few proposals that must be dropped."""

    nodes = {node["label"]: node for node in skeleton["nodes"]}
    route, handler, client = nodes["GET /api/plans"], nodes["plans()"], nodes["api"]
    edge = next(row for row in skeleton["edges"] if row["from"] == route["id"] and row["to"] == handler["id"])
    flow = skeleton["flows"][0]
    return {
        "title": "Raise the Team plan to 59 and add Enterprise",
        "summary": "plans() now returns two tiers; the recorded List plans call shows the new body.",
        "lanes": [{"id": "api", "label": "Pricing API"}],
        "nodes": [
            {"id": route["id"], "label": "Plans endpoint", "lane": "api"},
            {"id": "n_invented", "label": "Billing service", "lane": "api"},
        ],
        "edges": [{"id": edge["id"], "label": "lists the tiers"}],
        "flows": [
            {
                "title": "Pricing lookup",
                "summary": "The endpoint asks plans() for the tiers.",
                "participants": [route["id"], handler["id"], client["id"], "n_nowhere"],
                "steps": [
                    {
                        "from": route["id"],
                        "to": handler["id"],
                        "label": "calls plans()",
                        "kind": "sync",
                        "note": "Claims to be recorded; it is not.",
                        "evidence": "recorded",
                        "anchors": [{"path": "backend/app.py", "side": "head", "start_line": 7, "end_line": 8}],
                    },
                    {
                        "from": client["id"],
                        "to": handler["id"],
                        "label": "reads the database",
                        "kind": "sync",
                        "note": "",
                        "anchors": [{"path": "backend/db.py", "side": "head", "start_line": 1, "end_line": 3}],
                    },
                ],
            }
        ],
        "walkthrough": [
            {
                "heading": "What changed",
                "body": "Two tiers instead of one.",
                "stage": "view:overview",
                "focus_nodes": [route["id"]],
                "focus_edges": [],
                "focus_messages": [],
            },
            {
                "heading": "The recorded response",
                "body": "Base and head bodies differ.",
                "stage": f"flow:{flow['id']}",
                "focus_nodes": [],
                "focus_edges": [],
                "focus_messages": [flow["steps"][-1]["id"], "s99"],
            },
            {
                "heading": "How plans() is reached",
                "body": "The endpoint calls the handler.",
                "stage": "new_flow:0",
                "focus_nodes": [],
                "focus_edges": [],
                "focus_messages": ["s1"],
            },
        ],
    }


def test_ai_relabels_and_adds_flows_but_never_records_and_counts_what_it_could_not_ground() -> None:
    inputs, read = _revision(BASE_FILES, HEAD_FILES, **_recorded())
    graph = build_index_graph(inputs, read)
    plan = ai.plan_ai(graph, inputs, lambda paths: tuple(paths))
    merged = ai.merge_ai(
        graph,
        ai.validate_ai_result(_answer(plan.skeleton)),
        AnchorIndex(read),
        model="gpt-test",
        generated_at=graph["provenance"]["generated_at"],
    )
    _assert_contract_bounds(merged)
    assert merged["provenance"]["producer"] == "index+ai" and merged["provenance"]["model"] == "gpt-test"
    assert merged["title"] == "Raise the Team plan to 59 and add Enterprise"
    assert _node(merged, "Plans endpoint")["id"] == stable_id("n_", "route", "GET", "/api/plans")
    assert {lane["id"]: lane["label"] for lane in merged["lanes"]}["api"] == "Pricing API"
    # Recorded is exactly what the runs recorded: the model added nothing to it.
    assert [e["id"] for e in merged["edges"] if e["evidence"] == "recorded"] == [
        e["id"] for e in graph["edges"] if e["evidence"] == "recorded"
    ]
    added = next(flow for flow in merged["flows"] if flow["title"] == "Pricing lookup")
    assert [step["label"] for step in added["steps"]] == ["calls plans()"]
    assert all(step["evidence"] == "inferred" for step in added["steps"])
    assert {part["node"] for part in added["participants"]} >= {_node(merged, "plans()")["id"]}
    assert "n_invented" not in {node["id"] for node in merged["nodes"]}
    tour = merged["walkthrough"]["steps"]
    assert [step["heading"] for step in tour] == ["What changed", "The recorded response", "How plans() is reached"]
    assert tour[2]["stage"] == {"kind": "flow", "flow": added["id"]}
    # n_invented, n_nowhere, the ungrounded step and the unknown s99 message.
    assert merged["degraded"][0] == "Dropped 4 AI-proposed element(s) that did not resolve to this revision"
    assert (
        ai.merge_ai(
            graph,
            ai.validate_ai_result(_answer(plan.skeleton)),
            AnchorIndex(read),
            model="gpt-test",
            generated_at=graph["provenance"]["generated_at"],
        )
        == merged
    )


def test_the_model_sees_only_paths_the_content_access_policy_allows() -> None:
    inputs, read = _revision(BASE_FILES, HEAD_FILES, **_recorded())
    graph = build_index_graph(inputs, read)
    plan = ai.plan_ai(graph, inputs, lambda paths: tuple(path for path in paths if not path.startswith("backend/")))
    assert "backend/app.py" not in plan.content_paths and "backend/app.py" not in plan.prompt()
    assert all(
        not item["path"].startswith("backend/") for node in plan.skeleton["nodes"] for item in node.get("anchors") or ()
    )
    assert "Team" not in plan.prompt()  # recorded bodies never reach the model
    with pytest.raises(ai.XRayAIRejected):
        ai.validate_ai_result({"title": "t"})


# --------------------------------------------------------------------------- #
# the Review API on a real repository and Review store
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def settings_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    from lemoncrow.pro.capabilities.model_setup.transport import set_active_store_root

    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    monkeypatch.setenv("LEMONCROW_OPENAI_MODEL", "gpt-test")
    root = tmp_path / "lemoncrow-root"
    root.mkdir()
    set_active_store_root(root)
    xray_graph_api._running.clear()
    try:
        yield root
    finally:
        set_active_store_root(None)
        xray_graph_api._running.clear()


class Review:
    def __init__(self, tmp_path: Path) -> None:
        self.repo_root = tmp_path / "repo"
        self.store_root = tmp_path / "store"
        repo = pygit2.init_repository(str(self.repo_root), initial_head="main")
        signature = pygit2.Signature("Fixture Tester", "fixture@example.com", 1700000000, 0)
        self._write(BASE_FILES)
        repo.index.add_all()
        repo.index.write()
        repo.create_commit("HEAD", signature, signature, "base", repo.index.write_tree(), [])
        self._write(HEAD_FILES)
        self.store = ReviewStore(self.store_root)
        rev_range = resolve_rev_range(self.repo_root, None, working_tree=True)
        self.session = open_or_create_session(self.store, self.repo_root, rev_range, title="x-ray")
        build = build_review_packet_with_blobs(
            self.repo_root, rev_range, store_root=self.store_root, with_patch_text=True
        )
        self.revision = snapshot_revision(
            self.store, self.session, self.repo_root, rev_range, store_root=self.store_root, build=build
        )
        self.store.publish_snapshot(self.session.id, self.revision.id)
        self.policy: dict[str, Any] = {}

    def _write(self, files: Mapping[str, str]) -> None:
        for path, text in files.items():
            target = self.repo_root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def record(self, side: str, body: Any, at: str) -> ReviewEvidence:
        """One runtime-validation record of ``bruno/Plans.bru``, as the HTTP-service runner stores it."""

        record = {
            "side": side,
            "request": {"scenario_id": "bruno/Plans.bru", "scenario_kind": "surface", "side": side},
            "scenario": {"id": "bruno/Plans.bru", "title": "List plans", "surface_id": "bruno/Plans.bru"},
            "summary": "GET /api/plans returned HTTP 200",
            "data": {"status_code": 200, "headers": {}, "body": body, "truncated": False},
        }
        payload = json.dumps(record, sort_keys=True).encode()
        evidence_id = new_evidence_id()
        rel, digest, size = self.store.write_evidence_artifact(self.session.id, self.revision.id, evidence_id, payload)
        return self.store.add_evidence(
            ReviewEvidence(
                id=evidence_id,
                review_id=self.session.id,
                revision_id=self.revision.id,
                kind="verification",
                title="List plans",
                path="bruno/Plans.bru",
                artifact_path=rel,
                content_hash=digest,
                mime_type=RUNTIME_VALIDATION_MEDIA_TYPE,
                bytes=size,
                source="test",
                source_ref=f"xray-test:{side}",
                created_at=at,
            )
        )

    def client(self) -> TestClient:
        app = FastAPI()
        register_review_api(
            app,
            self.store,
            auth_dependency=make_token_dependency(TOKEN),
            repo_root=self.repo_root,
            port=PORT,
            intelligence_policy=lambda _session, _revision: self.policy,
        )
        return TestClient(app, base_url=BASE_URL, headers={"Authorization": f"Bearer {TOKEN}"})

    @property
    def url(self) -> str:
        return f"/api/reviews/{self.session.id}/xray-graph"


@pytest.fixture
def review(tmp_path: Path) -> Review:
    return Review(tmp_path)


def _forbidden(**kwargs: Any) -> StructuredExecution:
    raise AssertionError("the X-Ray called a model")


@pytest.fixture
def deferred(monkeypatch: pytest.MonkeyPatch) -> list[Callable[[], None]]:
    """Enrichments the POST started, held until the test runs them."""

    held: list[Callable[[], None]] = []
    monkeypatch.setattr(xray_graph_api, "_spawn", held.append)
    return held


def test_get_serves_the_index_graph_ready_and_never_calls_a_model(
    review: Review, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ai, "execute_structured", _forbidden)
    client = review.client()
    first = client.get(review.url, params={"revision_id": review.revision.id})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["state"] == "ready" and set(body) == {"state", "graph"}
    graph = body["graph"]
    assert graph["revision_id"] == review.revision.id and graph["provenance"]["producer"] == "index"
    assert _node(graph, "GET /api/plans")["anchors"][0]["path"] == "backend/app.py"
    _assert_contract_bounds(graph)
    assert client.get(review.url).json() == body  # the latest revision, served from the cache
    stored = review.store.derived_results("xray_graph", [review.revision.id])
    assert stored[(review.revision.id, "index")][1]["graph"]["digest"] == graph["digest"]
    assert client.get(f"/api/reviews/{review.session.id}/xray-graph", params={"revision_id": "nope"}).status_code in {
        404,
        409,
    }


def test_a_new_recorded_run_rebuilds_the_graph_with_recorded_evidence(review: Review) -> None:
    client = review.client()
    before = client.get(review.url).json()["graph"]
    assert before["provenance"]["recorded_runs"] == []
    base = review.record("old", BASE_BODY, "2026-11-19T10:01:00Z")
    head = review.record("new", HEAD_BODY, "2026-11-19T10:02:00Z")
    graph = client.get(review.url).json()["graph"]
    assert graph["digest"] != before["digest"]
    runs = {run["ref"]: run for run in graph["provenance"]["recorded_runs"]}
    assert set(runs) == {base.id, head.id} and runs[head.id]["side"] == "head"
    edge = next(edge for edge in graph["edges"] if edge["evidence"] == "recorded")
    assert edge["recorded"]["ref"] in runs and edge["label"] == "GET /api/plans"
    # The behavior collection's api_response comparison reaches the flow as the recorded before/after body.
    step = next(step for flow in graph["flows"] for step in flow["steps"] if step.get("payload"))
    response = step["payload"]["response"]
    assert step["evidence"] == "recorded" and response["evidence"] == "recorded"
    assert response["evidence_ref"] == head.id and response["before"] != response["after"]
    assert response["behavior_key"]
    assert EMAIL not in json.dumps(graph)


def test_an_unreadable_packet_is_unavailable_with_a_reason(review: Review, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review.sources import local

    monkeypatch.setattr(local, "read_packet_json", lambda store, revision: None)
    body = review.client().get(review.url).json()
    assert body == {"state": "unavailable", "reason": "This revision's change packet could not be read."}


def test_ai_off_answers_409_and_leaves_the_index_graph(review: Review, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai, "execute_structured", _forbidden)
    layer = {"origin": {"layer": "organization", "source": "operator_review_policy", "scope_id": ""}}
    review.policy = {"intelligence": {"layers": [{**layer, "settings": {"enabled": False}}]}}
    client = review.client()
    index = client.get(review.url).json()["graph"]
    refused = client.post(f"{review.url}/generate", json={"revision_id": review.revision.id, "ai": True})
    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["state"] == "ready" and body["graph"] == index and body["reason"].startswith("AI is off")
    assert review.store.list_intelligence_runs(review.session.id) == ()
    rebuilt = client.post(f"{review.url}/generate", json={"revision_id": review.revision.id, "ai": False})
    assert rebuilt.status_code == 200 and rebuilt.json() == {"state": "ready", "graph": index}
    assert client.post(f"{review.url}/generate", json={"ai": "yes"}).status_code == 400


def test_an_agent_cli_route_is_refused_before_anything_is_sent(review: Review, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai, "execute_structured", _forbidden)
    layer = {"origin": {"layer": "organization", "source": "operator_review_policy", "scope_id": ""}}
    review.policy = {"intelligence": {"layers": [{**layer, "settings": {"driver": "claude"}}]}}
    refused = review.client().post(f"{review.url}/generate", json={"ai": True})
    assert refused.status_code == 409 and "agent CLI" in refused.json()["reason"]


def test_a_spent_review_ceiling_is_refused_before_anything_is_sent(
    review: Review, monkeypatch: pytest.MonkeyPatch, deferred: list[Callable[[], None]]
) -> None:
    monkeypatch.setattr(ai, "execute_structured", _forbidden)
    monkeypatch.setenv("LEMONCROW_REVIEW_SYNTHESIS_MAX_CALLS", "0")
    refused = review.client().post(f"{review.url}/generate", json={"ai": True})
    assert refused.status_code == 409 and refused.json()["reason"] == "This Review has used its 0 AI calls."
    assert not deferred


class _Case:
    """The fixture's case identity: the repository the recording was made on."""

    @staticmethod
    def input_digest() -> str:
        return hashlib.sha256(canonical_json([BASE_FILES, HEAD_FILES, BASE_BODY, HEAD_BODY]).encode()).hexdigest()


def _synthetic(**kwargs: Any) -> StructuredExecution:
    """The recorder's responder: deterministic and offline (``LEMONCROW_XRAY_RECORD_FIXTURES=1`` re-records)."""

    return StructuredExecution(_answer(_skeleton(kwargs["prompt"])), "openai", "gpt-test", ModelCallUsage(calls=1))


@pytest.fixture
def replayed(monkeypatch: pytest.MonkeyPatch) -> Iterator[FixtureReplayer | RecordingExecutor]:
    if os.environ.get(RECORD_ENV) == "1":
        recorder = RecordingExecutor(_synthetic)
        monkeypatch.setattr(ai, "execute_structured", recorder)
        yield recorder
        calls = tuple(recorder.calls.values())
        write_case_fixtures(FIXTURES, CaseFixtures(FIXTURE_CASE, _Case.input_digest(), {"mode": "synthetic"}, calls))
        return
    replayer = FixtureReplayer(_Case(), load_case_fixtures(FIXTURES, FIXTURE_CASE))  # type: ignore[arg-type]
    monkeypatch.setattr(ai, "execute_structured", replayer)
    yield replayer
    assert not replayer.events, f"re-record with {RECORD_ENV}=1: {[event.to_wire() for event in replayer.events]}"


def test_ai_runs_once_in_the_background_through_a_recorded_fixture(
    review: Review, replayed: FixtureReplayer | RecordingExecutor, deferred: list[Callable[[], None]]
) -> None:
    review.record("old", BASE_BODY, "2026-11-19T10:01:00Z")
    review.record("new", HEAD_BODY, "2026-11-19T10:02:00Z")
    client = review.client()
    index = client.get(review.url).json()["graph"]

    started = client.post(f"{review.url}/generate", json={"revision_id": review.revision.id, "ai": True})
    assert started.status_code == 202, started.text
    assert started.json() == {"state": "pending", "graph": index}
    assert client.get(review.url).json() == {"state": "pending", "graph": index}
    again = client.post(f"{review.url}/generate", json={"ai": True})
    assert again.status_code == 202 and len(deferred) == 1  # one enrichment per graph at a time

    deferred.pop()()
    body = client.get(review.url).json()
    assert body["state"] == "ready", body
    graph = body["graph"]
    assert graph["provenance"]["producer"] == "index+ai" and graph["provenance"]["model"] == "gpt-test"
    assert graph["title"] == "Raise the Team plan to 59 and add Enterprise"
    assert graph["provenance"]["recorded_runs"] == index["provenance"]["recorded_runs"]
    assert [e["id"] for e in graph["edges"] if e["evidence"] == "recorded"] == [
        e["id"] for e in index["edges"] if e["evidence"] == "recorded"
    ]
    added = next(flow for flow in graph["flows"] if flow["title"] == "Pricing lookup")
    assert all(step["evidence"] == "inferred" for step in added["steps"])
    assert graph["degraded"][0].startswith("Dropped ")
    _assert_contract_bounds(graph)

    reused = client.post(f"{review.url}/generate", json={"ai": True})
    assert reused.status_code == 200 and reused.json() == {"state": "ready", "graph": graph}
    assert not deferred and len(replayed.served) == 1
    runs = review.store.list_intelligence_runs(review.session.id, capability="xray_graph")
    assert [run.status for run in runs] == ["ready"]


def test_a_failed_enrichment_keeps_the_index_graph_and_says_why(
    review: Review, monkeypatch: pytest.MonkeyPatch, deferred: list[Callable[[], None]]
) -> None:
    def malformed(**kwargs: Any) -> StructuredExecution:
        return StructuredExecution({"title": "only a title"}, "openai", "gpt-test", ModelCallUsage(calls=1))

    monkeypatch.setattr(ai, "execute_structured", malformed)
    client = review.client()
    index = client.get(review.url).json()["graph"]
    assert client.post(f"{review.url}/generate", json={"ai": True}).status_code == 202
    deferred.pop()()
    body = client.get(review.url).json()
    assert body["state"] == "ready" and body["graph"] == index and body["reason"].startswith("AI enrichment failed")
