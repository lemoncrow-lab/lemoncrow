"""Pre-embed all gold query strings into the LemonCrow vector cache.

Run with system python3 (has torch + sentence_transformers):
    python3 benchmarks/codebench/prewarm_query_cache.py

After this, every embed_query() call during the retrieval eval hits the cache
and returns in microseconds instead of 200-500ms GPU inference.
"""

from __future__ import annotations

import json
import sqlite3
import sys  # noqa: F401
import time
from hashlib import sha256
from pathlib import Path

GOLD_FILES = [
    "benchmarks/codebench/data/bench_pairs_def_gold.json",
    "benchmarks/codebench/data/bench_pairs_content_gold.json",
    "benchmarks/codebench/data/bench_pairs_semantic_gold.json",
]
EMBEDDER_NAME = "bge:BAAI/bge-code-v1"
MODEL_NAME = "BAAI/bge-code-v1"
STORE_ROOT = Path.home() / ".lemoncrow"
CACHE_DB = STORE_ROOT / "vector_cache.sqlite"
BATCH_SIZE = 128


def _cache_key(query: str) -> str:
    content = f"{EMBEDDER_NAME}:{query.strip().lower()}"
    digest = sha256(content.encode("utf-8")).hexdigest()
    return f"code-search-query:{digest}"


def _load_queries() -> list[str]:
    seen: set[str] = set()
    queries: list[str] = []
    for gf in GOLD_FILES:
        try:
            data = json.loads(Path(gf).read_text())
        except FileNotFoundError:
            print(f"WARNING: {gf} not found", flush=True)
            continue
        for pair in data.get("pairs", []):
            q = (pair[0] if isinstance(pair, list) else pair.get("query", "")).strip()
            if q and q not in seen:
                seen.add(q)
                queries.append(q)
    return queries


def _already_cached(conn: sqlite3.Connection, keys: list[str]) -> set[str]:
    rows = conn.execute(
        f"SELECT cache_key FROM playbook_embedding_cache WHERE embedder_name = ?"
        f" AND cache_key IN ({','.join('?' * len(keys))})",
        [EMBEDDER_NAME, *keys],
    ).fetchall()
    return {r[0] for r in rows}


def main() -> None:
    import torch
    from sentence_transformers import SentenceTransformer

    queries = _load_queries()
    print(f"{len(queries):,} unique queries loaded", flush=True)

    # Sort by length for bucket batching (minimal padding waste)
    queries.sort(key=len)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cuda":
        free_gb = torch.cuda.mem_get_info()[0] / 1024**3
        print(f"  device=cuda  free_vram={free_gb:.1f}GB", flush=True)
    model = SentenceTransformer(MODEL_NAME, device=device)
    if device == "cuda":
        model = model.half()

    STORE_ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CACHE_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS playbook_embedding_cache (
            cache_key    TEXT NOT NULL,
            embedder_name TEXT NOT NULL,
            vector_json  TEXT NOT NULL,
            PRIMARY KEY (cache_key, embedder_name)
        )
    """)
    conn.commit()

    keys = [_cache_key(q) for q in queries]
    already = _already_cached(conn, keys)
    pending_pairs = [(q, k) for q, k in zip(queries, keys, strict=False) if k not in already]
    print(f"  {len(already):,} already cached, {len(pending_pairs):,} to embed", flush=True)
    if not pending_pairs:
        print("All queries already cached — nothing to do.", flush=True)
        conn.close()
        return

    pending_queries = [p[0] for p in pending_pairs]
    pending_keys = [p[1] for p in pending_pairs]

    t0 = time.perf_counter()
    inserted = 0
    i = 0
    while i < len(pending_queries):
        # Dynamic batch: token budget keeps GPU fed without OOM
        est_tokens = max(1, len(pending_queries[i]) // 4)
        bs = max(4, min(512, (BATCH_SIZE * 128) // est_tokens))
        batch_q = pending_queries[i : i + bs]
        batch_k = pending_keys[i : i + bs]
        vecs = model.encode(batch_q, batch_size=len(batch_q), normalize_embeddings=True, show_progress_bar=False)
        rows = [(k, EMBEDDER_NAME, json.dumps(v.tolist())) for k, v in zip(batch_k, vecs, strict=False)]
        conn.executemany(
            "INSERT OR REPLACE INTO playbook_embedding_cache (cache_key, embedder_name, vector_json) VALUES (?,?,?)",
            rows,
        )
        conn.commit()
        inserted += len(batch_q)
        i += bs
        elapsed = time.perf_counter() - t0
        rate = inserted / elapsed
        eta = (len(pending_queries) - inserted) / rate if rate else 0
        print(f"  {inserted:,}/{len(pending_queries):,}  {rate:.0f}/s  eta={eta:.0f}s", flush=True)

    conn.close()
    print(f"Done — {inserted:,} queries cached in {time.perf_counter() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
