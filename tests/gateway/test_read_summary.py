"""Tests for the `read` tool's `:summary` and `:outline` suffixes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import lemoncrow.infra.internal_llm as internal_llm
from lemoncrow.core.capabilities.tool_supervision import text_summary
from lemoncrow.gateway.adapters import mcp_server

SMALL_CODE = (
    '"""A small module for testing outline-fits-budget."""\n'
    "\n\n"
    "def alpha(x):\n"
    '    """Alpha does a thing."""\n'
    "    return x + 1\n"
    "\n\n"
    "def beta(x):\n"
    '    """Beta does another thing."""\n'
    "    return x * 2\n"
    "\n\n"
    "class Gamma:\n"
    '    """Gamma groups behavior."""\n'
    "\n"
    "    def method_one(self):\n"
    '        """Method one."""\n'
    "        return 1\n"
    "\n"
    "    def method_two(self):\n"
    '        """Method two."""\n'
    "        return 2\n"
)


def _make_large_code(n: int = 250) -> str:
    """A python module with *n* substantial functions -- large enough that even
    the rendered outline overflows the 4096-char summary budget."""
    parts = ['"""A large module for testing outline overflow."""', ""]
    for i in range(n):
        parts.append(f"def func_{i}(x):")
        parts.append(f'    """Docstring for func_{i} with enough body to earn outline savings."""')
        for j in range(8):
            parts.append(f"    x = x + {j}")
        parts.append("    return x")
        parts.append("")
    return "\n".join(parts)


PROSE_DOC = "\n".join(
    [
        "This document explains the annual report for the fiscal year.",
        "It covers revenue, expenses, and other financial matters in detail.",
        "The company expanded operations into three new regions this year.",
        "Employee headcount grew by twelve percent compared to last year.",
        "Marketing spend increased slightly due to new campaign launches.",
        "Customer satisfaction scores remained steady throughout the year.",
        "The quokka breeding program showed remarkable quokka population growth this quarter.",
        "Researchers tracked quokka health metrics using new quokka monitoring tags.",
        "The zoo's quokka enclosure was renovated to support quokka wellbeing.",
        "Visitor numbers to see the quokka exhibit tripled after media coverage.",
        "Other exhibits saw modest but steady attendance increases as well.",
        "The finance team reconciled quarterly statements without major issues.",
        "Supply chain logistics were optimized to reduce shipping delays.",
        "Staff training programs were updated to reflect new safety standards.",
        "The board approved the budget for next year's capital projects.",
        "In conclusion, the year was marked by steady growth across divisions.",
    ]
)

MARKDOWN_DOC = (
    "# Project Overview\n\n"
    "This project provides a fast, reliable way to process orders. "
    "It integrates with the existing warehouse system.\n\n"
    "## Installation\n\n"
    "Run `pip install project` to get started. Requires Python 3.10 or later.\n\n"
    "## Configuration\n\n"
    "Set the `PROJECT_HOME` environment variable before running. "
    "See the config reference for all options.\n\n"
    "## Usage\n\n"
    "Call `project.run()` to start processing. The function blocks until completion.\n"
)


def _make_log_with_traceback() -> str:
    lines = [f"INFO: heartbeat {i}" for i in range(500)]
    lines.insert(250, "Traceback (most recent call last):")
    lines.insert(251, '  File "app.py", line 42, in run')
    lines.insert(252, "ValueError: unexpected sentinel value 42")
    return "\n".join(lines)


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_LLM_BACKEND", raising=False)
    monkeypatch.delenv("LEMONCROW_OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("LEMONCROW_OPENAI_MODEL", raising=False)


# --------------------------------------------------------------------------- #
# :summary ladder -- code files (outline-fits-budget vs gist-overflow)        #
# --------------------------------------------------------------------------- #


def test_summary_on_small_code_file_returns_outline_when_it_fits_budget(tmp_path: Path) -> None:
    f = tmp_path / "mod.py"
    f.write_text(SMALL_CODE, encoding="utf-8")
    result = mcp_server._smart_read_single(str(f), summary=True)
    assert result["mode"] == "summary"
    body = result["summary"]
    assert "summarized:outline" in body
    assert "alpha" in body and "beta" in body and "Gamma" in body
    # orig->kept accounting in the canonical footer.
    assert f"{len(SMALL_CODE)}→" in body


def test_summary_on_large_code_file_uses_line_capped_outline(tmp_path: Path) -> None:
    # The outline renderer caps symbol lines at _READ_OUTLINE_MAX_LINES with a
    # "+K more" tail, so even a 250-symbol file's outline fits the summary
    # budget -- the capped outline IS the summary (structure + precise pointer
    # beats a prose gist; the old heuristic-gist fallback no longer triggers
    # for outline-able code).
    large = _make_large_code(250)
    f = tmp_path / "big_mod.py"
    f.write_text(large, encoding="utf-8")
    result = mcp_server._smart_read_single(str(f), summary=True)
    assert result["mode"] == "summary"
    body = result["summary"]
    assert "summarized:heuristic" not in body
    assert "symbols:" in body
    assert "func_0" in body
    assert f"... +{250 - mcp_server._READ_OUTLINE_MAX_LINES} more symbols" in body
    assert f"func_{mcp_server._READ_OUTLINE_MAX_LINES}" not in body  # capped, not the full dump


# --------------------------------------------------------------------------- #
# :outline -- explicit force, regardless of file size                        #
# --------------------------------------------------------------------------- #


def test_outline_forces_outline_on_small_code_file(tmp_path: Path) -> None:
    f = tmp_path / "mod.py"
    f.write_text(SMALL_CODE, encoding="utf-8")
    # Default (no outline=True): well under the automatic outline threshold.
    default_result = mcp_server._smart_read_single(str(f))
    assert default_result["mode"] == "full"
    forced_result = mcp_server._smart_read_single(str(f), outline=True)
    assert forced_result["mode"] == "outline"
    assert "alpha" in json.dumps(forced_result["outline"])


def test_outline_on_non_code_file_errors_instead_of_falling_back(tmp_path: Path) -> None:
    f = tmp_path / "notes.txt"
    f.write_text("plain prose notes, not a code file.\n" * 20, encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        mcp_server._smart_read_single(str(f), outline=True)
    message = str(excinfo.value)
    assert "no outline available" in message
    assert "not a code file" in message
    assert ":summary" in message


def test_summary_and_outline_precedence_serves_outline(tmp_path: Path) -> None:
    """`:summary`+`:outline` no longer errors -- the more detailed view (outline)
    wins and `:summary` is silently dropped."""
    f = tmp_path / "mod.py"
    f.write_text(SMALL_CODE, encoding="utf-8")
    result = mcp_server._smart_read_single(str(f), summary=True, outline=True)
    assert result["mode"] == "outline"
    assert "alpha" in json.dumps(result["outline"])


def test_outline_and_expand_precedence_serves_expand(tmp_path: Path) -> None:
    """`:outline`+`:full` no longer errors -- the more detailed view (expand,
    full source) wins and `:outline` is silently dropped."""
    f = tmp_path / "mod.py"
    f.write_text(SMALL_CODE, encoding="utf-8")
    result = mcp_server._smart_read_single(str(f), outline=True, expand=True)
    assert result["mode"] == "full"
    assert result["content"] == SMALL_CODE


def test_range_wins_over_summary_and_outline(tmp_path: Path) -> None:
    """An explicit range is the most specific request -- it wins over both
    `:summary` and `:outline` rather than being silently dropped by them."""
    f = tmp_path / "mod.py"
    f.write_text(SMALL_CODE, encoding="utf-8")
    result = mcp_server._smart_read_single(str(f), range="L1-L2", summary=True, outline=True)
    assert result["mode"] == "range"


# --------------------------------------------------------------------------- #
# dict form, spill files, range-suffix parsing                               #
# --------------------------------------------------------------------------- #


def test_summary_dict_form_via_files_batch(tmp_path: Path) -> None:
    f = tmp_path / "notes.txt"
    f.write_text(PROSE_DOC * 5, encoding="utf-8")
    result = mcp_server.tool_smart_read({"files": [{"path": str(f), "summary": True}]})
    entry = result["files"][0]
    assert entry["mode"] == "summary"
    assert "summarized:heuristic" in entry["summary"]


def test_summary_on_spill_file_uses_log_tier_and_single_footer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spill_dir = tmp_path / "lemoncrow-spill"
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(spill_dir))
    from lemoncrow.core.capabilities.tool_supervision import tool_output_spill

    record = tool_output_spill.spill(_make_log_with_traceback(), tool_name="bash")
    assert record is not None

    result = mcp_server._smart_read_single(str(record.path), summary=True)
    assert result["mode"] == "summary"
    body = result["summary"]
    assert body.count("[lc: summarized:") == 1
    assert "summarized:heuristic" in body
    assert "ValueError: unexpected sentinel value 42" in body
    assert str(record.path) in body


def test_summary_suffix_not_eaten_by_range_parser() -> None:
    assert mcp_server._split_read_range_suffix("foo.py:summary") == ("foo.py:summary", None)
    path, rng, expand, head, tail, summary, outline = mcp_server._split_file_opts("foo.py:summary")
    assert (path, rng, expand, head, tail, summary, outline) == ("foo.py", None, False, None, None, True, False)


def test_summary_with_range_suffix_parses_both_but_range_wins(tmp_path: Path) -> None:
    path, rng, _expand, _head, _tail, summary, _outline = mcp_server._split_file_opts("foo.py:L10-L20:summary")
    assert path == "foo.py"
    assert rng == "L10-L20"
    assert summary is True

    f = tmp_path / "mod.py"
    f.write_text(SMALL_CODE, encoding="utf-8")
    result = mcp_server._smart_read_single(str(f), range="L1-L2", summary=True)
    assert result["mode"] == "range"


# --------------------------------------------------------------------------- #
# Internal-LLM tier: used when available, silent fallback on any failure     #
# --------------------------------------------------------------------------- #


def test_summary_uses_llm_tier_when_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "ollama")
    monkeypatch.setenv("LEMONCROW_OLLAMA_MODEL", "qwen2.5")
    monkeypatch.setattr(internal_llm, "summarize", lambda text, **kw: "An LLM-produced gist of the document.")

    f = tmp_path / "notes.txt"
    f.write_text(PROSE_DOC * 5, encoding="utf-8")
    result = mcp_server._smart_read_single(str(f), summary=True)
    body = result["summary"]
    assert "An LLM-produced gist of the document." in body
    assert "summarized:qwen2.5" in body


def test_summary_llm_failure_falls_back_silently_to_heuristic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "ollama")

    def _boom(text: str, **kw: object) -> str:
        raise internal_llm.InternalLLMError("local model unreachable")

    monkeypatch.setattr(internal_llm, "summarize", _boom)

    f = tmp_path / "notes.txt"
    f.write_text(PROSE_DOC * 5, encoding="utf-8")
    result = mcp_server._smart_read_single(str(f), summary=True)
    body = result["summary"]
    assert "summarized:heuristic" in body
    assert "An LLM-produced gist" not in body


# --------------------------------------------------------------------------- #
# heuristic_summary() gist quality, by type (no LLM involved)                #
# --------------------------------------------------------------------------- #


def test_heuristic_summary_markdown_preserves_heading_tree() -> None:
    out = text_summary.heuristic_summary(MARKDOWN_DOC, path="README.md")
    assert "# Project Overview" in out
    assert "## Installation" in out
    assert "## Configuration" in out
    assert "## Usage" in out


def test_heuristic_summary_json_names_top_level_keys() -> None:
    doc = json.dumps(
        {
            "name": "widget-service",
            "version": "3.2.1",
            "enabled": True,
            "tags": ["a", "b", "c"],
            "config": {"retries": 3, "timeout": 30},
        }
    )
    out = text_summary.heuristic_summary(doc, path="config.json")
    for key in ("name", "version", "enabled", "tags", "config"):
        assert key in out


def test_heuristic_summary_log_surfaces_injected_traceback() -> None:
    out = text_summary.heuristic_summary(_make_log_with_traceback(), path="run.log")
    assert "Traceback (most recent call last)" in out
    assert "ValueError: unexpected sentinel value 42" in out


def test_heuristic_summary_prose_includes_middle_high_frequency_sentence() -> None:
    out = text_summary.heuristic_summary(PROSE_DOC, path="report.txt", target_chars=300)
    assert "quokka" in out.lower()
    # Proves selection, not head+tail: the opening sentence alone wouldn't
    # mention the topic at all.
    assert "annual report" in out


def test_heuristic_summary_code_names_symbols() -> None:
    out = text_summary.heuristic_summary(SMALL_CODE, path="mod.py")
    assert "alpha" in out
    assert "beta" in out
    assert "Gamma" in out
    assert "more symbols" not in out


def test_heuristic_summary_code_symbol_inventory_escalation_marker() -> None:
    large = _make_large_code(30)
    out = text_summary.heuristic_summary(large, path="big_mod.py")
    assert "func_0" in out
    assert "more symbols; :outline for full structure" in out
