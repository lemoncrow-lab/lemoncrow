from __future__ import annotations

from pathlib import Path

from benchmarks.codebench.graft_eval import parse_find_all_paths, parse_find_code_paths
from benchmarks.mcp_tools import bench_external_indexers


def test_parse_find_code_paths_preserves_rank_and_ignores_inlined_source() -> None:
    text = """graph refreshed\ngraft ask — \"router\"  (lexical)\n\n1. buildRouter  [function]\n   src/router.ts:L10-L25\n   router implementation\n\n```\nconst example = \"docs/not-a-ranked-hit.md\";\n```\n2. RouterConfig  [class]\n   src/config.ts:L3-L9\n3. duplicate  [function]\n   src/router.ts:L30-L31\n"""

    assert parse_find_code_paths(text) == ["src/router.ts", "src/config.ts"]


def test_parse_find_code_paths_supports_structural_output() -> None:
    text = """graft ask — \"who calls render\"  (structural)\n\n- callRender  src/view.ts:L40-L48  (calls)\n- renderTest  tests/view.test.ts:L5-L8  (references)\n"""

    assert parse_find_code_paths(text) == ["src/view.ts", "tests/view.test.ts"]


def test_parse_find_all_paths_reads_only_ranked_group_headers() -> None:
    text = """\"token\" — 3 hits in 3 symbols across 2 files\n\nparseToken · function · src/parser.py:L10-L20 · 7 in-edges\n  L12: token = read()\n\nsrc/constants.py (module level) · 0 in-edges\n  L1: TOKEN = \"token\"\n\nparseAgain · method · src/parser.py:L30-L35 · 1 in-edges\n  L31: return token\n"""

    assert parse_find_all_paths(text) == ["src/parser.py", "src/constants.py"]


def test_ensure_graft_reuses_local_benchmark_install(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bench_external_indexers, "bench_tools_root", lambda: tmp_path)
    graft_bin = tmp_path / "graft" / "node_modules" / ".bin" / "graft"
    graft_bin.parent.mkdir(parents=True)
    graft_bin.write_text("#!/bin/sh\n")

    assert bench_external_indexers.ensure_graft() == graft_bin
