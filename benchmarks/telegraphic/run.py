#!/usr/bin/env python3
"""Baseline (vanilla Claude Code) vs atelier ultra reply-register.

Both arms run the identical Claude Code CLI in an isolated, plugin-free,
tool-free session (fresh tmp cwd, --strict-mcp-config, --tools "", no
CLAUDE.md) so the ONLY variable between arms is whether atelier's shipped
ultra register (integrations/agents/shared/reply-register.md) is appended to
the system prompt. Real per-call usage.output_tokens from Claude Code's
--output-format json result -- not a tokenizer approximation.

Prompts are the union of JuliusBrussee/caveman's benchmarks/prompts.json and
evals/prompts/en.txt (see prompts.json in this dir), so numbers are directly
comparable to caveman's own published table.

Run: uv run python benchmarks/telegraphic/run.py [--model sonnet] [--trials 1] [--limit N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent.parent
PROMPTS_PATH = SCRIPT_DIR / "prompts.json"
ULTRA_PATH = REPO_ROOT / "integrations" / "agents" / "shared" / "reply-register.md"
RESULTS_DIR = SCRIPT_DIR / "results"


def load_prompts() -> list[dict]:
    data = json.loads(PROMPTS_PATH.read_text(encoding="utf-8"))
    return data["prompts"]


def claude_version() -> str:
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except Exception:
        return "unknown"


def call_claude(prompt: str, model: str, system_prompt: str | None, timeout: int) -> dict:
    cmd = [
        "claude",
        "-p",
        "--strict-mcp-config",
        "--tools",
        "",
        "--output-format",
        "json",
        "--model",
        model,
    ]
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]
    cmd.append(prompt)
    with tempfile.TemporaryDirectory(prefix="atelier-telegraphic-bench-") as tmp:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=tmp, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr[:800]}")
    data = json.loads(proc.stdout)
    if data.get("is_error"):
        raise RuntimeError(f"claude reported error: {json.dumps(data)[:800]}")
    usage = data["usage"]
    return {
        "text": data.get("result", ""),
        "output_tokens": usage["output_tokens"],
        "input_tokens": usage["input_tokens"],
        "cost_usd": data.get("total_cost_usd"),
        "duration_ms": data.get("duration_ms"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="sonnet", help="claude CLI --model value (default: sonnet)")
    ap.add_argument("--trials", type=int, default=1, help="trials per prompt per arm (default: 1)")
    ap.add_argument("--limit", type=int, default=None, help="only run the first N prompts (smoke test)")
    ap.add_argument("--timeout", type=int, default=120, help="per-call subprocess timeout, seconds")
    args = ap.parse_args()

    prompts = load_prompts()
    if args.limit:
        prompts = prompts[: args.limit]
    ultra_system = ULTRA_PATH.read_text(encoding="utf-8").strip()
    ultra_hash = hashlib.sha256(ultra_system.encode("utf-8")).hexdigest()

    print(
        f"Running {len(prompts)} prompts x 2 arms x {args.trials} trial(s), model={args.model}",
        file=sys.stderr,
    )

    results = []
    for i, entry in enumerate(prompts, 1):
        pid, prompt_text = entry["id"], entry["prompt"]
        print(f"[{i}/{len(prompts)}] {pid}", file=sys.stderr)
        row = {
            "id": pid,
            "source": entry["source"],
            "category": entry.get("category"),
            "prompt": prompt_text,
            "baseline": [],
            "ultra": [],
        }
        for arm, sysprompt in (("baseline", None), ("ultra", ultra_system)):
            for t in range(args.trials):
                print(f"    {arm} trial {t + 1}/{args.trials}", file=sys.stderr)
                try:
                    row[arm].append(call_claude(prompt_text, args.model, sysprompt, args.timeout))
                except Exception as exc:  # record + keep going
                    print(f"    ERROR: {exc}", file=sys.stderr)
                    row[arm].append({"error": str(exc)})
                time.sleep(0.3)
        results.append(row)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out_path = RESULTS_DIR / f"telegraphic_{ts}.json"
    snapshot = {
        "metadata": {
            "generated_at": datetime.now(UTC).isoformat(),
            "model": args.model,
            "trials": args.trials,
            "n_prompts": len(prompts),
            "claude_cli_version": claude_version(),
            "ultra_register_sha256": ultra_hash,
            "harness": "strict-mcp-config, tools disabled, fresh tmp cwd per call, ultra = --append-system-prompt only",
        },
        "rows": results,
    }
    out_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
