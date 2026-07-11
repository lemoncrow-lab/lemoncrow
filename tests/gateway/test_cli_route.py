from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from lemoncrow.core.capabilities.lesson_promotion.models import TypedLesson
from lemoncrow.core.capabilities.lesson_promotion.store import TypedLessonStore
from lemoncrow.gateway.cli import cli


def _invoke(root: Path, *args: str) -> Result:
    runner = CliRunner()
    return runner.invoke(cli, ["--root", str(root), *args])


@pytest.fixture(autouse=True)
def _pro_entitlement(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Cross-vendor routing CLI is Pro-gated; grant a Pro plan so these tests
    exercise the routing behavior, not the entitlement wall."""
    from lemoncrow.core.capabilities.licensing import entitlements
    from tests.helpers import grant_oauth_pro

    grant_oauth_pro(monkeypatch)
    yield
    entitlements.reload()


def test_route_configure_writes_route_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / ".lemoncrow"
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
    root = tmp_path / ".lemoncrow"

    res = _invoke(root, "route", "plan", "--tool", "read", "--task", "find the failing test")

    assert res.exit_code != 0
    assert "route config not found" in res.output


def test_route_plan_returns_cross_vendor_recommendation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / ".lemoncrow"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    # Pin the vendor set explicitly so host-surface auto-detection (e.g. a locally
    # installed `ollama` binary) cannot leak a free local vendor into the routing
    # decision and make this test environment-dependent.
    configured = _invoke(
        root,
        "route",
        "configure",
        "--vendor",
        "anthropic",
        "--vendor",
        "openai",
        "--vendor",
        "google",
        "--json",
    )
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


def test_route_status_reports_recommendation_count_and_savings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / ".lemoncrow"
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
