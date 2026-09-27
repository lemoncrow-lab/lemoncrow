from __future__ import annotations

from lemoncrow_server_core.index.contracts import IndexBackend, ManifestEntry, RepoIdentityClaim, sha256_hex
from lemoncrow_server_core.index.manifest import chunk_manifest, manifest_root
from lemoncrow_server_core.index.memory import build_memory_backend
from lemoncrow_server_core.index.query import IndexQuery

CAP = 1024**2


def _view() -> tuple[IndexBackend, str]:
    backend = build_memory_backend(content_size_cap=CAP)
    org = "org_metrics"
    repo = backend.repos.canonical_repo_id(org, RepoIdentityClaim("", "", "metrics-repo"))
    files = {
        "alpha.py": b"def alpha():\n    return beta()\n",
        "beta.py": b"def beta():\n    return 1\n",
    }
    entries = tuple(
        ManifestEntry(path, sha256_hex(data), "python-1", 0o100644, len(data)) for path, data in sorted(files.items())
    )
    chunks = chunk_manifest(entries)
    opened = backend.views.open(
        org_id=org,
        repo_id=repo,
        base_revision="base",
        manifest_root=manifest_root(entries),
        chunk_ids=[chunk.chunk_id for chunk in chunks],
        lease_s=3600,
        content_subject="alice",
        owner_subject="alice",
    )
    view_id = opened.state.view_id
    for chunk in chunks:
        backend.views.put_manifest_chunk(org_id=org, view_id=view_id, chunk_id=chunk.chunk_id, entries=chunk.entries)
    for data in files.values():
        digest = sha256_hex(data)
        backend.content.put(org, digest, data)
        backend.analysis.ensure(org, digest, "python-1", data)
        backend.provenance.record(org, repo, "alice", (digest,))
    return backend, view_id


def test_query_metrics_separate_membership_retrieval_and_semantic_scan() -> None:
    backend, view_id = _view()
    query = IndexQuery(backend, content_size_cap=CAP)
    lexical = query.run(org_id="org_metrics", view_id=view_id, kind="lexical", query="alpha")
    semantic = query.run(org_id="org_metrics", view_id=view_id, kind="semantic", query="return beta")
    assert lexical.hits
    assert semantic.hits

    assert backend.ops is not None
    snapshot = backend.ops.snapshot()
    assert snapshot.value("lemoncrow_index_membership_build_seconds_count", operation="lexical") == 1
    assert snapshot.value("lemoncrow_index_membership_entries_total", operation="lexical") == 1
    assert snapshot.value("lemoncrow_index_membership_entries_total", operation="lexical") < 2
    assert snapshot.value("lemoncrow_index_retrieval_seconds_count", channel="lexical") == 1
    assert snapshot.value("lemoncrow_index_retrieval_hits_total", channel="lexical") >= 1
    assert snapshot.value("lemoncrow_index_semantic_scan_seconds_count") == 0
    assert snapshot.value("lemoncrow_index_semantic_ann_seconds_count") == 1
    assert snapshot.value("lemoncrow_index_semantic_ann_candidates_last") == 2
    assert snapshot.value("lemoncrow_index_semantic_ann_units_last") == 2


def test_metadata_pool_metrics_expose_wait_and_pressure_without_tenant_labels() -> None:
    backend, _view_id = _view()
    assert backend.ops is not None
    backend.ops.metadata_pool(wait_s=0.125, size=8, available=2, waiting=3)
    snapshot = backend.ops.snapshot()
    assert snapshot.value("lemoncrow_index_metadata_pool_acquires_total") == 1
    assert snapshot.value("lemoncrow_index_metadata_pool_wait_seconds_sum") == 0.125
    assert snapshot.value("lemoncrow_index_metadata_pool_size") == 8
    assert snapshot.value("lemoncrow_index_metadata_pool_available") == 2
    assert snapshot.value("lemoncrow_index_metadata_pool_waiting") == 3


def test_layer_metrics_expose_readiness_and_revision_lag() -> None:
    backend, view_id = _view()
    assert backend.ops is not None
    current = backend.views.get("org_metrics", view_id).view_revision
    status = backend.links.refresh("org_metrics", view_id)
    backend.ops.layer(
        layer="link",
        state=status.state.value,
        revision_lag=max(0, current - status.at_view_revision),
        covered_paths=status.covered_paths,
    )
    text = backend.ops.render_prometheus()
    assert 'lemoncrow_index_layer_state{layer="link",state="ready"} 1' in text
    assert 'lemoncrow_index_layer_revision_lag{layer="link"} 0' in text
