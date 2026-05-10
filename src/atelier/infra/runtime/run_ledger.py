"""Append-only ledger of observable events during an agent run."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atelier.core.foundation.models import (
    LedgerEvent,
    to_jsonable,
)
from atelier.infra.runtime.cost_tracker import CostTracker


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RunLedger:
    """Append-only ledger for a single agent run."""

    def __init__(
        self,
        run_id: str | None = None,
        agent: str | None = None,
        root: Path | None = None,
        task: str = "",
        domain: str | None = None,
    ) -> None:
        self.run_id = run_id or uuid.uuid4().hex
        self.agent = agent
        self.task = task
        self.domain = domain
        self.events: list[LedgerEvent] = []
        self.created_at = _utcnow()
        self.updated_at = self.created_at
        self.status: str = "running"
        self._root = root

        # V2 reasoning/procedural state
        self.current_plan: list[str] = []
        self.files_touched: list[str] = []
        self.tools_called: list[str] = []
        self.commands_run: list[str] = []
        self.tests_run: list[str] = []
        self.errors_seen: list[str] = []
        self.repeated_failures: list[str] = []
        self.hypotheses_tried: list[str] = []
        self.hypotheses_rejected: list[str] = []
        self.verified_facts: list[str] = []
        self.open_questions: list[str] = []
        self.active_reasonblocks: list[str] = []
        self.active_rubrics: list[str] = []
        self.current_blockers: list[str] = []
        self.next_required_validation: str | None = None
        self.token_count: int = 0
        self.tool_count: int = 0
        self.budget: dict[str, int] = {}
        # Per-call cost tracking (lazy: tracker only persists if a root is set).
        self._cost_root: Path | None = root
        self.cost_tracker: CostTracker | None = CostTracker(root) if root is not None else None

    # ----- setters -------------------------------------------------------- #

    def set_plan(self, plan: list[str]) -> None:
        self.current_plan = list(plan)
        self.updated_at = _utcnow()

    def add_hypothesis(self, hypothesis: str, *, rejected: bool = False) -> None:
        if rejected:
            if hypothesis not in self.hypotheses_rejected:
                self.hypotheses_rejected.append(hypothesis)
        else:
            if hypothesis not in self.hypotheses_tried:
                self.hypotheses_tried.append(hypothesis)
        self.updated_at = _utcnow()

    def add_verified_fact(self, fact: str) -> None:
        if fact not in self.verified_facts:
            self.verified_facts.append(fact)
        self.updated_at = _utcnow()

    def add_open_question(self, question: str) -> None:
        if question not in self.open_questions:
            self.open_questions.append(question)
        self.updated_at = _utcnow()

    def set_blocker(self, blocker: str) -> None:
        self.current_blockers = [blocker]
        self.updated_at = _utcnow()

    def set_next_validation(self, validation: str | None) -> None:
        self.next_required_validation = validation
        self.updated_at = _utcnow()

    # ----- recording ------------------------------------------------------ #

    def record(
        self,
        kind: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> LedgerEvent:
        event = LedgerEvent(
            kind=kind,  # type: ignore[arg-type]
            summary=summary,
            payload=payload or {},
        )
        self.events.append(event)
        self.updated_at = _utcnow()
        return event

    def record_tool_call(
        self,
        tool: str,
        args: dict[str, Any] | None = None,
        output: str | None = None,
        args_signature: str | None = None,
    ) -> LedgerEvent:
        from atelier.core.foundation.watchdogs import args_signature as _sig

        self.tool_count += 1
        if tool not in self.tools_called:
            self.tools_called.append(tool)

        signature = args_signature or _sig(args)

        return self.record(
            "tool_call",
            f"{tool}({signature})",
            {
                "tool": tool,
                "args": args or {},
                "output": output,
                "args_signature": signature,
                "output_chars": len(output) if output else 0,
            },
        )

    def record_command(
        self,
        command: str,
        ok: bool,
        error_signature: str = "",
        stdout: str | None = None,
        stderr: str | None = None,
    ) -> LedgerEvent:
        self.commands_run.append(command)
        if not ok:
            sig = error_signature.strip()
            if sig and sig not in self.errors_seen:
                self.errors_seen.append(sig)
        return self.record(
            "command_result",
            command,
            {"ok": ok, "error_signature": error_signature, "stdout": stdout, "stderr": stderr},
        )

    def record_file_event(self, path: str, event: str, diff: str | None = None) -> LedgerEvent:
        if path and path not in self.files_touched:
            self.files_touched.append(path)
        kind = "file_revert" if event == "revert" else "file_edit"
        payload = {"path": path, "event": event}
        if diff:
            payload["diff"] = diff
        return self.record(kind, f"{event}:{path}", payload)

    def record_alert(self, monitor: str, severity: str, message: str) -> LedgerEvent:
        if severity == "high":
            self.current_blockers = [f"[{monitor}] {message}"]
        return self.record(
            "watchdog_alert",
            f"[{severity}] {monitor}: {message}",
            {"monitor": monitor, "severity": severity, "message": message},
        )

    def record_test(self, test_id: str, passed: bool, detail: str = "") -> LedgerEvent:
        if test_id not in self.tests_run:
            self.tests_run.append(test_id)
        return self.record(
            "test_result",
            f"{test_id}={'pass' if passed else 'fail'}",
            {"test_id": test_id, "passed": passed, "detail": detail},
        )

    def record_call(
        self,
        *,
        operation: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cost_usd: float | None = None,
        lessons_used: list[str] | None = None,
        prompt: str | None = None,
        response: str | None = None,
    ) -> LedgerEvent:
        """Record a single LLM call with cost + lessons attribution."""
        if self.cost_tracker is None:
            self.cost_tracker = CostTracker(Path("."))
        rec = self.cost_tracker.record_call(
            operation=operation,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            domain=self.domain,
            task=self.task,
            cost_usd=cost_usd,
            lessons_used=lessons_used,
        )
        self.token_count += rec.input_tokens + rec.output_tokens
        return self.record(
            "tool_call",
            f"llm:{operation}({model})",
            {
                "kind": "llm_call",
                "operation": rec.operation,
                "model": rec.model,
                "input_tokens": rec.input_tokens,
                "output_tokens": rec.output_tokens,
                "cache_read_tokens": rec.cache_read_tokens,
                "cost_usd": rec.cost_usd,
                "lessons_used": list(rec.lessons_used),
                "op_key": rec.op_key,
                "prompt": prompt,
                "response": response,
            },
        )

    def close(self, status: str = "complete") -> None:
        self.status = status
        self.updated_at = _utcnow()

    # ----- trace summary --------------------------------------------------- #

    def to_trace_summary(self) -> dict[str, Any]:
        """Build enriched trace summary data from ledger events.

        Returns a dict with ``tools_called``, ``files_touched``, and
        ``commands_run`` populated with full args, diffs, and output
        rather than just names/paths.
        """
        # --- Enriched tool calls ---
        tool_call_events = [e for e in self.events if e.kind == "tool_call"]
        merged_tools: dict[tuple[str, str], dict[str, Any]] = {}
        for ev in tool_call_events:
            p = ev.payload
            name = p.get("tool", "")
            sig = p.get("args_signature", "")
            key = (name, sig)
            if key not in merged_tools:
                merged_tools[key] = {
                    "name": name,
                    "args_hash": sig,
                    "count": 1,
                    "args": p.get("args"),
                    "result_summary": (p.get("output") or "")[:200],
                }
            else:
                merged_tools[key]["count"] += 1

        tools_called = list(merged_tools.values())

        # --- Enriched file records ---
        file_events = [e for e in self.events if e.kind in ("file_edit", "file_revert")]
        files_touched: list[dict[str, Any] | str] = []
        seen_paths: set[str] = set()
        for ev in file_events:
            p = ev.payload
            path = p.get("path", "")
            if not path or path in seen_paths:
                continue
            seen_paths.add(path)
            files_touched.append(
                {
                    "path": path,
                    "diff": (p.get("diff") or "")[:4096],
                    "event": p.get("event", "edit"),
                }
            )
        # Add any paths that only appear in self.files_touched (no diff event)
        for path in self.files_touched:
            if path not in seen_paths:
                files_touched.append(path)

        # --- Enriched command records ---
        cmd_events = [e for e in self.events if e.kind == "command_result"]
        commands_run: list[dict[str, Any] | str] = []
        seen_cmds: set[str] = set()
        for ev in cmd_events:
            cmd = ev.summary
            if cmd in seen_cmds:
                continue
            seen_cmds.add(cmd)
            p = ev.payload
            commands_run.append(
                {
                    "command": cmd,
                    "exit_code": 0 if p.get("ok") else 1,
                    "stdout": (p.get("stdout") or "")[:1024],
                    "stderr": (p.get("stderr") or "")[:1024],
                }
            )
        # Add any commands that only appear in self.commands_run (no result event)
        for cmd in self.commands_run:
            if cmd not in seen_cmds:
                commands_run.append(cmd)

        return {
            "tools_called": tools_called,
            "files_touched": files_touched,
            "commands_run": commands_run,
        }

    # ----- snapshot / persistence ----------------------------------------- #

    def snapshot(self) -> dict[str, Any]:
        tool_calls = [e for e in self.events if e.kind == "tool_call"]
        total_output = sum(int(e.payload.get("output_chars", 0)) for e in tool_calls)
        alerts = [e for e in self.events if e.kind == "watchdog_alert"]
        return {
            "run_id": self.run_id,
            "agent": self.agent,
            "task": self.task,
            "domain": self.domain,
            "status": self.status,
            "tool_call_count": len(tool_calls),
            "total_tool_output_chars": total_output,
            "alert_count": len(alerts),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "current_plan": list(self.current_plan),
            "files_touched": list(self.files_touched),
            "tools_called": list(self.tools_called),
            "commands_run": list(self.commands_run),
            "tests_run": list(self.tests_run),
            "errors_seen": list(self.errors_seen),
            "repeated_failures": list(self.repeated_failures),
            "hypotheses_tried": list(self.hypotheses_tried),
            "hypotheses_rejected": list(self.hypotheses_rejected),
            "verified_facts": list(self.verified_facts),
            "open_questions": list(self.open_questions),
            "active_reasonblocks": list(self.active_reasonblocks),
            "active_rubrics": list(self.active_rubrics),
            "current_blockers": list(self.current_blockers),
            "next_required_validation": self.next_required_validation,
            "token_count": self.token_count,
            "tool_count": self.tool_count,
            "budget": dict(self.budget),
            "cost": (self.cost_tracker.snapshot() if self.cost_tracker else {}),
            "events": [to_jsonable(e) for e in self.events],
        }

    def persist(self, root: Path | None = None) -> Path:
        target_root = root or self._root
        if target_root is None:
            raise ValueError("RunLedger.persist requires a root directory.")
        runs_dir = Path(target_root) / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = runs_dir / f"{self.run_id}.json"
        path.write_text(json.dumps(self.snapshot(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> RunLedger:
        snap: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
        led = cls(
            run_id=snap.get("run_id"),
            agent=snap.get("agent"),
            task=snap.get("task", "") or "",
            domain=snap.get("domain"),
        )
        led.status = snap.get("status", "running")
        for ev in snap.get("events", []):
            led.events.append(
                LedgerEvent(
                    kind=ev.get("kind"),
                    summary=ev.get("summary", ""),
                    payload=ev.get("payload", {}),
                )
            )
        led.current_plan = list(snap.get("current_plan") or [])
        led.files_touched = list(snap.get("files_touched") or [])
        led.tools_called = list(snap.get("tools_called") or [])
        led.commands_run = list(snap.get("commands_run") or [])
        led.tests_run = list(snap.get("tests_run") or [])
        led.errors_seen = list(snap.get("errors_seen") or [])
        led.repeated_failures = list(snap.get("repeated_failures") or [])
        led.hypotheses_tried = list(snap.get("hypotheses_tried") or [])
        led.hypotheses_rejected = list(snap.get("hypotheses_rejected") or [])
        led.verified_facts = list(snap.get("verified_facts") or [])
        led.open_questions = list(snap.get("open_questions") or [])
        led.active_reasonblocks = list(snap.get("active_reasonblocks") or [])
        led.active_rubrics = list(snap.get("active_rubrics") or [])
        led.current_blockers = list(snap.get("current_blockers") or [])
        led.next_required_validation = snap.get("next_required_validation")
        led.token_count = int(snap.get("token_count") or 0)
        led.tool_count = int(snap.get("tool_count") or 0)
        led.budget = dict(snap.get("budget") or {})
        return led
