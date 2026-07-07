"""Tests for the model-facing code-intel renderers (line-numbered explore + node body)."""

from __future__ import annotations

from atelier.core.capabilities.code_context.renderer import render_code_payload


def test_render_explore_files_shape_with_numbers_and_skeleton_notice() -> None:
    payload = {
        "query": "Embedder",
        "files": [
            {
                "file_path": "src/alpha.py",
                "language": "python",
                "symbols": [{"qualified_name": "AlphaEmbedder", "kind": "class", "start_line": 1}],
                "source_sections": [
                    {
                        "file_path": "src/alpha.py",
                        "start_line": 1,
                        "end_line": 2,
                        "symbol_id": "a",
                        "content": "1\tclass AlphaEmbedder:\n2\t    def embed(self): ...",
                    }
                ],
            },
            {
                "file_path": "src/beta.py",
                "language": "python",
                "symbols": [{"qualified_name": "BetaEmbedder", "kind": "class", "start_line": 1}],
                "source_sections": [
                    {
                        "file_path": "src/beta.py",
                        "start_line": 1,
                        "end_line": 1,
                        "symbol_id": "b",
                        "content": "1\tclass BetaEmbedder:",
                        "skeleton": True,
                        "tokens_saved": 12,
                    }
                ],
            },
        ],
        "relationships": {
            "callers": [
                {
                    "symbol_id": "a",
                    "symbol_name": "AlphaEmbedder",
                    "related": [{"qualified_name": "factory.make", "file_path": "src/factory.py", "line": 5}],
                }
            ],
            "callees": [],
            "usages": [],
        },
        "additional_relevant_files": ["src/gamma.py"],
    }
    out = render_code_payload("explore", payload)
    assert out is not None
    assert "#### src/alpha.py" in out
    assert ":L1-L2" in out  # line range in header
    assert "AlphaEmbedder" in out  # symbol name present
    assert "class AlphaEmbedder:" in out  # no tab prefix in new format
    assert "· skeleton" in out  # skeleton marker in header
    assert "#### callers" in out
    assert "- src/factory.py" in out
    assert "  - 5 — factory.make" in out
    assert "#### additional_relevant_files" in out
    assert "- src/gamma.py" in out


def test_render_explore_items_fallback_for_legacy_shape() -> None:
    payload = {"items": [{"file_path": "src/x.py", "qualified_name": "Foo", "source": "def foo(): ..."}]}
    out = render_code_payload("explore", payload)
    assert out is not None
    assert "src/x.py — Foo" in out
    assert "def foo(): ..." in out


def test_render_explore_empty_is_no_results() -> None:
    assert render_code_payload("explore", {"files": []}) == "no results"


def test_render_node_includes_line_numbered_body() -> None:
    payload = {
        "symbol_id": "s",
        "qualified_name": "Foo.bar",
        "symbol_name": "bar",
        "kind": "method",
        "signature": "def bar(self) -> int",
        "file_path": "src/foo.py",
        "start_line": 10,
        "end_line": 12,
        "language": "python",
        "source": "def bar(self):\n    x = 1\n    return x\n",
    }
    out = render_code_payload("node", payload)
    assert out is not None
    assert "- signature: def bar(self) -> int" in out
    assert "#### " in out  # header present
    assert "10\tdef bar(self):" in out
    assert "11\t    x = 1" in out
    assert "12\t    return x" in out


def test_render_node_truncates_huge_body_to_head() -> None:
    source = "\n".join(f"line{i}" for i in range(500)) + "\n"
    payload = {
        "symbol_id": "s",
        "qualified_name": "big",
        "kind": "function",
        "file_path": "src/big.py",
        "start_line": 1,
        "end_line": 500,
        "language": "python",
        "source": source,
    }
    out = render_code_payload("node", payload)
    assert out is not None
    assert "first 120 of 500 lines" in out
    assert "1\tline0" in out
    assert "120\tline119" in out
    assert "121\tline120" not in out  # body capped at the head window
