"""Isolated system-prompt-only arm: caveman's own skill, with NO plugin/agent/MCP
-- unlike the "lemoncrow" arm (full plugin+agent+MCP runtime, driven through
benchmarks.codebench.run and its ARM_SPECS), this arm is just vanilla Claude
Code plus ONE appended system-prompt instruction, so the only variable vs
"baseline" is that one instruction. Reuses codebench's own baseline-config
isolation (``_make_baseline_config``: real auth, no plugins/hooks/MCP)
rather than reimplementing it.

``caveman_skill.md`` is JuliusBrussee/caveman's own skill file, copied
verbatim (MIT) from https://github.com/JuliusBrussee/caveman/blob/main/skills/caveman/SKILL.md
so this arm reproduces what a real caveman install actually sends as a
system prompt (its own benchmarks/run.py convention: raw skill text, no
extra "Answer concisely." prefix -- that prefix is specific to caveman's own
evals/ harness, not to a real caveman session).
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_HERE = Path(__file__).parent
_CAVEMAN_SKILL = _HERE / "caveman_skill.md"

EXTRA_ARM_SYSTEM_PROMPT_PATHS: dict[str, Path] = {
    "caveman": _CAVEMAN_SKILL,
}
EXTRA_ARMS: tuple[str, ...] = tuple(EXTRA_ARM_SYSTEM_PROMPT_PATHS)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return int(port)


def _wait_port(port: int, timeout: float = 15.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
        time.sleep(0.2)
    return False


def run_extra_arm(
    *,
    arm: str,
    task_id: str,
    prompt: str,
    model: str,
    rep: int,
    make_baseline_config: Callable[..., Path],
    timeout: int = 120,
    flow_path: Path | None = None,
) -> dict[str, Any]:
    """One (task, arm, rep) call: vanilla Claude Code + one appended system prompt.

    ``make_baseline_config`` is ``benchmarks.codebench.run._make_baseline_config``,
    injected by the caller (already on ``sys.path`` there) rather than imported
    here, so this module has no import-time dependency on codebench's runner.
    Row shape mirrors codebench's ``ArmResult`` fields that ``report.py`` reads
    (``task``/``arm``/``rep``/``ok``/``is_error``/``cost_usd``/``output_tokens``),
    so these rows merge into the same combined ``results.jsonl``.

    ``flow_path``, when given, wraps the call through a local mitmdump proxy
    (same wire capture codebench arms get) so the raw request/response and a
    human-readable ``<flow_path>_dump.txt`` transcript are on disk next to the
    other arms' -- and so turn/cache-token counts can be reconciled against the
    wire the same way ``run.py:_parse_claude_result`` does, rather than trusting
    only the CLI's own JSON receipt (which a plain ``claude -p`` call with no
    ``--agent`` does not reliably populate ``num_turns`` on).
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
        "num_turns": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "duration_ms": 0,
        "result_excerpt": "",
        "flow_path": str(flow_path) if flow_path else "",
    }
    from benchmarks.codebench.run import CA_CERT, REPO_ROOT

    proxy_supported = flow_path is not None
    port = _free_port() if proxy_supported else 0
    mitm = (
        subprocess.Popen(
            [
                "uv",
                "run",
                "--project",
                str(REPO_ROOT / "benchmarks"),
                "mitmdump",
                "-w",
                str(flow_path),
                "--listen-port",
                str(port),
                "-s",
                str(REPO_ROOT / "benchmarks" / "codebench" / "rate_limit.py"),
                "-q",
            ],
            cwd=str(REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if proxy_supported
        else None
    )
    try:
        if proxy_supported and not _wait_port(port):
            row["result_excerpt"] = "mitmdump did not start"
            return row
        if proxy_supported:
            env["HTTPS_PROXY"] = f"http://127.0.0.1:{port}"
            env["HTTP_PROXY"] = f"http://127.0.0.1:{port}"
            env["NODE_EXTRA_CA_CERTS"] = str(CA_CERT)
            env["SSL_CERT_FILE"] = str(CA_CERT)
            env["REQUESTS_CA_BUNDLE"] = str(CA_CERT)
            env["AWS_CA_BUNDLE"] = str(CA_CERT)
        with tempfile.TemporaryDirectory(prefix=f"lemoncrow-bench-{arm}-") as ws:
            try:
                proc = subprocess.run(cmd, cwd=ws, env=env, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                row["result_excerpt"] = f"timeout after {timeout}s"
                return row
    finally:
        if mitm is not None:
            mitm.terminate()
            with contextlib.suppress(Exception):
                mitm.wait(timeout=5)
    if proc.returncode != 0:
        row["result_excerpt"] = proc.stderr[-2000:]
        return row
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        row["result_excerpt"] = proc.stdout[-2000:]
        return row
    usage = data.get("usage") or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    num_turns = int(data.get("num_turns") or 0)
    if proxy_supported and flow_path is not None:
        with contextlib.suppress(Exception):
            from benchmarks.codebench.run import _read_flow_usage

            wire = _read_flow_usage(flow_path)
            if wire is not None:
                w_in, w_out, w_cr, w_cw, _w_cw1h, w_requests = wire
                if (w_in + w_out + w_cr + w_cw) >= (input_tokens + output_tokens + cache_read + cache_write):
                    input_tokens, output_tokens, cache_read, cache_write = w_in, w_out, w_cr, w_cw
                    num_turns = w_requests or num_turns
        with contextlib.suppress(Exception):
            from benchmarks.flowlib.dump import extract

            extract(str(flow_path), str(flow_path.with_suffix(".flow_dump.txt")))
    row.update(
        ok=not data.get("is_error", True),
        is_error=bool(data.get("is_error")),
        cost_usd=float(data.get("total_cost_usd") or 0.0),
        output_tokens=output_tokens,
        input_tokens=input_tokens,
        num_turns=num_turns,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_write,
        duration_ms=int(data.get("duration_ms") or 0),
        result_excerpt=str(data.get("result") or "")[:2000],
    )
    return row
