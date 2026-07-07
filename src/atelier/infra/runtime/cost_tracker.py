"""Per-call cost tracking and savings-delta computation.

Records every LLM call performed during an agent run with model, token
counts, USD cost, and the lessons (Playbooks) injected into the prompt.

Persists two artifacts under the atelier store root:

  * ``runs/<session_id>.json``             — already written by RunLedger; the
                                          tracker also appends a ``calls``
                                          list and ``total_cost_usd`` field.
  * ``cost_history.json``              — per-operation rolling history keyed
                                          by ``operation_key`` (a stable hash
                                          of ``domain + normalized_task``).

Savings model (fully deterministic):

    savings_usd = baseline_cost_usd - current_cost_usd
    savings_pct = savings_usd / baseline_cost_usd * 100

where ``baseline_cost_usd`` is the *first ever recorded cost* for the same
operation_key (so subsequent runs that benefit from injected lessons can
demonstrate compounding savings).  Per-call delta uses the *previous* call
of the same op_key as a reference (``last_cost - new_cost``) — exactly what
the user asked for.

Pricing resolution is LiteLLM-backed + overridable so we can stay aligned
with one catalog while still forcing deterministic values in tests.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Pricing                                                                     #
# --------------------------------------------------------------------------- #
# Model pricing now comes from the shared LiteLLM-backed pricing module.
# The dict below is kept only for backward-compatibility with any code that
# imports MODEL_PRICING directly.
from atelier.core.capabilities.pricing import get_model_pricing as _get_model_pricing


# Backward-compat shim — behaves like the old dict for code that does
# ``MODEL_PRICING.get(model)`` but always reads from the TOML config.
class _PricingProxy(dict):  # type: ignore[type-arg]
    """Dict-like proxy that falls through to the shared pricing table."""

    def get(self, model: str, default: Any = None) -> Any:
        p = _get_model_pricing(model)
        return {"input": p.input, "output": p.output, "cache_read": p.cache_read}


MODEL_PRICING = _PricingProxy()


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    thinking_tokens: int = 0,
) -> float:
    """Compute USD cost using the shared LiteLLM-backed pricing table.

    Unknown models fall back to the zero-cost default until LiteLLM or a test
    override provides pricing data for them.
    """
    return _get_model_pricing(model).cost_usd(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        thinking_tokens=thinking_tokens,
    )


# --------------------------------------------------------------------------- #
# Operation-key normalization                                                 #
# --------------------------------------------------------------------------- #

_WS_RE = re.compile(r"\s+")
_NUM_RE = re.compile(r"\b\d+\b")


def operation_key(domain: str | None, task: str) -> str:
    """Stable key for "the same operation".

    Heuristic: lowercase + collapse whitespace + replace numerals with ``N``
    so that e.g. "fix product 12" and "fix product 7" are clustered.
    """
    norm = _WS_RE.sub(" ", (task or "").strip().lower())
    norm = _NUM_RE.sub("N", norm)
    payload = f"{(domain or '-')}::{norm}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Per-call record                                                             #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CallRecord:
    """One LLM call inside a run."""

    operation: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    thinking_tokens: int = 0
    cost_usd: float = 0.0
    lessons_used: list[str] = field(default_factory=list)
    op_key: str = ""
    at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "thinking_tokens": self.thinking_tokens,
            "cost_usd": self.cost_usd,
            "lessons_used": self.lessons_used,
            "op_key": self.op_key,
            "at": self.at,
        }


# --------------------------------------------------------------------------- #
# Cost history file                                                           #
# --------------------------------------------------------------------------- #


def _history_path(root: Path) -> Path:
    return Path(root) / "cost_history.json"


def load_cost_history(root: Path) -> dict[str, Any]:
    p = _history_path(root)
    if not p.exists():
        return {"operations": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return {"operations": {}}
    except (OSError, json.JSONDecodeError):
        return {"operations": {}}


def save_cost_history(root: Path, history: dict[str, Any]) -> None:
    p = _history_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(history, indent=2, sort_keys=True), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Tracker                                                                     #
# --------------------------------------------------------------------------- #


class CostTracker:
    """Records per-call costs and computes savings deltas.

    A tracker is bound to one ``store_root`` (where ``cost_history.json``
    lives) and accumulates ``CallRecord`` instances for the *current* run.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.calls: list[CallRecord] = []

    # ----- recording ------------------------------------------------------ #

    def record_call(
        self,
        *,
        operation: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        thinking_tokens: int = 0,
        domain: str | None = None,
        task: str = "",
        cost_usd: float | None = None,
        lessons_used: list[str] | None = None,
        at: str | None = None,
    ) -> CallRecord:
        op_key = operation_key(domain, task or operation)
        cost = (
            cost_usd
            if cost_usd is not None
            else estimate_cost(
                model,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_write_tokens,
                thinking_tokens,
            )
        )
        rec = CallRecord(
            operation=operation,
            model=model,
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            cache_read_tokens=int(cache_read_tokens),
            cache_write_tokens=int(cache_write_tokens),
            thinking_tokens=int(thinking_tokens),
            cost_usd=cost,
            lessons_used=list(lessons_used or []),
            op_key=op_key,
            at=at or datetime.now(UTC).isoformat(),
        )
        self.calls.append(rec)
        # Persist to history immediately so concurrent runs see updates.
        self._append_history(rec, domain=domain, task=task)
        return rec

    # ----- history -------------------------------------------------------- #

    def _append_history(
        self,
        rec: CallRecord,
        *,
        domain: str | None,
        task: str,
    ) -> None:
        history = load_cost_history(self.root)
        ops = history.setdefault("operations", {})
        entry = ops.setdefault(
            rec.op_key,
            {
                "domain": domain or "-",
                "task_sample": task or rec.operation,
                "first_seen": rec.at,
                "calls": [],
            },
        )
        entry["calls"].append(rec.to_dict())
        save_cost_history(self.root, history)

    # ----- savings -------------------------------------------------------- #

    def savings_for(self, op_key: str) -> dict[str, Any]:
        """Compute savings vs. (a) prior call of same op and (b) baseline.

        Returns a dict with:
          baseline_cost_usd  — first ever cost recorded for this op
          last_cost_usd      — most recent cost prior to the latest call
          current_cost_usd   — most recent cost
          delta_vs_last_usd  — last - current   (the user's central question)
          delta_vs_base_usd  — baseline - current
          pct_vs_base        — savings % vs baseline
          calls_count        — total recorded calls for this op
        """
        history = load_cost_history(self.root)
        entry = history.get("operations", {}).get(op_key)
        if not entry or not entry.get("calls"):
            return {
                "op_key": op_key,
                "baseline_cost_usd": 0.0,
                "last_cost_usd": 0.0,
                "current_cost_usd": 0.0,
                "delta_vs_last_usd": 0.0,
                "delta_vs_base_usd": 0.0,
                "pct_vs_base": 0.0,
                "calls_count": 0,
            }
        calls = entry["calls"]
        baseline = float(calls[0].get("cost_usd", 0.0))
        current = float(calls[-1].get("cost_usd", 0.0))
        last = float(calls[-2].get("cost_usd", current)) if len(calls) >= 2 else current
        pct_base = ((baseline - current) / baseline * 100.0) if baseline > 0 else 0.0
        return {
            "op_key": op_key,
            "domain": entry.get("domain"),
            "task_sample": entry.get("task_sample"),
            "baseline_cost_usd": round(baseline, 6),
            "last_cost_usd": round(last, 6),
            "current_cost_usd": round(current, 6),
            "delta_vs_last_usd": round(last - current, 6),
            "delta_vs_base_usd": round(baseline - current, 6),
            "pct_vs_base": round(pct_base, 2),
            "calls_count": len(calls),
        }

    def total_savings(self, since: datetime | None = None) -> dict[str, Any]:
        """Aggregate savings across every operation key in history."""
        history = load_cost_history(self.root)
        ops = history.get("operations", {}) or {}
        total_baseline = 0.0
        total_current = 0.0
        total_calls = 0
        per_op: list[dict[str, Any]] = []

        since_iso = since.isoformat() if since else None

        for op_key, entry in ops.items():
            if not isinstance(entry, dict):
                continue
            calls = entry.get("calls") or []
            if not calls:
                continue

            filtered_calls = calls
            if since_iso:
                filtered_calls = [c for c in calls if (c.get("at") or "") >= since_iso]

            if not filtered_calls:
                continue

            baseline = float(calls[0].get("cost_usd", 0.0) or 0.0)
            actual_total = sum(float(c.get("cost_usd", 0.0) or 0.0) for c in filtered_calls)
            total_baseline += baseline * len(filtered_calls)
            total_current += actual_total
            total_calls += len(filtered_calls)

            # Note: savings_for still looks at latest call, but here we only
            # include ops that had activity since 'since'.
            s = self.savings_for(op_key)
            per_op.append(s)

        delta = max(0.0, total_baseline - total_current)
        pct = (delta / total_baseline * 100.0) if total_baseline > 0 else 0.0
        return {
            "operations_tracked": len(ops),
            "total_calls": total_calls,
            "would_have_cost_usd": round(total_baseline, 6),
            "actually_cost_usd": round(total_current, 6),
            "saved_usd": round(delta, 6),
            "saved_pct": round(pct, 2),
            "per_operation": sorted(per_op, key=lambda x: -x["delta_vs_base_usd"]),
        }

    # ----- per-tool breakdown -------------------------------------------- #

    @staticmethod
    def per_tool_cost_breakdown(
        events: list[dict[str, Any]],
    ) -> list[tuple[str, int, float]]:
        """Estimate cost per MCP tool from ``model_recommendation`` ledger events.

        Returns a list of ``(tool_name, call_count, estimated_cost_usd)`` tuples
        sorted by cost descending.  Cost is estimated as::

            estimated_input_tokens * recommended_model_input_price
        """
        from atelier.core.capabilities.pricing import get_model_pricing

        buckets: dict[str, tuple[int, float]] = {}
        for ev in events:
            if ev.get("kind") != "model_recommendation":
                continue
            payload = ev.get("payload") or {}
            tool = str(payload.get("tool_name") or "")
            model = str(payload.get("model") or "claude-haiku-4-5")
            tokens = int(payload.get("estimated_input_tokens") or 0)
            cost = get_model_pricing(model).cost_usd(input_tokens=tokens)
            count, total = buckets.get(tool, (0, 0.0))
            buckets[tool] = (count + 1, total + cost)

        return sorted(
            [(t, c, round(v, 6)) for t, (c, v) in buckets.items()],
            key=lambda x: -x[2],
        )

    # ----- ledger snapshot helpers --------------------------------------- #

    def snapshot(self) -> dict[str, Any]:
        return {
            "calls": [c.to_dict() for c in self.calls],
            "total_cost_usd": round(sum(c.cost_usd for c in self.calls), 6),
            "total_input_tokens": sum(c.input_tokens for c in self.calls),
            "total_output_tokens": sum(c.output_tokens for c in self.calls),
            "total_cache_read_tokens": sum(c.cache_read_tokens for c in self.calls),
            "lessons_used_unique": sorted({lid for c in self.calls for lid in c.lessons_used}),
        }
