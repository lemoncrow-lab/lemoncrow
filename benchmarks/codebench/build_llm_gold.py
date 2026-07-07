#!/usr/bin/env python3
"""Mine an embedder-independent semantic gold with Claude Haiku.

For sampled symbols, Haiku writes a natural-language 'what does this do' query
(no names, no verbatim docstring) -> the symbol's file is the target. Fresh
wording (not in the index, so not circular) and NO retrieval-rank filter (so not
biased to any embedder). This is the fair intent->code benchmark.

Usage:
    uv run python benchmarks/codebench/build_haiku_gold.py \
        --out benchmarks/codebench/data/bench_pairs_semantic_gold.json \
        --per-repo 12
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import textwrap
import urllib.request as _u

_SYSTEM = textwrap.dedent("""\
    You are building a CODE RETRIEVAL benchmark. Given a code snippet, write ONE
    natural-language question a developer would type to find THIS code.
    Rules:
    - Do NOT use the function/class/variable names from the code.
    - Do NOT copy phrases from comments or docstrings.
    - Describe the BEHAVIOR/PURPOSE in plain English, 8-18 words.
    - Output ONLY the question, nothing else.
""").strip()

_OLLAMA_MODEL = os.environ.get("HAIKU_OLLAMA_MODEL", "qwen2.5-coder:7b")


def haiku_query(code: str, name: str, kind: str) -> str:
    prompt = f"{_SYSTEM}\n\nCode ({kind}):\n```\n{code[:2000]}\n```\n\nOne search query:"
    try:
        body = json.dumps(
            {
                "model": _OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "think": False,
                "options": {"temperature": 0.4, "num_predict": 60},
            }
        ).encode()
        req = _u.Request("http://localhost:11434/api/generate", data=body, headers={"Content-Type": "application/json"})
        with _u.urlopen(req, timeout=60) as resp:
            q = json.loads(resp.read())["response"]
            q = __import__("re").sub(r"<think>.*?</think>", "", q, flags=16).strip()
        return " ".join(q.strip().strip('"').split())
    except Exception as e:
        print(f"  [ollama err] {e}", file=sys.stderr)
        return ""


def mine_repo(db, ws, cap):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT symbol_name,kind,file_path,start_byte,end_byte FROM symbols "
        "WHERE kind IN ('function','class','method') AND end_byte-start_byte BETWEEN 200 AND 4000"
    ).fetchall()
    conn.close()
    random.Random(42).shuffle(rows)
    out = []
    seen: set = set()  # (name, fp) keys -> allow multiple symbols per file so small
    per_file: dict = {}  # repos (requests=16 files) can still reach 100+ queries
    per_file_cap = max(cap // 4, 20)  # bound concentration; big repos stay file-diverse
    for name, kind, fp, sb, eb in rows:
        _low = fp.lower()
        _bn = os.path.basename(_low)
        key = (name, fp)
        if key in seen:
            continue
        if re.search(r"(^|/)(tests?|testing|examples?|galleries|gallery|docs?|benchmarks?)/", _low):
            continue
        if _bn.startswith(("test_", "conftest")) or _bn.endswith(("_test.py", "tests.py")):
            continue
        if per_file.get(fp, 0) >= per_file_cap:
            continue
        try:
            with open(os.path.join(ws, fp), encoding="utf-8", errors="replace") as fh:
                code = fh.read()[sb:eb]
        except Exception:
            continue
        if len(code) < 150:
            continue
        q = haiku_query(code, name, kind)
        if len(q.split()) < 5 or name.lower() in q.lower().replace("_", " "):
            continue
        out.append((q, fp))
        seen.add(key)
        per_file[fp] = per_file.get(fp, 0) + 1
        print(f"    {name} -> {q[:60]}", file=sys.stderr, flush=True)
        if len(out) >= cap:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="benchmarks/codebench/data/bench_pairs_semantic_gold.json")
    ap.add_argument("--repos-from", default="benchmarks/codebench/data/bench_pairs_def_gold.json")
    ap.add_argument("--per-repo", type=int, default=12)
    ap.add_argument("--repo", default="", help="only this repo substring")
    a = ap.parse_args()
    with open(a.repos_from) as fh:
        repos = json.load(fh)["repos"]
    _pairs, tmap, out_repos = {}, {}, {}
    P = []
    for pfx, m in repos.items():
        if a.repo and a.repo not in pfx:
            continue
        db = m.get("db")
        ws = m.get("ws")
        if not db or not os.path.isfile(db) or not ws:
            continue
        print(f"[haiku] {pfx} ...", file=sys.stderr, flush=True)
        got = mine_repo(db, ws, a.per_repo)
        for q, rel in got:
            tid = "haiku-" + hashlib.sha1(f"{pfx}:{rel}:{q}".encode()).hexdigest()[:12]
            P.append([q, tid, pfx])
            tmap[tid] = [rel]
        if got:
            out_repos[pfx] = m
        print(f"[haiku] {pfx} mined {len(got)}", file=sys.stderr, flush=True)
        # Write incrementally after every repo so a long (~40min) mine can't lose
        # everything to an interrupt -- the partial gold is always valid on disk.
        with open(a.out, "w") as fh:
            json.dump(
                {"gold_kind": "semantic", "pairs": P, "true_map": tmap, "repos": out_repos},
                fh,
                indent=1,
            )
    print(f"[haiku] wrote {len(P)} pairs across {len(out_repos)} repos -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
