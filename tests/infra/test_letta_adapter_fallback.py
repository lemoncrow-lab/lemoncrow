from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.foundation.memory_models import MemoryBlock
from lemoncrow.infra.memory_bridges.letta_adapter import LettaMemoryStore
from lemoncrow.infra.memory_bridges.openmemory import OpenMemoryMemoryStore
from lemoncrow.infra.storage.factory import make_memory_store
from lemoncrow.infra.storage.memory_store import MemorySidecarUnavailable


class _UnavailableClient:
    def upsert_block(self, payload: dict[str, Any]) -> dict[str, Any]:
        _ = payload
        raise RuntimeError("503 Service Unavailable")

    def archival_search(self, **kwargs: Any) -> list[dict[str, Any]]:
        _ = kwargs
        raise RuntimeError("503 Service Unavailable")


def test_letta_memory_store_raises_sidecar_unavailable_on_503(tmp_path: Path) -> None:
    store = LettaMemoryStore(tmp_path / "lemoncrow", client=_UnavailableClient())

    with pytest.raises(MemorySidecarUnavailable):
        store.upsert_block(
            MemoryBlock(agent_id="lc:code", label="persona", value="text"),
            actor="agent:lc:code",
        )

    with pytest.raises(MemorySidecarUnavailable):
        store.search_passages("lc:code", "query")


def test_make_memory_store_does_not_fallback_when_letta_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "lemoncrow.infra.memory_bridges.letta_adapter.LettaAdapter.is_available",
        classmethod(lambda cls: True),
    )

    def fail_init(
        self: object,
        root: object,
        *,
        adapter: object | None = None,
        client: object | None = None,
    ) -> None:
        _ = (self, root, adapter, client)
        raise MemorySidecarUnavailable("503 Service Unavailable")

    monkeypatch.setattr("lemoncrow.infra.memory_bridges.letta_adapter.LettaMemoryStore.__init__", fail_init)

    with pytest.raises(MemorySidecarUnavailable):
        make_memory_store(tmp_path / "lemoncrow", prefer="letta")


class _UnavailableOpenMemoryClient:
    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        _ = (name, arguments)
        raise RuntimeError("503 Service Unavailable")


def test_openmemory_memory_store_raises_sidecar_unavailable_on_503(tmp_path: Path) -> None:
    store = OpenMemoryMemoryStore(tmp_path / "lemoncrow", client=_UnavailableOpenMemoryClient())  # type: ignore[arg-type]

    with pytest.raises(MemorySidecarUnavailable):
        store.upsert_block(
            MemoryBlock(agent_id="lc:code", label="persona", value="text"),
            actor="agent:lc:code",
        )

    with pytest.raises(MemorySidecarUnavailable):
        store.search_passages("lc:code", "query")
