from __future__ import annotations

from pathlib import Path

from atelier.infra.runtime.realtime_context import RealtimeContextManager


def test_realtime_context_records_and_compacts(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    mgr = RealtimeContextManager(root)

    mgr.record_tool_input("lint", {"task": "fix", "plan": ["a", "b"]})
    mgr.record_tool_output(
        "lint",
        {"status": "blocked", "warnings": ["missing validation"]},
    )
    mgr.record_tool_error("lint", "AssertionError: expected 200 got 500")
    mgr.record_prompt_response(
        "Please fix the failing endpoint and avoid retries",
        "I found repeated AssertionError and will add a guardrail",
    )
    mgr.record_bash_output(
        "pytest -q",
        ok=False,
        stdout="..F..",
        stderr="AssertionError: expected 200 got 500\nTraceback ...",
    )
    mgr.persist()

    snap = mgr.snapshot()
    assert snap["items"] >= 4
    assert "prompt_block" in snap
    assert snap["raw_chars"] > 0
    assert "reduction_pct" in snap


def test_realtime_context_persists_to_disk(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    mgr = RealtimeContextManager(root)
    mgr.record_tool_input("reasoning", {"task": "ship"})
    mgr.persist()

    path = root / "runtime" / "realtime_context.json"
    assert path.exists()
    assert "snapshot" in path.read_text(encoding="utf-8")
