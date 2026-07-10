"""Token-savings infra test for search_read (WP-21).

Acceptance criterion: savings are computed against Claude Code's built-in
Grep content output, not against ``grep + read every matched file in full``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from atelier.core.capabilities.prompt_compilation.tokens import approx_tokens as _count_tokens
from atelier.core.capabilities.tool_supervision.search_read import (
    _naive_token_count,
    _run_grep,
    search_read,
    search_read_to_dict,
)

# ---------------------------------------------------------------------------
# Fixture corpus builder
# ---------------------------------------------------------------------------

_SEARCH_PATTERN = "TARGET_SYMBOL"

_FILE_TEMPLATE = textwrap.dedent("""\
    \"\"\"Module {idx}: demonstrates some patterns.\"\"\"

    from __future__ import annotations
    import os
    import sys
    from typing import Any

    CONSTANT_{idx} = "value_{idx}"


    class Handler_{idx}:
        \"\"\"A handler class.\"\"\"

        def __init__(self, config: dict[str, Any]) -> None:
            self.config = config

        def process(self, data: str) -> str:
            # Use TARGET_SYMBOL here to make this file a hit
            marker = "{TARGET_SYMBOL}"
            return data + marker

        def validate(self, value: Any) -> bool:
            return bool(value)

        def transform(self, items: list[Any]) -> list[Any]:
            return [self.process(str(x)) for x in items]

        def finalize(self) -> None:
            pass


    def helper_{idx}(x: int) -> int:
        \"\"\"Standalone helper.\"\"\"
        return x * 2 + {idx}


    def another_function_{idx}() -> None:
        \"\"\"Another function to pad the file.\"\"\"
        for i in range(10):
            _ = helper_{idx}(i)


    def yet_another_{idx}(a: str, b: str) -> str:
        return a + b + str({idx})


    # Additional padding lines to make files realistically sized
    _PAD_{idx}_A = list(range(50))
    _PAD_{idx}_B = {{str(i): i for i in range(20)}}
    _PAD_{idx}_C = [f"item_{{i}}" for i in range(30)]
    _PAD_{idx}_D = {{f"key_{{i}}_{idx}": f"value_{{i}}" for i in range(40)}}
    _PAD_{idx}_E = [None] * 50
    _PAD_{idx}_F = tuple(range(60))
    _PAD_{idx}_G = frozenset(range(20))
    _PAD_{idx}_H = "filler" * 20
    _PAD_{idx}_I = b"binary_filler" * 10
    _PAD_{idx}_J = [f"extra_item_{{j}}" for j in range(50)]
    _EXTRA_CONST_1_{idx} = "extra_value_a"
    _EXTRA_CONST_2_{idx} = "extra_value_b"
    _EXTRA_CONST_3_{idx} = "extra_value_c"
    _EXTRA_CONST_4_{idx} = "extra_value_d"
    _EXTRA_CONST_5_{idx} = "extra_value_e"
    """)


def _build_corpus(root: Path, n_files: int = 20) -> None:
    """Write n_files Python modules, each containing TARGET_SYMBOL once."""
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n_files):
        content = _FILE_TEMPLATE.format(idx=i, TARGET_SYMBOL=_SEARCH_PATTERN)
        (root / f"module_{i:03d}.py").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_search_read_token_savings_use_claude_grep_content_baseline(tmp_path: Path) -> None:
    """search_read must not use full matched-file reads as its baseline."""
    corpus = tmp_path / "corpus"
    _build_corpus(corpus, n_files=20)

    grep_output = _run_grep(_SEARCH_PATTERN, str(corpus))
    file_contents: dict[str, str] = {str(p): p.read_text(encoding="utf-8") for p in sorted(corpus.glob("*.py"))}
    naive_tokens = _naive_token_count(grep_output, file_contents)
    inflated_full_read_tokens = _count_tokens(grep_output + "".join(file_contents.values()))

    result = search_read(
        query=_SEARCH_PATTERN,
        path=str(corpus),
        max_files=20,
        max_chars_per_file=2000,
        include_outline=True,
    )

    smart_tokens = result.total_tokens

    assert len(result.matches) >= 10, f"expected ≥10 hit files, got {len(result.matches)}"
    assert naive_tokens == _count_tokens(grep_output)
    assert inflated_full_read_tokens > naive_tokens
    assert result.tokens_saved_vs_naive == max(0, naive_tokens - smart_tokens)


def test_search_read_token_savings_field_populated(tmp_path: Path) -> None:
    """tokens_saved_vs_naive must be a non-negative integer for a real corpus."""
    corpus = tmp_path / "small_corpus"
    _build_corpus(corpus, n_files=5)

    result = search_read(query=_SEARCH_PATTERN, path=str(corpus), max_files=5)
    assert result.tokens_saved_vs_naive >= 0

    d = search_read_to_dict(result)
    assert isinstance(d["tokens_saved_vs_naive"], int)
    assert isinstance(d["total_tokens"], int)


def test_search_read_result_deterministic_across_calls(tmp_path: Path) -> None:
    """Two consecutive calls on the same corpus must return identical results."""
    corpus = tmp_path / "det_corpus"
    _build_corpus(corpus, n_files=20)

    r1 = search_read_to_dict(search_read(query=_SEARCH_PATTERN, path=str(corpus), max_files=20))
    r2 = search_read_to_dict(search_read(query=_SEARCH_PATTERN, path=str(corpus), max_files=20))

    assert r1["cache_hit"] is False
    assert r2["cache_hit"] is True
    r1.pop("cache_hit", None)
    r2.pop("cache_hit", None)
    assert r1 == r2, "search_read is not deterministic for identical inputs"


def test_naive_token_count_matches_claude_grep_content_output(tmp_path: Path) -> None:
    """Verify naive counting does not include full matched-file contents."""
    corpus = tmp_path / "scale_corpus"
    _build_corpus(corpus, n_files=20)

    grep_output = _run_grep(_SEARCH_PATTERN, str(corpus))
    file_contents = {str(p): p.read_text(encoding="utf-8") for p in sorted(corpus.glob("*.py"))}
    naive_tokens = _naive_token_count(grep_output, file_contents)
    grep_tokens = _count_tokens(grep_output)
    inflated_full_read_tokens = _count_tokens(grep_output + "".join(file_contents.values()))

    assert naive_tokens == grep_tokens
    assert inflated_full_read_tokens > naive_tokens
