"""Unit tests for aggregation, pricing, and report formatting."""

from __future__ import annotations

import json

from benchmarks.flowlib.report import (
    _DEFAULT_PRICING,
    aggregate,
    format_report,
)


def _json_usage(inp: int, out: int, cr: int = 0, cw: int = 0) -> tuple[str, bytes]:
    body = json.dumps(
        {
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cw,
            }
        }
    ).encode()
    return "application/json", body


def test_aggregate_sums_and_skips_empty() -> None:
    records = [
        _json_usage(100, 20, cr=900),
        ("text/html", b"<html></html>"),  # skipped: no usage
        _json_usage(50, 10),
    ]
    stats = aggregate("on", records)
    assert stats.requests == 2
    assert stats.usage.input_tokens == 150
    assert stats.usage.output_tokens == 30
    assert stats.usage.cache_read_input_tokens == 900


def test_cache_read_ratio_and_cost() -> None:
    stats = aggregate("on", [_json_usage(100, 0, cr=900)])
    assert abs(stats.cache_read_ratio() - 0.9) < 1e-9
    expected = (100 * 3.00 + 900 * 0.30) / 1_000_000
    assert abs(stats.cost_usd(_DEFAULT_PRICING) - expected) < 1e-12


def test_format_report_two_runs_has_delta() -> None:
    base = aggregate("lemoncrow_off", [_json_usage(1000, 100)])
    cand = aggregate("lemoncrow_on", [_json_usage(100, 100, cr=900)])
    out = format_report([base, cand], _DEFAULT_PRICING)
    assert "lemoncrow_off" in out
    assert "lemoncrow_on" in out
    assert "cache-read ratio" in out
    assert "delta" in out
