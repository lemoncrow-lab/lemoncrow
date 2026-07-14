#!/usr/bin/env python3
"""Mine an embedder-independent semantic gold with a local Ollama model.

For sampled symbols, the model writes a natural-language 'what does this do' query
(no names, no verbatim docstring) -> the symbol's file is the target. Fresh
wording (not in the index, so not circular) and NO retrieval-rank filter (so not
biased to any embedder). This is the fair intent->code benchmark.

Usage:
    uv run python benchmarks/codebench/build_llm_gold.py \
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
from concurrent.futures import ThreadPoolExecutor, as_completed

_SYSTEM = textwrap.dedent("""\
    You are building a CODE RETRIEVAL benchmark. Given a code snippet, write ONE
    search-engine style KEYWORD QUERY a developer would type into a code search
    bar to find THIS code -- like a Google search, not a question to a person.
    Rules:
    - 3-7 words. A phrase/fragment, NOT a full sentence.
    - Do NOT phrase it as a question. Never start with "how", "what", "why",
      "does", "do", "is", "are", "can", "which", "when", "where". No "?".
    - Do NOT use the function/class/variable names from the code.
    - Do NOT copy phrases from comments or docstrings.
    - Just the core action/behavior as keywords, e.g. "convert hsv to lch color",
      "retry request on connection timeout", "parse duration string to seconds".
    - Output ONLY the keyword phrase, nothing else.
""").strip()

_OLLAMA_MODEL = os.environ.get("GOLD_MINE_OLLAMA_MODEL", "qwen2.5-coder:7b")
# Mining is one HTTP round-trip + generation per candidate symbol, entirely
# serial by default -- GPU/CPU sit mostly idle between requests. Ollama can
# serve several generate calls concurrently (queued/batched server-side), so
# fan candidates out across a small worker pool instead of one-at-a-time.
_MINE_WORKERS = int(os.environ.get("GOLD_MINE_WORKERS", "8"))

# Reject queries that leak enough literal vocabulary from the source snippet to
# be solvable by keyword/FTS5 search alone -- defeats the point of a semantic
# (intent -> code) gold set. Measured on the 2026-07-13 mine: 64% of generated
# queries shared >=60% of their content words with the target file verbatim
# (mean overlap 0.64), so the resulting gold barely separated semantic search
# from lexical search. Threshold matches the "hard" bucket that DID show a real
# lexical (0.330) < semantic (0.389) < combined (0.463) MRR gap.
_OVERLAP_STOP = {
    "a",
    "an",
    "the",
    "to",
    "of",
    "in",
    "on",
    "for",
    "with",
    "and",
    "or",
    "how",
    "do",
    "does",
    "is",
    "are",
    "what",
    "which",
    "that",
    "this",
    "be",
    "handle",
    "proper",
    "properly",
    "correctly",
    "used",
    "using",
    "use",
    "returns",
    "return",
    "value",
    "function",
    "method",
    "class",
    "if",
    "not",
    "can",
    "it",
    "its",
    "as",
    "by",
    "from",
    "into",
}
_OVERLAP_MAX = 0.3

_QUESTION_STARTS = (
    "how",
    "what",
    "why",
    "does",
    "do",
    "is",
    "are",
    "can",
    "which",
    "when",
    "where",
    "who",
    "would",
    "should",
    "could",
    "will",
)


def _is_question(q: str) -> bool:
    """Reject full-sentence questions -- real code search queries are keyword
    phrases ("convert hsv to lch color"), not questions to a person ("How do I
    convert an HSV color to LCH?"). Backstops the prompt: the local model
    reverts to a "How do you...?" template often enough that this must be
    enforced deterministically, not just requested.
    """
    s = q.strip()
    if s.endswith("?"):
        return True
    first = re.split(r"\s+", s.lower(), maxsplit=1)[0].strip("'\"") if s else ""
    return first in _QUESTION_STARTS


def _content_tokens(s: str) -> set:
    return {w for w in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", s.lower()) if w not in _OVERLAP_STOP}


def _lexical_overlap(query: str, code: str) -> float:
    qt = _content_tokens(query)
    if not qt:
        return 1.0  # no scorable content -> treat as leaky, reject
    return len(qt & _content_tokens(code)) / len(qt)


# Retrieval scores hits at file granularity (true_map maps a query to a whole
# file), so what matters for "is this lexically solvable" is overlap against
# the WHOLE target file -- not just the harvested symbol's body. A query with
# low overlap against its own function can still leak via shared imports/
# sibling functions in the same file. Checking only the symbol body under-
# counted leakage (audited mean overlap only dropped 0.638 -> 0.527 on a first
# pass filtered that way); checking the full file is what the eval actually
# tests against.


def mine_query(code: str, name: str, kind: str) -> str:
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

    def prepare(row):
        """Cheap, read-only pre-filter before the model call. Never mutates
        seen/per_file -- those are only updated on accept (see below), same as
        the pre-parallel version, so behavior matches except for benign
        staleness within a single in-flight batch (a per-file cap can be
        exceeded by up to _MINE_WORKERS-1 while several of that file's
        symbols are in flight at once -- acceptable for a benchmark miner).
        """
        name, kind, fp, sb, eb = row
        _low = fp.lower()
        _bn = os.path.basename(_low)
        key = (name, fp)
        if key in seen:
            return None
        if re.search(r"(^|/)(tests?|testing|examples?|galleries|gallery|docs?|benchmarks?)/", _low):
            return None
        if _bn.startswith(("test_", "conftest")) or _bn.endswith(("_test.py", "tests.py")):
            return None
        if per_file.get(fp, 0) >= per_file_cap:
            return None
        try:
            with open(os.path.join(ws, fp), encoding="utf-8", errors="replace") as fh:
                full_text = fh.read()
        except Exception:
            return None
        code = full_text[sb:eb]
        if len(code) < 150:
            return None
        return key, name, kind, fp, code, full_text

    row_iter = iter(rows)
    with ThreadPoolExecutor(max_workers=_MINE_WORKERS) as pool:
        while len(out) < cap:
            batch = []
            batch_keys: set = set()
            for row in row_iter:
                cand = prepare(row)
                if cand is None or cand[0] in batch_keys:
                    continue
                batch_keys.add(cand[0])
                batch.append(cand)
                if len(batch) >= _MINE_WORKERS:
                    break
            if not batch:
                break  # candidates exhausted

            futures = {
                pool.submit(mine_query, code, name, kind): (key, name, fp, full_text)
                for key, name, kind, fp, code, full_text in batch
            }
            for fut in as_completed(futures):
                key, name, fp, full_text = futures[fut]
                q = fut.result()
                if len(q.split()) < 3 or name.lower() in q.lower().replace("_", " "):
                    continue
                if _is_question(q):
                    continue
                if _lexical_overlap(q, full_text) >= _OVERLAP_MAX:
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
        print(f"[gold] {pfx} ...", file=sys.stderr, flush=True)
        got = mine_repo(db, ws, a.per_repo)
        for q, rel in got:
            tid = "gold-" + hashlib.sha1(f"{pfx}:{rel}:{q}".encode()).hexdigest()[:12]
            P.append([q, tid, pfx])
            tmap[tid] = [rel]
        if got:
            out_repos[pfx] = m
        print(f"[gold] {pfx} mined {len(got)}", file=sys.stderr, flush=True)
        # Write incrementally after every repo so a long (~40min) mine can't lose
        # everything to an interrupt -- the partial gold is always valid on disk.
        with open(a.out, "w") as fh:
            json.dump(
                {"gold_kind": "semantic", "pairs": P, "true_map": tmap, "repos": out_repos},
                fh,
                indent=1,
            )
    print(f"[gold] wrote {len(P)} pairs across {len(out_repos)} repos -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
