"""Provision every repo used by the retrieval benchmark's definition + content gold.

Merges what used to be three separate scripts into one idempotent entrypoint:
  - _provision_repos.py         diverse-6 SWE-bench repos, dump-mined queries
                                 anchored to a real base_commit
  - _provision_missing_repos.py 6 more repos, dump-mined at HEAD (no swebench
                                 base_commit available)
  - _provision_linux_kernel.py  the Linux kernel, scoped to its core subsystems
                                 and mined from its own symbol index (no
                                 SWE-bench dumps exist for it)

Per-repo work (clone, index, mine) is skipped when already present, so
re-running is cheap. Default runs every stage in order:

  1. diverse6  -- django, pytest, astropy, sympy, scikit-learn, xarray
  2. missing   -- matplotlib, seaborn, flask, requests, pylint, sphinx
  3. derive    -- build_definition_gold.py + build_content_gold.py from the
                  merged raw pairs -> bench_pairs_{def,content}_gold.json
  4. linux     -- torvalds/linux, merged into bench_pairs_def_gold.json

Usage (run as a module -- it imports benchmarks.codebench.swebench_data, so a bare
script path won't resolve that import):
    uv run python -m benchmarks.codebench.provision_repos               # everything
    uv run python -m benchmarks.codebench.provision_repos --only linux  # just the kernel
    uv run python -m benchmarks.codebench.provision_repos --only diverse6 --only missing
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import re
import signal
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "src")
from lemoncrow.core.capabilities.code_context.engine import CodeContextEngine

from benchmarks.codebench import swebench_data

try:
    from lemoncrow.infra.code_intel.zoekt.adapter import get_zoekt_supervisor
except Exception:
    get_zoekt_supervisor = None

RUN = Path("reports/benchmark/codebench/swe50_stress_run1")
DATA = Path("benchmarks/codebench/data")
SWEBENCH_GOLD = DATA / "bench_pairs_swebench_gold.json"
DEF_GOLD = DATA / "bench_pairs_def_gold.json"

TESTRE = re.compile(r"(^|/)(test_|tests?/|conftest)")
TID_RE = re.compile(r"^(.*?)_(?:lemoncrow|baseline)_rep\d+\.flow_dump\.txt$")
GREP_RE = re.compile(r"mcp__lc__grep\] (\{.*?\})", re.S)
EDIT_RE = re.compile(r"mcp__lc__edit\] (\{.*?\})", re.S)

DIVERSE6_REPOS = {
    "django__django": "django/django",
    "pytest-dev__pytest": "pytest-dev/pytest",
    "astropy__astropy": "astropy/astropy",
    "sympy__sympy": "sympy/sympy",
    "scikit-learn__scikit-learn": "scikit-learn/scikit-learn",
    "pydata__xarray": "pydata/xarray",
}
MISSING_REPOS = {
    "matplotlib__matplotlib": "matplotlib/matplotlib",
    "mwaskom__seaborn": "mwaskom/seaborn",
    "pallets__flask": "pallets/flask",
    "psf__requests": "psf/requests",
    "pylint-dev__pylint": "pylint-dev/pylint",
    "sphinx-doc__sphinx": "sphinx-doc/sphinx",
}

# ── Shared helpers ───────────────────────────────────────────────────────────


def symbol_count(db: Path) -> int:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        n = con.execute("SELECT count(*) FROM symbols").fetchone()[0]
        con.close()
        return int(n)
    except Exception:
        return -1


def mine_grep_queries(dump: Path) -> list[str]:
    out = []
    for blob in GREP_RE.findall(dump.read_text(errors="replace")):
        m = re.search(r'"regex":\s*"((?:[^"\\]|\\.)*)"', blob)
        if m:
            q = m.group(1).encode().decode("unicode_escape", "replace")
            if 3 <= len(q) <= 80:
                out.append(q)
    return out


def mine_edited_files(dump: Path, ws: Path) -> list[str]:
    """Extract .py files edited in the dump that exist in the workspace."""
    found: set[str] = set()
    for blob in EDIT_RE.findall(dump.read_text(errors="replace")):
        for path_match in re.finditer(r'"path":\s*"([^"]+)"', blob):
            p = path_match.group(1)
            if not p.endswith(".py") or TESTRE.search(p):
                continue
            rel = p.lstrip("/")
            for part in ws.parts:
                prefix = part + "/"
                if rel.startswith(prefix):
                    rel = rel[len(prefix) :]
            if (ws / rel).exists():
                found.add(rel)
    return list(found)


def _index_repo(prefix: str, ws: Path, db: Path) -> None:
    if not db.exists():
        print(f"[{prefix}] indexing -> {db}", flush=True)
        t0 = time.time()
        try:
            CodeContextEngine(ws, db_path=db, autosync_enabled=False).index_repo()
        except Exception as e:
            print(f"[{prefix}] INDEX FAILED: {e}", flush=True)
            return
        print(f"[{prefix}] index done {time.time() - t0:.0f}s, symbols={symbol_count(db)}", flush=True)
    else:
        print(f"[{prefix}] index exists, symbols={symbol_count(db)}", flush=True)
    if get_zoekt_supervisor is not None:
        try:
            get_zoekt_supervisor(ws)
        except Exception as e:
            print(f"[{prefix}] zoekt warn: {e}", flush=True)


# ── Stage 1: diverse-6 (SWE-bench anchored) ─────────────────────────────────


def provision_diverse6() -> None:
    """Clone at each repo's mined SWE-bench base_commit, index, and mine (query,
    gold-file) pairs from real task patches. Writes bench_pairs_swebench_gold.json."""
    repos_meta: dict[str, dict] = {}
    pairs: list[list[str]] = []
    true_map: dict[str, list[str]] = {}

    for prefix, repo in DIVERSE6_REPOS.items():
        dumps = sorted(d for d in RUN.glob(f"{prefix}*_dump.txt") if TID_RE.match(d.name))
        by_task: dict[str, list[str]] = {}
        for d in dumps:
            by_task.setdefault(TID_RE.match(d.name).group(1), []).extend(mine_grep_queries(d))
        if not by_task:
            print(f"[{prefix}] no dumps, skip", flush=True)
            continue
        task_ids = sorted(by_task)
        insts = {i.instance_id: i for i in swebench_data.load_instances(dataset=None, instances=task_ids)}
        anchor = max(task_ids, key=lambda t: len(by_task.get(t, [])))
        base_commit = getattr(insts.get(anchor), "base_commit", "") if insts.get(anchor) else ""

        safe = prefix.replace("/", "_")
        ws, db = Path(f"/tmp/idx_ws_{safe}"), Path(f"/tmp/idx_{safe}.db")
        if not ws.exists() or not any(ws.iterdir()):
            # Shallow-fetch just the pinned commit (not full history) -- a plain
            # `git clone` of a large/old repo (e.g. django) can take long enough to
            # blow the timeout and leave a broken .git-only checkout behind.
            print(f"[{prefix}] shallow-fetch {repo}@{base_commit[:10]} -> {ws}", flush=True)
            ws.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "--quiet", str(ws)], check=True, timeout=60)
            subprocess.run(
                ["git", "-C", str(ws), "remote", "add", "origin", f"https://github.com/{repo}.git"],
                check=True,
                timeout=60,
            )
            rev = base_commit or "HEAD"
            subprocess.run(
                ["git", "-C", str(ws), "fetch", "--quiet", "--depth", "1", "origin", rev], check=True, timeout=1200
            )
            subprocess.run(["git", "-C", str(ws), "checkout", "--quiet", "FETCH_HEAD"], check=True, timeout=300)
        _index_repo(prefix, ws, db)

        kept = 0
        for tid in task_ids:
            inst = insts.get(tid)
            files = re.findall(r"^\+\+\+ b/(.+)$", getattr(inst, "patch", "") or "", re.M) if inst else []
            files = [f for f in files if not TESTRE.search(f) and (ws / f).exists()]
            if files:
                true_map[tid] = files
                for q in by_task.get(tid, []):
                    pairs.append([q, tid, prefix])
                    kept += 1
        repos_meta[prefix] = {"ws": str(ws), "db": str(db), "anchor": anchor, "base_commit": base_commit}
        print(f"[{prefix}] ready: {kept} pairs, symbols={symbol_count(db)}", flush=True)

    SWEBENCH_GOLD.write_text(json.dumps({"pairs": pairs, "true_map": true_map, "repos": repos_meta}))
    uniq = len({(q, p) for q, _, p in pairs})
    print(f"\n[diverse6] {len(pairs)} pairs | {uniq} unique (query,repo) | {len(repos_meta)} repos", flush=True)


# ── Stage 2: missing-6 (HEAD-only, dump-mined) ──────────────────────────────


def provision_missing() -> None:
    """Clone at HEAD, index, and mine (query, gold-file) pairs from edited files in
    the dumps (no swebench base_commit available). Merges into bench_pairs_swebench_gold.json."""
    data = json.loads(SWEBENCH_GOLD.read_text())
    existing_repos, existing_true, existing_pairs = data["repos"], data["true_map"], data["pairs"]

    new_pairs: list[list[str]] = []
    added_repos: dict[str, dict] = {}
    added_true: dict[str, list[str]] = {}

    for prefix, repo in MISSING_REPOS.items():
        dumps = sorted(d for d in RUN.glob(f"{prefix}*_dump.txt") if TID_RE.match(d.name))
        if not dumps:
            print(f"[{prefix}] no dump files, skip", flush=True)
            continue

        ws, db = Path(f"/tmp/idx_ws_{prefix}"), Path(f"/tmp/idx_{prefix}.db")
        if not ws.exists() or not any(ws.iterdir()):
            print(f"[{prefix}] cloning {repo} ...", flush=True)
            subprocess.run(
                ["git", "clone", "--quiet", "--depth", "1", f"https://github.com/{repo}.git", str(ws)],
                check=True,
                timeout=1200,
            )
        else:
            print(f"[{prefix}] workspace exists", flush=True)
        _index_repo(prefix, ws, db)

        by_task: dict[str, dict] = {}
        for d in dumps:
            tid = TID_RE.match(d.name).group(1)  # type: ignore[union-attr]
            queries = mine_grep_queries(d)
            gold = mine_edited_files(d, ws)
            if queries and gold:
                by_task.setdefault(tid, {"queries": [], "gold": set()})
                by_task[tid]["queries"].extend(queries)
                by_task[tid]["gold"].update(gold)

        kept = 0
        for tid, d in by_task.items():
            gold = list(d["gold"])
            if not gold:
                continue
            added_true[tid] = gold
            for q in d["queries"]:
                new_pairs.append([q, tid, prefix])
                kept += 1

        added_repos[prefix] = {"ws": str(ws), "db": str(db), "anchor": "HEAD", "base_commit": ""}
        print(f"[{prefix}] {kept} pairs from {len(by_task)} tasks", flush=True)

    if not new_pairs:
        print("[missing] nothing to add", flush=True)
        return

    existing_repos.update(added_repos)
    existing_true.update(added_true)
    merged = existing_pairs + new_pairs
    uniq = len({(q, p) for q, _, p in merged})
    data["pairs"], data["true_map"], data["repos"] = merged, existing_true, existing_repos
    SWEBENCH_GOLD.write_text(json.dumps(data))
    print(f"[missing] +{len(new_pairs)} pairs across {len(added_repos)} new repos", flush=True)
    print(f"[missing] total: {len(merged)} pairs | {uniq} unique (query,repo)", flush=True)


# ── Stage 3: derive definition + content gold from the merged raw pairs ────


def derive_golds() -> None:
    """Derive the canonical retrieval golds every eval reads from bench_pairs_swebench_gold.json."""
    print("[derive] definition gold -> bench_pairs_def_gold.json ...", flush=True)
    subprocess.run([sys.executable, "benchmarks/codebench/build_definition_gold.py"], check=True)
    print("[derive] content gold -> bench_pairs_content_gold.json ...", flush=True)
    subprocess.run([sys.executable, "benchmarks/codebench/build_content_gold.py"], check=True)


# ── Stage 4: linux kernel (scoped, mined from its own symbol index) ────────

_LINUX_PREFIX = "torvalds__linux"
_LINUX_FULL_CLONE = Path("/tmp/idx_ws_linux")
_LINUX_WS = Path("/tmp/idx_ws_linux_core")
_LINUX_DB = Path("/tmp/idx_linux_core.db")
_LINUX_CORE_SUBTREES = ["kernel", "mm", "fs", "block", "ipc", "lib", "security", "crypto", "init", "virt", "include"]
_LINUX_URL = "https://github.com/torvalds/linux.git"

_LEMONCROW_URL = "https://github.com/lemoncrowhq/lemoncrow.git"
_LINUX_MULTI = Path("/tmp/bench_pairs_linux.json")
_LINUX_GOLD = Path("/tmp/bench_pairs_linux_def_gold.json")

# Symbol kinds that name a real definition (skip noise like variables/params/fields).
_DEF_KINDS = {"function", "struct", "class", "enum", "union", "typedef", "macro", "method"}
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{3,}$")
# Identifiers too generic to make a discriminating query (kernel is full of these).
_GENERIC = {
    "init",
    "exit",
    "open",
    "close",
    "read",
    "write",
    "start",
    "stop",
    "show",
    "store",
    "probe",
    "remove",
    "alloc",
    "free",
    "lock",
    "unlock",
    "get",
    "put",
    "set",
    "reset",
    "enable",
    "disable",
    "suspend",
    "resume",
    "register",
    "unregister",
    "handler",
    "create",
    "destroy",
    "update",
    "flush",
    "sync",
    "name",
    "size",
    "data",
    "info",
    "type",
    "node",
}


def _linux_prepare(commit_ref: str | None = None) -> None:
    """Clone (shallow), scope to the core subtrees, and LemonCrow-index the workspace.

    The full kernel tree is too large to provision whole (~64k C files, ~30M LOC
    across drivers/ + arch/ that are hardware-specific and repetitive), so we scope
    to the stable, symbol-rich core subsystems -- ~11k C files / ~4.5M LOC.

    When *commit_ref* is set, the clone is pinned to that tag/commit instead
    of HEAD, for reproducible evaluation.
    """
    if not _LINUX_WS.exists() or not any(_LINUX_WS.iterdir()):
        if not _LINUX_FULL_CLONE.exists():
            ref = commit_ref or "HEAD"
            print(f"[linux] shallow clone {_LINUX_URL} @ {ref[:12]} -> {_LINUX_FULL_CLONE}", flush=True)
            _LINUX_FULL_CLONE.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "--quiet", str(_LINUX_FULL_CLONE)], check=True, timeout=60)
            subprocess.run(
                ["git", "-C", str(_LINUX_FULL_CLONE), "remote", "add", "origin", _LINUX_URL],
                check=True,
                timeout=60,
            )
            subprocess.run(
                ["git", "-C", str(_LINUX_FULL_CLONE), "fetch", "--quiet", "--depth", "1", "origin", ref],
                check=True,
                timeout=3600,
            )
            subprocess.run(
                ["git", "-C", str(_LINUX_FULL_CLONE), "checkout", "--quiet", "FETCH_HEAD"],
                check=True,
                timeout=300,
            )
        _LINUX_WS.mkdir(parents=True, exist_ok=True)
        print(f"[linux] scoping core subtrees -> {_LINUX_WS}", flush=True)
        for d in _LINUX_CORE_SUBTREES:
            src, dst = _LINUX_FULL_CLONE / d, _LINUX_WS / d
            if src.is_dir() and not dst.exists():
                src.rename(dst)
        mk = _LINUX_FULL_CLONE / "Makefile"
        if mk.exists() and not (_LINUX_WS / "Makefile").exists():
            (_LINUX_WS / "Makefile").write_bytes(mk.read_bytes())
    n_c = sum(1 for _ in _LINUX_WS.rglob("*.c"))
    n_h = sum(1 for _ in _LINUX_WS.rglob("*.h"))
    print(f"[linux] scoped ws: {n_c} .c + {n_h} .h files", flush=True)
    if not _LINUX_DB.exists() or _LINUX_DB.stat().st_size < 1_000_000:
        print(f"[linux] indexing -> {_LINUX_DB} (this takes a few minutes) ...", flush=True)
        t0 = time.time()
        CodeContextEngine(_LINUX_WS, db_path=_LINUX_DB, autosync_enabled=False).index_repo()
        print(f"[linux] index done {time.time() - t0:.0f}s symbols={symbol_count(_LINUX_DB)}", flush=True)
    else:
        print(f"[linux] index exists, symbols={symbol_count(_LINUX_DB)}", flush=True)


def _linux_mine(*, n_single: int, n_alt: int, alt_size: int, max_def: int, seed: int) -> None:
    """Mine a (query, gold-file) universe from the symbol index, then derive the def gold.

    No SWE-bench dumps exist for the kernel, so gold queries are mined from the
    symbol index itself: bare specific symbol names, and clean alternations of
    symbols that share a definition file (mirroring the SWE grep style). Deterministic
    (seeded) so the gold is reproducible.
    """
    rng = random.Random(seed)
    con = sqlite3.connect(f"file:{_LINUX_DB}?mode=ro", uri=True)
    rows = con.execute("SELECT symbol_name, file_path, kind FROM symbols").fetchall()
    con.close()

    name_files: dict[str, set[str]] = defaultdict(set)
    file_names: dict[str, set[str]] = defaultdict(set)
    for name, fp, kind in rows:
        if not name or not fp or (kind or "").lower() not in _DEF_KINDS:
            continue
        if not _IDENT_RE.match(name) or name.lower() in _GENERIC:
            continue
        name_files[name].add(fp.replace("\\", "/"))
        file_names[fp.replace("\\", "/")].add(name)

    specific = [n for n, fs in name_files.items() if 1 <= len(fs) <= max_def]
    rng.shuffle(specific)
    print(f"[linux] {len(name_files)} def-symbols, {len(specific)} specific (<= {max_def} files)", flush=True)

    queries: list[str] = []
    seen: set[str] = set()
    for name in specific:
        if len(queries) >= n_single:
            break
        if name not in seen:
            seen.add(name)
            queries.append(name)

    files_with_specifics = [
        (f, sorted(ns & set(specific))) for f, ns in file_names.items() if len(ns & set(specific)) >= alt_size
    ]
    rng.shuffle(files_with_specifics)
    n_made = 0
    for _f, names in files_with_specifics:
        if n_made >= n_alt:
            break
        picks = rng.sample(names, alt_size)
        q = "|".join(picks)
        if q not in seen:
            seen.add(q)
            queries.append(q)
            n_made += 1

    print(f"[linux] mined {len(queries)} queries ({n_single} single + {n_made} alternation)", flush=True)

    pairs = [[q, f"lk-{i}", _LINUX_PREFIX] for i, q in enumerate(queries)]
    repos_meta = {_LINUX_PREFIX: {"ws": str(_LINUX_WS), "db": str(_LINUX_DB), "anchor": "HEAD", "base_commit": "HEAD"}}
    _LINUX_MULTI.parent.mkdir(parents=True, exist_ok=True)
    _LINUX_MULTI.write_text(json.dumps({"pairs": pairs, "true_map": {p[1]: [] for p in pairs}, "repos": repos_meta}))
    print(f"[linux] wrote {_LINUX_MULTI}", flush=True)

    print("[linux] deriving definition gold via build_definition_gold.py ...", flush=True)
    r = subprocess.run(
        [
            sys.executable,
            "benchmarks/codebench/build_definition_gold.py",
            "--in",
            str(_LINUX_MULTI),
            "--out",
            str(_LINUX_GOLD),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    sys.stderr.write(r.stderr)
    if r.returncode != 0:
        raise RuntimeError("build_definition_gold failed")
    g = json.loads(_LINUX_GOLD.read_text())
    print(f"[linux] gold: {len(g['pairs'])} scorable pairs, {len(g['true_map'])} golds", flush=True)


def _linux_merge() -> None:
    """Append the kernel repo + pairs + golds into the shared definition gold file."""
    lk = json.loads(_LINUX_GOLD.read_text())
    shared = json.loads(DEF_GOLD.read_text())
    shared["repos"].update(lk["repos"])
    existing = {(q, p) for q, _t, p in shared["pairs"]}
    added = 0
    for q, tid, p in lk["pairs"]:
        if (q, p) not in existing:
            shared["pairs"].append([q, tid, p])
            shared["true_map"][tid] = lk["true_map"][tid]
            added += 1
    DEF_GOLD.write_text(json.dumps(shared))
    print(f"[linux] merged {added} kernel pairs into {DEF_GOLD}", flush=True)


def provision_linux(
    *, n_single: int = 400, n_alt: int = 200, alt_size: int = 3, max_def: int = 3, seed: int = 20260629
) -> None:
    _linux_prepare()
    _linux_mine(n_single=n_single, n_alt=n_alt, alt_size=alt_size, max_def=max_def, seed=seed)
    _linux_merge()


# ── Eval auto-provision ──────────────────────────────────────────────────────


def ensure_eval_workspaces(gold_paths: list[Path]) -> None:
    """Clone + index repos from gold files whose workspaces/indexes are missing.

    Idempotent: skips repos whose ws and db already exist.
    Designed to be called by ``lemoncrow eval retrieval`` before the benchmark
    subprocess so the eval never fails with "ws not found" for the standard
    gold-set repos.
    """
    import shutil
    import subprocess

    # 1. Union of repos across all gold files
    repos: dict[str, dict] = {}
    for gp in gold_paths:
        raw = json.loads(gp.read_text())
        for prefix, meta in raw.get("repos", {}).items():
            if prefix not in repos:
                repos[prefix] = meta

    # 2. URL map (prefix -> full git URL) -- mirrors DIVERSE6_REPOS,
    #    MISSING_REPOS, and the linux constants above.
    url_map: dict[str, str] = {}
    for prefix, repo in DIVERSE6_REPOS.items():
        url_map[prefix] = f"https://github.com/{repo}.git"
    for prefix, repo in MISSING_REPOS.items():
        url_map[prefix] = f"https://github.com/{repo}.git"
    url_map[_LINUX_PREFIX] = _LINUX_URL
    url_map["lemoncrow__lc"] = _LEMONCROW_URL

    for prefix, meta in sorted(repos.items()):
        ws = Path(meta["ws"])
        db_path = Path(meta.get("db", "")) if meta.get("db") else None

        ws_missing = not ws.exists() or not any(ws.iterdir())
        db_missing = db_path is not None and not db_path.exists()

        if not ws_missing and not db_missing:
            continue

        # ── Clone workspace ──────────────────────────────────────────────
        if ws_missing:
            if prefix == _LINUX_PREFIX:
                ref = meta.get("base_commit", "") or None
                _linux_prepare(commit_ref=ref)
                # _linux_prepare handles the full clone + scope + index for
                # the kernel workspace. The isolated DB still gets built below.
            elif prefix in url_map:
                url = url_map[prefix]
                base_commit = meta.get("base_commit", "") or "HEAD"
                print(f"[provision] shallow clone {url} @ {base_commit[:12]} -> {ws}", flush=True)
                if ws.exists():
                    shutil.rmtree(ws)
                ws.mkdir(parents=True, exist_ok=True)
                subprocess.run(["git", "init", "--quiet", str(ws)], check=True, timeout=60)
                subprocess.run(
                    ["git", "-C", str(ws), "remote", "add", "origin", url],
                    check=True,
                    timeout=60,
                )
                subprocess.run(
                    ["git", "-C", str(ws), "fetch", "--quiet", "--depth", "1", "origin", base_commit],
                    check=True,
                    timeout=1200,
                )
                subprocess.run(
                    ["git", "-C", str(ws), "checkout", "--quiet", "FETCH_HEAD"],
                    check=True,
                    timeout=300,
                )
            else:
                print(f"[provision] no URL mapping for {prefix}, skip clone", flush=True)

        # ── Build index DB ───────────────────────────────────────────────
        if db_missing and db_path is not None and ws.exists() and any(ws.iterdir()):
            isolated_dir = db_path.parent
            isolated_dir.mkdir(parents=True, exist_ok=True)
            print(f"[provision] indexing {prefix} -> {db_path}", flush=True)
            # `code index` forks its own worker pool for parallel file
            # indexing. subprocess.run(timeout=...) only SIGKILLs the direct
            # child on timeout -- the worker pool is reparented to init and
            # keeps running (and can keep rewriting db_path) indefinitely.
            # Same fix as eval_external_provider_mrr.py's
            # _backfill_semantic_vectors: run in its own process group so a
            # timeout can SIGKILL the whole group, not just the one child.
            proc = subprocess.Popen(
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
                    str(db_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                _stdout, stderr = proc.communicate(timeout=7200)
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                try:
                    _stdout, stderr = proc.communicate(timeout=10)
                except Exception:
                    stderr = ""
                returncode = -1
                print(f"[provision] index TIMED OUT for {prefix}, killed process group {proc.pid}", flush=True)
            if returncode != 0:
                sys.stderr.write(stderr or "")
                print(f"[provision] index FAILED for {prefix} (exit {returncode})", flush=True)
            else:
                print(f"[provision] index done {prefix}", flush=True)

        if ws_missing or db_missing:
            print(f"[provision] ready {prefix}", flush=True)


# ── Entrypoint ───────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--only",
        choices=["diverse6", "missing", "linux"],
        action="append",
        help="Run only these stages (repeatable). Default: every stage, in order.",
    )
    ap.add_argument("--linux-n-single", type=int, default=400)
    ap.add_argument("--linux-n-alt", type=int, default=200)
    ap.add_argument("--linux-alt-size", type=int, default=3)
    ap.add_argument("--linux-max-def", type=int, default=3)
    ap.add_argument("--linux-seed", type=int, default=20260629)
    args = ap.parse_args()

    stages = args.only or ["diverse6", "missing", "linux"]

    if "diverse6" in stages:
        provision_diverse6()
    if "missing" in stages:
        provision_missing()
    if "diverse6" in stages or "missing" in stages:
        derive_golds()
    if "linux" in stages:
        provision_linux(
            n_single=args.linux_n_single,
            n_alt=args.linux_n_alt,
            alt_size=args.linux_alt_size,
            max_def=args.linux_max_def,
            seed=args.linux_seed,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
