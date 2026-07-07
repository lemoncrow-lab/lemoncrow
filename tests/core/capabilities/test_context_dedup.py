"""Tests for within-session content dedup (context_dedup)."""

from __future__ import annotations

import json

from atelier.core.capabilities.context_dedup import ContextDedup, current_epoch

_BIG = "x" * 5000  # above _MIN_DEDUP_CHARS
_BIG2 = "y" * 5000


def test_first_emit_is_not_stubbed_then_duplicate_is() -> None:
    d = ContextDedup()
    assert d.stub_for(session_id="s", content=_BIG, epoch=0, force=False) is None
    out = d.stub_for(session_id="s", content=_BIG, epoch=0, force=False)
    assert out is not None
    stub, saved = out
    assert "read #" in stub
    assert saved > 0


def test_force_bypasses_and_keeps_recording() -> None:
    d = ContextDedup()
    d.stub_for(session_id="s", content=_BIG, epoch=0, force=False)
    # force => no stub even though it's a duplicate
    assert d.stub_for(session_id="s", content=_BIG, epoch=0, force=True) is None
    # subsequent non-forced call still dedups
    assert d.stub_for(session_id="s", content=_BIG, epoch=0, force=False) is not None


def test_stub_and_delta_carry_self_heal_cue() -> None:
    # A fresh-context caller (e.g. a subagent) that never received the original
    # must be able to recover it: both the stub and the delta tell it to re-read
    # with force=true when the content is not in its context.
    d = ContextDedup()
    d.stub_for(session_id="s", content=_BIG, epoch=0, force=False)
    out = d.stub_for(session_id="s", content=_BIG, epoch=0, force=False)
    assert out is not None
    stub, _ = out
    assert "force=true" in stub
    assert "context" in stub

    d.delta_for(session_id="s", resource="r", content=_FILE_V1, epoch=0, force=False)
    out2 = d.delta_for(session_id="s", resource="r", content=_FILE_V2, epoch=0, force=False)
    assert out2 is not None
    delta, _ = out2
    assert "force=true" in delta


def test_small_content_is_never_stubbed() -> None:
    d = ContextDedup()
    small = "tiny"
    assert d.stub_for(session_id="s", content=small, epoch=0, force=False) is None
    assert d.stub_for(session_id="s", content=small, epoch=0, force=False) is None


def test_distinct_content_not_confused() -> None:
    d = ContextDedup()
    assert d.stub_for(session_id="s", content=_BIG, epoch=0, force=False) is None
    assert d.stub_for(session_id="s", content=_BIG2, epoch=0, force=False) is None


def test_epoch_change_resets_seen() -> None:
    d = ContextDedup()
    d.stub_for(session_id="s", content=_BIG, epoch=0, force=False)
    assert d.stub_for(session_id="s", content=_BIG, epoch=0, force=False) is not None
    # compaction bumped the epoch -> seen-set reset -> not a duplicate anymore
    assert d.stub_for(session_id="s", content=_BIG, epoch=1, force=False) is None


def test_sessions_are_isolated() -> None:
    d = ContextDedup()
    d.stub_for(session_id="a", content=_BIG, epoch=0, force=False)
    assert d.stub_for(session_id="b", content=_BIG, epoch=0, force=False) is None


def test_missing_session_id_is_noop() -> None:
    d = ContextDedup()
    assert d.stub_for(session_id="", content=_BIG, epoch=0, force=False) is None
    assert d.stub_for(session_id="", content=_BIG, epoch=0, force=False) is None


def test_current_epoch_reads_session_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    assert current_epoch() == 0
    from atelier.core.foundation.paths import workspace_key

    state_path = tmp_path / "workspaces" / workspace_key(tmp_path) / "session_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"compaction_epoch": 3}), encoding="utf-8")
    assert current_epoch() == 3


_FILE_V1 = "\n".join(f"line {i} {'pad' * 8}" for i in range(400)) + "\n"
_FILE_V2 = _FILE_V1.replace(f"line 200 {'pad' * 8}", "line 200 EDITED")


def test_delta_first_read_emits_full_then_diff_on_change() -> None:
    d = ContextDedup()
    assert d.delta_for(session_id="s", resource="r", content=_FILE_V1, epoch=0, force=False) is None
    out = d.delta_for(session_id="s", resource="r", content=_FILE_V2, epoch=0, force=False)
    assert out is not None
    delta, saved = out
    assert "[delta]" in delta
    assert "+line 200 EDITED" in delta
    assert saved > 0
    assert len(delta) < len(_FILE_V2) // 2


def test_delta_identical_content_returns_none_and_keeps_baseline() -> None:
    d = ContextDedup()
    d.delta_for(session_id="s", resource="r", content=_FILE_V1, epoch=0, force=False)
    assert d.delta_for(session_id="s", resource="r", content=_FILE_V1, epoch=0, force=False) is None
    # baseline survived: a change still diffs
    assert d.delta_for(session_id="s", resource="r", content=_FILE_V2, epoch=0, force=False) is not None


def test_delta_force_emits_full_but_updates_baseline() -> None:
    d = ContextDedup()
    d.delta_for(session_id="s", resource="r", content=_FILE_V1, epoch=0, force=False)
    assert d.delta_for(session_id="s", resource="r", content=_FILE_V2, epoch=0, force=True) is None
    # baseline is now V2, so re-reading V2 unchanged emits nothing
    assert d.delta_for(session_id="s", resource="r", content=_FILE_V2, epoch=0, force=False) is None


def test_delta_large_rewrite_falls_back_to_full_body() -> None:
    d = ContextDedup()
    d.delta_for(session_id="s", resource="r", content=_FILE_V1, epoch=0, force=False)
    rewritten = "\n".join(f"totally new {i}" for i in range(400)) + "\n"
    assert d.delta_for(session_id="s", resource="r", content=rewritten, epoch=0, force=False) is None


def test_delta_epoch_change_resets_baselines() -> None:
    d = ContextDedup()
    d.delta_for(session_id="s", resource="r", content=_FILE_V1, epoch=0, force=False)
    # epoch bump => V2 is treated as a first read
    assert d.delta_for(session_id="s", resource="r", content=_FILE_V2, epoch=1, force=False) is None
