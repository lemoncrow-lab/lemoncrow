"""Local installation identifier (formerly the commercial device id).

Open-source runtime: this id gates nothing, enforces no cap, and is NEVER
derived from hardware. It is a random, locally generated UUID cached at
``~/.lemoncrow/device_id``, with an optional env override for containers/CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.core.capabilities.licensing import store


@pytest.fixture(autouse=True)
def _isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.delenv(store.DEVICE_ID_ENV_VAR, raising=False)
    monkeypatch.delenv(store.AUTH_TOKEN_ENV_VAR, raising=False)


def test_device_id_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(store.DEVICE_ID_ENV_VAR, "abcdef123456")
    assert store.load_or_create_device_id() == "abcdef123456"


def test_device_id_env_override_wins_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # There is no anonymous cap to game, so the override no longer needs a token.
    monkeypatch.delenv(store.AUTH_TOKEN_ENV_VAR, raising=False)
    monkeypatch.setenv(store.DEVICE_ID_ENV_VAR, "feedface0001")
    assert store.load_or_create_device_id() == "feedface0001"


def test_device_id_env_override_rejects_malformed_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for bad in ("has spaces", "x" * 65, "abc", "semi;colon", ""):
        monkeypatch.setenv(store.DEVICE_ID_ENV_VAR, bad)
        assert store.load_or_create_device_id() != bad


def test_device_id_is_stable_within_a_root() -> None:
    first = store.load_or_create_device_id()
    assert first == store.load_or_create_device_id()
    assert len(first) >= 4


def test_device_id_is_random_local_not_machine_derived(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Two different store roots on the SAME machine yield DIFFERENT ids — proof
    # the id is random-local, not derived from any machine property.
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "a"))
    id_a = store.load_or_create_device_id()
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "b"))
    id_b = store.load_or_create_device_id()
    assert id_a != id_b
    assert len(id_a) == 12 and all(c in "0123456789abcdef" for c in id_a)


def test_stable_machine_device_id_ignores_override_and_is_hex(monkeypatch: pytest.MonkeyPatch) -> None:
    # The base id never reads the env override and is a random-local hex UUID
    # fragment (not a hardware/machine identifier).
    monkeypatch.setenv(store.DEVICE_ID_ENV_VAR, "deadbeef1234")
    base = store.stable_machine_device_id()
    assert base != "deadbeef1234"
    assert len(base) == 12 and all(c in "0123456789abcdef" for c in base)
