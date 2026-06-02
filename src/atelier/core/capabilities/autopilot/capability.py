"""Autopilot capability orchestrator (M5).

Pure and host-agnostic: it takes injected provider callables and returns an
:class:`AutopilotAction`. It performs the token-budget and dedup guards and is
fully fail-open. Telemetry/IO (emitting the action, recording to the ledger) is
the hook's job, keeping this layer testable.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from typing import Any

from atelier.core.capabilities.prompt_compilation.tokens import estimate_tokens
from atelier.core.capabilities.verification import RetryBudget

from .models import AutopilotAction, AutopilotConfig, AutopilotEvent
from .policy import select_behavior, should_inject_for_prompt

# Provider callable types (duck-typed return objects keep this decoupled).
RecallFn = Callable[[str], list[str]]
LessonsFn = Callable[[], list[str]]
ScopedPullFn = Callable[[str, list[str]], Any]  # (prompt, files) -> object with .chunks
VerifyFn = Callable[[list[str]], list[Any]]  # (files) -> objects with .to_prompt_block()


class AutopilotCapability:
    def __init__(
        self,
        config: AutopilotConfig,
        *,
        recall_fn: RecallFn | None = None,
        lessons_fn: LessonsFn | None = None,
        scoped_pull_fn: ScopedPullFn | None = None,
        verify_fn: VerifyFn | None = None,
        retry_budget: RetryBudget | None = None,
    ) -> None:
        self.config = config
        self._recall_fn = recall_fn
        self._lessons_fn = lessons_fn
        self._scoped_pull_fn = scoped_pull_fn
        self._verify_fn = verify_fn
        self._retry_budget = retry_budget or RetryBudget()
        self._seen: set[str] = set()

    def on_event(self, event: AutopilotEvent) -> AutopilotAction:
        if not self.config.enabled:
            return AutopilotAction.noop("disabled")
        try:
            behavior = select_behavior(event.trigger, self.config)
            if behavior is None:
                return AutopilotAction.noop("no_behavior")
            if behavior == "session_warm":
                return self._session_warm(event)
            if behavior == "scoped_inject":
                return self._scoped_inject(event)
            if behavior == "counterexamples":
                return self._counterexamples(event)
            return AutopilotAction.noop("no_behavior")
        except Exception:
            logging.exception("Recovered from broad exception handler")
            return AutopilotAction.noop("error")  # fail-open: never block the agent

    # -- behaviors ---------------------------------------------------------

    def _session_warm(self, event: AutopilotEvent) -> AutopilotAction:
        parts: list[str] = []
        if self._lessons_fn is not None:
            parts.extend(self._lessons_fn())
        if self._recall_fn is not None:
            query = str(event.payload.get("repo") or event.payload.get("cwd") or "")
            parts.extend(self._recall_fn(query))
        parts = [p for p in parts if p and p.strip()]
        if not parts:
            return AutopilotAction.noop("no_providers", "session_warm")
        content = "Relevant prior context (Atelier autopilot):\n" + "\n".join(f"- {p}" for p in parts)
        return self._emit("session_warm", content)

    def _scoped_inject(self, event: AutopilotEvent) -> AutopilotAction:
        advisory = self._workflow_advisory(event)
        if not bool(event.payload.get("workflow_share_context", True)):
            if advisory:
                return self._emit("scoped_inject", advisory)
            return AutopilotAction.noop("workflow_context_disabled", "scoped_inject")
        if self._scoped_pull_fn is None:
            if advisory:
                return self._emit("scoped_inject", advisory)
            return AutopilotAction.noop("no_provider", "scoped_inject")
        prompt = str(event.payload.get("prompt") or "")
        if not prompt.strip():
            if advisory:
                return self._emit("scoped_inject", advisory)
            return AutopilotAction.noop("empty_prompt", "scoped_inject")
        if not should_inject_for_prompt(prompt):
            if advisory:
                return self._emit("scoped_inject", advisory)
            return AutopilotAction.noop("not_coding_prompt", "scoped_inject")
        files = list(event.payload.get("files") or [])
        scoped = self._scoped_pull_fn(prompt, files)
        chunks = list(getattr(scoped, "chunks", []) or [])
        if not chunks:
            if advisory:
                return self._emit("scoped_inject", advisory)
            return AutopilotAction.noop("no_chunks", "scoped_inject")
        # Keep it small + scoped: only the top-K most-relevant chunks (the pull
        # returns them ranked). Avoids the "lost in the middle" context dump.
        top = chunks[: self.config.max_inject_chunks]
        lines = [f"- {getattr(c, 'symbol', '') or getattr(c, 'path', '')} ({getattr(c, 'path', '')})" for c in top]
        body = "Scoped context for this request (Atelier autopilot):\n" + "\n".join(lines)
        content = f"{advisory}\n{body}" if advisory else body
        return self._emit("scoped_inject", content)

    def _counterexamples(self, event: AutopilotEvent) -> AutopilotAction:
        if self._verify_fn is None:
            return AutopilotAction.noop("no_provider", "counterexamples")
        files = list(event.payload.get("touched_files") or [])
        if not files:
            return AutopilotAction.noop("no_files", "counterexamples")
        counterexamples = self._verify_fn(files)
        if not counterexamples:
            self._retry_budget.reset()
            self._seen.clear()
            return AutopilotAction.noop("clean", "counterexamples")
        signature = self._counterexample_signature(counterexamples)
        attempt = self._retry_budget.consume(signature)
        if self._retry_budget.exhausted(signature):
            content = self._rescue_guidance(counterexamples, attempt)
            return self._emit("counterexamples", content)
        body = "\n".join(c.to_prompt_block() for c in counterexamples)
        remaining = self._retry_budget.remaining(signature)
        header = (
            "Verification found issues to fix before continuing (Atelier autopilot):\n"
            f"retry budget: {attempt}/{self._retry_budget.max_attempts} "
            f"for this counterexample set ({remaining} remaining)\n"
        )
        content = header + body
        return self._emit("counterexamples", content)

    # -- guards ------------------------------------------------------------

    def _emit(self, behavior: str, content: str) -> AutopilotAction:
        content = content.strip()
        if not content:
            return AutopilotAction.noop("empty", behavior)
        budget = self.config.max_inject_tokens
        if estimate_tokens(content) > budget:
            content = self._truncate_to_budget(content, budget)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if digest in self._seen:
            return AutopilotAction.noop("deduped", behavior)
        self._seen.add(digest)
        return AutopilotAction(
            kind="inject",
            behavior=behavior,
            content=content,
            injected_tokens=estimate_tokens(content),
        )

    @staticmethod
    def _workflow_advisory(event: AutopilotEvent) -> str:
        if not bool(event.payload.get("workflow_vote_advisory")):
            return ""
        step = str(event.payload.get("workflow_step") or "planning").strip() or "planning"
        return (
            "Workflow note (Atelier autopilot): "
            f"{step} is marked critical for this session; converge on the plan before moving deeper into edits."
        )

    @staticmethod
    def _truncate_to_budget(content: str, budget_tokens: int) -> str:
        lines = content.splitlines()
        out: list[str] = []
        for line in lines:
            candidate = "\n".join([*out, line])
            if estimate_tokens(candidate) > budget_tokens:
                break
            out.append(line)
        result = "\n".join(out)
        return result if result else lines[0][: budget_tokens * 4]

    @staticmethod
    def _counterexample_signature(counterexamples: list[Any]) -> str:
        payloads: list[str] = []
        for item in counterexamples:
            if hasattr(item, "to_dict"):
                payload = item.to_dict()
            else:
                payload = {
                    "check": getattr(item, "check", ""),
                    "severity": getattr(item, "severity", ""),
                    "file_path": getattr(item, "file_path", ""),
                    "line": getattr(item, "line", None),
                    "diagnostic": getattr(item, "diagnostic", ""),
                    "expected": getattr(item, "expected", None),
                    "actual": getattr(item, "actual", None),
                    "repro_command": getattr(item, "repro_command", None),
                }
            payloads.append(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        joined = "\n".join(sorted(payloads))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def _rescue_guidance(self, counterexamples: list[Any], attempt: int) -> str:
        steps: list[str] = []
        commands: list[str] = []
        for item in counterexamples:
            diagnostic = str(getattr(item, "diagnostic", "") or "").strip()
            location = str(getattr(item, "file_path", "") or "").strip()
            if location and diagnostic:
                steps.append(f"- {location}: {diagnostic}")
            elif diagnostic:
                steps.append(f"- {diagnostic}")
            repro = str(getattr(item, "repro_command", "") or "").strip()
            if repro and repro not in commands:
                commands.append(repro)
        procedure = [
            "- Stop retrying the same edit pattern; change the approach before continuing.",
            "- Reproduce the failure with the narrowest command below.",
            "- Inspect the minimal failing file or symbol before editing again.",
        ]
        if commands:
            procedure.extend(f"- repro: {command}" for command in commands[:3])
        details = "\n".join(steps[:5])
        suffix = f"\nRepeated issues:\n{details}" if details else ""
        return (
            "Verification hit the retry budget for the same counterexample set "
            f"(attempt {attempt}/{self._retry_budget.max_attempts}).\n"
            "Switch to rescue-style debugging before another edit:\n" + "\n".join(procedure) + suffix
        )
