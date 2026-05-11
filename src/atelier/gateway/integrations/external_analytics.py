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
    "cost_usd": ("cost", "cost_usd", "total_cost", "total_usd", "usd_cost", "estimated_cost"),
    "calls": ("calls", "tool_calls", "call_count", "requests", "total_calls"),
    "sessions": ("sessions", "session_count", "runs", "total_sessions"),
    "input_tokens": ("input_tokens", "prompt_tokens", "total_input_tokens"),
    "output_tokens": ("output_tokens", "completion_tokens", "total_output_tokens"),
    "tokens": ("tokens", "total_tokens"),
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
        summary = summarize_external_payload(tool, report.get("payload"))
        session_id = store.record_external_analytics_run(
            tool=tool,
            period=str(report.get("period") or batch.get("period") or "unknown"),
            source=source,
            ok=bool(report.get("ok")),
            command_display=str(report.get("command_display") or ""),
            returncode=report.get("returncode"),
            summary=summary,
            payload=report.get("payload"),
            stdout=_truncate_text(str(report.get("stdout") or "")),
            stderr=_truncate_text(str(report.get("stderr") or "")),
            collected_at=collected_at,
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


def _tokscale_command(binary: str, period: str) -> list[str]:
    flags = {
        "today": ["--today"],
        "week": ["--week"],
        "month": ["--month"],
    }
    if period not in flags:
        raise ValueError(f"Tokscale supports only: {', '.join(TOKSCALE.supports_periods)}")
    return [binary, "--json", "--no-spinner", *flags[period]]


def _codeburn_command(binary: str, period: str) -> list[str]:
    if period not in CODEBURN.supports_periods:
        raise ValueError(f"CodeBurn supports only: {', '.join(CODEBURN.supports_periods)}")
    return [binary, "report", "--format", "json", "-p", period]


def _run_json_command(command: list[str], *, cwd: Path | None = None, timeout_s: int = 120) -> dict[str, Any]:
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
    if normalized == TOKSCALE.id:
        binary = _find_executable(TOKSCALE)
        if not binary:
            return {
                "tool": TOKSCALE.id,
                "ok": False,
                "error": "not_installed",
                "message": TOKSCALE.install_hint,
            }
        command = _tokscale_command(binary, period)
    elif normalized == CODEBURN.id:
        binary = _find_executable(CODEBURN)
        if not binary:
            return {
                "tool": CODEBURN.id,
                "ok": False,
                "error": "not_installed",
                "message": CODEBURN.install_hint,
            }
        command = _codeburn_command(binary, period)
    else:
        raise ValueError(f"Unsupported external report tool: {tool}")

    result = _run_json_command(command, cwd=cwd)
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
) -> dict[str, Any]:
    requested = tool.strip().lower()
    if requested == "all":
        selected = list(REPORTABLE_TOOL_IDS)
    else:
        if requested not in REPORTABLE_TOOL_IDS:
            raise ValueError(f"Unsupported report tool '{tool}'. Choose one of: all, {', '.join(REPORTABLE_TOOL_IDS)}")
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
