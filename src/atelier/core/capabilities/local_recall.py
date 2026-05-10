"""Local transcript recall for Atelier sessions and host transcripts."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

DIM = 256
MAX_SESSIONS = 200
MAX_CHUNK_CHARS = 3000
MIN_SCORE_THRESHOLD = 0.15


def _config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+|`[^`]+`|['\"][^'\"]+['\"]", text.lower())[:8192]


def vectorize(text: str, *, dim: int = DIM) -> list[float]:
    vector = [0.0] * dim
    tokens = _tokenize(text)
    grams: list[tuple[str, float]] = []
    for index, token in enumerate(tokens):
        grams.append((token, 1.0))
        if index + 1 < len(tokens):
            grams.append((token + " " + tokens[index + 1], 1.5))
        if index + 2 < len(tokens):
            grams.append((token + " " + tokens[index + 1] + " " + tokens[index + 2], 1.2))
    for marker in re.findall(r"\b(?:def|class|function|import|export)\s+([A-Za-z_][A-Za-z0-9_]*)", text):
        grams.append(("semantic:" + marker.lower(), 3.0))
    for gram, weight in grams:
        digest = hashlib.sha256(gram.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[bucket] += sign * weight
    norm = math.sqrt(sum(item * item for item in vector)) or 1.0
    return [item / norm for item in vector]


def _cosine(a: list[float], b: list[float]) -> float:
    return max(0.0, sum(x * y for x, y in zip(a, b, strict=False)))


def _chunk_messages(messages: list[str]) -> list[str]:
    chunks: list[str] = []
    current = ""
    for message in messages:
        if "task-notification:" in message:
            continue
        if len(current) + len(message) > MAX_CHUNK_CHARS and current:
            chunks.append(current)
            current = ""
        current += ("\n" if current else "") + message
    if current:
        chunks.append(current[:MAX_CHUNK_CHARS])
    return chunks


def _read_jsonl(path: Path) -> list[str]:
    messages: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            payload = json.loads(line)
        except Exception:
            continue
        content = payload.get("content") or payload.get("message", {}).get("content")
        if isinstance(content, str):
            messages.append(content)
        elif isinstance(content, list):
            texts = [str(item.get("text", "")) for item in content if isinstance(item, dict)]
            if texts:
                messages.append("\n".join(texts))
    return messages


def discover_transcripts(config_dir: str | Path | None = None, *, limit: int = MAX_SESSIONS) -> list[Path]:
    root = Path(config_dir) if config_dir else _config_dir()
    project_dir = root / "projects"
    if not project_dir.exists():
        return []
    files = sorted(project_dir.rglob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    return files[:limit]


def build_index(config_dir: str | Path | None = None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for transcript in discover_transcripts(config_dir):
        chunks = _chunk_messages(_read_jsonl(transcript))
        for idx, chunk in enumerate(chunks):
            entries.append(
                {
                    "transcript": str(transcript),
                    "chunk_index": idx,
                    "content": chunk,
                    "vector": vectorize(chunk),
                }
            )
    return entries


def recall_transcripts(query: str, *, top_k: int = 10, config_dir: str | Path | None = None) -> dict[str, Any]:
    entries = build_index(config_dir)
    if not entries:
        return {
            "isError": False,
            "content": [{"type": "text", "text": "No past sessions found."}],
            "matches": [],
        }
    query_vector = vectorize(query)
    scored = [(_cosine(query_vector, entry["vector"]), entry) for entry in entries]
    matches = [
        entry | {"score": score}
        for score, entry in sorted(scored, key=lambda item: item[0], reverse=True)
        if score >= MIN_SCORE_THRESHOLD
    ][:top_k]
    if not matches:
        return {
            "isError": False,
            "content": [{"type": "text", "text": f"No relevant results for: {query!r}"}],
            "matches": [],
        }
    lines = [f"Recall: {query!r} ({len(entries)} turns indexed)"]
    for idx, match in enumerate(matches, start=1):
        lines.append(f"\n[{idx}] {match['score']:.0%} -- {Path(match['transcript']).name}")
        lines.append(str(match["content"])[:2000])
    return {
        "isError": False,
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "matches": [{k: v for k, v in match.items() if k != "vector"} for match in matches],
    }


__all__ = [
    "DIM",
    "MAX_CHUNK_CHARS",
    "MAX_SESSIONS",
    "MIN_SCORE_THRESHOLD",
    "discover_transcripts",
    "recall_transcripts",
    "vectorize",
]
