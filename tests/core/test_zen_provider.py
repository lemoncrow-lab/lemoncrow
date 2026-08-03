"""OpenCode Zen keyless free tier."""

from __future__ import annotations

import pytest

from lemoncrow.core.capabilities.providers import zen
from lemoncrow.core.capabilities.providers.config import ProviderConfig
from lemoncrow.pro.capabilities.cross_vendor_routing import configuration as routing_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.delenv("LEMONCROW_ZEN_PUBLIC", raising=False)
    monkeypatch.setattr(zen, "_opencode_auth_key", lambda: None)


def test_public_key_used_without_credentials() -> None:
    assert zen.zen_api_key() == zen.ZEN_PUBLIC_KEY
    assert zen.has_account_key() is False


def test_env_key_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-zen-123")
    assert zen.zen_api_key() == "sk-zen-123"
    assert zen.has_account_key() is True


def test_opencode_login_key_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(zen, "_opencode_auth_key", lambda: "sk-from-opencode")
    assert zen.zen_api_key() == "sk-from-opencode"


def test_transport_rewrites_zen_models() -> None:
    patched = zen.apply_zen_transport({"model": "zen/big-pickle", "messages": []})
    assert patched["model"] == "openai/big-pickle"
    assert patched["api_base"] == zen.ZEN_BASE_URL
    assert patched["api_key"] == zen.ZEN_PUBLIC_KEY


def test_transport_leaves_other_models_untouched() -> None:
    original = {"model": "claude-sonnet-4-5", "messages": []}
    assert zen.apply_zen_transport(original) is original


def test_provider_config_reports_zen_configured_without_key() -> None:
    assert ProviderConfig({}).is_configured("zen") is True


def test_public_tier_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_ZEN_PUBLIC", "0")
    assert zen.public_tier_enabled() is False
    assert ProviderConfig({}).is_configured("zen") is False


def test_zen_is_the_fallback_vendor_when_nothing_else_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routing_config.shutil, "which", lambda _command: None)
    assert routing_config.detect_api_key_vendors({}) == ("zen",)
    assert routing_config.detect_configured_vendors({}) == ("zen",)


def test_zen_public_tier_never_competes_with_a_real_vendor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routing_config.shutil, "which", lambda _command: None)
    vendors = routing_config.detect_api_key_vendors({"ANTHROPIC_API_KEY": "sk-ant-x"})
    assert vendors == ("anthropic",)


def test_explicit_zen_key_enables_zen_alongside_other_vendors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routing_config.shutil, "which", lambda _command: None)
    vendors = routing_config.detect_api_key_vendors({"ANTHROPIC_API_KEY": "sk-ant-x", "OPENCODE_API_KEY": "sk-zen-x"})
    assert vendors == ("anthropic", "zen")


def test_free_model_ids_fall_back_when_catalog_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    zen.invalidate_cache()
    monkeypatch.setattr(zen, "_fetch_free_model_ids", lambda: ())
    try:
        assert zen.free_model_ids() == zen._FALLBACK_FREE_MODELS
    finally:
        zen.invalidate_cache()
