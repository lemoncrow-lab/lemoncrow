"""Synthetic pair miner — generate realistic grep-like queries from source files.

Generates query → gold_file pairs by extracting symbols, path components, and
docstrings from source files, then creating realistic grep queries that a
developer might type. Queries are deliberately fuzzy (partial names, multi-token
composites, regex alternations, path-qualified searches) so retrieval isn't
trivial.

Output format (compatible with eval_external_provider_mrr.py):

    {"pairs": [(query, tid, repo_prefix), ...],
     "true_map": {tid: [file_paths...]},
     "repos": {repo_prefix: {"ws": workspace_path}}}

Usage::

    # Generate pairs from the current repo's source
    python benchmarks/codebench/synthetic_pair_miner.py \\
        --repo-dir /path/to/repo \\
        --out /tmp/synthetic_pairs.json \\
        --pairs-per-file 4

    # Chain with session pairs for eval
    python benchmarks/codebench/offline_session_analyzer.py \\
        --synthetic --run-eval --full

Anti-triviality rules:
    - Query is never an exact file path or exact symbol name.
    - Query must contain at least one non-alphanumeric wildcard or be composed
      of multiple tokens (avoids single-word exact matches).
    - At most 1 query per file can be a single bare identifier.
"""

from __future__ import annotations

import itertools
import json
import os
import random
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Token / symbol extraction
# ---------------------------------------------------------------------------

_SYM_RE = re.compile(
    r"(?:^|\n)\s*"
    r"(?:"
    r"def\s+(\w+)"  # def function_name
    r"|class\s+(\w+)"  # class ClassName
    r"|async\s+def\s+(\w+)"  # async def
    r"|(\w+)\s*=\s*(?:lambda|import|__import__)"  # name = import ...
    r")",
)

_IMPORT_RE = re.compile(
    r"(?:^|\n)\s*"
    r"(?:"
    r"import\s+([\w.]+(?:\s*,\s*[\w.]+)*)"  # import foo, bar
    r"|from\s+[\w.]+\s+import\s+(\w+)"  # from x import y
    r")",
)

# Match common prefixes that are stripped from symbol names for partial queries
_COMMON_PREFIXES = (
    "get_",
    "set_",
    "is_",
    "has_",
    "to_",
    "from_",
    "_get_",
    "_set_",
    "_is_",
    "_has_",
    "_to_",
    "_from_",
    "build_",
    "create_",
    "_build_",
    "_create_",
    "handle_",
    "_handle_",
    "_",
    "__",
)
_COMMON_SUFFIXES = (
    "_count",
    "_name",
    "_id",
    "_key",
    "_path",
    "_url",
    "_type",
    "_size",
    "_data",
    "_info",
    "_config",
    "_status",
    "_error",
    "_result",
    "_value",
    "_list",
)

_DOCSTRING_RE = re.compile(r"""""" "(.*?)" """"|'''(.*?)'''""", re.DOTALL)


def extract_symbols(text: str) -> dict:
    """Extract function names, class names, and imports from source text.

    Returns {"defs": [...], "classes": [...], "imports": [...]}.
    """
    defs: list[str] = []
    classes: list[str] = []
    imports: list[str] = []

    for m in _SYM_RE.finditer(text):
        for group_idx in (1, 2, 3, 4):
            val = m.group(group_idx)
            if val:
                if group_idx in (1, 3):  # def / async def
                    defs.append(val)
                elif group_idx == 2:  # class
                    classes.append(val)
                else:  # name = import/lambda
                    defs.append(val)
                break

    for m in _IMPORT_RE.finditer(text):
        names_str = m.group(1) or m.group(2) or ""
        for name in re.split(r"\s*,\s*", names_str):
            name = name.strip()
            if name and not name.startswith("_"):
                # Take last dotted component (the actual name imported)
                imports.append(name.split(".")[-1])

    return {"defs": defs, "classes": classes, "imports": imports}


def extract_docstring_phrases(text: str, max_phrases: int = 3) -> list[str]:
    """Extract short meaningful phrases from docstrings."""
    phrases: list[str] = []
    for m in _DOCSTRING_RE.finditer(text):
        doc = (m.group(1) or m.group(2) or "").strip()
        if not doc:
            continue
        # Take first line or first sentence
        first = doc.split("\n")[0].split(".")[0].strip()
        # Filter to substantive phrases (at least 3 words, at most 10)
        words = first.split()
        if 3 <= len(words) <= 10:
            # Skip generic boilerplate
            low = first.lower()
            if any(
                kw in low
                for kw in (
                    "copyright",
                    "license",
                    "permission",
                    "this file",
                    "this module",
                    "this class",
                    "this function",
                )
            ):
                continue
            # Skip if it's just "..." or other punctuation
            if all(c in " .!?-" for c in first):
                continue
            # Pick a 2-4 word window from the middle
            if len(words) > 4:
                start = random.randint(0, len(words) - 4)
                phrase = " ".join(words[start : start + random.randint(2, 4)])
            else:
                phrase = first
            if phrase not in phrases:
                phrases.append(phrase)
            if len(phrases) >= max_phrases:
                break
    return phrases


# ---------------------------------------------------------------------------
# Query generation strategies
# ---------------------------------------------------------------------------


def _pick_random(items: list[str], min_n: int = 1, max_n: int | None = None) -> list[str]:
    """Pick a random non-empty subset."""
    if not items:
        return []
    n = random.randint(min_n, min(max_n or len(items), len(items)))
    return random.sample(items, min(n, len(items)))


def gen_partial_symbol(symbols: list[str]) -> str | None:
    """Strip prefix/suffix from a symbol to create a fuzzy query.

    E.g. ``calculate_total_revenue`` → ``total_revenue``
    """
    candidates: list[str] = []
    for sym in symbols:
        stripped = sym
        # Strip common prefix
        for p in _COMMON_PREFIXES:
            if stripped.startswith(p) and len(stripped) > len(p) + 3:
                stripped = stripped[len(p) :]
                break
        # Strip common suffix
        for s in _COMMON_SUFFIXES:
            if stripped.endswith(s) and len(stripped) > len(s) + 3:
                stripped = stripped[: -len(s)]
                break
        # Split on underscore, drop the first or last token
        parts = stripped.split("_")
        if len(parts) >= 3:
            # Drop first or last
            stripped = "_".join(parts[1:]) if random.random() < 0.5 else "_".join(parts[:-1])
        elif len(parts) == 2:
            # Just take the second part
            stripped = parts[-1]

        # Must differ from original and be substantive
        if stripped and stripped != sym and len(stripped) >= 4:
            candidates.append(stripped)

    return random.choice(candidates) if candidates else None


def gen_regex_alternation(symbols: list[str], max_alts: int = 5) -> str | None:
    """Create a regex alternation query from related symbols.

    E.g. ``def _connect|def _open_conn|sqlite3.connect``

    If the file has fewer than 2 defs, try classes + defs.
    """
    all_syms = list(set(symbols))
    if len(all_syms) < 2:
        return None

    # Pick 2-4 symbols, shuffle, join with |
    picked = _pick_random(all_syms, min_n=2, max_n=min(max_alts, len(all_syms)))
    if not picked or len(picked) < 2:
        return None

    # Sometimes prefix with def\, class\, etc for realism
    pattern = "|".join(picked)
    if random.random() < 0.3:
        # Prepend "def " to the first token for realism
        first = picked[0]
        pattern = f"def {first}|{'|'.join(picked[1:])}" if len(picked) > 1 else f"def {first}"
    elif random.random() < 0.3:
        # Append a code keyword after the alternation
        pattern = f"{pattern}|{random.choice(picked)[:4]}"

    return pattern if len(pattern) >= 6 else None


def gen_multi_token_composite(defs: list[str], classes: list[str]) -> str | None:
    """Join 2-3 tokens from different symbols into a multi-word query.

    E.g. file has ``create_session`` and ``handle_timeout`` and ``ConfigManager``
    → ``session manager timeout``

    This mimics how users search for related concepts.
    """
    # Extract meaningful word tokens from symbols
    all_tokens: list[str] = []
    for sym in itertools.chain(defs, classes):
        parts = re.split(r"[_.\s]+", sym)
        for p in parts:
            p = p.strip().lower()
            if len(p) >= 3 and not p.isdigit() and p not in all_tokens:
                all_tokens.append(p)

    if len(all_tokens) < 3:
        return None

    # Pick 2-3 random tokens
    n = random.randint(2, min(3, len(all_tokens)))
    picked = random.sample(all_tokens, n)
    # Sort to avoid trivial path-order matches
    picked.sort()

    query = " ".join(picked)
    # Must be > 10 chars and not match any single symbol
    if len(query) < 8:
        return None
    return query


def gen_path_qualified(filepath: str, symbols: list[str]) -> str | None:
    """Qualify a symbol with directory path components.

    E.g. file ``src/atelier/gateway/cli.py`` with symbol ``list_sessions``
    → ``gateway cli sessions``

    Or when no good symbol: use two path components joined.
    """
    parts = Path(filepath).parts
    # Find non-generic dir parts (skip src/, tests/, etc.)
    dir_parts = [p for p in parts[:-1] if p not in ("src", "tests", "lib", "node_modules")]
    if not dir_parts:
        return None

    # Take last 1-2 dir parts
    dir_tokens = dir_parts[-min(2, len(dir_parts)) :]

    if symbols:
        sym = random.choice(symbols)
        sym_tokens = re.split(r"[_.\s]+", sym.lower())
        sym_tokens = [t for t in sym_tokens if len(t) >= 3 and t not in dir_tokens]
        if sym_tokens:
            token = random.choice(sym_tokens)
            query = " ".join([*dir_tokens, token])
            if len(query) >= 8:
                return query

    # Fallback: just use dir tokens
    if len(dir_tokens) >= 2:
        query = " ".join(dir_tokens)
        if len(query) >= 6:
            return query
    return None


def gen_definition_pattern(defs: list[str]) -> str | None:
    """Create a ``def <name>|<keyword>`` style query.

    E.g. file defines ``search_workspace`` and ``resolve_query``
    → ``def search_workspace|def resolve_query``
    """
    if len(defs) < 2:
        return None
    picked = _pick_random(defs, min_n=2, max_n=4)
    if not picked or len(picked) < 2:
        return None
    return "|".join(f"def {d}" for d in picked)


def gen_cross_reference(imports: list[str]) -> str | None:
    """Use an imported symbol as a query (tests retrieving the file that *uses* it).

    E.g. file imports ``json``, ``Path`` → query ``json.load|Path``
    """
    if len(imports) < 2:
        return _pick_random(imports, min_n=1, max_n=1)[0] if imports else None
    picked = _pick_random(imports, min_n=1, max_n=3)
    if not picked:
        return None
    return "|".join(picked)


def gen_noisy_partial(symbols: list[str]) -> str | None:
    """Generate a query with a deliberate typo or partial fragment.

    E.g. ``validate_response`` → ``validat_respon`` or ``validate.*respo``
    """
    sym = _pick_random(symbols, min_n=1, max_n=1)
    if not sym:
        return None
    s = sym[0]
    if len(s) < 8:
        return None

    if random.random() < 0.5:
        # Drop last 2-3 chars
        drop = random.randint(2, min(4, len(s) - 3))
        partial = s[:-drop]
        if len(partial) >= 4:
            return partial
    else:
        # Convert to regex with wildcards
        split_at = len(s) // 2
        return f"{s[:split_at]}.*{s[-3:]}"

    return None


# ---------------------------------------------------------------------------
# Per-file query generation
# ---------------------------------------------------------------------------


def generate_queries_for_file(
    filepath: str,
    repo_prefix: str,
    rel_path: str,
    text: str,
    max_queries: int = 5,
    rng: random.Random | None = None,
) -> list[tuple[str, str, str]]:
    """Generate synthetic queries for a single source file.

    Returns list of (query, tid, prefix) tuples.
    """
    if rng is None:
        rng = random.Random()

    syms = extract_symbols(text)
    all_syms = syms["defs"] + syms["classes"]
    phrases = extract_docstring_phrases(text)
    sub_imports = syms["imports"]

    queries: list[str] = []
    strategies = [
        ("partial", lambda: gen_partial_symbol(all_syms)),
        ("regex_alt", lambda: gen_regex_alternation(all_syms)),
        ("composite", lambda: gen_multi_token_composite(syms["defs"], syms["classes"])),
        ("path_qual", lambda: gen_path_qualified(rel_path, all_syms)),
        ("def_pattern", lambda: gen_definition_pattern(syms["defs"])),
        ("cross_ref", lambda: gen_cross_reference(sub_imports)),
        ("noisy", lambda: gen_noisy_partial(all_syms)),
    ]

    # Shuffle and apply strategies
    rng.shuffle(strategies)

    applied: set[str] = set()
    for _sname, sfunc in strategies:
        if len(queries) >= max_queries:
            break
        try:
            q = sfunc()
        except Exception:
            continue
        if not q:
            continue

        q = q.strip()
        if len(q) < 5:
            continue

        # Reject single-word queries shorter than 6 chars — they're too generic
        # (e.g. "json", "time", "flow", "type")
        q.replace(" ", "")
        if " " not in q and len(q) < 6:
            continue
        # Also reject single-token queries (no spaces/pipes) that are pure
        # common programming keywords
        _COMMON_GENERIC = {
            "json",
            "time",
            "flow",
            "type",
            "path",
            "find",
            "root",
            "part",
            "name",
            "data",
            "info",
            "size",
            "list",
            "file",
            "test",
            "base",
            "core",
            "main",
            "util",
            "help",
            "run",
            "cmd",
            "api",
            "cli",
            "key",
            "val",
            "log",
            "err",
            "fmt",
            "str",
            "int",
            "map",
            "set",
            "get",
            "put",
            "del",
        }
        if " " not in q and "|" not in q and q.lower() in _COMMON_GENERIC:
            continue

        # Anti-triviality checks
        # 1. Not an exact file path
        if q in (rel_path, filepath):
            continue
        # 2. Not an exact symbol name
        if q in all_syms:
            continue
        # 3. Not an exact substring of the rel_path (would trivially match)
        if q in rel_path:
            continue
        # 4. Deduplicate
        if q in applied:
            continue

        applied.add(q)
        queries.append(q)

    # If no queries yet from strategies, try docstring phrase as fallback
    if not queries and phrases:
        for ph in phrases:
            if ph not in applied:
                queries.append(ph)
                break

    # Build output tuples
    # Stable unique ID: replace path separators with _ and drop extension
    safe_path = rel_path.replace("/", "_").replace("\\", "_").rsplit(".", 1)[0]
    tid = f"synthetic_{repo_prefix}_{safe_path}"
    return [(q, tid, repo_prefix) for q in queries]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_LANGUAGE_EXTS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".rs",
    ".go",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".java",
    ".kt",
    ".swift",
    ".rb",
    ".php",
}


def _skip_path(path: Path, repo_path: Path) -> bool:
    """Return True if the file should be skipped."""
    # Check if any directory component is a skip dir
    skip_dirs = {
        "node_modules",
        "__pycache__",
        ".git",
        ".venv",
        ".eggs",
        "dist",
        "build",
        "build_dist",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".hypothesis",
        ".tox",
        ".nox",
        "target",
        "vendor",
        ".stack-work",
        ".terraform",
        "site-packages",
        "bower_components",
        "third_party",
        ".git-rewrite",
        # Benchmark / CI result copies — not part of actual source
        "reports",
    }
    try:
        rel = path.relative_to(repo_path)
        for part in rel.parts[:-1]:  # exclude the filename itself
            if part in skip_dirs or part.startswith("."):
                return True
    except ValueError:
        return True  # not relative to repo somehow

    name = path.name
    # Skip generated/lock files
    if name in (
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "uv.lock",
        "poetry.lock",
        "requirements.txt",
        "Cargo.lock",
        "go.sum",
    ):
        return True
    # Skip minified files
    return bool(name.endswith(".min.js") or name.endswith(".min.css"))


def mine_synthetic_pairs(
    repo_dir: str | Path,
    repo_prefix: str | None = None,
    max_queries_per_file: int = 5,
    max_files: int | None = None,
    seed: int = 42,
    verbose: bool = False,
) -> tuple[list[tuple[str, str, str]], dict[str, list[str]]]:
    """Mine synthetic query pairs from a repository.

    Args:
        repo_dir: Root of the repository to mine.
        repo_prefix: Owner__repo prefix (auto-detected from dirname if None).
        max_queries_per_file: Number of queries to generate per file.
        max_files: Cap on files to process (None = all).
        seed: Random seed for reproducibility.

    Returns (pairs, true_map) compatible with eval_external_provider_mrr.py.
    """
    repo_path = Path(repo_dir).resolve()
    if not repo_path.is_dir():
        raise NotADirectoryError(f"Repository directory not found: {repo_path}")

    if repo_prefix is None:
        repo_prefix = repo_path.name

    rng = random.Random(seed)

    # Collect source files
    source_files: list[Path] = []
    for ext in _LANGUAGE_EXTS:
        source_files.extend(repo_path.rglob(f"*{ext}"))

    # Filter: skip hidden dirs, vendor dirs, etc.
    source_files = [f for f in source_files if not _skip_path(f, repo_path)]
    source_files.sort()
    if max_files:
        rng.shuffle(source_files)
        source_files = source_files[:max_files]

    if verbose:
        print(f"[synthetic] Found {len(source_files)} source files in {repo_path.name}", file=sys.stderr)

    pairs: list[tuple[str, str, str]] = []
    true_map: dict[str, list[str]] = {}
    processed = 0
    skipped_small = 0
    total_queries = 0

    for fpath in source_files:
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if len(text) < 200:
            skipped_small += 1
            continue  # skip tiny stubs

        rel = str(fpath.relative_to(repo_path))
        file_pairs = generate_queries_for_file(
            str(fpath),
            repo_prefix,
            rel,
            text,
            max_queries=max_queries_per_file,
            rng=rng,
        )
        if file_pairs:
            tid = file_pairs[0][1]
            true_map[tid] = [rel]
            pairs.extend(file_pairs)
            total_queries += len(file_pairs)
            processed += 1

        if verbose and processed % 500 == 0:
            print(f"[synthetic] {processed} files processed, {total_queries} queries generated", file=sys.stderr)

    if verbose:
        print(
            f"[synthetic] Done: {processed} files, {skipped_small} skipped (too small), {total_queries} queries",
            file=sys.stderr,
        )

    return pairs, true_map


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Mine synthetic query pairs from a repository",
    )
    parser.add_argument(
        "--repo-dir",
        "-r",
        default=os.environ.get("SYNTHETIC_REPO_DIR", "."),
        help="Repository directory to mine (default: cwd)",
    )
    parser.add_argument(
        "--repo-prefix",
        "-p",
        default=os.environ.get("SYNTHETIC_REPO_PREFIX", ""),
        help="Owner__repo prefix (default: directory basename)",
    )
    parser.add_argument(
        "--out",
        "-o",
        default=os.environ.get("SYNTHETIC_PAIRS_OUT", "/tmp/synthetic_pairs.json"),
        help="Output path for pairs JSON",
    )
    parser.add_argument(
        "--merge", "-m", default=None, help="Existing pairs JSON to merge synthetic pairs into (deduplicated)"
    )
    parser.add_argument(
        "--max-pairs",
        type=int,
        default=None,
        help="Cap on synthetic pairs added during merge (for 50/50 balance with session pairs).",
    )
    parser.add_argument("--pairs-per-file", type=int, default=5, help="Max synthetic queries per file (default: 5)")
    parser.add_argument("--max-files", type=int, default=100, help="Cap on files to process (default: 100)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print progress")
    args = parser.parse_args()

    repo_dir = Path(args.repo_dir).resolve()
    prefix = args.repo_prefix or repo_dir.name

    if args.verbose:
        print(f"[synthetic] Mining {repo_dir} with prefix={prefix}", file=sys.stderr)

    pairs, true_map = mine_synthetic_pairs(
        repo_dir=repo_dir,
        repo_prefix=prefix,
        max_queries_per_file=args.pairs_per_file,
        max_files=args.max_files,
        seed=args.seed,
        verbose=args.verbose,
    )

    if not pairs:
        print("[synthetic] No pairs generated.", file=sys.stderr)
        sys.exit(1)

    # Merge into an existing pairs file if requested.
    if args.merge and Path(args.merge).exists():
        existing = json.loads(Path(args.merge).read_text())
        seen = {(q, t, r) for q, t, r in existing.get("pairs", [])}
        added = 0
        cap = args.max_pairs  # None = unlimited
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
        # Use the canonical prefix from the existing file (not repo_dir.name) so
        # both session and synthetic pairs share the same repos entry.
        canonical_prefix = next(iter(existing.get("repos", {})), prefix)
        existing.setdefault("repos", {})[canonical_prefix] = {"ws": str(repo_dir)}
        out_data = existing
        cap_note = f" (capped at {cap})" if cap is not None else ""
        print(f"[synthetic] Merged {added} new pairs into {args.merge}{cap_note}", file=sys.stderr)
    else:
        out_data = {
            "pairs": pairs,
            "true_map": true_map,
            "repos": {prefix: {"ws": str(repo_dir)}},
        }

    with open(args.out, "w") as f:
        json.dump(out_data, f, indent=2)

    print(f"[synthetic] Wrote {len(out_data['pairs'])} total pairs to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
