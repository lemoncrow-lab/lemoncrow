"""Tests for index-free sibling skeletonization in tool_explore (codegraph parity)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.core.capabilities.code_context import CodeContextEngine
from lemoncrow.core.capabilities.code_context.models import SymbolRecord

_EMBEDDER_NAMES = [
    "AlphaEmbedder",
    "BetaEmbedder",
    "GammaEmbedder",
    "DeltaEmbedder",
    "EpsilonEmbedder",
    "ZetaEmbedder",
    "EtaEmbedder",
]


def _embedder_source(name: str) -> str:
    return (
        f"class {name}:\n"
        "    def __init__(self, dim):\n"
        "        self.dim = dim\n"
        "        self.cache = {}\n"
        "        self.count = 0\n"
        "    def embed(self, text):\n"
        "        self.count += 1\n"
        "        vector = [0.0] * self.dim\n"
        "        for index, char in enumerate(text):\n"
        "            vector[index % self.dim] += float(ord(char))\n"
        "        self.cache[text] = vector\n"
        "        return vector\n"
        "    def reset(self):\n"
        "        self.cache.clear()\n"
        "        self.count = 0\n"
    )


def _make_record(engine: CodeContextEngine, name: str, *, score: float) -> SymbolRecord:
    path = f"src/{name.lower()}.py"
    source = _embedder_source(name)
    return SymbolRecord(
        symbol_id=f"{path}:{name}",
        repo_id=engine.repo_id,
        file_path=path,
        language="python",
        symbol_name=name,
        qualified_name=name,
        kind="class",
        signature=f"class {name}",
        start_byte=0,
        end_byte=len(source.encode("utf-8")),
        start_line=1,
        end_line=source.count("\n"),
        content_hash=f"hash-{name}",
        score=score,
    )


def _build_sibling_repo(tmp_path: Path, engine: CodeContextEngine) -> list[SymbolRecord]:
    (tmp_path / "src").mkdir(exist_ok=True)
    records: list[SymbolRecord] = []
    for offset, name in enumerate(_EMBEDDER_NAMES):
        (tmp_path / "src" / f"{name.lower()}.py").write_text(_embedder_source(name), encoding="utf-8")
        records.append(_make_record(engine, name, score=1.0 - offset * 0.01))
    return records


def test_skeleton_affixes_splits_camel_and_filters_stopwords(tmp_path: Path) -> None:
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    assert engine._skeleton_affixes("AlphaEmbedder") == ["embedder", "alpha"]
    assert engine._skeleton_affixes("OrderService.calculate_total") == ["total", "calculate"]
    # All tokens are generic stopwords / too short -> no affix, never a family.
    assert engine._skeleton_affixes("get") == []
    assert engine._skeleton_affixes("handle_data") == []


def test_skeletonize_source_keeps_signatures_drops_bodies(tmp_path: Path) -> None:
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    skel = engine._skeletonize_source(
        _embedder_source("AlphaEmbedder"),
        file_path="src/alpha.py",
        start_line=10,
        language="python",
        line_numbers=True,
    )
    assert skel is not None
    # Class + method signature lines survive, numbered from the symbol start line.
    assert "10\tclass AlphaEmbedder:" in skel
    assert "def embed(self, text):" in skel
    assert "def reset(self):" in skel
    # Statement bodies are dropped.
    assert "self.count += 1" not in skel
    assert "return vector" not in skel


def test_select_skeleton_symbols_keeps_one_exemplar_per_family(tmp_path: Path) -> None:
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    records = _build_sibling_repo(tmp_path, engine)
    skeleton_ids, families = engine._select_skeleton_symbols(records, seed_set=set())
    # 7 siblings -> 1 exemplar kept full, 6 skeletoned.
    assert len(skeleton_ids) == len(records) - 1
    # Highest-scored member (first, score 1.0) is the exemplar, never skeletoned.
    assert records[0].symbol_id not in skeleton_ids
    assert all(family == "embedder:class" for family in families.values())


def test_select_skeleton_symbols_protects_seed_and_exemplar(tmp_path: Path) -> None:
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    records = _build_sibling_repo(tmp_path, engine)
    seed = {records[1].file_path}
    skeleton_ids, _ = engine._select_skeleton_symbols(records, seed_set=seed)
    assert records[1].symbol_id not in skeleton_ids  # seed-protected
    # The highest-scored family member is always the exemplar, never skeletoned.
    assert records[0].symbol_id not in skeleton_ids


def test_select_skeleton_symbols_inert_below_family_threshold(tmp_path: Path) -> None:
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    records = _build_sibling_repo(tmp_path, engine)[:2]  # only 2 siblings
    skeleton_ids, families = engine._select_skeleton_symbols(records, seed_set=set())
    assert skeleton_ids == set()
    assert families == {}


def test_select_skeleton_symbols_ignores_small_bodies(tmp_path: Path) -> None:
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    records = _build_sibling_repo(tmp_path, engine)
    for record in records:
        record.end_line = record.start_line + 3  # body now < min skeleton size
    skeleton_ids, _ = engine._select_skeleton_symbols(records, seed_set=set())
    assert skeleton_ids == set()


def test_tool_explore_skeletonizes_sibling_family(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    records = _build_sibling_repo(tmp_path, engine)
    engine.index_repo()
    monkeypatch.setattr(engine, "search_symbols", lambda *args, **kwargs: list(records))

    payload = engine.tool_explore(
        query="Embedder", max_files=8, max_symbols=20, skeletonize=True, complete_families=False, budget_tokens=30000
    )
    assert payload.get("skeletonized"), "expected sibling family to be skeletonized"
    assert int(payload.get("skeleton_tokens_saved") or 0) > 0
    # The exemplar's full body survives somewhere; at least one section is skeletoned.
    sections = [section for file_entry in payload["files"] for section in file_entry.get("source_sections", [])]
    assert any(section.get("skeleton") for section in sections)
    assert any(not section.get("skeleton") for section in sections)


def test_tool_explore_skeletonize_false_is_inert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    records = _build_sibling_repo(tmp_path, engine)
    engine.index_repo()
    monkeypatch.setattr(engine, "search_symbols", lambda *args, **kwargs: list(records))

    payload = engine.tool_explore(query="Embedder", max_files=8, max_symbols=20, skeletonize=False)
    assert "skeletonized" not in payload
    sections = [section for file_entry in payload["files"] for section in file_entry.get("source_sections", [])]
    assert all(not section.get("skeleton") for section in sections)
    # Full bodies are present verbatim (numbered).
    joined = "\n".join(str(section.get("content") or "") for section in sections)
    assert "return vector" in joined


def test_tool_explore_env_flag_disables_skeletonization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_EXPLORE_SKELETON", "0")
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    records = _build_sibling_repo(tmp_path, engine)
    engine.index_repo()
    monkeypatch.setattr(engine, "search_symbols", lambda *args, **kwargs: list(records))

    payload = engine.tool_explore(query="Embedder", max_files=8, max_symbols=20, skeletonize=True)
    assert "skeletonized" not in payload


def test_tool_explore_completes_sibling_family_from_single_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    records = _build_sibling_repo(tmp_path, engine)
    engine.index_repo()
    # Search finds only the seed -- mirrors FTS missing camelCase siblings.
    monkeypatch.setattr(engine, "search_symbols", lambda *args, **kwargs: [records[0]])

    payload = engine.tool_explore(query="Embedder", max_files=8, max_symbols=20, skeletonize=True, budget_tokens=30000)
    files = {str(entry.get("file_path") or entry.get("path")) for entry in payload["files"]}
    # Family-completion surfaced siblings the search missed.
    assert len(files) >= 3
    assert payload.get("skeletonized"), "completed family should be skeletonized"
    sections = [section for entry in payload["files"] for section in entry.get("source_sections", [])]
    assert any(section.get("skeleton") for section in sections)
    assert any(not section.get("skeleton") for section in sections)


def test_tool_explore_complete_families_false_keeps_seed_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    records = _build_sibling_repo(tmp_path, engine)
    engine.index_repo()
    monkeypatch.setattr(engine, "search_symbols", lambda *args, **kwargs: [records[0]])

    payload = engine.tool_explore(
        query="Embedder", max_files=8, max_symbols=20, skeletonize=True, complete_families=False, budget_tokens=30000
    )
    files = {str(entry.get("file_path") or entry.get("path")) for entry in payload["files"]}
    assert len(files) == 1  # completion disabled -> only the seed's file


def test_tool_explore_tolerates_missing_indexed_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    records = _build_sibling_repo(tmp_path, engine)
    engine.index_repo()
    # Delete an indexed file so the index references a path absent from disk
    # (deleted/moved/snapshot-excluded). explore must degrade, not crash.
    (tmp_path / records[2].file_path).unlink()
    monkeypatch.setattr(engine, "search_symbols", lambda *args, **kwargs: list(records))

    payload = engine.tool_explore(query="Embedder", max_files=8, max_symbols=20, skeletonize=True, budget_tokens=30000)
    assert isinstance(payload, dict)
    assert "files" in payload
