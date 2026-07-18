#!/usr/bin/env python3
"""Scrape per-turn/tool-call data for the baseline's 445 Harbor trials.

Harbor Hub's trial-detail page is client-rendered, but the page it serves
embeds a Next.js RSC payload (inside <script> tags a naive HTML-to-markdown
fetch strips out) that carries each trial's real UUID and job_id. Given
those, the site's own API -- discovered by reading its compiled JS bundle,
not documented anywhere -- serves the full ATIF-format trajectory with no
auth required:

    GET https://hub.harborframework.com/api/trials/{trial_id}/trajectory
        ?jobId={job_id}&trajectory_path=trials/{trial_id}/trajectory.json

ATIF steps alternate source="user"/"agent"; each agent step is one model
turn and carries its own tool_calls list and per-step token metrics
(prompt/completion/cached, plus cache_creation/cache_read split and the
ephemeral cache TTL bucket used). final_metrics on the trajectory gives an
exact per-trial total that we cross-check against the known real cost sum
($286.92-286.94 across sources) before trusting the scrape.

Raw trajectories are large (445 files, ~290MB total, one alone 178MB) and
are NOT checked in -- this script re-derives the small per-trial rollup CSV
from them and discards the raw JSON. Re-run this script to refresh/rescrape;
it's not fast (445 sequential-ish HTTP round trips) but is fully automated.
"""

from __future__ import annotations

import concurrent.futures
import csv
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
OUT_CSV = HERE / "results" / "baseline" / "tbench_opus48_claudecode_2.1.205_turns.csv"

ROW_URL = (
    "https://hub.harborframework.com/datasets/terminal-bench/terminal-bench-2-1/6"
    "/leaderboards/main/rows/dcd48d03-9df9-46ab-bc4c-ade6dc35b8da"
)
USER_AGENT = "Mozilla/5.0"
KNOWN_REAL_COST_TOTAL = 286.92  # from tbench_opus48_claudecode_2.1.205_aggregate.csv
COST_TOLERANCE_PCT = 0.5


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body: bytes = resp.read()
    return body.decode("utf-8", "replace")


def fetch_trial_index() -> dict[str, dict[str, Any]]:
    """Paginate the results table (?page=1..5, 100 rows/page) and pull every
    trial's {id, job_id, name, task_name} out of the embedded RSC JSON."""
    trials: dict[str, dict[str, Any]] = {}
    decoder = json.JSONDecoder()
    for page in range(1, 6):
        html = _fetch(f"{ROW_URL}?tab=results&page={page}")
        starts = [m.start() for m in re.finditer(r'\{\\"id\\":\\"[0-9a-f-]{36}\\"', html)]
        for start in starts:
            snippet = html[start : start + 6000].replace('\\"', '"').replace("\\\\", "\\")
            try:
                obj, _ = decoder.raw_decode(snippet)
            except (json.JSONDecodeError, ValueError):
                continue
            if "task_name" in obj:
                trials[obj["id"]] = obj
    return trials


def fetch_trajectory(trial_id: str, job_id: str) -> dict[str, Any] | None:
    trajectory_path = f"trials/{trial_id}/trajectory.json"
    qs = urllib.parse.urlencode({"jobId": job_id, "trajectory_path": trajectory_path})
    url = f"https://hub.harborframework.com/api/trials/{trial_id}/trajectory?{qs}"
    try:
        result: dict[str, Any] = json.loads(_fetch(url))
        return result
    except Exception as exc:  # best-effort scrape: report and skip on any failure
        print(f"  ! {trial_id}: {exc}", file=sys.stderr)
        return None


def summarize(trial_id: str, trial_meta: dict[str, Any], trajectory: dict[str, Any]) -> dict[str, Any]:
    agent_steps = [s for s in trajectory["steps"] if s.get("source") == "agent"]
    final_metrics = trajectory.get("final_metrics") or {}
    extra = final_metrics.get("extra") or {}
    return {
        "trial_id": trial_id,
        "task": trial_meta["task_name"].removeprefix("terminal-bench/"),
        "trial_name": trial_meta["name"],
        "n_turns": len(agent_steps),
        "n_tool_calls": sum(len(s.get("tool_calls") or []) for s in agent_steps),
        "prompt_tokens": final_metrics.get("total_prompt_tokens", 0) or 0,
        "completion_tokens": final_metrics.get("total_completion_tokens", 0) or 0,
        "cache_read_tokens": extra.get("total_cache_read_input_tokens", 0) or 0,
        "cache_creation_tokens": extra.get("total_cache_creation_input_tokens", 0) or 0,
        "cost_usd": final_metrics.get("total_cost_usd"),
    }


def main() -> None:
    print("Fetching trial index (5 pages)...", file=sys.stderr)
    trials = fetch_trial_index()
    print(f"  {len(trials)} trials found", file=sys.stderr)

    print("Fetching trajectories...", file=sys.stderr)
    rows: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(fetch_trajectory, tid, meta["job_id"]): (tid, meta) for tid, meta in trials.items()}
        for i, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            tid, meta = futures[future]
            trajectory = future.result()
            if trajectory is not None:
                rows.append(summarize(tid, meta, trajectory))
            if i % 100 == 0:
                print(f"  {i}/{len(trials)}", file=sys.stderr)

    rows.sort(key=lambda r: (r["task"], r["trial_name"]))

    cost_sum = sum(r["cost_usd"] for r in rows if r["cost_usd"])
    err_pct = abs(cost_sum - KNOWN_REAL_COST_TOTAL) / KNOWN_REAL_COST_TOTAL * 100
    print(
        f"Cross-check: sum(cost_usd)=${cost_sum:.2f} vs known real total "
        f"${KNOWN_REAL_COST_TOTAL:.2f} ({err_pct:.2f}% off)",
        file=sys.stderr,
    )
    if err_pct > COST_TOLERANCE_PCT:
        raise SystemExit(
            f"Cost cross-check failed ({err_pct:.2f}% > {COST_TOLERANCE_PCT}% tolerance) "
            "-- scrape looks wrong, not writing output."
        )

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows -> {OUT_CSV}", file=sys.stderr)


if __name__ == "__main__":
    main()
