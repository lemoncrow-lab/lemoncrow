"""Fine-tune a SentenceTransformer embedding model on code retrieval pairs.

Loads (query, positive_doc) pairs from train.jsonl (prepared by
prepare_train_data.py), fine-tunes a huggingface embedding model with
MultipleNegativesRankingLoss, saves the adapted model, then evaluates it
on the held-out test set — reporting MRR/hit@1/hit@3.

Usage::

    # After running prepare_train_data.py:
    python benchmarks/embedding/train_embedding.py \
        --train-data /tmp/train_data \
        --model BAAI/bge-code-v1 \
        --output-dir /tmp/my_finetuned_bge \
        --epochs 3 \
        --batch-size 16 \
        --device cuda

    # If you want to compare against the base (pre-trained) model automatically:
    python benchmarks/embedding/train_embedding.py \
        --train-data /tmp/train_data \
        --model BAAI/bge-code-v1 \
        --output-dir /tmp/my_finetuned_bge \
        --compare-baseline

Requirements:
    pip install sentence-transformers torch numpy
    (or: pip install -r benchmarks/embedding/requirements_hf.txt)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

try:
    import torch
    from sentence_transformers import InputExample, SentenceTransformer, losses
    from torch.utils.data import DataLoader
except ImportError:
    print("[train] ERROR: sentence-transformers + torch + accelerate required.", file=sys.stderr)
    print("[train] Install: pip install -r benchmarks/embedding/requirements_hf.txt", file=sys.stderr)
    sys.exit(1)


INSTRUCTION = "<instruct>Given a natural language query, retrieve relevant code.\n<query>"
BGE_MODEL_ID = "BAAI/bge-code-v1"


def load_jsonl(path: str | Path) -> list[dict]:
    """Load each JSON line from a file."""
    path = Path(path)
    if not path.exists():
        print(f"[train] ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(1)
    return [json.loads(line) for line in path.read_text().strip().split("\n") if line.strip()]


def load_examples(jsonl_path: str | Path) -> list[InputExample]:
    """Load (query, positive) pairs as SentenceTransformer InputExamples."""
    records = load_jsonl(jsonl_path)
    examples = []
    for r in records:
        examples.append(InputExample(texts=[r["query"], r["positive"]]))
    return examples


def embed_texts(
    model: SentenceTransformer, texts: list[str], instruction: str = INSTRUCTION, batch_size: int = 32
) -> np.ndarray:
    """Embed a list of texts with the given model, returning a numpy array."""
    # BGE models need the instruction prefix for queries, but not for docs.
    # Since our test set mixes queries and docs, we embed raw (the instruction
    # prefix should only be applied at query time during eval, not during training).
    # Here we embed without prefix for training/eval consistency.
    embeddings = model.encode(
        texts, batch_size=batch_size, normalize_embeddings=True, show_progress_bar=len(texts) > 100
    )
    return np.array(embeddings, dtype=np.float32)


def evaluate_mrr(
    model: SentenceTransformer,
    test_examples: list[InputExample],
    corpus_texts: list[str],
    corpus_ids: list[str],
    instruction: str = INSTRUCTION,
    batch_size: int = 32,
    verbose: bool = False,
) -> dict:
    """Compute MRR, hit@1, hit@3 on the test set.

    This embeds the corpus once, embeds each query,
    compute cosine similarity via dot-product (since vectors are normalized),
    rank corpus by similarity, and score rank-of-gold-file.

    Returns dict with mrr, hit1, hit3, n, latency_ms.
    """
    if not corpus_texts or not test_examples:
        return {"mrr": 0.0, "hit1": 0.0, "hit3": 0.0, "n": 0, "latency_ms": 0.0}

    # Embed corpus without prefix (documents, not queries)
    t0 = time.perf_counter()
    corpus_vecs = embed_texts(model, corpus_texts, batch_size=4)
    corpus_time = time.perf_counter() - t0
    if verbose:
        print(f"[eval] Corpus embedded: {len(corpus_vecs)} files in {corpus_time:.1f}s", file=sys.stderr)
        # Debug: check first few gold paths against corpus
        for ex in test_examples[:3]:
            gp = ex.texts[2] if len(ex.texts) > 2 else None
            found = gp in corpus_ids if gp else False
            print(f"[eval] debug: gold_path='{gp}' in_corpus={found}", file=sys.stderr)

    # For each test example, embed the query and score
    agg = {"rr": 0.0, "h1": 0, "h3": 0, "n": 0}
    latencies = []

    for ex in test_examples:
        query = ex.texts[0]
        gold_path = ex.texts[2] if len(ex.texts) > 2 else None  # stored in test set

        q_start = time.perf_counter()
        qv = model.encode([f"{instruction}{query}"], normalize_embeddings=True)[0]
        scores = corpus_vecs @ qv  # dot product (cosine since normalized)
        top_idx = np.argsort(-scores)
        latencies.append((time.perf_counter() - q_start) * 1000.0)

        if gold_path:
            # Find rank of gold file — try exact match first, then suffix match
            rank = None
            gp_norm = gold_path.replace("\\", "/")
            for rank_pos, idx in enumerate(top_idx, 1):
                cid = corpus_ids[idx].replace("\\", "/")
                if cid == gp_norm:
                    rank = rank_pos
                    break
            if rank is None and verbose:
                # Try suffix match like the other evals do
                for rank_pos, idx in enumerate(top_idx, 1):
                    cid = corpus_ids[idx].replace("\\", "/")
                    if cid.endswith(gp_norm) or gp_norm.endswith(cid):
                        rank = rank_pos
                        break
            if rank:
                agg["rr"] += 1.0 / rank
                agg["h1"] += int(rank == 1)
                agg["h3"] += int(rank <= 3)
        agg["n"] += 1

    n = max(agg["n"], 1)
    result = {
        "mrr": round(agg["rr"] / n, 4),
        "hit1": round(agg["h1"] / n, 4),
        "hit3": round(agg["h3"] / n, 4),
        "n": agg["n"],
        "latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
    }
    return result


def build_corpus_index(test_examples: list[InputExample], corpus_jsonl_path: str | Path):
    """Build corpus texts and ids from the corpus.jsonl and test set.

    Returns (corpus_texts, corpus_ids) where both are parallel lists.
    corpus_ids contain the relative file paths for scoring.
    """
    # Load corpus from corpus.jsonl
    corpus_texts: list[str] = []
    corpus_ids: list[str] = []

    cp = Path(corpus_jsonl_path)
    if cp.exists():
        for line in load_jsonl(cp):
            corpus_ids.append(line["id"])
            corpus_texts.append(line["text"])
    else:
        # Fallback: build from test examples' positive docs
        seen = set()
        for ex in test_examples:
            path = ex.texts[2] if len(ex.texts) > 2 else None
            content = ex.texts[1]
            if path and path not in seen:
                seen.add(path)
                corpus_ids.append(path)
                corpus_texts.append(f"Path: {path}\n{content}")

    return corpus_texts, corpus_ids


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Fine-tune a SentenceTransformer embedding model on code retrieval pairs",
    )
    parser.add_argument(
        "--train-data",
        "-d",
        default="/tmp/train_data",
        help="Directory with train.jsonl, test.jsonl, corpus.jsonl (from prepare_train_data.py)",
    )
    parser.add_argument(
        "--model", "-m", default=BGE_MODEL_ID, help="Base model to fine-tune (default: BAAI/bge-code-v1)"
    )
    parser.add_argument(
        "--output-dir", "-o", default="/tmp/finetuned_embedding", help="Where to save the fine-tuned model"
    )
    parser.add_argument("--epochs", type=int, default=3, help="Number of training epochs (default: 3)")
    parser.add_argument(
        "--batch-size", type=int, default=4, help="Batch size for training (default: 4 — reduce further if OOM)"
    )
    parser.add_argument("--warmup-steps", type=int, default=500, help="Warmup steps (default: 500)")
    parser.add_argument(
        "--learning-rate",
        type=float,
        default=1e-6,
        help="Peak learning rate (default: 1e-6 — conservative for large pre-trained models; "
        "use 2e-5 only for small models like bge-small-en-v1.5)",
    )
    parser.add_argument("--device", default=None, help="Device: cuda, cpu, or auto (default: auto)")
    parser.add_argument(
        "--max-seq",
        type=int,
        default=512,
        help="Max sequence length during training (caps activation memory; default 512).",
    )
    parser.add_argument(
        "--compare-baseline", action="store_true", help="Also eval the un-fine-tuned model for comparison"
    )
    parser.add_argument(
        "--skip-training", action="store_true", help="Skip training, only run eval (useful for debugging)"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Print detailed progress")
    args = parser.parse_args()

    train_dir = Path(args.train_data)
    train_jsonl = train_dir / "train.jsonl"
    test_jsonl = train_dir / "test.jsonl"
    corpus_jsonl = train_dir / "corpus.jsonl"

    if not train_jsonl.exists():
        print(f"[train] ERROR: train.jsonl not found at {train_jsonl}", file=sys.stderr)
        print("[train] Run prepare_train_data.py first.", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Determine device
    device = args.device
    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[train] Device: {device}", file=sys.stderr)

    # Load training examples
    print(f"[train] Loading train examples from {train_jsonl}...", file=sys.stderr)
    train_examples = load_examples(train_jsonl)
    print(f"[train] {len(train_examples)} training pairs loaded", file=sys.stderr)

    # Load test examples
    test_examples: list[InputExample] = []
    if test_jsonl.exists():
        test_records = load_jsonl(test_jsonl)
        for r in test_records:
            # Store gold_path as third text for scoring
            test_examples.append(InputExample(texts=[r["query"], r["positive"], r.get("gold_path", "")]))
        print(f"[train] {len(test_examples)} test pairs loaded", file=sys.stderr)

    # Build corpus index for scoring
    corpus_texts, corpus_ids = build_corpus_index(test_examples, corpus_jsonl)
    print(f"[train] Corpus: {len(corpus_texts)} files", file=sys.stderr)

    # ----- Baseline eval (before fine-tuning) -----
    if args.compare_baseline:
        print(f"[train] Loading base model for comparison: {args.model}", file=sys.stderr)
        kw = {"dtype": torch.float16} if device == "cuda" else {}
        base_model = SentenceTransformer(args.model, device=device, trust_remote_code=True, model_kwargs=kw)
        print("[train] Evaluating baseline...", file=sys.stderr)
        baseline = evaluate_mrr(base_model, test_examples, corpus_texts, corpus_ids, verbose=args.verbose)
        print(
            f"[baseline] MRR={baseline['mrr']}  hit@1={baseline['hit1']}  hit@3={baseline['hit3']}  n={baseline['n']}",
            file=sys.stderr,
        )
        del base_model
        torch.cuda.empty_cache()

    if args.skip_training:
        print("[train] --skip-training: skipping fine-tuning, only evaluating baseline", file=sys.stderr)
        if test_examples and corpus_texts:
            print("[train] Evaluating baseline model...", file=sys.stderr)
            result = evaluate_mrr(
                base_model if args.compare_baseline else None,
                test_examples,
                corpus_texts,
                corpus_ids,
                verbose=args.verbose,
            )
        return 0

    # ----- Fine-tune -----
    print(f"[train] Loading model for fine-tuning: {args.model}", file=sys.stderr)
    # bf16 (not fp16) for training: fp16 full-finetune of a 1.5B model is both
    # unstable (no GradScaler in fit()) and OOMs a 24 GB card once AdamW states are
    # added. bf16 weights+grads+states ~12 GB; gradient checkpointing trades compute
    # for activation memory; max_seq caps the O(seq^2) attention buffers. Together
    # they fit BGE-Code-v1 finetuning on a single RTX 4090.
    kw = {"dtype": torch.bfloat16} if device == "cuda" else {}
    model = SentenceTransformer(args.model, device=device, trust_remote_code=True, model_kwargs=kw)
    model.max_seq_length = args.max_seq
    if device == "cuda":
        try:
            model[0].auto_model.gradient_checkpointing_enable()
            model[0].auto_model.config.use_cache = False
            print(f"[train] mem: bf16 + grad-checkpoint + max_seq={args.max_seq}", file=sys.stderr)
        except Exception as e:
            print(f"[train] grad-checkpoint enable failed: {e!r}", file=sys.stderr)

    # Use MultipleNegativesRankingLoss — standard for embedding fine-tuning
    # Each batch: the query at position i is paired with the positive at position i,
    # and other positives in the batch serve as in-batch negatives.
    train_dataloader = DataLoader(
        train_examples,
        batch_size=args.batch_size,
        shuffle=True,
        drop_last=True,
    )
    train_loss = losses.MultipleNegativesRankingLoss(model)

    # Warmup steps
    num_train_steps = len(train_dataloader) * args.epochs
    warmup_steps = min(args.warmup_steps, num_train_steps // 5)

    print(
        f"[train] Training: {args.epochs} epochs, batch={args.batch_size}, "
        f"lr={args.learning_rate}, warmup={warmup_steps}",
        file=sys.stderr,
    )
    print(f"[train] {len(train_examples)} examples, {num_train_steps} total steps", file=sys.stderr)

    # No per-epoch evaluator: EmbeddingSimilarityEvaluator requires mixed
    # similarity scores but all our pairs are positives (label=1.0), which
    # causes ConstantInputWarning and meaningless Pearson/Spearman values.
    # We run a proper MRR eval on the held-out test set after training instead.
    evaluator = None

    # Train
    model.fit(
        train_objectives=[(train_dataloader, train_loss)],
        epochs=args.epochs,
        warmup_steps=warmup_steps,
        optimizer_params={"lr": args.learning_rate},
        evaluator=evaluator,
        evaluation_steps=0,  # no evaluation during training (we do our own below)
        output_path=str(output_dir),
        save_best_model=True,
        show_progress_bar=True,
    )

    print(f"[train] Model saved to {output_dir}", file=sys.stderr)

    # ----- Eval on test set with fine-tuned model -----
    if test_examples and corpus_texts:
        # Re-load the best saved model
        best_model = SentenceTransformer(str(output_dir), device=device, trust_remote_code=True)
        print("[train] Evaluating fine-tuned model...", file=sys.stderr)
        result = evaluate_mrr(best_model, test_examples, corpus_texts, corpus_ids, verbose=args.verbose)
        print(f"\n{'=' * 60}", file=sys.stderr)
        print(
            f"  Fine-tuned  MRR={result['mrr']}  hit@1={result['hit1']}  hit@3={result['hit3']}  n={result['n']}",
            file=sys.stderr,
        )
        if args.compare_baseline:
            print(
                f"  Baseline    MRR={baseline['mrr']}  hit@1={baseline['hit1']}  "
                f"hit@3={baseline['hit3']}  n={baseline['n']}",
                file=sys.stderr,
            )
            delta_mrr = result["mrr"] - baseline["mrr"]
            delta_h1 = result["hit1"] - baseline["hit1"]
            print(f"  Delta       MRR={delta_mrr:+.4f}  hit@1={delta_h1:+.4f}", file=sys.stderr)
        print(f"{'=' * 60}\n", file=sys.stderr)

        # JSON result line for programmatic consumption
        print(json.dumps(result))

    # Done
    print(f"[train] Done. Model: {output_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
