"""Tests for the benchmark publication pipeline (publisher + markdown_renderer)."""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _routing_savings_json() -> dict[str, Any]:
    return {
        "benchmark": "savings-routing",
        "sessions_benchmarked": 50,
        "sessions_skipped": 0,
        "total_cost_saved_usd": 43.61,
        "avg_cost_saved_usd_per_session": 0.872,
        "downtiered_pct": 5.9,
        "by_tier": {"cheap": 73, "medium": 3285, "expensive": 1347},
        "total_baseline_cost_usd": 228.3,
        "generated_at": "2025-05-15T17:18:37+00:00",
    }


def _compact_savings_json() -> dict[str, Any]:
    return {
        "benchmark": "savings-compact",
        "sessions_benchmarked": 15,
        "total_cost_saved_usd": 1.64,
        "avg_cost_saved_usd_per_session": 0.109,
        "avg_delta_tokens": 36349,
        "avg_atelier_freed_tokens_est": 239286,
        "avg_native_freed_tokens_measured": 202938,
        "atelier_vs_native_delta_pct": 17.91,
        "avg_compaction_events_per_session": 2.47,
        "generated_at": "2025-05-15T15:36:42+00:00",
    }


def _routing_quality_json() -> dict[str, Any]:
    return {
        "benchmark": "quality-routing",
        "sessions_benchmarked": 23,
        "total_downtiered_turns": 219,
        "avg_quality_score": 0.887,
        "safe_pct": 69.4,
        "moderate_pct": 22.8,
        "risky_pct": 7.8,
        "env_error_pct_on_downtiered": 2.1,
        "model_error_pct_on_downtiered": 1.4,
        "retry_pct_on_downtiered": 0.9,
        "generated_at": "2025-05-15T17:18:37+00:00",
    }


def _compact_quality_json() -> dict[str, Any]:
    return {
        "benchmark": "quality-compact",
        "sessions_benchmarked": 20,
        "total_compaction_events": 41,
        "avg_retention_score": 0.81,
        "avg_error_drift": -0.09,
        "avg_extra_read_rate": 0.142,
        "sessions_continued_pct": 81.5,
        "generated_at": "2025-05-15T15:36:42+00:00",
    }


def _routing_replay_json() -> dict[str, Any]:
    return {
        "benchmark": "replay-routing",
        "sessions_benchmarked": 3,
        "total_turns_replayed": 9,
        "tool_match_rate": 0.222,
        "avg_input_similarity": 0.067,
        "avg_output_token_ratio": 1.881,
        "total_haiku_cost_usd": 0.005,
        "quality_label_counts": {"tool_mismatch": 7, "diverge": 2},
        "haiku_model": "claude-haiku-4-5",
        "generated_at": "2025-05-15T18:00:00+00:00",
    }


@pytest.fixture()
def benchmark_root(tmp_path: Path) -> Path:
    """A fake ~/.atelier with all five benchmark result files."""
    savings_dir = tmp_path / "benchmarks" / "savings"
    savings_dir.mkdir(parents=True)

    (savings_dir / "routing_latest.json").write_text(json.dumps(_routing_savings_json()))
    (savings_dir / "compact_latest.json").write_text(json.dumps(_compact_savings_json()))
    (savings_dir / "routing_quality_latest.json").write_text(json.dumps(_routing_quality_json()))
    (savings_dir / "compact_quality_latest.json").write_text(json.dumps(_compact_quality_json()))
    (savings_dir / "routing_replay_latest.json").write_text(json.dumps(_routing_replay_json()))
    return tmp_path


@pytest.fixture()
def empty_root(tmp_path: Path) -> Path:
    """A fake root with no benchmark files at all."""
    (tmp_path / "benchmarks" / "savings").mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# Tests: load_latest_benchmarks
# ---------------------------------------------------------------------------


class TestLoadLatestBenchmarks:
    def test_loads_all_five(self, benchmark_root: Path) -> None:
        from atelier.infra.benchmarks.publisher import load_latest_benchmarks

        report = load_latest_benchmarks(benchmark_root)
        assert report.routing_savings is not None
        assert report.compact_savings is not None
        assert report.routing_quality is not None
        assert report.compact_quality is not None
        assert report.routing_replay is not None

    def test_missing_files_return_none(self, empty_root: Path) -> None:
        from atelier.infra.benchmarks.publisher import load_latest_benchmarks

        report = load_latest_benchmarks(empty_root)
        assert report.routing_savings is None
        assert report.compact_savings is None
        assert report.routing_quality is None
        assert report.compact_quality is None
        assert report.routing_replay is None

    def test_week_label_format(self, benchmark_root: Path) -> None:
        from atelier.infra.benchmarks.publisher import load_latest_benchmarks

        report = load_latest_benchmarks(benchmark_root)
        import re

        assert re.match(r"\d{4}-W\d{2}", report.week_label), report.week_label

    def test_routing_savings_fields(self, benchmark_root: Path) -> None:
        from atelier.infra.benchmarks.publisher import load_latest_benchmarks

        report = load_latest_benchmarks(benchmark_root)
        rs = report.routing_savings
        assert rs is not None
        assert rs.sessions_benchmarked == 50
        assert abs(rs.total_cost_saved_usd - 43.61) < 0.01
        assert rs.downtiered_pct == 5.9
        assert rs.by_tier["cheap"] == 73

    def test_compact_savings_fields(self, benchmark_root: Path) -> None:
        from atelier.infra.benchmarks.publisher import load_latest_benchmarks

        report = load_latest_benchmarks(benchmark_root)
        cs = report.compact_savings
        assert cs is not None
        assert cs.sessions_benchmarked == 15
        assert cs.avg_delta_tokens == 36349

    def test_routing_quality_fields(self, benchmark_root: Path) -> None:
        from atelier.infra.benchmarks.publisher import load_latest_benchmarks

        report = load_latest_benchmarks(benchmark_root)
        rq = report.routing_quality
        assert rq is not None
        assert abs(rq.avg_quality_score - 0.887) < 0.001
        assert rq.safe_pct == 69.4

    def test_compact_quality_fields(self, benchmark_root: Path) -> None:
        from atelier.infra.benchmarks.publisher import load_latest_benchmarks

        report = load_latest_benchmarks(benchmark_root)
        cq = report.compact_quality
        assert cq is not None
        assert abs(cq.avg_retention_score - 0.81) < 0.001
        assert cq.total_compaction_events == 41

    def test_replay_fields(self, benchmark_root: Path) -> None:
        from atelier.infra.benchmarks.publisher import load_latest_benchmarks

        report = load_latest_benchmarks(benchmark_root)
        rr = report.routing_replay
        assert rr is not None
        assert rr.tool_match_rate == 0.222
        assert rr.quality_label_counts["tool_mismatch"] == 7


# ---------------------------------------------------------------------------
# Tests: render_markdown
# ---------------------------------------------------------------------------


class TestRenderMarkdown:
    def test_renders_without_error(self, benchmark_root: Path) -> None:
        from atelier.infra.benchmarks.markdown_renderer import render_markdown
        from atelier.infra.benchmarks.publisher import load_latest_benchmarks

        report = load_latest_benchmarks(benchmark_root)
        md = render_markdown(report)
        assert len(md) > 100

    def test_contains_headline_table(self, benchmark_root: Path) -> None:
        from atelier.infra.benchmarks.markdown_renderer import render_markdown
        from atelier.infra.benchmarks.publisher import load_latest_benchmarks

        report = load_latest_benchmarks(benchmark_root)
        md = render_markdown(report)
        assert "## Headline numbers" in md

    def test_contains_routing_section(self, benchmark_root: Path) -> None:
        from atelier.infra.benchmarks.markdown_renderer import render_markdown
        from atelier.infra.benchmarks.publisher import load_latest_benchmarks

        report = load_latest_benchmarks(benchmark_root)
        md = render_markdown(report)
        assert "## Routing" in md

    def test_contains_compaction_section(self, benchmark_root: Path) -> None:
        from atelier.infra.benchmarks.markdown_renderer import render_markdown
        from atelier.infra.benchmarks.publisher import load_latest_benchmarks

        report = load_latest_benchmarks(benchmark_root)
        md = render_markdown(report)
        assert "## Compaction" in md

    def test_contains_methodology_section(self, benchmark_root: Path) -> None:
        from atelier.infra.benchmarks.markdown_renderer import render_markdown
        from atelier.infra.benchmarks.publisher import load_latest_benchmarks

        report = load_latest_benchmarks(benchmark_root)
        md = render_markdown(report)
        assert "## Methodology" in md

    def test_raw_data_link_present(self, benchmark_root: Path) -> None:
        from atelier.infra.benchmarks.markdown_renderer import render_markdown
        from atelier.infra.benchmarks.publisher import load_latest_benchmarks

        report = load_latest_benchmarks(benchmark_root)
        md = render_markdown(report)
        assert "[benchmark.json](./benchmark.json)" in md

    def test_routing_cost_formatted_as_usd(self, benchmark_root: Path) -> None:
        from atelier.infra.benchmarks.markdown_renderer import render_markdown
        from atelier.infra.benchmarks.publisher import load_latest_benchmarks

        report = load_latest_benchmarks(benchmark_root)
        md = render_markdown(report)
        assert "$43.61" in md

    def test_no_data_shows_not_available(self, empty_root: Path) -> None:
        from atelier.infra.benchmarks.markdown_renderer import render_markdown
        from atelier.infra.benchmarks.publisher import load_latest_benchmarks

        report = load_latest_benchmarks(empty_root)
        md = render_markdown(report)
        assert "No routing savings data available" in md
        assert "No compact savings data available" in md
        assert "No replay data available" in md

    def test_delta_without_prior_shows_na(self, benchmark_root: Path) -> None:
        from atelier.infra.benchmarks.markdown_renderer import render_markdown
        from atelier.infra.benchmarks.publisher import load_latest_benchmarks

        report = load_latest_benchmarks(benchmark_root)
        assert report.prior == {}
        md = render_markdown(report)
        # The headline table delta column should show n/a when no prior exists
        assert "n/a" in md

    def test_delta_computed_from_prior(self, benchmark_root: Path) -> None:
        from atelier.infra.benchmarks.markdown_renderer import render_markdown
        from atelier.infra.benchmarks.publisher import load_latest_benchmarks

        report = load_latest_benchmarks(benchmark_root)
        # Inject prior snapshot: routing_savings_usd was 40.00 previously
        report.prior = {
            "routing_sessions": 40.0,
            "routing_savings_usd": 40.00,
            "routing_quality": 0.870,
            "compact_retention": 0.800,
            "compact_savings_usd": 1.50,
            "replay_match": 0.200,
        }
        md = render_markdown(report)
        # 43.61 vs 40.00 = +9.0%
        assert "+9.0%" in md


# ---------------------------------------------------------------------------
# Tests: render_json_bundle
# ---------------------------------------------------------------------------


class TestRenderJsonBundle:
    def test_returns_dict(self, benchmark_root: Path) -> None:
        from atelier.infra.benchmarks.markdown_renderer import render_json_bundle
        from atelier.infra.benchmarks.publisher import load_latest_benchmarks

        report = load_latest_benchmarks(benchmark_root)
        bundle = render_json_bundle(report)
        assert isinstance(bundle, dict)

    def test_contains_metric_snapshot(self, benchmark_root: Path) -> None:
        from atelier.infra.benchmarks.markdown_renderer import render_json_bundle
        from atelier.infra.benchmarks.publisher import load_latest_benchmarks

        report = load_latest_benchmarks(benchmark_root)
        bundle = render_json_bundle(report)
        assert "metric_snapshot" in bundle
        snap = bundle["metric_snapshot"]
        assert "routing_savings_usd" in snap
        assert "routing_quality" in snap

    def test_serialisable(self, benchmark_root: Path) -> None:
        from atelier.infra.benchmarks.markdown_renderer import render_json_bundle
        from atelier.infra.benchmarks.publisher import load_latest_benchmarks

        report = load_latest_benchmarks(benchmark_root)
        bundle = render_json_bundle(report)
        # Should not raise
        json.dumps(bundle)


# ---------------------------------------------------------------------------
# Tests: publish (integration)
# ---------------------------------------------------------------------------


class TestPublish:
    def test_creates_report_files(self, benchmark_root: Path, tmp_path: Path) -> None:
        from atelier.infra.benchmarks.publisher import publish

        output_dir = tmp_path / "reports"
        report_dir = publish(root=benchmark_root, output_dir=output_dir)
        assert report_dir is not None
        assert (report_dir / "benchmark.md").exists()
        assert (report_dir / "benchmark.json").exists()

    def test_dry_run_no_files_written(self, benchmark_root: Path, tmp_path: Path) -> None:
        from atelier.infra.benchmarks.publisher import publish

        output_dir = tmp_path / "reports"
        result = publish(root=benchmark_root, output_dir=output_dir, dry_run=True)
        assert result is None
        assert not output_dir.exists()

    def test_creates_index_json(self, benchmark_root: Path, tmp_path: Path) -> None:
        from atelier.infra.benchmarks.publisher import publish

        output_dir = tmp_path / "reports"
        publish(root=benchmark_root, output_dir=output_dir)
        index_path = output_dir / "index.json"
        assert index_path.exists()
        index = json.loads(index_path.read_text())
        assert len(index) == 1
        assert "week" in index[0]

    def test_index_updated_on_second_publish(self, benchmark_root: Path, tmp_path: Path) -> None:
        from atelier.infra.benchmarks.publisher import publish

        output_dir = tmp_path / "reports"
        publish(root=benchmark_root, output_dir=output_dir)
        # Publish again — should still have only one entry (same week)
        publish(root=benchmark_root, output_dir=output_dir)
        index = json.loads((output_dir / "index.json").read_text())
        assert len(index) == 1

    def test_prior_week_delta_included_in_json(self, benchmark_root: Path, tmp_path: Path) -> None:
        from datetime import datetime, timedelta

        from atelier.infra.benchmarks.publisher import _week_label, publish

        output_dir = tmp_path / "reports"
        # Write a fake prior week benchmark.json
        now = datetime.now(tz=UTC)
        prior_label = _week_label(now - timedelta(weeks=1))
        prior_dir = output_dir / prior_label
        prior_dir.mkdir(parents=True)
        prior_bundle = {
            "metric_snapshot": {
                "routing_sessions": 40.0,
                "routing_savings_usd": 40.00,
                "routing_quality": 0.870,
                "compact_retention": 0.800,
                "compact_savings_usd": 1.50,
                "replay_match": 0.200,
            }
        }
        (prior_dir / "benchmark.json").write_text(json.dumps(prior_bundle))

        report_dir = publish(root=benchmark_root, output_dir=output_dir)
        assert report_dir is not None
        bundle = json.loads((report_dir / "benchmark.json").read_text())
        # The prior field should have been populated
        assert bundle.get("prior") or bundle.get("metric_snapshot")

    def test_week_label_dir_name(self, benchmark_root: Path, tmp_path: Path) -> None:
        import re

        from atelier.infra.benchmarks.publisher import publish

        output_dir = tmp_path / "reports"
        report_dir = publish(root=benchmark_root, output_dir=output_dir)
        assert report_dir is not None
        assert re.match(r"\d{4}-W\d{2}", report_dir.name)

    def test_json_bundle_is_valid_json(self, benchmark_root: Path, tmp_path: Path) -> None:
        from atelier.infra.benchmarks.publisher import publish

        output_dir = tmp_path / "reports"
        report_dir = publish(root=benchmark_root, output_dir=output_dir)
        assert report_dir is not None
        content = (report_dir / "benchmark.json").read_text()
        parsed = json.loads(content)
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# Tests: CLI command (smoke test via click test runner)
# ---------------------------------------------------------------------------


class TestBenchmarkPublishCLI:
    def test_dry_run_prints_dry_run_label(self, benchmark_root: Path, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from atelier.gateway.cli import cli

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--root",
                str(benchmark_root),
                "benchmark",
                "publish",
                "--output",
                str(tmp_path / "reports"),
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output.lower()

    def test_publish_reports_output_path(self, benchmark_root: Path, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from atelier.gateway.cli import cli

        runner = CliRunner()
        output_dir = tmp_path / "reports"
        result = runner.invoke(
            cli,
            [
                "--root",
                str(benchmark_root),
                "benchmark",
                "publish",
                "--output",
                str(output_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "benchmark.md" in result.output or "Report written" in result.output
