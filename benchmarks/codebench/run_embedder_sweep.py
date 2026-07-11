"""Run all embedders through eval_embedder_mrr.py and print a comparison table.

Runs sequentially (GPU is shared — can't parallelise).  Appends each result
to reports/benchmark/embedder_sweep.jsonl and prints a ranked table at the end.

Usage:
    uv run python benchmarks/codebench/run_embedder_sweep.py

    # quick check with sampling
    FITNESS_SAMPLE=30 uv run python benchmarks/codebench/run_embedder_sweep.py

    # single embedder (for resuming a partial sweep)
    uv run python benchmarks/codebench/run_embedder_sweep.py --only bge
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--only", default="", help="Run only embedders whose label contains this string")
ap.add_argument("--skip", default="", help="Skip embedders whose label contains this string")
args = ap.parse_args()

# ---------------------------------------------------------------------------
# Embedder registry — add new models here
# ---------------------------------------------------------------------------
EMBEDDERS: list[dict] = [
    {
        "label": "BGE-Code-v1",
        "dim": 1536,
        "env": {"LEMONCROW_CODE_EMBEDDER": "bge"},
        "note": "LemonCrow default semantic embedder",
    },
    {
        "label": "Nomic-embed-code 3584d",
        "dim": 3584,
        "env": {"LEMONCROW_CODE_EMBEDDER": "nomic"},
        "note": "Full precision; same model as CMM binary",
    },
    {
        "label": "Nomic-embed-code 768d",
        "dim": 768,
        "env": {"LEMONCROW_CODE_EMBEDDER": "nomic", "LEMONCROW_NOMIC_DIM": "768"},
        "note": "Matryoshka truncation; matches CMM int8 dim",
    },
    {
        "label": "SFR-Embedding-Code-400M",
        "dim": 1024,
        "env": {
            "LEMONCROW_CODE_EMBEDDER": "hf",
            "LEMONCROW_CODE_EMBED_MODEL": "Salesforce/SFR-Embedding-Code-400M_R",
            "LEMONCROW_HF_QUERY_PREFIX": "",
            "LEMONCROW_HF_DOC_PREFIX": "",
        },
        "note": "Salesforce code-specific, 400M params",
    },
    {
        "label": "Jina-embeddings-v3",
        "dim": 1024,
        "env": {
            "LEMONCROW_CODE_EMBEDDER": "hf",
            "LEMONCROW_CODE_EMBED_MODEL": "jinaai/jina-embeddings-v3",
            "LEMONCROW_HF_QUERY_PREFIX": "Represent this sentence for searching relevant passages: ",
            "LEMONCROW_HF_DOC_PREFIX": "",
        },
        "note": "Jina v3, task-aware prefixes",
    },
    {
        "label": "Qwen3-Embedding-0.6B",
        "dim": 1024,
        "env": {
            "LEMONCROW_CODE_EMBEDDER": "hf",
            "LEMONCROW_CODE_EMBED_MODEL": "Qwen/Qwen3-Embedding-0.6B",
            "LEMONCROW_HF_QUERY_PREFIX": "",
            "LEMONCROW_HF_DOC_PREFIX": "",
        },
        "note": "Qwen3 small, strong MTEB",
    },
    {
        "label": "Qwen3-Embedding-4B",
        "dim": 2560,
        "env": {
            "LEMONCROW_CODE_EMBEDDER": "hf",
            "LEMONCROW_CODE_EMBED_MODEL": "Qwen/Qwen3-Embedding-4B",
            "LEMONCROW_HF_QUERY_PREFIX": "",
            "LEMONCROW_HF_DOC_PREFIX": "",
        },
        "note": "Qwen3 large, top MTEB",
    },
    {
        "label": "GTE-Qwen2-7B",
        "dim": 3584,
        "env": {
            "LEMONCROW_CODE_EMBEDDER": "hf",
            "LEMONCROW_CODE_EMBED_MODEL": "Alibaba-NLP/gte-Qwen2-7B-instruct",
            "LEMONCROW_HF_QUERY_PREFIX": "Instruct: Given a code search query, retrieve the most relevant code snippet.\nQuery: ",
            "LEMONCROW_HF_DOC_PREFIX": "",
        },
        "note": "Alibaba GTE-Qwen2 7B, was #1 MTEB",
    },
    # --- new models ---
    {
        "label": "BGE-M3",
        "dim": 1024,
        "env": {
            "LEMONCROW_CODE_EMBEDDER": "hf",
            "LEMONCROW_CODE_EMBED_MODEL": "BAAI/bge-m3",
            "LEMONCROW_HF_QUERY_PREFIX": "",
            "LEMONCROW_HF_DOC_PREFIX": "",
        },
        "note": "BAAI hybrid dense+sparse+ColBERT, same maker as BGE-Code-v1",
    },
    {
        "label": "Arctic-Embed-L-v2",
        "dim": 1024,
        "env": {
            "LEMONCROW_CODE_EMBEDDER": "hf",
            "LEMONCROW_CODE_EMBED_MODEL": "Snowflake/snowflake-arctic-embed-l-v2.0",
            "LEMONCROW_HF_QUERY_PREFIX": "Represent this sentence for searching relevant passages: ",
            "LEMONCROW_HF_DOC_PREFIX": "",
        },
        "note": "Snowflake retrieval-tuned 568M, Apache 2.0",
    },
    {
        "label": "GTE-Qwen2-1.5B",
        "dim": 1536,
        "env": {
            "LEMONCROW_CODE_EMBEDDER": "hf",
            "LEMONCROW_CODE_EMBED_MODEL": "Alibaba-NLP/gte-Qwen2-1.5B-instruct",
            "LEMONCROW_HF_QUERY_PREFIX": "Instruct: Given a code search query, retrieve the most relevant code snippet.\nQuery: ",
            "LEMONCROW_HF_DOC_PREFIX": "",
        },
        "note": "Alibaba GTE-Qwen2 1.5B, smaller sibling of 7B that OOM'd",
    },
]

# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------
def _matches_filter(label: str, pattern: str) -> bool:
    """True if any pipe-separated token appears in label (case-insensitive)."""
    return any(tok.lower() in label.lower() for tok in pattern.split("|") if tok)

if args.only:
    EMBEDDERS = [e for e in EMBEDDERS if _matches_filter(e["label"], args.only)]
if args.skip:
    EMBEDDERS = [e for e in EMBEDDERS if not _matches_filter(e["label"], args.skip)]

if not EMBEDDERS:
    print("No embedders match filter.", file=sys.stderr)
    sys.exit(1)

SAMPLE = os.environ.get("FITNESS_SAMPLE", "")
SWEEP_LOG = Path("reports/benchmark/embedder_sweep.jsonl")
SWEEP_LOG.parent.mkdir(parents=True, exist_ok=True)

print(f"Running {len(EMBEDDERS)} embedders{'  (sample=' + SAMPLE + ')' if SAMPLE else ''}\n")

# ---------------------------------------------------------------------------
# Run each embedder
# ---------------------------------------------------------------------------
results: list[dict] = []

for i, emb in enumerate(EMBEDDERS, 1):
    label = emb["label"]
    print(f"[{i}/{len(EMBEDDERS)}] {label}  ({emb['note']})", flush=True)
    print("-" * 60, flush=True)

    env = dict(os.environ)
    env.update(emb["env"])
    if SAMPLE:
        env["FITNESS_SAMPLE"] = SAMPLE

    try:
        proc = subprocess.run(
            [sys.executable, "benchmarks/codebench/eval_embedder_mrr.py"],
            env=env,
            capture_output=False,  # let stdout/stderr stream live
            text=True,
            timeout=3600,
        )
    except subprocess.TimeoutExpired:
        print("  TIMEOUT after 3600s", flush=True)
        results.append({"label": label, "error": "timeout"})
        continue
    except Exception as exc:
        print(f"  ERROR: {exc}", flush=True)
        results.append({"label": label, "error": str(exc)})
        continue

    # eval_embedder_mrr.py emits one JSON line as its last stdout line
    # (it also streams progress to stdout; read the history file instead)
    history = Path("reports/benchmark/embedder_mrr_history.jsonl")
    if history.exists():
        runs = [json.loads(ln) for ln in history.read_text().splitlines() if ln.strip()]
        model_key = emb["env"].get("LEMONCROW_CODE_EMBEDDER", "")
        hf_model = emb["env"].get("LEMONCROW_CODE_EMBED_MODEL", "")
        if model_key == "hf" and hf_model:
            # stored as "nomic:<org>/<model>" — match on full model path
            matches = [r for r in runs if hf_model in r.get("embedder", "")]
        else:
            matches = [r for r in runs if model_key in r.get("embedder", "")]
            if emb["env"].get("LEMONCROW_NOMIC_DIM"):
                matches = [r for r in matches if str(emb["dim"]) in r.get("embedder", "") or r.get("dim") == emb["dim"]]
        if matches:
            latest = matches[-1]
            rec = {"label": label, "dim": emb["dim"], "note": emb["note"], **latest}
            results.append(rec)
            with SWEEP_LOG.open("a") as f:
                f.write(json.dumps(rec) + "\n")
            continue

    results.append({"label": label, "error": "no history entry written"})
    print("  WARNING: no history entry found", flush=True)

# ---------------------------------------------------------------------------
# Comparison table
# ---------------------------------------------------------------------------
GOLD_KINDS = ["definition", "content", "semantic_rescue"]

print("\n" + "=" * 90)
print(f"  EMBEDDER COMPARISON  ({dt.datetime.now(dt.UTC).strftime('%Y-%m-%d %H:%M UTC')})")
print("=" * 90)

# header
hdr_golds = [g[:7] for g in GOLD_KINDS]
print(f"  {'Label':<28} {'Dim':>5}  " + "  ".join(f"{g} MRR" for g in hdr_golds))
print("  " + "-" * 86)

for rec in results:
    if "error" in rec:
        print(f"  {rec['label']:<28}  ERROR: {rec['error']}")
        continue
    golds_data = rec.get("golds", {})
    mrrs = []
    for gk in GOLD_KINDS:
        s = golds_data.get(gk, {})
        mrrs.append(f"{s.get('mrr', 0):.4f}" if s else "  -   ")
    dim = rec.get("dim", "?")
    print(f"  {rec['label']:<28} {dim:>5}  " + "  ".join(f"{m:>11}" for m in mrrs))

print("=" * 90)

# per-repo breakdown for the primary gold
print("\n  Per-repo MRR (definition gold):")
print(f"  {'Repo':<22}" + "".join(f"  {r['label'][:12]:>12}" for r in results if "error" not in r))
print("  " + "-" * (22 + 14 * sum(1 for r in results if "error" not in r)))

# collect all repo names
all_repos: set[str] = set()
for rec in results:
    if "error" not in rec:
        by_repo = (rec.get("golds", {}).get("definition") or {}).get("by_repo", {})
        all_repos.update(by_repo.keys())

for rp in sorted(all_repos):
    short = rp.split("__")[-1] if "__" in rp else rp
    row = f"  {short:<22}"
    for rec in results:
        if "error" in rec:
            continue
        by_repo = (rec.get("golds", {}).get("definition") or {}).get("by_repo", {})
        mrr = by_repo.get(rp, {}).get("mrr", None)
        cell = f"{mrr:.3f}" if mrr is not None else "  -  "
        icon = "✓" if (mrr or 0) >= 0.9 else ("~" if (mrr or 0) >= 0.5 else "✗")
        row += f"  {icon}{cell:>11}"
    print(row)

print("=" * 90)
print(f"\n  Full results: {SWEEP_LOG}")
