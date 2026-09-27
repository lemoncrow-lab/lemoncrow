from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_server_core.errors import ErrorCode, ServerError
from lemoncrow_server_core.index.contracts import IndexBackend, ManifestEntry, RepoIdentityClaim, sha256_hex
from lemoncrow_server_core.index.manifest import chunk_manifest, manifest_root
from lemoncrow_server_core.index.memory import build_memory_backend
from lemoncrow_server_core.index.query import IndexQuery
from lemoncrow_server_core.index.sqlite import SqliteIndex

CAP = 1024 * 1024
ORG = "org_a"


def _entry(path: str, data: bytes) -> ManifestEntry:
    return ManifestEntry(
        path=path,
        content_digest=sha256_hex(data),
        parser_profile="python-1" if path.endswith(".py") else "lexical-1",
        mode=0o100644,
        size=len(data),
    )


def _open(
    backend: IndexBackend,
    files: dict[str, bytes],
    *,
    fingerprint: str,
    subject: str = "user_a",
    upload: set[str] | None = None,
) -> tuple[str, str]:
    entries = tuple(_entry(path, data) for path, data in sorted(files.items()))
    chunks = chunk_manifest(entries, max_rows=64)
    repo_id = backend.repos.canonical_repo_id(
        ORG,
        RepoIdentityClaim(scm_provider="", scm_repo_id="", fingerprint=fingerprint),
    )
    opened = backend.views.open(
        org_id=ORG,
        repo_id=repo_id,
        base_revision="HEAD",
        manifest_root=manifest_root(entries),
        chunk_ids=tuple(chunk.chunk_id for chunk in chunks),
        lease_s=600,
        content_subject=subject,
        owner_subject=subject,
    )
    view_id = opened.state.view_id
    for chunk in chunks:
        backend.views.put_manifest_chunk(
            org_id=ORG,
            view_id=view_id,
            chunk_id=chunk.chunk_id,
            entries=chunk.entries,
        )
    allowed = set(files) if upload is None else upload
    for path in sorted(allowed):
        data = files[path]
        entry = _entry(path, data)
        backend.content.put(ORG, entry.content_digest, data)
        backend.provenance.record(ORG, repo_id, subject, (entry.content_digest,))
        backend.analysis.ensure(ORG, entry.content_digest, entry.parser_profile, data)
    return repo_id, view_id


class _NoMembership:
    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> Any:
        if name == "membership":
            raise AssertionError("candidate-first query reconstructed full View membership")
        return getattr(self._delegate, name)


@pytest.mark.parametrize("kind", ("lexical", "symbol", "code_search"))
def test_sqlite_common_queries_do_not_reconstruct_full_membership(tmp_path: Path, kind: str) -> None:
    index = SqliteIndex(tmp_path / "index.sqlite3", content_size_cap=CAP, compact=True)
    try:
        backend = index.backend
        _repo, view_id = _open(
            backend,
            {
                "pkg/alpha.py": b"def AlphaWidget():\n    return 'needle-token'\n",
                "pkg/noise.py": b"def unrelated():\n    return 2\n",
            },
            fingerprint="repo-main",
        )
        guarded = replace(backend, views=_NoMembership(backend.views))
        query = IndexQuery(guarded, content_size_cap=CAP)
        if kind == "lexical":
            answer = query.run(org_id=ORG, view_id=view_id, kind="lexical", query="needle-token")
        elif kind == "symbol":
            answer = query.run(org_id=ORG, view_id=view_id, kind="symbol", symbol="AlphaWidget")
        else:
            answer = query.search(org_id=ORG, view_id=view_id, query="AlphaWidget", limit=4)
        assert [hit.path for hit in answer.hits] == ["pkg/alpha.py"]
    finally:
        index.close()


def test_sqlite_possession_check_is_bounded_by_requested_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = SqliteIndex(tmp_path / "index.sqlite3", content_size_cap=CAP, compact=True)
    try:
        backend = index.backend
        repo_id = backend.repos.canonical_repo_id(
            ORG,
            RepoIdentityClaim(scm_provider="", scm_repo_id="", fingerprint="provenance-scale-repo"),
        )
        held = tuple(sha256_hex(f"blob-{number}".encode()) for number in range(1_000))
        backend.provenance.record(ORG, repo_id, "user_a", held)
        db_type = type(index._db)
        original_rows = db_type.rows
        statements: list[str] = []

        def recording_rows(db: Any, sql: str, parameters: Any = ()) -> Any:
            if db is index._db:
                statements.append(sql)
            return original_rows(db, sql, parameters)

        monkeypatch.setattr(db_type, "rows", recording_rows)
        assert backend.provenance.held(ORG, repo_id, "user_a", (held[-1],)) == frozenset({held[-1]})
        selects = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
        assert selects
        assert all("digest IN (" in statement for statement in selects)
    finally:
        index.close()


def test_candidate_intersection_preserves_same_org_possession_boundary() -> None:
    backend = build_memory_backend(content_size_cap=CAP)
    alice = b"def only_alice_defines_this():\n    return 'alice-private-orion'\n"
    _alice_repo, _alice_view = _open(
        backend,
        {"pkg/alice.py": alice},
        fingerprint="alice-repo",
        subject="alice",
    )

    stolen_digest = sha256_hex(alice)
    bob_own = b"def bobs_own():\n    return 7\n"
    borrowed = ManifestEntry(
        path="vendor/borrowed.py",
        content_digest=stolen_digest,
        parser_profile="python-1",
        mode=0o100644,
        size=len(alice),
    )
    own = _entry("pkg/bob.py", bob_own)
    entries = (own, borrowed)
    chunks = chunk_manifest(entries, max_rows=64)
    bob_repo = backend.repos.canonical_repo_id(
        ORG,
        RepoIdentityClaim(scm_provider="", scm_repo_id="", fingerprint="bob-repo"),
    )
    opened = backend.views.open(
        org_id=ORG,
        repo_id=bob_repo,
        base_revision="HEAD",
        manifest_root=manifest_root(entries),
        chunk_ids=tuple(chunk.chunk_id for chunk in chunks),
        lease_s=600,
        content_subject="bob",
        owner_subject="bob",
    )
    for chunk in chunks:
        backend.views.put_manifest_chunk(
            org_id=ORG,
            view_id=opened.state.view_id,
            chunk_id=chunk.chunk_id,
            entries=chunk.entries,
        )
    backend.content.put(ORG, own.content_digest, bob_own)
    backend.provenance.record(ORG, bob_repo, "bob", (own.content_digest,))
    backend.analysis.ensure(ORG, own.content_digest, own.parser_profile, bob_own)

    query = IndexQuery(backend, content_size_cap=CAP)
    lexical = query.run(
        org_id=ORG,
        view_id=opened.state.view_id,
        kind="lexical",
        query="alice-private-orion",
    )
    symbol = query.run(
        org_id=ORG,
        view_id=opened.state.view_id,
        kind="symbol",
        symbol="only_alice_defines_this",
    )
    search = query.search(
        org_id=ORG,
        view_id=opened.state.view_id,
        query="only_alice_defines_this",
        limit=4,
    )
    assert lexical.hits == ()
    assert symbol.hits == ()
    assert search.hits == ()


def test_hidden_candidates_cannot_consume_bound_and_change_authorized_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow_server_core.index import query as query_module

    backend = build_memory_backend(content_size_cap=CAP)
    visible = b"def shared_anchor():\n    return 'shared-anchor'\n"
    _repo, view_id = _open(
        backend,
        {"pkg/visible.py": visible},
        fingerprint="visible-repo",
        subject="bob",
    )
    # Same tenant, other repositories/subjects: candidate discovery is allowed
    # to see these Layer-1 rows internally, but they must never alter Bob's answer.
    for number in range(8):
        data = f"def hidden_{number}():\n    return 'shared-anchor'\n".encode()
        _open(
            backend,
            {f"pkg/hidden_{number}.py": data},
            fingerprint=f"hidden-repo-{number}",
            subject=f"alice-{number}",
        )

    query = IndexQuery(backend, content_size_cap=CAP)
    bounded = query.run(
        org_id=ORG,
        view_id=view_id,
        kind="lexical",
        query="shared-anchor",
    )
    monkeypatch.setattr(query_module, "_CANDIDATE_SCAN_LIMIT", 2)
    overflow = query.run(
        org_id=ORG,
        view_id=view_id,
        kind="lexical",
        query="shared-anchor",
    )
    assert [hit.path for hit in overflow.hits] == ["pkg/visible.py"]
    assert overflow.to_wire() == bounded.to_wire()


def test_path_candidate_overflow_falls_back_without_changing_ranking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow_server_core.index import query as query_module

    index = SqliteIndex(tmp_path / "index.sqlite3", content_size_cap=CAP, compact=True)
    try:
        _repo, view_id = _open(
            index.backend,
            {
                "docs/aaa_needlex.md": b"documentation only\n",
                "src/zzz_needlex.py": b"def production_code():\n    return 1\n",
            },
            fingerprint="path-overflow-repo",
        )
        monkeypatch.setattr(query_module, "_PATH_CANDIDATE_LIMIT", 1)
        answer = IndexQuery(index.backend, content_size_cap=CAP).search(
            org_id=ORG,
            view_id=view_id,
            query="needlex",
            limit=1,
        )
        assert [hit.path for hit in answer.hits] == ["src/zzz_needlex.py"]
    finally:
        index.close()


def test_non_ascii_path_matching_preserves_python_casefold_semantics(tmp_path: Path) -> None:
    index = SqliteIndex(tmp_path / "index.sqlite3", content_size_cap=CAP, compact=True)
    try:
        _repo, view_id = _open(
            index.backend,
            {"pkg/Äpfel.py": b"def fruit():\n    return 1\n"},
            fingerprint="unicode-path-repo",
        )
        answer = IndexQuery(index.backend, content_size_cap=CAP).run(
            org_id=ORG,
            view_id=view_id,
            kind="lexical",
            query="äpfel",
        )
        assert [hit.path for hit in answer.hits] == ["pkg/Äpfel.py"]
        assert answer.hits[0].detail.get("matched") == "path"
    finally:
        index.close()


def test_candidate_lookup_respects_overlay_replacement_and_tombstone(tmp_path: Path) -> None:
    index = SqliteIndex(tmp_path / "index.sqlite3", content_size_cap=CAP, compact=True)
    try:
        backend = index.backend
        repo_id, view_id = _open(
            backend,
            {
                "pkg/replaced.py": b"def OldSymbol():\n    return 'old-anchor'\n",
                "pkg/deleted.py": b"def DeletedSymbol():\n    return 'deleted-anchor'\n",
            },
            fingerprint="overlay-repo",
        )
        replacement = b"def NewSymbol():\n    return 'new-anchor'\n"
        replacement_entry = _entry("pkg/replaced.py", replacement)
        backend.content.put(ORG, replacement_entry.content_digest, replacement)
        backend.provenance.record(ORG, repo_id, "user_a", (replacement_entry.content_digest,))
        backend.analysis.ensure(ORG, replacement_entry.content_digest, replacement_entry.parser_profile, replacement)
        state = backend.views.get(ORG, view_id)
        backend.views.apply_overlay(
            org_id=ORG,
            view_id=view_id,
            expected_view_revision=state.view_revision,
            client_seq=1,
            entries=(replacement_entry,),
            removed_paths=("pkg/deleted.py",),
        )

        query = IndexQuery(backend, content_size_cap=CAP)
        assert query.run(org_id=ORG, view_id=view_id, kind="symbol", symbol="OldSymbol").hits == ()
        assert query.run(org_id=ORG, view_id=view_id, kind="symbol", symbol="DeletedSymbol").hits == ()
        assert [hit.path for hit in query.run(org_id=ORG, view_id=view_id, kind="symbol", symbol="NewSymbol").hits] == [
            "pkg/replaced.py"
        ]
    finally:
        index.close()


class _AdvanceDuringCandidates(_NoMembership):
    def __init__(self, delegate: Any, backend: IndexBackend, *, org_id: str, view_id: str, repo_id: str) -> None:
        super().__init__(delegate)
        self._backend = backend
        self._org_id = org_id
        self._view_id = view_id
        self._repo_id = repo_id
        self._advanced = False

    def entries_for_digests(self, org_id: str, view_id: str, digests: tuple[str, ...]) -> Any:
        rows = self._delegate.entries_for_digests(org_id, view_id, digests)
        if not self._advanced:
            self._advanced = True
            data = b"def LaterSymbol():\n    return 9\n"
            entry = _entry("pkg/later.py", data)
            self._backend.content.put(self._org_id, entry.content_digest, data)
            self._backend.provenance.record(self._org_id, self._repo_id, "user_a", (entry.content_digest,))
            self._backend.analysis.ensure(self._org_id, entry.content_digest, entry.parser_profile, data)
            state = self._delegate.get(self._org_id, self._view_id)
            self._delegate.apply_overlay(
                org_id=self._org_id,
                view_id=self._view_id,
                expected_view_revision=state.view_revision,
                client_seq=1,
                entries=(entry,),
                removed_paths=(),
            )
        return rows


def test_candidate_query_refuses_revision_torn_read(tmp_path: Path) -> None:
    index = SqliteIndex(tmp_path / "index.sqlite3", content_size_cap=CAP, compact=True)
    try:
        backend = index.backend
        repo_id, view_id = _open(
            backend,
            {"pkg/alpha.py": b"def AlphaSymbol():\n    return 'alpha-anchor'\n"},
            fingerprint="race-repo",
        )
        guarded = replace(
            backend,
            views=_AdvanceDuringCandidates(backend.views, backend, org_id=ORG, view_id=view_id, repo_id=repo_id),
        )
        with pytest.raises(ServerError) as raised:
            IndexQuery(guarded, content_size_cap=CAP).run(
                org_id=ORG,
                view_id=view_id,
                kind="symbol",
                symbol="AlphaSymbol",
            )
        assert raised.value.code is ErrorCode.VIEW_REVISION_STALE
    finally:
        index.close()
