#!/usr/bin/env python3
"""Rebuild isolated per-repo secondary DBs for the codebench benchmark snapshots.

Root cause: eval_external_provider_mrr.py's _route_db() resolves sibling DBs
via db.parent/"fts.sqlite" -- the SAME convention the engine itself uses
(CodeContextEngine.{intel,fts,vectors}_db_path are always db_path.parent-
relative). Every frozen snapshot used to sit flat in /tmp
(/tmp/idx_<repo>.db), so db.parent was /tmp for EVERY repo, and that lookup
resolved to ONE shared /tmp/fts.sqlite / /tmp/intel.sqlite for all 14 repos --
mixing every repo's file_line_fts / references / call_edges / centrality_map
together. Confirmed: /tmp/fts.sqlite's file_line_fts held 8 different repos'
lines (2.85M rows; astropy's own share was only 500K).

The fix is simply to give each repo its own directory, exactly like a real
workspace has: point `lc code index --db-path` at
/tmp/idx_isolated/<repo>/code_context.sqlite directly (no intermediate temp
dir, no copy-out step -- the earlier version of this script did that and hit
a stale-WAL corruption bug from the copy; writing straight to the final
location sidesteps that class of bug entirely). intel.sqlite/fts.sqlite/
vectors.sqlite land alongside it automatically, correctly isolated.

Usage:
  uv run python scripts/rebuild_isolated_bench_secondary_dbs.py [prefix-substring ...]
  (no args = all repos in the manifest; args filter by substring match on repo prefix)
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ISOLATED_ROOT = Path("/tmp/idx_isolated")
MANIFEST_NAMES = [
    "bench_pairs_def_gold.json",
    "bench_pairs_content_gold.json",
    "bench_pairs_semantic_gold.json",
    "bench_pairs_swebench_gold.json",
    "bench_pairs_sessions_gold.json",
]


def _load_all_repo_meta() -> dict[str, dict]:
    """Union of repos across every gold manifest (they're kept in sync, but
    read all of them rather than assume one is authoritative)."""
    merged: dict[str, dict] = {}
    for name in MANIFEST_NAMES:
        path = REPO_ROOT / "benchmarks/codebench/data" / name
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        for prefix, meta in data.get("repos", {}).items():
            merged.setdefault(prefix, meta)
    return merged


def _snapshot_counts(db_path: Path) -> tuple[int, int]:
    """Return indexed file/symbol counts, failing if the snapshot is unreadable."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    try:
        files = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        symbols = int(conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0])
    finally:
        conn.close()
    return files, symbols


def _update_manifests(prefix: str, new_db_path: Path) -> None:
    """Point every gold manifest's repos[prefix].db at the isolated path."""
    for name in MANIFEST_NAMES:
        path = REPO_ROOT / "benchmarks/codebench/data" / name
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        repos = data.get("repos", {})
        if prefix not in repos:
            continue
        if repos[prefix].get("db") == str(new_db_path):
            continue
        repos[prefix]["db"] = str(new_db_path)
        path.write_text(json.dumps(data, indent=2) + "\n")


def main() -> None:
    filters = sys.argv[1:]
    repos = _load_all_repo_meta()

    for prefix, meta in sorted(repos.items()):
        if filters and not any(f in prefix for f in filters):
            continue
        ws = Path(meta["ws"])
        if not ws.is_dir():
            print(f"[skip] {prefix}: workspace not found ({ws})", flush=True)
            continue

        isolated_dir = ISOLATED_ROOT / prefix
        isolated_db = isolated_dir / "code_context.sqlite"
        isolated_dir.mkdir(parents=True, exist_ok=True)

        t0 = time.time()
        print(f"[start] {prefix} ws={ws} -> {isolated_db}", flush=True)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "lemoncrow.gateway.cli",
                "code",
                "index",
                "--repo-root",
                str(ws),
                "--reindex",
                "--no-stats",
                "--db-path",
                str(isolated_db),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=7200,
        )
        elapsed = time.time() - t0
        if result.returncode != 0:
            print(f"[FAIL] {prefix} after {elapsed:.0f}s (rc={result.returncode}):", flush=True)
            print(result.stderr[-3000:], flush=True)
            continue
        if not isolated_db.exists():
            print(f"[FAIL] {prefix}: no code_context.sqlite built at {isolated_db}", flush=True)
            continue
        try:
            files, symbols = _snapshot_counts(isolated_db)
        except sqlite3.Error as exc:
            print(f"[FAIL] {prefix}: unreadable snapshot {isolated_db}: {exc}", flush=True)
            continue
        if files <= 0 or symbols <= 0:
            print(
                f"[FAIL] {prefix}: empty snapshot after successful index command "
                f"(files={files}, symbols={symbols})",
                flush=True,
            )
            continue

        _update_manifests(prefix, isolated_db)
        siblings = [p.name for p in isolated_dir.glob("*.sqlite") if p.name != "code_context.sqlite"]
        print(
            f"[done] {prefix} in {elapsed:.0f}s -- files={files} symbols={symbols} siblings: {siblings}",
            flush=True,
        )


if __name__ == "__main__":
    main()
