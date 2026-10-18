"""PK2: Review, Ask, AI Review and Map consumption of Project Knowledge.

Project Knowledge is landed canonical-branch history. These tests pin the
product contract: without it Review is unchanged; with it every entry is
bounded, labeled with its state and freshness, linked to the history it came
from, filtered by AI content access before any model sees it, never usable as
finding evidence, and never a reason for a read to start background work.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.pro.capabilities.review.ai_review import HYPOTHESIS_SCHEMA, run_independent_review
from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.intelligence_runtime import StructuredExecution
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.project_context import (
    BOUNDS,
    CONTEXT_CLASSES,
    ProjectContextBounds,
    ProjectContextRequest,
    ProjectKnowledgeBinding,
    question_terms,
    retrieve_project_context,
)
from lemoncrow.pro.capabilities.review.project_knowledge import (
    CanonicalAdvanceProof,
    CanonicalRefIdentity,
    KnowledgeFact,
    KnowledgeSubject,
    ProjectKnowledgeStore,
    RepositoryGeneration,
    RepositoryIdentity,
    build_baseline_projection,
    build_merged_change_projection,
)
from lemoncrow.pro.capabilities.review.rules import ContentAccessRule
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_revision_id
from tests.gateway.test_review_ai_review import _execution, _store, _target
from tests.gateway.test_review_api import (
    _BASE_SOURCE,
    _CHANGED_SOURCE,
    BASE,
    PORT,
    TOKEN,
    Workspace,
    _commit,
    _init_repo,
    _write,
)

REPO = RepositoryIdentity("local", "acme/widgets")
_POLICY = """version: 1
ai_content_access:
  - mode: ai_denied
    paths: [src/vault/keys.py]
"""
_SECRET = "vault-secret-rotation-body"
_BASE_REVISION = "2" * 40


def _gen(char: str, repo: RepositoryIdentity = REPO) -> RepositoryGeneration:
    return RepositoryGeneration(CanonicalRefIdentity(repo, "refs/heads/main"), char * 40)


def _path_fact(
    path: str,
    body: str,
    *,
    kind: str = "responsibility",
    state: str = "current",
    char: str = "a",
    support: str = "deterministic",
) -> KnowledgeFact:
    return KnowledgeFact(
        subject=KnowledgeSubject("path", path),
        knowledge_kind=kind,
        body=body,
        source_refs=(f"source:new:{path}@sha256:{char * 64}",),
        state=state,  # type: ignore[arg-type]
        support=support,  # type: ignore[arg-type]
    )


def _rule(mode: str) -> ContentAccessRule:
    return ContentAccessRule(
        mode=mode,  # type: ignore[arg-type]
        path_scope=("src/vault/keys.py",),
        origin="base Snapshot .lemoncrow/review.yaml",
        trust_class="trusted_base_repository",
        source_identity="base:review.yaml",
    )


_PROOF = CanonicalAdvanceProof(authority="provider_observed", observed_ref="refs/heads/main", descendant=True)


def _seed(knowledge: ProjectKnowledgeStore, landed_review_id: str) -> None:
    g1, g2 = _gen("1"), _gen("2")
    assert knowledge.apply(
        build_baseline_projection(
            g1,
            facts=(
                _path_fact("src/session.py", "Session refresh rejects expired tokens."),
                _path_fact("src/session.py", "Committed path src/session.py; Git blob abc.", kind="inventory"),
                _path_fact(
                    "src/README.md",
                    "The src package hosts session management.",
                    kind="documentation",
                    support="source_document",
                ),
                _path_fact("src/vault/keys.py", f"Vault key rotation lives here: {_SECRET}."),
            ),
            observed_at="2026-10-01T10:00:00Z",
        )
    )
    assert knowledge.apply(
        build_merged_change_projection(
            g1,
            g2,
            proof=_PROOF,
            merge_ref="merge:local:acme/widgets#7",
            review_ref=f"review:{landed_review_id}",
            revision_ref="revision:rv-not-held",
            facts=(
                _path_fact("src/session.py", "Session refresh rejects expired and revoked tokens.", char="b"),
                _path_fact(
                    "src/session.py",
                    "Change 7 added revoked-token rejection to session refresh.",
                    kind="change",
                    state="historical",
                    char="b",
                ),
                _path_fact("src/session.py", "Refresh may race with logout.", kind="risk", state="uncertain", char="b"),
            ),
            observed_at="2026-10-02T10:00:00Z",
        )
    )


def _client(
    workspace: Workspace,
    *,
    binding: Any = None,
    answerer: Any = None,
    ai_review_submitter: Any = None,
) -> Workspace:
    store = ReviewStore(workspace.store_root)
    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=workspace.repo_root,
        port=PORT,
        ask_answerer=answerer,
        ai_review_submitter=ai_review_submitter,
        project_knowledge=binding,
    )
    return Workspace(
        TestClient(app, base_url=BASE), store, workspace.store_root, workspace.repo_root, workspace.review_id
    )


@pytest.fixture
def base(tmp_path: Path) -> tuple[Workspace, ProjectKnowledgeStore, str]:
    repo_root = tmp_path / "repo"
    repo = _init_repo(repo_root)
    _write(repo_root, "src/session.py", _BASE_SOURCE)
    _write(repo_root, "src/vault/keys.py", "KEYS = ()\n")
    _write(repo_root, ".lemoncrow/review.yaml", _POLICY)
    _commit(repo, "base", 0)
    _write(repo_root, "src/session.py", _CHANGED_SOURCE)
    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, None, working_tree=True)
    build = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, repo_root, rng, title=build.packet.title)
    snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)
    revision = store.latest_revision(session.id)
    assert revision is not None
    store.publish_snapshot(session.id, revision.id)
    # The Change's Git base is the canonical commit PK0 recorded as generation 2.
    store.update_session(session.id, review_base_sha=_BASE_REVISION)
    landed = store.create_session(
        ReviewSession(
            id="",
            subject_type="local_change",
            repo_root=session.repo_root,
            range_mode="working_tree",
            status="finished",
            title="Reject revoked tokens",
        )
    )
    store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=landed.id,
            revision_number=0,
            range_mode="working_tree",
            tree_fingerprint="tree-landed",
            source_fingerprint="source-landed",
            packet_schema_version=1,
        ),
        [],
    )
    knowledge = ProjectKnowledgeStore(store)
    _seed(knowledge, landed.id)
    workspace = Workspace(None, store, store_root, repo_root, session.id)  # type: ignore[arg-type]
    return workspace, knowledge, landed.id


def _binding(knowledge: ProjectKnowledgeStore, repo: RepositoryIdentity = REPO) -> Any:
    return lambda _session: ProjectKnowledgeBinding(knowledge, repo)


def _answer(contexts: list[dict[str, Any]]) -> Any:
    def answer(context: dict[str, Any]) -> dict[str, str]:
        contexts.append(json.loads(json.dumps(context)))
        return {
            "intent": "question",
            "body": "Earlier on main refresh already rejected revoked tokens.",
            "handoff_instruction": "",
        }

    return answer


# ---------------------------------------------------------------- adapter contract


def _brief(knowledge: ProjectKnowledgeStore, **kwargs: Any) -> Any:
    request = ProjectContextRequest(
        purpose=kwargs.pop("purpose", "brief"),
        changed_paths=("src/session.py",),
        paths=kwargs.pop("paths", ("src/session.py",)),
        **{"base_revision": _BASE_REVISION, **kwargs.pop("request", {})},
    )
    return retrieve_project_context(ProjectKnowledgeBinding(knowledge, REPO), request, **kwargs)


def test_without_a_binding_or_canonical_history_project_context_is_absent(tmp_path: Path) -> None:
    request = ProjectContextRequest(purpose="ask", paths=("src/a.py",), terms=("refresh",))
    missing = retrieve_project_context(None, request)
    assert (missing.available, missing.reason, missing.entries, missing.model_view()) == (
        False,
        "not_configured",
        (),
        {},
    )
    empty = retrieve_project_context(ProjectKnowledgeBinding(ProjectKnowledgeStore(tmp_path / "pk.db"), REPO), request)
    assert (empty.available, empty.reason, empty.model_view()) == (False, "no_canonical_generation", {})
    assert missing.to_wire()["entries"] == [] and empty.to_wire()["entries"] == []


def test_states_and_freshness_are_labeled_accurately(base: tuple[Workspace, ProjectKnowledgeStore, str]) -> None:
    _workspace, knowledge, _landed = base
    brief = _brief(knowledge)
    rows = {row.entry.knowledge_kind: row for row in brief.entries}
    # inventory restates the Snapshot: not shown on Brief; superseded never outside the Map
    assert set(rows) == {"responsibility", "change", "risk", "documentation"}
    current = rows["responsibility"]
    assert current.entry.body == "Session refresh rejects expired and revoked tokens."
    assert current.state_label == "Landed on main"
    assert current.stale_for_snapshot is True and current.relation == "predates"
    assert current.freshness_label == "Landed on main · predates this Change, which edits src/session.py"
    assert rows["change"].state_label == "Historical on main" and rows["change"].stale_for_snapshot is False
    assert rows["risk"].state_label.startswith("Uncertain") and rows["risk"].stale_for_snapshot is True
    assert rows["documentation"].relevance == "neighborhood"
    assert rows["documentation"].stale_for_snapshot is False
    assert all(row.context_class == "project_knowledge" for row in brief.entries)
    assert brief.to_wire()["narration"] == []

    trail = _brief(knowledge, purpose="map")
    superseded = [row for row in trail.entries if row.entry.state == "superseded"]
    assert [row.entry.body for row in superseded] == ["Session refresh rejects expired tokens."]
    assert superseded[0].state_label == "Superseded on main"
    assert superseded[0].stale_for_snapshot is False
    assert superseded[0].origin is not None and superseded[0].origin["kind"] == "baseline"
    assert superseded[0].superseded_by is not None and superseded[0].superseded_by["kind"] == "merged_change"
    assert any(row.entry.knowledge_kind == "inventory" for row in trail.entries)


def test_freshness_is_ordered_by_the_generation_chain_never_assumed(
    base: tuple[Workspace, ProjectKnowledgeStore, str],
) -> None:
    """'predates' needs PK0's chain to put the entry at or before the Change's base."""

    _workspace, knowledge, landed = base

    def current_of(**request: Any) -> Any:
        return next(
            row for row in _brief(knowledge, request=request).entries if row.entry.knowledge_kind == "responsibility"
        )

    # base = generation 1: the generation-2 entry landed after the Change's base
    after = current_of(base_revision="1" * 40)
    assert after.relation == "after_base" and after.stale_for_snapshot is False
    assert after.freshness_label == "Landed on main · landed after this Change's base, which edits src/session.py"
    # unknown base (never observed by PK0) or no base at all: order unknowable -> Uncertain, never predates
    for request in ({"base_revision": "9" * 40}, {"base_revision": ""}):
        unknown = current_of(**request)
        assert unknown.relation == "unknown_order" and unknown.stale_for_snapshot is False
        assert unknown.freshness_label.startswith("Uncertain order")
        assert "predates this Change" not in unknown.freshness_label
        assert unknown.model_view()["predates_this_change"] is False
    # history produced by this Review's own landing is from this Change
    own = current_of(own_refs=(f"review:{landed}",))
    assert own.relation == "this_change"
    assert own.freshness_label == "Landed on main · from this Change's own landing"
    assert own.model_view()["relation_to_change"] == "this_change"


def test_citations_resolve_to_the_source_history(base: tuple[Workspace, ProjectKnowledgeStore, str]) -> None:
    _workspace, knowledge, landed = base
    held = {landed}
    brief = _brief(
        knowledge,
        resolve_review=lambda kind, value: value if kind == "review" and value in held else "",
    )
    landed_row = next(row for row in brief.entries if row.entry.knowledge_kind == "change")
    wire = landed_row.to_wire()
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for citation in wire["citations"]:
        by_kind.setdefault(citation["kind"], []).append(citation)
    generation = by_kind["generation"][0]
    assert generation["resolved"] is True and generation["generation_id"] == _gen("2").id
    assert generation["label"].startswith("Landed Change · 2026-10-02")
    assert by_kind["review"] == [
        {
            "ref": f"review:{landed}",
            "kind": "review",
            "resolved": True,
            "review_id": landed,
            "label": f"Review {landed[:12]}",
        }
    ]
    assert by_kind["revision"][0]["resolved"] is False
    assert by_kind["source"][0]["path"] == "src/session.py"
    assert wire["history"]["origin"]["kind"] == "merged_change"
    assert wire["history"]["origin"]["generation_id"] == _gen("2").id
    # model view drops raw provenance refs so a model cannot cite them as evidence
    model = json.dumps(landed_row.model_view())
    assert "source:new:" not in model and "generation:" not in model and f"review:{landed}" not in model


def test_retrieval_is_bounded_by_count_chars_and_tokens(tmp_path: Path) -> None:
    knowledge = ProjectKnowledgeStore(tmp_path / "pk.db")
    paths = tuple(f"src/m{index:02d}.py" for index in range(40))
    knowledge.apply(
        build_baseline_projection(
            _gen("1"),
            facts=tuple(
                _path_fact(path, f"Module {path} " + ("owns a long responsibility. " * 80), kind=f"k{index}")
                for index, path in enumerate(paths)
            ),
            observed_at="2026-10-01T10:00:00Z",
        )
    )
    binding = ProjectKnowledgeBinding(knowledge, REPO)
    for purpose, limits in BOUNDS.items():
        result = retrieve_project_context(
            binding,
            ProjectContextRequest(purpose=purpose, changed_paths=paths, paths=paths, terms=("module", "owns", "long")),  # type: ignore[arg-type]
        )
        assert 0 < len(result.entries) <= limits.max_entries
        assert result.used_chars <= limits.max_chars and result.used_tokens <= limits.max_tokens
        assert all(len(row.summary) <= limits.max_entry_chars for row in result.entries)
        assert all(row.summary_truncated for row in result.entries)
        assert result.queries <= limits.max_queries
        assert result.omitted > 0 and result.truncated is True
        wire = result.to_wire()
        assert wire["used"]["entries"] == len(result.entries) and wire["bounds"] == limits.to_wire()
    tight = ProjectContextBounds(max_entries=10, max_chars=300, max_tokens=4000, max_entry_chars=120)
    clipped = retrieve_project_context(binding, ProjectContextRequest(purpose="ask", paths=paths), bounds=tight)
    # each entry costs its 120-char summary + subject key + source path
    assert len(clipped.entries) == 2 and clipped.used_chars <= 300
    by_tokens = ProjectContextBounds(max_entries=10, max_chars=16000, max_tokens=150, max_entry_chars=120)
    token_clipped = retrieve_project_context(
        binding, ProjectContextRequest(purpose="brief", paths=paths), bounds=by_tokens
    )
    assert 0 < len(token_clipped.entries) < 10 and token_clipped.used_tokens <= 150
    by_count = ProjectContextBounds(max_entries=3, max_chars=16000, max_tokens=4000, max_entry_chars=80)
    count_clipped = retrieve_project_context(
        binding, ProjectContextRequest(purpose="brief", paths=paths), bounds=by_count
    )
    assert len(count_clipped.entries) == 3 and count_clipped.omitted > 0 and count_clipped.truncated is True
    with pytest.raises(ValueError):
        ProjectContextBounds(max_entries=21, max_chars=100, max_tokens=100)


def test_model_purposes_exclude_denied_knowledge_before_returning_it(
    base: tuple[Workspace, ProjectKnowledgeStore, str],
) -> None:
    _workspace, knowledge, _landed = base
    denied = (_rule("ai_denied"),)
    local_only = (_rule("customer_local_only"),)
    request = {"terms": ("rotation",)}
    vault = ("src/vault/keys.py",)
    for purpose in ("ask", "ai_review"):
        result = _brief(knowledge, purpose=purpose, paths=vault, request=request, content_access=denied)
        assert _SECRET not in json.dumps(result.model_view())
        assert result.filtered_by_content_access >= 1 and result.content_access_applied is True
        external = _brief(
            knowledge, purpose=purpose, paths=vault, request=request, content_access=local_only, route_trust="external"
        )
        assert _SECRET not in json.dumps(external.to_wire())
        local = _brief(
            knowledge,
            purpose=purpose,
            paths=vault,
            request=request,
            content_access=local_only,
            route_trust="customer_local",
        )
        assert _SECRET in json.dumps(local.model_view())
    # A person who can open the repository reads its history; the AI-route policy is not applied.
    human = _brief(knowledge, purpose="map", paths=vault, content_access=denied)
    assert _SECRET in json.dumps(human.to_wire()) and human.content_access_applied is False


def test_broken_knowledge_degrades_and_never_raises(base: tuple[Workspace, ProjectKnowledgeStore, str]) -> None:
    _workspace, knowledge, _landed = base

    class Broken(ProjectKnowledgeStore):
        def query(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("disk gone")

    result = retrieve_project_context(
        ProjectKnowledgeBinding(Broken(knowledge._review_store), REPO),  # type: ignore[arg-type]
        ProjectContextRequest(purpose="ask", paths=("src/session.py",)),
    )
    assert (result.available, result.reason, result.entries) == (False, "unavailable: RuntimeError", ())


def test_question_terms_are_bounded_and_distinctive() -> None:
    assert question_terms("Why does this change touch session refresh and token rotation?") == (
        "rotation",
        "refresh",
        "session",
    )
    assert question_terms("what is this?") == ()
    assert len(question_terms("alpha " * 10_000 + "zzzzzzzz")) <= 3


# ---------------------------------------------------------------- Review API


def test_no_knowledge_leaves_brief_ask_and_project_context_unchanged(
    base: tuple[Workspace, ProjectKnowledgeStore, str], tmp_path: Path
) -> None:
    workspace, _knowledge, _landed = base
    plain_contexts: list[dict[str, Any]] = []
    empty_contexts: list[dict[str, Any]] = []
    plain = _client(workspace, answerer=_answer(plain_contexts))
    empty = _client(
        workspace,
        binding=_binding(ProjectKnowledgeStore(tmp_path / "empty.db")),
        answerer=_answer(empty_contexts),
    )
    review = f"/api/reviews/{workspace.review_id}"
    plain_overview = plain.get(review).json()
    assert plain_overview == empty.get(review).json()

    for client, contexts in ((plain, plain_contexts), (empty, empty_contexts)):
        conversation = client.post(f"{review}/ask/conversations", json={"title": "pk2"})
        assert conversation.status_code in {200, 201}, conversation.text
        asked = client.post(
            f"{review}/ask",
            json={
                "revision_number": 0,
                "body": "Why does session refresh change?",
                "conversation_id": conversation.json()["id"],
            },
        )
        assert asked.status_code == 201, asked.text
        assert all(ref["kind"] != "project_knowledge" for ref in asked.json()["assistant"]["references"])
        assert "project_knowledge" not in contexts[-1] and "context_classes" not in contexts[-1]
    assert json.dumps(plain_contexts[0], sort_keys=True) == json.dumps(empty_contexts[0], sort_keys=True)

    for client, reason in ((plain, "not_configured"), (empty, "no_canonical_generation")):
        context = client.get(f"{review}/project-context").json()["project_context"]
        assert (context["available"], context["reason"], context["entries"]) == (False, reason, [])


def test_brief_and_map_project_context_are_labeled_linked_and_bounded(
    base: tuple[Workspace, ProjectKnowledgeStore, str],
) -> None:
    workspace, knowledge, landed = base
    client = _client(workspace, binding=_binding(knowledge))
    review = f"/api/reviews/{workspace.review_id}"
    payload = client.get(f"{review}/project-context").json()
    context = payload["project_context"]
    latest = workspace.store.latest_revision(workspace.review_id)
    assert latest is not None and payload["revision_number"] == latest.revision_number
    assert context["available"] is True and context["authority"] == "advisory"
    assert len(context["entries"]) <= BOUNDS["brief"].max_entries
    current = next(row for row in context["entries"] if row["knowledge_kind"] == "responsibility")
    assert current["stale_for_snapshot"] is True and "predates this Change" in current["freshness_label"]
    change = next(row for row in context["entries"] if row["knowledge_kind"] == "change")
    review_links = [row for row in change["citations"] if row.get("review_id")]
    assert [row["review_id"] for row in review_links] == [landed]
    # the linked Review actually opens
    assert client.get(f"/api/reviews/{landed}").status_code == 200
    revision_ref = next(row for row in change["citations"] if row["kind"] == "revision")
    assert revision_ref == {
        "ref": "revision:rv-not-held",
        "kind": "revision",
        "resolved": False,
        "label": "Revision not held in this workspace",
    }

    mapped = client.get(f"{review}/project-context", params={"purpose": "map", "path": "src/session.py"}).json()
    states = {row["state"] for row in mapped["project_context"]["entries"]}
    assert "superseded" in states
    assert (
        client.get(f"{review}/project-context", params={"purpose": "map", "path": "../etc/passwd"}).status_code == 400
    )
    assert client.get(f"{review}/project-context", params={"purpose": "ask"}).status_code == 400
    many = [("path", f"src/{index}.py") for index in range(17)]
    assert client.get(f"{review}/project-context", params=[("purpose", "map"), *many]).status_code == 400
    assert client.client.get(f"{review}/project-context").status_code in {401, 403}


def test_history_links_only_resolve_to_reviews_this_surface_can_open(
    base: tuple[Workspace, ProjectKnowledgeStore, str],
) -> None:
    workspace, knowledge, _landed = base
    foreign = workspace.store.create_session(
        ReviewSession(
            id="",
            subject_type="local_change",
            repo_root=str(workspace.repo_root.parent / "elsewhere"),
            range_mode="working_tree",
            status="finished",
            title="Another repository",
        )
    )
    assert knowledge.apply(
        build_merged_change_projection(
            _gen("2"),
            _gen("3"),
            proof=_PROOF,
            merge_ref="merge:local:acme/widgets#8",
            review_ref=f"review:{foreign.id}",
            revision_ref="revision:rv-other",
            facts=(
                _path_fact(
                    "src/billing.py", "Change 8 moved billing retries.", kind="change", state="historical", char="c"
                ),
            ),
            observed_at="2026-10-03T10:00:00Z",
        )
    )
    client = _client(workspace, binding=_binding(knowledge))
    assert client.get(f"/api/reviews/{foreign.id}").status_code == 404
    mapped = client.get(
        f"/api/reviews/{workspace.review_id}/project-context", params={"purpose": "map", "path": "src/billing.py"}
    ).json()
    row = next(row for row in mapped["project_context"]["entries"] if row["subject"]["key"] == "src/billing.py")
    review_ref = next(citation for citation in row["citations"] if citation["kind"] == "review")
    # a Review from another repository is never linked or revealed
    assert review_ref["resolved"] is False and "review_id" not in review_ref


def test_ask_separates_context_classes_and_excludes_denied_knowledge(
    base: tuple[Workspace, ProjectKnowledgeStore, str],
) -> None:
    workspace, knowledge, landed = base
    contexts: list[dict[str, Any]] = []
    client = _client(workspace, binding=_binding(knowledge), answerer=_answer(contexts))
    asked = client.post(
        f"/api/reviews/{workspace.review_id}/ask",
        json={"revision_number": 0, "body": "Why does session refresh reject revoked tokens, and what about rotation?"},
    )
    assert asked.status_code == 201, asked.text
    context = contexts[-1]
    assert (
        set(context["context_classes"])
        == set(CONTEXT_CLASSES)
        == {
            "current_snapshot",
            "current_index",
            "project_knowledge",
            "ai_narration",
        }
    )
    knowledge_block = context["project_knowledge"]
    assert knowledge_block["context_class"] == "project_knowledge"
    assert "never cite it as evidence" in knowledge_block["authority"].lower()
    assert knowledge_block["ai_narration"] == []
    assert {row["knowledge_kind"] for row in knowledge_block["historical"]} >= {"responsibility", "change"}
    assert knowledge_block["filtered_by_content_access"] >= 1
    assert _SECRET not in json.dumps(context)
    references = [row for row in asked.json()["assistant"]["references"] if row["kind"] == "project_knowledge"]
    assert references and all(row["exactness"] == "historical" for row in references)
    assert any(row.get("review_id") == landed for row in references)
    assert all("Snapshot" not in row["freshness_label"] or "predates" in row["freshness_label"] for row in references)


_TABLES = (
    "review_intelligence_runs",
    "review_intelligence_spend",
    "review_plan_syntheses",
    "review_synthesis_spend",
    "review_hosted_actions",
    "project_knowledge_entries",
    "project_knowledge_events",
)


def _counts(store_root: Path) -> dict[str, int]:
    with sqlite3.connect(store_root / "lemoncrow_reviews.db") as conn:
        return {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in _TABLES}


def test_opening_brief_and_map_history_starts_no_background_work(
    base: tuple[Workspace, ProjectKnowledgeStore, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, knowledge, _landed = base
    from lemoncrow.pro.capabilities.review import intelligence_runtime

    def no_model(**_: Any) -> Any:
        raise AssertionError("reading project context must not call a model")

    submitted: list[Any] = []
    monkeypatch.setattr(intelligence_runtime, "execute_structured", no_model)
    client = _client(workspace, binding=_binding(knowledge), ai_review_submitter=lambda *a, **k: submitted.append(a))
    review = f"/api/reviews/{workspace.review_id}"
    before = _counts(workspace.store_root)
    digest = knowledge.deterministic_digest(REPO)
    for _ in range(3):  # mount, remount, remount
        assert client.get(f"{review}/project-context").status_code == 200
        assert (
            client.get(f"{review}/project-context", params={"purpose": "map", "path": "src/session.py"}).status_code
            == 200
        )
        assert client.get(review).status_code == 200
    assert _counts(workspace.store_root) == before
    assert knowledge.deterministic_digest(REPO) == digest
    assert submitted == []


# ---------------------------------------------------------------- AI Review


def test_ai_review_uses_history_only_as_a_hypothesis_seed_never_as_evidence(tmp_path: Path) -> None:
    store, revision = _store(tmp_path / "ai")
    knowledge = ProjectKnowledgeStore(store)
    knowledge.apply(
        build_baseline_projection(
            _gen("1"),
            facts=(
                _path_fact("src/auth.py", "Authorization once allowed anonymous admins; reverted in change 12."),
                _path_fact("src/vault/keys.py", _SECRET),
            ),
            observed_at="2026-10-01T10:00:00Z",
        )
    )
    binding = ProjectKnowledgeBinding(knowledge, REPO)
    denied = (_rule("ai_denied"),)
    loaded: list[str] = []

    def loader(route_trust: Any, targets: Any) -> dict[str, Any]:
        loaded.append(route_trust)
        return retrieve_project_context(
            binding,
            ProjectContextRequest(
                purpose="ai_review",
                changed_paths=tuple(target.path for target in targets),
                paths=(*(target.path for target in targets), "src/vault/keys.py"),
            ),
            content_access=denied,
            route_trust=route_trust,
        ).model_view()

    prompts: list[dict[str, Any]] = []
    knowledge_ids: list[str] = []

    def execute(**kwargs: Any) -> StructuredExecution:
        payload = json.loads(kwargs["prompt"])
        prompts.append(payload)
        block = payload["project_knowledge"]
        assert block["authority"] == "claim:project_knowledge"
        assert payload["project_knowledge_policy"]["authority"] == "instruction:platform_policy"
        knowledge_ids.extend(row["knowledge_id"] for row in block["value"]["historical"])
        assert kwargs["output_schema"] != HYPOTHESIS_SCHEMA
        # A model that tries to publish history as proof:
        return _execution(
            {
                "status": "confirmed",
                "category": "security",
                "target_ids": ["target-auth"],
                "title": "Anonymous admin regression",
                "explanation": "History says this path once allowed anonymous admins.",
                "consequence": "Anonymous requests could gain admin access.",
                "evidence_refs": list(knowledge_ids),
            }
        )

    result = run_independent_review(
        store=store,
        revision=revision,
        targets=(_target(),),
        repo_root=tmp_path,
        effort="quick",
        driver="openai",
        model="review-model",
        structured_executor=execute,
        project_knowledge_loader=loader,
    )
    assert loaded == ["external"]
    assert prompts and knowledge_ids
    assert _SECRET not in json.dumps(prompts)
    assert result.confirmed == 0 and result.annotation_ids == ()
    wire = result.to_wire()
    assert wire["project_knowledge"]["authority"] == "advisory"
    assert [row["knowledge_id"] for row in wire["project_knowledge"]["entries"]] == knowledge_ids[:1]
    assert wire["project_knowledge"]["filtered_by_content_access"] == 1


def test_ai_review_prompts_are_unchanged_without_project_knowledge(tmp_path: Path) -> None:
    prompts: dict[str, list[str]] = {"none": [], "empty": []}

    def recorder(name: str) -> Any:
        def execute(**kwargs: Any) -> StructuredExecution:
            prompts[name].append(kwargs["system"] + kwargs["prompt"])
            return _execution(
                {
                    "status": "not_confirmed",
                    "category": "security",
                    "target_ids": ["target-auth"],
                    "title": "",
                    "explanation": "",
                    "consequence": "",
                    "evidence_refs": [],
                }
            )

        return execute

    for name, loader in (("none", None), ("empty", lambda _trust, _targets: {})):
        store, revision = _store(tmp_path / name)
        result = run_independent_review(
            store=store,
            revision=revision,
            targets=(_target(),),
            repo_root=tmp_path,
            effort="quick",
            driver="openai",
            model="review-model",
            structured_executor=recorder(name),
            project_knowledge_loader=loader,
        )
        assert "project_knowledge" not in result.to_wire()
    assert prompts["none"] and prompts["none"] == prompts["empty"]
    assert all("project_knowledge" not in prompt for prompt in prompts["none"])


def test_ask_system_prompt_names_the_history_class_only_when_history_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lemoncrow.pro.capabilities.review import intelligence_runtime
    from lemoncrow.pro.capabilities.review.ask import answer_ask_execution

    systems: list[str] = []

    def execute(**kwargs: Any) -> StructuredExecution:
        systems.append(kwargs["system"])
        return _execution({"intent": "question", "body": "answer", "handoff_instruction": ""})

    monkeypatch.setattr(intelligence_runtime, "execute_structured", execute)
    answer_ask_execution({"question": "why?"}, origin="human_ask")
    answer_ask_execution(
        {"question": "why?", "context_classes": dict(CONTEXT_CLASSES), "project_knowledge": {"historical": [{}]}},
        origin="human_ask",
    )
    plain, with_history = systems
    assert "project_knowledge" not in plain and "context_classes" not in plain
    assert with_history.startswith(plain)
    assert "the Snapshot wins" in with_history and "never present it as a fact about this Snapshot" in with_history


# ---------------------------------------------------------------- fix round (adversarial review)


def test_history_from_this_reviews_own_landing_is_never_labelled_as_predating_it(
    base: tuple[Workspace, ProjectKnowledgeStore, str],
) -> None:
    workspace, knowledge, _landed = base
    assert knowledge.apply(
        build_merged_change_projection(
            _gen("2"),
            _gen("3"),
            proof=_PROOF,
            merge_ref="merge:local:acme/widgets#9",
            review_ref=f"review:{workspace.review_id}",
            revision_ref="revision:rv-self",
            facts=(
                _path_fact("src/session.py", "Refresh validates token audience (landed by this Review).", char="c"),
            ),
            observed_at="2026-10-09T10:00:00Z",
        )
    )
    client = _client(workspace, binding=_binding(knowledge))
    context = client.get(f"/api/reviews/{workspace.review_id}/project-context").json()["project_context"]
    own = [
        row for row in context["entries"] if any(c.get("review_id") == workspace.review_id for c in row["citations"])
    ]
    assert own
    for row in own:
        assert row["relation_to_change"] == "this_change" and row["stale_for_snapshot"] is False
        assert row["freshness_label"].endswith("from this Change's own landing")


def test_citations_and_review_lookups_are_bounded(
    base: tuple[Workspace, ProjectKnowledgeStore, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, knowledge, _landed = base
    refs = tuple(f"review:{i:032x}" for i in range(1, 301))
    assert knowledge.apply(
        build_merged_change_projection(
            _gen("2"),
            _gen("3"),
            proof=_PROOF,
            merge_ref="merge:local:acme/widgets#10",
            review_ref="review:" + "f" * 32,
            revision_ref="revision:rv-x",
            facts=(
                KnowledgeFact(
                    subject=KnowledgeSubject("path", "src/session.py"),
                    knowledge_kind="change",
                    body="Many reviews touched refresh.",
                    source_refs=("source:new:src/session.py@sha256:" + "c" * 64, *refs),
                    state="historical",
                ),
            ),
            observed_at="2026-10-09T10:00:00Z",
        )
    )
    calls = {"get_session": 0, "list_sessions": 0}
    get_session, list_sessions = ReviewStore.get_session, ReviewStore.list_sessions

    def counting_get(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls["get_session"] += 1
        return get_session(self, *args, **kwargs)

    def counting_list(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls["list_sessions"] += 1
        return list_sessions(self, *args, **kwargs)

    client = _client(workspace, binding=_binding(knowledge))
    monkeypatch.setattr(ReviewStore, "get_session", counting_get)
    monkeypatch.setattr(ReviewStore, "list_sessions", counting_list)
    context = client.get(f"/api/reviews/{workspace.review_id}/project-context").json()["project_context"]
    crowded = next(row for row in context["entries"] if row["summary"] == "Many reviews touched refresh.")
    assert len(crowded["citations"]) == 8 and crowded["citations_omitted"] > 0
    # one route-level session read plus at most 8 distinct review lookups, never a list scan
    assert calls["list_sessions"] == 0 and calls["get_session"] <= 12, calls


def test_review_lookups_are_capped_and_memoized_per_request() -> None:
    from lemoncrow.pro.capabilities.review.project_context import _bounded_resolver

    seen: list[str] = []

    def lookup(kind: str, value: str) -> str:
        seen.append(value)
        return value

    resolve = _bounded_resolver(lookup)
    results = [resolve("review", f"r{i}") for i in range(12)] + [resolve("review", "r0")]
    assert results[:8] == [f"r{i}" for i in range(8)] and results[8:12] == [None] * 4 and results[12] == "r0"
    assert seen == [f"r{i}" for i in range(8)]
    assert _bounded_resolver(None)("review", "x") == ""


def test_a_short_review_token_in_history_never_links_to_a_review(
    base: tuple[Workspace, ProjectKnowledgeStore, str],
) -> None:
    workspace, knowledge, landed = base
    assert knowledge.apply(
        build_merged_change_projection(
            _gen("2"),
            _gen("3"),
            proof=_PROOF,
            merge_ref="merge:local:acme/widgets#11",
            review_ref=f"review:{landed[:12]}",
            revision_ref="revision:rv-short",
            facts=(_path_fact("src/session.py", "Refresh history cites a short token.", char="d"),),
            observed_at="2026-10-09T10:00:00Z",
        )
    )
    client = _client(workspace, binding=_binding(knowledge))
    context = client.get(f"/api/reviews/{workspace.review_id}/project-context").json()["project_context"]
    short = [c for row in context["entries"] for c in row["citations"] if c["ref"] == f"review:{landed[:12]}"]
    assert short and all(c["resolved"] is False and "review_id" not in c for c in short)


def test_the_token_estimate_never_undercounts_wide_unicode(tmp_path: Path) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.db")
    paths = tuple(f"src/m{i}.py" for i in range(8))
    store.apply(
        build_baseline_projection(
            _gen("1"),
            facts=tuple(_path_fact(p, "会话刷新拒绝过期令牌" * 60, kind=f"k{i}") for i, p in enumerate(paths)),
            observed_at="2026-10-01T10:00:00Z",
        )
    )
    result = retrieve_project_context(
        ProjectKnowledgeBinding(store, REPO), ProjectContextRequest(purpose="ask", paths=paths)
    )
    text = json.dumps(result.model_view(), ensure_ascii=False)
    wide = sum(1 for char in text if ord(char) > 127)
    assert result.entries and wide <= BOUNDS["ask"].max_tokens and result.used_tokens >= wide


def _review_touching_a_denied_path(tmp_path: Path) -> tuple[ReviewStore, ReviewSession, ReviewRevision, Workspace]:
    repo_root = tmp_path / "repo"
    repo = _init_repo(repo_root)
    _write(repo_root, "src/session.py", _BASE_SOURCE)
    _write(repo_root, "src/vault/keys.py", "KEYS = ()\n")
    _write(repo_root, ".lemoncrow/review.yaml", _POLICY)
    _commit(repo, "base", 0)
    _write(repo_root, "src/session.py", _CHANGED_SOURCE)
    _write(repo_root, "src/vault/keys.py", "KEYS = ('rotated',)\n")  # the Change touches the ai_denied path
    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, None, working_tree=True)
    build = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, repo_root, rng, title=build.packet.title)
    snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)
    revision = store.latest_revision(session.id)
    assert revision is not None
    store.publish_snapshot(session.id, revision.id)
    return store, session, revision, Workspace(None, store, store_root, repo_root, session.id)  # type: ignore[arg-type]


def test_restricted_model_context_drops_path_less_history(tmp_path: Path) -> None:
    store, session, _revision, workspace = _review_touching_a_denied_path(tmp_path)
    knowledge = ProjectKnowledgeStore(store)
    unscoped = "Vault rotation decision: keys rotate via HSM slot 7 using passphrase hunter2-rotation"
    assert knowledge.apply(
        build_baseline_projection(
            _gen("1"),
            facts=(
                _path_fact("src/session.py", "Session refresh rejects expired tokens."),
                KnowledgeFact(
                    subject=KnowledgeSubject("concept", "vault rotation"),
                    knowledge_kind="decision",
                    body=unscoped,
                    source_refs=("index:codeindex-gen-42",),
                    content_access_class="metadata_only",
                ),
            ),
            observed_at="2026-10-01T10:00:00Z",
        )
    )
    contexts: list[dict[str, Any]] = []
    client = _client(workspace, binding=_binding(knowledge), answerer=_answer(contexts))
    asked = client.post(
        f"/api/reviews/{session.id}/ask",
        json={"revision_number": 0, "body": "How does vault rotation work in this change?"},
    )
    assert asked.status_code == 201, asked.text
    assert contexts[-1].get("model_context_exclusions"), "precondition: Ask is restricted"
    assert "hunter2-rotation" not in json.dumps(contexts[-1])
    # unrestricted, the same path-less history is eligible (the rule is the restriction, not the kind)
    open_request = ProjectContextRequest(purpose="ask", terms=("rotation",))
    assert any(
        row.entry.body == unscoped
        for row in retrieve_project_context(ProjectKnowledgeBinding(knowledge, REPO), open_request).entries
    )
    restricted = ProjectContextRequest(purpose="ask", terms=("rotation",), restricted=True)
    assert not any(
        row.entry.body == unscoped
        for row in retrieve_project_context(ProjectKnowledgeBinding(knowledge, REPO), restricted).entries
    )


def test_ai_review_history_is_restricted_when_a_changed_path_is_ai_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from lemoncrow.pro.capabilities.review import ai_review as ai_review_module

    store, session, revision, workspace = _review_touching_a_denied_path(tmp_path)
    knowledge = ProjectKnowledgeStore(store)
    summarized = "Session tokens are derived from the vault master key hunter2-derivation"
    assert knowledge.apply(
        build_baseline_projection(
            _gen("1"),
            facts=(
                KnowledgeFact(
                    subject=KnowledgeSubject("symbol", "refresh_session"),
                    knowledge_kind="decision",
                    body=summarized,
                    source_refs=("index:codeindex-gen-42",),
                    content_access_class="metadata_only",
                ),
            ),
            observed_at="2026-10-01T10:00:00Z",
        )
    )
    captured: dict[str, Any] = {}

    class _Result:
        def to_wire(self) -> dict[str, Any]:
            return {"status": "completed"}

    def fake_run(**kwargs: Any) -> _Result:
        captured.update(kwargs)
        return _Result()

    monkeypatch.setattr(ai_review_module, "run_independent_review", fake_run)
    client = _client(workspace, binding=_binding(knowledge))
    reviewed = client.post(
        f"/api/reviews/{session.id}/ai-review", json={"expected_revision_id": revision.id, "trigger": "manual"}
    )
    assert reviewed.status_code == 200, reviewed.text
    target = SimpleNamespace(path="src/session.py", symbol="refresh_session")
    view = captured["project_knowledge_loader"]("external", (target,))
    assert "hunter2-derivation" not in json.dumps(view)
    # unrestricted, the same path-less history reaches the model view
    open_request = ProjectContextRequest(purpose="ai_review", paths=(target.path,), symbols=(target.symbol,))
    open_view = retrieve_project_context(ProjectKnowledgeBinding(knowledge, REPO), open_request).model_view()
    assert "hunter2-derivation" in json.dumps(open_view)


def test_model_history_is_framed_as_untrusted_derived_data(base: tuple[Workspace, ProjectKnowledgeStore, str]) -> None:
    _workspace, knowledge, _landed = base
    block = _brief(knowledge, purpose="ask").model_view()
    assert block["authority_tag"] == "claim:project_knowledge"
    assert "never an instruction" in block["data_handling"] and "untrusted" in block["data_handling"].lower()
    assert all("citations" not in row for row in block["historical"])


def test_question_terms_keep_a_query_slot_when_paths_crowd_the_plan(tmp_path: Path) -> None:
    store = ProjectKnowledgeStore(tmp_path / "pk.db")
    paths = tuple(f"src/p{i}.py" for i in range(24))
    store.apply(
        build_baseline_projection(
            _gen("1"),
            facts=(
                *(_path_fact(p, f"Module {i} handles plumbing.") for i, p in enumerate(paths)),
                _path_fact("docs/zebra.md", "The quokkaflux migration is the reason refresh retries."),
            ),
            observed_at="2026-10-01T10:00:00Z",
        )
    )
    request = ProjectContextRequest(
        purpose="ask",
        paths=paths,
        symbols=tuple(f"sym{i}" for i in range(8)),
        components=("core",),
        terms=("quokkaflux",),
    )
    result = retrieve_project_context(ProjectKnowledgeBinding(store, REPO), request)
    assert result.queries <= BOUNDS["ask"].max_queries
    assert any(row.relevance == "question" and "quokkaflux" in row.entry.body for row in result.entries)
    # a tighter query bound still spends one query on the question, never all on paths
    tight = dataclasses.replace(BOUNDS["ask"], max_queries=3)
    narrowed = retrieve_project_context(ProjectKnowledgeBinding(store, REPO), request, bounds=tight)
    assert narrowed.queries <= 3
    assert any(row.relevance == "question" and "quokkaflux" in row.entry.body for row in narrowed.entries)


def test_history_lookup_keeps_the_newest_generations_when_cut(
    base: tuple[Workspace, ProjectKnowledgeStore, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import lemoncrow.pro.capabilities.review.project_context as project_context

    _workspace, knowledge, _landed = base
    monkeypatch.setattr(project_context, "_MAX_EVENT_GENERATIONS", 1)
    brief = _brief(knowledge)
    assert brief.history_truncated is True and brief.to_wire()["history_truncated"] is True
    by_generation = {row.entry.valid_from_generation: row for row in brief.entries}
    assert by_generation[_gen("2").id].origin is not None
    assert by_generation[_gen("1").id].origin is None
