"""Build full corpus + queries from src/lemoncrow.

Output (under benchmarks/embedding/data/):
  corpus.jsonl       — every function/class chunk (id + text)
  queries.jsonl      — natural-language query → relevant chunk id(s)
"""
from __future__ import annotations

import ast
import json
import pathlib
import random
import re

SRC = pathlib.Path("src/lemoncrow")
OUT_DIR = pathlib.Path("benchmarks/embedding/data")
random.seed(42)


def _source_segment(source: str, node: ast.AST) -> str:
    lines = source.splitlines(keepends=True)
    start = node.lineno - 1 if hasattr(node, "lineno") else 0
    end = (node.end_lineno if hasattr(node, "end_lineno") and node.end_lineno else start)
    return "".join(lines[start:end])


def extract_chunks(filepath: str) -> list[dict]:
    path = pathlib.Path(filepath)
    rel = path.relative_to(SRC).with_suffix("")
    rel_str = str(rel).replace("/", ".")
    chunks = []

    try:
        source = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return chunks
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return chunks

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else \
                   "class" if isinstance(node, ast.ClassDef) else "function"
            chunk_id = f"{rel_str}::{node.name}"
            text = _source_segment(source, node)
            doc = ast.get_docstring(node) or ""
            chunks.append({
                "id": chunk_id,
                "file": str(rel),
                "symbol": node.name,
                "type": kind,
                "text": text,
                "docstring": doc,
            })
    return chunks


def split_long_chunks(chunks: list[dict], max_lines: int = 60) -> list[dict]:
    out = []
    for c in chunks:
        lines = c["text"].split("\n")
        if len(lines) <= max_lines:
            out.append(c)
        else:
            n = (len(lines) + max_lines - 1) // max_lines
            for i in range(n):
                a, b = i * max_lines, min((i + 1) * max_lines, len(lines))
                out.append({**c, "id": f"{c['id']}#part{i+1}", "text": "\n".join(lines[a:b])})
    return out


def make_queries(chunk: dict) -> list[dict]:
    """Generate natural-language queries from a chunk."""
    results = []
    symbol = chunk["symbol"]
    docstring = chunk["docstring"]
    ctype = chunk["type"]
    parts = re.split(r"[_]", symbol)
    desc = " ".join(p for p in parts if len(p) > 1)

    # From symbol name
    if desc:
        if ctype == "class":
            results.append(f"Which class implements {desc}?")
            results.append(f"Find the {desc} class definition")
        else:
            results.append(f"How does the code handle {desc}?")
            results.append(f"Where is {desc} implemented?")
            if parts and parts[0] in ("get", "set", "is", "has", "find", "load", "save", "create", "delete", "update"):
                results.append(f"Find the function that {parts[0]}s {' '.join(parts[1:])}")

    # From docstring
    if docstring:
        first = docstring.split(".")[0].strip()
        if len(first) > 15:
            if first.lower().startswith("return "):
                results.append(f"What does the function return when {' '.join(first.split()[1:])}?")
            elif first.lower().startswith("raise"):
                results.append(f"When is {first} raised?")
            elif not first.endswith("?"):
                q = first.rstrip(".")
                if len(q) > 120:
                    q = q[:117] + "..."
                results.append(q)

    # Deduplicate
    seen = set()
    final = []
    for q in results:
        key = q.lower().strip()
        if key not in seen and 8 < len(q) < 160:
            seen.add(key)
            final.append({"query": q, "relevant": [chunk["id"]]})
    return final


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    py_files = sorted(SRC.rglob("*.py"))
    all_chunks = []
    for fp in py_files:
        all_chunks.extend(extract_chunks(str(fp)))
    all_chunks = split_long_chunks(all_chunks)

    print(f"Total chunks: {len(all_chunks)} from {len(py_files)} files")

    # Write corpus
    corpus_path = OUT_DIR / "corpus.jsonl"
    with open(corpus_path, "w", encoding="utf-8") as f:
        for c in all_chunks:
            f.write(json.dumps({
                "id": c["id"],
                "text": f"Path: {c['file']}\nSymbol: {c['symbol']}\n\n{c['text']}",
            }) + "\n")
    print(f"Wrote {corpus_path} ({len(all_chunks)} chunks)")

    # Generate queries (1-3 per chunk, then shuffle + pick reasonable total)
    all_queries = []
    for c in all_chunks:
        all_queries.extend(make_queries(c))
    random.shuffle(all_queries)
    # Cap at ~2000 for practical benchmarking
    if len(all_queries) > 2000:
        all_queries = all_queries[:2000]

    queries_path = OUT_DIR / "queries.jsonl"
    with open(queries_path, "w", encoding="utf-8") as f:
        for q in all_queries:
            f.write(json.dumps(q) + "\n")
    print(f"Wrote {queries_path} ({len(all_queries)} queries)")

    # Summary
    print(f"\nDone. {len(all_chunks)} chunks, {len(all_queries)} queries")


if __name__ == "__main__":
    main()
