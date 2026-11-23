"""UX0a: local AI settings read/write projections and budget authority.

Only non-secret, allowlisted machine preferences are editable in the browser.
Hosted policies and provider credentials are never part of this surface.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from lemoncrow.core import settings
from lemoncrow.pro.capabilities.review.ai_budget import BUDGET_SETTING
from lemoncrow.pro.capabilities.review.background_budget import BackgroundWorkRequest, JobPriority
from lemoncrow.pro.capabilities.review.background_budget_store import LocalBackgroundBudgetStore
from lemoncrow.pro.capabilities.review.background_local import (
    LOCAL_BACKGROUND_SETTINGS,
    local_intelligence_settings,
    update_local_intelligence_settings,
)
from lemoncrow.pro.capabilities.review.intelligence_policy import SETTINGS_KEYS


@pytest.fixture(autouse=True)
def settings_root(tmp_path: Path) -> Iterator[Path]:
    from lemoncrow.pro.capabilities.model_setup.transport import set_active_store_root

    root = tmp_path / "lemoncrow-root"
    root.mkdir()
    set_active_store_root(root)
    try:
        yield root
    finally:
        set_active_store_root(None)


def test_default_projection_is_editable_local_and_names_its_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    payload = local_intelligence_settings()
    assert payload["scope"] == "local"
    route = payload["intelligence"]
    assert route["editable"] is True and "provider credentials" in route["edit_source"]
    assert route["settings"] == [BUDGET_SETTING, *SETTINGS_KEYS.values(), *LOCAL_BACKGROUND_SETTINGS]
    # No budget set: no limit, and a call may start (OWNER-2).
    assert payload["preferences"][BUDGET_SETTING] is None
    assert payload["ai_budget"]["monthly_budget_usd"] is None and payload["ai_budget"]["may_run"] is True
    assert "intelligence.enabled" not in payload["preferences"]
    assert route["status"] == "ready" and route["resolved"]["driver"] == "openai"
    assert route["run_on"]["effective_value"] == "local" and route["run_on"]["offered_here"] is True
    assert payload["preferences"]["intelligence.driver"] == "auto"
    assert payload["preferences"]["intelligence.background.monthly_model_calls"] == 200
    assert route["background_enabled"] is True
    background = payload["background"]
    assert background["available"] is True and background["scope"] == "local"
    assert background["state"] == "running"
    # No LemonCrow-managed execution locally: the dollar ceiling cannot apply, and says so.
    assert background["managed_spend"] == {
        "applies": False,
        "enforced": False,
        "note": background["managed_spend"]["note"],
    }
    # Shipped envelope: automatic Review is metered but not yet bound by its ceilings.
    assert background["automatic_review"]["envelope_enforced"] is False
    # Local lifetime: no daemon claim.
    assert "only while LemonCrow is running" in payload["lifetime"]


def test_background_switch_off_shows_off_in_the_route_and_the_envelope(settings_root: Path) -> None:
    settings.write_setting(settings_root, "intelligence.background.enabled", False)
    payload = local_intelligence_settings()
    assert payload["intelligence"]["background_enabled"] is False
    assert payload["background"]["state"] == "disabled"
    assert payload["background"]["reason"] == "background_disabled"


def test_a_zero_budget_pauses_the_envelope_and_changes_no_label(settings_root: Path) -> None:
    settings.write_setting(settings_root, BUDGET_SETTING, 0)
    payload = local_intelligence_settings()
    # Which route would run is a label: it does not refuse at $0 (OWNER-1: nothing is hidden).
    assert payload["intelligence"]["status"] != "disabled"
    assert payload["intelligence"]["background_enabled"] is True
    assert payload["ai_budget"]["may_run"] is False and payload["ai_budget"]["reason"] == "AI budget is $0"
    assert payload["preferences"][BUDGET_SETTING] == 0
    assert payload["background"]["state"] == "paused" and payload["background"]["reason"] == "ai_budget_exhausted"
    request = BackgroundWorkRequest(
        key="zero-budget", capability="review.auto_ai", priority=JobPriority.REVIEW_AUTO, max_calls=1, locus="local"
    )
    refused = LocalBackgroundBudgetStore(settings_root).reserve(request)
    assert not refused.admitted and refused.reason.value == "ai_budget_exhausted"


def test_spent_monthly_budget_is_one_paused_state_with_its_reason(settings_root: Path) -> None:
    settings.write_setting(settings_root, "intelligence.background.monthly_model_calls", 1)
    store = LocalBackgroundBudgetStore(settings_root)
    request = BackgroundWorkRequest(
        key="ux0a-spend", capability="review.auto_ai", priority=JobPriority.REVIEW_AUTO, max_calls=1, locus="local"
    )
    assert store.reserve(request).admitted
    store.settle("ux0a-spend", calls=1)
    background = local_intelligence_settings()["background"]
    assert background["state"] == "paused" and background["reason"] == "background_exhausted"
    assert background["background"]["model_calls_used"] == 1
    assert background["background"]["model_calls_remaining"] == 0
    assert background["automatic_review"]["envelope_enforced"] is True
    assert [row["capability"] for row in background["by_capability"]] == ["review.auto_ai"]


def test_unreadable_ledger_degrades_only_the_budget(settings_root: Path) -> None:
    (settings_root / "background_budget.db").mkdir()
    payload = local_intelligence_settings()
    assert payload["background"] == {"available": False, "reason": "the background budget ledger could not be read"}
    assert payload["intelligence"]["run_on"]["effective_value"] == "local"


def test_no_credential_or_endpoint_reaches_the_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-ux0a-never-on-the-wire"
    endpoint = "https://ux0a-private-endpoint.invalid/v1"
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("LEMONCROW_OPENAI_API_KEY", secret)
    monkeypatch.setenv("LEMONCROW_OPENAI_BASE_URL", endpoint)
    document = json.dumps(local_intelligence_settings())
    assert secret not in document and endpoint not in document


def test_a_pinned_model_without_a_provider_is_refused_before_it_breaks_the_route(settings_root: Path) -> None:
    before = settings.load_raw(settings_root)
    with pytest.raises(ValueError, match="choose a provider"):
        update_local_intelligence_settings({"settings": {"intelligence.model": "some-model"}})
    with pytest.raises(ValueError, match="choose a provider"):
        update_local_intelligence_settings(
            {"settings": {"intelligence.driver": "auto", "intelligence.model": "some-model"}}
        )
    assert settings.load_raw(settings_root) == before

    update_local_intelligence_settings(
        {"settings": {"intelligence.driver": "claude", "intelligence.model": "some-model"}}
    )
    assert local_intelligence_settings()["intelligence"]["status"] != "invalid"
    with pytest.raises(ValueError, match="choose a provider"):
        update_local_intelligence_settings({"settings": {"intelligence.driver": "auto"}})


def test_browser_updates_use_the_same_local_settings_as_terminal(settings_root: Path) -> None:
    update_local_intelligence_settings(
        {
            "settings": {
                "intelligence.driver": "claude",
                "intelligence.model": "auto",
                "intelligence.run_on": "local",
                BUDGET_SETTING: 12.5,
                "intelligence.background.enabled": False,
                "intelligence.background.monthly_model_calls": 90,
                "intelligence.background.daily_model_calls": 5,
            }
        }
    )
    values = settings.load_settings(settings_root, category="intelligence")
    assert values["intelligence.driver"] == "claude"
    assert values["intelligence.background.monthly_model_calls"] == 90
    assert values["intelligence.background.daily_model_calls"] == 5
    assert values[BUDGET_SETTING] == 12.5
    projection = local_intelligence_settings()
    assert projection["preferences"][BUDGET_SETTING] == 12.5 and projection["ai_budget"]["monthly_budget_usd"] == 12.5
    update_local_intelligence_settings({"settings": {BUDGET_SETTING: None}})
    assert "intelligence.monthly_budget_usd" not in settings.load_raw(settings_root)
    projection = local_intelligence_settings()
    assert projection["preferences"]["intelligence.driver"] == "claude"
    assert projection["preferences"]["intelligence.background.enabled"] is False
    assert projection["background"]["state"] == "disabled"


@pytest.mark.parametrize(
    "key,value",
    [
        ("llm.api_key", "secret"),
        ("intelligence.driver", "invalid-driver"),
        ("intelligence.enabled", False),
        (BUDGET_SETTING, -2),
        (BUDGET_SETTING, "5"),
        (BUDGET_SETTING, True),
        (BUDGET_SETTING, float("inf")),
        (BUDGET_SETTING, float("nan")),
        ("intelligence.background.monthly_model_calls", -2),
        ("intelligence.background.daily_model_calls", 1.5),
        ("intelligence.run_on", "managed"),
        ("intelligence.model", "bad\nmodel"),
    ],
)
def test_browser_update_rejects_unsafe_values_without_writing(
    settings_root: Path,
    key: str,
    value: object,
) -> None:
    before = settings.load_raw(settings_root)
    with pytest.raises(ValueError):
        update_local_intelligence_settings(
            {
                "settings": {
                    "intelligence.background.monthly_model_calls": 100,
                    key: value,
                }
            }
        )
    assert settings.load_raw(settings_root) == before
