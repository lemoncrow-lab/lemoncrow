"""Behavior targets through the Review API: projection, queue, judgment, checklist, narrative.

Every behavior delta here comes from a real producer over evidence persisted the
way executors persist it (runtime validation records, browser-check reports).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pygit2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.behavior import BEHAVIOR_CONTRACT_VERSION
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.runtime_validation import (
    build_surface_validation_request,
    normalize_surface_run,
    persist_validation_record,
    surface_validation_scenario,
    trusted_policy_digest,
)
from lemoncrow.pro.capabilities.review.runtimes import SurfaceRunResult
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.surfaces import ReviewSurface, RuntimeConfigEntry

TOKEN = "behavior-token"
PORT = 45998
BASE = f"http://127.0.0.1:{PORT}"

_BASE_SOURCE = "def refresh(user):\n    return {'status': 'expired'}\n"
_CHANGED_SOURCE = "def refresh(user):\n    return {'status': 'invalid'}\n"

_CONFIG = """version: 1
browser_checks:
  trusted_origins: ["https://preview.example.test"]
  scenarios:
    - id: checkout
      title: Checkout
      route: /checkout
      paths: ["src/session.py"]
      visual_threshold: 0.05
manual_verification:
  required: true
  integrations:
    - id: stripe
      title: Stripe checkout
      paths: ["src/session.py"]
      steps: ["Pay with a test card in staging"]
"""

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


class Workspace:
    def __init__(self, client: TestClient, store: ReviewStore, store_root: Path, repo_root: Path, review_id: str):
        self.client = client
        self.store = store
        self.store_root = store_root
        self.repo_root = repo_root
        self.review_id = review_id

    @property
    def revision(self) -> ReviewRevision:
        revision = self.store.latest_revision(self.review_id)
        assert revision is not None
        return revision

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.client.get(f"/api/reviews/{self.review_id}{path}", headers={"Authorization": f"Bearer {TOKEN}"})

    def post(self, path: str, body: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        return self.client.post(
            f"/api/reviews/{self.review_id}{path}",
            headers={"Authorization": f"Bearer {TOKEN}", **kwargs.pop("headers", {})},
            json=body,
            **kwargs,
        )

    def snapshot(self, source: str) -> ReviewRevision:
        _write(self.repo_root, "src/session.py", source)
        rng = resolve_rev_range(self.repo_root, None, working_tree=True)
        build = build_review_packet_with_blobs(self.repo_root, rng, store_root=self.store_root, with_patch_text=True)
        session = self.store.get_session(self.review_id)
        assert session is not None
        snapshot_revision(self.store, session, self.repo_root, rng, store_root=self.store_root, build=build)
        self.store.publish_snapshot(self.review_id, self.revision.id)
        return self.revision


def _workspace(tmp_path: Path, *, config: str | None = _CONFIG) -> Workspace:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    repo = pygit2.init_repository(str(repo_root), initial_head="main")
    _write(repo_root, "src/session.py", _BASE_SOURCE)
    if config is not None:
        _write(repo_root, ".lemoncrow/review.yaml", config)
    repo.index.add_all()
    repo.index.write()
    signature = pygit2.Signature("Fixture", "fixture@example.invalid", 1700000000, 0)
    repo.create_commit("HEAD", signature, signature, "base", repo.index.write_tree(), [])
    _write(repo_root, "src/session.py", _CHANGED_SOURCE)
    _write(repo_root, "src/billing.py", "def charge(amount):\n    return amount\n")
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
    return Workspace(TestClient(app, base_url=BASE), store, store_root, repo_root, session.id)


def _api_run(workspace: Workspace, *, side: str, status_code: int, body: Any) -> None:
    revision = workspace.revision
    runtime = RuntimeConfigEntry(id="api", runner="http-service", values={"command": ["python", "app.py"]})
    surface = ReviewSurface(
        id="orders",
        provider="fixture",
        kind="api.request",
        title="GET /orders",
        runtime="api",
        affected_paths=("src/session.py",),
        capabilities=("source", "execute"),
        metadata={"expected_status": 200},
    )
    request = build_surface_validation_request(
        revision, surface, runtime, side=side, policy_digest=trusted_policy_digest("version: 1")  # type: ignore[arg-type]
    )
    scenario = surface_validation_scenario(surface, runtime)
    record = normalize_surface_run(
        request,
        scenario,
        SurfaceRunResult(
            surface.id,
            surface.provider,
            side,
            "passed",
            f"HTTP {status_code}",
            "http-service",
            runtime.id,
            exit_code=0,
            data={"status_code": status_code, "body": body, "assertions": [], "assertions_executed": False},
        ),
    )
    persist_validation_record(
        workspace.store, revision, request, scenario, record, path="src/session.py", source="test"
    )


def _upload(workspace: Workspace, payload: bytes) -> str:
    response = workspace.client.post(
        f"/api/reviews/{workspace.review_id}/evidence/upload",
        params={
            "kind": "screenshot",
            "title": "checkout",
            "revision_id": workspace.revision.id,
            "expected_revision_id": workspace.revision.id,
            "source": "ci",
            "mime_type": "image/png",
        },
        content=payload,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "image/png"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["evidence"]["id"])


def _browser_run(workspace: Workspace, *, side: str, screenshot: bytes, diff_ratio: float | None = None) -> None:
    body: dict[str, Any] = {
        "revision_id": workspace.revision.id,
        "scenario": "checkout",
        "route": "/checkout",
        "side": side,
        "viewport": {"width": 1280, "height": 800},
        "provider": "playwright",
        "source": "ci",
        "target_url": "http://127.0.0.1:4173/checkout",
        "completed": True,
        "navigation": {"status": 200},
        "assertions": [{"name": "total shown", "status": "PASS"}],
        "console": [],
        "artifacts": {"screenshot": _upload(workspace, screenshot)},
    }
    if diff_ratio is not None:
        body["visual"] = {"diff_ratio": diff_ratio}
    response = workspace.post("/browser-checks", body)
    assert response.status_code == 201, response.text


def _condition(workspace: Workspace, identifier: str) -> dict[str, Any] | None:
    readiness = workspace.get("/readiness").json()
    return next((row for row in readiness["conditions"] if row["id"] == identifier), None)


def _target(workspace: Workspace, behavior_key: str) -> dict[str, Any]:
    response = workspace.get(f"/behavior/targets/{behavior_key}")
    assert response.status_code == 200, response.text
    return dict(response.json()["target"])


def _judge(workspace: Workspace, *judgments: dict[str, Any]) -> Any:
    return workspace.post(
        "/behavior/marks", {"expected_revision_id": workspace.revision.id, "judgments": list(judgments)}
    )


# --------------------------------------------------------------------------- #


def test_no_behavior_block_queue_entry_or_condition_when_nothing_ran(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    projection = workspace.get("/projection").json()
    # The policy names an integration the change reaches, so only the checklist speaks.
    behavior = projection["behavior"]
    assert behavior is not None
    assert behavior["total"] == 0 and behavior["targets"] == [] and behavior["narrative"] is None
    assert behavior["execution_ran"] is False and behavior["coverage"] is None
    assert not [row for row in projection["actions"] if row["id"].startswith("behavior:")]
    assert _condition(workspace, "behavior-changes-judged") is None

    bare = _workspace(tmp_path / "bare", config=None)
    assert bare.get("/projection").json()["behavior"] is None
    assert bare.get("/behavior").json()["behavior"] is None


def test_behavior_targets_round_trip_projection_queue_judgment_and_readiness(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _api_run(workspace, side="old", status_code=200, body={"orders": [1, 2]})
    _api_run(workspace, side="new", status_code=500, body={"error": "boom"})
    _browser_run(workspace, side="old", screenshot=_PNG + b"base")
    _browser_run(workspace, side="new", screenshot=_PNG + b"head", diff_ratio=0.01)

    projection = workspace.get("/projection").json()
    behavior = projection["behavior"]
    assert (
        behavior["contract_version"] == BEHAVIOR_CONTRACT_VERSION and behavior["revision_id"] == workspace.revision.id
    )
    kinds = sorted(row["kind"] for row in behavior["targets"])
    assert kinds == ["api_response", "test_outcome", "visual"]
    # The digest travels only with the evidence: never in the projection list.
    assert all("evidence" not in row for row in behavior["targets"])
    assert behavior["progress"]["outstanding"] == 3 and behavior["progress"]["critical"] == 2
    critical = [row for row in behavior["targets"] if row["critical"]]
    visual = next(row for row in behavior["targets"] if row["kind"] == "visual")
    assert visual["bulk_eligible"] is True and all(row["bulk_eligible"] is False for row in critical)
    # Canonical queue: behavior targets are review-level entries in tier order.
    queue = [row for row in projection["actions"] if row["id"].startswith("behavior:")]
    assert [row["id"] for row in queue] == [f"behavior:{row['behavior_key']}" for row in behavior["targets"]]
    tiers = [int(row["tier"]) for row in projection["actions"]]
    assert tiers == sorted(tiers)
    # Item 12: execution ran, so the changed-but-never-executed list is stated.
    coverage = behavior["coverage"]
    assert coverage["runs"] == 4
    assert [row["path"] for row in coverage["never_executed"]] == ["src/billing.py"]
    assert behavior["narrative"]["mode"] == "deterministic"
    pending = _condition(workspace, "behavior-changes-judged")
    assert pending is not None and pending["state"] == "pending" and pending["category"] == "human_review"

    # Item 38: no digest, a wrong digest, a stale Snapshot -> refused, nothing recorded.
    key = visual["behavior_key"]
    missing = _judge(workspace, {"behavior_key": key, "state": "reviewed"})
    assert missing.status_code == 422 and "open this behavior change's evidence" in missing.text
    wrong = _judge(workspace, {"behavior_key": key, "state": "reviewed", "evidence_digest": "0" * 64})
    assert wrong.status_code == 409
    unknown = _judge(workspace, {"behavior_key": "beh:visual:nope", "state": "reviewed", "evidence_digest": "x"})
    assert unknown.status_code == 409
    stale = workspace.post(
        "/behavior/marks",
        {"expected_revision_id": "rr-other", "judgments": [{"behavior_key": key, "state": "reviewed"}]},
    )
    assert stale.status_code == 409
    assert not [mark for mark in workspace.store.list_marks(workspace.review_id) if mark.unit_key.startswith("beh:")]

    # The ordinary unit-mark doors do not accept behavior keys at all.
    assert (
        workspace.post(
            "/marks", {"unit_key": key, "state": "reviewed", "expected_revision_id": workspace.revision.id}
        ).status_code
        == 404
    )
    bulk_units = workspace.post("/marks/bulk", {"unit_keys": [key], "expected_revision_id": workspace.revision.id})
    assert bulk_units.status_code == 200 and bulk_units.json()["skipped"][0]["unit_key"] == key
    assert not [mark for mark in workspace.store.list_marks(workspace.review_id) if mark.unit_key.startswith("beh:")]

    shown = _target(workspace, key)
    evidence = shown["evidence"]
    assert evidence["head"] and evidence["refs"] and len(evidence["digest"]) == 64
    accepted = _judge(workspace, {"behavior_key": key, "state": "reviewed", "evidence_digest": evidence["digest"]})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["recorded"] == [{"behavior_key": key, "recorded": "reviewed"}]

    # Bulk: critical targets are refused individually, never swept.
    digests = {row["behavior_key"]: _target(workspace, row["behavior_key"])["evidence"]["digest"] for row in critical}
    bulk = _judge(
        workspace,
        *({"behavior_key": name, "state": "reviewed", "evidence_digest": value} for name, value in digests.items()),
    )
    assert bulk.status_code == 200, bulk.text
    assert bulk.json()["recorded"] == []
    assert {row["code"] for row in bulk.json()["refused"]} == {"critical_bulk"}
    for name, value in digests.items():
        single = _judge(workspace, {"behavior_key": name, "state": "reviewed", "evidence_digest": value})
        assert single.status_code == 200, single.text

    after = workspace.get("/behavior").json()["behavior"]
    assert after["progress"]["reviewed"] == 3 and after["progress"]["outstanding"] == 0
    done = _condition(workspace, "behavior-changes-judged")
    assert done is not None and done["state"] == "pass"
    assert not [row for row in workspace.get("/projection").json()["actions"] if row["id"].startswith("behavior:")]


def test_behavior_judgment_carries_to_a_new_snapshot_only_on_an_identical_fingerprint(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _api_run(workspace, side="old", status_code=200, body={"orders": [1]})
    _api_run(workspace, side="new", status_code=200, body={"orders": [1, 2]})
    [target] = workspace.get("/behavior").json()["behavior"]["targets"]
    assert target["kind"] == "api_response" and target["critical"] is False
    key = target["behavior_key"]
    digest = _target(workspace, key)["evidence"]["digest"]
    assert _judge(workspace, {"behavior_key": key, "state": "reviewed", "evidence_digest": digest}).status_code == 200

    # Snapshot 2 re-observes the same behavior: the judgment survives capture and holds.
    second = workspace.snapshot(_CHANGED_SOURCE + "\n# comment\n")
    assert any(mark.unit_key == key for mark in workspace.store.list_marks(workspace.review_id))
    _api_run(workspace, side="old", status_code=200, body={"orders": [1]})
    _api_run(workspace, side="new", status_code=200, body={"orders": [1, 2]})
    [carried] = workspace.get("/behavior").json()["behavior"]["targets"]
    assert carried["behavior_key"] == key
    assert carried["state"] == "reviewed" and carried["changed_since_mark"] is False
    assert carried["reviewed_revision_id"] != second.id

    # Snapshot 3 observes a different response: the same target reopens as re-review.
    workspace.snapshot(_CHANGED_SOURCE + "\n# comment 2\n")
    _api_run(workspace, side="old", status_code=200, body={"orders": [1]})
    _api_run(workspace, side="new", status_code=200, body={"orders": [1, 2, 3]})
    [reopened] = workspace.get("/behavior").json()["behavior"]["targets"]
    assert reopened["behavior_key"] == key
    assert reopened["state"] == "changed_since_review" and reopened["changed_since_mark"] is True
    assert reopened["priority"]["outstanding"] is True


def test_behavior_v1_mark_reopens_without_overwriting_its_audit_history(tmp_path: Path) -> None:
    """A previously recorded v1 judgment is not a valid current v2 approval."""
    from dataclasses import replace

    workspace = _workspace(tmp_path)
    _api_run(workspace, side="old", status_code=200, body={"orders": [1]})
    _api_run(workspace, side="new", status_code=200, body={"orders": [1, 2]})
    [target] = workspace.get("/behavior").json()["behavior"]["targets"]
    key = target["behavior_key"]
    evidence = _target(workspace, key)["evidence"]["digest"]
    assert _judge(workspace, {"behavior_key": key, "state": "reviewed", "evidence_digest": evidence}).status_code == 200
    recorded = next(mark for mark in workspace.store.list_marks(workspace.review_id) if mark.unit_key == key)
    previous = replace(recorded, content_fingerprint="v1:retained-for-audit")
    workspace.store.set_mark(previous)
    reopened = _restarted(workspace)
    [current] = reopened.get("/behavior").json()["behavior"]["targets"]
    assert current["behavior_key"] == key and current["state"] == "changed_since_review"
    assert current["priority"]["outstanding"] is True
    assert (
        next(mark for mark in reopened.store.list_marks(reopened.review_id) if mark.unit_key == key).content_fingerprint
        == previous.content_fingerprint
    )
    assert (
        _judge(
            reopened, {"behavior_key": key, "state": "reviewed", "evidence_digest": "v1:old-displayed-proof"}
        ).status_code
        == 409
    )
    assert _judge(reopened, {"behavior_key": key, "state": "reviewed", "evidence_digest": evidence}).status_code == 200
    [after] = reopened.get("/behavior").json()["behavior"]["targets"]
    assert after["state"] == "reviewed"


def _restarted(workspace: Workspace) -> Workspace:
    """A new server process over the same durable store root (nothing carried in memory)."""

    store = ReviewStore(workspace.store_root)
    app = FastAPI()
    register_review_api(
        app, store, auth_dependency=make_token_dependency(TOKEN), repo_root=workspace.repo_root, port=PORT
    )
    return Workspace(
        TestClient(app, base_url=BASE), store, workspace.store_root, workspace.repo_root, workspace.review_id
    )


def test_behavior_and_unit_marks_survive_a_server_restart(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _api_run(workspace, side="old", status_code=200, body={"orders": [1]})
    _api_run(workspace, side="new", status_code=200, body={"orders": [1, 2]})
    [target] = workspace.get("/behavior").json()["behavior"]["targets"]
    key = target["behavior_key"]
    assert key.startswith("beh:")
    digest = _target(workspace, key)["evidence"]["digest"]
    assert _judge(workspace, {"behavior_key": key, "state": "reviewed", "evidence_digest": digest}).status_code == 200
    units = workspace.get("/targets").json()["targets"]
    for row in units:
        marked = workspace.post(
            "/marks", {"unit_key": row["unit_key"], "state": "reviewed", "expected_revision_id": workspace.revision.id}
        )
        assert marked.status_code == 200, marked.text
    before_targets = {row["unit_key"]: row["state"] for row in workspace.get("/targets").json()["targets"]}
    before = workspace.get("/readiness").json()

    restarted = _restarted(workspace)
    after_targets = {row["unit_key"]: row["state"] for row in restarted.get("/targets").json()["targets"]}
    [behavior] = restarted.get("/behavior").json()["behavior"]["targets"]
    after = restarted.get("/readiness").json()

    assert after_targets == before_targets and set(after_targets.values()) == {"reviewed"}
    assert behavior["behavior_key"] == key and behavior["state"] == "reviewed"
    assert next(row for row in after["conditions"] if row["id"] == "behavior-changes-judged")["state"] == "pass"
    assert next(row for row in after["conditions"] if row["id"] == "latest-snapshot-reviewed")["state"] == "pass"
    assert after["conditions_hash"] == before["conditions_hash"]


def test_manual_checklist_result_is_human_observation_whatever_the_body_claims(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    checklist = workspace.get("/projection").json()["behavior"]["checklist"]
    assert checklist["required"] is True and checklist["attestation_class"] == "human_observation"
    [item] = checklist["items"]
    assert item["item_id"] == "manual:stripe" and item["result"] is None
    assert item["steps"] == ["Pay with a test card in staging"]

    def record(**body: Any) -> Any:
        return workspace.post(
            "/behavior/checklist", {"expected_revision_id": workspace.revision.id, "item_id": "manual:stripe", **body}
        )

    gate = _condition(workspace, "manual-verification")
    assert gate is not None and gate["state"] == "pending" and gate["category"] == "verification"
    assert record(status="MAYBE").status_code == 422
    assert record(item_id="manual:unknown", status="PASS").status_code == 404
    assert record(status="FAIL", note="Card was declined").status_code == 201
    failing = _condition(workspace, "manual-verification")
    assert failing is not None and failing["state"] == "fail"
    spoof = record(
        status="pass",
        note="Charged a test card; receipt arrived.",
        source="test",
        attestation_class="lemoncrow_verification",
        stable_key="surface:fixture:orders",
        title="GET /orders",
    )
    assert spoof.status_code == 201, spoof.text
    stored = spoof.json()["evidence"]
    row = next(item for item in workspace.store.list_evidence(workspace.review_id) if item.id == stored["id"])
    assert (row.attestation_class, row.source, row.kind) == ("human_observation", "human", "verification")
    assert row.stable_key == "manual:stripe" and row.title == "Manual verification · Stripe checkout"
    assert row.attested_by and row.verification_status == "PASS"
    result = spoof.json()["checklist"]["items"][0]["result"]
    assert result["label"] == "Human observation" and result["status"] == "PASS"
    # In the Checks list it is typed as what it is, never a LemonCrow-executed result.
    checks = workspace.get("/verification").json()["checks"]
    manual = next(row for row in checks if row["name"] == "Manual verification · Stripe checkout")
    assert manual["attestation_class"] == "human_observation" and manual["source"] == "human"
    # The newest observation is the result; the required checklist is now satisfied.
    passed = _condition(workspace, "manual-verification")
    assert passed is not None and passed["state"] == "pass"


def test_manual_checklist_is_optional_unless_the_trusted_base_requires_it(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, config=_CONFIG.replace("required: true", "required: false"))
    checklist = workspace.get("/projection").json()["behavior"]["checklist"]
    assert checklist["required"] is False and checklist["items"][0]["required"] is False
    assert _condition(workspace, "manual-verification") is None

    broken = _workspace(tmp_path / "broken", config=_CONFIG.replace("integrations:", "integrations: oops\n  x:"))
    error = broken.get("/projection").json()["behavior"]["checklist"]["policy_error"]
    assert "manual_verification" in error
    gate = _condition(broken, "manual-verification")
    assert gate is not None and gate["state"] == "pending"


class _NarrativeModel:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, messages: Any, *, model: Any = None, json_schema: Any = None, backend: Any = None) -> Any:
        del model, json_schema, backend
        self.calls += 1
        payload = json.loads(messages[-1]["content"])
        return {
            "statements": [
                {
                    "text": f"{row['title']}: {row['before']} before, {row['after']} after.",
                    "behavior_keys": [row["behavior_key"]],
                    "evidence_refs": [row["evidence"][0]["ref"]],
                }
                for row in payload["deltas"]
            ]
            + [{"text": "Invented.", "behavior_keys": ["beh:visual:x"], "evidence_refs": ["artifact:x"]}]
        }


def _enable_model(monkeypatch: pytest.MonkeyPatch, model: Any) -> None:
    import lemoncrow.infra.internal_llm as internal_llm
    from lemoncrow.infra.internal_llm import openai_client
    from lemoncrow.infra.internal_llm.result import InternalLLMChatResult

    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    monkeypatch.setenv("LEMONCROW_OPENAI_MODEL", "fixture-model")
    monkeypatch.setattr(internal_llm, "chat", model)

    def structured(messages: Any, *, model: str | None = None, json_schema: Any = None, **_kwargs: Any) -> Any:
        parsed = model_callable(messages, model=model, json_schema=json_schema, backend="openai")
        return InternalLLMChatResult(
            content=json.dumps(parsed),
            parsed_json=parsed,
            model=model or "fixture-model",
            request_id="fixture-request",
            input_tokens=7,
            output_tokens=3,
        )

    model_callable = model
    monkeypatch.setattr(openai_client, "chat_with_result", structured)


def _narrate(workspace: Workspace) -> Any:
    return workspace.post("/behavior/narrative", {"expected_revision_id": workspace.revision.id})


def test_behavior_narrative_is_one_cached_call_citing_typed_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    _api_run(workspace, side="old", status_code=200, body={"orders": [1]})
    _api_run(workspace, side="new", status_code=500, body={"error": "boom"})
    model = _NarrativeModel()
    _enable_model(monkeypatch, model)

    first = _narrate(workspace)
    assert first.status_code == 200, first.text
    narrative = first.json()["narrative"]
    assert narrative["mode"] == "inferred" and model.calls == 1
    assert narrative["synthesis"]["status"] == "ready"
    targets = {row["behavior_key"] for row in workspace.get("/behavior").json()["behavior"]["targets"]}
    for statement in narrative["statements"]:
        assert statement["origin"] == "inferred"
        assert set(statement["behavior_keys"]) <= targets
        assert all(ref.startswith(("artifact:", "check:")) for ref in statement["evidence_refs"])
    assert "Invented." not in [row["text"] for row in narrative["statements"]]
    # The deterministic list always travels beside it.
    assert len(narrative["deterministic"]) == 2

    # Cached: the projection overlays it and a second request spends nothing.
    cached = workspace.get("/projection").json()["behavior"]["narrative"]
    assert cached["mode"] == "inferred" and cached["synthesis"]["mode"] == "cached_synthesis"
    assert _narrate(workspace).json()["narrative"]["mode"] == "inferred"
    assert model.calls == 1


def test_behavior_narrative_falls_back_when_ai_is_off_denied_or_over_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    _api_run(workspace, side="old", status_code=200, body={"orders": [1]})
    _api_run(workspace, side="new", status_code=500, body={"error": "boom"})

    def assert_deterministic(narrative: dict[str, Any]) -> None:
        assert narrative["mode"] == "deterministic"
        assert narrative["statements"] == narrative["deterministic"] and len(narrative["statements"]) == 2
        for row in narrative["statements"]:
            assert row["origin"] == "deterministic" and row["evidence_refs"]
            assert "PASS" not in row["text"] and "verified" not in row["text"].lower()

    model = _NarrativeModel()
    # Over budget: no call is made.
    monkeypatch.setenv("LEMONCROW_REVIEW_INTELLIGENCE_MAX_CALLS", "0")
    _enable_model(monkeypatch, model)
    over = _narrate(workspace).json()["narrative"]
    assert_deterministic(over)
    assert over["synthesis"]["mode"] == "skipped_by_budget" and model.calls == 0

    # Denied: the only behavior paths are excluded from model access.
    denied = _workspace(
        tmp_path / "denied",
        config=_CONFIG + "ai_content_access:\n  - mode: ai_denied\n    paths: [src/session.py]\n",
    )
    monkeypatch.setenv("LEMONCROW_REVIEW_INTELLIGENCE_MAX_CALLS", "20")
    _api_run(denied, side="old", status_code=200, body={"orders": [1]})
    _api_run(denied, side="new", status_code=500, body={"error": "boom"})
    refused = _narrate(denied).json()["narrative"]
    assert_deterministic(refused)
    assert refused["synthesis"]["candidates"] == 0 and model.calls == 0

    # Off: no model configured at all.
    monkeypatch.delenv("LEMONCROW_LLM_BACKEND", raising=False)
    monkeypatch.delenv("LEMONCROW_OPENAI_MODEL", raising=False)
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "none")
    off = _narrate(workspace).json()["narrative"]
    assert_deterministic(off)
    assert model.calls == 0
