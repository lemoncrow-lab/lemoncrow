"""Benchmark publication pipeline.

Loads the latest benchmark result JSONs from ``{root}/benchmarks/savings/``
and assembles them into a weekly Atelier benchmark post inside ``reports/``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shapes for each benchmark result (all fields optional so missing data → n/a)
# ---------------------------------------------------------------------------


@dataclass
class RoutingSavings:
    sessions_benchmarked: int = 0
    total_cost_saved_usd: float = 0.0
    avg_cost_saved_usd_per_session: float = 0.0
    downtiered_pct: float = 0.0
    by_tier: dict[str, int] = field(default_factory=dict)
    total_baseline_cost_usd: float = 0.0
    generated_at: str = ""


@dataclass
class CompactSavings:
    sessions_benchmarked: int = 0
    total_cost_saved_usd: float = 0.0
    avg_cost_saved_usd_per_session: float = 0.0
    avg_delta_tokens: int = 0
    avg_atelier_freed_tokens_est: int = 0
    avg_native_freed_tokens_measured: int = 0
    atelier_vs_native_delta_pct: float = 0.0
    avg_compaction_events_per_session: float = 0.0
    generated_at: str = ""


@dataclass
class RoutingQuality:
    sessions_benchmarked: int = 0
    total_downtiered_turns: int = 0
    avg_quality_score: float = 0.0
    safe_pct: float = 0.0
    moderate_pct: float = 0.0
    risky_pct: float = 0.0
    env_error_pct_on_downtiered: float = 0.0
    model_error_pct_on_downtiered: float = 0.0
    retry_pct_on_downtiered: float = 0.0
    generated_at: str = ""


@dataclass
class CompactQuality:
    sessions_benchmarked: int = 0
    total_compaction_events: int = 0
    avg_retention_score: float = 0.0
    avg_error_drift: float = 0.0
    avg_extra_read_rate: float = 0.0
    sessions_continued_pct: float = 0.0
    generated_at: str = ""


@dataclass
class RoutingReplay:
    sessions_benchmarked: int = 0
    total_turns_replayed: int = 0
    tool_match_rate: float = 0.0
    avg_input_similarity: float = 0.0
    avg_output_token_ratio: float = 0.0
    total_haiku_cost_usd: float = 0.0
    quality_label_counts: dict[str, int] = field(default_factory=dict)
    haiku_model: str = ""
    generated_at: str = ""


@dataclass
class PublishReport:
    week_label: str  # e.g. "2025-W20"
    week_start: str  # human-readable, e.g. "2025-05-12"
    generated_at: str
    since_arg: str
    corpus_arg: str
    corpus_path: str

    routing_savings: RoutingSavings | None
    compact_savings: CompactSavings | None
    routing_quality: RoutingQuality | None
    compact_quality: CompactQuality | None
    routing_replay: RoutingReplay | None

    # prior-week values for Δ computation (keyed by metric name → float | None)
    prior: dict[str, float | None] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Loader helpers
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(path.read_text())
    return result


def _load_routing_savings(root: Path) -> RoutingSavings | None:
    p = root / "benchmarks" / "savings" / "routing_latest.json"
    if not p.exists():
        return None
    raw = _load_json(p)
    return RoutingSavings(
        sessions_benchmarked=int(raw.get("sessions_benchmarked", 0)),
        total_cost_saved_usd=float(raw.get("total_cost_saved_usd", 0.0)),
        avg_cost_saved_usd_per_session=float(raw.get("avg_cost_saved_usd_per_session", 0.0)),
        downtiered_pct=float(raw.get("downtiered_pct", 0.0)),
        by_tier=dict(raw.get("by_tier") or {}),
        total_baseline_cost_usd=float(raw.get("total_baseline_cost_usd", 0.0)),
        generated_at=str(raw.get("generated_at", "")),
    )


def _load_compact_savings(root: Path) -> CompactSavings | None:
    p = root / "benchmarks" / "savings" / "compact_latest.json"
    if not p.exists():
        return None
    raw = _load_json(p)
    return CompactSavings(
        sessions_benchmarked=int(raw.get("sessions_benchmarked", 0)),
        total_cost_saved_usd=float(raw.get("total_cost_saved_usd", 0.0)),
        avg_cost_saved_usd_per_session=float(raw.get("avg_cost_saved_usd_per_session", 0.0)),
        avg_delta_tokens=int(raw.get("avg_delta_tokens", 0)),
        avg_atelier_freed_tokens_est=int(raw.get("avg_atelier_freed_tokens_est", 0)),
        avg_native_freed_tokens_measured=int(raw.get("avg_native_freed_tokens_measured", 0)),
        atelier_vs_native_delta_pct=float(raw.get("atelier_vs_native_delta_pct", 0.0)),
        avg_compaction_events_per_session=float(raw.get("avg_compaction_events_per_session", 0.0)),
        generated_at=str(raw.get("generated_at", "")),
    )


def _load_routing_quality(root: Path) -> RoutingQuality | None:
    p = root / "benchmarks" / "savings" / "routing_quality_latest.json"
    if not p.exists():
        return None
    raw = _load_json(p)
    return RoutingQuality(
        sessions_benchmarked=int(raw.get("sessions_benchmarked", 0)),
        total_downtiered_turns=int(raw.get("total_downtiered_turns", 0)),
        avg_quality_score=float(raw.get("avg_quality_score", 0.0)),
        safe_pct=float(raw.get("safe_pct", 0.0)),
        moderate_pct=float(raw.get("moderate_pct", 0.0)),
        risky_pct=float(raw.get("risky_pct", 0.0)),
        env_error_pct_on_downtiered=float(raw.get("env_error_pct_on_downtiered", 0.0)),
        model_error_pct_on_downtiered=float(raw.get("model_error_pct_on_downtiered", 0.0)),
        retry_pct_on_downtiered=float(raw.get("retry_pct_on_downtiered", 0.0)),
        generated_at=str(raw.get("generated_at", "")),
    )


def _load_compact_quality(root: Path) -> CompactQuality | None:
    p = root / "benchmarks" / "savings" / "compact_quality_latest.json"
    if not p.exists():
        return None
    raw = _load_json(p)
    return CompactQuality(
        sessions_benchmarked=int(raw.get("sessions_benchmarked", 0)),
        total_compaction_events=int(raw.get("total_compaction_events", 0)),
        avg_retention_score=float(raw.get("avg_retention_score", 0.0)),
        avg_error_drift=float(raw.get("avg_error_drift", 0.0)),
        avg_extra_read_rate=float(raw.get("avg_extra_read_rate", 0.0)),
        sessions_continued_pct=float(raw.get("sessions_continued_pct", 0.0)),
        generated_at=str(raw.get("generated_at", "")),
    )


def _load_routing_replay(root: Path) -> RoutingReplay | None:
    p = root / "benchmarks" / "savings" / "routing_replay_latest.json"
    if not p.exists():
        return None
    raw = _load_json(p)
    return RoutingReplay(
        sessions_benchmarked=int(raw.get("sessions_benchmarked", 0)),
        total_turns_replayed=int(raw.get("total_turns_replayed", 0)),
        tool_match_rate=float(raw.get("tool_match_rate", 0.0)),
        avg_input_similarity=float(raw.get("avg_input_similarity", 0.0)),
        avg_output_token_ratio=float(raw.get("avg_output_token_ratio", 0.0)),
        total_haiku_cost_usd=float(raw.get("total_haiku_cost_usd", 0.0)),
        quality_label_counts=dict(raw.get("quality_label_counts") or {}),
        haiku_model=str(raw.get("haiku_model", "")),
        generated_at=str(raw.get("generated_at", "")),
    )


# ---------------------------------------------------------------------------
# Prior-week delta extraction
# ---------------------------------------------------------------------------


def _metric_snapshot(
    rs: RoutingSavings | None,
    cs: CompactSavings | None,
    rq: RoutingQuality | None,
    cq: CompactQuality | None,
    rr: RoutingReplay | None,
) -> dict[str, float | None]:
    return {
        "routing_sessions": float(rs.sessions_benchmarked) if rs else None,
        "routing_savings_usd": rs.total_cost_saved_usd if rs else None,
        "routing_quality": rq.avg_quality_score if rq else None,
        "compact_retention": cq.avg_retention_score if cq else None,
        "compact_savings_usd": cs.total_cost_saved_usd if cs else None,
        "replay_match": rr.tool_match_rate if rr else None,
    }


def _load_prior_snapshot(prior_dir: Path) -> dict[str, float | None]:
    p = prior_dir / "benchmark.json"
    if not p.exists():
        return {}
    bundle = json.loads(p.read_text())
    prior = bundle.get("prior_snapshot", bundle.get("metric_snapshot", {}))
    return {k: float(v) if v is not None else None for k, v in prior.items()}


# ---------------------------------------------------------------------------
# Week helpers
# ---------------------------------------------------------------------------


def _week_label(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _week_start_str(dt: datetime) -> str:
    from datetime import timedelta

    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_latest_benchmarks(root: Path, since: str = "7d", corpus_arg: str = "") -> PublishReport:
    """Load all available latest benchmark results and assemble a PublishReport."""
    now = datetime.now(tz=UTC)
    week_label = _week_label(now)
    week_start = _week_start_str(now)

    rs = _load_routing_savings(root)
    cs = _load_compact_savings(root)
    rq = _load_routing_quality(root)
    cq = _load_compact_quality(root)
    rr = _load_routing_replay(root)

    corpus_path = str(root / "benchmarks" / "savings")

    return PublishReport(
        week_label=week_label,
        week_start=week_start,
        generated_at=now.strftime("%Y-%m-%d %H:%M UTC"),
        since_arg=since,
        corpus_arg=corpus_arg,
        corpus_path=corpus_path,
        routing_savings=rs,
        compact_savings=cs,
        routing_quality=rq,
        compact_quality=cq,
        routing_replay=rr,
        prior={},
    )


def build_publish_report(
    root: Path,
    output_dir: Path,
    since: str = "7d",
    corpus_arg: str = "",
) -> PublishReport:
    """Build a full PublishReport, including delta vs prior week if available."""
    report = load_latest_benchmarks(root, since=since, corpus_arg=corpus_arg)

    # Locate prior week directory by checking the week before the current one
    from datetime import timedelta

    now = datetime.now(tz=UTC)
    prior_week_dt = now - timedelta(weeks=1)
    prior_label = _week_label(prior_week_dt)
    prior_dir = output_dir / prior_label
    prior_snapshot = _load_prior_snapshot(prior_dir)
    report.prior = prior_snapshot

    return report


def publish(
    root: Path,
    output_dir: Path,
    since: str = "7d",
    corpus_arg: str = "",
    *,
    dry_run: bool = False,
) -> Path | None:
    """Render and write the benchmark report. Returns the report directory (or None on dry-run)."""
    from atelier.infra.benchmarks.markdown_renderer import render_json_bundle, render_markdown

    report = build_publish_report(root, output_dir, since=since, corpus_arg=corpus_arg)
    report_dir = output_dir / report.week_label

    md_content = render_markdown(report)
    json_content = render_json_bundle(report)

    if dry_run:
        _print_dry_run(report_dir, md_content, json_content)
        return None

    report_dir.mkdir(parents=True, exist_ok=True)
    md_path = report_dir / "benchmark.md"
    json_path = report_dir / "benchmark.json"
    md_path.write_text(md_content)
    json_path.write_text(json.dumps(json_content, indent=2))

    _update_index(output_dir, report)

    return report_dir


def _print_dry_run(report_dir: Path, md_content: str, json_content: dict[str, Any]) -> None:
    logger.info("[dry-run] Would write %s (%d bytes)", report_dir / "benchmark.md", len(md_content))
    json_str = json.dumps(json_content)
    logger.info("[dry-run] Would write %s (%d bytes)", report_dir / "benchmark.json", len(json_str))


def _update_index(output_dir: Path, report: PublishReport) -> None:
    """Append or update this week's entry in reports/index.json."""
    index_path = output_dir / "index.json"
    if index_path.exists():
        index: list[dict[str, Any]] = json.loads(index_path.read_text())
    else:
        index = []

    entry: dict[str, Any] = {
        "week": report.week_label,
        "week_start": report.week_start,
        "generated_at": report.generated_at,
        "routing_sessions": report.routing_savings.sessions_benchmarked if report.routing_savings else None,
        "total_routing_savings_usd": report.routing_savings.total_cost_saved_usd if report.routing_savings else None,
        "routing_quality_score": report.routing_quality.avg_quality_score if report.routing_quality else None,
        "compact_retention_score": report.compact_quality.avg_retention_score if report.compact_quality else None,
    }

    # Replace if week already present
    index = [e for e in index if e.get("week") != report.week_label]
    index.append(entry)
    index.sort(key=lambda e: e.get("week", ""), reverse=True)

    index_path.write_text(json.dumps(index, indent=2))


def report_to_dict(report: PublishReport) -> dict[str, Any]:
    """Serialize a PublishReport to a plain dict for JSON export."""
    d = asdict(report)
    return d
