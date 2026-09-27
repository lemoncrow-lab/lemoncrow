from __future__ import annotations

from pathlib import Path

from lemoncrow.pro.capabilities.code_context.engine import CodeContextEngine, _line_fts_source_section


def _write_line_only_repo(root: Path) -> None:
    src = root / "src"
    src.mkdir(parents=True)
    (src / "headers.py").write_text(
        "# The client sends x-flipt-accept-server-version so the server can negotiate the response schema.\n"
        "VALUE = 1\n",
        encoding="utf-8",
    )


def test_line_fts_source_section_renders_bounded_context() -> None:
    lines = [f"line {index}" for index in range(1, 21)]
    section = _line_fts_source_section(
        "pkg/service.py",
        lines,
        {"line": 10, "text": "line 10"},
        context_lines=2,
    )

    assert section is not None
    assert section["start_line"] == 8
    assert section["end_line"] == 12
    assert section["provenance"] == "line_fts"
    assert section["matched"] is True
    assert section["content"].splitlines() == [
        "8\tline 8",
        "9\tline 9",
        "10\tline 10",
        "11\tline 11",
        "12\tline 12",
    ]


def test_tool_explore_hydrates_line_only_source_after_ranking(tmp_path: Path) -> None:
    _write_line_only_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    payload = engine.tool_explore(
        "server negotiate response schema",
        max_files=3,
        include_source=True,
        budget_tokens=2000,
    )

    assert payload["files"]
    entry = payload["files"][0]
    assert (entry.get("path") or entry.get("file_path")) == "src/headers.py"
    assert entry["source_sections"]
    assert entry["source_sections"][0]["provenance"] == "line_fts"
    assert "server can negotiate the response schema" in entry["source_sections"][0]["content"]
    assert "_line_fts_match" not in entry


def test_line_fts_hydration_respects_budget_and_strips_private_metadata(tmp_path: Path) -> None:
    _write_line_only_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    payload = {
        "files": [
            {
                "path": "src/headers.py",
                "symbols": [],
                "source_sections": [],
                "_line_fts_match": {"line": 1, "text": "server negotiate response schema"},
            }
        ]
    }

    finalized = engine._finalize_line_fts_sources(payload, include_source=True, budget_tokens=1)

    assert finalized["files"][0]["source_sections"] == []
    assert "_line_fts_match" not in finalized["files"][0]


def test_line_fts_hydration_disabled_when_source_not_requested(tmp_path: Path) -> None:
    _write_line_only_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    payload = {
        "files": [
            {
                "path": "src/headers.py",
                "symbols": [],
                "source_sections": [],
                "_line_fts_match": {"line": 1, "text": "server negotiate response schema"},
            }
        ]
    }

    finalized = engine._finalize_line_fts_sources(payload, include_source=False, budget_tokens=2000)

    assert finalized["files"][0]["source_sections"] == []
    assert "_line_fts_match" not in finalized["files"][0]
