"""AtelierClaudeAgent — TerminalBench adapter that runs claude via stream-json.

Wires the Atelier bench-mode toggle into TerminalBench's Docker execution model.
``run_terminalbench_trial()`` is the central function: it instantiates Harness,
runs the trial, then post-processes TrialResults + stream.jsonl to assemble the
full AdapterResult transcript.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from terminal_bench.agents.installed_agents.abstract_installed_agent import (
    AbstractInstalledAgent,
)
from terminal_bench.terminal.models import TerminalCommand

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONTAINER_STREAM_LOG = "/agent-logs/stream.jsonl"
DEFAULT_DATASET_NAME = "terminal-bench-core"
DEFAULT_DATASET_VERSION = "0.1.1"


# ---------------------------------------------------------------------------
# AdapterResult — TB-04 transcript schema
# ---------------------------------------------------------------------------


@dataclass
class AdapterResult:
    """Full transcript record for a single TerminalBench trial run.

    All fields correspond to the TB-04 schema.  Use ``to_dict()`` to serialise
    to a plain dict for JSON output.
    """

    task_id: str
    mode: str  # "on" | "off"
    rep: int
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    latency_ms: float
    latency_api_ms: float
    num_turns: int
    cost_usd: float
    grader_verdict: str | None  # "pass" | "fail" | "error" | None
    grader_is_resolved: bool | None
    grader_failure_mode: str | None
    trial_started_at: str | None
    trial_ended_at: str | None
    is_error: bool
    stop_reason: str
    claude_error: str | None
    stream_log_path: str | None
    atelier_bench_mode: str
    atelier_root: str
    dataset_name: str
    dataset_version: str

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict representation for JSON serialisation."""
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# parse_stream_jsonl — extract token / cost / latency from stream-json log
# ---------------------------------------------------------------------------


def parse_stream_jsonl(log_path: Path) -> dict[str, Any]:
    """Parse a claude --output-format stream-json NDJSON log file.

    Finds the last line where ``type == "result"`` and extracts token usage,
    cost, latency, turn count, and error fields.  Handles malformed / partial
    lines gracefully — never raises.

    Args:
        log_path: Path to the ``.jsonl`` file produced by the tee command.

    Returns:
        Dict with keys: ``input_tokens``, ``output_tokens``,
        ``cache_creation_input_tokens``, ``cache_read_input_tokens``,
        ``total_cost_usd``, ``duration_ms``, ``duration_api_ms``,
        ``num_turns``, ``is_error``, ``stop_reason``.
        Returns ``{"error": "no_result_line"}`` (with zeros) when no result
        line is present.
    """
    _ZERO_RESULT: dict[str, Any] = {
        "error": "no_result_line",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "total_cost_usd": 0.0,
        "duration_ms": 0.0,
        "duration_api_ms": 0.0,
        "num_turns": 0,
        "is_error": False,
        "stop_reason": "",
    }

    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return dict(_ZERO_RESULT)

    result_line: dict[str, Any] | None = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            result_line = obj

    if result_line is None:
        return dict(_ZERO_RESULT)

    usage: dict[str, Any] = result_line.get("usage") or {}
    return {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        "total_cost_usd": result_line.get("total_cost_usd", 0.0),
        "duration_ms": result_line.get("duration_ms", 0.0),
        "duration_api_ms": result_line.get("duration_api_ms", 0.0),
        "num_turns": result_line.get("num_turns", 0),
        "is_error": result_line.get("is_error", False),
        "stop_reason": result_line.get("stop_reason", ""),
    }


# ---------------------------------------------------------------------------
# AtelierClaudeAgent — AbstractInstalledAgent subclass
# ---------------------------------------------------------------------------


class AtelierClaudeAgent(AbstractInstalledAgent):
    """TerminalBench agent that runs ``claude`` CLI via stream-json inside Docker.

    The agent supports two bench-mode arms:
    - ``bench_mode="on"``  — Atelier routing / compaction / MCP tools active
    - ``bench_mode="off"`` — clean baseline (no Atelier tooling)

    The mode is injected into the container environment via
    ``ATELIER_BENCH_MODE``.  ``ATELIER_DEV_MODE`` is explicitly excluded to
    prevent tool-visibility contamination (PITFALLS.md #3b).
    """

    def __init__(
        self,
        bench_mode: str = "on",
        model: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._bench_mode = bench_mode
        self._model = model

    @staticmethod
    def name() -> str:  # type: ignore[override]
        return "atelier-claude"

    @property
    def _env(self) -> dict[str, str]:
        """Minimal container env — only specific keys forwarded (T-02-04)."""
        env: dict[str, str] = {
            "ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"],
            "ATELIER_BENCH_MODE": self._bench_mode,
            "FORCE_AUTO_BACKGROUND_TASKS": "1",
            "ENABLE_BACKGROUND_TASKS": "1",
        }
        # Forward model override if set
        if self._model is not None:
            env["ANTHROPIC_MODEL"] = self._model
        elif "ANTHROPIC_MODEL" in os.environ:
            env["ANTHROPIC_MODEL"] = os.environ["ANTHROPIC_MODEL"]
        # ATELIER_DEV_MODE is intentionally NOT forwarded (PITFALLS.md #3b)
        return env

    @property
    def _install_agent_script_path(self) -> Path:
        """Return path to the rendered setup.sh.j2 install script."""
        return self._get_templated_script_path("setup.sh.j2")

    def _run_agent_commands(self, instruction: str) -> list[TerminalCommand]:
        """Build the claude CLI command that tees stream-json to the log file.

        The instruction is escaped via ``shlex.quote`` to prevent shell
        injection (T-02-05).
        """
        escaped = shlex.quote(instruction)
        allowed = "Bash Edit Write Read Glob Grep LS"
        cmd = (
            f"claude --verbose --output-format stream-json -p {escaped} "
            f"--allowedTools {allowed} --dangerously-skip-permissions "
            f"2>&1 | tee {CONTAINER_STREAM_LOG}"
        )
        return [
            TerminalCommand(
                command=cmd,
                min_timeout_sec=0.0,
                max_timeout_sec=float("inf"),
                block=True,
                append_enter=True,
            )
        ]


# ---------------------------------------------------------------------------
# run_terminalbench_trial — orchestrate a single trial end-to-end
# ---------------------------------------------------------------------------


def run_terminalbench_trial(
    task_id: str,
    bench_mode: str,
    rep: int,
    out_dir: Path,
    model: str = "claude-sonnet-4-5",
    dataset_name: str = DEFAULT_DATASET_NAME,
    dataset_version: str = DEFAULT_DATASET_VERSION,
) -> AdapterResult:
    """Run a single TerminalBench trial and return a fully-populated AdapterResult.

    Injects the Atelier arm env into the process environment before starting
    Harness, then restores the original env in a finally block (T-02-env-snapshot).

    Args:
        task_id:         TerminalBench task identifier.
        bench_mode:      "on" or "off" — controls ATELIER_BENCH_MODE in container.
        rep:             Repetition number (1-based).
        out_dir:         Host directory for trial outputs and transcripts.
        model:           Claude model slug passed to the agent.
        dataset_name:    TerminalBench dataset name.
        dataset_version: TerminalBench dataset version.

    Returns:
        Fully-populated ``AdapterResult`` with all TB-04 fields.
    """
    from atelier.bench.mode import BenchMode, make_arm_env
    from terminal_bench import BenchmarkResults, Harness

    # Isolated temp dir for Atelier runtime state
    arm_tmp = Path(tempfile.mkdtemp(prefix=f"atelier_bench_{bench_mode}_"))

    mode_enum = BenchMode.ON if bench_mode == "on" else BenchMode.OFF
    arm_env = make_arm_env(arm_tmp, mode=mode_enum)

    # Snapshot current env for keys that will be overwritten
    old_env: dict[str, str | None] = {k: os.environ.get(k) for k in arm_env}
    os.environ.update(arm_env)

    trial_result = None
    stream_parsed: dict[str, Any] = {}
    stream_log_path: str | None = None
    claude_error: str | None = None

    try:
        run_id = f"{task_id}__{bench_mode}__rep{rep}"
        trial_out = out_dir / run_id
        trial_out.mkdir(parents=True, exist_ok=True)

        harness = Harness(
            output_path=trial_out,
            run_id=run_id,
            agent_import_path="terminalbench.agent_adapter:AtelierClaudeAgent",
            agent_kwargs={"bench_mode": bench_mode, "model": model},
            dataset_name=dataset_name,
            dataset_version=dataset_version,
            task_ids=[task_id],
            n_attempts=1,
            n_concurrent_trials=1,
            cleanup=True,
        )

        results: BenchmarkResults = harness.run()
        trial_result = results.results[0] if results.results else None

        # Find stream log produced inside the container and copied to host
        stream_files = list(trial_out.rglob("stream.jsonl"))
        if stream_files:
            stream_log_path = str(stream_files[0])
            stream_parsed = parse_stream_jsonl(stream_files[0])
        else:
            stream_parsed = {"error": "no_stream_log"}

    except Exception as exc:
        claude_error = str(exc)
        stream_parsed = {}
    finally:
        # Restore original environment
        for key, original_value in old_env.items():
            if original_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original_value

    # Derive grader verdict from TrialResults
    if trial_result is not None:
        if trial_result.is_resolved is True:
            grader_verdict = "pass"
        elif trial_result.is_resolved is False:
            grader_verdict = "fail"
        else:
            grader_verdict = "error"
        grader_is_resolved = trial_result.is_resolved
        failure_mode = trial_result.failure_mode
        grader_failure_mode = failure_mode.value if failure_mode is not None else None
        trial_started_at = trial_result.trial_started_at
        trial_ended_at = trial_result.trial_ended_at
    else:
        grader_verdict = "error"
        grader_is_resolved = None
        grader_failure_mode = None
        trial_started_at = None
        trial_ended_at = None

    return AdapterResult(
        task_id=task_id,
        mode=bench_mode,
        rep=rep,
        model=model,
        input_tokens=int(stream_parsed.get("input_tokens", 0)),
        output_tokens=int(stream_parsed.get("output_tokens", 0)),
        cache_creation_input_tokens=int(stream_parsed.get("cache_creation_input_tokens", 0)),
        cache_read_input_tokens=int(stream_parsed.get("cache_read_input_tokens", 0)),
        latency_ms=float(stream_parsed.get("duration_ms", 0.0)),
        latency_api_ms=float(stream_parsed.get("duration_api_ms", 0.0)),
        num_turns=int(stream_parsed.get("num_turns", 0)),
        cost_usd=float(stream_parsed.get("total_cost_usd", 0.0)),
        grader_verdict=grader_verdict,
        grader_is_resolved=grader_is_resolved,
        grader_failure_mode=grader_failure_mode,
        trial_started_at=trial_started_at,
        trial_ended_at=trial_ended_at,
        is_error=bool(stream_parsed.get("is_error", False)),
        stop_reason=str(stream_parsed.get("stop_reason", "")),
        claude_error=claude_error,
        stream_log_path=stream_log_path,
        atelier_bench_mode=bench_mode,
        atelier_root=str(arm_tmp),
        dataset_name=dataset_name,
        dataset_version=dataset_version,
    )
