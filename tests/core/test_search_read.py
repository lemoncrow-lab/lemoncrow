"""Tests for the search_read core capability (WP-21)."""

from __future__ import annotations

from pathlib import Path

import pytest

from atelier.core.capabilities.tool_supervision.search_read import (
    FileMatch,
    SearchReadResult,
    Snippet,
    _assert_safe_args,
    _cluster_snippets,
    _detect_lang,
    _parse_grep_output,
    search_read,
    search_read_to_dict,
)

# ---------------------------------------------------------------------------
# Unit: _detect_lang
# ---------------------------------------------------------------------------


def test_detect_lang_python() -> None:
    assert _detect_lang("foo/bar.py") == "python"


def test_detect_lang_typescript() -> None:
    assert _detect_lang("src/widget.tsx") == "typescript"


def test_detect_lang_fallback() -> None:
    assert _detect_lang("Makefile") == "text"


# ---------------------------------------------------------------------------
# Unit: _assert_safe_args
# ---------------------------------------------------------------------------


def test_safe_args_accepts_plain_query() -> None:
    _assert_safe_args("Playbook", "src/")  # must not raise


def test_safe_args_rejects_semicolons() -> None:
    with pytest.raises(ValueError, match="metacharacter"):
        _assert_safe_args("foo; rm -rf /", ".")


def test_safe_args_rejects_backtick() -> None:
    with pytest.raises(ValueError, match="metacharacter"):
        _assert_safe_args("`ls`", ".")


def test_safe_args_rejects_leading_dash() -> None:
    with pytest.raises(ValueError, match="must not start"):
        _assert_safe_args("-e expression", ".")


def test_safe_args_rejects_path_with_pipe() -> None:
    with pytest.raises(ValueError, match="metacharacter"):
        _assert_safe_args("foo", "src | cat")


# ---------------------------------------------------------------------------
# Unit: _parse_grep_output
# ---------------------------------------------------------------------------


def test_parse_grep_output_basic() -> None:
    raw = "src/foo.py:42: def example():\nsrc/bar.py:7: x = example()\nsrc/foo.py:100: return None\n"
    hits = _parse_grep_output(raw)
    assert hits["src/foo.py"] == [42, 100]
    assert hits["src/bar.py"] == [7]


def test_parse_grep_output_empty() -> None:
    assert _parse_grep_output("") == {}


def test_parse_grep_output_malformed_lines_skipped() -> None:
    raw = "not-a-match\nsrc/ok.py:5: def foo():\n"
    hits = _parse_grep_output(raw)
    assert list(hits.keys()) == ["src/ok.py"]


# ---------------------------------------------------------------------------
# Unit: _cluster_snippets
# ---------------------------------------------------------------------------


def _make_lines(n: int) -> list[str]:
    return [f"line {i}" for i in range(1, n + 1)]


def test_cluster_snippets_single_match() -> None:
    lines = _make_lines(50)
    snippets = _cluster_snippets([25], lines, context=3)
    assert len(snippets) == 1
    sn = snippets[0]
    assert sn.line_start == 22  # 25 - 3
    assert sn.line_end == 28  # 25 + 3


def test_cluster_snippets_merges_nearby() -> None:
    lines = _make_lines(50)
    # Two close matches should merge
    snippets = _cluster_snippets([10, 12], lines, context=3)
    assert len(snippets) == 1


def test_cluster_snippets_keeps_distant_separate() -> None:
    lines = _make_lines(100)
    # Two matches far apart should stay separate
    snippets = _cluster_snippets([5, 90], lines, context=3)
    assert len(snippets) == 2


def test_cluster_snippets_score_higher_for_dense() -> None:
    lines = _make_lines(100)
    sparse = _cluster_snippets([5], lines, context=8)
    dense = _cluster_snippets([5, 6, 7, 8, 9], lines, context=8)
    assert dense[0].score > sparse[0].score


# ---------------------------------------------------------------------------
# Integration: search_read on real files
# ---------------------------------------------------------------------------


def test_search_read_finds_matches_in_fixture_dir(tmp_path: Path) -> None:
    # Write a small corpus
    (tmp_path / "alpha.py").write_text(
        "class Playbook:\n    def __init__(self):\n        self.data = {}\n",
        encoding="utf-8",
    )
    (tmp_path / "beta.py").write_text(
        "from alpha import Playbook\n\ndef use():\n    rb = Playbook()\n    return rb\n",
        encoding="utf-8",
    )

    result = search_read(query="Playbook", path=str(tmp_path))

    assert isinstance(result, SearchReadResult)
    assert len(result.matches) >= 1
    paths = {m.path for m in result.matches}
    assert any("alpha.py" in p for p in paths)


def test_search_read_result_is_deterministic(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text(
        "\n".join(f"def func_{i}(): pass  # pattern" for i in range(30)),
        encoding="utf-8",
    )

    r1 = search_read(query="pattern", path=str(tmp_path))
    r2 = search_read(query="pattern", path=str(tmp_path))

    d1 = search_read_to_dict(r1)
    d2 = search_read_to_dict(r2)
    d1.pop("cache_hit", None)
    d2.pop("cache_hit", None)
    assert d1 == d2


def test_search_read_cache_hit_on_repeat(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text("x = 'pattern'\n", encoding="utf-8")

    first = search_read(query="pattern", path=str(tmp_path))
    second = search_read(query="pattern", path=str(tmp_path))

    assert first.cache_hit is False
    assert second.cache_hit is True


def test_search_read_respects_cache_disabled_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "module.py").write_text("x = 'pattern'\n", encoding="utf-8")
    monkeypatch.setenv("ATELIER_CACHE_DISABLED", "1")

    first = search_read(query="pattern", path=str(tmp_path))
    second = search_read(query="pattern", path=str(tmp_path))

    assert first.cache_hit is False
    assert second.cache_hit is False


def test_search_read_preserves_existing_smart_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src.py").write_text("value = 'needle'\n", encoding="utf-8")

    state_dir = tmp_path / ".atelier"
    state_dir.mkdir(parents=True, exist_ok=True)
    smart_state = state_dir / "smart_state.json"
    smart_state.write_text(
        '{"mode": "replace", "savings": {"calls_avoided": 7}, "cache": {}}',
        encoding="utf-8",
    )

    result = search_read(query="needle", path=str(tmp_path / "src.py"))
    assert result.cache_hit is False

    persisted = smart_state.read_text(encoding="utf-8")
    assert '"mode": "replace"' in persisted
    assert '"calls_avoided": 7' in persisted


def test_search_read_respects_max_files(tmp_path: Path) -> None:
    for i in range(8):
        (tmp_path / f"file_{i}.py").write_text(f"x = 'needle'  # file {i}\n", encoding="utf-8")

    result = search_read(query="needle", path=str(tmp_path), max_files=3)
    assert len(result.matches) <= 3


def test_search_read_attaches_outline_for_dense_files(tmp_path: Path) -> None:
    # Create a file with >5 matches so outline is requested
    lines = ["class Foo:"] + [f"    def method_{i}(self):  # target\n        pass" for i in range(10)]
    (tmp_path / "dense.py").write_text("\n".join(lines), encoding="utf-8")

    result = search_read(query="target", path=str(tmp_path))

    assert len(result.matches) == 1
    m = result.matches[0]
    # Dense file: only top-3 snippets
    assert len(m.snippets) <= 3
    # Outline should be present for python file
    assert m.outline is not None
    assert "symbols" in m.outline


def test_search_read_no_outline_flag(tmp_path: Path) -> None:
    lines = ["class Foo:"] + [f"    def m_{i}(self):  # target\n        pass" for i in range(10)]
    (tmp_path / "dense.py").write_text("\n".join(lines), encoding="utf-8")

    result = search_read(query="target", path=str(tmp_path), include_outline=False)
    assert result.matches[0].outline is None


def test_search_read_no_matches_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "empty.py").write_text("x = 1\n", encoding="utf-8")
    result = search_read(query="zzz_no_such_pattern_xyz", path=str(tmp_path))
    assert result.matches == []
    assert result.total_tokens == 0


def test_search_read_to_dict_schema(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("def fn():  # token\n    pass\n", encoding="utf-8")
    result = search_read(query="token", path=str(tmp_path))
    d = search_read_to_dict(result)

    assert "matches" in d
    assert "total_tokens" in d
    assert "tokens_saved_vs_naive" in d
    assert "cache_hit" in d

    if d["matches"]:
        m = d["matches"][0]
        assert "path" in m
        assert "lang" in m
        assert "snippets" in m
        assert "tokens" in m
        if m["snippets"]:
            sn = m["snippets"][0]
            assert "line_start" in sn
            assert "line_end" in sn
            assert "score" in sn
            assert "text" in sn


def test_search_read_to_dict_omits_empty_metadata_fields() -> None:
    result = SearchReadResult(
        matches=[
            FileMatch(
                path="src/example.py",
                lang="python",
                snippets=[
                    Snippet(line_start=1, line_end=2, score=0.5, text="alpha"),
                    Snippet(line_start=3, line_end=4, score=0.8, text="beta", byte_start=10, byte_end=14),
                ],
                outline=None,
                tokens=12,
            )
        ],
        total_tokens=12,
        tokens_saved_vs_naive=0,
        cache_hit=False,
    )

    payload = search_read_to_dict(result)
    match = payload["matches"][0]

    assert "outline" not in match
    assert "byte_start" not in match["snippets"][0]
    assert "byte_end" not in match["snippets"][0]
    assert match["snippets"][1]["byte_start"] == 10
    assert match["snippets"][1]["byte_end"] == 14


def test_search_read_rejects_injection_attempt(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="search_read rejected"):
        search_read(query="foo; rm -rf /", path=str(tmp_path))


def test_search_read_rejects_dotdot_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Workspace is a subdir; a relative ../ path that climbs out must be rejected.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET needle\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(workspace))

    with pytest.raises(ValueError, match="escapes the workspace"):
        search_read(query="needle", path="../secret.txt")


def test_search_read_does_not_follow_symlinked_result_out_of_base(tmp_path: Path) -> None:
    # A symlink inside the searched tree that points outside it must not be read.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "loot.py").write_text("SECRET = 'needle'\n", encoding="utf-8")

    base = tmp_path / "base"
    base.mkdir()
    (base / "real.py").write_text("value = 'needle'\n", encoding="utf-8")
    link = base / "loot.py"
    try:
        link.symlink_to(outside / "loot.py")
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    result = search_read(query="needle", path=str(base))
    for match in result.matches:
        for snippet in match.snippets:
            assert "SECRET" not in snippet.text
