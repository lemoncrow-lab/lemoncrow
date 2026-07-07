#!/usr/bin/env python3
"""Index-build latency benchmark: lexical vs zoekt vs semantic, one row per repo.

Each phase is timed as a COLD full rebuild of one repo:
  - lexical : ``atelier code index --reindex`` with the semantic embedder OFF
              (parse -> tree-sitter symbols -> FTS5 + trigram symbol index).
  - zoekt   : ``zoekt-git-index`` over the repo's committed git tree (trigram).
  - semantic: embed every indexed symbol with BGE-Code-v1 (FP16 on GPU).

Reproduce (all repos in the def-gold set):

    uv run python benchmarks/codebench/bench_index_latency.py --out /tmp/index_latency.json

    # a subset, by prefix substring:
    uv run python benchmarks/codebench/bench_index_latency.py --repos django,flask,linux

Semantic needs torch + sentence_transformers.  If they are not importable in the
active venv, point ATELIER_BGE_PYTHON at a python that has them (e.g. a GPU venv):

    ATELIER_BGE_PYTHON=/tmp/bge_env/bin/python uv run python .../bench_index_latency.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_GOLD = _ROOT / "benchmarks/codebench/data/bench_pairs_def_gold.json"

# Embed helper run under a torch-capable python (see ATELIER_BGE_PYTHON). Reads
# symbol text straight from the freshly-built lexical DB so the count matches.
_SEM_HELPER = r"""
import json, sqlite3, sys, time
db = sys.argv[1]
import torch
from sentence_transformers import SentenceTransformer
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
rows = c.execute("SELECT symbol_name, COALESCE(qualified_name,''), COALESCE(signature,'') FROM symbols").fetchall()
texts = [" ".join(x for x in r if x).strip() for r in rows]
dev = "cuda" if torch.cuda.is_available() else "cpu"
m = SentenceTransformer("BAAI/bge-code-v1", trust_remote_code=True, device=dev)
if dev == "cuda":
    m = m.half()
m.max_seq_length = 512
t = time.perf_counter()
for i in range(0, len(texts), 256):
    m.encode(texts[i:i+256], normalize_embeddings=True, show_progress_bar=False)
print(json.dumps({"n": len(texts), "sec": round(time.perf_counter() - t, 2), "device": dev}))
"""


def _load_repos() -> dict:
    return json.loads(_GOLD.read_text())["repos"]


def _count(db: Path, table: str) -> int:
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
            return int(c.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return 0


def _run(cmd: list[str], env: dict | None = None) -> tuple[float, int, str]:
    t = time.perf_counter()
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    return round(time.perf_counter() - t, 2), p.returncode, (p.stderr or "")[-400:]


def _bge_python() -> str:
    override = os.environ.get("ATELIER_BGE_PYTHON")
    if override:
        return override
    # Prefer the active venv if it can import torch; else a known GPU venv.
    probe = subprocess.run(
        [sys.executable, "-c", "import torch, sentence_transformers"],
        capture_output=True,
        check=False,
    )
    if probe.returncode == 0:
        return sys.executable
    fallback = "/tmp/bge_env/bin/python"
    return fallback if Path(fallback).exists() else sys.executable


def _zoekt_bin(name: str = "zoekt-git-index") -> str | None:
    found = shutil.which(name)
    if found:
        return found
    cand = Path.home() / "go/bin" / name
    return str(cand) if cand.exists() else None


def bench_repo(prefix: str, meta: dict, *, bge_py: str, zoekt: str | None) -> dict:
    ws = Path(meta["ws"])
    short = prefix.split("__")[-1]
    workdir = Path(tempfile.mkdtemp(prefix=f"idxbench_{short}_"))
    lex_db = workdir / "code_context.sqlite"
    row: dict = {"repo": short}

    # 1) Lexical index (embedder OFF so we time only parse + FTS/trigram).
    env = {**os.environ, "ATELIER_CODE_EMBEDDER": "null", "ATELIER_ZOEKT_MODE": "off"}
    lex_s, rc, err = _run(
        ["atelier", "code", "index", "--repo-root", str(ws), "--db-path", str(lex_db), "--reindex", "--no-stats"],
        env=env,
    )
    row["lexical_s"] = lex_s if rc == 0 else None
    row["files"] = _count(lex_db, "files")
    row["symbols"] = _count(lex_db, "symbols")
    if rc != 0:
        row["lexical_err"] = err

    # 2) Zoekt index. Match production (ZoektServer.build_index): zoekt-git-index
    # over the committed tree for git repos, plain zoekt-index otherwise.
    zoekt_bin = zoekt if (ws / ".git").exists() else _zoekt_bin("zoekt-index")
    if zoekt_bin:
        zdir = workdir / "zoekt"
        zdir.mkdir(exist_ok=True)
        z_s, zrc, zerr = _run([zoekt_bin, "-index", str(zdir), str(ws)])
        row["zoekt_s"] = z_s if zrc == 0 else None
        if zrc != 0:
            row["zoekt_err"] = zerr
    else:
        row["zoekt_s"] = None
        row["zoekt_err"] = "no usable zoekt index binary for this workspace"

    # 3) Semantic embedding of every symbol (BGE-Code-v1, FP16 on GPU). The helper
    # prints one JSON line ({n, sec, device}) to stdout.
    if row["symbols"]:
        helper = workdir / "_embed.py"
        helper.write_text(_SEM_HELPER)
        out = subprocess.run([bge_py, str(helper), str(lex_db)], capture_output=True, text=True, check=False)
        try:
            data = json.loads(out.stdout.strip().splitlines()[-1])
            row["semantic_s"] = data["sec"]
            row["semantic_device"] = data["device"]
        except (ValueError, IndexError):
            row["semantic_s"] = None
            row["semantic_err"] = (out.stderr or "")[-400:]
    else:
        row["semantic_s"] = None

    shutil.rmtree(workdir, ignore_errors=True)
    return row


def main() -> None:
    ap = argparse.ArgumentParser(description="Index-build latency: lexical vs zoekt vs semantic")
    ap.add_argument("--repos", default="", help="Comma-separated prefix substrings (default: all def-gold repos).")
    ap.add_argument("--out", default="/tmp/index_latency.json", help="Write results JSON here.")
    args = ap.parse_args()

    wanted = [s.strip() for s in args.repos.split(",") if s.strip()]
    repos = _load_repos()
    bge_py = _bge_python()
    zoekt = _zoekt_bin()
    print(f"bge_python={bge_py}  zoekt={zoekt}", file=sys.stderr, flush=True)

    rows: list[dict] = []
    for prefix, meta in repos.items():
        if wanted and not any(w in prefix for w in wanted):
            continue
        if not meta.get("ws") or not Path(meta["ws"]).exists():
            continue
        print(f"[bench] {prefix} ...", file=sys.stderr, flush=True)
        row = bench_repo(prefix, meta, bge_py=bge_py, zoekt=zoekt)
        rows.append(row)
        print(
            f"  {row['repo']:14} sym={row.get('symbols', 0):>8}  "
            f"lex={row.get('lexical_s')}s  zoekt={row.get('zoekt_s')}s  sem={row.get('semantic_s')}s",
            file=sys.stderr,
            flush=True,
        )

    rows.sort(key=lambda r: r.get("symbols", 0))
    Path(args.out).write_text(json.dumps(rows, indent=2))
    # Markdown table to stdout.
    print("\n| Repo | Symbols | Lexical (s) | Zoekt (s) | Semantic (s) |")
    print("| --- | ---: | ---: | ---: | ---: |")
    for r in rows:
        print(
            f"| {r['repo']} | {r.get('symbols', 0):,} | {r.get('lexical_s', '-')} "
            f"| {r.get('zoekt_s', '-')} | {r.get('semantic_s', '-')} |"
        )
    print(f"\n[bench] wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
