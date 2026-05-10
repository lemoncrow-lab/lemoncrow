"""Compresses a run ledger into a tiny state packet for the next turn.

The compressor is the token-optimizer in the spec. Instead of feeding
the next agent turn the entire raw transcript, we feed it:

  - the files changed (with most recent action per file)
  - the unique error fingerprints seen
  - the monitor alerts at >= medium severity
  - the current blocker, computed as the latest unresolved alert or the
    last failed command

This is enough for the next turn to make a coherent decision without
re-reading 50k tokens of tool output.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from atelier.infra.runtime.run_ledger import RunLedger


@dataclass
class CompactState:
    files_changed: dict[str, str] = field(default_factory=dict)
    """Mapping of file path -> last action ('edit' or 'revert')."""
    error_fingerprints: list[str] = field(default_factory=list)
    high_severity_alerts: list[str] = field(default_factory=list)
    current_blocker: str | None = None
    tool_call_count: int = 0
    total_tool_output_chars: int = 0

    def to_prompt_block(self) -> str:
        lines: list[str] = ["## Atelier compact state"]
        if self.files_changed:
            lines.append("Files touched:")
            for path, action in self.files_changed.items():
                lines.append(f"  - {action}: {path}")
        if self.error_fingerprints:
            lines.append("Distinct errors seen:")
            for fp in self.error_fingerprints:
                lines.append(f"  - {fp}")
        if self.high_severity_alerts:
            lines.append("Active alerts:")
            for msg in self.high_severity_alerts:
                lines.append(f"  - {msg}")
        if self.current_blocker:
            lines.append(f"Current blocker: {self.current_blocker}")
        lines.append(f"Stats: tool_calls={self.tool_call_count} output_chars={self.total_tool_output_chars}")
        return "\n".join(lines)


class ContextCompressor:
    def compress(self, ledger: RunLedger) -> CompactState:
        files: dict[str, str] = {}
        errors: list[str] = []
        seen_errors: set[str] = set()
        alerts: list[str] = []
        last_failed_cmd: str | None = None

        for event in ledger.events:
            if event.kind in ("file_edit", "file_revert"):
                path = str(event.payload.get("path", ""))
                action = "revert" if event.kind == "file_revert" else "edit"
                if path:
                    files[path] = action
            elif event.kind == "command_result":
                ok = bool(event.payload.get("ok"))
                err = str(event.payload.get("error_signature", "")).strip()
                if not ok:
                    last_failed_cmd = event.summary
                    if err and err not in seen_errors:
                        seen_errors.add(err)
                        errors.append(err)
            elif event.kind == "watchdog_alert":
                sev = str(event.payload.get("severity", ""))
                if sev in ("medium", "high"):
                    alerts.append(event.summary)

        blocker: str | None = None
        if alerts:
            blocker = alerts[-1]
        elif last_failed_cmd:
            blocker = f"last failed command: {last_failed_cmd}"

        tool_calls = [e for e in ledger.events if e.kind == "tool_call"]
        total_chars = sum(int(e.payload.get("output_chars", 0)) for e in tool_calls)

        return CompactState(
            files_changed=files,
            error_fingerprints=errors,
            high_severity_alerts=alerts,
            current_blocker=blocker,
            tool_call_count=len(tool_calls),
            total_tool_output_chars=total_chars,
        )
