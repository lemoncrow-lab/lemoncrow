"""Tests for the provider authentication wizard."""

from __future__ import annotations

import os
import stat

import pytest

from atelier.core.capabilities.auth import wizard


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
    return tmp_path


def test_load_saved_credentials_empty(isolated_store):
    assert wizard.load_saved_credentials() == {}


def test_save_credentials_creates_env(isolated_store):
    wizard.save_credentials({"ANTHROPIC_API_KEY": "sk-test-123"})
    path = wizard.credentials_path()
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert 'ANTHROPIC_API_KEY="sk-test-123"' in content
    assert wizard.load_saved_credentials()["ANTHROPIC_API_KEY"] == "sk-test-123"


def test_save_credentials_merges(isolated_store):
    wizard.save_credentials({"OPENAI_API_KEY": "sk-a"})
    wizard.save_credentials({"GROQ_API_KEY": "sk-b"})
    saved = wizard.load_saved_credentials()
    assert saved["OPENAI_API_KEY"] == "sk-a"
    assert saved["GROQ_API_KEY"] == "sk-b"


def test_load_env_into_process(isolated_store, monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    wizard.save_credentials({"MISTRAL_API_KEY": "sk-mistral"})
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    wizard.load_env_into_process()
    assert os.environ["MISTRAL_API_KEY"] == "sk-mistral"


def test_load_env_does_not_override(isolated_store, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    wizard.save_credentials({"OPENAI_API_KEY": "from-file"})
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    wizard.load_env_into_process()
    assert os.environ["OPENAI_API_KEY"] == "from-env"


def test_all_provider_configs_have_required_fields():
    for pid, cfg in wizard.PROVIDER_CONFIGS.items():
        assert cfg["name"], pid
        assert cfg["link"], pid
        assert cfg["test_model"], pid
        assert cfg["litellm_prefix"], pid
        assert cfg["fields"], pid
        for field in cfg["fields"]:
            assert "name" in field, pid
            assert "label" in field, pid
            assert "secret" in field, pid


def test_save_credentials_sets_owner_only_file_and_dir_perms(tmp_path, monkeypatch):
    # Point ATELIER_ROOT at a directory that does NOT exist yet so
    # save_credentials has to create the parent and tighten it to 0o700.
    fresh_root = tmp_path / "fresh"
    assert not fresh_root.exists()
    monkeypatch.setenv("ATELIER_ROOT", str(fresh_root))

    wizard.save_credentials({"ANTHROPIC_API_KEY": "sk-secret"})

    path = wizard.credentials_path()
    assert path.exists()
    # .env file must be owner read/write only.
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # The freshly-created parent dir must be owner-only.
    assert path.parent == fresh_root
    assert stat.S_IMODE(fresh_root.stat().st_mode) == 0o700


def test_validate_provider_unknown():
    ok, msg = wizard.validate_provider("nope", {})
    assert ok is False
    assert "Unknown provider" in msg
