from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import click


def _gpu_supports_embedder(min_free_mb: int = 512) -> tuple[bool, str]:
    """Return (ok, reason). True when a CUDA GPU with enough free VRAM is present.

    Uses ``nvidia-smi`` so it works regardless of which Python env is active.
    Falls back to ``torch.cuda`` when nvidia-smi is unavailable.
    """
    import subprocess

    # Primary: nvidia-smi — works in any Python env
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if not out:
            return False, "nvidia-smi returned no GPU info"
        free_mb = max(int(line.strip()) for line in out.splitlines() if line.strip())
        if free_mb < min_free_mb:
            return False, f"only {free_mb} MB free VRAM (need {min_free_mb} MB)"
        return True, f"{free_mb} MB free VRAM"
    except FileNotFoundError:
        pass  # nvidia-smi not installed; fall through to torch
    except Exception as exc:  # noqa: BLE001
        return False, f"nvidia-smi error: {exc}"

    # Fallback: torch (only available when torch is installed in this env)
    try:
        import torch

        if not torch.cuda.is_available():
            return False, "no CUDA GPU detected"
        free_bytes, _ = torch.cuda.mem_get_info()
        free_mb = free_bytes // (1024 * 1024)
        if free_mb < min_free_mb:
            return False, f"only {free_mb} MB free VRAM (need {min_free_mb} MB)"
        return True, f"{free_mb} MB free VRAM"
    except ImportError:
        return False, "nvidia-smi not found and torch not installed"
    except Exception as exc:  # noqa: BLE001
        return False, f"GPU check failed: {exc}"


@click.group(name="eval")
def eval_() -> None:
    """Evaluation case management."""


@eval_.command("mcp")
@click.option("--out", type=click.Path(path_type=Path, file_okay=False), default=None)
@click.option(
    "--tool",
    "tools",
    multiple=True,
    metavar="NAME",
    help="Run only the named tool suite(s), e.g. --tool node --tool read. "
    "Repeatable or comma-separated; use 'code' for all code-intel tools. Default: all tools.",
)
@click.option(
    "--jobs",
    type=int,
    default=0,
    show_default="auto",
    help="Parallel suite shards. Use 0 to auto-size.",
)
def eval_mcp(out: Path | None, tools: tuple[str, ...], jobs: int) -> None:
    """No LLM: Runs the public MCP tool benchmark suite and write results."""
    from lemoncrow.gateway.cli.commands import benchmark as _bm

    repo_root = Path.cwd().resolve()
    suite_filter = _bm._mcp_suite_filter(tools)
    if suite_filter is not None:
        _bm._validate_mcp_suites(suite_filter, repo_root=repo_root)
    run_dir = _bm._run_dir("mcp", out)
    workspace_dir = _bm._workspace_dir("mcp", repo_root=repo_root, run_id=run_dir.name)
    resolved_jobs = _bm._resolve_mcp_jobs(jobs, repo_root=repo_root, suite_names=suite_filter)
    from lemoncrow.gateway.cli.progress import ProgressReporter

    progress = ProgressReporter("mcp", total=1)
    progress.start("starting benchmark", current=f"reports {run_dir} | jobs {resolved_jobs}")
    bench_root = _bm._bench_source_root()
    cmd = [
        *_bm._python_cmd(bench_root),
        "-m",
        "benchmarks.mcp_tools.export_public_mcp_csv",
        "--artifact-root",
        str(workspace_dir),
        "--csv-out",
        str(run_dir / "results.csv"),
        "--jobs",
        str(resolved_jobs),
    ]
    if suite_filter is not None:
        cmd += ["--suites", ",".join(suite_filter)]
    _bm._run(cmd, cwd=bench_root, label="MCP benchmark")
    progress.step("benchmark command complete", current="public MCP tools")
    progress.finish("benchmark complete")
    click.echo(f"Results: {run_dir}")


# LemonCrow channels are env-toggled variants of the SAME shipped stdio surface
# (--provider lemoncrow): zoekt, semantic, lexical, lexical+zoekt, lexical+zoekt+semantic.
_LEMONCROW_CHANNELS = ["zoekt", "semantic", "lexical", "lexical+zoekt", "lexical+zoekt+semantic"]
_EXTERNAL_CHANNELS = ["cg", "ctags", "ast-grep", "serena", "code-index-mcp", "jcodemunch", "rg", "cmm", "fff", "ccc"]
_RETRIEVAL_CHANNELS = _LEMONCROW_CHANNELS + _EXTERNAL_CHANNELS
_ALL_CHANNEL = "all"


def _make_golds(pairs: tuple[Path, ...]) -> list[Path]:
    if pairs:
        return list(pairs)
    # Default: every gold set that exists, so a bare `eval retrieval` scores the
    # full ~7.5k-query suite (def + content + semantic + swebench + sessions)
    # across all channels, not just a swebench+sessions subset. Narrow with
    # --pairs <file> (single gold) or --sample N when a quick run is wanted.
    base = Path("benchmarks/codebench/data")
    names = [
        "bench_pairs_def_gold.json",
        "bench_pairs_content_gold.json",
        "bench_pairs_semantic_gold.json",
        "bench_pairs_swebench_gold.json",
        "bench_pairs_sessions_gold.json",
    ]
    found: list[Path] = []
    seen_kinds: set[str] = set()
    for name in names:
        path = base / name
        if not path.exists():
            continue
        kind = "semantic" if "semantic" in name else name.removeprefix("bench_pairs_").removesuffix("_gold.json")
        if kind in seen_kinds:
            continue
        seen_kinds.add(kind)
        found.append(path)
    return found


def _fitness_gold_kind(query: str, tid: str) -> str:
    import re

    if tid.startswith("sem-"):
        return "semantic"
    if re.search(r"\b(?:async\s+def|def|class)\s+[A-Za-z_]\w*", query):
        return "definition"
    if "|" in query or ".*" in query or "\\" in query or re.search(r"\[[^\]]+\]", query):
        return "regex"
    return "content"


def _split_fitness_pairs(path: Path) -> list[Path]:
    import json

    data = json.loads(path.read_text())
    pairs = data.get("pairs", [])
    true_map = data.get("true_map", {})
    repos = data.get("repos", {})
    buckets: dict[str, list[list[str]]] = {"definition": [], "regex": [], "content": [], "semantic": []}
    for raw in pairs:
        if len(raw) != 3:
            continue
        query, tid, prefix = str(raw[0]), str(raw[1]), str(raw[2])
        buckets.setdefault(_fitness_gold_kind(query, tid), []).append([query, tid, prefix])

    split_paths: list[Path] = []
    for kind in ("definition", "regex", "content", "semantic"):
        kind_pairs = buckets.get(kind, [])
        if not kind_pairs:
            continue
        kind_tids = {entry[1] for entry in kind_pairs}
        out_path = path.with_name(f"{path.stem}.{kind}{path.suffix}")
        out_path.write_text(
            json.dumps(
                {
                    "gold_kind": kind,
                    "pairs": kind_pairs,
                    "true_map": {tid: true_map[tid] for tid in kind_tids if tid in true_map},
                    "repos": repos,
                },
                indent=2,
            )
        )
        split_paths.append(out_path)
        click.echo(f"[eval fitness] split {kind}: {len(kind_pairs)} pairs -> {out_path}", err=True)
    return split_paths or [path]


def _channel_cmd_env(
    channel: str,
    *,
    full: bool,
    sample: int,
    repo: str,
    pairs: tuple[Path, ...],
    workers: int = 1,
) -> tuple[list[str], dict[str, str], list[Path]]:
    """Every channel -- LemonCrow included -- runs through the same provider
    harness over the shipped stdio surface. LemonCrow channel variants are env
    toggles the server honours (the provider forwards its environment)."""
    import os
    import sys

    golds = _make_golds(pairs)
    env = dict(os.environ)
    env["FITNESS_PAIRS"] = ",".join(str(g) for g in golds)
    env["EVAL_PAIRS"] = str(golds[0])

    provider = "lemoncrow" if channel in _LEMONCROW_CHANNELS else channel
    env["EVAL_CHANNEL_LABEL"] = channel
    if channel == "zoekt":
        # Pure Zoekt trigram/regex index: disable both lexical FTS5 and semantic.
        env["LEMONCROW_EXPLORE_LEXICAL"] = "0"
        env["LEMONCROW_EXPLORE_SEMANTIC"] = "0"
    elif channel == "semantic":
        # Pure semantic (embedding) search: disable both lexical FTS5 and Zoekt.
        env["LEMONCROW_EXPLORE_LEXICAL"] = "0"
        env["LEMONCROW_ZOEKT_MODE"] = "off"
    elif channel == "lexical":
        env["LEMONCROW_ZOEKT_MODE"] = "off"
        env["LEMONCROW_EXPLORE_SEMANTIC"] = "0"
        # Unset regardless of the caller's shell env: if LEMONCROW_CODE_EMBEDDER is
        # exported for a multi-channel run (e.g. `--channel lexical --channel
        # lexical+zoekt+semantic` in one invocation), leaving it set here makes
        # self._semantic_ranker.available true, which triggers a GPU-heavy
        # embedding backfill in the background autosync worker WHILE this
        # channel's timed queries are running against the same DB file --
        # measured contention that inflated large-repo p100 latency ~50x in a
        # real run (linux: 11.7s observed vs 0.2s in an isolated, uncontended
        # direct engine call for the same query).
        env.pop("LEMONCROW_CODE_EMBEDDER", None)
    elif channel == "lexical+zoekt":
        env["LEMONCROW_EXPLORE_SEMANTIC"] = "0"
        env.pop("LEMONCROW_CODE_EMBEDDER", None)
    elif channel == "lexical+zoekt+semantic":
        # Use the embedder pinned by LEMONCROW_CODE_EMBEDDER in the caller's
        # env, or fall back to the configured best (BGE-Code-v1 by default --
        # see BENCHMARKS.md's embedder sweep).
        env.setdefault("LEMONCROW_CODE_EMBEDDER", os.environ.get("LEMONCROW_CODE_EMBEDDER", "bge"))
        # Production's _SEMANTIC_SYMBOL_DEADLINE_S default (2s) exists to bound
        # interactive latency -- it is NOT what we want measuring MRR: a query
        # that times out here silently degrades to lexical-only, so the CSV
        # would report a lower MRR that's an artifact of the benchmark's own
        # cold-cache timing, not a real quality difference between channels.
        # Give it a generous ceiling instead of removing it outright, so a
        # genuinely stuck load still can't hang a benchmark run forever.
        env.setdefault("LEMONCROW_SEMANTIC_SYMBOL_DEADLINE_S", "60")

    cmd: list[str] = [
        sys.executable,
        "benchmarks/codebench/eval_external_provider_mrr.py",
        "--provider",
        provider,
    ]
    if full:
        cmd.append("--full")
    elif sample:
        cmd += ["--sample", str(sample)]
    if repo:
        cmd += ["--repo", repo]
    if workers > 1:
        cmd += ["--workers", str(workers)]

    return cmd, env, golds


def _render_comparison(channel_results: dict[str, dict[str, Any]], csv_path: Path | None = None) -> None:
    """Print side-by-side MRR + hit@1 + n + p100 table with bottom summary, and optionally write CSV."""
    import sys

    channels = list(channel_results.keys())
    # Collect every gold_kind present across any channel result.
    _all_gks = ["definition", "regex", "content", "semantic", "swebench", "sessions"]
    gold_kinds = [gk for gk in _all_gks if any(gk in r.get("golds", {}) for r in channel_results.values())]
    if not gold_kinds:
        gold_kinds = ["definition"]

    all_repos: set[str] = set()
    for r in channel_results.values():
        for gk in gold_kinds:
            all_repos.update(r.get("golds", {}).get(gk, {}).get("by_repo", {}).keys())
    repos = sorted(all_repos)

    display_gold = {
        "definition": "definition",
        "regex": "regex",
        "content": "content",
        "semantic": "semantic",
        "swebench": "swebench",
        "sessions": "sessions",
    }

    # ── ANSI helpers (colour only when stdout is a terminal) ────────────
    _is_tty = sys.stdout.isatty()
    _B = "\033[1m" if _is_tty else ""
    _D = "\033[2m" if _is_tty else ""
    _G = "\033[92m" if _is_tty else ""
    _R = "\033[0m" if _is_tty else ""

    def _best(vals: list[float | None]) -> set[int]:
        """Return indices of the maximum value (non-None only)."""
        ok = [(i, v) for i, v in enumerate(vals) if v is not None]
        if not ok:
            return set()
        best_val = max(v for _, v in ok)
        return {i for i, v in ok if abs(v - best_val) < 1e-9}

    def _weighted_overall(ch: str) -> tuple[float | None, int | None, float | None]:
        r = channel_results[ch]
        sup = r.get("supported_overall")
        if isinstance(sup, dict) and sup.get("n"):
            lat = r.get("latency_ms", {})
            return float(sup.get("mrr") or 0.0), int(sup["n"]), (lat.get("max") if isinstance(lat, dict) else None)
        total_n = 0
        weighted = 0.0
        for gk in gold_kinds:
            gd = r.get("golds", {}).get(gk, {})
            n = gd.get("n") or 0
            mrr = gd.get("mrr")
            if n and mrr is not None:
                total_n += int(n)
                weighted += float(mrr) * int(n)
        lat = r.get("latency_ms", {})
        p100 = lat.get("max") if isinstance(lat, dict) else None
        return (weighted / total_n if total_n else None), (total_n or None), p100

    def _cell(ch: str, gk: str, repo: str) -> dict[str, Any]:
        r = channel_results[ch]
        gd = r.get("golds", {}).get(gk, {})
        if repo == "OVERALL":
            return {
                "mrr": gd.get("mrr"),
                "hit1": gd.get("hit1"),
                "n": gd.get("n"),
                "p100": (r.get("latency_ms", {}).get("max") if isinstance(r.get("latency_ms"), dict) else None),
            }
        byr = gd.get("by_repo", {}).get(repo) or {}
        lat = byr.get("latency_ms") or {}
        return {
            "mrr": byr.get("mrr"),
            "hit1": byr.get("hit1"),
            "n": byr.get("n"),
            "p100": lat.get("max") if isinstance(lat, dict) else None,
        }

    # ── Layout ──────────────────────────────────────────────────────────
    # Each channel column shows: MRR hit1 n p100
    #   MRR: 6  "0.742"
    #   hit1: 6  "0.612"
    #   n:    5  "  500"
    #   p100: 6  "215ms"
    #   spaces between: 3
    #   Total ≈ 26
    LABEL_W = max(26, max((len(r.split("__")[-1] if "__" in r else r) + 10) for r in [*repos, "OVERALL"]))
    CELL_W = 28
    SEP = "  "

    # Get overall MRR for summary (used early, displayed at bottom)
    overalls = [_weighted_overall(ch) for ch in channels]
    best_overall_idx = _best([m for m, _, _ in overalls])

    # ── Print table ─────────────────────────────────────────────────────
    print()
    print(f"{_B}Retrieval Evaluation{_R}  ({', '.join(gold_kinds)} golds  |  {len(repos)} repos)")
    print()

    # Header row 1 — channel names
    h1 = f"{'':<{LABEL_W}}"
    for i, ch in enumerate(channels):
        label = ch
        if i in best_overall_idx:
            label = f"{_G}● {ch}{_R}"
        h1 += f"{SEP}{label:^{CELL_W}}"
    print(h1)

    # Header row 2 — metric labels
    h2 = f"{'':<{LABEL_W}}"
    for _ in channels:
        h2 += f"{SEP}{'MRR':>6} {'hit@1':>6} {'  n':>5} {'p100':>6}"
    print(_D + h2 + _R)

    # Separator
    sep_w = LABEL_W + (CELL_W + len(SEP)) * len(channels)
    print(_D + "─" * sep_w + _R)

    # ── OVERALL row ──
    row = f"{_B}{'OVERALL':<{LABEL_W}}{_R}"
    for i, _ch in enumerate(channels):
        m, n_q, p = overalls[i]
        is_best = i in best_overall_idx
        clr = _G if is_best else ""
        rst = _R if is_best else ""
        mrr_s = f"{clr}{_B}{m:.3f}{_R}" if m is not None else "  ---"
        n_s = f"{_D}{n_q or '?'}{_R}" if n_q else ""
        p_s = f"{_D}{p:3.0f}ms{_R}" if p is not None else ""
        row += f"{SEP}{mrr_s:>6} {n_s:>5} {p_s:>7}"
    print(row)

    # ── Per-repo / per-gold rows ──
    for repo in repos:
        short = repo.split("__")[-1] if "__" in repo else repo
        # Collect row metrics for this repo across all channels and gold kinds
        rows: list[tuple[str, list[dict[str, Any]]]] = []
        for gk in gold_kinds:
            cells = [_cell(ch, gk, repo) for ch in channels]
            rows.append((display_gold.get(gk, gk), cells))

        # Print repo section label
        print(f" {_D}{short}{_R}")

        for gk_label, cells in rows:
            # Determine best MRR across channels for this cell
            mrr_vals = [c.get("mrr") for c in cells]
            best_mrr = _best(mrr_vals)
            line = f"  {_D}{gk_label:<8}{_R}"
            for i, c in enumerate(cells):
                m = c.get("mrr")
                i_best = i in best_mrr
                clr = _G if i_best else ""
                rst = _R if i_best else ""
                mrr_s = f"{clr}{m:.3f}{rst}" if m is not None else f"{_D}---{_R}"
                h1_s = f"{_D}{c.get('hit1', 0):.3f}{_R}" if c.get("hit1") is not None else f"{_D}---{_R}"
                n_s = f"{_D}{int(c['n'])}{_R}" if c.get("n") else ""
                p_s = f"{_D}{c['p100']:3.0f}ms{_R}" if c.get("p100") is not None else ""
                line += f"{SEP}{mrr_s:>6} {h1_s:>6} {n_s:>5} {p_s:>7}"
            print(line)

    # ── Bottom separator ──
    print(_D + "─" * sep_w + _R)

    # ── MRR Summary ──
    print()
    print(f"{_B}MRR Summary{_R}")
    for i, ch in enumerate(channels):
        m, n_q, p = overalls[i]
        is_best = i in best_overall_idx
        clr = _G if is_best else ""
        rst = _R if is_best else ""
        marker = f"  {_G}← best{_R}" if is_best else ""
        mrr_s = f"{clr}{_B}{m:.4f}{rst}" if m is not None else "---"

        # Also get the per-channel hit1 and p95 for the summary
        r = channel_results[ch]
        lat = r.get("latency_ms", {}) if isinstance(r.get("latency_ms"), dict) else {}
        p95 = lat.get("p95")
        p95_s = f"{p95:.0f}ms" if p95 is not None else "---"
        overall_hit1 = r.get("supported_overall", {}).get("hit1")
        hit1_s = f"{overall_hit1:.4f}" if overall_hit1 else "---"

        p100_s = f"{int(p)}ms" if p is not None else "---"
        print(
            f"  {ch:<{LABEL_W - 4}}{clr}MRR {mrr_s}{rst}  "
            f"hit@1 {hit1_s}  "
            f"n {n_q or '?'}  "
            f"p95 {p95_s}  "
            f"p100 {p100_s}"
            f"{marker}"
        )
    print()

    # ── CSV (unchanged) ──
    if csv_path is None:
        return

    import csv as _csv

    def _get_full(channel: str, gold_kind: str, repo: str) -> tuple[Any, Any, Any, Any, Any, Any, Any]:
        r = channel_results[channel]
        gdata = r.get("golds", {}).get(gold_kind, {})
        if repo == "OVERALL":
            mrr = gdata.get("mrr")
            hit1 = gdata.get("hit1")
            hit2 = gdata.get("hit2")
            hit3 = gdata.get("hit3")
            n = gdata.get("n")
            lat = r.get("latency_ms", {})
        else:
            byr = gdata.get("by_repo", {}).get(repo) or {}
            mrr, hit1, hit2, hit3, n = byr.get("mrr"), byr.get("hit1"), byr.get("hit2"), byr.get("hit3"), byr.get("n")
            lat = byr.get("latency_ms") or {}
        p95 = lat.get("p95") if isinstance(lat, dict) else None
        p100 = lat.get("max") if isinstance(lat, dict) else None
        return mrr, hit1, hit2, hit3, n, p95, p100

    fieldnames = ["repo", "gold_kind"]
    for ch in channels:
        fieldnames += [f"{ch}_MRR", f"{ch}_hit1", f"{ch}_hit2", f"{ch}_hit3", f"{ch}_n", f"{ch}_p95ms", f"{ch}_p100ms"]

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="") as fh:
        writer = _csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for repo in ["OVERALL", *repos]:
            short = repo.split("__")[-1] if "__" in repo else repo
            for gk in gold_kinds:
                row_d: dict[str, object] = {"repo": short, "gold_kind": gk}
                for ch in channels:
                    mrr, hit1, hit2, hit3, n, p95, p100 = _get_full(ch, gk, repo)
                    row_d[f"{ch}_MRR"] = f"{mrr:.4f}" if mrr is not None else ""
                    row_d[f"{ch}_hit1"] = f"{hit1:.4f}" if hit1 is not None else ""
                    row_d[f"{ch}_hit2"] = f"{hit2:.4f}" if hit2 is not None else ""
                    row_d[f"{ch}_hit3"] = f"{hit3:.4f}" if hit3 is not None else ""
                    row_d[f"{ch}_n"] = n if n is not None else ""
                    row_d[f"{ch}_p95ms"] = f"{int(p95)}" if p95 is not None else ""
                    row_d[f"{ch}_p100ms"] = f"{int(p100)}" if p100 is not None else ""
                writer.writerow(row_d)
    click.echo(f"[eval] CSV written -> {csv_path}", err=True)


@eval_.command("retrieval")
@click.option(
    "--channel",
    "channels",
    multiple=True,
    type=click.Choice([_ALL_CHANNEL, *_RETRIEVAL_CHANNELS]),
    default=("lexical",),
    show_default=True,
    help="Channel(s) to benchmark. Repeatable for side-by-side comparison: "
    "--channel lexical --channel lexical+zoekt. "
    "Use 'all' to run every channel. "
    "LemonCrow (env-toggled variants of the shipped MCP surface): zoekt, "
    "semantic, lexical, lexical+zoekt, lexical+zoekt+semantic. "
    "External: cg, ctags, ast-grep, serena, code-index-mcp, jcodemunch, rg, cmm, fff.",
)
@click.option("--full", is_flag=True, default=False, help="Run all available query pairs (no cap).")
@click.option("--sample", type=int, default=0, help="Total queries to sample across repos (0 = default 500).")
@click.option("--repo", default="", metavar="PREFIX", help="Substring filter on repo prefix.")
@click.option(
    "--pairs",
    type=click.Path(path_type=Path),
    multiple=True,
    default=(),
    help="Explicit (query, gold-file) pairs JSON. Repeat for multiple gold sets. Default scores all built-in golds.",
)
@click.option(
    "--csv",
    "csv_path",
    type=click.Path(path_type=Path),
    default=None,
    metavar="FILE",
    help="Write comparison results to a CSV file (wide: one row per repoxgold, one column-group per channel).",
)
@click.option(
    "--serial/--parallel",
    "serial",
    default=True,
    help="--parallel runs channels concurrently (faster but shared CPU skews latency). Default: serial.",
)
@click.option(
    "--resume",
    is_flag=True,
    default=False,
    help="Reuse per-channel results already cached beside --csv (in <csv-stem>_channels/): "
    "skip channels that finished, run only the missing ones, then re-render the CSV. "
    "Lets a long multi-channel/all-gold sweep continue after an interruption without redoing finished channels.",
)
@click.option(
    "--workers",
    type=int,
    default=int(os.environ.get("EVAL_WORKERS", "1")),
    metavar="N",
    help="Parallel repo workers within each channel (1 = sequential). Each worker "
    "spawns its own provider subprocess so repos with independent per-repo "
    "start/stop (lemon, rg, etc.) benefit. Default: $EVAL_WORKERS or 1.",
)
@click.option(
    "--json/--no-json",
    "json_output",
    default=False,
    help="Output only JSON at the end, suppressing logs and the table view.",
)
def eval_retrieval(
    channels: tuple[str, ...],
    full: bool,
    sample: int,
    repo: str,
    pairs: tuple[Path, ...],
    csv_path: Path | None,
    serial: bool,
    resume: bool,
    workers: int = 1,
    json_output: bool = False,
) -> None:
    """Retrieval MRR + latency over definition + content golds.

    Scores BOTH golds (definition = which file defines the symbol, content =
    which files contain the pattern) in one run.

    Pass --channel multiple times for a side-by-side comparison table::

        lemon eval retrieval --channel lexical --channel lexical+zoekt --full
    """
    import json
    import subprocess
    import sys
    from concurrent.futures import ThreadPoolExecutor, as_completed

    repo_root = Path.cwd().resolve()

    # Expand 'all' to every real channel, preserving any explicit order and deduplicating.
    if _ALL_CHANNEL in channels:
        seen_ch: set[str] = set()
        expanded: list[str] = []
        for ch in channels:
            targets = _RETRIEVAL_CHANNELS if ch == _ALL_CHANNEL else [ch]
            for t in targets:
                if t not in seen_ch:
                    seen_ch.add(t)
                    expanded.append(t)
        channels = tuple(expanded)

    # Per-channel result cache: each channel's JSON is persisted the moment it
    # finishes, so a --resume run can skip completed channels and re-run only the
    # missing ones — essential for long all-gold sweeps across many channels that
    # may be interrupted. The cache is *always* written so a future --resume can
    # pick up where the last run left off; it is *read* only when --resume is
    # passed (omit --resume to start fresh).
    results_dir: Path | None = None
    if csv_path is not None:
        results_dir = csv_path.parent / f"{csv_path.stem}_channels"
    else:
        results_dir = repo_root / ".eval_retrieval_channels"
    results_dir.mkdir(parents=True, exist_ok=True)

    # ── Auto-provision: ensure workspaces + indexes exist ───────────────
    golds_for_provision = _make_golds(pairs)
    _repo_root_str = str(repo_root)
    if _repo_root_str not in sys.path:
        sys.path.insert(0, _repo_root_str)
    from benchmarks.codebench.provision_repos import ensure_eval_workspaces as _ensure_ws

    _ensure_ws(golds_for_provision)

    channel_results: dict[str, dict[str, Any]] = {}
    any_failed = False

    def _cache_path(ch: str) -> Path | None:
        if results_dir is None:
            return None
        safe = ch.replace("+", "_").replace("/", "_")
        return results_dir / f"{safe}.json"

    def _run_channel(ch: str) -> tuple[str, dict[str, Any] | None]:
        cache = _cache_path(ch)
        if resume and cache is not None and cache.exists():
            try:
                cached = json.loads(cache.read_text())
                if not json_output:
                    click.echo(f"[eval] resume channel={ch} (cached {cache})", err=True)
                return ch, cached
            except Exception:  # noqa: BLE001 — corrupt cache: fall through and re-run
                if not json_output:
                    click.echo(f"[eval] cache for channel={ch} unreadable — re-running", err=True)
        cmd, env, _ = _channel_cmd_env(
            ch,
            full=full,
            sample=sample,
            repo=repo,
            pairs=pairs,
            workers=workers,
        )
        if not json_output:
            click.echo(f"[eval] start channel={ch} :: {' '.join(cmd)}", err=True)
        proc = subprocess.run(
            cmd,
            cwd=repo_root,
            env=env,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL if json_output else None,
        )
        if proc.returncode != 0:
            if not json_output:
                click.echo(f"[eval] channel={ch} exited {proc.returncode}", err=True)
            return ch, None
        stdout = (proc.stdout or b"").decode(errors="replace")
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    result = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cache is not None:
                    try:
                        cache.write_text(json.dumps(result))
                    except Exception:  # noqa: BLE001 — caching is best-effort
                        pass
                if not json_output:
                    click.echo(f"[eval] done  channel={ch}", err=True)
                return ch, result
        if not json_output:
            click.echo(f"[eval] channel={ch}: no JSON in stdout — check stderr above", err=True)
        return ch, None

    if len(channels) == 1 and csv_path is None and not resume:
        # Fast path: single channel with no CSV — parse JSON and fall through
        # to unified rendering so the user gets the comparison table (default)
        # or pure JSON (--json) instead of raw subprocess output.
        cmd, env, golds = _channel_cmd_env(
            channels[0],
            full=full,
            sample=sample,
            repo=repo,
            pairs=pairs,
            workers=workers,
        )
        if not json_output:
            click.echo(f"[eval] channel={channels[0]} golds={len(golds)} :: {' '.join(cmd)}", err=True)
        proc = subprocess.run(
            cmd,
            cwd=repo_root,
            env=env,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL if json_output else None,
        )
        proc_stdout = (proc.stdout or b"").decode(errors="replace")
        parsed: dict[str, Any] | None = None
        for line in reversed(proc_stdout.splitlines()):
            ls = line.strip()
            if ls.startswith("{"):
                try:
                    parsed = json.loads(ls)
                except json.JSONDecodeError:
                    continue
                break
        if parsed is not None:
            channel_results[channels[0]] = parsed
        else:
            any_failed = True
    else:
        if not serial and len(channels) > 1:
            if not json_output:
                click.echo(f"[eval] running {len(channels)} channels in parallel", err=True)
            with ThreadPoolExecutor(max_workers=len(channels)) as pool:
                futures = {pool.submit(_run_channel, ch): ch for ch in channels}
                for fut in as_completed(futures):
                    ch, result = fut.result()
                    if result is None:
                        any_failed = True
                    else:
                        channel_results[ch] = result
            # Restore original channel order.
            channel_results = {ch: channel_results[ch] for ch in channels if ch in channel_results}
        else:
            for ch in channels:
                ch, result = _run_channel(ch)
                if result is None:
                    any_failed = True
                else:
                    channel_results[ch] = result

    if channel_results:
        if json_output:
            click.echo(json.dumps(channel_results, indent=2, default=str))
        else:
            _render_comparison(channel_results, csv_path=csv_path)

    raise SystemExit(1 if any_failed else 0)


@eval_.command("fitness")
@click.option(
    "--session-dir",
    "-d",
    default=os.environ.get("SESSION_ROOT", os.path.expanduser("~/.claude/projects/")),
    show_default=True,
    help="Directory to scan for Claude Code session files.",
)
@click.option(
    "--repo-filter",
    "-f",
    default=os.environ.get("SESSION_REPO_FILTER", ""),
    help="Substring filter on project directory name (e.g. 'lemoncrow').",
)
@click.option(
    "--out",
    "-o",
    type=click.Path(path_type=Path),
    default=os.environ.get("SESSION_PAIRS_OUT", "/tmp/session_pairs.json"),
    show_default=True,
    help="Output path for mined pairs JSON.",
)
@click.option("--no-eval", is_flag=True, default=False, help="Skip the retrieval benchmark; only mine and save pairs.")
@click.option(
    "--channel",
    "channels",
    multiple=True,
    type=click.Choice([_ALL_CHANNEL, *_RETRIEVAL_CHANNELS]),
    default=("lexical", "lexical+zoekt", "lexical+zoekt+semantic", "rg"),
    show_default=True,
    help="Retrieval channel(s) to benchmark. Pass multiple times for side-by-side.",
)
@click.option("--full", is_flag=True, default=False, help="Run eval on all mined pairs (no cap).")
def eval_fitness(
    session_dir: str,
    repo_filter: str,
    out: Path,
    no_eval: bool,
    channels: tuple[str, ...],
    full: bool,
) -> None:
    """Mine search patterns from your real Claude Code & Codex sessions and
    benchmark LemonCrow's retrieval quality against them.

    Scans ``~/.claude/projects/`` and ``~/.codex/sessions/`` for real explore
    queries you made during past coding sessions, writes them to a pairs file,
    then immediately runs ``eval retrieval`` on those pairs so you can see how
    well LemonCrow finds files for queries like the ones you actually ask.

    Use ``--no-eval`` to only mine and save the pairs without running the benchmark.
    """
    import subprocess

    from lemoncrow.gateway.cli.commands import benchmark as _bm

    bench_root = _bm._bench_source_root()
    env = dict(os.environ)
    env["FITNESS_PAIRS"] = str(out)
    env["SESSION_ROOT"] = session_dir
    env["SESSION_REPO_FILTER"] = repo_filter

    # Step 1: mine queries from sessions.
    mine_cmd = [
        *_bm._python_cmd(bench_root),
        "benchmarks/codebench/offline_session_analyzer.py",
        "--session-dir",
        session_dir,
        "--out",
        str(out),
    ]
    if repo_filter:
        mine_cmd += ["--repo-filter", repo_filter]

    click.echo("[eval fitness] mining queries ...", err=True)
    r = subprocess.run(mine_cmd, cwd=bench_root, env=env, check=False)
    if r.returncode != 0:
        raise SystemExit(r.returncode)

    # Step 2: augment with synthetic queries from the current project.
    # Cap synthetic pairs at the session pair count so neither source dominates
    # (50/50 split). When there are no session pairs, the cap is lifted so
    # synthetic pairs fill the whole benchmark.
    click.echo("[eval fitness] generating synthetic pairs ...", err=True)
    import json as _json

    _session_count = 0
    _session_prefix = ""
    try:
        _pd = _json.loads(out.read_text())
        _session_count = len(_pd.get("pairs", []))
        _session_prefix = next(iter(_pd.get("repos", {})), "")
    except (_json.JSONDecodeError, KeyError, TypeError):
        pass
    syn_cmd = [
        *_bm._python_cmd(bench_root),
        "benchmarks/codebench/synthetic_pair_miner.py",
        "--repo-dir",
        str(Path(".").resolve()),
        "--merge",
        str(out),  # merge into the session pairs file in-place
        "--out",
        str(out),
    ]
    if _session_prefix:
        syn_cmd += ["--repo-prefix", _session_prefix]
    # 50/50 cap with gap-fill:
    # - No sessions       → unlimited synthetic (pure synthetic benchmark)
    # - Sessions sparse   → synthetic fills up to MIN_TOTAL so the benchmark
    #                       stays meaningful even when real sessions are few
    # - Sessions plentiful → synthetic capped at session_count (≤50% of total)
    _MIN_TOTAL = 500
    if _session_count == 0:
        pass  # no cap — synthetic fills everything
    elif _session_count < _MIN_TOTAL // 2:
        syn_cmd += ["--max-pairs", str(_MIN_TOTAL - _session_count)]
    else:
        syn_cmd += ["--max-pairs", str(_session_count)]
    r2 = subprocess.run(syn_cmd, cwd=bench_root, env=env, check=False)
    if r2.returncode != 0:
        click.echo("[eval fitness] synthetic mining failed (continuing without it)", err=True)

    # Step 2.5: augment with semantic queries (docstring + intent-based) from the project.
    click.echo("[eval fitness] generating semantic pairs ...", err=True)
    # Re-read session count (may have changed after synthetic merge)
    try:
        _pd = _json.loads(out.read_text())
        _session_count = len(_pd.get("pairs", []))
        _session_prefix = next(iter(_pd.get("repos", {})), "")
    except (_json.JSONDecodeError, KeyError, TypeError):
        pass
    sem_cmd = [
        *_bm._python_cmd(bench_root),
        "benchmarks/codebench/semantic_pair_miner.py",
        "--repo-dir",
        str(Path(".").resolve()),
        "--merge",
        str(out),
        "--out",
        str(out),
    ]
    if _session_prefix:
        sem_cmd += ["--repo-prefix", _session_prefix]
    # Similar 50/50 cap: semantic fills up to session_count (≤50% of total)
    if _session_count == 0:
        sem_cmd += ["--max-pairs", "200"]  # pure semantic fallback
    elif _session_count < _MIN_TOTAL // 2:
        sem_cmd += ["--max-pairs", str(_MIN_TOTAL - _session_count)]
    else:
        sem_cmd += ["--max-pairs", str(_session_count)]
    r3 = subprocess.run(sem_cmd, cwd=bench_root, env=env, check=False, capture_output=True, text=True)
    if r3.returncode != 0:
        click.echo("[eval fitness] building code index for semantic pairs ...", err=True)
        index_cmd = [
            "lemon",
            "code",
            "index",
            "--repo-root",
            str(Path(".").resolve()),
            "--reindex",
            "--no-stats",
        ]
        r_index = subprocess.run(
            index_cmd,
            cwd=str(Path(".").resolve()),
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        if r_index.returncode == 0:
            click.echo("[eval fitness] retrying semantic pairs ...", err=True)
            r3 = subprocess.run(sem_cmd, cwd=bench_root, env=env, check=False, capture_output=True, text=True)
        else:
            detail = (r_index.stderr or r_index.stdout or "").strip().splitlines()
            if detail:
                click.echo(f"[eval fitness] code index failed: {detail[-1]}", err=True)
        if r3.returncode != 0:
            click.echo("[eval fitness] semantic mining failed (continuing without it)", err=True)

    split_pairs = _split_fitness_pairs(out)

    if no_eval:
        click.echo(f"[eval fitness] pairs written -> {out}", err=True)
        return

    # Step 3: run retrieval benchmark on the split pairs.
    # Drop semantic channel if the GPU can't support the embedding model.
    active_channels = list(channels)
    if "lexical+zoekt+semantic" in active_channels:
        gpu_ok, gpu_reason = _gpu_supports_embedder()
        if not gpu_ok:
            click.echo(
                f"[eval fitness] skipping lexical+zoekt+semantic ({gpu_reason})",
                err=True,
            )
            active_channels.remove("lexical+zoekt+semantic")

    eval_cmd = ["lemon", "eval", "retrieval"]
    for pair_path in split_pairs:
        eval_cmd += ["--pairs", str(pair_path)]
    for ch in active_channels:
        eval_cmd += ["--channel", ch]
    if full:
        eval_cmd.append("--full")

    click.echo(f"[eval fitness] {' '.join(eval_cmd)}", err=True)
    raise SystemExit(subprocess.run(eval_cmd, cwd=str(Path(".").resolve()), env=env, check=False).returncode)


__all__ = [
    "eval_",
]
