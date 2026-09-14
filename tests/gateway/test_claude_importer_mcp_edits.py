"""The Claude importer must see edits made through the LemonCrow MCP server.

``_FILE_TOOLS`` named only Claude Code's own built-ins, so a session whose
default edit path is ``mcp__lc__edit`` imported with an empty
``files_touched`` -- 1731 claude traces on the machine this was measured on.
That is what jammed ``lc review``'s authorship gate: the correlator refuses to
name an author that recorded no edit to a reviewed file, and there was none to
find, so every review printed "Generated with: unknown".

Unlike host/model/HEAD, this one *is* recoverable -- the transcripts are
retained -- so a re-import backfills it. It is worthless until the importer
stops clobbering hook-written run.json events
(``test_session_import_preserves_events.py``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.models import FileEditRecord, Trace
from lemoncrow.gateway.hosts.session_parsers.claude import (
    ClaudeImporter,
    _mcp_edit_records,
    _mcp_read_paths,
    _undecorate_lc_path,
)
from lemoncrow.infra.storage.bundle import StoreBundle
from lemoncrow.pro.capabilities.review.provenance import split_files_touched

# ----- path selectors ------------------------------------------------------ #


def test_undecorate_strips_every_selector_a_path_can_carry() -> None:
    assert _undecorate_lc_path("src/app.py") == "src/app.py"
    assert _undecorate_lc_path("src/app.py:L10-L14") == "src/app.py"
    assert _undecorate_lc_path("src/app.py:L10") == "src/app.py"
    assert _undecorate_lc_path("src/app.py:full") == "src/app.py"
    assert _undecorate_lc_path("src/app.py:outline") == "src/app.py"
    assert _undecorate_lc_path("src/app.py:head=50") == "src/app.py"
    # They stack.
    assert _undecorate_lc_path("src/app.py:minified:L10-L14") == "src/app.py"


def test_undecorate_leaves_a_colon_that_is_part_of_the_name() -> None:
    assert _undecorate_lc_path("src/weird:name.py") == "src/weird:name.py"


# ----- edit projection ----------------------------------------------------- #


def test_a_batched_edit_yields_one_record_per_path() -> None:
    records = _mcp_edit_records(
        {
            "edits": [
                {"path": "src/a.py:L1-L4", "old": "x = 1", "new": "x = 2"},
                {"path": "src/b.py", "new": "whole file", "replace": True},
            ]
        }
    )
    assert [record.path for record in records] == ["src/a.py", "src/b.py"]
    assert all(record.event == "edit" for record in records)
    assert "x = 1" in records[0].diff and "x = 2" in records[0].diff
    assert "whole file" in records[1].diff


def test_an_edit_with_no_content_still_records_the_path() -> None:
    # A structured edit this importer does not model is still a recorded edit;
    # demoting it to a read would understate authorship.
    records = _mcp_edit_records({"edits": [{"path": "src/a.py", "symbol": "foo"}]})
    assert [record.path for record in records] == ["src/a.py"]
    assert records[0].diff


def test_a_malformed_edit_call_yields_nothing_rather_than_raising() -> None:
    assert _mcp_edit_records({}) == []
    assert _mcp_edit_records({"edits": "not a list"}) == []
    assert _mcp_edit_records({"edits": [None, 7, {"path": ""}]}) == []


def test_the_diff_is_capped() -> None:
    records = _mcp_edit_records({"edits": [{"path": "a.py", "new": "x" * 20_000}]})
    assert len(records[0].diff) <= 4096


# ----- read projection ----------------------------------------------------- #


def test_reads_are_recorded_as_reads_not_edits() -> None:
    paths = _mcp_read_paths({"files": ["src/a.py:full", {"path": "src/b.py", "range": "L1-L2"}, 7]})
    assert paths == ["src/a.py", "src/b.py"]


def test_a_malformed_read_call_yields_nothing() -> None:
    assert _mcp_read_paths({}) == []
    assert _mcp_read_paths({"files": "src/a.py"}) == []


# ----- through the importer ------------------------------------------------ #


_EVENTS: list[dict[str, Any]] = [
    {
        "type": "assistant",
        "message": {
            "id": "msg1",
            "model": "claude-opus-5",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "content": [
                {
                    "type": "tool_use",
                    "name": "mcp__lc__edit",
                    "id": "tu1",
                    "input": {
                        "edits": [
                            {"path": "/repo/src/service.py:L10-L14", "old": "old", "new": "new"},
                            {"path": "/repo/src/router.py", "new": "body", "replace": True},
                        ]
                    },
                },
                {
                    "type": "tool_use",
                    "name": "mcp__lc__read",
                    "id": "tu2",
                    "input": {"files": ["/repo/docs/notes.md:full"]},
                },
            ],
        },
    }
]


def _import(store: StoreBundle, tmp_path: Path) -> Trace:
    jsonl = tmp_path / "mcp-edit-session.jsonl"
    jsonl.write_text("\n".join(json.dumps(event) for event in _EVENTS), encoding="utf-8")
    assert ClaudeImporter(store).import_session("slug", jsonl, force=True) is not None
    traces = store.history.list_traces(host="claude", limit=1)
    assert traces
    return traces[0]  # type: ignore[no-any-return]


def test_an_mcp_edited_session_no_longer_imports_with_nothing_touched(store: StoreBundle, tmp_path: Path) -> None:
    trace = _import(store, tmp_path)

    edits = [entry for entry in trace.files_touched if isinstance(entry, FileEditRecord)]
    assert {record.path for record in edits} == {"/repo/src/service.py", "/repo/src/router.py"}


def test_the_correlator_sees_those_edits_on_the_edit_side(store: StoreBundle, tmp_path: Path) -> None:
    # This is the whole point: `_score`'s authorship gate reads the edit half of
    # `split_files_touched`, and an MCP-edited session used to land there empty.
    reads, edits = split_files_touched(_import(store, tmp_path))

    assert edits == ("/repo/src/router.py", "/repo/src/service.py")
    assert reads == ("/repo/docs/notes.md",)
