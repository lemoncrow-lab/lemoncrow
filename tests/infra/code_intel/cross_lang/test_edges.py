from __future__ import annotations

import sqlite3
from pathlib import Path

from atelier.infra.code_intel.cross_lang.edges import CrossLangEdge, CrossLangEdgeStore
from atelier.infra.code_intel.cross_lang.runner import CrossLangRunner


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def test_cross_lang_edge_store_upserts_uniquely_by_source_target_and_kind(tmp_path: Path) -> None:
    db_path = tmp_path / "code.sqlite"
    store = CrossLangEdgeStore(lambda: _connect(db_path))
    edge = CrossLangEdge(
        repo_id="repo",
        src_symbol_id="src:loader",
        src_symbol_name="loader",
        src_qualified_name="pkg.loader",
        src_language="python",
        src_file_path="src/loader.py",
        src_line=4,
        tgt_symbol_name="foo_compute",
        tgt_symbol_id=None,
        tgt_language="c",
        tgt_file_path=None,
        edge_kind="ffi_ctypes",
        confidence=0.45,
    )

    store.upsert_edges(
        [
            edge,
            edge.model_copy(update={"confidence": 0.85, "tgt_symbol_id": "c:foo_compute", "tgt_file_path": "native/foo.c"}),
        ]
    )

    rows = store.query_by_source_symbol("src:loader")

    assert len(rows) == 1
    assert rows[0].tgt_symbol_id == "c:foo_compute"
    assert rows[0].confidence == 0.85
    assert rows[0].edge_kind == "ffi_ctypes"


def test_cross_lang_edge_store_preserves_typed_nullable_resolution_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "code.sqlite"
    store = CrossLangEdgeStore(lambda: _connect(db_path))
    edge = CrossLangEdge(
        repo_id="repo",
        src_symbol_id="src:bootstrap",
        src_symbol_name="bootstrap",
        src_qualified_name="pkg.bootstrap",
        src_language="python",
        src_file_path="src/bootstrap.py",
        src_line=7,
        tgt_symbol_name="pkg.dynamic",
        tgt_symbol_id=None,
        tgt_language="python",
        tgt_file_path=None,
        edge_kind="dynamic_import",
        confidence=0.55,
    )

    store.upsert_edges([edge])
    rows = store.query_by_source_symbol("src:bootstrap")

    assert rows == [edge]
    assert rows[0].model_dump(mode="json")["tgt_symbol_id"] is None
    assert rows[0].model_dump(mode="json")["tgt_file_path"] is None
    assert rows[0].model_dump(mode="json")["confidence"] == 0.55


def test_cross_lang_runner_contract_stays_literal_only_for_phase5(tmp_path: Path) -> None:
    db_path = tmp_path / "code.sqlite"
    runner = CrossLangRunner(
        repo_root=tmp_path,
        repo_id="repo",
        connection_factory=lambda: _connect(db_path),
    )

    assert runner.resolver_names == ("ctypes", "dynamic_import", "subprocess")
    assert runner.scope_ceiling == "literal_only_static_edges"
    assert "external" not in runner.scope_exclusions
    assert "multi_repo" not in runner.scope_exclusions
