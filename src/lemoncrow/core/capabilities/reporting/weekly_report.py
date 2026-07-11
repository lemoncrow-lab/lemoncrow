"""Weekly governance report aggregation.

The report is intentionally deterministic and read-only: it summarizes traces,
rubric validation outcomes, lesson-candidate inbox state, and context-budget
telemetry without calling an LLM or mutating the store.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lemoncrow.core.foundation.lesson_models import LessonCandidate
from lemoncrow.core.foundation.models import (
    FileEditRecord,
    Playbook,
    ToolCall,
    Trace,
    ValidationResult,
)
from lemoncrow.core.foundation.store import ContextStore

_CONTEXT_TOOL_NAMES = {"reasoning", "get_context", "context"}
_RESCUE_TOOL_NAMES = {"rescue_failure", "rescue"}


class RateMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: int = 0
    total: int = 0
    pass_rate: float | None = None


class DomainRubricRate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str
    passed: int
    total: int
    pass_rate: float | None


class PlaybookRetrieval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = ""
    domain: str = ""
    count: int


class RubricFailureSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str
    check_name: str
    count: int
    file_paths: list[str] = Field(default_factory=list)
    summary: str


class LessonCandidateSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    domain: str
    kind: str
    cluster_size: int
    body: str = ""


class DriftSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prior_period_start: datetime
    prior_period_end: datetime
    rubric_pass_rate_delta_pct: float | None = None
    rescue_attempt_rate_delta_pct: float | None = None
    context_budget_usage_delta_pct: float | None = None
    current_rescue_attempt_rate: float | None = None
    prior_rescue_attempt_rate: float | None = None
    current_context_budget_usage: float | None = None
    prior_context_budget_usage: float | None = None


class Report(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_start: datetime
    period_end: datetime
    git_sha: str
    rubric_pass_rate: RateMetric
    rubric_pass_rate_by_domain: list[DomainRubricRate] = Field(default_factory=list)
    top_playbooks: list[PlaybookRetrieval] = Field(default_factory=list)
    top_rubric_failures: list[RubricFailureSummary] = Field(default_factory=list)
    pending_lesson_candidates_count: int = 0
    top_lesson_candidates: list[LessonCandidateSummary] = Field(default_factory=list)
    drift: DriftSignals


def generate_report(
    since: timedelta,
    *,
    store: ContextStore,
    now: datetime | None = None,
    repo_root: Path | None = None,
    git_sha: str | None = None,
) -> Report:
    """Generate a deterministic report for the period ending at ``now``."""

    period_end = _as_utc(now or datetime.now(UTC))
    period_start = period_end - since
    prior_end = period_start
    prior_start = period_start - since

    current_traces = _list_traces_between(store, period_start, period_end)
    prior_traces = _list_traces_between(store, prior_start, prior_end)

    current_rates, current_by_domain = _rubric_rates(current_traces)
    prior_rates, _ = _rubric_rates(prior_traces)
    current_rescue_rate = _rescue_rate(current_traces)
    prior_rescue_rate = _rescue_rate(prior_traces)
    current_budget_usage = _average_context_budget_usage(store, current_traces)
    prior_budget_usage = _average_context_budget_usage(store, prior_traces)

    return Report(
        period_start=period_start,
        period_end=period_end,
        git_sha=git_sha or _git_sha(repo_root or Path.cwd()),
        rubric_pass_rate=current_rates,
        rubric_pass_rate_by_domain=current_by_domain,
        top_playbooks=_top_playbooks(store, current_traces),
        top_rubric_failures=_top_rubric_failures(current_traces),
        pending_lesson_candidates_count=_pending_lesson_count(store),
        top_lesson_candidates=_top_lesson_candidates(store),
        drift=DriftSignals(
            prior_period_start=prior_start,
            prior_period_end=prior_end,
            rubric_pass_rate_delta_pct=_delta_pct(current_rates.pass_rate, prior_rates.pass_rate),
            rescue_attempt_rate_delta_pct=_delta_pct(current_rescue_rate, prior_rescue_rate),
            context_budget_usage_delta_pct=_delta_pct(current_budget_usage, prior_budget_usage),
            current_rescue_attempt_rate=current_rescue_rate,
            prior_rescue_attempt_rate=prior_rescue_rate,
            current_context_budget_usage=current_budget_usage,
            prior_context_budget_usage=prior_budget_usage,
        ),
    )


def render_markdown(report: Report) -> str:
    """Render a Slack-pasteable Markdown report in a stable compact shape."""

    lines = [
        "# LemonCrow Weekly Governance Report",
        "",
        f"Period: {report.period_start.date().isoformat()} to {report.period_end.date().isoformat()}",
        f"Git SHA: `{report.git_sha}`",
        "",
        "## Summary",
        "| Metric | Current | Prior | Change |",
        "|---|---:|---:|---:|",
        (
            "| Rubric pass rate | "
            f"{_fmt_rate(report.rubric_pass_rate)} | "
            f"{_fmt_pct(report.rubric_pass_rate.pass_rate)} | "
            f"{_fmt_delta(report.drift.rubric_pass_rate_delta_pct)} |"
        ),
        (
            "| Rescue attempt rate | "
            f"{_fmt_pct(report.drift.current_rescue_attempt_rate)} | "
            f"{_fmt_pct(report.drift.prior_rescue_attempt_rate)} | "
            f"{_fmt_delta(report.drift.rescue_attempt_rate_delta_pct)} |"
        ),
        (
            "| Avg context budget usage | "
            f"{_fmt_pct(report.drift.current_context_budget_usage)} | "
            f"{_fmt_pct(report.drift.prior_context_budget_usage)} | "
            f"{_fmt_delta(report.drift.context_budget_usage_delta_pct)} |"
        ),
        "",
        "## Rubric Pass Rate By Domain",
    ]

    if report.rubric_pass_rate_by_domain:
        lines.extend(["| Domain | Passed | Total | Rate |", "|---|---:|---:|---:|"])
        for domain_rate in report.rubric_pass_rate_by_domain:
            lines.append(
                f"| `{domain_rate.domain}` | {domain_rate.passed} | {domain_rate.total} | "
                f"{_fmt_pct(domain_rate.pass_rate)} |"
            )
    else:
        lines.append("No rubric validations recorded in this period.")

    lines.extend(["", "## Top Playbooks Retrieved"])
    if report.top_playbooks:
        lines.extend(["| Playbook | Domain | Count |", "|---|---|---:|"])
        for retrieval in report.top_playbooks:
            title = retrieval.title or retrieval.id
            lines.append(f"| `{retrieval.id}` {title} | `{retrieval.domain}` | {retrieval.count} |")
    else:
        lines.append("No reasoning-context retrievals recorded in this period.")

    lines.extend(["", "## Top Rubric Failures"])
    if report.top_rubric_failures:
        lines.extend(["| Check | Domain | Count | Files | Summary |", "|---|---|---:|---|---|"])
        for failure in report.top_rubric_failures:
            files = ", ".join(f"`{path}`" for path in failure.file_paths[:3]) or "n/a"
            lines.append(
                f"| `{failure.check_name}` | `{failure.domain}` | {failure.count} | {files} | "
                f"{_escape_table(failure.summary)} |"
            )
    else:
        lines.append("No rubric failures recorded in this period.")

    lines.extend(["", "## Lesson Candidates Pending Review"])
    lines.append(f"Pending candidates: {report.pending_lesson_candidates_count}")
    if report.top_lesson_candidates:
        lines.extend(["", "| Candidate | Domain | Cluster | Summary |", "|---|---|---:|---|"])
        for candidate in report.top_lesson_candidates:
            lines.append(
                f"| `{candidate.id}` | `{candidate.domain}` | {candidate.cluster_size} | "
                f"{_escape_table(_one_line(candidate.body))} |"
            )

    return "\n".join(lines).rstrip() + "\n"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _list_traces_between(store: ContextStore, start: datetime, end: datetime) -> list[Trace]:
    # Traces live in the file-based session store; window the upper bound in Python.
    return [trace for trace in store.list_traces(since=start, limit=100_000) if trace.created_at < end]


def _rubric_rates(traces: Iterable[Trace]) -> tuple[RateMetric, list[DomainRubricRate]]:
    total = 0
    passed = 0
    by_domain: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for trace in traces:
        if not trace.validation_results:
            continue
        trace_passed = all(result.passed for result in trace.validation_results)
        total += 1
        passed += 1 if trace_passed else 0
        by_domain[trace.domain][1] += 1
        by_domain[trace.domain][0] += 1 if trace_passed else 0

    domain_rates = [
        DomainRubricRate(
            domain=domain,
            passed=values[0],
            total=values[1],
            pass_rate=_rate(values[0], values[1]),
        )
        for domain, values in sorted(by_domain.items())
    ]
    return RateMetric(passed=passed, total=total, pass_rate=_rate(passed, total)), domain_rates


def _top_playbooks(store: ContextStore, traces: Iterable[Trace]) -> list[PlaybookRetrieval]:
    known_blocks = {block.id: block for block in store.list_blocks(include_deprecated=True)}
    counts: Counter[str] = Counter()
    for trace in traces:
        for tool in trace.tools_called:
            if tool.name not in _CONTEXT_TOOL_NAMES:
                continue
            ids = _playbook_ids_from_tool(tool, known_blocks)
            for block_id in ids:
                counts[block_id] += max(1, tool.count)

    if not counts:
        for block in known_blocks.values():
            if block.usage_count > 0:
                counts[block.id] = block.usage_count

    out: list[PlaybookRetrieval] = []
    for block_id, count in counts.most_common(5):
        known_block = known_blocks.get(block_id)
        out.append(
            PlaybookRetrieval(
                id=block_id,
                title=known_block.title if known_block else "",
                domain=known_block.domain if known_block else "",
                count=count,
            )
        )
    return out


def _playbook_ids_from_tool(tool: ToolCall, known_blocks: dict[str, Playbook]) -> list[str]:
    ids: list[str] = []
    args = tool.args or {}
    for key in (
        "matched_blocks",
        "block_ids",
        "playbooks",
        "playbooks",
        "active_playbooks",
    ):
        ids.extend(_string_items(args.get(key)))
    matched = args.get("matched")
    if isinstance(matched, list):
        for item in matched:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                ids.append(item["id"])
    text = "\n".join([tool.result_summary, str(args.get("context", "")), str(args.get("output", ""))])
    for block_id in known_blocks:
        if block_id in text:
            ids.append(block_id)
    return list(dict.fromkeys(item for item in ids if item in known_blocks))


def _string_items(value: Any) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    if isinstance(value, str):
        return [value]
    return []


def _top_rubric_failures(traces: Iterable[Trace]) -> list[RubricFailureSummary]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for trace in traces:
        failed = [result for result in trace.validation_results if not result.passed]
        for result in failed:
            summary = _failure_summary(trace, result)
            key = (trace.domain, result.name, summary)
            bucket = grouped.setdefault(
                key,
                {
                    "count": 0,
                    "files": set(),
                },
            )
            bucket["count"] += 1
            bucket["files"].update(_trace_file_paths(trace))

    rows = sorted(
        grouped.items(),
        key=lambda item: (-int(item[1]["count"]), item[0][0], item[0][1], item[0][2]),
    )
    return [
        RubricFailureSummary(
            domain=domain,
            check_name=check_name,
            count=int(data["count"]),
            file_paths=sorted(data["files"])[:5],
            summary=summary,
        )
        for (domain, check_name, summary), data in rows[:5]
    ]


def _failure_summary(trace: Trace, result: ValidationResult) -> str:
    if trace.output_summary.strip():
        return _one_line(trace.output_summary)
    if result.detail.strip():
        return _one_line(result.detail)
    if trace.errors_seen:
        return _one_line(trace.errors_seen[0])
    return "Rubric check failed."


def _trace_file_paths(trace: Trace) -> list[str]:
    paths: list[str] = []
    for item in trace.files_touched:
        if isinstance(item, str):
            paths.append(item)
        elif isinstance(item, FileEditRecord):
            paths.append(item.path)
    return list(dict.fromkeys(paths))


def _pending_lesson_count(store: ContextStore) -> int:
    with store._connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM lesson_candidate WHERE status = 'inbox'").fetchone()
    return int(row["n"] if row is not None else 0)


def _top_lesson_candidates(store: ContextStore) -> list[LessonCandidateSummary]:
    candidates = store.list_lesson_candidates(status="inbox", limit=500)
    candidates.sort(key=lambda item: (-_cluster_size(item), item.created_at, item.id))
    return [
        LessonCandidateSummary(
            id=item.id,
            domain=item.domain,
            kind=item.kind,
            cluster_size=_cluster_size(item),
            body=item.body or _candidate_body(item),
        )
        for item in candidates[:3]
    ]


def _cluster_size(candidate: LessonCandidate) -> int:
    evidence_ids = candidate.evidence.get("trace_ids")
    if isinstance(evidence_ids, list):
        return max(len(evidence_ids), len(candidate.evidence_trace_ids))
    return len(candidate.evidence_trace_ids)


def _candidate_body(candidate: LessonCandidate) -> str:
    if candidate.proposed_block is not None:
        return candidate.proposed_block.situation
    if candidate.proposed_rubric_check:
        return candidate.proposed_rubric_check
    return ""


def _rescue_rate(traces: list[Trace]) -> float | None:
    if not traces:
        return None
    rescue_traces = 0
    for trace in traces:
        if any(tool.name in _RESCUE_TOOL_NAMES for tool in trace.tools_called):
            rescue_traces += 1
    return rescue_traces / len(traces)


def _average_context_budget_usage(store: ContextStore, traces: Iterable[Trace]) -> float | None:
    input_tokens = 0
    naive_tokens = 0
    seen_session_ids: set[str] = set()
    for trace in traces:
        if not trace.session_id or trace.session_id in seen_session_ids:
            continue
        seen_session_ids.add(trace.session_id)
        for record in store.list_context_budgets(trace.session_id):
            if record.naive_input_tokens <= 0:
                continue
            input_tokens += int(record.input_tokens)
            naive_tokens += int(record.naive_input_tokens)
    if naive_tokens <= 0:
        return None
    return input_tokens / naive_tokens


def _rate(passed: int, total: int) -> float | None:
    return passed / total if total else None


def _delta_pct(current: float | None, prior: float | None) -> float | None:
    if current is None or prior is None:
        return None
    return round((current - prior) * 100.0, 4)


def _fmt_rate(metric: RateMetric) -> str:
    if metric.total == 0:
        return "n/a"
    return f"{metric.passed}/{metric.total} ({_fmt_pct(metric.pass_rate)})"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100.0:.1f}%"


def _fmt_delta(value: float | None) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.1f} pp"


def _one_line(text: str, *, limit: int = 160) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "..."


def _escape_table(text: str) -> str:
    return text.replace("|", "\\|")


def _git_sha(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return "unknown"
    return result.stdout.strip() or "unknown"


__all__ = [
    "DomainRubricRate",
    "DriftSignals",
    "LessonCandidateSummary",
    "PlaybookRetrieval",
    "RateMetric",
    "Report",
    "RubricFailureSummary",
    "generate_report",
    "render_markdown",
]
