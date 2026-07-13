"""Smoke-test for the rotating savings_segment function."""

import json
import time
from pathlib import Path

import pytest


@pytest.fixture()
def lemoncrow_root(tmp_path: Path) -> Path:
    root = tmp_path / ".lemoncrow"
    root.mkdir()
    (root / "runs").mkdir()
    (root / "reviews").mkdir()
    # Suppress the "login" status tip so status_text is empty in tests.
    (root / "auth.json").write_text(json.dumps({"authenticated": True}))
    # Signed-in: keeps the login-nudge frame out of the default frame set.
    (root / "auth_token").write_text("test-token")
    # Suppress status tips so no extra frame is injected.
    (root / "plugin_settings.json").write_text(json.dumps({"lemoncrow": {"statusLineTips": False}}))
    return root


def _set_frame(root: Path, counter: int) -> None:
    # Fresh ts so _get_frame_index does NOT auto-advance during the test.
    state = root / "statusline_frame_state.json"
    state.write_text(json.dumps({"counter": counter, "ts": time.time()}))


def _segment(root: Path, counter: int, **kw: object) -> str:
    from lemoncrow.core.capabilities.savings_summary import savings_segment

    _set_frame(root, counter)
    return savings_segment("", lemoncrow_root=root, no_color=True, **kw)  # type: ignore[arg-type]


def test_frame0_shows_cost_and_total_saved_breakdown(lemoncrow_root: Path) -> None:
    # Frame 0: cost + I/C/O breakdown (unchanged), then total-saved + R/C breakdown.
    seg = _segment(lemoncrow_root, 0, live_in_tok=10_000, live_cache_tok=50_000, live_out_tok=2_000)
    assert seg.startswith(" $0.00(I:10.0k C:50.0k O:2.0k)"), f"expected cost-led output, got: {seg!r}"
    # No realized/output/carry savings configured — trailing savings is $0.00.
    assert "$0.00(I:0)" in seg


def test_frame0_folds_savings_output_and_carry_into_one_headline(
    lemoncrow_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Realized savings, output-savings share, and the context-carry
    counterfactual are folded into ONE ↓ $ figure (cost segment is separate
    and unaffected; there's no more standalone ♻ carry segment). Carry TOKENS
    still surface as their own breakdown field (C:)."""
    from lemoncrow.core.capabilities.savings_summary import (
        SavingsSummary,
        savings_frames,
    )

    summary = SavingsSummary(
        saved_usd=1.242,
        output_saved_usd=0.500,
        carry_usd=1.932,
        carry_tokens=1_900_000,
        carry_pct=61.0,
    )

    def _fake_summary(*args: object, **kw: object) -> SavingsSummary:
        return summary

    monkeypatch.setattr(
        "lemoncrow.core.capabilities.savings_summary.compute_savings_summary",
        _fake_summary,
    )

    frames = savings_frames(
        "test-session",
        lemoncrow_root=lemoncrow_root,
        no_color=True,
        live_in_tok=379_000,
        live_cache_tok=28_600_000,
        live_out_tok=147_000,
    )
    frame0 = frames[0]

    # Headline = total_saved_usd = saved_usd + carry_usd = 3.174 (output_saved_usd
    # is already folded INTO saved_usd by compute_savings_summary, so it must
    # not be added again here).
    assert "$3.17" in frame0, f"expected folded total in {frame0!r}"
    assert "1.242" not in frame0, f"realized savings must not appear standalone in {frame0!r}"
    assert "1.932" not in frame0, f"carry usd must not appear standalone in {frame0!r}"
    # Carry TOKENS still surface as their own breakdown field.
    assert "K:1.90M" in frame0, f"expected carry token breakdown in {frame0!r}"
    assert "↓ $3.17" in frame0, f"expected ↓-led folded total in {frame0!r}"
    assert "♻" not in frame0, f"no separate carry icon expected in {frame0!r}"


def test_frame1_shows_token_breakdown(lemoncrow_root: Path) -> None:
    # Weighted index 1 is still frame 0's content (frame 0 holds 3 slots).
    seg = _segment(lemoncrow_root, 1, live_in_tok=10_000, live_cache_tok=50_000, live_out_tok=2_000)
    assert "I:10.0k" in seg
    assert "C:50.0k" in seg
    assert "O:2.0k" in seg


def test_frame_wraps_when_few_frames(lemoncrow_root: Path) -> None:
    """With no savings/carry/usage/historical activity, frame 0 (cost + I/C/O)
    is the sole frame and is shown for every counter."""
    for i in range(4):
        seg = _segment(lemoncrow_root, i)
        assert "$0.00(I:0 C:0 O:0)" in seg, f"counter={i}: {seg!r}"
        assert seg.startswith(" $"), f"counter={i}: {seg!r}"


def test_historical_savings_empty(lemoncrow_root: Path) -> None:
    from lemoncrow.core.capabilities.savings_summary import _read_historical_savings

    usd, tok, _calls, _turns, _spend, _carry, _routing, *_rest = _read_historical_savings(7, lemoncrow_root)
    assert usd == 0.0
    assert tok == 0


def test_historical_savings_reads_recent_rows(lemoncrow_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.core.capabilities.savings_summary import _read_historical_savings

    sidecar = lemoncrow_root / "sessions" / "abc123"
    sidecar.mkdir(parents=True)
    ledger = sidecar / "savings.jsonl"
    now_iso = "2026-06-15T10:00:00"
    old_iso = "2020-01-01T00:00:00"  # definitely outside any window
    rows = [
        json.dumps({"ts": now_iso, "tokens": 1000, "cost_saved_usd": 0.5}),
        json.dumps({"ts": old_iso, "tokens": 9999, "cost_saved_usd": 99.0}),
    ]
    ledger.write_text("\n".join(rows))

    # Patch time.time so "now" is close to now_iso (2026-06-15)
    import time as time_mod

    target_ts = 1781524800.0  # approx 2026-06-15T10:00:00 UTC
    monkeypatch.setattr(time_mod, "time", lambda: target_ts)

    usd7, tok7, _calls7, _turns7, _spend7, _carry7, _routing7, *_rest7 = _read_historical_savings(7, lemoncrow_root)
    assert tok7 == 1000
    assert abs(usd7 - 0.5) < 1e-6


def test_review_verdict_none(lemoncrow_root: Path) -> None:
    from lemoncrow.core.capabilities.savings_summary import _read_review_verdict

    assert _read_review_verdict("nosuchsession", lemoncrow_root) == ""


def test_review_verdict_needs_fix(lemoncrow_root: Path) -> None:
    from lemoncrow.core.capabilities.savings_summary import _read_review_verdict

    sid = "test-session-001"
    log = lemoncrow_root / "reviews" / f"{sid}.jsonl"
    log.write_text(json.dumps({"verdict": "NEEDS_FIX", "consumed": False}) + "\n")
    assert _read_review_verdict(sid, lemoncrow_root) == "NEEDS_FIX"


def test_review_verdict_consumed_ignored(lemoncrow_root: Path) -> None:
    from lemoncrow.core.capabilities.savings_summary import _read_review_verdict

    sid = "test-session-002"
    log = lemoncrow_root / "reviews" / f"{sid}.jsonl"
    log.write_text(json.dumps({"verdict": "NEEDS_FIX", "consumed": True}) + "\n")
    assert _read_review_verdict(sid, lemoncrow_root) == ""


def test_savings_frames_weighted_and_segment_consistent(lemoncrow_root: Path) -> None:
    """savings_frames returns the full weighted list (frame 0 x3) and
    savings_segment always returns one of its entries — the MCP sidecar and
    the subprocess path can never disagree on frame content."""
    from lemoncrow.core.capabilities.savings_summary import savings_frames, savings_segment

    kw = {"live_in_tok": 10_000, "live_cache_tok": 50_000, "live_out_tok": 2_000}
    frames = savings_frames("", lemoncrow_root=lemoncrow_root, no_color=True, **kw)  # type: ignore[arg-type]
    assert len(frames) >= 3
    assert frames[0] == frames[1] == frames[2]  # frame 0 holds 3 slots
    assert "I:10.0k" in frames[0]

    for i in range(len(frames) + 1):
        _set_frame(lemoncrow_root, i)
        seg = savings_segment("", lemoncrow_root=lemoncrow_root, no_color=True, **kw)  # type: ignore[arg-type]
        assert seg in frames, f"counter={i}: {seg!r} not in frames"


def test_login_frame_only_for_unauthenticated(lemoncrow_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Free/unauthenticated users get a rotating '/lemoncrow login' frame; a
    signed-in user (auth_token present) does not."""
    from lemoncrow.core.capabilities.savings_summary import savings_frames

    monkeypatch.delenv("LEMONCROW_AUTH_TOKEN", raising=False)
    kw = {"live_in_tok": 10_000, "live_cache_tok": 50_000}

    # Signed in (fixture wrote auth_token): no login frame.
    frames = savings_frames("", lemoncrow_root=lemoncrow_root, no_color=True, **kw)  # type: ignore[arg-type]
    assert not any("/lemoncrow login" in f for f in frames)

    # Free: remove the token -> login frame appears exactly once.
    (lemoncrow_root / "auth_token").unlink()
    frames = savings_frames("", lemoncrow_root=lemoncrow_root, no_color=True, **kw)  # type: ignore[arg-type]
    login = [f for f in frames if "/lemoncrow login" in f]
    assert len(login) == 1, f"expected one login frame, got {login!r}"
    assert "not signed in" in login[0]

    # Env token also counts as signed in.
    monkeypatch.setenv("LEMONCROW_AUTH_TOKEN", "env-token")
    frames = savings_frames("", lemoncrow_root=lemoncrow_root, no_color=True, **kw)  # type: ignore[arg-type]
    assert not any("/lemoncrow login" in f for f in frames)


def test_dynamic_status_lines_excludes_frame0_and_strips_separators(
    lemoncrow_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plain-text dynamic messages omit the live cost/savings headline."""
    from lemoncrow.core.capabilities.savings_summary import dynamic_status_lines

    monkeypatch.delenv("LEMONCROW_AUTH_TOKEN", raising=False)

    # Signed in (fixture wrote auth_token): no login nudge, no frame-0 leak.
    lines = dynamic_status_lines("", lemoncrow_root=lemoncrow_root)
    assert not any("/lemoncrow login" in line for line in lines)
    assert not any("$0.00(I:" in line for line in lines)

    # Free: login nudge appears exactly once, as bare text (no "|", no ANSI).
    (lemoncrow_root / "auth_token").unlink()
    lines = dynamic_status_lines("", lemoncrow_root=lemoncrow_root)
    assert lines.count("not signed in -- /lemoncrow login to unlock Pro") == 1
    assert all("|" not in line and "\033" not in line for line in lines)


def test_dynamic_status_line_rotates_only_dynamic_messages(
    lemoncrow_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.core.capabilities import savings_summary

    monkeypatch.setattr(
        savings_summary,
        "dynamic_status_lines",
        lambda *_args, **_kwargs: ["1d: ↓ $2.00", "/lemoncrow:recall — past-session learning"],
    )
    (lemoncrow_root / "statusline_frame_state.json").write_text(json.dumps({"counter": 1, "ts": 9_000_000_000}))

    assert (
        savings_summary.dynamic_status_line("session", lemoncrow_root=lemoncrow_root)
        == "/lemoncrow:recall — past-session learning"
    )


def test_segment_pins_review_needs_fix(lemoncrow_root: Path) -> None:
    """NEEDS_FIX verdict must appear on every frame."""
    sid = "pinned-session"
    log = lemoncrow_root / "reviews" / f"{sid}.jsonl"
    log.write_text(json.dumps({"verdict": "NEEDS_FIX", "consumed": False}) + "\n")

    from lemoncrow.core.capabilities.savings_summary import savings_segment

    state = lemoncrow_root / "statusline_frame_state.json"
    for i in range(4):
        state.write_text(json.dumps({"counter": i, "ts": time.time()}))
        seg = savings_segment(sid, lemoncrow_root=lemoncrow_root, no_color=True)
        assert "NEEDS_FIX" in seg, f"frame {i}: {seg!r}"
