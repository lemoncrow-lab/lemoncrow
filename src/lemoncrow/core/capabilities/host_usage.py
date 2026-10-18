"""Host usage ingestion shared by Stop and idle hooks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.paths import session_dir
from lemoncrow.core.foundation.run_file_io import RunFileLock, atomic_write_json


def persist_host_usage(
    root: str | Path, host: str, session_id: str, calls: list[dict[str, Any]], *, workspace: str = ""
) -> None:
    """Persist a complete usage snapshot; rescanning never adds duplicate calls."""
    if not session_id or session_id == "default" or not calls:
        return
    from lemoncrow.core.capabilities.pricing import get_model_pricing

    priced = []
    for original in calls:
        call = dict(original)
        card = get_model_pricing(str(call.get("model") or ""))
        if call.get("cost_usd") is None and card.known:
            call["cost_usd"] = round(
                card.request_cost_usd(
                    input_tokens=int(call.get("input_tokens") or 0),
                    output_tokens=int(call.get("output_tokens") or 0) + int(call.get("thinking_tokens") or 0),
                    cache_read_tokens=int(call.get("cache_read_tokens") or 0),
                    cache_write_tokens=int(call.get("cache_write_tokens") or 0),
                ),
                8,
            )
        call["usage_source"] = host
        priced.append(call)
    directory = session_dir(root, host, session_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "run.json"
    with RunFileLock(path):
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        cost = dict(data.get("cost") or {})
        internal = [
            call
            for call in cost.get("calls", [])
            if not call.get("usage_source")
            and not str(call.get("operation") or "").startswith((host + ".", "imported."))
        ]
        cost["calls"] = internal + priced
        cost["total_cost_usd"] = round(sum(float(call.get("cost_usd") or 0) for call in cost["calls"]), 6)
        unchanged = cost == data.get("cost") and (not workspace or workspace == data.get("workspace_path"))
        data["cost"] = cost
        data.setdefault("session_id", session_id)
        data.setdefault("agent", host)
        data.setdefault("status", "running")
        data.setdefault("events", [])
        data.setdefault("created_at", str(priced[0].get("at") or datetime.now(UTC).isoformat()))
        if not unchanged:
            data["updated_at"] = str(priced[-1].get("at") or datetime.now(UTC).isoformat())
            if workspace:
                data["workspace_path"] = workspace
            atomic_write_json(path, data)

    # Evidence of usage, with no fabricated token/call savings credit.
    sidecar = directory / "savings.jsonl"
    signature = hashlib.sha256(json.dumps(priced, sort_keys=True).encode()).hexdigest()
    with RunFileLock(sidecar):
        previous: dict[str, Any] = {}
        if sidecar.exists():
            for line in sidecar.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("kind") == "session_end":
                    previous = row
        if previous.get("usage_signature") == signature:
            return
        row = {
            "kind": "session_end",
            "model": priced[-1].get("model", ""),
            "ts": priced[-1].get("at") or datetime.now(UTC).isoformat(),
            "turns": len(priced),
            "usage_signature": signature,
            "carry_usd": previous.get("carry_usd", 0),
            "carry_tokens": previous.get("carry_tokens", 0),
        }
        if all(call.get("cost_usd") is not None for call in priced):
            row["est_cost_usd"] = round(sum(float(call["cost_usd"]) for call in priced), 6)
        with sidecar.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")


def opencode_usage_calls(session_id: str, db_path: Path, *, host: str = "opencode") -> list[dict[str, Any]]:
    """Read measured step-finish usage without loading any tool output."""
    if not db_path.is_file():
        return []
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            """SELECT p.id, p.time_created, p.data, m.data
               FROM part p JOIN message m ON p.message_id = m.id
               WHERE p.session_id = ? AND json_extract(p.data, '$.type') = 'step-finish'
               ORDER BY p.time_created, p.id""",
            (session_id,),
        ).fetchall()
    calls = []
    for source_id, timestamp, raw_part, raw_message in rows:
        part, message = json.loads(raw_part), json.loads(raw_message)
        if message.get("role") != "assistant":
            continue
        tokens = part.get("tokens") or {}
        cache = tokens.get("cache") or {}
        model = str(message.get("modelID") or message.get("model") or "")
        provider = str(message.get("providerID") or "")
        call = {
            "source_id": source_id,
            "operation": host + ".step_finish",
            "model": f"{provider}/{model}" if provider else model,
            "at": datetime.fromtimestamp(float(timestamp) / 1000, tz=UTC).isoformat(),
            "input_tokens": int(tokens.get("input") or 0),
            "output_tokens": int(tokens.get("output") or 0),
            "cache_read_tokens": int(cache.get("read") or 0),
            "cache_write_tokens": int(cache.get("write") or 0),
            "thinking_tokens": int(tokens.get("reasoning") or 0),
        }
        if isinstance(part.get("cost"), (int, float)):
            call["cost_usd"] = float(part["cost"])
        if any(call[key] > 0 for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")):
            calls.append(call)
    return calls
