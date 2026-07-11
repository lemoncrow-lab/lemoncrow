"""Semantic pair miner — mine docstring + intent-based queries from the symbol index.

Connects to the repo's code context SQLite DB, extracts docstring first-sentences
and generates natural-language intent queries from symbol names, then merges them
into a pairs JSON file compatible with eval_external_provider_mrr.py.

Usage::

    # Mine semantic pairs for the current repo and merge into existing pairs
    python benchmarks/codebench/semantic_pair_miner.py \\
        --repo-dir . \\
        --merge /tmp/session_pairs.json \\
        --out /tmp/session_pairs.json \\
        --max-pairs 100

Output format (compatible with eval_external_provider_mrr.py)::

    {"pairs": [(query, tid, repo_prefix), ...],
     "true_map": {tid: [file_paths...]},
     "repos": {repo_prefix: {"ws": workspace_path}}}
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sqlite3
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# DB path resolution (bench convention: /tmp/<workspace_key>/code_context.sqlite)
# ---------------------------------------------------------------------------


def _workspace_key(repo_root: Path) -> str:
    """Derive the workspace dir name from a repo root path."""
    from lemoncrow.core.foundation.paths import workspace_key

    return workspace_key(repo_root.resolve())


def _db_path_for(ws_path: Path) -> Path:
    p = Path("/tmp") / _workspace_key(ws_path.resolve()) / "code_context.sqlite"
    return p


def _user_db_path_for(ws_path: Path) -> Path:
    """Return the user's actual (non-temp) DB path."""
    from lemoncrow.core.foundation.paths import default_store_root

    key = _workspace_key(ws_path.resolve())
    return default_store_root() / "workspaces" / key / "code_context.sqlite"


# ---------------------------------------------------------------------------
# Semantic pair generation
# ---------------------------------------------------------------------------

_CODE_RE = re.compile(r"[{}<>()\[\]|=]|\b(?:def|class|import|return|self)\b")
_SHORT = 25
_TENSE = re.compile(r"[AEIOUaeiou]")

_INTENT_VERBS = frozenset(
    {
        "get",
        "set",
        "is",
        "has",
        "to",
        "build",
        "make",
        "create",
        "handle",
        "parse",
        "run",
        "check",
        "find",
        "search",
        "load",
        "save",
        "fetch",
        "push",
        "pull",
        "merge",
        "split",
        "format",
        "encode",
        "decode",
        "validate",
        "compute",
        "resolve",
        "render",
        "dispatch",
        "register",
        "connect",
        "close",
        "start",
        "stop",
        "init",
        "reset",
        "update",
        "delete",
        "insert",
        "append",
        "prepend",
    }
)


def _clean_doc_sentence(doc: str) -> str | None:
    """Extract the first meaningful sentence from a docstring."""
    if not doc:
        return None
    text = doc.strip().lstrip(":")
    first = re.split(r"(?<=[.!?])\s", text)[0].strip()
    if len(first) < _SHORT:
        return None
    if _CODE_RE.search(first):
        return None
    if first.startswith(("Args:", "Parameters", "Returns:", "Note:", "Example")):
        return None
    return first[:120]


def _name_to_intent(name: str, kind: str) -> str | None:
    """Turn a snake_case function name into a natural-language intent query."""
    if not name or len(name) < 4:
        return None
    if name.startswith("__") or name.startswith("_"):
        return None
    parts = name.split("_")
    if len(parts) < 2:
        return None
    verb = parts[0]
    rest = " ".join(parts[1:])
    if verb in _INTENT_VERBS:
        templates = [
            f"find the {kind} to {verb} {rest}",
            f"where is the code that {verb}s {rest}",
            f"show me the {verb} implementation for {rest}",
        ]
        return random.choice(templates)
    return None


def _tid(kind: str, file_path: str, symbol_name: str, extra: str = "") -> str:
    h = hashlib.sha256(f"{kind}:{file_path}:{symbol_name}:{extra}".encode()).hexdigest()[:16]
    return f"sem-{h}"


def mine_semantic_pairs(
    repo_dir: Path,
    repo_prefix: str,
    *,
    max_docstring: int = 50,
    max_intent: int = 50,
    seed: int = 42,
    verbose: bool = False,
) -> tuple[list[list[str]], dict[str, list[str]]]:
    """Mine semantic (docstring + intent) query pairs from the repo's symbol index.

    Returns (pairs, true_map).
    """
    # Try temp DB first (fresh index), fall back to user DB.
    db_path = _db_path_for(repo_dir)
    if not db_path.exists():
        db_path = _user_db_path_for(repo_dir)
        if not db_path.exists():
            print(
                f"[semantic] No DB found at {db_path} — run 'lemon code index --reindex' first.",
                file=sys.stderr,
            )
            return [], {}

    if verbose:
        print(f"[semantic] DB: {db_path}", file=sys.stderr)

    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT file_path, symbol_name, kind, doc_summary "
            "FROM symbols WHERE file_path IS NOT NULL AND symbol_name IS NOT NULL"
        ).fetchall()
    finally:
        con.close()

    if verbose:
        print(f"[semantic] {len(rows)} symbols in DB", file=sys.stderr)

    rng = random.Random(seed)
    rows_list = list(rows)
    rng.shuffle(rows_list)

    seen_tids: set[str] = set()
    doc_pairs: list[list[str]] = []
    intent_pairs: list[list[str]] = []
    true_map: dict[str, list[str]] = {}

    for fp, sname, kind, doc in rows_list:
        if not sname:
            continue
        if not any(fp.endswith(ext) for ext in (".py", ".ts", ".js", ".rs", ".go")):
            continue

        # Docstring queries
        if doc and len(doc_pairs) < max_docstring:
            q = _clean_doc_sentence(doc)
            if q:
                tid = _tid("sem", fp, sname)
                if tid not in seen_tids:
                    seen_tids.add(tid)
                    doc_pairs.append([q, tid, repo_prefix])
                    true_map[tid] = [fp]

        # Intent queries
        if len(intent_pairs) < max_intent:
            intent_q = _name_to_intent(sname, kind or "function")
            if intent_q:
                tid = _tid("sem", fp, sname, "intent")
                if tid not in seen_tids:
                    seen_tids.add(tid)
                    intent_pairs.append([intent_q, tid, repo_prefix])
                    true_map[tid] = [fp]

    rng.shuffle(doc_pairs)
    rng.shuffle(intent_pairs)

    pairs = doc_pairs + intent_pairs
    rng.shuffle(pairs)

    if verbose:
        print(
            f"[semantic] Mined {len(doc_pairs)} docstring + {len(intent_pairs)} intent = {len(pairs)} total",
            file=sys.stderr,
        )

    return pairs, true_map


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Mine semantic (docstring + intent) query pairs")
    parser.add_argument(
        "--repo-dir",
        default=".",
        help="Path to the repository root (default: cwd)",
    )
    parser.add_argument(
        "--repo-prefix",
        default=os.environ.get("SYNTHETIC_REPO_PREFIX", ""),
        help="Owner__repo prefix (default: directory basename)",
    )
    parser.add_argument(
        "--out",
        "-o",
        default=os.environ.get("SEMANTIC_PAIRS_OUT", "/tmp/semantic_pairs.json"),
        help="Output path for pairs JSON",
    )
    parser.add_argument(
        "--merge",
        "-m",
        default=None,
        help="Existing pairs JSON to merge semantic pairs into (deduplicated)",
    )
    parser.add_argument(
        "--max-pairs",
        type=int,
        default=None,
        help="Cap on semantic pairs added during merge.",
    )
    parser.add_argument(
        "--max-docstring",
        type=int,
        default=50,
        help="Max docstring-based queries (default: 50)",
    )
    parser.add_argument(
        "--max-intent",
        type=int,
        default=50,
        help="Max intent-based queries (default: 50)",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print progress")
    args = parser.parse_args()

    repo_dir = Path(args.repo_dir).resolve()
    prefix = args.repo_prefix or repo_dir.name

    if args.verbose:
        print(f"[semantic] Mining {repo_dir} with prefix={prefix}", file=sys.stderr)

    pairs, true_map = mine_semantic_pairs(
        repo_dir=repo_dir,
        repo_prefix=prefix,
        max_docstring=args.max_docstring,
        max_intent=args.max_intent,
        seed=args.seed,
        verbose=args.verbose,
    )

    if not pairs:
        print("[semantic] No pairs generated.", file=sys.stderr)
        sys.exit(1)

    # Merge into an existing pairs file if requested.
    if args.merge and Path(args.merge).exists():
        existing = json.loads(Path(args.merge).read_text())
        seen = {(q, t, r) for q, t, r in existing.get("pairs", [])}
        added = 0
        cap = args.max_pairs
        for entry in pairs:
            if cap is not None and added >= cap:
                break
            key = tuple(entry)
            if key not in seen:
                seen.add(key)
                existing["pairs"].append(entry)
                tid = entry[1]
                if tid not in existing.get("true_map", {}):
                    existing.setdefault("true_map", {})[tid] = true_map.get(tid, [])
                added += 1
        canonical_prefix = next(iter(existing.get("repos", {})), prefix)
        existing.setdefault("repos", {})[canonical_prefix] = {"ws": str(repo_dir)}
        out_data = existing
        cap_note = f" (capped at {cap})" if cap is not None else ""
        print(f"[semantic] Merged {added} new pairs into {args.merge}{cap_note}", file=sys.stderr)
    else:
        out_data = {
            "pairs": pairs,
            "true_map": true_map,
            "repos": {prefix: {"ws": str(repo_dir)}},
        }

    with open(args.out, "w") as f:
        json.dump(out_data, f, indent=2)

    print(f"[semantic] Wrote {len(out_data['pairs'])} total pairs to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
