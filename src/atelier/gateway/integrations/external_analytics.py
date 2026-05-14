"""Optional upstream analytics bridges.

This module lets Atelier execute external OSS analyzers as sidecars instead of
copying their internals into the runtime.

Design constraints:
- prefer user-installed binaries over vendoring source trees
- fail open when a tool is not installed or returns invalid output
- keep licensing posture explicit for non-MIT tools
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ExternalAnalyzerSpec:
    id: str
    display_name: str
    license_name: str
    execution_mode: str
    install_hint: str
    update_strategy: str
    env_var: str | None = None
    executable_names: tuple[str, ...] = ()
    reportable: bool = False
    supports_periods: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


TOKSCALE = ExternalAnalyzerSpec(
    id="tokscale",
    display_name="Tokscale",
    license_name="MIT",
    execution_mode="installed_cli",
    install_hint="Install the tokscale CLI, then expose it on PATH or set ATELIER_TOKSCALE_BIN.",
    update_strategy="Pin a tested CLI version and upgrade it independently from Atelier.",
    env_var="ATELIER_TOKSCALE_BIN",
    executable_names=("tokscale",),
    reportable=True,
    supports_periods=("today", "week", "month"),
    notes=(
        "Best used for provider-aware pricing, model usage, daily and hourly usage analytics.",
        "Not a runtime optimizer; treat it as a reporting sidecar.",
    ),
)

CODEBURN = ExternalAnalyzerSpec(
    id="codeburn",
    display_name="CodeBurn",
    license_name="MIT",
    execution_mode="installed_cli",
    install_hint="Install the codeburn CLI, then expose it on PATH or set ATELIER_CODEBURN_BIN.",
    update_strategy="Pin a tested CLI version and upgrade it independently from Atelier.",
    env_var="ATELIER_CODEBURN_BIN",
    executable_names=("codeburn",),
    reportable=True,
    supports_periods=("today", "week", "month", "30days", "all"),
    notes=(
        "Best used for post-hoc efficiency analytics, one-shot rate, compare, and waste detection.",
        "Its optimizer is session analytics, not an in-loop runtime policy engine.",
    ),
)

SPECS: tuple[ExternalAnalyzerSpec, ...] = (TOKSCALE, CODEBURN)
REPORTABLE_TOOL_IDS: tuple[str, ...] = tuple(spec.id for spec in SPECS if spec.reportable)

_SUMMARY_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "cost_usd": (
        "cost",
        "cost_usd",
        "total_cost",
        "total_usd",
        "totalCost",
        "usd_cost",
        "estimated_cost",
    ),
    "calls": ("calls", "tool_calls", "call_count", "requests", "total_calls"),
    "sessions": ("sessions", "session_count", "runs", "total_sessions"),
    "input_tokens": ("input_tokens", "prompt_tokens", "total_input_tokens", "totalInput"),
    "output_tokens": (
        "output_tokens",
        "completion_tokens",
        "total_output_tokens",
        "totalOutput",
    ),
    "tokens": ("tokens", "total_tokens", "totalTokens"),
    "saved_tokens": ("saved_tokens", "waste_tokens", "redundant_tokens", "estimated_tokens_saved"),
    "savings_usd": ("savings_usd", "saved_usd", "estimated_usd_saved"),
    "one_shot_rate": ("one_shot_rate", "success_rate", "yield", "yield_rate"),
}


def _find_executable(spec: ExternalAnalyzerSpec) -> str | None:
    if spec.env_var:
        explicit = os.environ.get(spec.env_var, "").strip()
        if explicit:
            path = Path(explicit).expanduser()
            if path.exists():
                return str(path)
    for name in spec.executable_names:
        resolved = shutil.which(name)
        if resolved:
            return resolved
    return None


def _status_entry(spec: ExternalAnalyzerSpec, *, cwd: Path | None = None) -> dict[str, Any]:
    detected = _find_executable(spec)
    available = bool(detected)
    return {
        "tool": spec.id,
        "display_name": spec.display_name,
        "available": available,
        "path": detected,
        "license": spec.license_name,
        "execution_mode": spec.execution_mode,
        "reportable": spec.reportable,
        "install_hint": spec.install_hint,
        "update_strategy": spec.update_strategy,
        "notes": list(spec.notes),
        "recommended_integration": "pinned_sidecar_cli",
        "cwd": str(cwd or Path.cwd()),
    }


def external_status(*, cwd: Path | None = None) -> list[dict[str, Any]]:
    return [_status_entry(spec, cwd=cwd) for spec in SPECS]


def _find_number(mapping: dict[str, Any], aliases: tuple[str, ...]) -> int | float | None:
    for alias in aliases:
        value = mapping.get(alias)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return value
    return None


def _parse_tokens(raw: str) -> int:
    """Parse strings like '746.5M' or '5.1K' into integers."""
    if not raw:
        return 0
    raw = raw.strip().upper().replace("~", "").replace(",", "")
    if raw.endswith("B"):
        return int(float(raw[:-1]) * 1_000_000_000)
    if raw.endswith("M"):
        return int(float(raw[:-1]) * 1_000_000)
    if raw.endswith("K"):
        return int(float(raw[:-1]) * 1_000)
    try:
        return int(float(raw))
    except ValueError:
        return 0


def _parse_usd(raw: str) -> float:
    """Parse strings like '$98.43' into floats."""
    if not raw:
        return 0.0
    raw = raw.strip().replace("$", "").replace("~", "").replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return 0.0


def parse_codeburn_optimize_output(text: str) -> dict[str, Any]:
    """Parse the text output of `codeburn optimize` into a structured dict.

    This is necessary because codeburn optimize does not currently support --json.
    """
    lines = text.split("\n")
    payload: dict[str, Any] = {
        "kind": "optimization_report",
        "overview": {},
        "recommendations": [],
    }

    # Extract overview numbers
    # Example: 182 sessions   9,643 calls   $653.16   Health: F (20/100, 7 issues)
    overview_pattern = re.compile(
        r"(\d+,?\d*)\s+sessions\s+(\d+,?\d*)\s+calls\s+\$(\d+\.?\d*)\s+Health:\s+([A-F])\s+\((\d+)/100,\s+(\d+)\s+issues\)"
    )
    # Example: Potential savings: ~746.5M tokens (~$98.43, ~15% of spend)
    savings_pattern = re.compile(r"Potential savings:\s+~?([\d\.]+M?B?K?)\s+tokens\s+\(~?\$([\d\.]+)")

    for line in lines:
        ov_match = overview_pattern.search(line)
        if ov_match:
            payload["overview"].update(
                {
                    "sessions": int(ov_match.group(1).replace(",", "")),
                    "calls": int(ov_match.group(2).replace(",", "")),
                    "cost": float(ov_match.group(3)),
                    "health_grade": ov_match.group(4),
                    "health_score": int(ov_match.group(5)),
                    "issue_count": int(ov_match.group(6)),
                }
            )
        sav_match = savings_pattern.search(line)
        if sav_match and "estimated_tokens_saved" not in payload["overview"]:
            payload["overview"].update(
                {
                    "estimated_tokens_saved": _parse_tokens(sav_match.group(1)),
                    "estimated_usd_saved": _parse_usd(sav_match.group(2)),
                }
            )

    # Extract recommendations
    # Example: ─── 1. 14 MCP servers configured but never used ────────── High ───
    # Potential savings: ~5.1M tokens (~$0.672)
    # -- Run this command ────────────────────────────────────
    rec_header_pattern = re.compile(r"[─\-]{3}\s+\d+\.\s+(.*?)\s+[─\-]{3,}\s+(High|Medium|Low)\s+[─\-]{3}")
    current_rec: dict[str, Any] | None = None
    collecting_action = False

    for line in lines:
        header_match = rec_header_pattern.search(line)
        if header_match:
            if current_rec:
                payload["recommendations"].append(current_rec)
            current_rec = {
                "title": header_match.group(1).strip(),
                "severity": header_match.group(2).lower(),
                "description": "",
                "estimated_tokens_saved": 0,
                "estimated_usd_saved": 0.0,
                "action": "",
            }
            collecting_action = False
            continue

        if current_rec:
            if "Potential savings:" in line:
                sav_match = savings_pattern.search(line)
                if sav_match:
                    current_rec["estimated_tokens_saved"] = _parse_tokens(sav_match.group(1))
                    current_rec["estimated_usd_saved"] = _parse_usd(sav_match.group(2))
                continue

            if (
                "-- Run this command" in line
                or "-- One-time session opener" in line
                or "-- Ask Claude" in line
                or "-- Add to your shell config" in line
            ):
                collecting_action = True
                continue

            if collecting_action:
                if line.strip() and "───" not in line and "───" not in line:
                    current_rec["action"] += line.strip() + "\n"
            else:
                if line.strip() and "───" not in line and "───" not in line:
                    current_rec["description"] += line.strip() + " "

    if current_rec:
        payload["recommendations"].append(current_rec)

    # Clean up whitespace
    for rec in payload["recommendations"]:
        rec["description"] = rec["description"].strip()
        rec["action"] = rec["action"].strip()

    return payload


def summarize_external_payload(tool: str, payload: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "tool": tool,
        "top_level_keys": [],
        "sections": [],
        "highlights": [],
    }
    if isinstance(payload, list):
        summary["sections"] = [{"name": "items", "kind": "list", "count": len(payload)}]
        return summary
    if not isinstance(payload, dict):
        return summary

    summary["top_level_keys"] = sorted(str(key) for key in payload)[:40]
    sections: list[dict[str, Any]] = []
    for key, value in payload.items():
        if isinstance(value, list):
            sections.append({"name": key, "kind": "list", "count": len(value)})
        elif isinstance(value, dict):
            sections.append({"name": key, "kind": "object", "count": len(value)})
    summary["sections"] = sections[:20]

    containers: list[dict[str, Any]] = [payload]
    for key in ("overview", "summary", "totals", "metrics"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            containers.append(nested)

    highlights: list[dict[str, Any]] = []
    seen: set[str] = set()
    for metric_key, aliases in _SUMMARY_METRIC_ALIASES.items():
        for container in containers:
            value = _find_number(container, aliases)
            if value is None or metric_key in seen:
                continue
            highlights.append(
                {
                    "key": metric_key,
                    "label": metric_key.replace("_", " "),
                    "value": value,
                }
            )
            seen.add(metric_key)
    summary["highlights"] = highlights
    return summary


def _truncate_text(value: str, *, limit: int = 12_000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]"


def persist_external_reports(
    store: Any,
    batch: dict[str, Any],
    *,
    source: str,
) -> list[dict[str, Any]]:
    collected_at = str(batch.get("generated_at") or datetime.now(UTC).isoformat())
    persisted: list[dict[str, Any]] = []
    for report in batch.get("reports", []):
        if not isinstance(report, dict):
            continue
        tool = str(report.get("tool") or "unknown")
        payload = report.get("payload")
        summary = summarize_external_payload(tool, payload)
        session_id = store.record_external_analytics_run(
            tool=tool,
            period=str(report.get("period") or batch.get("period") or "unknown"),
            source=source,
            ok=bool(report.get("ok")),
            command_display=str(report.get("command_display") or ""),
            returncode=report.get("returncode"),
            summary=summary,
            payload=payload,
            stdout=_truncate_text(str(report.get("stdout") or "")),
            stderr=_truncate_text(str(report.get("stderr") or "")),
            collected_at=collected_at,
            replace_period_snapshot=True,
        )
        persisted.append(
            {
                "id": session_id,
                "tool": tool,
                "period": str(report.get("period") or batch.get("period") or "unknown"),
                "ok": bool(report.get("ok")),
                "returncode": report.get("returncode"),
                "summary": summary,
                "collected_at": collected_at,
            }
        )
    return persisted


def _tokscale_period_flags(period: str) -> list[str]:
    flags = {
        "today": ["--today"],
        "week": ["--week"],
        "month": ["--month"],
    }
    if period not in flags:
        raise ValueError(f"Tokscale supports only: {', '.join(TOKSCALE.supports_periods)}")
    return flags[period]


def _tokscale_command(binary: str, period: str, *, view: str = "overview") -> list[str]:
    period_flags = _tokscale_period_flags(period)
    if view == "overview":
        return [binary, "--json", "--no-spinner", *period_flags]
    if view in {"models", "monthly", "hourly"}:
        return [binary, view, "--json", "--no-spinner", *period_flags]
    if view == "graph":
        return [binary, "graph", "--no-spinner", *period_flags]
    raise ValueError(f"Unsupported tokscale view: {view}")


def _tokscale_capture_metadata(command: list[str], result: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "ok": bool(result.get("ok")),
        "command_display": shlex.join(command),
        "returncode": result.get("returncode"),
    }
    payload = result.get("payload")
    if isinstance(payload, dict):
        metadata["keys"] = sorted(str(key) for key in payload)[:20]
    parse_error = str(result.get("parse_error") or "").strip()
    if parse_error:
        metadata["parse_error"] = parse_error
    stderr = str(result.get("stderr") or "").strip()
    if stderr:
        metadata["stderr"] = _truncate_text(stderr, limit=600)
    return metadata


def _tokscale_payload_dict(result: dict[str, Any]) -> dict[str, Any]:
    payload = result.get("payload")
    return payload if isinstance(payload, dict) else {}


def _codeburn_capture_metadata(command: list[str], result: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "ok": bool(result.get("ok")),
        "command_display": shlex.join(command),
        "returncode": result.get("returncode"),
    }
    payload = result.get("payload")
    if isinstance(payload, list):
        metadata["count"] = len(payload)
    elif isinstance(payload, dict):
        metadata["keys"] = sorted(str(key) for key in payload)[:20]
    parse_error = str(result.get("parse_error") or "").strip()
    if parse_error:
        metadata["parse_error"] = parse_error
    stderr = str(result.get("stderr") or "").strip()
    if stderr:
        metadata["stderr"] = _truncate_text(stderr, limit=600)
    return metadata


def _codeburn_period_flags(period: str) -> list[str]:
    if period not in CODEBURN.supports_periods:
        raise ValueError(f"CodeBurn supports only: {', '.join(CODEBURN.supports_periods)}")
    return ["-p", period]


def _codeburn_models_command(binary: str, period: str) -> list[str]:
    return [binary, "models", "--format", "json", *_codeburn_period_flags(period)]


def _codeburn_provider_entries(model_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    providers: dict[str, dict[str, Any]] = {}
    for entry in model_entries:
        if not isinstance(entry, dict):
            continue
        provider_id = str(entry.get("provider") or "unknown").strip() or "unknown"
        provider_display = str(entry.get("providerDisplayName") or provider_id).strip() or provider_id
        bucket = providers.setdefault(
            provider_id,
            {
                "provider": provider_id,
                "providerDisplayName": provider_display,
                "models": 0,
                "calls": 0,
                "inputTokens": 0,
                "outputTokens": 0,
                "cacheReadTokens": 0,
                "cacheWriteTokens": 0,
                "totalTokens": 0,
                "costUSD": 0.0,
                "_model_set": set(),
            },
        )
        model_id = str(entry.get("model") or "").strip()
        if model_id:
            model_set = bucket["_model_set"]
            if isinstance(model_set, set):
                model_set.add(model_id)
                bucket["models"] = len(model_set)
        bucket["calls"] += int(entry.get("calls") or 0)
        bucket["inputTokens"] += int(entry.get("inputTokens") or 0)
        bucket["outputTokens"] += int(entry.get("outputTokens") or 0)
        bucket["cacheReadTokens"] += int(entry.get("cacheReadTokens") or 0)
        bucket["cacheWriteTokens"] += int(entry.get("cacheWriteTokens") or 0)
        bucket["totalTokens"] += int(entry.get("totalTokens") or 0)
        bucket["costUSD"] += float(entry.get("costUSD") or 0.0)

    rows: list[dict[str, Any]] = []
    for bucket in providers.values():
        bucket.pop("_model_set", None)
        bucket["costUSD"] = round(float(bucket["costUSD"]), 8)
        rows.append(bucket)
    rows.sort(key=lambda row: float(row.get("costUSD") or 0.0), reverse=True)
    return rows


def _run_codeburn_report_bundle(binary: str, period: str, *, cwd: Path | None = None) -> dict[str, Any]:
    commands = {
        "report": _codeburn_command(binary, period),
        "models": _codeburn_models_command(binary, period),
    }
    results = {view: _run_json_command(command, cwd=cwd) for view, command in commands.items()}

    report_payload = results["report"].get("payload")
    base_payload = dict(report_payload) if isinstance(report_payload, dict) else {}
    model_entries_raw = results["models"].get("payload")
    model_entries = (
        [entry for entry in model_entries_raw if isinstance(entry, dict)] if isinstance(model_entries_raw, list) else []
    )

    payload: dict[str, Any] = dict(base_payload)
    payload["reportKind"] = "codeburn_bundle"
    payload["modelEntries"] = model_entries
    payload["providerEntries"] = _codeburn_provider_entries(model_entries)
    payload["captures"] = {view: _codeburn_capture_metadata(commands[view], results[view]) for view in commands}

    combined_stdout = "\n\n".join(
        f"[{view}]\n{stdout}" for view, result in results.items() if (stdout := str(result.get("stdout") or "").strip())
    )
    combined_stderr = "\n\n".join(
        f"[{view}]\n{stderr}" for view, result in results.items() if (stderr := str(result.get("stderr") or "").strip())
    )
    parse_errors = [
        f"{view}: {parse_error}"
        for view, result in results.items()
        if (parse_error := str(result.get("parse_error") or "").strip())
    ]
    first_failure = next(
        (result for result in results.values() if not bool(result.get("ok"))),
        None,
    )

    return {
        "ok": all(bool(result.get("ok")) for result in results.values()),
        "returncode": first_failure.get("returncode") if first_failure else 0,
        "stdout": combined_stdout,
        "stderr": combined_stderr,
        "payload": payload,
        "parse_error": "; ".join(parse_errors) or None,
        "command": commands["report"],
        "command_display": " && ".join(shlex.join(command) for command in commands.values()),
    }


def _run_tokscale_report_bundle(binary: str, period: str, *, cwd: Path | None = None) -> dict[str, Any]:
    commands = {
        view: _tokscale_command(binary, period, view=view)
        for view in ("overview", "models", "monthly", "hourly", "graph")
    }
    results = {view: _run_json_command(command, cwd=cwd) for view, command in commands.items()}

    overview_payload = _tokscale_payload_dict(results["overview"])
    models_payload = _tokscale_payload_dict(results["models"])
    monthly_payload = _tokscale_payload_dict(results["monthly"])
    hourly_payload = _tokscale_payload_dict(results["hourly"])
    graph_payload = _tokscale_payload_dict(results["graph"])

    payload: dict[str, Any] = dict(overview_payload)
    payload["reportKind"] = "tokscale_bundle"
    payload["modelEntries"] = list(models_payload.get("entries") or [])
    payload["modelGroupBy"] = models_payload.get("groupBy")
    payload["monthlyEntries"] = list(monthly_payload.get("entries") or [])
    payload["monthlyTotalCost"] = monthly_payload.get("totalCost")
    payload["hourlyEntries"] = list(hourly_payload.get("entries") or [])
    payload["hourlyTotalCost"] = hourly_payload.get("totalCost")
    payload["dailyEntries"] = list(graph_payload.get("contributions") or [])
    payload["dailySummary"] = dict(graph_payload.get("summary") or {})
    payload["dailyMeta"] = dict(graph_payload.get("meta") or {})
    payload["dailyYears"] = list(graph_payload.get("years") or [])
    payload["captures"] = {view: _tokscale_capture_metadata(commands[view], results[view]) for view in commands}

    combined_stdout = "\n\n".join(
        f"[{view}]\n{stdout}" for view, result in results.items() if (stdout := str(result.get("stdout") or "").strip())
    )
    combined_stderr = "\n\n".join(
        f"[{view}]\n{stderr}" for view, result in results.items() if (stderr := str(result.get("stderr") or "").strip())
    )
    parse_errors = [
        f"{view}: {parse_error}"
        for view, result in results.items()
        if (parse_error := str(result.get("parse_error") or "").strip())
    ]
    first_failure = next(
        (result for result in results.values() if not bool(result.get("ok"))),
        None,
    )

    return {
        "ok": all(bool(result.get("ok")) for result in results.values()),
        "returncode": first_failure.get("returncode") if first_failure else 0,
        "stdout": combined_stdout,
        "stderr": combined_stderr,
        "payload": payload,
        "parse_error": "; ".join(parse_errors) or None,
        "command": commands["overview"],
        "command_display": " && ".join(shlex.join(command) for command in commands.values()),
    }


def _codeburn_command(binary: str, period: str, subcommand: str = "report") -> list[str]:
    cmd = [binary, subcommand]
    if subcommand == "report":
        cmd.extend(["--format", "json"])
    cmd.extend(_codeburn_period_flags(period))
    return cmd


def _run_json_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout_s: int = 120,
    parser: Any | None = None,
) -> dict[str, Any]:
    proc = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_s,
    )
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    payload: Any = None
    parse_error: str | None = None
    if stdout:
        if parser:
            try:
                payload = parser(stdout)
            except Exception as exc:
                parse_error = str(exc)
        else:
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError as exc:
                parse_error = str(exc)
    return {
        "ok": proc.returncode == 0 and payload is not None,
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "payload": payload,
        "parse_error": parse_error,
    }


def run_external_report(tool: str, *, period: str = "week", cwd: Path | None = None) -> dict[str, Any]:
    normalized = tool.strip().lower()
    parser = None
    if normalized == TOKSCALE.id:
        binary = _find_executable(TOKSCALE)
        if not binary:
            return {
                "tool": TOKSCALE.id,
                "ok": False,
                "error": "not_installed",
                "message": TOKSCALE.install_hint,
            }
        result = _run_tokscale_report_bundle(binary, period, cwd=cwd)
        return {
            "tool": normalized,
            "period": period,
            **result,
        }
    elif normalized == CODEBURN.id:
        binary = _find_executable(CODEBURN)
        if not binary:
            return {
                "tool": CODEBURN.id,
                "ok": False,
                "error": "not_installed",
                "message": CODEBURN.install_hint,
            }
        result = _run_codeburn_report_bundle(binary, period, cwd=cwd)
        return {
            "tool": normalized,
            "period": period,
            **result,
        }
    elif normalized == f"{CODEBURN.id}:optimize":
        binary = _find_executable(CODEBURN)
        if not binary:
            return {
                "tool": f"{CODEBURN.id}:optimize",
                "ok": False,
                "error": "not_installed",
                "message": CODEBURN.install_hint,
            }
        command = _codeburn_command(binary, period, subcommand="optimize")
        parser = parse_codeburn_optimize_output
    else:
        raise ValueError(f"Unsupported external report tool: {tool}")

    result = _run_json_command(command, cwd=cwd, parser=parser)
    return {
        "tool": normalized,
        "period": period,
        "command": command,
        "command_display": shlex.join(command),
        **result,
    }


def run_external_reports(
    *,
    tool: str = "all",
    period: str = "week",
    cwd: Path | None = None,
    include_optimize: bool = False,
) -> dict[str, Any]:
    requested = tool.strip().lower()
    if requested == "all":
        selected = list(REPORTABLE_TOOL_IDS)
        if include_optimize:
            selected.append(f"{CODEBURN.id}:optimize")
    else:
        if requested not in REPORTABLE_TOOL_IDS and requested != f"{CODEBURN.id}:optimize":
            raise ValueError(
                f"Unsupported report tool '{tool}'. Choose one of: all, {', '.join(REPORTABLE_TOOL_IDS)}, codeburn:optimize"
            )
        selected = [requested]

    reports = [run_external_report(item, period=period, cwd=cwd) for item in selected]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "cwd": str(cwd or Path.cwd()),
        "tool": requested,
        "period": period,
        "reports": reports,
    }


__all__ = [
    "REPORTABLE_TOOL_IDS",
    "external_status",
    "persist_external_reports",
    "run_external_report",
    "run_external_reports",
    "summarize_external_payload",
]
