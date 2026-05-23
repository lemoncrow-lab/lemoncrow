from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner, Result

from atelier.core.capabilities.lesson_promotion.models import TypedLesson
from atelier.core.capabilities.lesson_promotion.store import TypedLessonStore
from atelier.gateway.adapters.cli import cli


def _invoke(root: Path, *args: str) -> Result:
    runner = CliRunner()
    return runner.invoke(cli, ["--root", str(root), *args])


def test_route_configure_writes_route_yaml(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / ".atelier"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    res = _invoke(root, "route", "configure", "--json")

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert "anthropic" in payload["enabled_vendors"]
    assert "google" in payload["enabled_vendors"]
    assert (root / "route.yaml").exists()


def test_route_plan_fails_closed_without_config(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"

    res = _invoke(root, "route", "plan", "--tool", "read", "--task", "find the failing test")

    assert res.exit_code != 0
    assert "route config not found" in res.output


def test_route_plan_returns_cross_vendor_recommendation(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / ".atelier"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    configured = _invoke(root, "route", "configure", "--json")
    assert configured.exit_code == 0, configured.output

    res = _invoke(
        root,
        "route",
        "plan",
        "--tool",
        "read",
        "--task",
        "find the failing test",
        "--json",
    )

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["vendor"] == "google"
    assert payload["model"].startswith("gemini")
    assert payload["fallback"] is not None


def test_route_status_reports_recommendation_count_and_savings(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / ".atelier"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    configured = _invoke(root, "route", "configure", "--vendor", "anthropic", "--json")
    assert configured.exit_code == 0, configured.output
    TypedLessonStore(root).upsert_lesson(
        TypedLesson(
            kind="route-preference",
            match={"tool": "read", "phase": "explore"},
            prefer={"vendor": "anthropic", "model": "claude-haiku-4-5"},
            confidence=0.9,
        )
    )
    (root / "live_savings_events.jsonl").write_text(
        json.dumps(
            {
                "kind": "model_recommendation",
                "configured": True,
                "cost_saved_usd": 0.125,
                "applied_lessons": ["tl-1"],
                "cost_cap_triggered": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    res = _invoke(root, "route", "status", "--json")

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["recommendation_count"] == 1
    assert payload["estimated_savings_usd"] == 0.125
    assert payload["active_lesson_count"] == 1
    assert payload["lesson_application_count"] == 1
    assert payload["cost_cap_trigger_count"] == 1
