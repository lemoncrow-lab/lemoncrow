"""Stop hook: verify-before-done (Claude Code).

Thin host adapter over ``lemoncrow.core.capabilities.verify_gate``. This file
owns only the Claude-Code-specific concern -- parsing the transcript JSONL into
host-neutral :class:`VerifySignals` -- then delegates the decision to the shared
core so Codex/OpenCode reuse identical logic off their own run ledgers.

See ``verify_gate`` for the full rationale (real-test-runner bar, completeness
detectors A/B, fire-once state) and the LEMONCROW_VERIFY_* opt-outs. Bounded and
fail-open: fires at most once per session (returns immediately when
``stop_hook_active`` is set) and any error exits 0 without blocking.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.verify_gate import (
    _TEST_RUN,
    VerifySignals,
    disabled,
    is_code_path,
    is_verifiable_path,
)
from lemoncrow.core.capabilities.verify_gate import decide as decide_signals


def _is_edit_tool(name: str) -> bool:
    return name in {"edit", "write", "multiedit", "notebookedit"} or name.endswith("edit")


def _edit_targets(tool_input: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("file_path", "path", "filename"):
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            out.append(val)
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for entry in edits:
            if isinstance(entry, dict):
                fp = entry.get("file_path") or entry.get("path")
                if isinstance(fp, str) and fp:
                    out.append(fp)
    return out


def _edit_diffs(tool_input: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Return (path, old, new) for every edit in an edit-tool input."""
    out: list[tuple[str, str, str]] = []
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for entry in edits:
            if isinstance(entry, dict):
                p = entry.get("path") or entry.get("file_path")
                if isinstance(p, str) and p:
                    old = entry.get("old") or entry.get("old_string") or ""
                    new = entry.get("new") or entry.get("new_string") or ""
                    out.append((p, str(old), str(new)))
    else:
        p = tool_input.get("file_path") or tool_input.get("path")
        if isinstance(p, str) and p:
            old = tool_input.get("old_string") or ""
            new = tool_input.get("new_string") or tool_input.get("new") or ""
            out.append((p, str(old), str(new)))
    return out


def _block_text(entry: dict[str, Any]) -> str:
    """Concatenate the text of a user message (the issue prompt lives here)."""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def scan_transcript(transcript_path: str | None) -> tuple[list[str], bool]:
    """Return (edited code files, whether a behavioral check was executed)."""
    signals = scan_transcript_rich(transcript_path)
    return signals.edited, signals.verified


def scan_transcript_rich(transcript_path: str | None) -> VerifySignals:
    """Parse a Claude Code transcript JSONL into host-neutral VerifySignals.

    A test run only counts as verification when it happened AFTER the last
    code edit (a pre-edit run proves nothing about the change) and, when the
    outcome is detectable via the tool_result ``is_error`` flag, only when it
    succeeded. A run with no visible result is counted (fail-open).

    An edit tool call only counts as an edit when its own tool_result did NOT
    come back ``is_error`` -- a rejected/no-op edit (user declined it, nothing
    written) must not poison ``last_edit_idx``, or the hook re-blocks forever
    on every later Stop even though nothing on disk changed and the real edit
    was already verified (edit tool_use precedes its tool_result in the
    transcript, so this is resolved in a second pass after ``failed_ids`` is
    fully known).

    ``checked`` = a bash command names an edited file -- the real check for a
    data/artifact task that has no test suite to run.
    """
    edited: list[str] = []
    checked = False
    diffs: list[tuple[str, str, str]] = []
    prompt = ""
    edit_events: list[tuple[int, str, list[str], list[tuple[str, str, str]]]] = (
        []
    )  # (order, tool_use id, targets, diffs)
    test_runs: list[tuple[int, str]] = []  # (event order, tool_use id)
    failed_ids: set[str] = set()
    idx = 0
    cmds: list[str] = []
    if not transcript_path:
        return VerifySignals()
    p = Path(transcript_path)
    if not p.exists():
        return VerifySignals()
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return VerifySignals()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "user":
            if not prompt:
                prompt = _block_text(entry)
            message = entry.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                        tid = str(block.get("tool_use_id") or "")
                        if tid:
                            failed_ids.add(tid)
            continue
        if entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            idx += 1
            name = str(block.get("name") or "").split("__")[-1].lower()
            tool_input = block.get("input")
            if not isinstance(tool_input, dict):
                continue
            if _is_edit_tool(name):
                targets = [t for t in _edit_targets(tool_input) if is_verifiable_path(t, include_docs=True)]
                call_diffs = [d for d in _edit_diffs(tool_input) if is_code_path(d[0])]
                if targets or call_diffs:
                    edit_events.append((idx, str(block.get("id") or ""), targets, call_diffs))
            elif name in {"bash", "shell"}:
                cmd = str(tool_input.get("command") or "")
                cmds.append(cmd)
                if _TEST_RUN.search(cmd):
                    test_runs.append((idx, str(block.get("id") or "")))
    # Second pass: drop edit attempts whose own tool_result came back is_error
    # (rejected/no-op -- see docstring) now that failed_ids covers the whole
    # transcript.
    live_edits = [e for e in edit_events if not e[1] or e[1] not in failed_ids]
    for _, _, targets, call_diffs in live_edits:
        edited.extend(targets)
        diffs.extend(call_diffs)
    last_edit_idx = max((i for i, _, _, _ in live_edits), default=-1)
    verified = any(i > last_edit_idx and (not tid or tid not in failed_ids) for i, tid in test_runs)
    # A data/artifact deliverable has no test suite -- exercising it (a bash command
    # naming the edited file) is the authoritative check. Code keeps the stricter
    # test-runner bar in decide(); the >=5 length guard avoids tiny-basename matches.
    bases = {b for b in (Path(p.split("#")[0]).name for p in edited) if len(b) >= 5}
    checked = any(b in c for c in cmds for b in bases)
    return VerifySignals(edited=edited, verified=verified, checked=checked, diffs=diffs, prompt=prompt)


def decide(payload: dict[str, Any]) -> dict[str, str] | None:
    if disabled():
        return None
    if payload.get("stop_hook_active") is True:
        return None
    signals = scan_transcript_rich(payload.get("transcript_path"))
    return decide_signals(signals, dedup_key=str(payload.get("transcript_path") or ""))


def _dormant() -> bool:
    """Cap exhausted -> LemonCrow steps aside (behavioral hooks emit nothing).
    Fail-open: any error -> False so the hook stays active rather than wrongly
    silencing. Measurement/reporting hooks are never gated by this."""
    try:
        import os

        from lemoncrow.core.capabilities.plugin_runtime import cap_exhausted

        root = (
            os.environ.get("LEMONCROW_ROOT")
            or os.environ.get("LEMONCROW_STORE_ROOT")
            or str(Path.home() / ".lemoncrow")
        )
        return bool(cap_exhausted(root))
    except Exception:  # noqa: BLE001 — hooks must never crash; fail-open (active)
        return False


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        if _dormant():
            return 0  # dormant: no verify-before-done nudge
        result = decide(payload)
        if result is not None:
            print(json.dumps(result))
    except Exception:  # noqa: BLE001  # fail-open: a hook must never crash the agent
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
