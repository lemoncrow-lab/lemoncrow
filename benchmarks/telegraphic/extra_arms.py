"""Isolated system-prompt-only arms: atelier's register text alone, and
caveman's own skill, both with NO plugin/agent/MCP -- unlike the "atelier"
arm (full plugin+agent+MCP runtime, driven through benchmarks.codebench.run
and its ARM_SPECS), these two arms are just vanilla Claude Code plus ONE
appended system-prompt instruction, so the only variable vs "baseline" is
that one instruction. Reuses codebench's own baseline-config isolation
(``_make_baseline_config``: real auth, no plugins/hooks/MCP) rather than
reimplementing it.

``caveman_skill.md`` is JuliusBrussee/caveman's own skill file, copied
verbatim (MIT) from https://github.com/JuliusBrussee/caveman/blob/main/skills/caveman/SKILL.md
so this arm reproduces what a real caveman install actually sends as a
system prompt (its own benchmarks/run.py convention: raw skill text, no
extra "Answer concisely." prefix -- that prefix is specific to caveman's own
evals/ harness, not to a real caveman session).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

_HERE = Path(__file__).parent
_ULTRA_REGISTER = _HERE.parent.parent / "integrations" / "agents" / "shared" / "reply-register.md"
_CAVEMAN_SKILL = _HERE / "caveman_skill.md"

EXTRA_ARM_SYSTEM_PROMPT_PATHS: dict[str, Path] = {
    "atelier-telegraphic": _ULTRA_REGISTER,
    "caveman": _CAVEMAN_SKILL,
}
EXTRA_ARMS: tuple[str, ...] = tuple(EXTRA_ARM_SYSTEM_PROMPT_PATHS)


def run_extra_arm(
    *,
    arm: str,
    task_id: str,
    prompt: str,
    model: str,
    rep: int,
    make_baseline_config: Callable[..., Path],
    timeout: int = 120,
) -> dict[str, Any]:
    """One (task, arm, rep) call: vanilla Claude Code + one appended system prompt.

    ``make_baseline_config`` is ``benchmarks.codebench.run._make_baseline_config``,
    injected by the caller (already on ``sys.path`` there) rather than imported
    here, so this module has no import-time dependency on codebench's runner.
    Row shape mirrors codebench's ``ArmResult`` fields that ``report.py`` reads
    (``task``/``arm``/``rep``/``ok``/``is_error``/``cost_usd``/``output_tokens``),
    so these rows merge into the same combined ``results.jsonl``.
    """
    system_prompt = EXTRA_ARM_SYSTEM_PROMPT_PATHS[arm].read_text(encoding="utf-8").strip()
    config_dir = make_baseline_config(None, copy_creds=True)
    cmd = [
        "claude",
        "-p",
        "--mcp-config",
        json.dumps({"mcpServers": {}}),
        "--strict-mcp-config",
        "--append-system-prompt",
        system_prompt,
        "--output-format",
        "json",
        "--model",
        model,
        prompt,
    ]
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    row: dict[str, Any] = {
        "task": task_id,
        "arm": arm,
        "rep": rep,
        "ok": False,
        "is_error": True,
        "cost_usd": 0.0,
        "output_tokens": 0,
        "input_tokens": 0,
        "result_excerpt": "",
    }
    with tempfile.TemporaryDirectory(prefix=f"atelier-telegraphic-{arm}-") as ws:
        try:
            proc = subprocess.run(cmd, cwd=ws, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            row["result_excerpt"] = f"timeout after {timeout}s"
            return row
    if proc.returncode != 0:
        row["result_excerpt"] = proc.stderr[-2000:]
        return row
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        row["result_excerpt"] = proc.stdout[-2000:]
        return row
    usage = data.get("usage") or {}
    row.update(
        ok=not data.get("is_error", True),
        is_error=bool(data.get("is_error")),
        cost_usd=float(data.get("total_cost_usd") or 0.0),
        output_tokens=int(usage.get("output_tokens") or 0),
        input_tokens=int(usage.get("input_tokens") or 0),
        result_excerpt=str(data.get("result") or "")[:2000],
    )
    return row
