"""Integration tests for bench-on vs bench-off tool visibility (MODE-08).

These tests exercise the full pipeline from bootstrap() → mcp_tool_visible_to_llm()
to verify that the bench-off arm hides all LemonCrow MCP tools while the bench-on arm
exposes stable tools as expected.

Marked @pytest.mark.slow — excluded from the default `pytest -m 'not slow'` run.
Run explicitly with:  uv run pytest tests/core/test_bench_mode_integration.py -q -m slow
"""

from __future__ import annotations

import sys

import pytest

# Ensure the mode submodule is loaded so we can access it via sys.modules.
import lemoncrow.bench.mode  # noqa: F401


@pytest.mark.slow
def test_bench_on_vs_off_mcp_tool_counts_differ(monkeypatch: pytest.MonkeyPatch) -> None:
    """bench-off hides more tools than bench-on (MODE-08).

    Asserts:
    - on_count > off_count  (bench-on exposes at least the stable tool set)
    - off_count == 0        (bench-off hides every MCP tool without exception)
    """
    m = sys.modules["lemoncrow.bench.mode"]

    from lemoncrow.core.environment import mcp_tool_visible_to_llm

    visible_sample = ("compact", "context", "read", "verify")

    # ---- bench-on arm -------------------------------------------------------
    monkeypatch.setattr(m, "_mode", None)
    monkeypatch.setenv("LEMONCROW_BENCH_MODE", "on")
    m.bootstrap()
    on_count = sum(1 for t in visible_sample if mcp_tool_visible_to_llm(t))

    # ---- bench-off arm -------------------------------------------------------
    monkeypatch.setattr(m, "_mode", None)
    monkeypatch.setenv("LEMONCROW_BENCH_MODE", "off")
    m.bootstrap()
    off_count = sum(1 for t in visible_sample if mcp_tool_visible_to_llm(t))

    # ---- assertions ----------------------------------------------------------
    assert on_count > off_count, f"Expected on_count({on_count}) > off_count({off_count})"
    assert off_count == 0, f"Expected off_count=0, got {off_count}"
