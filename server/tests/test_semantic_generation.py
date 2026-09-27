from __future__ import annotations

from lemoncrow_server_core.index.analysis import SEMANTIC_EMBEDDING_VERSION, analyze_foreground, embed_terms
from lemoncrow_server_core.index.contracts import ManifestEntry, RepoIdentityClaim, sha256_hex
from lemoncrow_server_core.index.manifest import chunk_manifest, manifest_root
from lemoncrow_server_core.index.memory import build_memory_backend
from lemoncrow_server_core.index.query import IndexQuery

ORG = "org_semantic_generation"
SUBJECT = "alice"
CAP = 1024 * 1024
BASE = {
    "auth.py": b"def authenticate_user(token):\n    validate_token(token)\n    return load_account(token)\n",
    "billing.py": b"def calculate_invoice(items):\n    return sum(item.price for item in items)\n",
    "cache.py": b"def invalidate_cache(key):\n    cache.delete(key)\n",
}


def _entries(files: dict[str, bytes]) -> tuple[ManifestEntry, ...]:
    return tuple(
        ManifestEntry(path, sha256_hex(data), "python-3", 0o100644, len(data)) for path, data in sorted(files.items())
    )


def _open(backend, repo: str, files: dict[str, bytes], *, foreground: bool = False) -> str:  # type: ignore[no-untyped-def]
    entries = _entries(files)
    chunks = chunk_manifest(entries)
    opened = backend.views.open(
        org_id=ORG,
        repo_id=repo,
        base_revision="b" * 40,
        manifest_root=manifest_root(entries),
        chunk_ids=tuple(chunk.chunk_id for chunk in chunks),
        lease_s=3600,
        content_subject=SUBJECT,
        owner_subject=SUBJECT,
    )
    for chunk in chunks:
        backend.views.put_manifest_chunk(
            org_id=ORG,
            view_id=opened.state.view_id,
            chunk_id=chunk.chunk_id,
            entries=chunk.entries,
        )
    for path, data in files.items():
        entry = next(item for item in entries if item.path == path)
        backend.content.put(ORG, entry.content_digest, data)
        backend.provenance.record(ORG, repo, SUBJECT, (entry.content_digest,))
        artifact = analyze_foreground(entry.content_digest, entry.parser_profile, data) if foreground else None
        backend.analysis.ensure(ORG, entry.content_digest, entry.parser_profile, data, artifact=artifact)
    return opened.state.view_id


def _backend():  # type: ignore[no-untyped-def]
    backend = build_memory_backend(content_size_cap=CAP, inline_rebuild_budget=100)
    repo = backend.repos.canonical_repo_id(ORG, RepoIdentityClaim("", "", "semantic-repo"))
    return backend, repo


def test_semantic_units_are_versioned_without_mutating_structural_v2_artifacts() -> None:
    data = BASE["auth.py"]
    old = __import__("lemoncrow_server_core.index.analysis", fromlist=["analyze"]).analyze(
        sha256_hex(data), "python-2", data
    )
    new = __import__("lemoncrow_server_core.index.analysis", fromlist=["analyze"]).analyze(
        sha256_hex(data), "python-3", data
    )
    assert old.semantic_units == ()
    assert new.semantic_units
    assert {unit.embedding_version for unit in new.semantic_units} == {SEMANTIC_EMBEDDING_VERSION}
    assert any(unit.symbol == "authenticate_user" for unit in new.semantic_units)


def test_semantic_refresh_builds_chunk_units_from_foreground_artifacts() -> None:
    backend, repo = _backend()
    view_id = _open(backend, repo, BASE, foreground=True)
    entry = _entries(BASE)[0]
    artifact = backend.analysis.get(ORG, entry.content_digest, entry.parser_profile)
    assert artifact is not None
    assert artifact.semantic_units == ()

    assert backend.semantic.refresh(ORG, view_id, budget=100, wait=True).ready
    answer = IndexQuery(backend, content_size_cap=CAP).run(
        org_id=ORG,
        view_id=view_id,
        kind="semantic",
        query="Authenticate User token",
        limit=5,
    )
    assert answer.degraded is False
    assert answer.hits
    assert answer.hits[0].path == "auth.py"
    assert answer.hits[0].detail["semantic_unit"] != "file-fallback"


def test_clean_views_share_one_generation_ann() -> None:
    backend, repo = _backend()
    first = _open(backend, repo, BASE)
    second = _open(backend, repo, BASE)
    assert backend.semantic.refresh(ORG, first, budget=100, wait=True).ready
    derivations = backend.analysis.derivations  # type: ignore[attr-defined]
    assert backend.semantic.refresh(ORG, second, budget=100, wait=True).ready
    assert backend.analysis.derivations == derivations  # type: ignore[attr-defined]
    assert len(backend.semantic._generation_indexes) == 1  # type: ignore[attr-defined]


def test_ready_semantic_query_does_not_reconstruct_full_view_membership(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    backend, repo = _backend()
    view_id = _open(backend, repo, BASE)
    assert backend.semantic.refresh(ORG, view_id, budget=100, wait=True).ready

    store_type = type(backend.views)
    original = store_type.membership

    def forbidden(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("ready semantic query reconstructed full View membership")

    monkeypatch.setattr(store_type, "membership", forbidden)
    try:
        answer = IndexQuery(backend, content_size_cap=CAP).run(
            org_id=ORG,
            view_id=view_id,
            kind="semantic",
            query="Authenticate User token",
            limit=5,
        )
    finally:
        monkeypatch.setattr(store_type, "membership", original)
    assert answer.degraded is False
    assert answer.hits
    assert answer.hits[0].path == "auth.py"
    assert answer.hits[0].detail["semantic_unit"] != "file-fallback"


def test_overlay_ann_replaces_base_path_and_tombstone_excludes_base() -> None:
    backend, repo = _backend()
    clean = _open(backend, repo, BASE)
    dirty = _open(backend, repo, BASE)
    assert backend.semantic.refresh(ORG, clean, budget=100, wait=True).ready

    replacement = b"def authenticate_user(token):\n    rotate_session_secret(token)\n    issue_fresh_session(token)\n"
    entry = ManifestEntry("auth.py", sha256_hex(replacement), "python-3", 0o100644, len(replacement))
    backend.content.put(ORG, entry.content_digest, replacement)
    backend.provenance.record(ORG, repo, SUBJECT, (entry.content_digest,))
    state = backend.views.apply_overlay(
        org_id=ORG,
        view_id=dirty,
        expected_view_revision=0,
        client_seq=1,
        entries=(entry,),
        removed_paths=("cache.py",),
    )
    backend.semantic.note_revision(ORG, dirty, state.view_revision)
    assert backend.semantic.refresh(ORG, dirty, budget=100, wait=True).ready

    fresh = backend.semantic.nearest(ORG, dirty, embed_terms(("rotate", "session", "secret")), 5)
    assert fresh.matches
    assert fresh.matches[0].path == "auth.py"
    assert fresh.matches[0].digest == entry.content_digest
    assert all(match.path != "cache.py" for match in fresh.matches)


def test_missing_overlay_content_keeps_semantic_readiness_degraded() -> None:
    backend, repo = _backend()
    view_id = _open(backend, repo, BASE)
    assert backend.semantic.refresh(ORG, view_id, budget=100, wait=True).ready
    missing = b"def reconcile_ledger():\n    return never_uploaded()\n"
    entry = ManifestEntry("ledger.py", sha256_hex(missing), "python-3", 0o100644, len(missing))
    state = backend.views.apply_overlay(
        org_id=ORG,
        view_id=view_id,
        expected_view_revision=0,
        client_seq=1,
        entries=(entry,),
        removed_paths=(),
    )
    backend.semantic.note_revision(ORG, view_id, state.view_revision)
    status = backend.semantic.refresh(ORG, view_id, budget=100, wait=True)
    assert status.ready is False
    assert status.reason == "semantic_content_incomplete"


def test_ann_quality_preserves_old_file_vector_top_result_on_fixture_queries() -> None:
    backend, repo = _backend()
    view_id = _open(backend, repo, BASE)
    assert backend.semantic.refresh(ORG, view_id, budget=100, wait=True).ready
    membership = backend.views.membership(ORG, view_id)
    digests = tuple(entry.content_digest for entry in membership.values())
    by_digest = {entry.content_digest: path for path, entry in membership.items()}
    for query in ("authenticate_user token load_account", "calculate_invoice items price", "invalidate_cache key"):
        vector = embed_terms(query.split())
        old = backend.analysis.nearest(ORG, digests, vector, 1)
        assert old
        expected_path = by_digest[old[0][0]]
        new = backend.semantic.nearest(ORG, view_id, vector, 3)
        assert expected_path in {match.path for match in new.matches}
