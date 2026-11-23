"""Explicit fresh re-run of Review Intelligence (Phase 11 item 15b).

A fresh re-run is a reviewer/admin action that executes AI review again on the
*unchanged* Snapshot. It never fakes a Snapshot or input change: the run key
stays the same and the store inserts a new run row for it
(``claim_intelligence_run(bypass_cache=True)``). Each call is reserved against
the normal Review-wide budget. The newest ready row then serves every later
ordinary request, so reloads and identical requests keep reusing cache.

Idempotency is the action id: a replayed fresh action reuses the row its first
delivery produced (``prior_fresh_run``) instead of spending again.

Run details distinguish the three execution modes from the durable rows:
``fresh_rerun`` (an explicit re-run executed), ``executed`` (an ordinary run
that called a provider) and ``reused`` (served from an earlier row).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

ExecutionMode = Literal["standard", "fresh_rerun"]
STANDARD: Final[ExecutionMode] = "standard"
FRESH_RERUN: Final[ExecutionMode] = "fresh_rerun"
MAX_ACTION_ID_CHARS: Final[int] = 128
_ACTION_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def valid_action_id(value: object) -> str:
    """The action id when it is a bounded opaque token, else ``""``."""

    text = str(value or "").strip() if isinstance(value, str) else ""
    return text if _ACTION_ID_RE.match(text) else ""


def usage_document(row: Any) -> Mapping[str, Any]:
    try:
        value = json.loads(str(getattr(row, "usage_json", "") or "{}"))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}


def run_execution(row: Any) -> dict[str, str]:
    """``{"mode", "action_id"}`` persisted on one run row (``standard`` when absent)."""

    raw = usage_document(row).get("execution")
    if not isinstance(raw, Mapping):
        return {"mode": STANDARD, "action_id": ""}
    mode = FRESH_RERUN if raw.get("mode") == FRESH_RERUN else STANDARD
    return {"mode": mode, "action_id": valid_action_id(raw.get("action_id")) if mode == FRESH_RERUN else ""}


def execution_document(action_id: str) -> dict[str, str]:
    return {"mode": FRESH_RERUN, "action_id": action_id} if action_id else {"mode": STANDARD}


def prior_fresh_run(
    store: Any,
    *,
    review_id: str,
    revision_id: str,
    capability: str,
    run_key: str,
    action_id: str,
) -> Any | None:
    """The settled row an earlier delivery of this fresh action produced for *run_key*."""

    if not action_id:
        return None
    for row in store.list_intelligence_runs(review_id, revision_id=revision_id, capability=capability):
        if row.run_key != run_key or row.status != "ready":
            continue
        execution = run_execution(row)
        if execution["mode"] == FRESH_RERUN and execution["action_id"] == action_id:
            return row
    return None


def execution_summary(rows: Sequence[Any]) -> dict[str, Any]:
    """How the AI review rows of one Snapshot were produced, for Run details.

    ``latest_mode`` is the newest settled row's mode: ``fresh_rerun`` or
    ``executed``. Reuse is a property of a request, not of a row; the
    projection reports it from the request result (``cache_reuse``).
    """

    settled = [row for row in rows if row.status in {"ready", "failed", "cancelled"}]
    fresh = [row for row in settled if run_execution(row)["mode"] == FRESH_RERUN]
    latest = settled[0] if settled else None
    return {
        "latest_mode": (
            "" if latest is None else (FRESH_RERUN if run_execution(latest)["mode"] == FRESH_RERUN else "executed")
        ),
        "fresh_reruns": len({run_execution(row)["action_id"] for row in fresh}),
        "fresh_rerun_runs": len(fresh),
        "latest_fresh_at": str(fresh[0].completed_at or fresh[0].created_at) if fresh else "",
        "runs": len(settled),
    }


AI_REVIEW_ENTRIES: Final[tuple[str, ...]] = ("ai.investigators", "ai.hypotheses", "ai.rule_checks")
NO_AI_REASON: Final[str] = "the effective run plan runs no AI review on this revision"
_LOCUS_OF: Final[Mapping[str, str]] = {"local": "local", "managed": "managed", "worker": "customer"}
_METERING_OF: Final[Mapping[str, str]] = {
    "local": "customer_provider",
    "managed": "lemoncrow_managed",
    "worker": "customer_provider",
}


def fresh_rerun_preview(
    plan: Any,
    *,
    calls_used: int,
    max_calls: int,
    provider: str,
    model_label: str,
    model_data_locus: str,
    available: bool,
    reason: str = "",
) -> dict[str, Any]:
    """What a fresh re-run would execute and spend, shown at the action boundary (8D).

    Execution, metering and cost class come from the Snapshot's persisted plan,
    the same plan the re-run executes; the model-call estimate is the plan's
    own per-entry estimate for every AI review entry that runs.
    """

    from lemoncrow.pro.capabilities.review.capability_catalog import COST_RANK

    rows = [plan.entry(entry_id) for entry_id in AI_REVIEW_ENTRIES]
    running = [row for row in rows if row is not None and row.status == "run"]
    lead = running[0] if running else None
    calls = sum(int(row.estimated_model_calls) for row in running)
    remaining = max(0, int(max_calls) - int(calls_used))
    cost_class = max((str(row.cost_class) for row in running), key=lambda value: COST_RANK.get(value, -1), default="")
    runs_ai = lead is not None
    return {
        "available": bool(available and runs_ai),
        "reason": "" if available and runs_ai else (reason if not available and reason else NO_AI_REASON),
        "effort": str(lead.depth) if lead is not None else "",
        "entries": [row.id for row in running],
        "estimated_model_calls": calls,
        "budget": {"calls_used": int(calls_used), "max_calls": int(max_calls), "remaining": remaining},
        "within_budget": calls <= remaining,
        "cost_class": cost_class,
        "execution_locus": _LOCUS_OF.get(str(lead.executor), "") if lead is not None else "",
        "metering_owner": _METERING_OF.get(str(lead.executor), "") if lead is not None else "",
        # Managed execution uses the managed model-data locus; a customer worker reports its own.
        "model_data_locus": (
            {"managed": "managed", "worker": "unknown"}.get(str(lead.executor), model_data_locus)
            if lead is not None
            else model_data_locus
        ),
        "provider": provider,
        "model": model_label,
        "reuses_cache": False,
    }


__all__ = [
    "AI_REVIEW_ENTRIES",
    "FRESH_RERUN",
    "MAX_ACTION_ID_CHARS",
    "STANDARD",
    "ExecutionMode",
    "execution_document",
    "execution_summary",
    "fresh_rerun_preview",
    "prior_fresh_run",
    "run_execution",
    "usage_document",
    "valid_action_id",
]
