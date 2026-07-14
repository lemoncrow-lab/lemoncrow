from __future__ import annotations

import pytest

from lemoncrow.infra.internal_llm import InternalLLMError
from lemoncrow.pro.capabilities.tool_supervision.compact_output import compact


def test_compact_passthrough_under_threshold() -> None:
    # "short output" is well under the 1800-char threshold
    result = compact("short output", content_type="tool_output")
    assert result.method == "passthrough"
    assert result.compacted == "short output"


def test_compact_passthrough_boundary() -> None:
    # Exactly at threshold: passthrough; over threshold: truncate
    at_limit = "a" * 1800
    result = compact(at_limit, content_type="unknown")
    assert result.method == "passthrough"

    over_limit = "a" * 1801
    result2 = compact(over_limit, content_type="unknown")
    assert result2.method == "deterministic_truncate"


def test_compact_groups_grep_output_deterministically() -> None:
    content = "\n".join(f"src/app.py:{i}: hit" for i in range(800))
    result = compact(content, content_type="grep", budget_tokens=80)
    assert result.method == "deterministic_truncate"
    # A single file's overflow is summarized once, not scattered.
    assert result.compacted.count("more in src/app.py") == 1
    assert "more in src/app.py" in result.compacted
    assert result.compacted_tokens < result.original_tokens


def test_compact_grep_keeps_more_hits_when_budget_allows() -> None:
    # The 4th+ hit must not be silently dropped: a larger budget keeps more.
    content = "\n".join(f"src/app.py:{i}: hit" for i in range(800))
    small = compact(content, content_type="grep", budget_tokens=40)
    large = compact(content, content_type="grep", budget_tokens=4000)
    small_hits = small.compacted.count(": hit")
    large_hits = large.compacted.count(": hit")
    assert large_hits > small_hits
    assert large_hits > 3


def test_compact_grep_does_not_scatter_context_lines() -> None:
    # Group separators and context lines stay attached to their file instead of
    # spawning pseudo-file buckets that fragment a single file's matches.
    content = "\n".join(
        [
            "src/app.py:1: alpha",
            "src/app.py-2- context",
            "--",
            "src/app.py:3: beta",
        ]
        + [f"src/app.py:{i}: more match line {i}" for i in range(4, 200)]
    )
    result = compact(content, content_type="grep", budget_tokens=40)
    # Only the real file is summarized — no "unknown" pseudo-file from the
    # separator / context lines.
    assert "more in unknown" not in result.compacted
    assert "more in src/app.py" in result.compacted


def test_compact_uses_llm_when_enable_llm_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.tool_supervision.compact_output.summarize",
        lambda prompt, max_tokens=500: "llm compacted",
    )
    result = compact("alpha " * 2500, content_type="bash", enable_llm=True)
    assert result.method == "llm_summary"
    assert result.compacted == "llm compacted"


def test_compact_does_not_use_llm_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM should NOT be tried unless enable_llm=True — head/tail is enough."""
    called = []

    def _should_not_be_called(prompt: str, max_tokens: int = 500) -> str:
        called.append(prompt)
        return "llm compacted"

    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.tool_supervision.compact_output.summarize",
        _should_not_be_called,
    )
    result = compact("alpha " * 2500, content_type="bash")
    assert result.method == "deterministic_truncate"
    assert not called, "LLM should not be called without enable_llm=True"


def test_compact_large_output_falls_back_when_llm_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(prompt: str, max_tokens: int = 500) -> str:
        _ = (prompt, max_tokens)
        raise InternalLLMError("offline")

    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.tool_supervision.compact_output.summarize",
        unavailable,
    )
    result = compact("alpha " * 2500, content_type="unknown", budget_tokens=100, enable_llm=True)
    assert result.method == "deterministic_truncate"
    assert result.compacted_tokens < result.original_tokens


def test_compress_tool_output_standalone() -> None:
    """compress_tool_output() standalone helper."""
    from lemoncrow.pro.capabilities.tool_supervision.compact_output import compress_tool_output

    short = "hello world"
    assert compress_tool_output(short) == short

    big = "x" * 2000
    out = compress_tool_output(big, threshold_chars=1800, head_chars=900, tail_chars=700)
    assert out.startswith("x" * 900)
    assert out.endswith("x" * 700)
    assert "400 chars omitted" in out
    assert len(out) < len(big)


def test_head_tail_asymmetric_split() -> None:
    """Head should be ~60% of budget, tail ~40% (more signal at start)."""
    content = "HEADER" + "M" * 2000 + "TAIL"
    result = compact(content, content_type="unknown", budget_tokens=250)
    assert result.method == "deterministic_truncate"
    # Head (600 chars of 1000 budget) should include "HEADER"
    assert "HEADER" in result.compacted
    assert "TAIL" in result.compacted
