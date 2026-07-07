"""PostToolUse hook — Atelier's bash compaction for HOST Bash tool results.

The host's builtin Bash tool bypasses Atelier's MCP bash lane entirely, so its
output reaches the model raw (vanilla ~30k-char truncation only). This hook
closes that gap: it applies the same post-hoc compaction pipeline the MCP lane
runs after every command -- ANSI stripping, dedup-with-count, test-failure
extraction, suppress-on-success for noisy mutators, anomaly windows, per-kind
char budgets, spill-store recovery, and secret redaction -- and replaces the
tool result via ``hookSpecificOutput.updatedToolOutput``.

Post-hoc by construction: the command has already run exactly once, so unlike
wrapper-style compactors (which must confine themselves to read-only
commands) this is safe for every command, including side-effecting ones.

FAIL-OPEN everywhere: small outputs, trivial savings, unrecognized payload
shapes, an unknown-schema host, import errors, or any exception -> this
script emits nothing and exits 0, so the original tool result stands.

Exit-code reality: current Claude Code passes no exit code in the Bash
``tool_response`` (only stdout/stderr/interrupted/isImage), so the compactor
treats the run as unproven-success -- suppress-on-success stays off unless a
``returnCode``-style field is present (SDK variants). All other tiers (dedup,
test extraction, anomaly windows, budgets) are exit-code independent.

Deliberately NOT done here: PreToolUse ``updatedInput`` command rewriting for
the host lane. Modifying input requires ``permissionDecision: "allow"``,
which auto-approves the rewritten command past the user's permission prompt
-- the same permission-widening we reject in wrapper-style hook integrations.
Post-hoc output compaction needs no permission changes.

Env:
    ATELIER_HOST_BASH_SHRINK=0   Kill switch -- disables this hook entirely.
    ATELIER_TOOL_OUTPUT_SPILL=0  Disables the spill store (recovery hint is
                                 then omitted; the compaction still applies).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# Below the smallest MCP-lane budget there is nothing to gain, and skipping
# early avoids importing atelier (the expensive part) for every small command.
_MIN_SHRINK_CHARS = 2000
# Never churn the transcript for a trivial trim.
_MIN_SAVED_CHARS = 500


def _exit_code(tool_response: dict[str, Any]) -> int | None:
    for key in ("returnCode", "return_code", "exitCode", "exit_code"):
        value = tool_response.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def _run(payload: dict[str, Any]) -> int:
    if str(payload.get("tool_name") or "") != "Bash":
        return 0
    tool_input = payload.get("tool_input") or {}
    tool_response = payload.get("tool_response")
    if not isinstance(tool_input, dict) or not isinstance(tool_response, dict):
        return 0
    if tool_response.get("isImage"):
        return 0  # image output is not text; nothing to compact
    command = str(tool_input.get("command") or "")
    stdout = str(tool_response.get("stdout") or "")
    stderr = str(tool_response.get("stderr") or "")
    original_chars = len(stdout) + len(stderr)
    if not command or original_chars < _MIN_SHRINK_CHARS:
        return 0

    from atelier.core.capabilities.tool_supervision.bash_exec import compact_host_bash_output

    result = compact_host_bash_output(command, stdout, stderr, _exit_code(tool_response))
    compact_chars = len(result.stdout) + len(result.stderr)
    if original_chars - compact_chars < _MIN_SAVED_CHARS:
        return 0

    if result.spill_hint:
        # Already the canonical footer -- bash_exec composed it via
        # tool_output_spill.spill_notice against its own internal char
        # accounting.
        footer = result.spill_hint
    else:
        from atelier.core.capabilities.tool_supervision import tool_output_spill

        footer = tool_output_spill.spill_notice(
            verb="shrunk", original_chars=original_chars, kept_chars=compact_chars, path=None
        )
    updated = dict(tool_response)
    updated["stdout"] = f"{result.stdout}\n\n{footer}" if result.stdout else footer
    updated["stderr"] = result.stderr
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "updatedToolOutput": updated,
                }
            }
        )
    )
    return 0


def main() -> int:
    if os.environ.get("ATELIER_HOST_BASH_SHRINK", "1").strip() == "0":
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        return _run(payload)
    except Exception:  # noqa: BLE001 -- fail-open contract: never break the tool result
        return 0


if __name__ == "__main__":
    sys.exit(main())
