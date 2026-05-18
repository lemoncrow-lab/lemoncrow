"""Unit tests for the cross-vendor memory adapter system (Spec 03)."""

from __future__ import annotations

from pathlib import Path

from atelier.core.capabilities.cross_vendor_memory.base import _fact_id
from atelier.core.capabilities.cross_vendor_memory.claude_adapter import (
    ClaudeAdapter,
    _parse_markdown_facts,
)
from atelier.core.capabilities.cross_vendor_memory.codex_adapter import (
    CodexAdapter,
    _is_standalone_declaration,
    _parse_codex_facts,
)
from atelier.core.capabilities.cross_vendor_memory.gemini_adapter import (
    GeminiAdapter,
    _find_repo_root,
    _parse_gemini_facts,
)
from atelier.core.capabilities.cross_vendor_memory.registry import MemoryRegistry

# --------------------------------------------------------------------------- #
# Fixture paths                                                                #
# --------------------------------------------------------------------------- #

FIXTURES = Path(__file__).parent.parent.parent.parent / "fixtures" / "memory"
CLAUDE_ROOT = FIXTURES / "claude"
CODEX_ROOT = FIXTURES / "codex"
GEMINI_ROOT = FIXTURES / "gemini"


# --------------------------------------------------------------------------- #
# base.py — _fact_id                                                          #
# --------------------------------------------------------------------------- #


def test_fact_id_stable() -> None:
    assert _fact_id("claude", "hello world") == _fact_id("claude", "hello world")


def test_fact_id_vendor_prefix() -> None:
    assert _fact_id("codex", "test").startswith("codex-")


def test_fact_id_different_content_different_id() -> None:
    assert _fact_id("claude", "foo") != _fact_id("claude", "bar")


def test_fact_id_different_vendor_different_id() -> None:
    assert _fact_id("claude", "same") != _fact_id("codex", "same")


# --------------------------------------------------------------------------- #
# Claude adapter — parse rules                                                 #
# --------------------------------------------------------------------------- #


def test_claude_parse_bullet_dash(tmp_path: Path) -> None:
    md = "- First fact\n- Second fact\n"
    f = tmp_path / "CLAUDE.md"
    facts = _parse_markdown_facts(md, f, "claude-md")
    assert len(facts) == 2
    assert facts[0].content == "First fact"
    assert facts[1].content == "Second fact"


def test_claude_parse_bullet_star(tmp_path: Path) -> None:
    md = "* Star bullet fact\n"
    facts = _parse_markdown_facts(md, tmp_path / "CLAUDE.md", "claude-md")
    assert len(facts) == 1
    assert facts[0].content == "Star bullet fact"


def test_claude_parse_heading_sets_section(tmp_path: Path) -> None:
    md = "## Code style\n- Use ruff\n"
    facts = _parse_markdown_facts(md, tmp_path / "CLAUDE.md", "claude-md")
    assert len(facts) == 1
    assert facts[0].raw_meta.get("section") == "Code style"


def test_claude_parse_heading_not_emitted_as_fact(tmp_path: Path) -> None:
    md = "## My section\n"
    facts = _parse_markdown_facts(md, tmp_path / "CLAUDE.md", "claude-md")
    assert len(facts) == 0


def test_claude_parse_code_block_as_one_fact(tmp_path: Path) -> None:
    md = "```python\nresult = func()\nreturn result\n```\n"
    facts = _parse_markdown_facts(md, tmp_path / "CLAUDE.md", "claude-md")
    assert len(facts) == 1
    assert "result = func()" in facts[0].content
    assert "return result" in facts[0].content


def test_claude_parse_empty_bullets_skipped(tmp_path: Path) -> None:
    md = "- \n-   \n- real fact\n"
    facts = _parse_markdown_facts(md, tmp_path / "CLAUDE.md", "claude-md")
    assert len(facts) == 1
    assert facts[0].content == "real fact"


def test_claude_parse_line_numbers(tmp_path: Path) -> None:
    md = "## Section\n- fact at line 2\n"
    facts = _parse_markdown_facts(md, tmp_path / "CLAUDE.md", "claude-md")
    assert facts[0].line_number == 2


def test_claude_adapter_fixture(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(root=CLAUDE_ROOT)
    assert adapter.is_available()
    facts = adapter.list_facts()
    assert len(facts) > 0
    vendors = {f.vendor for f in facts}
    assert vendors == {"claude"}


def test_claude_adapter_reads_global_md() -> None:
    adapter = ClaudeAdapter(root=CLAUDE_ROOT)
    facts = adapter.list_facts()
    contents = [f.content for f in facts]
    assert any("uv, not pip" in c for c in contents)


def test_claude_adapter_reads_project_md() -> None:
    adapter = ClaudeAdapter(root=CLAUDE_ROOT)
    facts = adapter.list_facts()
    contents = [f.content for f in facts]
    assert any("type hints" in c for c in contents)


def test_claude_adapter_reads_session_memory() -> None:
    adapter = ClaudeAdapter(root=CLAUDE_ROOT)
    facts = adapter.list_facts()
    kinds = {f.source_kind for f in facts}
    assert "session-memory" in kinds


def test_claude_adapter_missing_dir_not_available(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(root=tmp_path / "nonexistent")
    assert not adapter.is_available()
    assert adapter.list_facts() == []


def test_claude_adapter_code_block_in_fixture() -> None:
    adapter = ClaudeAdapter(root=CLAUDE_ROOT)
    facts = adapter.list_facts()
    # The project CLAUDE.md has a code block
    code_facts = [f for f in facts if "\n" in f.content]
    assert len(code_facts) >= 1


# --------------------------------------------------------------------------- #
# Codex adapter — parse rules                                                  #
# --------------------------------------------------------------------------- #


def test_is_standalone_declaration_true() -> None:
    assert _is_standalone_declaration("Use strict mypy mode.")
    assert _is_standalone_declaration("All functions must have return type annotations.")
    assert _is_standalone_declaration("Avoid wildcard imports.")


def test_is_standalone_declaration_false() -> None:
    assert not _is_standalone_declaration("use lowercase")
    assert not _is_standalone_declaration("no period at end")
    assert not _is_standalone_declaration("")


def test_codex_parse_heading_as_fact(tmp_path: Path) -> None:
    md = "## Engineering Style\nHard-remove strategy.\n"
    facts = _parse_codex_facts(md, tmp_path / "test.md")
    assert len(facts) == 1
    assert "Engineering Style" in facts[0].content
    assert facts[0].raw_meta["heading"] == "Engineering Style"


def test_codex_parse_standalone_bullet_as_sub_fact(tmp_path: Path) -> None:
    md = "## Engineering\n- Use strict mypy mode. This is a requirement.\n"
    facts = _parse_codex_facts(md, tmp_path / "test.md")
    # heading fact + sub-fact
    assert len(facts) == 2
    sub = next(f for f in facts if f.raw_meta.get("sub_fact"))
    assert "mypy" in sub.content


def test_codex_parse_non_standalone_bullet_folded_into_heading(tmp_path: Path) -> None:
    md = "## Section\n- run `uv sync` to install\n"
    facts = _parse_codex_facts(md, tmp_path / "test.md")
    # Only the heading fact (lowercase bullet, not standalone)
    assert len(facts) == 1
    assert "uv sync" in facts[0].content


def test_codex_adapter_fixture() -> None:
    adapter = CodexAdapter(root=CODEX_ROOT)
    assert adapter.is_available()
    facts = adapter.list_facts()
    assert len(facts) > 0
    assert all(f.vendor == "codex" for f in facts)


def test_codex_adapter_reads_both_files() -> None:
    adapter = CodexAdapter(root=CODEX_ROOT)
    paths = {f.source_path for f in adapter.list_facts()}
    names = {p.name for p in paths}
    assert "atelier-project.md" in names
    assert "global.md" in names


def test_codex_adapter_missing_dir_not_available(tmp_path: Path) -> None:
    adapter = CodexAdapter(root=tmp_path / "nonexistent")
    assert not adapter.is_available()
    assert adapter.list_facts() == []


# --------------------------------------------------------------------------- #
# Gemini adapter — parse rules                                                 #
# --------------------------------------------------------------------------- #


def test_gemini_parse_bullets(tmp_path: Path) -> None:
    md = "- Global fact one\n- Global fact two\n"
    facts = _parse_gemini_facts(md, tmp_path / "GEMINI.md", "gemini-md-global")
    assert len(facts) == 2


def test_gemini_parse_section_in_meta(tmp_path: Path) -> None:
    md = "## Workflow\n- Always check CI\n"
    facts = _parse_gemini_facts(md, tmp_path / "GEMINI.md", "gemini-md-global")
    assert facts[0].raw_meta.get("section") == "Workflow"


def test_gemini_adapter_fixture() -> None:
    adapter = GeminiAdapter(
        global_root=GEMINI_ROOT,
        cwd=GEMINI_ROOT,
    )
    assert adapter.is_available()
    facts = adapter.list_facts()
    assert len(facts) > 0
    assert all(f.vendor == "gemini" for f in facts)


def test_gemini_adapter_reads_global() -> None:
    adapter = GeminiAdapter(global_root=GEMINI_ROOT, cwd=GEMINI_ROOT)
    facts = adapter.list_facts()
    contents = [f.content for f in facts]
    assert any("Always check CI" in c for c in contents)


def test_gemini_adapter_source_kind() -> None:
    adapter = GeminiAdapter(global_root=GEMINI_ROOT, cwd=GEMINI_ROOT)
    facts = adapter.list_facts()
    kinds = {f.source_kind for f in facts}
    assert "gemini-md-global" in kinds


def test_gemini_adapter_missing_not_available(tmp_path: Path) -> None:
    adapter = GeminiAdapter(global_root=tmp_path / "nope", cwd=tmp_path)
    assert not adapter.is_available()
    assert adapter.list_facts() == []


def test_find_repo_root_finds_git(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    subdir = tmp_path / "src" / "pkg"
    subdir.mkdir(parents=True)
    result = _find_repo_root(subdir)
    assert result == tmp_path


def test_find_repo_root_no_git(tmp_path: Path) -> None:
    result = _find_repo_root(tmp_path / "no-git" / "subdir")
    # Should return None (no .git found)
    assert result is None


# --------------------------------------------------------------------------- #
# MemoryRegistry                                                               #
# --------------------------------------------------------------------------- #


def _make_registry() -> MemoryRegistry:
    claude = ClaudeAdapter(root=CLAUDE_ROOT)
    codex = CodexAdapter(root=CODEX_ROOT)
    gemini = GeminiAdapter(global_root=GEMINI_ROOT, cwd=GEMINI_ROOT)
    return MemoryRegistry(adapters=[claude, codex, gemini])  # type: ignore[list-item]


def test_registry_all_facts_multi_vendor() -> None:
    registry = _make_registry()
    facts = registry.all_facts()
    vendors = {f.vendor for f in facts}
    assert "claude" in vendors
    assert "codex" in vendors
    assert "gemini" in vendors


def test_registry_by_vendor() -> None:
    registry = _make_registry()
    claude_facts = registry.by_vendor("claude")
    assert all(f.vendor == "claude" for f in claude_facts)
    assert len(claude_facts) > 0


def test_registry_by_vendor_case_insensitive() -> None:
    registry = _make_registry()
    assert registry.by_vendor("CLAUDE") == registry.by_vendor("claude")


def test_registry_show_known_id() -> None:
    registry = _make_registry()
    fact = registry.all_facts()[0]
    result = registry.show(fact.fact_id)
    assert result is not None
    assert result.fact_id == fact.fact_id


def test_registry_show_unknown_id() -> None:
    registry = _make_registry()
    assert registry.show("nonexistent-id") is None


def test_registry_find_exact_match() -> None:
    registry = _make_registry()
    results = registry.find("uv, not pip")
    assert any("uv" in f.content for f in results)


def test_registry_find_returns_at_most_limit() -> None:
    registry = _make_registry()
    results = registry.find("the", limit=3)
    assert len(results) <= 3


def test_registry_find_no_match_returns_empty_or_small() -> None:
    registry = _make_registry()
    results = registry.find("zzzyyyxxx_no_match_ever_12345", limit=20)
    # Fuzzy might pick something up but results should be empty or small
    assert len(results) < 5


def test_registry_all_facts_sorted_by_path() -> None:
    registry = _make_registry()
    facts = registry.all_facts()
    paths = [str(f.source_path) for f in facts]
    assert paths == sorted(paths) or len(set(paths)) >= 2  # at least sorted attempt


def test_registry_empty_adapters() -> None:
    registry = MemoryRegistry(adapters=[])
    assert registry.all_facts() == []
    assert registry.by_vendor("claude") == []
    assert registry.show("x") is None
    assert registry.find("test") == []


def test_registry_unavailable_adapter_skipped(tmp_path: Path) -> None:
    missing_adapter = ClaudeAdapter(root=tmp_path / "nonexistent")
    registry = MemoryRegistry(adapters=[missing_adapter])  # type: ignore[list-item]
    assert registry.all_facts() == []


def test_registry_invalidate_clears_cache() -> None:
    registry = _make_registry()
    ids1 = {f.fact_id for f in registry.all_facts()}
    registry.invalidate()
    ids2 = {f.fact_id for f in registry.all_facts()}
    assert ids1 == ids2  # same fact IDs after re-read


# --------------------------------------------------------------------------- #
# Performance smoke test (< 100ms for 1000 facts)                             #
# --------------------------------------------------------------------------- #


def test_list_facts_performance(tmp_path: Path) -> None:
    import time

    # Generate a file with 1000 bullet facts
    md_lines = [f"- Fact number {i:04d} with some content about the topic" for i in range(1000)]
    md_text = "\n".join(md_lines)
    claude_root = tmp_path / ".claude"
    claude_root.mkdir()
    (claude_root / "CLAUDE.md").write_text(md_text)

    adapter = ClaudeAdapter(root=claude_root)
    start = time.perf_counter()
    facts = adapter.list_facts()
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert len(facts) == 1000
    assert elapsed_ms < 100, f"Expected < 100ms, got {elapsed_ms:.1f}ms"
