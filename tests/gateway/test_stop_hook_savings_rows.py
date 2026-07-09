"""Stop-hook savings writers: telegraphic output-style rows + rtk gain credit."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path
from types import ModuleType

import pytest

from atelier.core.capabilities.pricing import get_model_pricing

_STOP = Path("integrations/claude/plugin/hooks/stop.py")
MODEL = "claude-sonnet-4-5"


def _load_stop() -> ModuleType:
    spec = importlib.util.spec_from_file_location("atelier_stop_hook_savings", _STOP)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_sidecar(root: Path, sid: str) -> Path:
    from atelier.core.foundation.paths import session_dir

    d = session_dir(root, "claude", sid)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "savings.jsonl"
    p.write_text(
        json.dumps({"tool": "read", "tokens": 100, "calls": 0, "model": MODEL, "ts": "2026-07-05T10:00:00"}) + "\n",
        encoding="utf-8",
    )
    return p


def _rows(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _write_transcript(tmp_path: Path, prose_chars: int, code_chars: int) -> str:
    text = ("p" * prose_chars) + "```\n" + ("c" * code_chars) + "\n```"
    line = {
        "type": "assistant",
        "timestamp": "2026-07-05T10:00:00Z",
        "message": {"id": "m1", "model": MODEL, "content": [{"type": "text", "text": text}]},
    }
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps(line) + "\n", encoding="utf-8")
    return str(p)


def test_output_style_row_credits_prose_not_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
    monkeypatch.setenv("ATELIER_OUTPUT_STYLE_RATIO", "1.5")
    stop = _load_stop()
    sid = "sid-out"
    sidecar = _seed_sidecar(tmp_path, sid)
    transcript = _write_transcript(tmp_path, prose_chars=4000, code_chars=4000)

    stop._write_output_style_row(sid, {"last_model": MODEL}, transcript)

    out_rows = [r for r in _rows(sidecar) if r.get("kind") == "output_style"]
    assert len(out_rows) == 1
    row = out_rows[0]
    # Code fence excluded: basis is ~1000 prose tokens (4000 chars / 4), so
    # ratio 1.5 credits ~500 avoided output tokens — never the code chars.
    assert 480 <= row["tokens"] <= 520
    pricing = get_model_pricing(MODEL)
    assert pricing is not None
    expected = pricing.request_cost_usd(output_tokens=row["tokens"], cache_write_tokens=row["tokens"])
    assert row["cost_saved_usd"] == pytest.approx(expected, rel=1e-3)

    # Second Stop fire on the unchanged transcript: cumulative marker → no new row.
    stop._write_output_style_row(sid, {"last_model": MODEL}, transcript)
    assert len([r for r in _rows(sidecar) if r.get("kind") == "output_style"]) == 1


def test_output_style_disabled_at_ratio_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
    monkeypatch.setenv("ATELIER_OUTPUT_STYLE_RATIO", "1.0")
    stop = _load_stop()
    sid = "sid-off"
    sidecar = _seed_sidecar(tmp_path, sid)
    transcript = _write_transcript(tmp_path, prose_chars=4000, code_chars=0)
    stop._write_output_style_row(sid, {"last_model": MODEL}, transcript)
    assert [r for r in _rows(sidecar) if r.get("kind") == "output_style"] == []


def test_rtk_total_tokens_saved_parses_variants() -> None:
    stop = _load_stop()
    # The REAL rtk v0.43.0 shape (verified against the binary): the token total
    # lives at summary.total_saved; avg_savings_pct must never be mistaken for it.
    real = {
        "summary": {
            "total_commands": 12,
            "total_input": 50_000,
            "total_output": 9_000,
            "total_saved": 41_000,
            "avg_savings_pct": 82.0,
            "total_time_ms": 300,
            "avg_time_ms": 25,
        }
    }
    assert stop._rtk_total_tokens_saved(real) == 41_000
    # Alternate/older spellings still parse; totals dominate per-command rows.
    payload = {
        "total": {"tokens_saved": 1234},
        "commands": [{"name": "git", "saved_tokens": 10}, {"name": "grep", "tokens_saved": 90}],
        "noise": {"tokens": 99999, "saved": 5},
    }
    assert stop._rtk_total_tokens_saved(payload) == 1234
    assert stop._rtk_total_tokens_saved({}) == 0
    assert stop._rtk_total_tokens_saved([{"total_tokens_saved": 7}]) == 7


def test_credit_rtk_gain_with_fake_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(workspace))
    stop = _load_stop()
    sid = "sid-rtk"
    sidecar = _seed_sidecar(tmp_path, sid)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "rtk"
    fake.write_text("#!/bin/sh\necho '{\"total_tokens_saved\": 5000}'\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")

    stop._credit_rtk_gain(sid, {"last_model": MODEL})

    rtk_rows = [r for r in _rows(sidecar) if r.get("kind") == "external_compactor"]
    assert len(rtk_rows) == 1
    assert rtk_rows[0]["tokens"] == 5000
    pricing = get_model_pricing(MODEL)
    assert pricing is not None
    assert rtk_rows[0]["cost_saved_usd"] == pytest.approx(pricing.request_cost_usd(input_tokens=5000), rel=1e-3)
    # Per-workspace marker: a second Stop fire credits nothing new.
    stop._credit_rtk_gain(sid, {"last_model": MODEL})
    assert len([r for r in _rows(sidecar) if r.get("kind") == "external_compactor"]) == 1
    marker = json.loads((tmp_path / "rtk_gain_state.json").read_text(encoding="utf-8"))
    assert marker["credited_by_workspace"][stop._workspace_key(str(workspace))] == 5000
    # A different workspace has its own counter — project A's rtk tokens are
    # never attributed to project B's sessions.
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(other))
    stop._credit_rtk_gain(sid, {"last_model": MODEL})
    rows_after = [r for r in _rows(sidecar) if r.get("kind") == "external_compactor"]
    assert len(rows_after) == 2  # fresh workspace counter starts at 0 → credits its own 5000


def test_output_style_default_ratio_is_bench_measured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default ratio = 2.09: prose-only telegraphic Q&A ratio, no turn-cut overlap."""
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
    monkeypatch.delenv("ATELIER_OUTPUT_STYLE_RATIO", raising=False)
    stop = _load_stop()
    sid = "sid-out-default"
    sidecar = _seed_sidecar(tmp_path, sid)
    transcript = _write_transcript(tmp_path, prose_chars=4000, code_chars=0)

    stop._write_output_style_row(sid, {"last_model": MODEL}, transcript)

    rows = [r for r in _rows(sidecar) if r.get("kind") == "output_style"]
    assert len(rows) == 1
    assert rows[0]["ratio"] == pytest.approx(2.09)
    # ~1000 prose tokens x (2.09 - 1) ≈ 1090 avoided output tokens.
    assert 1050 <= rows[0]["tokens"] <= 1130


def test_output_style_basis_excludes_thinking_and_codeish_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixed output never enters the basis: thinking blocks and bare
    code/diff/JSON lines are style-invariant, so crediting them would apply
    the measured prose ratio to a quantity it was never measured on."""
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
    monkeypatch.delenv("ATELIER_OUTPUT_STYLE_RATIO", raising=False)
    stop = _load_stop()
    sid = "sid-out-fixed"
    sidecar = _seed_sidecar(tmp_path, sid)
    # Thinking-only + code-ish-only output → zero basis → no row.
    line = {
        "type": "assistant",
        "timestamp": "2026-07-05T10:00:00Z",
        "message": {
            "id": "m1",
            "model": MODEL,
            "content": [
                {"type": "thinking", "thinking": "t" * 8000},
                {"type": "text", "text": '+ added line\n- removed line\n{"json": 1}\ndef f():\n'},
            ],
        },
    }
    p = tmp_path / "fixed.jsonl"
    p.write_text(json.dumps(line) + "\n", encoding="utf-8")
    stop._write_output_style_row(sid, {"last_model": MODEL}, str(p))
    assert [r for r in _rows(sidecar) if r.get("kind") == "output_style"] == []


def test_input_style_row_credits_cache_read_delta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
    monkeypatch.setenv("ATELIER_INPUT_STYLE_RATIO", "1.5")
    stop = _load_stop()
    sid = "sid-in"
    sidecar = _seed_sidecar(tmp_path, sid)
    stats = {"last_model": MODEL, "cache_read_tokens": 100_000}

    stop._write_input_style_row(sid, stats)

    in_rows = [r for r in _rows(sidecar) if r.get("kind") == "input_style"]
    assert len(in_rows) == 1
    row = in_rows[0]
    # 100k cache-read tokens x (1.5 - 1) = 50k credited tokens.
    assert row["tokens"] == 50_000
    pricing = get_model_pricing(MODEL)
    assert pricing is not None
    expected = pricing.request_cost_usd(cache_read_tokens=row["tokens"])
    assert row["cost_saved_usd"] == pytest.approx(expected, rel=1e-3)

    # Second Stop fire at the SAME cumulative cache_read_tokens: no new row.
    stop._write_input_style_row(sid, stats)
    assert len([r for r in _rows(sidecar) if r.get("kind") == "input_style"]) == 1

    # Third fire with more cache-read consumed: only the incremental delta credits.
    stop._write_input_style_row(sid, {**stats, "cache_read_tokens": 140_000})
    in_rows = [r for r in _rows(sidecar) if r.get("kind") == "input_style"]
    assert len(in_rows) == 2
    # 40k delta x 0.5 = 20k credited tokens.
    assert in_rows[1]["tokens"] == 20_000


def test_input_style_disabled_at_ratio_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
    monkeypatch.setenv("ATELIER_INPUT_STYLE_RATIO", "1.0")
    stop = _load_stop()
    sid = "sid-in-off"
    sidecar = _seed_sidecar(tmp_path, sid)
    stop._write_input_style_row(sid, {"last_model": MODEL, "cache_read_tokens": 100_000})
    assert [r for r in _rows(sidecar) if r.get("kind") == "input_style"] == []


def test_input_style_default_ratio_is_bench_measured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default ratio = 1.16: swe50 per-turn cache-read leanness, net of turn_cut."""
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
    monkeypatch.delenv("ATELIER_INPUT_STYLE_RATIO", raising=False)
    stop = _load_stop()
    sid = "sid-in-default"
    sidecar = _seed_sidecar(tmp_path, sid)

    stop._write_input_style_row(sid, {"last_model": MODEL, "cache_read_tokens": 100_000})

    rows = [r for r in _rows(sidecar) if r.get("kind") == "input_style"]
    assert len(rows) == 1
    assert rows[0]["ratio"] == pytest.approx(1.16)
    # 100k cache-read tokens x (1.16 - 1) ≈ 16k avoided cache-read tokens.
    assert 15_950 <= rows[0]["tokens"] <= 16_000


def test_turn_cut_row_tops_up_to_bench_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """turn_cut credits target = turns x 0.642 minus ledger calls; converges."""
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
    monkeypatch.delenv("ATELIER_TURN_CUT_RATIO", raising=False)
    stop = _load_stop()
    sid = "sid-turncut"
    sidecar = _seed_sidecar(tmp_path, sid)
    with sidecar.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps({"tool": "read", "tokens": 0, "calls": 3, "model": MODEL, "ts": "2026-07-05T10:00:01"}) + "\n"
        )
    stats = {"turns": 50, "cache_read_tokens": 5_000_000, "output_tokens": 50_000, "last_model": MODEL}

    stop._write_turn_cut_row(sid, stats)

    rows = [r for r in _rows(sidecar) if r.get("kind") == "turn_cut"]
    assert len(rows) == 1
    assert rows[0]["calls"] == int(50 * 0.642) - 3  # bench floor minus explicit per-call credits
    pricing = get_model_pricing(MODEL)
    assert pricing is not None
    # Priced like the dispatcher's avoided-call rule: ctx re-send + a turn of output.
    expected = pricing.request_cost_usd(
        cache_read_tokens=rows[0]["calls"] * 100_000,
        output_tokens=rows[0]["calls"] * 1_000,
        cache_write_tokens=rows[0]["calls"] * 1_000,
    )
    assert rows[0]["calls_usd"] == pytest.approx(expected, rel=1e-3)

    # Same turn count → target already met → no second row.
    stop._write_turn_cut_row(sid, stats)
    assert len([r for r in _rows(sidecar) if r.get("kind") == "turn_cut"]) == 1

    # Session grows → only the growth is credited (prior turn_cut rows count).
    stop._write_turn_cut_row(sid, dict(stats, turns=100))
    rows = [r for r in _rows(sidecar) if r.get("kind") == "turn_cut"]
    assert sum(r["calls"] for r in rows) == int(100 * 0.642) - 3


def test_turn_cut_disabled_and_never_guesses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
    stop = _load_stop()
    sid = "sid-turncut-off"
    sidecar = _seed_sidecar(tmp_path, sid)
    stats = {"turns": 50, "cache_read_tokens": 5_000_000, "output_tokens": 50_000, "last_model": MODEL}
    # ratio <= 0 disables
    monkeypatch.setenv("ATELIER_TURN_CUT_RATIO", "0")
    stop._write_turn_cut_row(sid, stats)
    assert [r for r in _rows(sidecar) if r.get("kind") == "turn_cut"] == []
    # unknown model → no row, never guess a rate
    monkeypatch.delenv("ATELIER_TURN_CUT_RATIO", raising=False)
    stop._write_turn_cut_row(sid, dict(stats, last_model="mystery-model-9"))
    assert [r for r in _rows(sidecar) if r.get("kind") == "turn_cut"] == []


def test_refresh_statusline_frames_writes_sidecar_at_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop-time ledger rows must reach the statusline without waiting a turn.

    The MCP server rewrites statusline_frames only on tool-call appends, so the
    output_style (↓O) and cost/carry rows written by THIS hook would otherwise
    stay invisible exactly while the user sits at the prompt.
    """
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
    stop = _load_stop()
    sid = "sid-frames"
    _seed_sidecar(tmp_path, sid)

    stop._refresh_statusline_frames(sid)

    from atelier.core.foundation.paths import find_session_dir

    d = find_session_dir(tmp_path, sid)
    assert d is not None
    frames = (d / "statusline_frames").read_text(encoding="utf-8")
    assert frames.strip()  # at least the cost frame is always emitted
    assert (d / "statusline_segment").read_text(encoding="utf-8").strip()
    # Unknown session → no directory minted, no crash.
    stop._refresh_statusline_frames("no-such-session")
    assert find_session_dir(tmp_path, "no-such-session") is None


def test_credit_rtk_gain_disabled_by_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
    monkeypatch.setenv("ATELIER_RTK_GAIN_CREDIT", "0")
    stop = _load_stop()
    sid = "sid-rtk-off"
    sidecar = _seed_sidecar(tmp_path, sid)
    stop._credit_rtk_gain(sid, {"last_model": MODEL})
    assert [r for r in _rows(sidecar) if r.get("kind") == "external_compactor"] == []
