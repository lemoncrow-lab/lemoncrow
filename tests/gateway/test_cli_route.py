from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from lemoncrow.gateway.cli import cli
from lemoncrow.pro.capabilities.cross_vendor_routing.configuration import RouteConfig, save_route_config
from lemoncrow.pro.capabilities.lesson_promotion.models import TypedLesson
from lemoncrow.pro.capabilities.lesson_promotion.store import TypedLessonStore


def _invoke(root: Path, *args: str) -> Result:
    runner = CliRunner()
    return runner.invoke(cli, ["--root", str(root), *args])


@pytest.mark.parametrize("command", ["configure", "plan"])
def test_route_commands_report_that_routing_is_unavailable(tmp_path: Path, command: str) -> None:
    root = tmp_path / ".lemoncrow"
    args = ["route", command]
    if command == "plan":
        args.extend(["--tool", "read", "--task", "find the failing test"])

    res = _invoke(root, *args)

    assert res.exit_code != 0
    assert "not available in this release" in res.output
    assert not (root / "route.yaml").exists()


def test_proof_run_survives_non_dict_smart_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hand-edited/corrupt ~/.lemoncrow/smart_state.json holding a valid but
    non-dict JSON value (null, list, string, number) must fall through to the
    graceful ClickException, not crash with an uncaught AttributeError."""
    root = tmp_path / ".lemoncrow"
    fake_home = tmp_path / "home"
    (fake_home / ".lemoncrow").mkdir(parents=True)
    (fake_home / ".lemoncrow" / "smart_state.json").write_text("null", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))

    res = _invoke(root, "proof", "run", "--session-id", "wp32-proof")

    assert res.exit_code != 0
    assert not isinstance(res.exception, AttributeError), res.exception
    assert "Could not auto-measure context reduction" in res.output


def test_route_status_reports_recommendation_count_and_savings(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    save_route_config(root, RouteConfig(enabled_vendors=["anthropic"]))
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
