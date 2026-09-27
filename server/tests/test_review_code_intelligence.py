from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from lemoncrow_server_core.index.contracts import ManifestEntry, RepoIdentityClaim, sha256_hex
from lemoncrow_server_core.index.manifest import chunk_manifest, manifest_root
from lemoncrow_server_core.index.query import IndexQuery
from lemoncrow_server_core.index.repositories import RepositoryIndex
from lemoncrow_server_core.review_code_intelligence import NativeReviewCodeIntelligence

from lemoncrow.pro.capabilities.review.review_map import _stable_id, build_review_map

CAP = 1024**2
ORG = "org_review_intelligence"


def _open(
    owner: RepositoryIndex,
    *,
    subject: str = "alice",
    files: dict[str, bytes] | None = None,
    base_revision: str = "h" * 40,
    lease_s: float = 100_000,
) -> tuple[str, str]:
    files = files or {
        "a.py": b"from b import beta\n\ndef alpha():\n    return beta()\n",
        "b.py": b"def beta():\n    return 1\n",
    }
    repo = owner.backend.repos.canonical_repo_id(ORG, RepoIdentityClaim("", "", "review-intelligence"))
    entries = tuple(
        ManifestEntry(path, sha256_hex(data), "python-1", 0o100644, len(data)) for path, data in sorted(files.items())
    )
    chunks = chunk_manifest(entries)
    opened = owner.backend.views.open(
        org_id=ORG,
        repo_id=repo,
        base_revision=base_revision,
        manifest_root=manifest_root(entries),
        chunk_ids=tuple(chunk.chunk_id for chunk in chunks),
        lease_s=lease_s,
        content_subject=subject,
        owner_subject=subject,
    )
    view_id = opened.state.view_id
    for chunk in chunks:
        owner.backend.views.put_manifest_chunk(
            org_id=ORG, view_id=view_id, chunk_id=chunk.chunk_id, entries=chunk.entries
        )
    leaf = owner.backend.for_view(ORG, view_id)
    for data in files.values():
        digest = sha256_hex(data)
        leaf.content.put(ORG, digest, data)
        leaf.analysis.ensure(ORG, digest, "python-1", data)
        leaf.provenance.record(ORG, repo, subject, (digest,))
    return repo, view_id


def _provider(
    owner: RepositoryIndex,
    repo: str,
    subject: str = "alice",
    *,
    preferred_view_id: str = "",
    preferred_view_revision: int = 0,
) -> NativeReviewCodeIntelligence:
    return NativeReviewCodeIntelligence(
        backend=owner.backend,
        queries=IndexQuery(owner.backend, content_size_cap=CAP),
        org_id=ORG,
        repo_id=repo,
        subject=lambda: subject,
        content_size_cap=CAP,
        preferred_view_id=preferred_view_id,
        preferred_view_revision=preferred_view_revision,
    )


def test_native_review_provider_uses_generation_view_and_bounded_graph(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        repo, view_id = _open(owner)
        provider = _provider(owner, repo)

        before = provider.readiness()
        assert before.backend == "native"
        assert before.view_id == view_id
        assert before.generation_id.startswith("gen_")
        assert before.graph.state == "building"

        beta = provider.find_symbol("beta", path="b.py")
        assert beta is not None
        neighborhood = provider.neighborhood(beta, depth=9, limit=999)
        assert len(neighborhood["nodes"]) <= 40
        assert len(neighborhood["edges"]) <= 40
        assert any(node["path"] == "a.py" for node in neighborhood["nodes"])

        refs = provider.references(beta, limit=999)
        assert len(refs["references"]) <= 20
        assert any(row["path"] == "a.py" for row in refs["references"])

        semantic = provider.semantic_search("beta caller", limit=999)
        assert len(semantic["hits"]) <= 12
        assert semantic["view_revision"] == 0
        assert all(str(hit["path"]) in {"a.py", "b.py"} for hit in semantic["hits"])

        after = provider.readiness()
        assert after.graph.state == "ready"
        assert after.view_revision == 0
    finally:
        owner.close()


def test_native_review_provider_fails_closed_without_owned_view(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        repo, _view_id = _open(owner, subject="alice")
        provider = _provider(owner, repo, subject="mallory")
        readiness = provider.readiness()
        assert not readiness.available
        assert readiness.reason == "no_authorized_native_view"
        assert provider.find_symbol("beta", path="b.py") is None
    finally:
        owner.close()


def test_native_review_provider_binds_to_snapshot_source_view_across_worktrees(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        repo, review_view = _open(
            owner,
            files={"reviewed.py": b"def reviewed_symbol():\n    return 1\n"},
            base_revision="a" * 40,
            lease_s=1_000,
        )
        same_repo, other_view = _open(
            owner,
            files={"other.py": b"def other_worktree_symbol():\n    return 2\n"},
            base_revision="b" * 40,
            lease_s=2_000,
        )
        assert same_repo == repo
        assert other_view != review_view

        ambiguous = _provider(owner, repo)
        assert ambiguous.readiness().reason == "ambiguous_authorized_native_views"

        provider = _provider(owner, repo, preferred_view_id=review_view)
        readiness = provider.readiness()
        assert readiness.view_id == review_view
        assert readiness.base_revision == "a" * 40
        assert provider.find_symbol("reviewed_symbol", path="reviewed.py", start_line=1) is not None
        assert provider.find_symbol("other_worktree_symbol", path="other.py", start_line=1) is None
    finally:
        owner.close()


def test_native_symbol_matching_refuses_ambiguous_same_name_without_exact_line(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        source = b"class A:\n    def run(self):\n        return 1\n\nclass B:\n    def run(self):\n        return 2\n"
        repo, view_id = _open(owner, files={"a.py": source})
        provider = _provider(owner, repo, preferred_view_id=view_id)

        assert provider.find_symbol("run", path="a.py", qualified_name="B.run") is None
        second = provider.find_symbol("run", path="a.py", qualified_name="B.run", start_line=6)
        assert second is not None
        assert second["line"] == 6
        assert second["end_line"] == 6
    finally:
        owner.close()


def test_native_symbol_matching_does_not_reuse_snapshot_line_after_bound_view_advances(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        original = b"class A:\n    def run(self):\n        return 1\n\nclass B:\n    def run(self):\n        return 2\n"
        repo, view_id = _open(owner, files={"a.py": original})
        provider = _provider(
            owner,
            repo,
            preferred_view_id=view_id,
            preferred_view_revision=0,
        )
        before = provider.find_symbol("run", path="a.py", qualified_name="B.run", start_line=6)
        assert before is not None
        assert before["qualified_name"] == "B.run"

        advanced = b"class B:\n    def run(self):\n        return 2\n\nclass A:\n    def run(self):\n        return 1\n"
        digest = sha256_hex(advanced)
        leaf = owner.backend.for_view(ORG, view_id)
        leaf.content.put(ORG, digest, advanced)
        leaf.analysis.ensure(ORG, digest, "python-1", advanced)
        leaf.provenance.record(ORG, repo, "alice", (digest,))
        state = owner.backend.views.apply_overlay(
            org_id=ORG,
            view_id=view_id,
            expected_view_revision=0,
            client_seq=1,
            entries=(ManifestEntry("a.py", digest, "python-1", 0o100644, len(advanced)),),
            removed_paths=(),
        )
        assert state.view_revision == 1

        # Snapshot line 6 now names A.run. Native Layer-1 cannot prove the
        # qualified identity B.run independently, so current-index context must
        # degrade instead of relabeling A.run as B.run.
        assert provider.find_symbol("run", path="a.py", qualified_name="B.run", start_line=6) is None
    finally:
        owner.close()


def test_native_neighborhood_does_not_attribute_other_function_outgoing_calls(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        repo, view_id = _open(
            owner,
            files={
                "a.py": b"def target():\n    return 1\n\ndef unrelated():\n    return helper()\n",
                "b.py": b"def helper():\n    return 2\n",
            },
        )
        provider = _provider(owner, repo, preferred_view_id=view_id)
        target = provider.find_symbol("target", path="a.py", start_line=1)
        assert target is not None
        assert target["end_line"] == 1
        neighborhood = provider.neighborhood(target)
        assert neighborhood["edges"] == []
        assert all(node["path"] != "b.py" for node in neighborhood["nodes"])
    finally:
        owner.close()


def test_native_neighborhood_preserves_call_and_reference_relation_kinds(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        repo, view_id = _open(
            owner,
            files={
                "a.py": b"from b import beta\n\ndef alpha():\n    x = beta\n    return beta()\n",
                "b.py": b"def beta():\n    return 1\n",
            },
        )
        provider = _provider(owner, repo, preferred_view_id=view_id)
        beta = provider.find_symbol("beta", path="b.py", start_line=1)
        assert beta is not None
        neighborhood = provider.neighborhood(beta)
        kinds = {str(edge["kind"]) for edge in neighborhood["edges"]}
        assert "call" in kinds
        assert "reference" in kinds
        assert "import" not in kinds
    finally:
        owner.close()


def test_review_map_keeps_native_current_index_non_snapshot_exact_without_checkout(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        repo, _view_id = _open(owner)
        provider = _provider(owner, repo)
        symbol = {
            "symbol_name": "beta",
            "qualified_name": "beta",
            "kind": "function",
            "file_path": "b.py",
            "start_line": 1,
            "end_line": 2,
            "change": "modified",
            "caller_count": -1,
            "usage_count": -1,
        }
        packet = {
            "files": [{"path": "b.py", "status": "modified", "category": "code"}],
            "symbols": [symbol],
            "impact": [],
            "degraded": [],
        }
        node_id = _stable_id("symbol", "b.py", "beta", 1, "modified")
        revision = SimpleNamespace(
            id="rev-1",
            revision_number=1,
            tree_fingerprint="tree",
            source_fingerprint="source",
            base_sha="",
            head_sha="h" * 40,
            degraded=(),
        )
        payload = build_review_map(
            review_id="review-1",
            revision=revision,
            packet=packet,
            targets=(),
            evidence=(),
            context_repo_root=None,
            code_intelligence=provider,
            level="symbol",
            selected_node_id=node_id,
        )

        assert payload["snapshot"]["exactness"] == "snapshot"
        assert payload["context"]["source"] == "current_index"
        assert payload["context"]["exact_for_snapshot"] is False
        assert payload["context"]["generation_matches_snapshot_head"] is True
        assert payload["context"]["snapshot_head_matches_checkout"] is False
        assert payload["context"]["readiness"]["graph"]["state"] == "ready"
        focus = next(row for row in payload["graph"]["nodes"] if row["id"] == payload["graph"]["focus"])
        assert focus["exactness"] == "snapshot"
        current_nodes = [row for row in payload["graph"]["nodes"] if row["id"] != node_id]
        assert current_nodes
        assert all(row["exactness"] == "current_index" for row in current_nodes)
        current_edges = [edge for edge in payload["graph"]["edges"] if edge["exactness"] == "current_index"]
        assert {edge["kind"] for edge in current_edges} >= {"call", "reference"}
        assert all(edge["exactness"] in {"snapshot", "current_index"} for edge in payload["graph"]["edges"])
    finally:
        owner.close()


def test_review_map_reports_graph_deferred_without_losing_snapshot(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=1)
    try:
        repo, _view_id = _open(owner)
        provider = _provider(owner, repo)
        symbol = {
            "symbol_name": "beta",
            "qualified_name": "beta",
            "kind": "function",
            "file_path": "b.py",
            "start_line": 1,
            "end_line": 2,
            "change": "modified",
            "caller_count": -1,
            "usage_count": -1,
        }
        node_id = _stable_id("symbol", "b.py", "beta", 1, "modified")
        payload = build_review_map(
            review_id="review-deferred",
            revision=SimpleNamespace(
                id="rev-deferred",
                revision_number=1,
                tree_fingerprint="tree",
                source_fingerprint="source",
                base_sha="",
                head_sha="h" * 40,
                degraded=(),
            ),
            packet={
                "files": [{"path": "b.py", "status": "modified", "category": "code"}],
                "symbols": [symbol],
                "impact": [],
                "degraded": [],
            },
            targets=(),
            evidence=(),
            context_repo_root=None,
            code_intelligence=provider,
            level="symbol",
            selected_node_id=node_id,
        )
        assert payload["snapshot"]["exactness"] == "snapshot"
        assert payload["context"]["source"] == "current_index"
        assert payload["context"]["readiness"]["graph"]["state"] == "building"
        assert payload["context"]["readiness"]["graph"]["reason"] == "link_rebuild_deferred"
        assert payload["context"]["exact_for_snapshot"] is False
        assert "Graph intelligence is building" in payload["context"]["note"]
    finally:
        owner.close()


def test_review_map_stays_snapshot_usable_when_native_view_is_unavailable(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        repo, _view_id = _open(owner, subject="alice")
        provider = _provider(owner, repo, subject="mallory")
        symbol = {
            "symbol_name": "beta",
            "qualified_name": "beta",
            "kind": "function",
            "file_path": "b.py",
            "start_line": 1,
            "end_line": 2,
            "change": "modified",
            "caller_count": -1,
            "usage_count": -1,
        }
        payload = build_review_map(
            review_id="review-unavailable",
            revision=SimpleNamespace(
                id="rev-unavailable",
                revision_number=1,
                tree_fingerprint="tree",
                source_fingerprint="source",
                base_sha="",
                head_sha="h" * 40,
                degraded=(),
            ),
            packet={
                "files": [{"path": "b.py", "status": "modified", "category": "code"}],
                "symbols": [symbol],
                "impact": [],
                "degraded": [],
            },
            targets=(),
            evidence=(),
            context_repo_root=None,
            code_intelligence=provider,
            level="architecture",
        )
        assert payload["snapshot"]["exactness"] == "snapshot"
        assert payload["context"]["source"] == "unavailable"
        assert payload["context"]["exact_for_snapshot"] is False
        assert payload["context"]["readiness"]["reason"] == "no_authorized_native_view"
        assert payload["graph"]["nodes"]
    finally:
        owner.close()


def test_native_readiness_reports_content_incomplete_layers_as_degraded(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        files = {
            "a.py": b"from b import beta\n\ndef alpha():\n    return beta()\n",
            "b.py": b"def beta():\n    return 1\n",
        }
        repo = owner.backend.repos.canonical_repo_id(ORG, RepoIdentityClaim("", "", "review-degraded"))
        entries = tuple(
            ManifestEntry(path, sha256_hex(data), "python-1", 0o100644, len(data))
            for path, data in sorted(files.items())
        )
        chunks = chunk_manifest(entries)
        opened = owner.backend.views.open(
            org_id=ORG,
            repo_id=repo,
            base_revision="d" * 40,
            manifest_root=manifest_root(entries),
            chunk_ids=tuple(chunk.chunk_id for chunk in chunks),
            lease_s=100_000,
            content_subject="alice",
            owner_subject="alice",
        )
        view_id = opened.state.view_id
        for chunk in chunks:
            owner.backend.views.put_manifest_chunk(
                org_id=ORG, view_id=view_id, chunk_id=chunk.chunk_id, entries=chunk.entries
            )
        leaf = owner.backend.for_view(ORG, view_id)
        only = files["a.py"]
        digest = sha256_hex(only)
        leaf.content.put(ORG, digest, only)
        leaf.analysis.ensure(ORG, digest, "python-1", only)
        leaf.provenance.record(ORG, repo, "alice", (digest,))

        assert leaf.links.refresh(ORG, view_id, budget=100).reason == "link_content_incomplete"
        assert leaf.semantic.refresh(ORG, view_id, budget=100).reason == "semantic_content_incomplete"

        readiness = _provider(owner, repo).readiness()
        assert readiness.lexical.state == "degraded"
        assert readiness.lexical.reason == "content_incomplete"
        assert readiness.structural.state == "degraded"
        assert readiness.structural.reason == "content_incomplete"
        assert readiness.graph.state == "degraded"
        assert readiness.graph.reason == "link_content_incomplete"
        assert readiness.semantic.state == "degraded"
        assert readiness.semantic.reason == "semantic_content_incomplete"
        assert "graph:link_content_incomplete" in readiness.degraded_reasons
        assert "semantic:semantic_content_incomplete" in readiness.degraded_reasons
    finally:
        owner.close()
