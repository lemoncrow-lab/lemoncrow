from __future__ import annotations

import importlib
import sys

import pytest


def test_letta_adapter_unavailable_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_LETTA_URL", raising=False)

    from atelier.infra.memory_bridges.letta_adapter import LettaAdapter

    assert LettaAdapter.is_available() is False


def test_letta_adapter_constructor_raises_clearly_without_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATELIER_LETTA_URL", raising=False)

    from atelier.infra.memory_bridges.letta_adapter import LettaAdapter

    with pytest.raises(RuntimeError, match=r"letta-client not installed|ATELIER_LETTA_URL not set"):
        LettaAdapter()


def test_import_is_safe_without_real_letta_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_LETTA_URL", raising=False)
    sys.modules.pop("atelier.infra.memory_bridges.letta_adapter", None)
    sys.modules.pop("letta_client", None)

    module = importlib.import_module("atelier.infra.memory_bridges.letta_adapter")

    if not module._HAS_LETTA:
        assert "letta_client" not in sys.modules
    assert module.LettaAdapter.is_available() is False
