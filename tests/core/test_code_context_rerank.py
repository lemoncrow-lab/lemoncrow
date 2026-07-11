from __future__ import annotations

from pathlib import Path

from lemoncrow.core.capabilities.code_context import CodeContextEngine
from lemoncrow.core.capabilities.code_context.models import SymbolRecord
from lemoncrow.core.capabilities.code_context.rerank import SearchReranker
from lemoncrow.infra.internal_llm.exceptions import OllamaUnavailable


def _make_symbol(
    symbol_id: str,
    *,
    file_path: str,
    symbol_name: str | None = None,
    score: float = 0.0,
    provenance: str = "local",
    kind: str = "function",
    signature: str | None = None,
) -> SymbolRecord:
    name = symbol_name or symbol_id
    return SymbolRecord(
        symbol_id=symbol_id,
        repo_id="repo",
        file_path=file_path,
        language="python",
        symbol_name=name,
        qualified_name=name,
        kind=kind,
        signature=signature or f"def {name}() -> str",
        start_byte=0,
        end_byte=10,
        start_line=1,
        end_line=2,
        content_hash=f"hash-{symbol_id}",
        score=score,
        provenance=provenance,
    )


def _write_symbol_file(root: Path, rel: str, symbol_name: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"def {symbol_name}() -> str:\n    return '{symbol_name}'\n",
        encoding="utf-8",
    )


def test_search_reranker_reorders_top_window_and_preserves_tail() -> None:
    hits = [_make_symbol(f"s{index}", file_path=f"src/s{index}.py", score=index / 100.0) for index in range(1, 8)]
    reranker = SearchReranker(
        model="test-reranker",
        top_n=6,
        min_candidates=2,
        max_latency_ms=250,
        scorer=lambda _query, _docs, _timeout: [0.1, 0.3, 0.2, 0.4, 0.5, 0.9],
    )

    reranked = reranker.rerank(
        "session token",
        hits,
        mode="hybrid",
        scope="repo",
        source_loader=lambda symbol: f"def {symbol.symbol_name}() -> str:\n    return '{symbol.symbol_name}'\n",
    )

    assert [symbol.symbol_id for symbol in reranked] == ["s6", "s5", "s4", "s2", "s3", "s1", "s7"]
    assert reranked[0].score == 0.9
    assert reranked[-1].symbol_id == "s7"


def test_search_reranker_is_noop_when_backend_unavailable() -> None:
    hits = [
        _make_symbol("alpha", file_path="src/alpha.py"),
        _make_symbol("beta", file_path="src/beta.py"),
    ]
    reranker = SearchReranker(
        model="test-reranker",
        top_n=2,
        min_candidates=2,
        max_latency_ms=250,
        scorer=lambda _query, _docs, _timeout: (_ for _ in ()).throw(OllamaUnavailable("offline")),
    )

    reranked = reranker.rerank(
        "session token",
        hits,
        mode="hybrid",
        scope="repo",
        source_loader=lambda _symbol: "",
    )

    assert [symbol.symbol_id for symbol in reranked] == ["alpha", "beta"]


def test_search_symbols_reranks_after_filters_before_final_limit(tmp_path: Path, monkeypatch) -> None:
    for name in ("local1", "local2", "local3", "local4", "local5", "local6"):
        _write_symbol_file(tmp_path, f"src/{name}.py", name)

    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code-context.sqlite")
    monkeypatch.setattr(engine, "_ensure_indexed", lambda: None)
    monkeypatch.setattr(engine, "_zoekt_candidate_files", lambda *_args, **_kwargs: None)

    local_hits = [
        _make_symbol("local1", file_path="src/local1.py", score=0.11),
        _make_symbol("local2", file_path="src/local2.py", score=0.12),
        _make_symbol("local3", file_path="src/local3.py", score=0.13),
        _make_symbol("local4", file_path="src/local4.py", score=0.14),
        _make_symbol("local5", file_path="src/local5.py", score=0.15),
        _make_symbol("local6", file_path="src/local6.py", score=0.16),
    ]
    commit_hit = _make_symbol(
        "commit1",
        file_path="",
        symbol_name="commit1",
        provenance="commit",
        kind="commit",
        signature="Fix token regression",
    )

    monkeypatch.setattr(engine.intel_store, "search_symbols", lambda *_args, **_kwargs: local_hits)
    monkeypatch.setattr(engine, "_search_symbols_semantic_local", lambda *_args, **_kwargs: local_hits)
    monkeypatch.setattr(engine, "_search_commit_chunks", lambda *_args, **_kwargs: [commit_hit])
    monkeypatch.setattr(
        engine._semantic_ranker,
        "reciprocal_rank_fuse",
        lambda _lexical, _semantic, limit, semantic_additive_k=0: [local_hits[0], commit_hit, *local_hits[1:]][:limit],
    )

    class _FakeReranker:
        def __init__(self) -> None:
            self.received_ids: list[str] = []

        def pre_rerank_limit(self, limit: int, *, mode: str, scope: str) -> int:
            assert mode == "hybrid"
            assert scope == "repo"
            return max(limit, 7)

        def cache_fingerprint(self, *, mode: str, scope: str) -> dict[str, object]:
            return {"enabled": True, "mode": mode, "scope": scope}

        def rerank(
            self,
            _query: str,
            hits: list[SymbolRecord],
            *,
            mode: str,
            scope: str,
            source_loader,
        ) -> list[SymbolRecord]:
            assert mode == "hybrid"
            assert scope == "repo"
            self.received_ids = [symbol.symbol_id for symbol in hits]
            return [hits[-1], *hits[:-1]]

    fake_reranker = _FakeReranker()
    engine._search_reranker = fake_reranker

    results = engine.search_symbols(
        "session token",
        limit=5,
        mode="hybrid",
        provenance_filter="local",
        auto_index=False,
    )

    assert fake_reranker.received_ids == [
        "local1",
        "local2",
        "local3",
        "local4",
        "local5",
        "local6",
    ]
    assert [symbol.symbol_id for symbol in results] == [
        "local6",
        "local1",
        "local2",
        "local3",
        "local4",
    ]


def test_tool_search_cache_keys_include_rerank_fingerprint(tmp_path: Path, monkeypatch) -> None:
    _write_symbol_file(tmp_path, "src/helper.py", "helper")
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code-context.sqlite")
    monkeypatch.setattr(type(engine._semantic_ranker), "available", property(lambda self: True))
    monkeypatch.setattr(engine, "_ensure_indexed", lambda: None)
    monkeypatch.setattr(engine, "_sync_symbol_intel", lambda: None)

    calls = {"count": 0}
    helper_symbol = _make_symbol("helper", file_path="src/helper.py", symbol_name="helper", score=0.8)

    def _fake_search_symbols(*_args, **_kwargs) -> list[SymbolRecord]:
        calls["count"] += 1
        return [helper_symbol]

    monkeypatch.setattr(engine, "search_symbols", _fake_search_symbols)

    class _FingerprintReranker:
        def __init__(self) -> None:
            self.model = "v1"

        def pre_rerank_limit(self, limit: int, *, mode: str, scope: str) -> int:
            return limit

        def cache_fingerprint(self, *, mode: str, scope: str) -> dict[str, object]:
            return {"enabled": True, "model": self.model, "mode": mode, "scope": scope}

    fake_reranker = _FingerprintReranker()
    engine._search_reranker = fake_reranker

    first = engine.tool_search("helper", limit=1, mode="hybrid", auto_index=False)
    fake_reranker.model = "v2"
    second = engine.tool_search("helper", limit=1, mode="hybrid", auto_index=False)

    assert first["cache_hit"] is False
    assert second["cache_hit"] is False
    assert calls["count"] == 2
