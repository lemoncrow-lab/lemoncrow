"""Prepare embedding training data from mined (query, gold_file) pairs.

Takes a pairs JSON (from synthetic_pair_miner.py of offline_session_analyzer.py)
and reads the actual file contents from the repo to create a SentenceTransformer
training dataset with (query, positive_document_text) pairs.

Output: train.jsonl + test.jsonl + corpus.jsonl

Usage::

    # Step 1: mine pairs from your repo
    python benchmarks/codebench/synthetic_pair_miner.py \\
        --repo-dir /path/to/your/repo --out /tmp/mypairs.json \\
        --pairs-per-file 4 --verbose

    # Step 2: prepare training data
    python benchmarks/codebench/prepare_train_data.py \\
        --pairs /tmp/mypairs.json \\
        --repo-dir /path/to/your/repo \\
        --out-dir /tmp/train_data

    # Step 3 (next script): train the embedding model
    python benchmarks/embedding/train_embedding.py \
        --train-data /tmp/train_data \
        --model BAAI/bge-code-v1 \
        --output-dir /tmp/my_finetuned_bge \
        --compare-baseline

Format notes:
    - train.jsonl:  {"query": "...", "positive": "file content ..."}
    - test.jsonl:   {"query": "...", "positive": "file content ...",
                     "gold_path": "rel/path", "tid": "..."}
    - corpus.jsonl: {"id": "rel/file.py", "text": "Path: rel/file.py\\n...content..."}
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path


def load_pairs(pairs_path: str | Path):
    """Load pairs JSON and return (pairs, true_map, repos)."""
    with open(pairs_path) as f:
        data = json.load(f)
    return data["pairs"], data["true_map"], data.get("repos", {})


def read_file_content(repo_dir: str | Path, rel_path: str, max_chars: int = 8192) -> str | None:
    """Read a repo-relative file, return its content truncated to max_chars."""
    full_path = Path(repo_dir) / rel_path
    try:
        text = full_path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, IsADirectoryError, OSError):
        return None
    if len(text) > max_chars:
        # Keep first max_chars chars — preserve docstring + imports + first symbols
        text = text[:max_chars] + "\n# ... (truncated)"
    return text


def build_corpus(
    repo_dir: str | Path, true_map: dict[str, list[str]], max_chars: int = 8192, verbose: bool = False
) -> dict[str, str]:
    """Build a deduplicated corpus of {rel_path: content} from all gold files.

    Also includes other source files for negative mining if needed.
    The corpus dict maps relative paths to file content.
    """
    corpus: dict[str, str] = {}
    for _tid, paths in true_map.items():
        for p in paths:
            if p in corpus:
                continue
            content = read_file_content(repo_dir, p, max_chars=max_chars)
            if content:
                corpus[p] = content

    if verbose:
        print(f"[prepare] Corpus: {len(corpus)} unique files loaded", file=sys.stderr)
    return corpus


def create_train_test_split(
    pairs: list[tuple[str, str, str]],
    true_map: dict[str, list[str]],
    corpus: dict[str, str],
    test_frac: float = 0.15,
    seed: int = 42,
    verbose: bool = False,
):
    """Split pairs into train/test, reading file content as positive passages.

    Returns (train_examples, test_examples, val_examples) where each example is:
        {"query": str, "positive": str, "gold_path": str, "tid": str}
    """
    rng = random.Random(seed)

    # Group by tid so same gold file doesn't appear in both train and test
    tid_to_pairs: dict[str, list[tuple[str, str, str]]] = {}
    for q, tid, prefix in pairs:
        tid_to_pairs.setdefault(tid, []).append((q, tid, prefix))

    all_tids = list(tid_to_pairs.keys())
    rng.shuffle(all_tids)

    # Split on tids, not on pairs — all queries for same file stay together
    n_test = max(1, int(len(all_tids) * test_frac))
    test_tids = set(all_tids[:n_test])
    train_tids = set(all_tids[n_test:])

    train_examples: list[dict] = []
    test_examples: list[dict] = []
    skipped_no_content = 0

    for tid, tid_pairs in tid_to_pairs.items():
        gold_paths = true_map.get(tid, [])
        if not gold_paths:
            continue

        # Use first gold path that has content in the corpus
        pos_content = None
        pos_path = None
        for gp in gold_paths:
            if gp in corpus:
                pos_content = corpus[gp]
                pos_path = gp
                break

        if not pos_content:
            skipped_no_content += 1
            continue

        is_test = tid in test_tids

        for q, _, _ in tid_pairs:
            example = {
                "query": q,
                "positive": pos_content,
                "gold_path": pos_path,
                "tid": tid,
            }
            if is_test:
                test_examples.append(example)
            else:
                train_examples.append(example)

    if verbose:
        print(f"[prepare] Train: {len(train_examples)} examples ({len(train_tids)} files)", file=sys.stderr)
        print(f"[prepare] Test:  {len(test_examples)} examples ({len(test_tids)} files)", file=sys.stderr)
        if skipped_no_content:
            print(f"[prepare] Skipped (no content): {skipped_no_content} tids", file=sys.stderr)

    return train_examples, test_examples


def write_dataset(
    out_dir: str | Path,
    train_examples: list[dict],
    test_examples: list[dict],
    corpus: dict[str, str],
    verbose: bool = False,
):
    """Write JSONL files for training.

    - train.jsonl:  query + positive (for MultipleNegativesRankingLoss)
    - test.jsonl:   query + positive + gold_path + tid (for eval)
    - corpus.jsonl: id + text (for building the search index; each line has
                    "id" and "text" with a "Path:" prefix)
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Training set
    with open(out_dir / "train.jsonl", "w") as f:
        for ex in train_examples:
            f.write(json.dumps({"query": ex["query"], "positive": ex["positive"]}) + "\n")
    if verbose:
        print(f"[prepare] Wrote {len(train_examples)} train pairs → {out_dir / 'train.jsonl'}", file=sys.stderr)

    # Test set (includes gold_path for scoring)
    with open(out_dir / "test.jsonl", "w") as f:
        for ex in test_examples:
            f.write(
                json.dumps(
                    {
                        "query": ex["query"],
                        "positive": ex["positive"],
                        "gold_path": ex["gold_path"],
                        "tid": ex["tid"],
                    }
                )
                + "\n"
            )
    if verbose:
        print(f"[prepare] Wrote {len(test_examples)} test pairs → {out_dir / 'test.jsonl'}", file=sys.stderr)

    # Corpus (id + text pairs for a search-index/eval corpus)
    with open(out_dir / "corpus.jsonl", "w") as f:
        for rel_path, content in corpus.items():
            entry = {"id": rel_path, "text": f"Path: {rel_path}\n{content}"}
            f.write(json.dumps(entry) + "\n")
    if verbose:
        print(f"[prepare] Wrote {len(corpus)} corpus files → {out_dir / 'corpus.jsonl'}", file=sys.stderr)

    # Write a metadata file
    meta = {
        "num_train": len(train_examples),
        "num_test": len(test_examples),
        "num_corpus": len(corpus),
        "train_tids": len({ex["tid"] for ex in train_examples}),
        "test_tids": len({ex["tid"] for ex in test_examples}),
    }
    with open(out_dir / "meta.json", "w") as f:
        json.dump(meta, f, indent=2)
    if verbose:
        print(f"[prepare] Metadata → {out_dir / 'meta.json'}", file=sys.stderr)

    return meta


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Prepare embedding training data from mined pairs",
    )
    parser.add_argument(
        "--pairs",
        "-p",
        required=True,
        help="Path to pairs JSON (from synthetic_pair_miner or offline_session_analyzer)",
    )
    parser.add_argument("--repo-dir", "-r", required=True, help="Repository root directory (to read file contents)")
    parser.add_argument(
        "--out-dir", "-o", default="/tmp/train_data", help="Output directory for train/test/corpus JSONL"
    )
    parser.add_argument(
        "--test-frac", type=float, default=0.15, help="Fraction of files to hold out for test (default: 0.15)"
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed for split")
    parser.add_argument(
        "--max-chars",
        type=int,
        default=1024,
        help="Max characters per file content (default: 1024 — shorter = less GPU memory during training)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Print progress")
    args = parser.parse_args()

    # Load pairs
    pairs, true_map, repos = load_pairs(args.pairs)
    if args.verbose:
        print(f"[prepare] Loaded {len(pairs)} pairs, {len(true_map)} tids, {len(repos)} repos", file=sys.stderr)

    # Build corpus
    corpus = build_corpus(args.repo_dir, true_map, max_chars=args.max_chars, verbose=args.verbose)

    # Split into train/test
    train_examples, test_examples = create_train_test_split(
        pairs,
        true_map,
        corpus,
        test_frac=args.test_frac,
        seed=args.seed,
        verbose=args.verbose,
    )

    # Write outputs
    meta = write_dataset(
        args.out_dir,
        train_examples,
        test_examples,
        corpus,
        verbose=args.verbose,
    )

    print(
        f"[prepare] Done. Train={meta['num_train']} pairs, "
        f"Test={meta['num_test']} pairs, "
        f"Corpus={meta['num_corpus']} files",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
