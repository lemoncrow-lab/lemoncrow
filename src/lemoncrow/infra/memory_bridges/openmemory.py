"""OpenMemory-backed MemoryStore implementation."""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from lemoncrow.core.foundation.memory_models import (
    ArchivalPassage,
    MemoryBlock,
    MemoryBlockHistory,
    MemoryRecall,
    RunMemoryFrame,
)
from lemoncrow.gateway.integrations import openmemory as openmemory_bridge
from lemoncrow.infra.memory_bridges.base import MemorySyncResult
from lemoncrow.infra.storage.memory_store import MemorySidecarUnavailable
from lemoncrow.infra.storage.sqlite_memory_store import MEMORY_DB_NAME, SqliteMemoryStore

_BLOCK_KIND = "lemoncrow_block"
_PASSAGE_KIND = "lemoncrow_passage"
_PINNED_TAG = "lemoncrow:pinned"
_T = TypeVar("_T")


def _sidecar_error(exc: Exception) -> MemorySidecarUnavailable:
    return MemorySidecarUnavailable(f"OpenMemory sidecar unavailable: {exc}")


class OpenMemoryAdapter:
    source = "openmemory"

    def __init__(self, *, client: openmemory_bridge.OpenMemoryClient | None = None) -> None:
        self.client = client or openmemory_bridge.get_client()

    def fetch_context(self, *, task: str, project_id: str | None = None) -> MemorySyncResult:
        result = self._call_bridge(openmemory_bridge.maybe_fetch_memory_context_for_task, task, project_id)
        data = result.get("data", {})
        context = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)
        return MemorySyncResult(
            ok=bool(result.get("ok", False)),
            skipped=bool(result.get("skipped", False)),
            source=self.source,
            context=context,
            detail=str(result.get("reason", "")),
        )

    def push_procedural_lesson(self, *, trace_id: str, memory_id: str) -> MemorySyncResult:
        result = self._call_bridge(openmemory_bridge.maybe_store_memory_pointer, trace_id, memory_id)
        return MemorySyncResult(
            ok=bool(result.get("ok", False)),
            skipped=bool(result.get("skipped", False)),
            source=self.source,
            detail=str(result.get("reason", "")),
        )

    def _call_bridge(self, fn: Callable[..., _T], *args: Any) -> _T:
        previous_client = openmemory_bridge._CLIENT
        openmemory_bridge._CLIENT = self.client
        try:
            return fn(*args)
        finally:
            openmemory_bridge._CLIENT = previous_client

    def add_memory(
        self,
        *,
        text: str,
        user_id: str,
        metadata: dict[str, Any],
        infer: bool = False,
        force_rest: bool = False,
    ) -> Any:
        tool_payload = {
            "messages": [{"role": "system", "content": text}],
            "user_id": user_id,
            "metadata": metadata,
            "infer": infer,
        }
        if force_rest:
            try:
                return self._rest_add_memory(text=text, user_id=user_id, metadata=metadata, infer=infer)
            except Exception as exc:
                raise _sidecar_error(exc) from exc
        try:
            payload = self.client.call_tool("add_memories", tool_payload)
            if payload not in ({}, None):
                return payload
        except Exception as exc:
            logging.exception("Recovered from broad exception handler")
            last_error = exc
        else:
            last_error = RuntimeError("empty add_memories response")
        try:
            return self._rest_add_memory(text=text, user_id=user_id, metadata=metadata, infer=infer)
        except Exception as exc:
            if isinstance(last_error, Exception):
                raise _sidecar_error(RuntimeError(f"{last_error}; fallback failed: {exc}")) from exc
            raise _sidecar_error(exc) from exc

    def search_memories(self, *, query: str, user_id: str, limit: int) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        try:
            payload = self.client.call_tool(
                "search_memory",
                {
                    "query": query,
                    "user_id": user_id,
                    "limit": limit,
                },
            )
        except Exception as exc:
            logging.exception("Recovered from broad exception handler")
            last_error = exc
            payload = None
        else:
            last_error = None
        rows = openmemory_bridge._coerce_memory_list(payload)
        if rows:
            return rows
        if payload in ({}, None):
            try:
                rows = self._rest_list_memories(user_id=user_id, limit=limit, search_query=query)
                broad_rows = self._rest_list_memories(user_id=user_id, limit=max(limit * 20, 200))
                merged = rows + [row for row in broad_rows if row not in rows]
                ranked = self._rank_rows_by_query(merged, query=query, limit=max(limit * 20, 200))
                if ranked:
                    return ranked
                return rows
            except Exception as exc:
                if last_error is not None:
                    raise _sidecar_error(last_error) from exc
                raise _sidecar_error(exc) from exc
        return rows

    def list_memories(self, *, user_id: str, limit: int) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        payload: dict[str, Any] | None = None
        try:
            payload = self.client.call_tool(
                "list_memories",
                {
                    "user_id": user_id,
                    "limit": limit,
                },
            )
        except Exception as exc:
            logging.exception("Recovered from broad exception handler")
            last_error = exc
            payload = None
        else:
            last_error = None
        rows = openmemory_bridge._coerce_memory_list(payload)
        if rows:
            return rows
        if payload in ({}, None):
            try:
                return self._rest_list_memories(user_id=user_id, limit=limit)
            except Exception as exc:
                if last_error is not None:
                    raise _sidecar_error(last_error) from exc
                raise _sidecar_error(exc) from exc
        return rows

    def _rest_add_memory(self, *, text: str, user_id: str, metadata: dict[str, Any], infer: bool) -> Any:
        return self._rest_json(
            method="POST",
            path="/api/v1/memories/",
            timeout_seconds=60 if infer else None,
            payload={
                "user_id": user_id,
                "text": text,
                "metadata": metadata,
                "infer": infer,
                "app": "lemoncrow",
            },
        )

    def _rest_list_memories(self, *, user_id: str, limit: int, search_query: str | None = None) -> list[dict[str, Any]]:
        page_size = max(1, min(limit, 100))
        params: dict[str, str] = {
            "user_id": user_id,
            "size": str(page_size),
            "sort_column": "created_at",
            "sort_direction": "desc",
        }
        if search_query:
            params["search_query"] = search_query
        payload = self._rest_json(method="GET", path="/api/v1/memories/", params=params)
        return openmemory_bridge._coerce_memory_list(payload)

    def _rest_json(
        self,
        *,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
    ) -> Any:
        base_url = str(getattr(self.client, "base_url", "") or os.environ.get("LEMONCROW_OPENMEMORY_URL", "")).rstrip(
            "/"
        )
        if not base_url:
            raise RuntimeError("LEMONCROW_OPENMEMORY_URL not set")
        url = f"{base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        timeout = int(timeout_seconds or getattr(self.client, "timeout", 15) or 15)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url=url,
            data=body,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
        data = json.loads(raw) if raw else {}
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(str(data["error"]))
        return data

    def _rank_rows_by_query(self, rows: list[dict[str, Any]], *, query: str, limit: int) -> list[dict[str, Any]]:
        query_tokens = self._normalize_tokens(query)
        if not query_tokens:
            return rows[:limit]
        ranked: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            text = str(row.get("content", row.get("text", row.get("memory", ""))))
            metadata = row.get("metadata_") if isinstance(row.get("metadata_"), dict) else {}
            haystack = f"{text} {json.dumps(metadata, ensure_ascii=False)}"
            haystack_tokens = self._normalize_tokens(haystack)
            if not haystack_tokens:
                continue
            overlap = len(query_tokens.intersection(haystack_tokens))
            if overlap == 0:
                continue
            score = overlap / max(len(query_tokens), 1)
            qnorm = " ".join(sorted(query_tokens))
            hnorm = " ".join(sorted(haystack_tokens))
            if qnorm and qnorm in hnorm:
                score += 0.5
            ranked.append((score, row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [row for _, row in ranked[:limit]]

    @staticmethod
    def _normalize_tokens(value: str) -> set[str]:
        raw = [tok for tok in re.split(r"[^a-z0-9]+", value.lower()) if tok]
        synonyms = {
            "db": "database",
            "prod": "production",
            "case": "scenario",
            "cases": "scenario",
            "better": "improves",
        }
        normalized: set[str] = set()
        for token in raw:
            if len(token) <= 1 and not token.isdigit():
                continue
            normalized.add(synonyms.get(token, token))
        return normalized


class OpenMemoryMemoryStore:
    """MemoryStore implementation backed by OpenMemory as the primary."""

    def __init__(
        self,
        root: str | Path,
        *,
        adapter: OpenMemoryAdapter | None = None,
        client: openmemory_bridge.OpenMemoryClient | None = None,
    ) -> None:
        self._recall_store = SqliteMemoryStore(root, db_name=MEMORY_DB_NAME)
        self._adapter = adapter or OpenMemoryAdapter(client=client)
        self._user_id = getattr(self._adapter.client, "user_id", None) or os.environ.get(
            "LEMONCROW_OPENMEMORY_USER_ID", ""
        )
        self._user_id = self._user_id.strip() or os.environ.get("USER", "").strip() or "lemoncrow"

    @property
    def root(self) -> Path:
        return self._recall_store.root

    @property
    def db_path(self) -> Path:
        return self._recall_store.db_path

    def upsert_block(self, block: MemoryBlock, *, actor: str, reason: str = "") -> MemoryBlock:
        _ = actor
        existing = self.get_block(block.agent_id, block.label, include_tombstoned=True)
        stored = block
        if existing is not None:
            stored = block.model_copy(
                update={
                    "id": existing.id,
                    "created_at": existing.created_at,
                    "updated_at": datetime.now(UTC),
                    "version": existing.version + 1 if existing.value != block.value else existing.version,
                }
            )
        metadata: dict[str, Any] = {
            "lemoncrow_kind": _BLOCK_KIND,
            "lemoncrow_agent_id": stored.agent_id,
            "lemoncrow_block_id": stored.id,
            "lemoncrow_label": stored.label,
            "lemoncrow_reason": reason,
        }
        if stored.pinned:
            metadata["lemoncrow_pinned"] = True
        self._adapter.add_memory(
            text=self._serialize({"lemoncrow_kind": _BLOCK_KIND, "block": stored.model_dump(mode="json")}),
            user_id=self._user_id,
            metadata=metadata,
        )
        return stored

    def get_block(self, agent_id: str | None, label: str, *, include_tombstoned: bool = False) -> MemoryBlock | None:
        target_agent = agent_id or "default"
        blocks = [
            block
            for block in self.list_blocks(target_agent, include_tombstoned=True, limit=500)
            if block.label == label
        ]
        if not blocks:
            return None
        block = blocks[0]
        if block.deprecated_at is not None and not include_tombstoned:
            return None
        return block

    def list_blocks(
        self,
        agent_id: str | None,
        *,
        include_tombstoned: bool = False,
        limit: int = 500,
    ) -> list[MemoryBlock]:
        target_agent = agent_id or "default"
        rows = self._adapter.list_memories(user_id=self._user_id, limit=max(limit * 4, 200))
        latest: dict[str, MemoryBlock] = {}
        for row in rows:
            block = self._row_to_block(row, agent_id=target_agent)
            if block is None:
                continue
            if block.agent_id != target_agent:
                continue
            current = latest.get(block.label)
            if current is None or (block.updated_at, block.version) > (current.updated_at, current.version):
                latest[block.label] = block
        blocks = sorted(latest.values(), key=lambda item: item.updated_at, reverse=True)
        if not include_tombstoned:
            blocks = [block for block in blocks if block.deprecated_at is None]
        return blocks[:limit]

    def list_pinned_blocks(self, agent_id: str | None) -> list[MemoryBlock]:
        return [block for block in self.list_blocks(agent_id, include_tombstoned=False, limit=500) if block.pinned]

    def list_block_history(self, block_id: str, *, limit: int = 50) -> list[MemoryBlockHistory]:
        _ = (block_id, limit)
        return []

    def delete_block(self, block_id: str) -> None:
        self.tombstone_block(block_id, reason="deleted")

    def tombstone_block(
        self,
        block_id: str,
        *,
        deprecated_by_block_id: str | None = None,
        reason: str = "",
    ) -> None:
        rows = self._adapter.list_memories(user_id=self._user_id, limit=1000)
        target = next(
            (
                block
                for row in rows
                for block in [self._row_to_block(row, agent_id="default")]
                if block is not None and block.id == block_id
            ),
            None,
        )
        if target is None:
            return
        tombstoned = target.model_copy(
            update={
                "deprecated_at": datetime.now(UTC),
                "deprecated_by_block_id": deprecated_by_block_id,
                "deprecation_reason": reason,
                "updated_at": datetime.now(UTC),
                "version": target.version + 1,
            }
        )
        self.upsert_block(tombstoned, actor="openmemory", reason=reason)

    def insert_passage(self, passage: ArchivalPassage) -> ArchivalPassage:
        metadata = {
            "lemoncrow_kind": _PASSAGE_KIND,
            "lemoncrow_agent_id": passage.agent_id,
            "lemoncrow_passage_id": passage.id,
            "lemoncrow_dedup_hash": passage.dedup_hash,
            "lemoncrow_source": passage.source,
            "lemoncrow_source_ref": passage.source_ref,
            "lemoncrow_tags": list(passage.tags),
        }
        self._adapter.add_memory(
            text=passage.text,
            user_id=self._user_id,
            metadata=metadata,
            force_rest=bool(
                getattr(self._adapter.client, "base_url", "") or os.environ.get("LEMONCROW_OPENMEMORY_URL")
            ),
        )
        return passage.model_copy(update={"dedup_hit": False})

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
        rows = self._adapter.search_memories(query=query, user_id=self._user_id, limit=max(top_k * 4, 20))
        passages = self._filter_passages(rows, agent_id=target_agent, tags=tags, since=since, preserve_order=True)
        if len(passages) < top_k:
            rows = self._adapter.list_memories(user_id=self._user_id, limit=max(top_k * 8, 200))
            passages = self._filter_passages(rows, agent_id=target_agent, tags=tags, since=since, query=query)
        return passages[:top_k]

    def list_passages(
        self,
        agent_id: str | None,
        *,
        tags: list[str] | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[ArchivalPassage]:
        target_agent = agent_id or "default"
        rows = self._adapter.list_memories(user_id=self._user_id, limit=max(limit * 4, 200))
        return self._filter_passages(rows, agent_id=target_agent, tags=tags, since=since)[:limit]

    def record_recall(self, recall: MemoryRecall) -> MemoryRecall:
        return self._recall_store.record_recall(recall)

    def list_recalls(self, agent_id: str | None, *, limit: int = 50) -> list[MemoryRecall]:
        return self._recall_store.list_recalls(agent_id, limit=limit)

    def write_run_frame(self, frame: RunMemoryFrame) -> None:
        self._recall_store.write_run_frame(frame)

    def get_run_frame(self, session_id: str) -> RunMemoryFrame | None:
        return self._recall_store.get_run_frame(session_id)

    def _filter_passages(
        self,
        rows: list[dict[str, Any]],
        *,
        agent_id: str,
        tags: list[str] | None,
        since: datetime | None,
        query: str | None = None,
        preserve_order: bool = False,
    ) -> list[ArchivalPassage]:
        passages: list[ArchivalPassage] = []
        seen: set[str] = set()
        query_lower = query.lower() if query else ""
        for row in rows:
            passage = self._row_to_passage(row, agent_id=agent_id)
            if passage is None or passage.id in seen:
                continue
            if passage.agent_id != agent_id:
                continue
            if tags and not set(tags).issubset(set(passage.tags)):
                continue
            if since is not None and passage.created_at < since:
                continue
            if query_lower and query_lower not in passage.text.lower():
                metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                if not metadata and isinstance(row.get("metadata_"), dict):
                    metadata = row.get("metadata_")
                if query_lower not in json.dumps(metadata, ensure_ascii=False).lower():
                    continue
            seen.add(passage.id)
            passages.append(passage)
        if not preserve_order:
            passages.sort(key=lambda item: item.created_at, reverse=True)
        return passages

    @staticmethod
    def _serialize(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _parse_payload(row: dict[str, Any]) -> dict[str, Any] | None:
        text = str(row.get("memory", row.get("text", row.get("content", ""))))
        if not text:
            return None
        try:
            payload = json.loads(text)
        except Exception:
            logging.exception("Recovered from broad exception handler")
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    @staticmethod
    def _row_to_block(row: dict[str, Any], *, agent_id: str) -> MemoryBlock | None:
        payload = OpenMemoryMemoryStore._parse_payload(row)
        if payload and payload.get("lemoncrow_kind") == _BLOCK_KIND:
            raw = payload.get("block")
            if not isinstance(raw, dict):
                return None
        else:
            raw_metadata = row.get("metadata")
            metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
            if not metadata:
                raw_metadata_ = row.get("metadata_")
                if isinstance(raw_metadata_, dict):
                    metadata = dict(raw_metadata_)
            if not isinstance(metadata, dict) or metadata.get("lemoncrow_kind") != _BLOCK_KIND:
                return None
            raw = {
                "id": metadata.get("lemoncrow_block_id") or row.get("id"),
                "agent_id": metadata.get("lemoncrow_agent_id") or agent_id,
                "label": metadata.get("lemoncrow_label") or "",
                "value": str(row.get("content", row.get("text", row.get("memory", "")))),
                "metadata": metadata,
            }
        metadata = dict(raw.get("metadata") or {})
        tags = list(metadata.get("tags") or [])
        if raw.get("pinned") and _PINNED_TAG not in tags:
            tags.append(_PINNED_TAG)
            metadata["tags"] = tags
        return MemoryBlock.model_validate(
            {
                **raw,
                "agent_id": raw.get("agent_id") or agent_id,
                "metadata": metadata,
            }
        )

    @staticmethod
    def _row_to_passage(row: dict[str, Any], *, agent_id: str) -> ArchivalPassage | None:
        payload = OpenMemoryMemoryStore._parse_payload(row)
        if payload and payload.get("lemoncrow_kind") == _PASSAGE_KIND:
            raw = payload.get("passage")
            if not isinstance(raw, dict):
                return None
            return ArchivalPassage.model_validate({**raw, "agent_id": raw.get("agent_id") or agent_id})

        raw_metadata = row.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        if not metadata:
            raw_metadata_ = row.get("metadata_")
            if isinstance(raw_metadata_, dict):
                metadata = dict(raw_metadata_)

        if not isinstance(metadata, dict) or metadata.get("lemoncrow_kind") != _PASSAGE_KIND:
            return None

        raw_created_at = row.get("created_at")
        created_at: datetime
        if isinstance(raw_created_at, (int, float)):
            created_at = datetime.fromtimestamp(float(raw_created_at), tz=UTC)
        elif isinstance(raw_created_at, str) and raw_created_at:
            try:
                created_at = datetime.fromisoformat(raw_created_at)
            except ValueError:
                logging.warning("Ignoring malformed OpenMemory created_at: %r", raw_created_at)
                created_at = datetime.now(UTC)
        else:
            created_at = datetime.now(UTC)
        tags: list[str] = []
        if isinstance(row.get("categories"), list):
            tags = [str(tag) for tag in row.get("categories") or []]
        if not tags and isinstance(metadata.get("lemoncrow_tags"), list):
            tags = [str(tag) for tag in metadata.get("lemoncrow_tags") or []]
        return ArchivalPassage(
            id=str(metadata.get("lemoncrow_passage_id") or row.get("id") or ""),
            agent_id=str(metadata.get("lemoncrow_agent_id") or agent_id),
            text=str(row.get("content", row.get("text", row.get("memory", "")))),
            source=str(metadata.get("lemoncrow_source", "user")),  # type: ignore[arg-type]
            source_ref=str(metadata.get("lemoncrow_source_ref", "")),
            dedup_hash=str(metadata.get("lemoncrow_dedup_hash") or row.get("id") or ""),
            tags=tags,
            created_at=created_at,
        )


__all__ = ["OpenMemoryAdapter", "OpenMemoryMemoryStore"]
