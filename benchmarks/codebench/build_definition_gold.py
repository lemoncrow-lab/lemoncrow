"""Regenerate the retrieval bench gold as a DEFINITION gold.

The shipped ``bench_pairs_swebench_gold.json`` gold is the file the SWE-task PR edited
(edit-localization). For retrieval/search eval that is mislabeled: measured on
LemonCrow, ~45%% of golds are test files and ~40%% point to a file that does NOT
define the symbols the query names, so a definition-retriever structurally cannot
score them however good it is (overall lexical MRR 0.67, but 0.95 on the subset
whose gold actually defines a named symbol).

This rebuilds the gold as the file(s) that DEFINE the specific symbols each query
names -- a search-correct gold, auto-derived from each repo's symbol index, no
human labeling. A query's gold = union of definition files of its *specific*
identifiers (a bare symbol name defined in <= --max-def files, length >=
--min-len, not a common token). Queries that name no specific symbol are DROPPED
(unscorable for definition retrieval -- those need an NL-intent eval).

Each surviving query gets a stable unique id, so the existing harnesses
(``eval_external_provider_mrr.py``, which keys ``true_map`` by
tid) work unchanged via ``--pairs`` / ``FITNESS_PAIRS`` / ``EVAL_PAIRS``.

Usage::

    uv run --no-sync python benchmarks/codebench/build_definition_gold.py \\
        --in benchmarks/codebench/data/bench_pairs_swebench_gold.json \\
        --out benchmarks/codebench/data/bench_pairs_def_gold.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

# Short/common identifiers that match too many definitions to localize a query.
_COMMON = {
    "def",
    "class",
    "async",
    "return",
    "self",
    "import",
    "from",
    "none",
    "true",
    "false",
    "path",
    "name",
    "value",
    "data",
    "text",
    "line",
    "file",
    "type",
    "test",
    "main",
    "run",
    "get",
    "set",
    "args",
    "kwargs",
    "result",
    "error",
    "node",
    "key",
    "item",
    "index",
}
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _norm(p: str) -> str:
    return (p or "").replace("\\", "/")


def _idents(q: str) -> list[str]:
    return [t for t in _IDENT_RE.findall(q) if t.lower() not in _COMMON]


def _symbol_def_index(db: str) -> dict[str, set[str]]:
    """lowercased symbol_name -> set of definition file paths, from the symbol index."""
    out: dict[str, set[str]] = {}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT symbol_name, file_path FROM symbols").fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    for name, fp in rows:
        if name and fp:
            out.setdefault(str(name).lower(), set()).add(_norm(str(fp)))
    return out


def _def_gold(
    query: str,
    sym2files: dict[str, set[str]],
    max_def: int,
    min_len: int,
    min_purity: float,
) -> set[str]:
    """Consensus definition gold restricted to the *reliably-labelable* subset.

    Gold = the file(s) defining the MOST of the query's specific symbols. Three
    gates drop queries that cannot be trusted to auto-label:

    1. No specific symbol named  -> drop (needs an NL-intent eval, not this gold).
    2. ``purity`` gate: ``purity = n_specific / n_words`` is the fraction of the
       query's word-tokens that are real symbols. Symbol-naming queries -- a bare
       symbol (``apply_fuzzy_replace``) or a clean alternation of symbols
       (``apply_fuzzy_replace|resolve_symbol_edit|...``) -- have purity ~1.0.
       Descriptive English phrases (``no install-time indexers detected SCIP
       message``) have a couple of coincidental symbol hits among many ordinary
       words, so purity is low; below ``min_purity`` the query is dropped because
       its gold rests on a word that merely *happens* to name a bare symbol
       (polyglot repos: a Python query mislabeled to api.ts).
    3. Scatter gate: a multi-symbol query whose symbols are scattered (no file
       defines >= 2 of them) is ambiguous -> drop.

    What survives is exactly the symbol-naming subset (single-token + clean
    alternation) whose definition file is unambiguous.
    """
    words = [t for t in {t.lower() for t in _idents(query)} if len(t) >= min_len]
    if not words:
        return set()
    hits: dict[str, int] = {}
    n_specific = 0
    unambiguous: set[str] = set()
    for tok in words:
        files = sym2files.get(tok)
        if files and len(files) <= max_def:
            n_specific += 1
            for f in files:
                hits[f] = hits.get(f, 0) + 1
            if len(files) == 1:
                unambiguous |= files
    if not hits:
        return set()
    purity_ok = n_specific / len(words) >= min_purity
    scatter_ok = not (n_specific >= 2 and max(hits.values()) < 2)
    if purity_ok and scatter_ok:
        best = max(hits.values())
        return {f for f, c in hits.items() if c == best}
    # Reintroduce hard regex/content alternations that still name a symbol with
    # an UNAMBIGUOUS single-file definition: that file is a confident gold even
    # when the purity/scatter gates would otherwise drop the query. These are
    # exactly the content-recall queries where the broad Zoekt channel earns its
    # keep, so dropping them hid that signal from the eval.
    return unambiguous


def _tid(prefix: str, query: str) -> str:
    return "def-" + hashlib.blake2s(f"{prefix}\x00{query}".encode()).hexdigest()[:16]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", default="benchmarks/codebench/data/bench_pairs_swebench_gold.json")
    ap.add_argument("--out", default="benchmarks/codebench/data/bench_pairs_def_gold.json")
    ap.add_argument("--max-def", type=int, default=5, help="identifier must be defined in <= this many files")
    ap.add_argument("--min-len", type=int, default=4, help="minimum identifier length to count")
    ap.add_argument(
        "--min-purity",
        type=float,
        default=0.5,
        help="min fraction of query word-tokens that are real symbols (drops descriptive queries)",
    )
    args = ap.parse_args()

    with open(args.src) as fh:
        data = json.load(fh)
    repos = data["repos"]
    by_repo: dict[str, list[str]] = {}
    for q, _old_tid, prefix in data["pairs"]:
        by_repo.setdefault(prefix, [])
        if q not in by_repo[prefix]:  # dedup queries per repo
            by_repo[prefix].append(q)

    out_pairs: list[list[str]] = []
    true_map: dict[str, list[str]] = {}
    print(f"{'repo':28s} {'queries':>8} {'scorable':>9} {'dropped':>8} {'avg_gold':>9}", file=sys.stderr)
    tot_q = tot_s = 0
    for prefix, queries in sorted(by_repo.items()):
        meta = repos.get(prefix, {})
        db = meta.get("db")
        if not db or not Path(db).is_file():
            print(f"{prefix:28s}  (no db -> skip {len(queries)} queries)", file=sys.stderr)
            continue
        sym2files = _symbol_def_index(db)
        scorable = 0
        gsizes: list[int] = []
        for q in queries:
            gold = _def_gold(q, sym2files, args.max_def, args.min_len, args.min_purity)
            if not gold:
                continue
            tid = _tid(prefix, q)
            out_pairs.append([q, tid, prefix])
            true_map[tid] = sorted(gold)
            scorable += 1
            gsizes.append(len(gold))
        tot_q += len(queries)
        tot_s += scorable
        avg = sum(gsizes) / len(gsizes) if gsizes else 0.0
        print(f"{prefix:28s} {len(queries):8d} {scorable:9d} {len(queries) - scorable:8d} {avg:9.1f}", file=sys.stderr)

    out = {
        "pairs": out_pairs,
        "true_map": true_map,
        "repos": repos,
        "gold_kind": "definition",
        "params": {"max_def": args.max_def, "min_len": args.min_len, "min_purity": args.min_purity},
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(out, fh)
    print(
        f"\nwrote {args.out}: {tot_s}/{tot_q} queries scorable ({100 * tot_s / max(tot_q, 1):.0f}%), "
        f"{len(true_map)} golds",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
