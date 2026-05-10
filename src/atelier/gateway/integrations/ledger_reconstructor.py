from __future__ import annotations

import json
from pathlib import Path

from atelier.gateway.hosts.session_parsers._session_parser import parse_session_turns
from atelier.infra.runtime.run_ledger import RunLedger


class LedgerReconstructor:
    """Reconstructs a RunLedger from imported session logs."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root

    def reconstruct(
        self,
        source: str,
        session_id: str,
        raw_content: str,
        task: str = "",
        domain: str = "coding",
    ) -> RunLedger:
        """Parse raw session content and build a fully populated RunLedger."""
        led = RunLedger(
            run_id=session_id,
            agent=source,
            task=task,
            domain=domain,
            root=self.root,
        )

        turns = parse_session_turns(raw_content, source)

        # We need to bypass the max length constraints of `parse_session_turns` ideally,
        # but since we are retrofitting, we use the parsed turns for events.

        for turn in turns:
            kind = turn.get("kind", "")
            summary = turn.get("summary", "")
            content = turn.get("content", "")

            if kind == "tool_call":
                name = summary.split("(")[0]
                try:
                    args = json.loads(content) if content else {}
                except Exception:
                    args = {}
                led.record_tool_call(tool=name, args=args)

            elif kind == "shell_command":
                led.record_command(command=content, ok=True)  # We might not know success accurately

            elif kind == "file_edit":
                # summary is usually name(path) or path
                path = summary
                if "(" in summary and summary.endswith(")"):
                    path = summary.split("(")[1][:-1]
                # content contains the full diff
                diff = content if content else None
                led.record_file_event(path=path, event="edit", diff=diff)

            elif kind in ("agent_message", "thinking"):
                text_lower = str(content).lower()

                # Record as an explicit ledger event so it shows up in the timeline
                snippet = summary if len(summary) < 150 else summary[:147] + "..."
                led.record(
                    kind="reasoning" if kind == "thinking" else "agent_message",
                    summary=snippet,
                    payload={"text": content},
                )

                if "hypothesis" in text_lower or "trying" in text_lower:
                    # Avoid extremely long hypotheses
                    led.add_hypothesis(snippet)
                if "verified" in text_lower or "confirmed" in text_lower:
                    led.add_verified_fact(snippet)

            elif kind == "patch":
                # For open code patch
                try:
                    files = json.loads(content)
                    if isinstance(files, list):
                        for f in files:
                            led.record_file_event(path=f, event="edit")
                except Exception:
                    # If content is a patch string directly, store it
                    if content:
                        led.record_file_event(path="patch", event="edit", diff=content)

        # Second pass: Extract token usage from raw content
        input_tokens = 0
        output_tokens = 0

        for line in raw_content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Claude usage extraction
            if source == "claude" and ev.get("type") == "assistant":
                usage = ev.get("message", {}).get("usage", {})
                input_tokens += usage.get("input_tokens", 0)
                output_tokens += usage.get("output_tokens", 0)

            # Codex / Copilot usage (best effort)
            elif source in ("codex", "copilot"):
                if "usage" in ev:
                    usage = ev.get("usage", {})
                    input_tokens += usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
                    output_tokens += usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)

            # OpenCode usage (best effort)
            elif source == "opencode":
                data = ev.get("data", {})
                if "usage" in data:
                    usage = data.get("usage", {})
                    input_tokens += usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
                    output_tokens += usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)

        # Update ledger token counts
        if input_tokens > 0 or output_tokens > 0:
            led.token_count = input_tokens + output_tokens

        led.close(status="success")  # Assume success for imported sessions if not failed explicitly
        return led
