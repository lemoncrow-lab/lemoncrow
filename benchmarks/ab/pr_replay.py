"""PR-replay benchmarks — PR-01 through PR-06.

Fetches a GitHub PR, checks out the base commit in git worktrees per arm
(on/off), runs the agent with PR title+body as prompt, then scores diff quality
against the real merged diff.
"""

from __future__ import annotations

import difflib
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import click

# PR-04: pinned non-Claude judge (stated in every report)
JUDGE_MODEL = "gpt-4o-2024-11-20"
JUDGE_VERSION = "1"

_DIFF_JUDGE_PROMPT = """\
You are evaluating an AI assistant's code change quality.

The reference (merged) diff for PR "{pr_title}" is:
<reference_diff>
{reference_diff}
</reference_diff>

The assistant produced this diff:
<generated_diff>
{generated_diff}
</generated_diff>

Score the assistant's diff on a 0.0-1.0 scale for:
1. correctness: Does it solve the PR objective described by the title?
2. completeness: Does it cover all files/hunks in the reference?
3. style: Is it clean, idiomatic code?

Respond ONLY with valid JSON:
{{"correctness": <float>, "completeness": <float>, "style": <float>, "notes": "<brief>"}}
"""

# --------------------------------------------------------------------------- #
# PR metadata (PR-01)                                                          #
# --------------------------------------------------------------------------- #

_PR_URL_RE = re.compile(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def parse_pr_url(github_url: str) -> tuple[str, str, int]:
    """Return (owner, repo, pr_number) from a GitHub PR URL (PR-01)."""
    m = _PR_URL_RE.match(github_url)
    if not m:
        raise ValueError(f"Invalid GitHub PR URL: {github_url!r}. Expected https://github.com/owner/repo/pull/N")
    owner, repo, number = m.groups()
    return owner, repo, int(number)


def fetch_pr_metadata(github_url: str, *, _gh_run: Any = None) -> dict[str, Any]:
    """Fetch PR metadata via gh CLI (PR-01).

    Returns: title, body, base_sha, head_sha, merged_at, repo, diff.
    """
    owner, repo, pr_number = parse_pr_url(github_url)

    def gh(args: list[str]) -> str:
        if _gh_run is not None:
            return _gh_run(args)
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    data = json.loads(gh(["api", f"/repos/{owner}/{repo}/pulls/{pr_number}"]))
    diff_text = gh(
        [
            "api",
            f"/repos/{owner}/{repo}/pulls/{pr_number}",
            "--header",
            "Accept: application/vnd.github.v3.diff",
        ]
    )

    return {
        "pr_number": pr_number,
        "title": data["title"],
        "body": data.get("body") or "",
        "base_sha": data["base"]["sha"],
        "head_sha": data["head"]["sha"],
        "merged_at": data.get("merged_at"),
        "repo": f"{owner}/{repo}",
        "diff": diff_text,
    }


# --------------------------------------------------------------------------- #
# Diff scoring (PR-03)                                                         #
# --------------------------------------------------------------------------- #


def score_diff(generated: str, reference: str) -> dict[str, Any]:
    """Score generated diff against reference using SequenceMatcher + file overlap (PR-03)."""
    ratio = difflib.SequenceMatcher(None, generated, reference).ratio()

    gen_files = set(re.findall(r"^diff --git a/(\S+)", generated, re.MULTILINE))
    ref_files = set(re.findall(r"^diff --git a/(\S+)", reference, re.MULTILINE))

    file_overlap = len(gen_files & ref_files) / len(ref_files) if ref_files else 0.0

    return {
        "sequence_ratio": round(ratio, 4),
        "file_overlap": round(file_overlap, 4),
        "gen_files": sorted(gen_files),
        "ref_files": sorted(ref_files),
    }


# --------------------------------------------------------------------------- #
# LLM-as-judge diff quality (PR-04)                                           #
# --------------------------------------------------------------------------- #


def grade_diff_quality(
    pr_title: str,
    generated_diff: str,
    reference_diff: str,
    judge_model: str = JUDGE_MODEL,
    *,
    _llm_call: Any = None,
) -> dict[str, Any]:
    """Grade diff quality using LLM-as-judge (PR-04).

    Judge model is pinned and stated in output (non-Claude to avoid bias).
    """
    prompt = _DIFF_JUDGE_PROMPT.format(
        pr_title=pr_title,
        reference_diff=reference_diff[:3000],
        generated_diff=generated_diff[:3000],
    )

    if _llm_call is not None:
        raw = _llm_call(prompt, judge_model)
    else:
        raw = _default_llm_call(prompt, judge_model)

    try:
        parsed = json.loads(raw)
        correctness = float(parsed.get("correctness", 0.0))
        completeness = float(parsed.get("completeness", 0.0))
        style = float(parsed.get("style", 0.0))
        notes = str(parsed.get("notes", ""))
    except (json.JSONDecodeError, ValueError):
        correctness = completeness = style = 0.0
        notes = f"parse error: {raw[:200]}"

    # Weighted score: correctness 50%, completeness 35%, style 15%
    weighted = round(0.50 * correctness + 0.35 * completeness + 0.15 * style, 4)

    return {
        "correctness": correctness,
        "completeness": completeness,
        "style": style,
        "weighted_score": weighted,
        "verdict": "pass" if weighted >= 0.5 else "fail",
        "judge_model": judge_model,
        "judge_version": JUDGE_VERSION,
        "notes": notes,
    }


def _default_llm_call(prompt: str, model: str) -> str:
    try:
        import openai  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ImportError("openai package required for diff quality grader. Install: pip install openai") from exc
    client = openai.OpenAI()
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=256,
    )
    return resp.choices[0].message.content or ""


# --------------------------------------------------------------------------- #
# Git worktree helpers (PR-02)                                                 #
# --------------------------------------------------------------------------- #


def create_worktree(repo_root: Path, commit_sha: str, worktree_dir: Path) -> None:
    """Create a git worktree at commit_sha (PR-02)."""
    subprocess.run(
        ["git", "worktree", "add", str(worktree_dir), commit_sha],
        check=True,
        cwd=repo_root,
        capture_output=True,
    )


def remove_worktree(repo_root: Path, worktree_dir: Path) -> None:
    """Remove a git worktree (PR-02)."""
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_dir)],
        check=True,
        cwd=repo_root,
        capture_output=True,
    )


# --------------------------------------------------------------------------- #
# Per-PR comparison table (PR-05)                                              #
# --------------------------------------------------------------------------- #


def print_pr_comparison_table(results: list[dict[str, Any]]) -> None:
    """Print per-PR comparison table (PR-05): cost, latency, diff score, judge score."""
    try:
        from rich.console import Console
        from rich.table import Table

        console = Console()
        table = Table(title="PR Replay: Atelier-on vs off", show_lines=True)
        table.add_column("Arm", style="cyan")
        table.add_column("Cost (USD)", justify="right")
        table.add_column("Latency (ms)", justify="right")
        table.add_column("Diff Sim", justify="right")
        table.add_column("Judge Score", justify="right")

        for r in results:
            table.add_row(
                r.get("mode", "?"),
                f"${r.get('cost_usd', 0):.4f}",
                f"{r.get('latency_ms', 0):.0f}",
                f"{r.get('diff_score', {}).get('sequence_ratio', 0):.2%}",
                f"{r.get('judge_score', {}).get('weighted_score', 0):.2f}",
            )
        console.print(table)
    except ImportError:
        click.echo("\n=== PR Replay Results ===")
        click.echo(f"{'Arm':<6} {'Cost':>10} {'Latency':>12} {'DiffSim':>10} {'JudgeScore':>12}")
        click.echo("-" * 55)
        for r in results:
            click.echo(
                f"{r.get('mode', '?'):<6}"
                f" ${r.get('cost_usd', 0):>9.4f}"
                f" {r.get('latency_ms', 0):>11.0f}"
                f" {r.get('diff_score', {}).get('sequence_ratio', 0):>9.2%}"
                f" {r.get('judge_score', {}).get('weighted_score', 0):>12.2f}"
            )


# --------------------------------------------------------------------------- #
# Run a single PR replay arm (PR-02, PR-06)                                   #
# --------------------------------------------------------------------------- #


def run_pr_arm(
    pr_metadata: dict[str, Any],
    mode: str,
    out_dir: Path,
    repo_root: Path,
    *,
    _agent_run: Any = None,
) -> dict[str, Any]:
    """Run one arm (on/off) of the PR replay and store transcript (PR-02, PR-06).

    Returns arm result dict with diff, cost, latency, and transcript_path.
    """
    import time

    arm_dir = out_dir / mode
    arm_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = arm_dir / "transcript.json"

    prompt = f"Implement: {pr_metadata['title']}\n\n{pr_metadata['body']}"

    start = time.monotonic()

    if _agent_run is not None:
        result = _agent_run(prompt=prompt, mode=mode, repo_root=repo_root)
    else:
        result = _real_agent_run(prompt=prompt, mode=mode, repo_root=repo_root)

    elapsed_ms = (time.monotonic() - start) * 1000

    # Store transcript (PR-06)
    transcript_path.write_text(json.dumps(result, indent=2, default=str))

    return {
        "mode": mode,
        "generated_diff": result.get("diff", ""),
        "cost_usd": result.get("cost_usd", 0.0),
        "latency_ms": elapsed_ms,
        "transcript_path": str(transcript_path),
    }


def _real_agent_run(prompt: str, mode: str, repo_root: Path) -> dict[str, Any]:
    """Placeholder for real agent execution — requires TerminalBench integration."""
    raise NotImplementedError(
        "Real agent execution requires TerminalBench. " "Use _agent_run= parameter to inject a mock in tests."
    )


# --------------------------------------------------------------------------- #
# Top-level PR replay orchestrator                                             #
# --------------------------------------------------------------------------- #


def run_pr_replay(
    github_url: str,
    out_dir: Path,
    modes: list[str] | None = None,
    judge_model: str = JUDGE_MODEL,
    repo_root: Path | None = None,
    *,
    _gh_run: Any = None,
    _agent_run: Any = None,
    _llm_call: Any = None,
) -> list[dict[str, Any]]:
    """Orchestrate a full PR A/B replay (PR-01 through PR-06).

    Returns list of arm results (one per mode).
    """
    if modes is None:
        modes = ["on", "off"]

    # PR-01: fetch PR metadata
    pr_metadata = fetch_pr_metadata(github_url, _gh_run=_gh_run)
    reference_diff = pr_metadata["diff"]

    (out_dir / "pr_metadata.json").write_text(json.dumps(pr_metadata, indent=2, default=str))

    resolved_root = repo_root or Path.cwd()
    arm_results: list[dict[str, Any]] = []

    for mode in modes:
        arm_out = out_dir / f"arm_{mode}"
        arm_out.mkdir(parents=True, exist_ok=True)

        # PR-02, PR-06: run arm and store transcript
        with tempfile.TemporaryDirectory() as tmp:
            worktree_dir = Path(tmp) / "worktree"
            try:
                create_worktree(resolved_root, pr_metadata["base_sha"], worktree_dir)
                arm_result = run_pr_arm(pr_metadata, mode, arm_out, worktree_dir, _agent_run=_agent_run)
            except Exception:
                remove_worktree(resolved_root, worktree_dir)
                raise
            remove_worktree(resolved_root, worktree_dir)

        # PR-03: diff similarity score
        arm_result["diff_score"] = score_diff(arm_result["generated_diff"], reference_diff)

        # PR-04: LLM judge score
        arm_result["judge_score"] = grade_diff_quality(
            pr_metadata["title"],
            arm_result["generated_diff"],
            reference_diff,
            judge_model=judge_model,
            _llm_call=_llm_call,
        )

        arm_result["pr_number"] = pr_metadata["pr_number"]
        arm_result["pr_title"] = pr_metadata["title"]

        arm_results.append(arm_result)

    # Write summary
    (out_dir / "pr_summary.json").write_text(json.dumps(arm_results, indent=2, default=str))

    # PR-05: print comparison table
    print_pr_comparison_table(arm_results)

    return arm_results
