"""Native Bash compression beyond optional upstream RTK formatting."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.tool_supervision import bash_exec as bx
from lemoncrow.pro.capabilities.tool_supervision import bash_output_compression as bc


@pytest.fixture(autouse=True)
def _enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LEMONCROW_BASH_NATIVE_COMPRESSION", raising=False)
    monkeypatch.setenv("LEMONCROW_TOOL_OUTPUT_SPILL", "1")
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(tmp_path / "spill"))


def _compact(stdout: str, *, command: str = "bash build.sh", budget: int = 1200) -> bx.RunResult:
    return bx._compact_result(
        command=command,
        raw_stdout=stdout,
        raw_stderr="",
        exit_code=0,
        duration_ms=1,
        max_lines=200,
        max_chars=budget,
    )


def test_carriage_return_progress_keeps_final_visible_frame() -> None:
    raw = "download 1%\rdownload 20%\rdownload 100%\ncomplete\n"
    result = _compact(raw, budget=5000)
    assert "download 100%" in result.stdout
    assert "download 1%" not in result.stdout
    assert "download 20%" not in result.stdout
    assert result.chars_omitted > 0


def test_pretty_json_is_minified_losslessly() -> None:
    value = {"items": [{"id": i, "name": f"item-{i}"} for i in range(20)]}
    raw = json.dumps(value, indent=4)
    result = _compact(raw, command="curl https://example.test", budget=20_000)
    assert json.loads(result.stdout) == value
    assert len(result.stdout) < len(raw)
    assert result.truncated is False


def test_large_json_array_is_parseable_and_keeps_diagnostic_row() -> None:
    rows = [{"id": i, "status": "error" if i == 55 else "ok", "payload": "x" * 80} for i in range(100)]
    result = bc.compact_bash_stream(json.dumps(rows), budget=1000)
    payload = json.loads(result.text)
    assert payload["_lemoncrow"]["kind"] == "json-array-sample"
    assert payload["_lemoncrow"]["omitted"] > 0
    assert any(item["id"] == 55 for item in payload["items"])
    assert result.lossy is True


def test_large_json_object_is_sampled_as_valid_json() -> None:
    value = {f"key-{i}": {"value": "x" * 100} for i in range(80)}
    value["key-44"] = {"error": "database refused connection"}
    result = bc.compact_bash_stream(json.dumps(value), budget=800)
    payload = json.loads(result.text)
    assert payload["_lemoncrow"]["kind"] == "json-object-sample"
    assert "key-44" in payload["sample"]
    assert result.lossy is True


def test_large_ndjson_keeps_error_records() -> None:
    rows = [{"id": i, "level": "error" if i == 42 else "info", "message": f"record-{i}"} for i in range(80)]
    raw = "\n".join(json.dumps(row) for row in rows)
    result = bc.compact_bash_stream(raw, budget=800)
    assert "NDJSON records omitted" in result.text
    assert '"id": 42' in result.text
    assert result.lines_omitted > 0


def test_volatile_adjacent_logs_are_folded_with_boundaries() -> None:
    raw = "\n".join(f"2026-07-25T10:00:{i:02d}Z INFO processed batch {i} in {i + 1}ms" for i in range(80))
    result = _compact(raw)
    assert "similar log lines omitted" in result.stdout
    assert "batch 0" in result.stdout
    assert "batch 79" in result.stdout
    assert result.truncated is True
    assert "full:" in result.spill_hint


def test_nonadjacent_heartbeats_are_aggregated() -> None:
    lines: list[str] = []
    for i in range(20):
        lines.append(f"12:00:{i:02d} INFO heartbeat worker=api sequence={i}")
        lines.append(f"plain event name-{chr(65 + i)}")
    result = bc.compact_bash_stream("\n".join(lines), budget=800)
    assert "message recurred" in result.text
    assert "sequence=0" in result.text
    assert "sequence=19" in result.text


def test_progress_phase_keeps_first_and_last_samples() -> None:
    raw = "\n".join(f"Compiling dependency package-{i} v1.{i}.0" for i in range(60))
    result = bc.compact_bash_stream(raw, budget=700)
    assert "compiling progress lines omitted" in result.text
    assert "package-0" in result.text
    assert "package-59" in result.text


def test_repeated_multiline_blocks_are_folded() -> None:
    block = ["retry worker alpha", "sleeping before next attempt", "connection pending"]
    raw = "\n".join(block * 12)
    result = bc.compact_bash_stream(raw, budget=500)
    assert "repeated 3-line blocks" in result.text
    assert result.lines_omitted > 0


def test_homogeneous_table_is_sampled() -> None:
    header = "NAME        STATUS      AGE"
    rows = [f"pod-{i:<6}    Running     {i}m" for i in range(100)]
    result = bc.compact_bash_stream("\n".join([header, *rows]), budget=700)
    assert "table rows omitted" in result.text
    assert "pod-0" in result.text
    assert "pod-99" in result.text
    assert result.lossy is True


def test_pathological_long_line_keeps_both_ends_and_digest() -> None:
    raw = "HEAD" + "x" * 20_000 + "TAIL"
    result = bc.compact_bash_stream(raw, budget=1200)
    assert result.text.startswith("HEAD")
    assert result.text.endswith("TAIL")
    assert "sha256=" in result.text
    assert result.lossy is True


def test_error_lines_are_never_folded_as_near_duplicates() -> None:
    raw = "\n".join(f"2026-07-25T10:00:{i:02d}Z ERROR failed shard {i}" for i in range(20))
    result = bc.compact_bash_stream(raw, budget=500)
    assert "similar log lines omitted" not in result.text
    assert "failed shard 10" in result.text


def test_native_compression_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_BASH_NATIVE_COMPRESSION", "0")
    raw = "\n".join(f"12:00:{i:02d} INFO processed batch {i}" for i in range(80))
    result = bc.compact_bash_stream(raw, budget=500)
    assert result.text == raw
    assert result.chars_saved == 0


def test_compound_test_command_keeps_later_command_output() -> None:
    raw = "20 passed in 0.4s\n--- PATCH SUMMARY ---\n M src/example.py"
    result = _compact(raw, command="pytest -q && git status --short", budget=20_000)
    assert "20 passed" in result.stdout
    assert "PATCH SUMMARY" in result.stdout
    assert "src/example.py" in result.stdout


def test_lossy_compaction_spill_recovers_full_clean_output() -> None:
    raw = "\n".join(f"12:00:{i:02d} INFO processed batch {i}" for i in range(100))
    result = _compact(raw, budget=600)
    match = re.search(r"full: (\S+\.txt)", result.spill_hint)
    assert match is not None
    recovered = Path(match.group(1)).read_text(encoding="utf-8")
    assert recovered == raw
