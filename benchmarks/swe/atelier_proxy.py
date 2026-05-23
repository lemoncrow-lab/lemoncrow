"""Atelier context-compression proxy for SWE-bench evaluation.

Exposes an OpenAI-compatible `/v1/chat/completions` endpoint. Each request:
  1. Applies Atelier's ``compress_history()`` to the incoming messages.
  2. Records token savings to a JSONL log file.
  3. Forwards the compressed request to the upstream Ollama/OpenAI endpoint.
  4. Returns the upstream response unchanged.

Usage::

    uv run python benchmarks/swe/atelier_proxy.py \\
        --upstream http://localhost:11434/v1 \\
        --port 11435 \\
        --log benchmarks/swe/outputs/proxy_savings.jsonl

The proxy reports cumulative savings on shutdown (CTRL+C).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import httpx

# ---------------------------------------------------------------------------
# Import Atelier compression from the installed package
# ---------------------------------------------------------------------------
# Ensure project root is on sys.path so the local src/ install is found.
_repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_repo_root / "src"))

from atelier.core.capabilities.tool_supervision.compact_output import (  # noqa: E402
    compress_history,
    TokenSavingStats,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("atelier-proxy")

app = FastAPI(title="Atelier Compression Proxy")

# ---------------------------------------------------------------------------
# Global state (initialised by main)
# ---------------------------------------------------------------------------
_upstream: str = "http://localhost:11434/v1"
_log_path: Path | None = None
_stats = TokenSavingStats()
_start_time: float = 0.0


def _tok(text: str) -> int:
    """Rough token count: chars / 4 (GPT-style approximation)."""
    return max(1, len(text) // 4)


def _messages_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        total += _tok(str(content))
    return total


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages: list[dict] = body.get("messages", [])

    # ---- compress -----------------------------------------------------------
    before_tokens = _messages_tokens(messages)
    compressed = compress_history(messages, keep_recent=2, stats=_stats)
    after_tokens = _messages_tokens(compressed)
    saved = before_tokens - after_tokens

    # ---- log ----------------------------------------------------------------
    entry = {
        "ts": time.time(),
        "before_tokens": before_tokens,
        "after_tokens": after_tokens,
        "saved": saved,
        "pct": round(100 * saved / max(1, before_tokens), 1),
        "model": body.get("model", ""),
    }
    log.info(
        "compress  before=%d  after=%d  saved=%d (%.1f%%)",
        before_tokens, after_tokens, saved, entry["pct"],
    )
    if _log_path:
        with _log_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    # ---- forward ------------------------------------------------------------
    body["messages"] = compressed
    upstream_url = f"{_upstream.rstrip('/')}/chat/completions"

    async with httpx.AsyncClient(timeout=300) as client:
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ("host", "content-length")}

        if body.get("stream"):
            async def stream_gen():
                async with client.stream("POST", upstream_url, json=body, headers=headers) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
            return StreamingResponse(stream_gen(), media_type="text/event-stream")
        else:
            resp = await client.post(upstream_url, json=body, headers=headers)
            return resp.json()


@app.get("/v1/models")
async def list_models(request: Request):
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{_upstream.rstrip('/')}/models")
        return resp.json()


def _summary():
    elapsed = time.time() - _start_time
    print("\n" + "=" * 60)
    print("ATELIER PROXY — SESSION SUMMARY")
    print("=" * 60)
    print(f"  Elapsed       : {elapsed:.0f}s")
    print(f"  Compressions  : {_stats.compressions}")
    print(f"  Chars saved   : {_stats.chars_saved:,}")
    print(f"  Est. tokens   : ~{_stats.chars_saved // 4:,}")
    print(f"  Ratio         : {_stats.compression_ratio:.1%}")
    print("=" * 60)
    if _log_path:
        print(f"  Full log      : {_log_path}")


def main():
    global _upstream, _log_path, _start_time

    parser = argparse.ArgumentParser(description="Atelier compression proxy")
    parser.add_argument("--upstream", default="http://localhost:11434/v1")
    parser.add_argument("--port", type=int, default=11435)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--log", default=None, help="Path to JSONL savings log")
    args = parser.parse_args()

    _upstream = args.upstream
    if args.log:
        _log_path = Path(args.log)
        _log_path.parent.mkdir(parents=True, exist_ok=True)

    _start_time = time.time()
    log.info("Atelier proxy listening on %s:%d → %s", args.host, args.port, _upstream)

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        _summary()


if __name__ == "__main__":
    main()
