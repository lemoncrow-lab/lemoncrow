"""Transport-neutral per-call savings accounting for LemonCrow tools."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from lemoncrow.gateway.tools.state import tool_call_counterfactual, tool_call_tokens_saved

_LOG = logging.getLogger(__name__)
_pending_hint = True


@dataclass(frozen=True, slots=True)
class ToolAccountingHooks:
    state_lock: Any
    read_workspace_state: Any
    write_workspace_state: Any
    session_id_getter: Any
    append_savings: Any


def process_tool_accounting(
    name: str,
    args: dict[str, Any],
    result: Any,
    rid: Any,
    *,
    hooks: ToolAccountingHooks,
) -> None:
    """Apply honest per-call savings corrections without transport coupling."""
    global _pending_hint
    code_intel_on = os.environ.get("LEMONCROW_CODE_INTEL_CREDIT", "1") != "0"
    read_dedup_on = os.environ.get("LEMONCROW_READ_BASELINE_DEDUP", "1") != "0"
    if not code_intel_on and not read_dedup_on:
        return
    try:
        from lemoncrow.core.capabilities import code_intel_credit, read_baseline_credit
        from lemoncrow.pro.capabilities import context_dedup

        is_read = name == "read"
        is_code_intel = code_intel_on and name in code_intel_credit.CODE_INTEL_TOOLS
        counterfactual = getattr(tool_call_counterfactual, "value", None) if read_dedup_on else None
        if not is_read and not is_code_intel and counterfactual is None and not _pending_hint:
            return

        result_errored = isinstance(result, dict) and bool(result.get("error"))

        with hooks.state_lock:
            state = hooks.read_workspace_state()
            epoch = context_dedup.current_epoch()
            current_sid = str(state.get("session_id") or "").strip() or hooks.session_id_getter()
            epoch_changed = state.get("code_intel_epoch") != epoch
            session_changed = bool(current_sid) and state.get("code_intel_session_id") != current_sid
            if epoch_changed or session_changed:
                state = code_intel_credit.reset_pending(state)
                state = read_baseline_credit.reset(state)
                state["code_intel_epoch"] = epoch
                if current_sid:
                    state["code_intel_session_id"] = current_sid

            if is_read:
                if result_errored:
                    tool_call_tokens_saved.value = 0
                    if isinstance(result, dict):
                        result.pop("tokens_saved", None)
                elif read_dedup_on and isinstance(result, dict):
                    files_out = result.get("files")
                    if isinstance(files_out, list):
                        surviving = 0
                        for entry in files_out:
                            if not isinstance(entry, dict) or entry.get("error"):
                                continue
                            state, credit = read_baseline_credit.should_credit(
                                state, entry.get("path"), entry.get("mode")
                            )
                            if not credit:
                                entry.pop("tokens_saved", None)
                            else:
                                surviving += int(entry.get("tokens_saved", 0) or 0)
                        tool_call_tokens_saved.value = surviving
                    else:
                        mode = result.get("mode")
                        path = result.get("path") or args.get("path")
                        state, credit = read_baseline_credit.should_credit(state, path, mode)
                        if not credit:
                            tool_call_tokens_saved.value = 0
                            result.pop("tokens_saved", None)
                if code_intel_on and not result_errored:
                    read_paths: list[str] = []
                    single = args.get("path")
                    if isinstance(single, str) and single:
                        read_paths.append(single)
                    files = args.get("files")
                    if isinstance(files, list):
                        for entry in files:
                            if isinstance(entry, str) and entry:
                                read_paths.append(entry)
                            elif isinstance(entry, dict):
                                path = entry.get("path")
                                if isinstance(path, str) and path:
                                    read_paths.append(path)
                    state = code_intel_credit.consume_reads(state, read_paths)
            elif is_code_intel and isinstance(result, dict) and not result_errored:
                paths = code_intel_credit.extract_credited_paths(name, result)
                state = code_intel_credit.record_pending(state, name, paths)

            if counterfactual and not result_errored and isinstance(result, dict):
                surviving_chars = 0
                for path, chars in dict(counterfactual.get("per_file_chars") or {}).items():
                    state, credit = read_baseline_credit.should_credit_path(state, path)
                    if credit:
                        surviving_chars += int(chars)
                netted = max(0, surviving_chars - int(counterfactual.get("returned_chars") or 0)) // 4
                final = max(int(counterfactual.get("floor_tokens") or 0), netted)
                if final > 0:
                    result["tokens_saved"] = final
                else:
                    result.pop("tokens_saved", None)
                tool_call_tokens_saved.value = final

            pending_credits: list[dict[str, Any]] = []
            if code_intel_on:
                threshold = int(os.environ.get("LEMONCROW_CODE_INTEL_CREDIT_AGE", "8"))
                state, credits = code_intel_credit.tick_and_credit(state, threshold=threshold)
                pending_credits = list(credits)

            hooks.write_workspace_state(state)
            _pending_hint = bool(state.get("code_intel_pending"))

        for entry in pending_credits:
            hooks.append_savings(entry["tool"], 0, 1, rid=str(rid))
    except Exception:
        _LOG.exception("Recovered from broad exception handler")


__all__ = ["ToolAccountingHooks", "process_tool_accounting"]
