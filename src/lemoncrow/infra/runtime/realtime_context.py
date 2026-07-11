"""Realtime context minimization for next-call prompting.

This manager continuously ingests runtime signals (tool calls/results,
bash outputs, prompt/response snippets), compresses them, and persists
an always-ready next-call context pack.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class RealtimeContextManager:
    """Rolling context compressor with persistent state on disk."""

    _SALIENT_RE = re.compile(
        r"error|exception|traceback|fail|failed|warning|blocked|missing|assert|timeout|retry",
        re.IGNORECASE,
    )

    def __init__(
        self,
        root: Path,
        *,
        max_items: int = 120,
        prompt_budget_tokens: int = 1500,
    ) -> None:
        self._root = Path(root)
        self._path = self._root / "runtime" / "realtime_context.json"
        self._max_items = max_items
        self._budget_chars = max(1000, prompt_budget_tokens * 4)
        self._state = self._load()

    def record_tool_input(self, tool_name: str, args: dict[str, Any]) -> None:
        self._append(
            kind="tool_input",
            title=tool_name,
            raw=_safe_json(args),
            compact=self._compact_struct(args),
            tags=["tool", tool_name],
        )

    def record_tool_output(self, tool_name: str, result: dict[str, Any]) -> None:
        self._append(
            kind="tool_output",
            title=tool_name,
            raw=_safe_json(result),
            compact=self._compact_struct(result),
            tags=["tool", tool_name],
        )

    def record_tool_error(self, tool_name: str, error: str) -> None:
        compact = self._compact_text(error)
        self._append(
            kind="tool_error",
            title=tool_name,
            raw=error,
            compact=compact,
            tags=["tool", tool_name, "error"],
        )

    def record_prompt_response(self, prompt: str, response: str | None = None) -> None:
        compact = {
            "prompt": self._compact_text(prompt, max_chars=700),
            "response": self._compact_text(response or "", max_chars=900),
        }
        self._append(
            kind="llm_turn",
            title="prompt_response",
            raw=_safe_json({"prompt": prompt, "response": response or ""}),
            compact=compact,
            tags=["llm"],
        )

    def record_bash_output(
        self,
        command: str,
        *,
        stdout: str = "",
        stderr: str = "",
        ok: bool = True,
    ) -> None:
        compact = {
            "command": command,
            "ok": ok,
            "stdout": self._compact_text(stdout, max_chars=900),
            "stderr": self._compact_text(stderr, max_chars=700),
        }
        self._append(
            kind="bash_result",
            title=command[:120],
            raw=_safe_json({"command": command, "ok": ok, "stdout": stdout, "stderr": stderr}),
            compact=compact,
            tags=["bash", "error" if not ok else "ok"],
        )

    def snapshot(self) -> dict[str, Any]:
        self._prune()
        items = list(self._state.get("items", []))
        raw_chars = sum(int(i.get("raw_chars", 0)) for i in items)
        compact_chars = sum(int(i.get("compact_chars", 0)) for i in items)
        reduction_pct = round((1 - compact_chars / max(1, raw_chars)) * 100.0, 1)

        prompt_lines = ["## Realtime compact context"]
        for item in items[-18:]:
            prompt_lines.append(f"[{item.get('kind')}] {item.get('title')}")
            prompt_lines.append(_safe_json(item.get("compact")))

        errors = [
            i for i in items if "error" in set(i.get("tags", [])) or i.get("kind") in ("tool_error", "bash_result")
        ]
        latest_error = errors[-1]["title"] if errors else None

        return {
            "items": len(items),
            "raw_chars": raw_chars,
            "compact_chars": compact_chars,
            "reduction_pct": reduction_pct,
            "latest_error": latest_error,
            "prompt_block": "\n".join(prompt_lines)[:8000],
        }

    def persist(self) -> None:
        self._prune()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": _now_iso(),
            "items": self._state.get("items", []),
            "snapshot": self.snapshot(),
        }
        self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _append(
        self,
        *,
        kind: str,
        title: str,
        raw: str,
        compact: Any,
        tags: list[str],
    ) -> None:
        item = {
            "ts": _now_iso(),
            "kind": kind,
            "title": title[:180],
            "raw_chars": len(raw),
            "compact_chars": len(_safe_json(compact)),
            "compact": compact,
            "tags": tags,
        }
        self._state.setdefault("items", []).append(item)
        self._prune()

    def _prune(self) -> None:
        items = self._state.setdefault("items", [])
        if len(items) > self._max_items:
            self._state["items"] = items[-self._max_items :]
            items = self._state["items"]

        while sum(int(i.get("compact_chars", 0)) for i in items) > self._budget_chars and len(items) > 8:
            # Prefer dropping the oldest low-signal item first.
            drop_idx = 0
            for idx, item in enumerate(items):
                tags = set(item.get("tags", []))
                if "error" not in tags and item.get("kind") not in ("tool_error", "bash_result"):
                    drop_idx = idx
                    break
            items.pop(drop_idx)

    def _load(self) -> dict[str, Any]:
        if not self._path.is_file():
            return {"items": []}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            items = payload.get("items", [])
            if isinstance(items, list):
                return {"items": items}
        except Exception:
            logging.exception("Recovered from broad exception handler")
            logger.warning(
                "Suppressed exception at realtime_context.py:187",
                exc_info=True,
            )
        return {"items": []}

    def _compact_text(self, text: str, *, max_chars: int = 1200) -> str:
        value = "".join(text or "")
        value = value.replace("\r\n", "\n")
        lines = [ln.rstrip() for ln in value.split("\n") if ln.strip()]
        if not lines:
            return ""

        salient = [ln for ln in lines if self._SALIENT_RE.search(ln)]
        chosen: list[str] = []
        if salient:
            chosen.extend(salient[:8])
        chosen.extend(lines[:2])
        chosen.extend(lines[-2:])

        deduped: list[str] = []
        for line in chosen:
            if line not in deduped:
                deduped.append(line)

        out = "\n".join(deduped)
        if len(out) <= max_chars:
            return out
        head = out[: max_chars // 2]
        tail = out[-max_chars // 2 :]
        return f"{head}\n...<trimmed>...\n{tail}"

    def _compact_struct(self, value: Any) -> Any:
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for k, v in list(value.items())[:40]:
                out[str(k)] = self._compact_struct(v)
            return out
        if isinstance(value, list):
            return [self._compact_struct(v) for v in value[:20]]
        if isinstance(value, str):
            return self._compact_text(value)
        return value


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return str(value)
