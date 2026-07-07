from __future__ import annotations

from pathlib import Path

import pytest

from atelier.core.capabilities.code_context.embedding import SemanticSearchRanker
from atelier.core.capabilities.code_context.models import SymbolRecord


class _RecordingEmbedder:
    """Deterministic embedder that records the batch size of each embed call."""

    name = "recording"
    dim = 3

    def __init__(self) -> None:
        self.calls: list[int] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        return [[float(len(text)), float(sum(ord(c) for c in text) % 97), 1.0] for text in texts]


def _make_symbol(symbol_id: str) -> SymbolRecord:
    return SymbolRecord(
        symbol_id=symbol_id,
        repo_id="repo",
        file_path=f"src/{symbol_id}.py",
        language="python",
        symbol_name=symbol_id,
        qualified_name=symbol_id,
        kind="function",
        signature=f"def {symbol_id}() -> str",
        start_byte=0,
        end_byte=10,
        start_line=1,
        end_line=2,
        content_hash=f"hash-{symbol_id}",
        score=0.0,
        provenance="local",
    )


def _ranker(tmp_path: Path, store_name: str, embedder: _RecordingEmbedder) -> SemanticSearchRanker:
    store = tmp_path / store_name
    store.mkdir(parents=True, exist_ok=True)
    return SemanticSearchRanker(tmp_path, store_root=store, embedder=embedder)


def test_embed_symbols_batches_uncached_documents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_EMBED_BATCH_SIZE", "2")
    embedder = _RecordingEmbedder()
    ranker = _ranker(tmp_path, "batch", embedder)
    symbols = [_make_symbol(f"s{i}") for i in range(5)]
    source_texts = {symbol.symbol_id: f"body of {symbol.symbol_id}" for symbol in symbols}

    result = ranker.embed_symbols(symbols, source_texts=source_texts)

    assert set(result) == {symbol.symbol_id for symbol in symbols}
    assert all(len(vector) == 3 for vector in result.values())
    # 5 uncached symbols at batch size 2 -> chunks of 2, 2, 1.
    assert embedder.calls == [2, 2, 1]


def test_embed_symbols_matches_single_symbol_path(tmp_path: Path) -> None:
    symbols = [_make_symbol(f"s{i}") for i in range(3)]
    source_texts = {symbol.symbol_id: f"body of {symbol.symbol_id}" for symbol in symbols}

    batch_ranker = _ranker(tmp_path, "batch", _RecordingEmbedder())
    batched = batch_ranker.embed_symbols(symbols, source_texts=source_texts)

    single_ranker = _ranker(tmp_path, "single", _RecordingEmbedder())
    single = {s.symbol_id: single_ranker.embed_symbol(s, source_text=source_texts[s.symbol_id]) for s in symbols}

    assert batched == single


def test_embed_symbols_reuses_cache_on_second_call(tmp_path: Path) -> None:
    embedder = _RecordingEmbedder()
    ranker = _ranker(tmp_path, "batch", embedder)
    symbols = [_make_symbol(f"s{i}") for i in range(4)]
    source_texts = {symbol.symbol_id: f"body of {symbol.symbol_id}" for symbol in symbols}

    first = ranker.embed_symbols(symbols, source_texts=source_texts)
    calls_after_first = len(embedder.calls)
    second = ranker.embed_symbols(symbols, source_texts=source_texts)

    assert first == second
    # Every symbol served from the vector cache -> no further embed calls.
    assert len(embedder.calls) == calls_after_first


def test_embed_symbols_empty_input_makes_no_calls(tmp_path: Path) -> None:
    embedder = _RecordingEmbedder()
    ranker = _ranker(tmp_path, "batch", embedder)

    assert ranker.embed_symbols([]) == {}
    assert embedder.calls == []
