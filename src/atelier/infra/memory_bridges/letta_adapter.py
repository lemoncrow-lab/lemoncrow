"""Optional Letta sidecar adapter and MemoryStore implementation."""

from __future__ import annotations

import contextlib
import logging
import os
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any

from atelier.core.foundation.memory_models import (
    ArchivalPassage,
    MemoryBlock,
    MemoryBlockHistory,
    MemoryRecall,
    RunMemoryFrame,
)
from atelier.infra.storage.memory_store import MemorySidecarUnavailable
from atelier.infra.storage.sqlite_memory_store import SqliteMemoryStore

logger = logging.getLogger(__name__)

_PINNED_TAG = "atelier:pinned"
_HAS_LETTA = False
LettaClient: Any = None


def _load_letta_client() -> bool:
    global LettaClient, _HAS_LETTA
    if _HAS_LETTA:
        return True
    try:
        module = import_module("letta_client")
    except ImportError:
        LettaClient = None
        _HAS_LETTA = False
        return False
    client_type = getattr(module, "LettaClient", None) or getattr(module, "Letta", None)
    if client_type is None:
        LettaClient = None
        _HAS_LETTA = False
        return False
    LettaClient = client_type
    _HAS_LETTA = True
    return True


def _sidecar_error(exc: Exception) -> MemorySidecarUnavailable:
    return MemorySidecarUnavailable(f"Letta sidecar unavailable: {exc}")


class LettaAdapter:
    """Small compatibility wrapper around the optional Letta client."""

    source = "letta"

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        *,
        client: Any | None = None,
    ) -> None:
        self.url = url or os.environ.get("ATELIER_LETTA_URL", "")
        self.api_key = api_key or os.environ.get("ATELIER_LETTA_API_KEY", "")
        if client is not None:
            self.client = client
            return
        if not self.url:
            raise RuntimeError("ATELIER_LETTA_URL not set")
        if not _load_letta_client():
            raise RuntimeError("letta-client not installed; install 'atelier[memory]'")
        self.client = self._construct_client()

    @classmethod
    def is_available(cls) -> bool:
        return bool(os.environ.get("ATELIER_LETTA_URL")) and _load_letta_client()

    def upsert_block(self, block: MemoryBlock) -> dict[str, Any]:
        payload = self.block_to_letta(block)
        agent_id = block.agent_id or "default"
        try:
            # New SDK (>= 1.7): agents.blocks.update does label-scoped upsert
            if hasattr(self.client, "agents") and hasattr(self.client.agents, "blocks"):
                result = self.client.agents.blocks.update(
                    block_label=block.label,
                    agent_id=agent_id,
                    value=payload["value"],
                    metadata=payload.get("metadata"),
                    tags=payload.get("tags"),
                    limit=payload.get("limit"),
                )
                return self._as_mapping(result)
        except Exception:
            # Block may not exist yet — create standalone + attach
            logger.debug("upsert_block label-scoped update fallback", exc_info=True)
        try:
            if hasattr(self.client, "blocks") and hasattr(self.client.blocks, "create"):
                result = self.client.blocks.create(**payload)
                block_id = result.id if hasattr(result, "id") else result.get("id", "")
                if block_id and hasattr(self.client, "agents") and hasattr(self.client.agents, "blocks"):
                    with contextlib.suppress(Exception):
                        self.client.agents.blocks.attach(block_id=block_id, agent_id=agent_id)
                return self._as_mapping(result)
        except Exception as exc:
            raise _sidecar_error(exc) from exc
        # Fallback: try flat API
        try:
            if hasattr(self.client, "upsert_block"):
                result = self.client.upsert_block(payload)
                return self._as_mapping(result)
        except Exception as exc:
            raise _sidecar_error(exc) from exc
        raise RuntimeError("Letta client does not expose block upsert")

    def get_block(self, agent_id: str, label: str) -> dict[str, Any] | None:
        try:
            # New SDK: agents.blocks.retrieve by label
            if hasattr(self.client, "agents") and hasattr(self.client.agents, "blocks"):
                result = self.client.agents.blocks.retrieve(block_label=label, agent_id=agent_id)
                return self._as_mapping(result)
        except Exception:
            logger.debug("get_block label-scoped retrieve fallback", exc_info=True)
        # Fallback: flat get_block
        try:
            if hasattr(self.client, "get_block"):
                result = self.client.get_block(agent_id=agent_id, label=label)
                return self._as_mapping(result) if result else None
        except Exception as exc:
            raise _sidecar_error(exc) from exc
        return None

    def list_blocks(self, agent_id: str) -> list[dict[str, Any]]:
        try:
            # New SDK: agents.blocks.list
            if hasattr(self.client, "agents") and hasattr(self.client.agents, "blocks"):
                result = self.client.agents.blocks.list(agent_id=agent_id)
                items = result.data if hasattr(result, "data") else result
                return [self._as_mapping(item) for item in items or []]
        except Exception as exc:
            raise _sidecar_error(exc) from exc
        # Fallback: flat list_blocks
        try:
            if hasattr(self.client, "list_blocks"):
                result = self.client.list_blocks(agent_id=agent_id)
                return [self._as_mapping(item) for item in result or []]
        except Exception as exc:
            raise _sidecar_error(exc) from exc
        return []

    def delete_block(self, block_id: str) -> None:
        try:
            # New SDK: blocks.delete
            if hasattr(self.client, "blocks") and hasattr(self.client.blocks, "delete"):
                self.client.blocks.delete(block_id)
                return
        except Exception as exc:
            raise _sidecar_error(exc) from exc
        # Fallback
        try:
            if hasattr(self.client, "delete_block"):
                self.client.delete_block(block_id)
                return
        except Exception as exc:
            raise _sidecar_error(exc) from exc

    def tombstone_block(
        self,
        block_id: str,
        *,
        deprecated_by_block_id: str | None = None,
        reason: str = "",
    ) -> None:
        metadata = {
            "atelier_deprecated_at": datetime.now(UTC).isoformat(),
            "atelier_deprecated_by_block_id": deprecated_by_block_id,
            "atelier_deprecation_reason": reason,
        }
        try:
            if (
                hasattr(self.client, "blocks")
                and not isinstance(self.client.blocks, dict)
                and hasattr(self.client.blocks, "update")
            ):
                self.client.blocks.update(block_id=block_id, metadata=metadata)
                return
        except Exception as exc:
            raise _sidecar_error(exc) from exc
        try:
            if hasattr(self.client, "update_block"):
                self.client.update_block(block_id=block_id, metadata=metadata)
                return
        except Exception as exc:
            raise _sidecar_error(exc) from exc
        try:
            if hasattr(self.client, "tombstone_block"):
                self.client.tombstone_block(block_id=block_id, metadata=metadata)
                return
        except Exception as exc:
            raise _sidecar_error(exc) from exc
        raise RuntimeError("Letta client does not expose block tombstone/update")

    def insert_archival(self, passage: ArchivalPassage) -> dict[str, Any]:
        agent_id = passage.agent_id
        try:
            # New SDK: agents.passages.create
            if hasattr(self.client, "agents") and hasattr(self.client.agents, "passages"):
                result = self.client.agents.passages.create(
                    agent_id=agent_id,
                    text=passage.text,
                    tags=list(passage.tags or []),
                    created_at=passage.created_at.isoformat() if passage.created_at else None,
                )
                return self._as_mapping(result)
        except Exception as exc:
            raise _sidecar_error(exc) from exc
        # Fallback: flat archival_insert
        try:
            payload = self.passage_to_letta(passage)
            if hasattr(self.client, "archival_insert"):
                result = self.client.archival_insert(**payload)
            else:
                raise RuntimeError("Letta client does not expose archival insert")
            return self._as_mapping(result)
        except Exception as exc:
            raise _sidecar_error(exc) from exc

    def list_archival(
        self,
        *,
        agent_id: str,
        tags: list[str] | None,
        since: datetime | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        try:
            # New SDK: agents.passages.list
            if hasattr(self.client, "agents") and hasattr(self.client.agents, "passages"):
                result = self.client.agents.passages.list(agent_id=agent_id, limit=limit)
                items = self._extract_items(result)
                return [self._as_mapping(item) for item in items or []]
        except Exception as exc:
            raise _sidecar_error(exc) from exc
        # Fallback: flat archival_list
        try:
            if hasattr(self.client, "archival_list"):
                result = self.client.archival_list(agent_id=agent_id, tags=tags or [], limit=limit)
            else:
                result = self.search_archival(agent_id=agent_id, query="", top_k=limit, tags=tags, since=since)
            raw = result.get("results", result.get("passages", [])) if isinstance(result, dict) else result
            return [self._as_mapping(item) for item in raw or []]
        except Exception as exc:
            raise _sidecar_error(exc) from exc

    def update_passage_metadata(self, passage_id: str, metadata: dict[str, Any]) -> None:
        # New letta-client SDK does not expose passage metadata update.
        # This is a best-effort no-op; callers should handle gracefully.
        _ = (passage_id, metadata)

    def search_archival(
        self,
        *,
        agent_id: str,
        query: str,
        top_k: int,
        tags: list[str] | None,
        since: datetime | None,
    ) -> list[dict[str, Any]]:
        try:
            # New SDK: agents.passages.search
            if hasattr(self.client, "agents") and hasattr(self.client.agents, "passages"):
                result = self.client.agents.passages.search(
                    agent_id=agent_id,
                    query=query,
                    top_k=top_k,
                    tags=list(tags or []),
                    start_datetime=since.isoformat() if since else None,
                )
                items = self._extract_items(result)
                return [self._as_mapping(item) for item in items or []]
        except Exception as exc:
            raise _sidecar_error(exc) from exc
        # Fallback: flat archival_search
        try:
            if hasattr(self.client, "archival_search"):
                result = self.client.archival_search(
                    agent_id=agent_id, query=query, top_k=top_k, tags=tags or [], since=since
                )
            else:
                raise RuntimeError("Letta client does not expose archival search")
            raw = result.get("results", result.get("passages", [])) if isinstance(result, dict) else result
            return [self._as_mapping(item) for item in raw or []]
        except Exception as exc:
            raise _sidecar_error(exc) from exc

    def _construct_client(self) -> Any:
        assert LettaClient is not None
        # letta-client >= 1.7 uses keyword-only constructor: Letta(*, base_url=..., api_key=...)
        try:
            return LettaClient(base_url=self.url, api_key=self.api_key or None)
        except TypeError:
            pass
        # Older versions: positional args or different kwarg names
        try:
            return LettaClient(base_url=self.url, token=self.api_key or None)
        except TypeError:
            pass
        try:
            return LettaClient(url=self.url, api_key=self.api_key or None)
        except TypeError:
            pass
        # Last-resort fallback — unlikely to work but preserves backward compat
        return LettaClient(self.url, self.api_key)

    def summarize_run(
        self,
        dropped_events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Delegate run summarization to the Letta sidecar.

        Returns a list of dicts compatible with ``SleeptimeChunk`` fields:
        ``start_event_index``, ``end_event_index``, ``paraphrase``.

        Raises ``RuntimeError`` on any failure so callers can fall back to the
        local summariser.
        """
        try:
            if hasattr(self.client, "summarize_run"):
                result = self.client.summarize_run(dropped_events)
                return [self._as_mapping(item) for item in result or []]
        except Exception as exc:
            raise RuntimeError(f"Letta summarize_run failed: {exc}") from exc
        raise RuntimeError("Letta client does not expose summarize_run()")

    @staticmethod
    def block_to_letta(block: MemoryBlock) -> dict[str, Any]:
        tags = list(block.metadata.get("tags", [])) if isinstance(block.metadata.get("tags"), list) else []
        if block.pinned and _PINNED_TAG not in tags:
            tags.append(_PINNED_TAG)
        metadata = dict(block.metadata)
        metadata["atelier_agent_id"] = block.agent_id
        metadata["atelier_block_id"] = block.id
        metadata["atelier_description"] = block.description
        metadata["atelier_read_only"] = block.read_only
        if block.deprecated_at is not None:
            metadata["atelier_deprecated_at"] = block.deprecated_at.isoformat()
            metadata["atelier_deprecated_by_block_id"] = block.deprecated_by_block_id
            metadata["atelier_deprecation_reason"] = block.deprecation_reason
        return {
            "label": block.label,
            "value": block.value,
            "limit": block.limit_chars,
            "metadata": metadata,
            "tags": tags,
        }

    @staticmethod
    def letta_to_block(data: dict[str, Any], *, agent_id: str) -> MemoryBlock:
        metadata = dict(data.get("metadata") or {})
        tags = list(data.get("tags") or metadata.get("tags") or [])
        return MemoryBlock(
            id=str(metadata.get("atelier_block_id") or data.get("id") or data.get("block_id")),
            agent_id=str(metadata.get("atelier_agent_id") or agent_id),
            label=str(data.get("label", "")),
            value=str(data.get("value", "")),
            limit_chars=int(data.get("limit", data.get("limit_chars", 8000)) or 8000),
            description=str(metadata.get("atelier_description", data.get("description", ""))),
            read_only=bool(metadata.get("atelier_read_only", data.get("read_only", False))),
            metadata=metadata,
            pinned=_PINNED_TAG in tags,
            deprecated_at=(
                datetime.fromisoformat(str(metadata["atelier_deprecated_at"]))
                if metadata.get("atelier_deprecated_at")
                else None
            ),
            deprecated_by_block_id=(
                str(metadata["atelier_deprecated_by_block_id"])
                if metadata.get("atelier_deprecated_by_block_id")
                else None
            ),
            deprecation_reason=str(metadata.get("atelier_deprecation_reason", "")),
        )

    @staticmethod
    def passage_to_letta(passage: ArchivalPassage) -> dict[str, Any]:
        metadata = {
            "atelier_agent_id": passage.agent_id,
            "atelier_passage_id": passage.id,
            "atelier_dedup_hash": passage.dedup_hash,
            "atelier_source": passage.source,
            "atelier_source_ref": passage.source_ref,
            "atelier_embedding_provenance": passage.embedding_provenance,
        }
        return {
            "agent_id": passage.agent_id,
            "text": passage.text,
            "value": passage.text,
            "tags": passage.tags,
            "metadata": metadata,
        }

    @staticmethod
    def letta_to_passage(data: dict[str, Any], *, agent_id: str) -> ArchivalPassage | None:
        text = str(data.get("text", data.get("value", data.get("content", ""))))
        if not text:
            return None
        metadata = dict(data.get("metadata") or {})
        created_at = data.get("created_at", data.get("timestamp"))
        return ArchivalPassage(
            id=str(metadata.get("atelier_passage_id") or data.get("id") or data.get("passage_id")),
            agent_id=str(metadata.get("atelier_agent_id") or data.get("agent_id") or agent_id),
            text=text,
            embedding=data.get("embedding") if isinstance(data.get("embedding"), list) else None,
            embedding_model=str(data.get("embedding_model", "")),
            embedding_provenance=str(
                metadata.get("atelier_embedding_provenance", data.get("embedding_provenance", "letta"))
            ),
            tags=[str(tag) for tag in data.get("tags", [])],
            source=str(metadata.get("atelier_source", data.get("source", "user"))),  # type: ignore[arg-type]
            source_ref=str(metadata.get("atelier_source_ref", data.get("source_ref", ""))),
            dedup_hash=str(metadata.get("atelier_dedup_hash", data.get("dedup_hash", data.get("id", text)))),
            created_at=(datetime.fromisoformat(str(created_at)) if created_at else datetime.now(UTC)),
        )

    @staticmethod
    def _extract_items(value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        if hasattr(value, "results"):
            maybe_results = value.results
            if isinstance(maybe_results, list):
                return maybe_results
        if hasattr(value, "data"):
            maybe_data = value.data
            if isinstance(maybe_data, list):
                return maybe_data
        if hasattr(value, "model_dump"):
            dumped = value.model_dump()
            if isinstance(dumped, dict):
                for key in ("results", "data", "items"):
                    raw = dumped.get(key)
                    if isinstance(raw, list):
                        return raw
        return []

    @staticmethod
    def _as_mapping(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if hasattr(value, "model_dump"):
            return dict(value.model_dump())
        if hasattr(value, "dict"):
            return dict(value.dict())
        return {
            key: getattr(value, key)
            for key in dir(value)
            if not key.startswith("_") and not callable(getattr(value, key))
        }


class LettaMemoryStore:
    """MemoryStore implementation backed by Letta as the single memory primary."""

    def __init__(
        self,
        root: str | Path,
        *,
        adapter: LettaAdapter | None = None,
        client: Any | None = None,
    ) -> None:
        self._recall_store = SqliteMemoryStore(root)
        self._adapter = adapter or LettaAdapter(client=client)

    def upsert_block(self, block: MemoryBlock, *, actor: str, reason: str = "") -> MemoryBlock:
        _ = (actor, reason)
        data = self._adapter.upsert_block(block)
        return LettaAdapter.letta_to_block(data or LettaAdapter.block_to_letta(block), agent_id=block.agent_id)

    def get_block(self, agent_id: str | None, label: str, *, include_tombstoned: bool = False) -> MemoryBlock | None:
        target_agent = agent_id or "default"
        data = self._adapter.get_block(target_agent, label)
        if data is not None:
            block = LettaAdapter.letta_to_block(data, agent_id=target_agent)
            if block.deprecated_at is not None and not include_tombstoned:
                return None
            return block
        return None

    def list_blocks(
        self, agent_id: str | None, *, include_tombstoned: bool = False, limit: int = 500
    ) -> list[MemoryBlock]:
        target_agent = agent_id or "default"
        blocks = [
            LettaAdapter.letta_to_block(item, agent_id=target_agent) for item in self._adapter.list_blocks(target_agent)
        ]
        if not include_tombstoned:
            blocks = [block for block in blocks if block.deprecated_at is None]
        return blocks[:limit]

    def list_pinned_blocks(self, agent_id: str | None) -> list[MemoryBlock]:
        target_agent = agent_id or "default"
        blocks = [
            LettaAdapter.letta_to_block(item, agent_id=target_agent) for item in self._adapter.list_blocks(target_agent)
        ]
        pinned = [block for block in blocks if block.pinned]
        return [block for block in pinned if block.deprecated_at is None]

    def list_block_history(self, block_id: str, *, limit: int = 50) -> list[MemoryBlockHistory]:
        _ = (block_id, limit)
        return []

    def delete_block(self, block_id: str) -> None:
        self._adapter.delete_block(block_id)

    def tombstone_block(
        self,
        block_id: str,
        *,
        deprecated_by_block_id: str | None = None,
        reason: str = "",
    ) -> None:
        self._adapter.tombstone_block(
            block_id,
            deprecated_by_block_id=deprecated_by_block_id,
            reason=reason,
        )

    def insert_passage(self, passage: ArchivalPassage) -> ArchivalPassage:
        data = self._adapter.insert_archival(passage)
        converted = LettaAdapter.letta_to_passage(
            data or LettaAdapter.passage_to_letta(passage), agent_id=passage.agent_id
        )
        if converted is None:
            return passage
        return converted.model_copy(update={"dedup_hit": False})

    def search_passages(
        self,
        agent_id: str | None,
        query: str,
        *,
        top_k: int = 5,
        tags: list[str] | None = None,
        since: datetime | None = None,
    ) -> list[ArchivalPassage]:
        target_agent = agent_id or "default"
        results = self._adapter.search_archival(
            agent_id=agent_id or "default",
            query=query,
            top_k=top_k,
            tags=tags,
            since=since,
        )
        passages: list[ArchivalPassage] = []
        seen_ids: set[str] = set()
        query_lower = query.lower().strip()
        query_matched = False
        for item in results:
            text = str(item.get("text", item.get("value", item.get("content", ""))))
            if not text:
                continue
            passage = LettaAdapter.letta_to_passage(item, agent_id=target_agent)
            if passage is not None:
                seen_ids.add(passage.id)
                passages.append(passage)
                text_lower = passage.text.lower()
                tags_blob = " ".join(passage.tags).lower()
                if query_lower in text_lower or query_lower in tags_blob:
                    query_matched = True
        if query_lower and (len(passages) < top_k or not query_matched):
            lexical_rows = self._adapter.list_archival(
                agent_id=target_agent,
                tags=tags,
                since=since,
                limit=max(top_k * 20, 200),
            )
            lexical_hits: list[ArchivalPassage] = []
            for row in lexical_rows:
                passage = LettaAdapter.letta_to_passage(row, agent_id=target_agent)
                if passage is None or passage.id in seen_ids:
                    continue
                text_lower = passage.text.lower()
                tags_blob = " ".join(passage.tags).lower()
                if query_lower not in text_lower and query_lower not in tags_blob:
                    continue
                seen_ids.add(passage.id)
                lexical_hits.append(passage)
                if len(lexical_hits) >= top_k:
                    break
            passages = lexical_hits + passages
        return passages[:top_k]

    def list_passages(
        self,
        agent_id: str | None,
        *,
        tags: list[str] | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[ArchivalPassage]:
        rows = self._adapter.list_archival(agent_id=agent_id or "default", tags=tags, since=since, limit=limit)
        passages = [LettaAdapter.letta_to_passage(row, agent_id=agent_id or "default") for row in rows]
        return [passage for passage in passages if passage is not None]

    def record_recall(self, recall: MemoryRecall) -> MemoryRecall:
        if recall.selected_passage_id:
            self._adapter.update_passage_metadata(
                recall.selected_passage_id,
                {"atelier_last_recall_at": recall.created_at.isoformat()},
            )
        return self._recall_store.record_recall(recall)

    def list_recalls(self, agent_id: str | None, *, limit: int = 50) -> list[MemoryRecall]:
        return self._recall_store.list_recalls(agent_id, limit=limit)

    def write_run_frame(self, frame: RunMemoryFrame) -> None:
        self._recall_store.write_run_frame(frame)

    def get_run_frame(self, session_id: str) -> RunMemoryFrame | None:
        return self._recall_store.get_run_frame(session_id)


__all__ = ["LettaAdapter", "LettaMemoryStore"]
