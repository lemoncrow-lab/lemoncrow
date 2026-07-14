"""Runtime-hygiene gateway features: tool-output spill (T7), reversible
auto-compaction (T8), and the autonomous-compaction lever (T6).

T7 spilling is default-on and explicitly disableable; T8 auto-compaction remains
default-off. These exercise the spill store, the two dispatch helpers, and the
`compact` tool ops directly — deterministic, no network, no LLM.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.pro.capabilities.tool_supervision import tool_output_spill

_PATH_RE = re.compile(r"read (\S+\.txt)\]")


def _extract_path(text: str) -> Path:
    """Pull the spill file path out of a host-facing summary string."""
    match = _PATH_RE.search(text)
    assert match is not None, f"no spill path in: {text[-200:]!r}"
    return Path(match.group(1))


@pytest.fixture(autouse=True)
def _isolated_spill_dir(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(tmp_path / "spill"))
    # T7 defaults ON; tests that exercise the off path disable it explicitly.
    monkeypatch.delenv("LEMONCROW_TOOL_OUTPUT_SPILL", raising=False)
    monkeypatch.delenv("LEMONCROW_MCP_SPILL_RESULT_CHARS", raising=False)
    monkeypatch.delenv("LEMONCROW_MCP_SPILL_MAX_FILES", raising=False)
    monkeypatch.delenv("LEMONCROW_MCP_SPILL_TTL_SECONDS", raising=False)
    monkeypatch.delenv("LEMONCROW_AUTO_COMPACT_OUTPUT", raising=False)


# --------------------------------------------------------------------------- #
# T7 — tool_output_spill store: write + direct read round-trip                 #
# --------------------------------------------------------------------------- #


def test_spill_write_is_lossless() -> None:
    payload = "HEAD" + ("x" * 50000) + "TAIL"
    record = tool_output_spill.spill(payload, tool_name="bash", kind="tool_output")
    assert record is not None
    assert record.path.exists()
    assert record.path.suffix == ".txt"
    assert record.path.read_text(encoding="utf-8") == payload
    assert record.original_bytes == len(payload.encode("utf-8"))


# --------------------------------------------------------------------------- #
# T7 — dispatch helper: _spill_oversized_result_text                            #
# --------------------------------------------------------------------------- #


def test_spill_helper_noop_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_TOOL_OUTPUT_SPILL", "0")
    text = "z" * 200_000
    out = mcp_server._spill_oversized_result_text(text, "bash", {}, limit=1000)
    assert out == text  # flag off -> unchanged, legacy truncation still runs


def test_spill_helper_noop_for_unlisted_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_TOOL_OUTPUT_SPILL", "1")
    text = "z" * 200_000
    out = mcp_server._spill_oversized_result_text(text, "grep", {}, limit=1000)
    assert out == text  # grep is not a spill-worthy tool


def test_spill_helper_passes_through_small_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_TOOL_OUTPUT_SPILL", "1")
    text = "small enough"
    out = mcp_server._spill_oversized_result_text(text, "bash", {}, limit=1_000_000)
    assert out == text  # within budget -> nothing spilled


def test_spill_helper_spills_and_returns_recoverable_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_TOOL_OUTPUT_SPILL", "1")
    text = "HEAD-MARKER" + ("q" * 200_000) + "TAIL-MARKER"
    out = mcp_server._spill_oversized_result_text(text, "bash", {}, limit=64 * 1024)

    assert len(out) < len(text)  # host-facing text is a compact summary
    assert out.startswith("HEAD-MARKER")  # head preserved in summary
    assert "TAIL-MARKER" in out  # tail preserved in summary
    assert "[lc: shrunk" in out  # canonical footer present
    assert "read " in out  # recovery hint present
    assert _extract_path(out).read_text(encoding="utf-8") == text  # full original preserved


def test_spill_helper_enforces_strict_char_cap_including_ref() -> None:
    text = "HEAD-MARKER" + ("q" * 200_000) + "TAIL-MARKER"
    out = mcp_server._spill_oversized_result_text(text, "bash", {}, limit=2048, unit="chars")

    assert len(out) <= 2048
    assert out.startswith("HEAD-MARKER")
    assert "TAIL-MARKER" in out
    assert _extract_path(out).read_text(encoding="utf-8") == text


def test_read_on_spill_file_does_not_re_spill() -> None:
    """Reading a spill file must not create a second spill: the dispatch layer
    returns text unchanged so normal truncation applies instead."""
    record = tool_output_spill.spill("x" * 200_000, tool_name="bash")
    assert record is not None

    text = "x" * 200_000
    out = mcp_server._spill_oversized_result_text(text, "read", {"path": str(record.path)}, limit=64 * 1024)
    assert out == text  # returned unchanged — no new spill
    assert "spilled to" not in out


def test_spill_result_chars_defaults_to_2k(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_MCP_SPILL_RESULT_CHARS", raising=False)
    assert mcp_server._spill_result_chars() == 2048


def test_spill_result_chars_per_tool_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_MCP_SPILL_RESULT_CHARS", raising=False)
    # bash gets a larger inline budget; web_fetch/sql fall back to the 2 KiB default.
    assert mcp_server._spill_result_chars("bash") == 8 * 1024
    assert mcp_server._spill_result_chars("web_fetch") == 2048
    assert mcp_server._spill_result_chars("sql") == 2048


def test_spill_result_chars_env_overrides_all_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_RESULT_CHARS", "1234")
    # An explicit env value wins for every tool, including the bash override.
    assert mcp_server._spill_result_chars("bash") == 1234
    assert mcp_server._spill_result_chars("web_fetch") == 1234
    assert mcp_server._spill_result_chars() == 1234


def test_spill_result_chars_env_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_RESULT_CHARS", "0")
    assert mcp_server._spill_result_chars("bash") == 0


def test_enforce_retention_caps_file_count(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_MAX_FILES", "3")
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_TTL_SECONDS", "0")  # isolate count-axis
    for i in range(6):
        p = tmp_path / f"tool_output-bash-{i}-{i:08x}.json"
        p.write_text("{}", encoding="utf-8")
        os.utime(p, (1000 + i, 1000 + i))  # strictly ascending mtime

    tool_output_spill._enforce_retention(tmp_path)

    assert len(list(tmp_path.glob("*.json"))) == 3
    # The three newest (i=3,4,5) survive; the three oldest are evicted.
    for i in (3, 4, 5):
        assert (tmp_path / f"tool_output-bash-{i}-{i:08x}.json").exists()
    for i in (0, 1, 2):
        assert not (tmp_path / f"tool_output-bash-{i}-{i:08x}.json").exists()


def test_enforce_retention_evicts_by_age(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_MCP_SPILL_MAX_FILES", raising=False)
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_TTL_SECONDS", "100")
    now = time.time()
    old = tmp_path / "tool_output-bash-old.json"
    fresh = tmp_path / "tool_output-bash-fresh.json"
    old.write_text("{}", encoding="utf-8")
    os.utime(old, (now - 500, now - 500))
    fresh.write_text("{}", encoding="utf-8")
    os.utime(fresh, (now - 1, now - 1))

    tool_output_spill._enforce_retention(tmp_path)

    assert not old.exists()
    assert fresh.exists()


def test_enforce_retention_disabled_keeps_all(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_MAX_FILES", "0")
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_TTL_SECONDS", "0")
    for i in range(5):
        (tmp_path / f"tool_output-bash-{i}.json").write_text("{}", encoding="utf-8")

    tool_output_spill._enforce_retention(tmp_path)

    assert len(list(tmp_path.glob("*.json"))) == 5


def test_spill_bounded_and_leaves_no_temp(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cap to 2 then create 5 spills via the real spill() path (autouse spill dir).
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_MAX_FILES", "2")
    monkeypatch.delenv("LEMONCROW_MCP_SPILL_TTL_SECONDS", raising=False)
    spill_dir = tool_output_spill._spill_dir()
    for i in range(5):
        assert tool_output_spill.spill(f"content-{i}", tool_name="bash") is not None

    # Directory stays bounded and the atomic write leaves no in-flight temp files.
    assert len(list(spill_dir.glob("*.txt"))) <= 2
    assert list(spill_dir.glob("*.tmp")) == []


def test_summary_with_ref_preserves_ref_under_tiny_cap() -> None:
    # A cap below the full footer but above the bare path must keep the path
    # intact so the on-disk spill stays recoverable.
    record = tool_output_spill.spill("x" * 5000, tool_name="bash")
    assert record is not None
    path_str = str(record.path)
    tiny = len(path_str) + 5

    out = tool_output_spill.summary_with_ref("SUMMARY-TEXT", record, original_chars=5000, max_chars=tiny)

    assert len(out) <= tiny
    assert path_str in out
    assert record.path.read_text(encoding="utf-8") == "x" * 5000


def test_spill_notice_shrunk_with_path() -> None:
    text = tool_output_spill.spill_notice(
        verb="shrunk", original_chars=100907, kept_chars=5035, path=Path("/tmp/lemoncrow-spill/tool_output-x.txt")
    )
    assert text == ("[lc: shrunk 100907→5035; full: /tmp/lemoncrow-spill/tool_output-x.txt]")


def test_spill_notice_truncated_with_path() -> None:
    text = tool_output_spill.spill_notice(
        verb="truncated", original_chars=9000, kept_chars=1024, path=Path("/tmp/x.txt")
    )
    assert text == "[lc: truncated 9000→1024; full: /tmp/x.txt]"


def test_spill_notice_compacted_with_method_verb() -> None:
    text = tool_output_spill.spill_notice(
        verb="compacted:dedup", original_chars=500, kept_chars=100, path=Path("/tmp/x.txt")
    )
    assert text == "[lc: compacted:dedup 500→100; full: /tmp/x.txt]"


def test_spill_notice_no_path_is_spill_failed_shape() -> None:
    # No recovery path -> always reported as a hard truncation, regardless of
    # the requested verb -- from the model's perspective there's nothing to
    # recover either way.
    text = tool_output_spill.spill_notice(verb="shrunk", original_chars=9000, kept_chars=1024, path=None)
    assert text == "[lc: truncated 9000→1024; narrow the query for full]"


def test_summary_with_ref_inserts_clipped_marker_when_summary_must_shrink() -> None:
    record = tool_output_spill.spill("x" * 5000, tool_name="bash")
    assert record is not None
    summary = "S" * 500
    footer_len = (
        len(
            tool_output_spill.spill_notice(
                verb="shrunk", original_chars=5000, kept_chars=len(summary), path=record.path
            )
        )
        + 2
    )  # + the "\n\n" separator summary_with_ref prepends
    cap = footer_len + 80  # room for the footer + a clipped (not full) summary

    out = tool_output_spill.summary_with_ref(summary, record, original_chars=5000, max_chars=cap)

    assert len(out) <= cap
    assert "[… summary clipped; full in spill …]" in out
    assert "[lc: shrunk" in out


def test_spill_is_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_TOOL_OUTPUT_SPILL", raising=False)
    assert mcp_server._tool_output_spill_enabled() is True


def test_read_exempt_from_strict_char_cap() -> None:
    """`read` is the incremental retrieval surface (ranges, full=true) and the
    tool used to recover spilled output, so the 2 KiB char cap must NOT
    force-summarize it. It stays in _SPILL_TOOLS (multi-MB wire backstop) but is
    absent from the char-cap set the dispatcher passes.
    """
    assert "read" not in mcp_server._SPILL_CHAR_CAP_TOOLS
    assert "read" in mcp_server._SPILL_TOOLS

    text = "HEAD-MARKER" + ("q" * 200_000) + "TAIL-MARKER"
    out = mcp_server._spill_oversized_result_text(
        text, "read", {}, limit=2048, unit="chars", tools=mcp_server._SPILL_CHAR_CAP_TOOLS
    )
    assert out == text  # returned in full, not spilled


def test_shell_still_char_capped() -> None:
    """Primary target: shell/bash output above the cap is still spilled (full
    original recoverable)."""
    text = "x" * 200_000
    out = mcp_server._spill_oversized_result_text(
        text, "bash", {}, limit=2048, unit="chars", tools=mcp_server._SPILL_CHAR_CAP_TOOLS
    )
    assert len(out) <= 2048
    assert _extract_path(out).read_text(encoding="utf-8") == text


# --------------------------------------------------------------------------- #
# T8 — reversible auto-compaction: _auto_compact_result_text                    #
# --------------------------------------------------------------------------- #


def test_auto_compact_noop_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_COMPACT_RESULT_CHARS", "1000")
    text = "a" * 50_000
    out = mcp_server._auto_compact_result_text(text, "read", {"path": "x.txt"})
    assert out == text  # flag off -> byte-identical to prior behavior


def test_auto_compact_passes_through_small_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_AUTO_COMPACT_OUTPUT", "1")
    monkeypatch.setenv("LEMONCROW_MCP_COMPACT_RESULT_CHARS", "100000")
    text = "a" * 1000
    out = mcp_server._auto_compact_result_text(text, "read", {"path": "x.txt"})
    assert out == text  # under threshold -> untouched


def test_auto_compact_is_reversible_via_spill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_AUTO_COMPACT_OUTPUT", "1")
    monkeypatch.setenv("LEMONCROW_MCP_COMPACT_RESULT_CHARS", "2000")
    # Non-code tool -> deterministic compact_output path.
    text = "START" + ("data line\n" * 5000) + "END"
    out = mcp_server._auto_compact_result_text(text, "bash", {})

    assert len(out) < len(text)  # compacted
    assert "compacted" in out
    # Recovery hint uses `read <path>`
    path_match = re.search(r"full: (\S+\.txt)", out)
    assert path_match is not None
    spill_path = Path(path_match.group(1))
    assert spill_path.read_text(encoding="utf-8") == text  # original fully recoverable


def test_auto_compact_code_is_ast_aware(monkeypatch: pytest.MonkeyPatch) -> None:
    # The AST source-projection path is Pro; treat the install as licensed.
    monkeypatch.setattr("lemoncrow.core.capabilities.licensing.has_feature", lambda *a, **k: True)
    monkeypatch.setenv("LEMONCROW_AUTO_COMPACT_OUTPUT", "1")
    monkeypatch.setenv("LEMONCROW_MCP_COMPACT_RESULT_CHARS", "2000")
    # Python source with lots of blank lines -> source_projection compact applies.
    src = "def f():\n" + "\n\n\n".join(f"    x{i} = {i}  " for i in range(2000)) + "\n"
    out = mcp_server._auto_compact_result_text(src, "read", {"path": "mod.py"})
    assert "projection:python" in out  # AST/structure-aware method tag
    # Still reversible: a .txt spill path appears in the hint
    path_match = re.search(r"full: (\S+\.txt)", out)
    assert path_match is not None
    assert Path(path_match.group(1)).exists()


# --------------------------------------------------------------------------- #
# T6 — autonomous-compaction lever on the `compact` tool                        #
# --------------------------------------------------------------------------- #


def test_compact_tool_default_op_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake(session_id=None):  # type: ignore[no-untyped-def]
        captured["session_id"] = session_id
        return {"prompt_block": "BLOCK", "tokens_freed": 42}

    monkeypatch.setattr(mcp_server, "_compress_context", _fake)
    out = mcp_server.tool_compact({})  # default op="compact"
    assert out == {"prompt_block": "BLOCK", "tokens_freed": 42}
    assert "op" not in out  # default op adds no marker


def test_compact_tool_consolidate_reuses_compaction_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def _fake(session_id=None):  # type: ignore[no-untyped-def]
        calls.append(session_id)
        return {"prompt_block": "BLOCK", "tokens_freed": 7}

    monkeypatch.setattr(mcp_server, "_compress_context", _fake)
    out = mcp_server.tool_compact({"op": "consolidate", "session_id": "sess-1"})
    assert calls == ["sess-1"]  # reused the existing entrypoint exactly once
    assert out["op"] == "consolidate"
    assert out["tokens_freed"] == 7


# --------------------------------------------------------------------------- #
# M1 — spill fires at the CHAR threshold, BEFORE legacy char compaction, so the #
# spilled file holds the FULL untransformed payload (not compacted text).       #
# --------------------------------------------------------------------------- #


def test_spill_helper_char_unit_fires_at_char_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_TOOL_OUTPUT_SPILL", "1")
    # Under default byte caps (6MB) this 200K-char payload would NOT spill, but
    # the char-gated call (threshold 1000 chars) must.
    text = "HEAD" + ("m" * 200_000) + "TAIL"
    out = mcp_server._spill_oversized_result_text(text, "bash", {}, 1000, unit="chars")
    assert len(out) < len(text)
    assert _extract_path(out).read_text(encoding="utf-8") == text  # FULL untransformed payload


def test_handle_spills_full_untransformed_payload_before_compaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through _handle: with the flag on and an oversized result, the
    spill file must hold the FULL untransformed payload — specifically the
    MIDDLE that the legacy _compact_result_text would otherwise drop."""
    monkeypatch.setenv("LEMONCROW_TOOL_OUTPUT_SPILL", "1")
    # Char threshold well below the payload so the char-gated spill fires.
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_RESULT_CHARS", "2048")

    # `sql` stays in the generic char-capped set (web_fetch is now exempt: it
    # spills+truncates itself, see web_fetch._truncate_with_spill). A bare string
    # result bypasses render_tool_result_text's per-tool dict branches entirely
    # (it early-returns None for non-dict results), so it reaches response_text
    # byte-identical -- isolating the dispatch ordering cleanly.
    middle_marker = "UNIQUE-MIDDLE-MARKER-THAT-COMPACTION-WOULD-DROP"
    payload = "HEAD" + ("a" * 100_000) + middle_marker + ("b" * 100_000) + "TAIL"

    def _fake_sql(_args: dict) -> str:  # type: ignore[type-arg]
        return payload

    monkeypatch.setitem(mcp_server.TOOLS["sql"], "handler", _fake_sql)

    resp = mcp_server._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "sql", "arguments": {"query": "select 1"}},
        }
    )
    assert resp is not None
    host_text = resp["result"]["content"][0]["text"]
    # Host-facing text is a compact summary that fits the budget...
    assert len(host_text) <= 2048
    assert middle_marker not in host_text  # the middle is dropped from the summary
    # ...but the spill file recovers the FULL untransformed payload, middle and all.
    recovered = _extract_path(host_text).read_text(encoding="utf-8")
    assert recovered == payload
    assert middle_marker in recovered


def test_handle_spill_flag_off_does_not_spill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag-off behavior preserved: no spill path, legacy char compaction applies."""
    monkeypatch.setenv("LEMONCROW_TOOL_OUTPUT_SPILL", "0")
    monkeypatch.setenv("LEMONCROW_MCP_COMPACT_RESULT_CHARS", "2000")
    payload = "HEAD" + ("a" * 200_000) + "TAIL"

    def _fake_web_fetch(_args: dict) -> dict:  # type: ignore[type-arg]
        return {"content": payload}

    monkeypatch.setitem(mcp_server.TOOLS["web_fetch"], "handler", _fake_web_fetch)
    resp = mcp_server._handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "web_fetch", "arguments": {"url": "https://example.test"}},
        }
    )
    assert resp is not None
    host_text = resp["result"]["content"][0]["text"]
    assert "spilled to" not in host_text  # flag off -> no spill
    # Legacy char compaction still ran; without a recovery path the canonical
    # footer reports a hard truncation (format 3) regardless of the verb.
    assert "[lc: truncated" in host_text
    assert "narrow the query for full" in host_text


def test_handle_passes_per_tool_char_cap_to_spill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real _handle dispatch must pass the PER-TOOL char cap to the char-gated
    spill: bash -> 8 KiB, web_fetch -> 2 KiB (i.e. _spill_result_chars(name), not
    a single global cap). Guards against the call site dropping ``name``."""
    monkeypatch.setenv("LEMONCROW_TOOL_OUTPUT_SPILL", "1")
    monkeypatch.delenv("LEMONCROW_MCP_SPILL_RESULT_CHARS", raising=False)  # per-tool defaults

    seen: dict[str, int] = {}
    real = mcp_server._spill_oversized_result_text

    def _spy(
        text: str,
        tool_name: str,
        args: dict,  # type: ignore[type-arg]
        limit: int,
        *,
        unit: str = "bytes",
        tools: object = None,
    ) -> str:
        if unit == "chars":
            seen[tool_name] = limit
        if tools is None:
            return real(text, tool_name, args, limit, unit=unit)
        return real(text, tool_name, args, limit, unit=unit, tools=tools)  # type: ignore[arg-type]

    monkeypatch.setattr(mcp_server, "_spill_oversized_result_text", _spy)

    payload = "x" * 5000
    monkeypatch.setitem(mcp_server.TOOLS["web_fetch"], "handler", lambda _a: {"content": payload})
    monkeypatch.setitem(mcp_server.TOOLS["bash"], "handler", lambda _a: payload)

    for rid, tool, arguments in (
        (1, "web_fetch", {"url": "https://example.test"}),
        (2, "bash", {"command": "echo hi"}),
    ):
        mcp_server._handle(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        )

    assert seen["web_fetch"] == 2048
    assert seen["bash"] == 8 * 1024
