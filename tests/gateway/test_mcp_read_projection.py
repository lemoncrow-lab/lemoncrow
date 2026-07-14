from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.gateway.adapters.mcp_server import (
    _read_dedup_resource,
    tool_smart_edit,
    tool_smart_read,
)
from lemoncrow.pro.capabilities.source_projection import build_compact_projection


def test_default_reader_read_uses_minified_projection_for_safe_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    # Pin the outline threshold above this file's LOC — this test exercises
    # minified (tree-sitter) projection of full bodies, not outline-by-default.
    monkeypatch.setenv("LEMONCROW_OUTLINE_THRESHOLD", "200")
    target = tmp_path / "sample.go"
    source = (
        "package   main\n\n"
        'import   "fmt"\n\n'
        "func   main()   {\n"
        '    message := "keep   quoted   spacing"\n'
        "    fmt.Println(   message   )\n"
        "}\n"
    )
    target.write_text(source, encoding="utf-8")

    payload = tool_smart_read({"path": str(target), "include_meta": True})

    assert payload["content"] != source
    assert "fmt.Println( message )" in payload["content"]
    assert payload["projection"]["view"] == "minified"
    assert payload["projection"]["transformed"] is True
    assert payload["projection_delta"]["saved_tokens"] > 0
    assert payload["projection_delta"]["lang"] == "go"
    assert payload["projection_mapping"]["projection_kind"] == "minified"
    assert payload["projection_mapping"]["segments"]


def test_expand_true_keeps_untransformed_text_and_skips_projection_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "sample.go"
    source = 'package   main\nfunc   main()   { println("x") }\n'
    target.write_text(source, encoding="utf-8")

    payload = tool_smart_read({"path": str(target), "full": True, "include_meta": True})

    assert payload["content"] == source
    assert payload["projection"]["view"] == "exact"
    assert payload["projection"]["untransformed_text"] is True
    assert payload["projection"]["transformed"] is False
    assert "projection_delta" not in payload


def test_expand_large_file_returns_line_aligned_prefix_with_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A whole-file expand larger than the inline budget must return a line-aligned
    # prefix + an EXACT continuation range, NOT the full body (which the host would
    # otherwise dump to a temp file and force the agent to re-read in blind ranges).
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    # Budget above the 8KB floor; file comfortably larger than the budget.
    monkeypatch.setenv("LEMONCROW_READ_INLINE_BUDGET_BYTES", "10240")
    target = tmp_path / "big.py"
    source = "".join(f"line_{i:04d} = {i}\n" for i in range(1000))
    target.write_text(source, encoding="utf-8")

    payload = tool_smart_read({"path": str(target), "full": True, "include_meta": True})

    assert payload["truncated"] is True
    assert payload["lines_total"] == 1000
    assert 0 < payload["lines_shown"] < 1000
    content = payload["content"]
    # line-aligned prefix: first line kept, a late line dropped
    assert "line_0000 = 0" in content
    assert "line_0999 = 999" not in content
    # exact continuation range points at the next unread line
    assert f'range="L{payload["lines_shown"] + 1}-"' in content
    # the kept body (before the notice) stays within budget
    body = content.split("\n\n[lines")[0]
    assert len(body.encode("utf-8")) <= 10240 + 16


def test_expand_inline_budget_disabled_returns_full_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # LEMONCROW_READ_INLINE_BUDGET_BYTES=0 opts out: full exact body, no truncation.
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_READ_INLINE_BUDGET_BYTES", "0")
    target = tmp_path / "big.py"
    source = "".join(f"line_{i:04d} = {i}\n" for i in range(1000))
    target.write_text(source, encoding="utf-8")

    payload = tool_smart_read({"path": str(target), "full": True, "include_meta": True})

    assert payload.get("truncated") is not True
    assert "line_0999 = 999" in payload["content"]


def test_explicit_range_keeps_untransformed_slice_and_skips_projection_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "sample.go"
    source = 'package   main\nfunc   main()   { println("x") }\nreturn\n'
    target.write_text(source, encoding="utf-8")

    payload = tool_smart_read({"path": str(target), "range": "1-2", "include_meta": True})

    assert payload["content"] == 'package   main\nfunc   main()   { println("x") }'
    assert payload["projection"]["view"] == "range"
    assert payload["projection"]["untransformed_text"] is True
    assert payload["projection"]["body_complete"] is False
    assert "projection_delta" not in payload


def test_default_reader_read_keeps_unknown_language_untransformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "notes.txt"
    source = "keep    unknown    spacing\n"
    target.write_text(source, encoding="utf-8")

    payload = tool_smart_read({"path": str(target), "include_meta": True})

    assert payload["content"] == source
    assert payload["projection"]["view"] == "exact"
    assert payload["projection"]["transformed"] is False
    assert "projection_delta" not in payload


def test_projection_edit_descriptor_round_trips_through_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "sample.go"
    source = 'package   main\n\nfunc   main()   {\n    println("hi")\n}\n'
    target.write_text(source, encoding="utf-8")
    projection = build_compact_projection(source, "go", path=str(target), include_mapping=True)

    assert projection.mapping is not None
    projected_start = projection.content.index("println")
    projected_end = projected_start + len("println")

    payload = tool_smart_edit(
        {
            "edits": [
                {
                    "kind": "projection",
                    "file_path": str(target),
                    "projection_kind": "compact",
                    "projection_mapping": projection.mapping.to_dict(),
                    "projected_start": projected_start,
                    "projected_end": projected_end,
                    "new_string": "fmt.Println",
                }
            ],
            "post_edit_hooks": False,
        }
    )

    assert not payload.get("failed")
    assert "fmt.Println" in target.read_text(encoding="utf-8")


def test_projection_edit_descriptor_supports_multi_span_replacements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "sample.go"
    source = 'package   main\n\nfunc   main()   {\n    println("hi")\n    println("bye")\n}\n'
    target.write_text(source, encoding="utf-8")
    projection = build_compact_projection(source, "go", path=str(target), include_mapping=True)

    assert projection.mapping is not None
    first = projection.content.index("println")
    second = projection.content.index("println", first + 1)

    payload = tool_smart_edit(
        {
            "edits": [
                {
                    "kind": "projection",
                    "file_path": str(target),
                    "projection_kind": "compact",
                    "projection_mapping": projection.mapping.to_dict(),
                    "projected_ranges": [
                        {
                            "projected_start": first,
                            "projected_end": first + len("println"),
                            "new_string": "fmt.Println",
                        },
                        {
                            "projected_start": second,
                            "projected_end": second + len("println"),
                            "new_string": "fmt.Println",
                        },
                    ],
                }
            ],
            "post_edit_hooks": False,
        }
    )

    assert not payload.get("failed")
    assert target.read_text(encoding="utf-8").count("fmt.Println") == 2


def test_projection_edit_descriptor_rejects_overlapping_multi_span_replacements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "sample.go"
    source = 'package   main\n\nfunc   main()   {\n    println("hi")\n}\n'
    target.write_text(source, encoding="utf-8")
    projection = build_compact_projection(source, "go", path=str(target), include_mapping=True)

    assert projection.mapping is not None
    start = projection.content.index("println")

    payload = tool_smart_edit(
        {
            "edits": [
                {
                    "kind": "projection",
                    "file_path": str(target),
                    "projection_kind": "compact",
                    "projection_mapping": projection.mapping.to_dict(),
                    "projected_ranges": [
                        {
                            "projected_start": start,
                            "projected_end": start + 4,
                            "new_string": "fmt.",
                        },
                        {
                            "projected_start": start + 2,
                            "projected_end": start + len("println"),
                            "new_string": "Println",
                        },
                    ],
                }
            ],
            "post_edit_hooks": False,
        }
    )

    assert payload["rolled_back"] is True
    assert payload["failed"][0]["code"] == "overlapping_projected_ranges"
    assert payload["failed"][0]["retry_with"]["tool"] == "read"
    assert "non-overlapping exact spans" in payload["failed"][0]["hint"]


def test_projection_edit_descriptor_requires_span_or_projected_ranges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "sample.go"
    source = 'package   main\n\nfunc   main()   {\n    println("hi")\n}\n'
    target.write_text(source, encoding="utf-8")
    projection = build_compact_projection(source, "go", path=str(target), include_mapping=True)

    assert projection.mapping is not None
    payload = tool_smart_edit(
        {
            "edits": [
                {
                    "kind": "projection",
                    "file_path": str(target),
                    "projection_kind": "compact",
                    "projection_mapping": projection.mapping.to_dict(),
                }
            ],
            "post_edit_hooks": False,
        }
    )

    assert payload["rolled_back"] is True
    assert payload["failed"][0]["code"] == "missing_projection_span"


def test_projection_edit_descriptor_returns_guidance_for_ambiguous_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "sample.go"
    source = "package   main\n"
    target.write_text(source, encoding="utf-8")
    projection = build_compact_projection(source, "go", path=str(target), include_mapping=True)

    assert projection.mapping is not None
    ambiguous = projection.content.index(" ") + 1

    payload = tool_smart_edit(
        {
            "edits": [
                {
                    "kind": "projection",
                    "file_path": str(target),
                    "projection_kind": "compact",
                    "projection_mapping": projection.mapping.to_dict(),
                    "projected_start": ambiguous,
                    "projected_end": ambiguous,
                    "new_string": "log.",
                }
            ],
            "post_edit_hooks": False,
        }
    )

    assert payload["rolled_back"] is True
    assert payload["failed"][0]["code"] == "ambiguous_projected_range"
    retry_with = payload["failed"][0]["retry_with"]
    assert retry_with["tool"] == "read"
    assert retry_with["path"] == str(target)
    assert retry_with["range"] == "L1-L1"
    assert retry_with["include_meta"] is True
    assert retry_with["selection_context"]["line_range"] == "L1-L1"
    assert "whitespace" in retry_with["selection_context"]["segment_kinds"]
    assert retry_with["selection_context"]["before"] == "package"
    assert retry_with["selection_context"]["after"] == "main"
    assert "range=L1-L1" in payload["failed"][0]["hint"]


def test_read_batch_files_one_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("alpha\nbeta\n", encoding="utf-8")
    b.write_text("one\ntwo\nthree\n", encoding="utf-8")

    payload = tool_smart_read(
        {
            "files": [
                {"path": str(a)},
                {"path": str(b), "range": "1-2"},
                {"path": str(tmp_path / "missing.txt")},
                {},
            ]
        }
    )

    results = payload["files"]
    assert len(results) == 4
    assert "alpha" in results[0]["content"]
    assert "two" in results[1]["content"] and "three" not in results[1]["content"]
    assert "error" in results[2] and results[2]["path"].endswith("missing.txt")
    assert "error" in results[3]


def test_read_path_range_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "store.py"
    target.write_text("".join(f"line{i}\n" for i in range(1, 11)), encoding="utf-8")

    # Single read: "path:Lstart-Lend" is parsed as a line range.
    payload = tool_smart_read({"path": f"{target}:L2-L4"})
    assert payload["mode"] == "range"
    assert payload["content"] == "line2\nline3\nline4"

    # Explicit range= wins over the suffix.
    payload = tool_smart_read({"path": f"{target}:L2-L4", "range": "6-7"})
    assert payload["content"] == "line6\nline7"

    # Batch: plain-string specs may carry the suffix (the failing case).
    batch = tool_smart_read({"files": [f"{target}:L1-L2", {"path": f"{target}:L9-L10"}]})
    results = batch["files"]
    assert results[0]["content"] == "line1\nline2"
    assert results[1]["content"] == "line9\nline10"


def test_range_read_returns_only_requested_lines_without_meta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ranged read strips outline/projection scaffolding -- just the lines.

    The caller asked for specific lines and already knows the range, so the
    outline/projection/language metadata is pure redundancy. include_meta=True
    (power callers) still gets the projection, asserted elsewhere.
    """
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "slice.py"
    target.write_text("".join(f"line{i} = {i}\n" for i in range(1, 11)), encoding="utf-8")

    payload = tool_smart_read({"path": str(target), "range": "2-4"})

    assert payload["mode"] == "range"
    assert payload["content"] == "line2 = 2\nline3 = 3\nline4 = 4"
    # The redundant outline/projection scaffolding is gone.
    assert "outline" not in payload
    assert "projection" not in payload
    assert "language" not in payload

    # include_meta still surfaces the projection for power callers.
    meta = tool_smart_read({"path": str(target), "range": "2-4", "include_meta": True})
    assert meta["projection"]["view"] == "range"


def test_read_single_path_still_works_without_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    target = tmp_path / "solo.txt"
    target.write_text("solo content\n", encoding="utf-8")

    payload = tool_smart_read({"path": str(target)})

    assert "files" not in payload
    assert "solo content" in payload["content"]


def test_edit_surfaces_inline_diff_only_for_nonexact_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))

    # Exact match: the caller already knows old->new and gets the `applied`
    # line ranges, so an inline diff is pure redundancy and is omitted.
    exact_target = tmp_path / "exact.py"
    exact_target.write_text("value = 1\n", encoding="utf-8")
    exact = tool_smart_edit(
        {
            "edits": [{"file_path": str(exact_target), "old_string": "value = 1", "new_string": "value = 2"}],
            "post_edit_hooks": False,
        }
    )
    # A clean exact edit echoes the applied range (orientation) but no redundant diff.
    assert "failed" not in exact
    assert "applied" in exact
    assert "diff" not in exact, "exact edits must not surface a redundant inline diff"
    assert exact_target.read_text(encoding="utf-8") == "value = 2\n"

    # Non-exact match (placeholder via `...`): the applied text may diverge from
    # what the caller asked for. The inline diff is no longer surfaced (it would
    # echo old+new content into context); the applied entry's match_mode is the
    # divergence signal, and the agent re-reads on demand.
    fuzzy_target = tmp_path / "fuzzy.py"
    fuzzy_target.write_text("start = 1\nmiddle = 2\nend = 3\n", encoding="utf-8")
    fuzzy = tool_smart_edit(
        {
            "edits": [
                {
                    "file_path": str(fuzzy_target),
                    "old_string": "start = 1\n...\nend = 3",
                    "new_string": "start = 10\nend = 30",
                }
            ],
            "post_edit_hooks": False,
        }
    )
    # Loud via match_mode (the divergence signal), but the empty `failed` noise is
    # stripped. The applied entry retains match_mode so the result is not silenced.
    assert "failed" not in fuzzy
    modes = [e.get("match_mode") for e in fuzzy.get("applied") or [] if isinstance(e, dict)]
    assert any(m and m != "exact" for m in modes), fuzzy
    assert "diff" not in fuzzy, "edits never surface an inline diff; match_mode signals a non-exact apply"
    assert fuzzy_target.read_text(encoding="utf-8") == "start = 10\nend = 30\n"


def test_large_range_read_returns_prefix_with_continuation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An explicit range read larger than the inline budget is bounded just like
    # the expand path: a line-aligned prefix + an EXACT continuation range that
    # resumes at the next unread line of the requested slice (not line 1).
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_READ_INLINE_BUDGET_BYTES", "10240")
    target = tmp_path / "big.py"
    target.write_text("".join(f"line_{i:04d} = {i}\n" for i in range(1000)), encoding="utf-8")

    payload = tool_smart_read({"path": str(target), "range": "100-1000", "include_meta": True})

    assert payload["truncated"] is True
    assert 0 < payload["lines_shown"] < 901
    content = payload["content"]
    # prefix starts at the requested range's first line (line 100 == index 99)
    assert "line_0099 = 99" in content
    # a late line of the range is dropped
    assert "line_0999 = 999" not in content
    # continuation resumes mid-file at 100 + shown, NOT at L1
    assert f'range="L{100 + payload["lines_shown"]}-"' in content
    body = content.split("\n\n[lines")[0]
    assert len(body.encode("utf-8")) <= 10240 + 32


def test_small_range_read_not_truncated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The range cap must never bite a normal (sub-budget) range read.
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_READ_INLINE_BUDGET_BYTES", "10240")
    target = tmp_path / "small.py"
    target.write_text("".join(f"line_{i:04d} = {i}\n" for i in range(1000)), encoding="utf-8")

    payload = tool_smart_read({"path": str(target), "range": "100-120", "include_meta": True})

    assert payload.get("truncated") is not True
    assert "line_0119 = 119" in payload["content"]


def test_read_dedup_resource_tracks_single_file_batch_form() -> None:
    # The public `read` tool emits files=[...]; a single-entry batch must yield a
    # stable, non-empty resource key (so delta_for can fire), while a multi-file
    # batch stays untracked. Regression: the key previously bailed on any files=.
    string_key = _read_dedup_resource({"files": ["foo.py:L10-L20"]})
    assert string_key and "foo.py" in string_key
    # dict entry of the same view yields the same key as the string form
    assert _read_dedup_resource({"files": [{"path": "foo.py", "range": "L10-L20"}]}) == string_key
    # idempotent: re-reading the identical view repeats the key (delta-eligible)
    assert _read_dedup_resource({"files": ["foo.py:L10-L20"]}) == string_key
    # different view of the same file -> different key (never cross-diffs)
    assert _read_dedup_resource({"files": ["foo.py:L30-L40"]}) != string_key
    assert _read_dedup_resource({"files": ["foo.py:full"]}) != string_key
    # multi-file batch is not delta-trackable
    assert _read_dedup_resource({"files": ["a.py", "b.py"]}) == ""
    # legacy path= form still works
    assert _read_dedup_resource({"path": "leg.py", "range": "1-3"})
    assert _read_dedup_resource({"files": []}) == ""
