"""Lean grep output: long docstrings collapse to summary + code, not 80-line dumps."""

from __future__ import annotations

from lemoncrow.pro.capabilities.tool_supervision.native_search import (
    _collapse_docstrings,
    search_workspace,
)


def test_collapse_keeps_signature_summary_and_code() -> None:
    win = (
        ["    def quantile(self, q):", '        """Compute the qth quantile.', "", "        Returns quantiles."]
        + [f"        p{i} : description padding the docstring" for i in range(40)]
        + ['        """', "        result = self._compute(q)", "        return result"]
    )
    out = _collapse_docstrings(win)
    assert len(out) < len(win) // 2
    assert "    def quantile(self, q):" in out  # signature kept
    assert "        return result" in out  # code kept
    assert any("elided" in line for line in out)  # marker present
    assert not any("p39 : description" in line for line in out)  # docstring bulk dropped


def test_short_docstrings_untouched() -> None:
    win = ["def f():", '    """Short."""', "    return 1"]
    assert _collapse_docstrings(win) == win


def test_grep_end_to_end_collapses_docstring(tmp_path) -> None:
    src = (
        'def quantile(q):\n    """Compute quantile.\n\n    summary line\n'
        + "".join(f"    p{i} : desc\n" for i in range(40))
        + '    """\n    return q\n'
    )
    f = tmp_path / "m.py"
    f.write_text(src)
    res = search_workspace(
        path=str(f),
        content_regex="def quantile",
        output_mode="file_paths_with_content",
        lines_after=60,
        repo_root=str(tmp_path),
    )
    text = " ".join(b.get("text", "") for b in res.get("content", []) if isinstance(b, dict))
    assert "elided" in text  # collapsed
    assert "return q" in text  # code preserved
    assert "p39 : desc" not in text  # docstring bulk gone
