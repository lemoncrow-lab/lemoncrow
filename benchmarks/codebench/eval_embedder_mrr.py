"""Canonical embedder MRR benchmark — pure semantic retrieval quality.

Embeds all symbols from each repo's index DB, embeds queries, ranks by
cosine similarity (numpy matmul), scores MRR/hit@1/hit@3 against the
definition and content gold sets.  Apples-to-apples across embedders.

Usage:
    # single embedder
    ATELIER_CODE_EMBEDDER=nomic  uv run python benchmarks/codebench/eval_embedder_mrr.py
    ATELIER_CODE_EMBEDDER=bge    uv run python benchmarks/codebench/eval_embedder_mrr.py

    # any HF SentenceTransformer model (no prefix)
    ATELIER_CODE_EMBEDDER=hf ATELIER_CODE_EMBED_MODEL=Salesforce/SFR-Embedding-Code-400M_R \
        uv run python benchmarks/codebench/eval_embedder_mrr.py

    # Matryoshka truncation (match CMM's 768d)
    ATELIER_CODE_EMBEDDER=nomic ATELIER_NOMIC_DIM=768 \
        uv run python benchmarks/codebench/eval_embedder_mrr.py

    # cap queries for a quick signal
    FITNESS_SAMPLE=30 ATELIER_CODE_EMBEDDER=bge \
        uv run python benchmarks/codebench/eval_embedder_mrr.py

Results are appended to reports/benchmark/embedder_mrr_history.jsonl so
you can track all embedders over time and compare.

Gold files (comma-sep via FITNESS_PAIRS):
    benchmarks/codebench/data/bench_pairs_def_gold.json     (definition retrieval)
    benchmarks/codebench/data/bench_pairs_content_gold.json (content retrieval)
    benchmarks/codebench/data/bench_pairs_semantic_gold.json (semantic-rescue queries)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, "src")

from atelier.core.foundation.paths import workspace_key
from atelier.infra.embeddings.factory import make_code_embedder

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SAMPLE = int(os.environ.get("FITNESS_SAMPLE", "0"))
REPO_FILTER = os.environ.get("FITNESS_REPO", "")
EMBEDDER_PIN = os.environ.get("ATELIER_CODE_EMBEDDER", "bge")

_DEFAULT_GOLD = ",".join(
    filter(
        None,
        [
            "benchmarks/codebench/data/bench_pairs_def_gold.json",
            "benchmarks/codebench/data/bench_pairs_content_gold.json",
            # semantic gold (behavior->code, mined by build_llm_gold.py)
            (
                "benchmarks/codebench/data/bench_pairs_semantic_gold.json"
                if Path("benchmarks/codebench/data/bench_pairs_semantic_gold.json").exists()
                else ""
            ),
        ],
    )
)
_gold_paths = [
    p.strip()
    for p in os.environ.get("FITNESS_PAIRS", _DEFAULT_GOLD).split(",")
    if p.strip() and Path(p.strip()).exists()
]

HISTORY = Path("reports/benchmark/embedder_mrr_history.jsonl")

# ---------------------------------------------------------------------------
# Load golds
# ---------------------------------------------------------------------------
golds: list[tuple[str, list, dict]] = []
repos: dict | None = None
for gp in _gold_paths:
    with open(gp) as f:
        d = json.load(f)
    if repos is None:
        repos = d["repos"]
    golds.append((d.get("gold_kind", "definition"), d["pairs"], d["true_map"]))
assert repos is not None, "No gold files found"

pairs_all = [r for _, p, _ in golds for r in p]

uq: dict[str, set[str]] = {}
for q, _, prefix in pairs_all:
    uq.setdefault(prefix, set()).add(q)

if REPO_FILTER:
    uq = {p: qs for p, qs in uq.items() if REPO_FILTER in p}
if SAMPLE:
    per = max(1, SAMPLE // max(len(uq), 1))
    uq = {p: set(sorted(qs)[:per]) for p, qs in uq.items()}

runset = {p: set(qs) for p, qs in uq.items()}
total = sum(len(qs) for qs in uq.values())

# ---------------------------------------------------------------------------
# Load embedder
# ---------------------------------------------------------------------------
embedder = make_code_embedder(pin=EMBEDDER_PIN)
print(f"[emb-eval] embedder={embedder.name} dim={embedder.dim} queries={total}", flush=True)


def _norm(p: str) -> str:
    return (p or "").replace("\\", "/")


def _db_for(prefix: str) -> Path | None:
    meta = repos[prefix]
    if meta.get("db"):
        p = Path(meta["db"])
        return p if p.exists() else None
    p = Path("/tmp") / workspace_key(Path(meta["ws"]).resolve()) / "code_context.sqlite"
    return p if p.exists() else None


# ---------------------------------------------------------------------------
# Embed + score per repo
# ---------------------------------------------------------------------------
results: dict[tuple[str, str], list[str]] = {}
latencies: list[float] = []
t0_total = time.time()

for prefix, queries in sorted(uq.items()):
    if not Path(repos[prefix]["ws"]).is_dir():
        print(f"[skip] {prefix}: workspace missing", flush=True)
        continue
    db = _db_for(prefix)
    if db is None:
        print(f"[skip] {prefix}: no index DB", flush=True)
        continue

    # No symbol cap: a hardcoded LIMIT 100000 here silently excluded 92% of
    # linux's 1.24M symbols from the ranking pool (only the repo in this
    # corpus over 100k), so any query whose correct symbol fell in the
    # excluded ~1.14M scored as an automatic miss regardless of embedding
    # quality -- not a real MRR measurement, a coverage bug (confirmed: MRR
    # collapsed 0.768->0.078 purely from this, on an unchanged embedder).
    # Overridable down via EVAL_EMBEDDER_MAX_SYMS for a quick/cheap smoke run.
    _max_syms = int(os.environ.get("EVAL_EMBEDDER_MAX_SYMS", "0"))
    _limit_sql = f" LIMIT {_max_syms}" if _max_syms > 0 else ""
    con = sqlite3.connect(str(db))
    rows = con.execute(
        f"SELECT file_path, symbol_name, kind, doc_summary FROM symbols WHERE file_path IS NOT NULL{_limit_sql}"
    ).fetchall()
    con.close()
    if not rows:
        print(f"[skip] {prefix}: no symbols", flush=True)
        continue

    doc_texts = [f"{r[1]} {r[2]} {r[3] or ''}".strip() for r in rows]
    doc_paths = [_norm(r[0]) for r in rows]

    # Chunked AND pre-allocated: embedding one unbounded batch at linux scale
    # (1.24M texts) is the same "accumulate everything, one write" pattern
    # that OOM-killed the production embedding backfill earlier. The first fix
    # attempt here only chunked the embed_documents() CALLS but still
    # accumulated every chunk's output into one Python list[list[float]]
    # before a single np.array(...) at the end -- earlyoom confirmed this
    # still hit 49.6GB RSS and got SIGTERM'd (a Python float object is ~24
    # bytes vs 4 packed, so 1.24M*1536 floats as nested lists is ~45GB+ of
    # pure object overhead). Fix: pre-allocate the destination float32 matrix
    # up front and fill it slice-by-slice per chunk -- no Python list of
    # floats ever exists at any size. Same default chunk size as production's
    # ATELIER_EMBED_COMMIT_CHUNK; overridable via EVAL_EMBEDDER_CHUNK.
    t1 = time.time()
    _embed_chunk = int(os.environ.get("EVAL_EMBEDDER_CHUNK", "20000"))
    doc_mat = np.empty((len(doc_texts), embedder.dim), dtype=np.float32)
    for _start in range(0, len(doc_texts), _embed_chunk):
        _end = min(_start + _embed_chunk, len(doc_texts))
        doc_mat[_start:_end] = embedder.embed_documents(doc_texts[_start:_end])
    t_doc = time.time() - t1

    query_list = sorted(queries)
    t1 = time.time()
    q_mat = np.array(embedder.embed_queries(query_list), dtype=np.float32)
    t_q = time.time() - t1

    t1 = time.time()
    sim = q_mat @ doc_mat.T  # (Q, N) — single BLAS/GPU matmul
    t_sim = time.time() - t1

    print(
        f"[{prefix.split('__')[-1]:<18}] syms={len(doc_texts):>6}  doc={t_doc:.1f}s  q={t_q:.3f}s  sim={t_sim:.3f}s",
        flush=True,
    )

    for qi, query in enumerate(query_list):
        t_q0 = time.time()
        order = np.argsort(-sim[qi])
        seen: set[str] = set()
        ranked: list[str] = []
        for idx in order:
            p = doc_paths[int(idx)]
            if p not in seen:
                seen.add(p)
                ranked.append(p)
            if len(ranked) >= 10:
                break
        results[(prefix, query)] = ranked
        latencies.append((time.time() - t_q0) * 1000)

total_elapsed = time.time() - t0_total


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------
def _rank_of_true(ranked: list[str], trues: list[str]) -> int | None:
    tn = [_norm(t) for t in trues]
    for i, f in enumerate(ranked, 1):
        nf = _norm(f)
        if any(nf.endswith(t) or t.endswith(nf) for t in tn):
            return i
    return None


def _score(kind: str, gpairs: list, gtm: dict) -> dict:
    agg = {"rr": 0.0, "h1": 0, "h3": 0, "n": 0}
    by_repo: dict[str, dict] = {}
    for q, tid, prefix in gpairs:
        if q not in runset.get(prefix, set()):
            continue
        trues = [_norm(t) for t in gtm.get(tid, []) if t]
        if not trues:
            continue
        r = _rank_of_true(results.get((prefix, q), []), trues)
        br = by_repo.setdefault(prefix, {"rr": 0.0, "h1": 0, "h3": 0, "n": 0})
        for d in (agg, br):
            d["n"] += 1
            if r:
                d["rr"] += 1.0 / r
                if r == 1:
                    d["h1"] += 1
                if r <= 3:
                    d["h3"] += 1
    return {
        "mrr": round(agg["rr"] / max(agg["n"], 1), 4),
        "hit1": round(agg["h1"] / max(agg["n"], 1), 4),
        "hit3": round(agg["h3"] / max(agg["n"], 1), 4),
        "n": agg["n"],
        "by_repo": {p: {"mrr": round(d["rr"] / max(d["n"], 1), 4), "n": d["n"]} for p, d in sorted(by_repo.items())},
    }


gold_scores = {kind: _score(kind, gp, gtm) for kind, gp, gtm in golds}
primary = golds[0][0]


def _pct(v: list[float], p: int) -> float:
    if not v:
        return 0.0
    s = sorted(v)
    return s[min(len(s) - 1, int(p / 100 * (len(s) - 1)))]


lat = {
    "mean": round(sum(latencies) / max(len(latencies), 1), 1),
    "p50": round(_pct(latencies, 50), 1),
    "p95": round(_pct(latencies, 95), 1),
    "max": round(max(latencies), 1) if latencies else 0.0,
}

# ---------------------------------------------------------------------------
# Print
# ---------------------------------------------------------------------------
W = 64
print("\n" + "─" * W)
print(f"  embedder : {embedder.name}")
print(f"  elapsed  : {total_elapsed:.1f}s  queries={len(latencies)}")
for kind, _gp, _gtm in golds:
    s = gold_scores[kind]
    print(f"  gold={kind:<20} MRR {s['mrr']:.4f}  hit@1 {s['hit1']:.4f}  hit@3 {s['hit3']:.4f}  n={s['n']}")
    for rp, rd in sorted(s["by_repo"].items(), key=lambda kv: kv[1]["mrr"]):
        icon = "✓" if rd["mrr"] >= 0.9 else ("~" if rd["mrr"] >= 0.5 else "✗")
        print(f"    {icon}  {rp.split('__')[-1]:<22} n={rd['n']:<4} MRR={rd['mrr']:.3f}")

# compare vs history
print()
if HISTORY.exists():
    runs = [json.loads(line) for line in HISTORY.read_text().splitlines() if line.strip()]
    same = [r for r in runs if r.get("embedder") == embedder.name]
    if same:
        prev = same[-1]
        delta = gold_scores[primary]["mrr"] - prev["golds"][primary]["mrr"]
        sign = "+" if delta >= 0 else ""
        print(
            f"  vs last run [{prev['ts'][:16]}]: MRR {prev['golds'][primary]['mrr']:.4f} → {gold_scores[primary]['mrr']:.4f} ({sign}{delta:.4f})"
        )
print("─" * W)

# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
try:
    sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip())
    sha += "+" if dirty else ""
except Exception:
    sha = "unknown"

record = {
    "ts": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    "sha": sha,
    "embedder": embedder.name,
    "dim": embedder.dim,
    "sample": SAMPLE,
    "golds": gold_scores,
    "latency_ms": lat,
    "elapsed_s": round(total_elapsed, 1),
}
HISTORY.parent.mkdir(parents=True, exist_ok=True)
with HISTORY.open("a") as f:
    f.write(json.dumps(record) + "\n")
print(f"  → appended to {HISTORY}")

# also emit JSON for piping
print(json.dumps({"embedder": embedder.name, "golds": gold_scores}))
