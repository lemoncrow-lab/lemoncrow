"""Head-to-head runner: vanilla Claude Code (baseline) vs Atelier-enabled (candidate).

For each task and arm we:
  1. prepare an isolated workspace (empty / git checkout / bundled copy),
  2. start mitmdump capturing the model traffic to a .flow file,
  3. run ``claude -p <prompt>`` headless, pinned to one model, through the proxy,
  4. record cost (real, from CLI JSON), latency, and token usage.

Baseline uses an isolated CLAUDE_CONFIG_DIR with plugins/hooks/MCP stripped
(but real subscription credentials copied in) so it is contamination-free.
The Atelier arm adds the atelier stdio MCP server + a tool-discipline CLAUDE.md.

Usage:
    uv run python -m benchmarks.vix_eval.run --tasks task1 --reps 1 --model sonnet
    uv run python -m benchmarks.vix_eval.run --tasks task1 --arms baseline atelier \
        --model claude-opus-4-8 \
        --agent-env ANTHROPIC_BASE_URL=https://openrouter.ai/api \
        --agent-env-from-host ANTHROPIC_AUTH_TOKEN=OPENROUTER_API_KEY \
        --agent-env ANTHROPIC_API_KEY=
    uv run python -m benchmarks.vix_eval.run --report results/<run_dir>
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

from atelier.core.capabilities.host_runners import build_vix_cli_command

from benchmarks.vix_eval.tasks import BY_ID, TASKS, Task

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPO_ROOT / "benchmarks" / "vix_eval" / "results"
CA_CERT = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"

EMPTY_MCP: dict[str, dict[str, object]] = {"mcpServers": {}}
VALID_ARMS = ("baseline", "atelier", "vix")
CLI_DRIVERS = ("claude", "copilot", "codex", "opencode")
PLACEHOLDER_RESPONSE_MARKERS = (
    "i'm ready to help",
    "what would you like to work on",
    "how can i help",
    "what can i help you with",
)
META_ACTION_MARKERS = (
    "i need to research",
    "let me research",
    "i'll start by",
    "i will start by",
    "let me investigate",
    "search the web",
    "search broadly",
    "let me search",
)
CLARIFICATION_REQUEST_MARKERS = (
    "could you tell me more",
    "could you clarify",
    "please provide",
    "need more context",
    "is there a repo",
    "should i scaffold",
    "once you share",
    "share the source",
    "actual task description",
)
WORKSPACE_CONFUSION_MARKERS = (
    "workspace contains only",
    "workspace only contains",
    "only the `claude.md` file",
    "only the claude.md file",
    "empty project directory",
    "no git repository",
)
RUNTIME_ERROR_MARKERS = (
    "requires more credits",
    "the server returned http",
    "api error:",
    "permission denied",
    "timed out",
)
STOPWORDS = frozenset(
    {
        "about",
        "after",
        "again",
        "also",
        "because",
        "been",
        "before",
        "being",
        "between",
        "both",
        "cache",
        "could",
        "each",
        "from",
        "have",
        "into",
        "last",
        "make",
        "must",
        "name",
        "prompt",
        "return",
        "should",
        "task",
        "that",
        "their",
        "them",
        "then",
        "there",
        "these",
        "this",
        "those",
        "through",
        "using",
        "with",
        "without",
        "work",
        "would",
        "your",
    }
)
API_DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "litellm": "http://localhost:4000/v1",
    "ollama": "http://localhost:11434/v1",
}
ATELIER_CLAUDE_MD = """# Tool discipline (benchmark candidate)

Prefer Atelier MCP tools over native ones to minimise context/token use:
- Read files with `mcp__atelier__read` (outline mode for large files), not full reads.
- Search with `mcp__atelier__grep` / `mcp__atelier__search` instead of dumping files.
- For symbols use `mcp__atelier__node` / `mcp__atelier__symbols` (one symbol, not whole file).
- Trace callers/callees with `mcp__atelier__callers` / `mcp__atelier__callees`.
Keep reads narrow; do not re-read unchanged files.
"""


def _atelier_mcp_config(host: str) -> dict[str, object]:
    return {
        "mcpServers": {
            "atelier": {
                "type": "stdio",
                "command": "atelier-mcp",
                "args": ["--host", host],
                "env": {},
            }
        }
    }


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return int(port)


def _wait_port(port: int, timeout: float = 15.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        with (
            contextlib.suppress(OSError),
            socket.create_connection(("127.0.0.1", port), timeout=0.5),
        ):
            return True
        time.sleep(0.2)
    return False


def _make_baseline_config() -> Path:
    """Isolated CLAUDE_CONFIG_DIR: real auth, no plugins/hooks/MCP."""
    cfg = Path(_mktemp("cfg-"))
    src = Path.home() / ".claude.json"
    data = json.loads(src.read_text())
    for k in ("enabledPlugins", "hooks", "mcpServers"):
        data.pop(k, None)
    for proj in data.get("projects", {}).values():
        if isinstance(proj, dict):
            for k in ("mcpServers", "enabledPlugins", "hooks"):
                proj.pop(k, None)
    (cfg / ".claude.json").write_text(json.dumps(data))
    creds = Path.home() / ".claude" / ".credentials.json"
    if creds.exists():
        shutil.copy(creds, cfg / ".credentials.json")
    return cfg


def _copy_if_exists(src: Path, dest: Path) -> None:
    if src.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest)


def _instruction_filename(cli_driver: str) -> str:
    return "AGENTS.md" if cli_driver == "codex" else "CLAUDE.md"


def _make_codex_home(enable_atelier: bool) -> Path:
    home = Path(_mktemp("codex-home-"))
    source_home = Path.home() / ".codex"
    _copy_if_exists(source_home / "auth.json", home / "auth.json")
    _copy_if_exists(source_home / "installation_id", home / "installation_id")
    if not enable_atelier:
        return home
    env = os.environ.copy()
    env["CODEX_HOME"] = str(home)
    subprocess.run(
        ["codex", "mcp", "add", "atelier", "--", "atelier-mcp", "--host", "codex"],
        check=True,
        timeout=120,
        env=env,
        capture_output=True,
        text=True,
    )
    return home


def _make_opencode_home(enable_atelier: bool, workspace: Path) -> Path:
    home = Path(_mktemp("opencode-home-"))
    (home / "agents").mkdir(parents=True, exist_ok=True)
    config: dict[str, object] = {}
    if enable_atelier:
        config = {
            "default_agent": "atelier",
            "permission": {"atelier_*": "allow"},
            "mcp": {
                "atelier": {
                    "type": "local",
                    "command": ["atelier-mcp", "--host", "opencode"],
                    "environment": {"ATELIER_WORKSPACE_ROOT": str(workspace)},
                }
            },
        }
        shutil.copy(
            REPO_ROOT / "integrations" / "opencode" / "agents" / "atelier.md",
            home / "agents" / "atelier.md",
        )
    (home / "opencode.json").write_text(json.dumps(config), encoding="utf-8")
    return home


def _mktemp(prefix: str) -> str:
    import tempfile

    return tempfile.mkdtemp(prefix=f"vixeval-{prefix}")


def prepare_workspace(task: Task) -> Path:
    ws = Path(_mktemp(f"ws-{task.id}-"))
    kind = task.source[0]
    if kind == "empty":
        pass
    elif kind == "workspace":
        src = task.workspace_src()
        if not src or not src.exists():
            raise FileNotFoundError(f"bundled workspace missing for {task.id}: {src}")
        for item in src.iterdir():
            dst = ws / item.name
            if item.is_dir():
                shutil.copytree(item, dst)
            else:
                shutil.copy(item, dst)
    elif kind == "repo":
        if len(task.source) < 3:
            raise ValueError(f"repo source missing url/commit for {task.id}: {task.source}")
        url, commit = task.source[1], task.source[2]
        subprocess.run(["git", "clone", "--quiet", url, str(ws)], check=True, timeout=900)
        if commit:
            subprocess.run(["git", "-C", str(ws), "checkout", "--quiet", commit], check=True, timeout=120)
    else:
        raise ValueError(f"unknown source kind {kind}")
    return ws


@dataclass
class ArmResult:
    task: str
    arm: str
    rep: int
    ok: bool
    cost_usd: float
    duration_ms: int
    duration_api_ms: int
    num_turns: int
    input_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    output_tokens: int
    models: list[str]
    is_error: bool
    result_excerpt: str
    flow_path: str
    valid: bool = True
    validity_reason: str = ""
    correct: bool | None = None
    score: float | None = None
    judge_model: str = ""
    judge_reason: str = ""


def _parse_claude_result(stdout: str, flow_path: Path, task: str, arm: str, rep: int) -> ArmResult:
    try:
        d = json.loads(stdout)
    except json.JSONDecodeError:
        return ArmResult(task, arm, rep, False, 0.0, 0, 0, 0, 0, 0, 0, 0, [], True, stdout[:200], str(flow_path))
    u = d.get("usage", {}) or {}
    return ArmResult(
        task=task,
        arm=arm,
        rep=rep,
        ok=not d.get("is_error", False),
        cost_usd=float(d.get("total_cost_usd", 0.0) or 0.0),
        duration_ms=int(d.get("duration_ms", 0) or 0),
        duration_api_ms=int(d.get("duration_api_ms", 0) or 0),
        num_turns=int(d.get("num_turns", 0) or 0),
        input_tokens=int(u.get("input_tokens", 0) or 0),
        cache_read_tokens=int(u.get("cache_read_input_tokens", 0) or 0),
        cache_creation_tokens=int(u.get("cache_creation_input_tokens", 0) or 0),
        output_tokens=int(u.get("output_tokens", 0) or 0),
        models=list((d.get("modelUsage", {}) or {}).keys()),
        is_error=bool(d.get("is_error", False)),
        result_excerpt=str(d.get("result", ""))[:4000],
        flow_path=str(flow_path),
    )


def _iter_jsonl_objects(text: str) -> list[dict[str, object]]:
    objects: list[dict[str, object]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            objects.append(obj)
    return objects


def _flatten_text_blocks(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            for key in ("text", "content", "value", "message"):
                raw = item.get(key)
                if isinstance(raw, str) and raw.strip():
                    parts.append(raw)
                    break
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "content", "value", "message"):
            raw = value.get(key)
            flattened = _flatten_text_blocks(raw)
            if flattened:
                return flattened
    return ""


def _usage_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        return int(float(value))
    return 0


def _parse_copilot_result(
    stdout: str,
    flow_path: Path,
    task: str,
    arm: str,
    rep: int,
    wall_duration_ms: int,
) -> ArmResult:
    events = _iter_jsonl_objects(stdout)
    assistant_messages: list[str] = []
    models: set[str] = set()
    input_tokens = 0
    cache_read_tokens = 0
    cache_creation_tokens = 0
    output_tokens = 0
    for event in events:
        event_type = str(event.get("type") or "")
        data = event.get("data")
        payload = data if isinstance(data, dict) else event
        if event_type == "assistant.message":
            text = _flatten_text_blocks(payload.get("content"))
            if not text:
                text = _flatten_text_blocks(payload.get("message"))
            if not text:
                text = _flatten_text_blocks(payload.get("text"))
            if text:
                assistant_messages.append(text)
            model = payload.get("model")
            if isinstance(model, str) and model:
                models.add(model)
        elif event_type == "session.shutdown":
            metrics = payload.get("modelMetrics")
            if isinstance(metrics, dict):
                input_tokens = 0
                cache_read_tokens = 0
                cache_creation_tokens = 0
                output_tokens = 0
                for model_name, metric in metrics.items():
                    if isinstance(model_name, str) and model_name:
                        models.add(model_name)
                    if not isinstance(metric, dict):
                        continue
                    input_total = _usage_int(
                        metric.get("inputTokens")
                        or metric.get("promptTokens")
                        or metric.get("input_tokens")
                        or metric.get("prompt_tokens")
                    )
                    cached_tokens = _usage_int(
                        metric.get("cachedInputTokens")
                        or metric.get("cacheReadTokens")
                        or metric.get("cached_input_tokens")
                        or metric.get("cache_read_tokens")
                    )
                    input_tokens += max(input_total - cached_tokens, 0)
                    cache_read_tokens += cached_tokens
                    cache_creation_tokens += _usage_int(
                        metric.get("cacheCreationInputTokens")
                        or metric.get("cacheWriteTokens")
                        or metric.get("cache_creation_input_tokens")
                        or metric.get("cache_write_tokens")
                    )
                    output_tokens += _usage_int(
                        metric.get("outputTokens")
                        or metric.get("completionTokens")
                        or metric.get("output_tokens")
                        or metric.get("completion_tokens")
                    )
    excerpt = (assistant_messages[-1] if assistant_messages else stdout)[:4000]
    return ArmResult(
        task=task,
        arm=arm,
        rep=rep,
        ok=bool(assistant_messages),
        cost_usd=0.0,
        duration_ms=wall_duration_ms,
        duration_api_ms=wall_duration_ms,
        num_turns=len(assistant_messages),
        input_tokens=input_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        output_tokens=output_tokens,
        models=sorted(models),
        is_error=not bool(assistant_messages),
        result_excerpt=excerpt,
        flow_path=str(flow_path),
    )


def _parse_codex_result(
    stdout: str,
    flow_path: Path,
    task: str,
    arm: str,
    rep: int,
    wall_duration_ms: int,
) -> ArmResult:
    events = _iter_jsonl_objects(stdout)
    assistant_messages: list[str] = []
    models: set[str] = set()
    input_tokens = 0
    cache_read_tokens = 0
    cache_creation_tokens = 0
    output_tokens = 0
    token_count_seen = False
    for event in events:
        event_type = str(event.get("type") or "")
        model = event.get("model") or event.get("model_id") or event.get("modelId")
        if isinstance(model, str) and model:
            models.add(model)
        if event_type == "message" and event.get("role") == "assistant":
            text = _flatten_text_blocks(event.get("content"))
            if text:
                assistant_messages.append(text)
            usage = event.get("usage")
            if isinstance(usage, dict) and not token_count_seen:
                input_tokens += max(
                    _usage_int(
                        usage.get("input_tokens")
                        or usage.get("inputTokens")
                        or usage.get("prompt_tokens")
                        or usage.get("promptTokens")
                    )
                    - _usage_int(
                        usage.get("cached_input_tokens")
                        or usage.get("cachedInputTokens")
                        or usage.get("cache_read_tokens")
                        or usage.get("cacheReadTokens")
                    ),
                    0,
                )
                cache_read_tokens += _usage_int(
                    usage.get("cached_input_tokens")
                    or usage.get("cachedInputTokens")
                    or usage.get("cache_read_tokens")
                    or usage.get("cacheReadTokens")
                )
                cache_creation_tokens += _usage_int(
                    usage.get("cache_creation_input_tokens")
                    or usage.get("cacheCreationInputTokens")
                    or usage.get("cache_write_tokens")
                    or usage.get("cacheWriteTokens")
                )
                output_tokens += _usage_int(
                    usage.get("output_tokens")
                    or usage.get("outputTokens")
                    or usage.get("completion_tokens")
                    or usage.get("completionTokens")
                )
        elif event_type == "event_msg":
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            total_usage = info.get("total_token_usage")
            if not isinstance(total_usage, dict):
                continue
            token_count_seen = True
            input_total = _usage_int(total_usage.get("input_tokens") or total_usage.get("inputTokens"))
            cache_read_tokens = _usage_int(
                total_usage.get("cached_input_tokens") or total_usage.get("cachedInputTokens")
            )
            cache_creation_tokens = _usage_int(
                total_usage.get("cache_creation_input_tokens") or total_usage.get("cacheCreationInputTokens")
            )
            output_tokens = _usage_int(total_usage.get("output_tokens") or total_usage.get("outputTokens"))
            input_tokens = max(input_total - cache_read_tokens, 0)
        elif event_type == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            if item.get("type") == "agent_message":
                text = str(item.get("text") or "").strip()
                if text:
                    assistant_messages.append(text)
        elif event_type == "turn.completed":
            usage = event.get("usage")
            if not isinstance(usage, dict):
                continue
            output_tokens = _usage_int(usage.get("output_tokens") or usage.get("outputTokens"))
            cache_read_tokens = _usage_int(usage.get("cached_input_tokens") or usage.get("cachedInputTokens"))
            input_total = _usage_int(usage.get("input_tokens") or usage.get("inputTokens"))
            input_tokens = max(input_total - cache_read_tokens, 0)
    excerpt = (assistant_messages[-1] if assistant_messages else stdout)[:4000]
    return ArmResult(
        task=task,
        arm=arm,
        rep=rep,
        ok=bool(assistant_messages),
        cost_usd=0.0,
        duration_ms=wall_duration_ms,
        duration_api_ms=wall_duration_ms,
        num_turns=len(assistant_messages),
        input_tokens=input_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        output_tokens=output_tokens,
        models=sorted(models),
        is_error=not bool(assistant_messages),
        result_excerpt=excerpt,
        flow_path=str(flow_path),
    )


def _parse_opencode_result(
    stdout: str,
    flow_path: Path,
    task: str,
    arm: str,
    rep: int,
    wall_duration_ms: int,
) -> ArmResult:
    events = _iter_jsonl_objects(stdout)
    assistant_messages: list[str] = []
    models: set[str] = set()
    input_tokens = 0
    cache_read_tokens = 0
    cache_creation_tokens = 0
    output_tokens = 0
    for event in events:
        event_type = str(event.get("_type") or event.get("type") or "")
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        if event_type == "message" and data.get("role") == "assistant":
            text = str(data.get("text") or "").strip()
            if text:
                assistant_messages.append(text)
            model_id = str(data.get("modelID") or data.get("model") or "").strip()
            provider_id = str(data.get("providerID") or "").strip()
            model = f"{provider_id}/{model_id}" if provider_id and model_id else model_id
            if model:
                models.add(model)
        elif event_type == "part" and data.get("type") == "step-finish":
            tokens = data.get("tokens")
            if not isinstance(tokens, dict):
                continue
            cache = tokens.get("cache")
            cache_read = _usage_int((cache or {}).get("read") if isinstance(cache, dict) else 0)
            input_total = _usage_int(tokens.get("input"))
            input_tokens = max(input_total - cache_read, 0)
            cache_read_tokens = cache_read
            cache_creation_tokens = _usage_int((cache or {}).get("write") if isinstance(cache, dict) else 0)
            output_tokens = _usage_int(tokens.get("output"))
    excerpt = (assistant_messages[-1] if assistant_messages else stdout)[:4000]
    return ArmResult(
        task=task,
        arm=arm,
        rep=rep,
        ok=bool(assistant_messages),
        cost_usd=0.0,
        duration_ms=wall_duration_ms,
        duration_api_ms=wall_duration_ms,
        num_turns=len(assistant_messages),
        input_tokens=input_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        output_tokens=output_tokens,
        models=sorted(models),
        is_error=not bool(assistant_messages),
        result_excerpt=excerpt,
        flow_path=str(flow_path),
    )


def _parse_cli_result(
    stdout: str,
    flow_path: Path,
    task: str,
    arm: str,
    rep: int,
    cli_driver: str,
    wall_duration_ms: int,
) -> ArmResult:
    if cli_driver == "claude":
        result = _parse_claude_result(stdout, flow_path, task, arm, rep)
        if result.duration_ms == 0:
            result.duration_ms = wall_duration_ms
        if result.duration_api_ms == 0:
            result.duration_api_ms = wall_duration_ms
        return result
    if cli_driver == "copilot":
        return _parse_copilot_result(stdout, flow_path, task, arm, rep, wall_duration_ms)
    if cli_driver == "codex":
        return _parse_codex_result(stdout, flow_path, task, arm, rep, wall_duration_ms)
    if cli_driver == "opencode":
        return _parse_opencode_result(stdout, flow_path, task, arm, rep, wall_duration_ms)
    raise ValueError(f"unsupported cli driver: {cli_driver}")


def _extract_keywords(text: str, *, limit: int = 24) -> set[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{3,}", text.lower())
    counts: dict[str, int] = {}
    for token in tokens:
        if token in STOPWORDS:
            continue
        counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))
    return {token for token, _count in ranked[:limit]}


def _validate_result_excerpt(task: Task, excerpt: str) -> tuple[bool, str]:
    text = excerpt.strip()
    lowered = text.lower()
    if not text:
        return False, "empty response"
    if lowered.startswith("harness error:"):
        return False, "harness/runtime error"
    if any(marker in lowered for marker in RUNTIME_ERROR_MARKERS):
        return False, "runtime/provider error surfaced in result"
    if any(marker in lowered for marker in PLACEHOLDER_RESPONSE_MARKERS):
        return False, "generic placeholder response"
    if text.lstrip().startswith('{"title"'):
        return False, "session-title payload instead of task response"
    task_keywords = _extract_keywords(f"{task.prompt()}\n{_task_description(task)}")
    response_keywords = _extract_keywords(text)
    overlap = task_keywords & response_keywords
    list_item_count = sum(
        1 for line in text.splitlines() if line.lstrip().startswith("- ") or re.match(r"^\s*\d+\.\s", line) is not None
    )
    if len(overlap) == 0 and list_item_count >= 3:
        return False, f"off-task capability/list response (list_items={list_item_count})"
    if any(marker in lowered for marker in META_ACTION_MARKERS) and len(overlap) < 2:
        return False, f"off-topic planning/research response (keyword overlap={len(overlap)})"
    if (
        any(marker in lowered for marker in CLARIFICATION_REQUEST_MARKERS)
        and len(task.prompt()) > 200
        and len(overlap) < 2
    ):
        return False, f"unnecessary clarification request (keyword overlap={len(overlap)})"
    if any(marker in lowered for marker in WORKSPACE_CONFUSION_MARKERS) and len(overlap) < 2:
        return False, f"workspace confusion overrode task prompt (keyword overlap={len(overlap)})"
    if task_keywords and len(overlap) == 0:
        return False, "no task keyword overlap"
    return True, ""


def _apply_result_validity(task: Task, result: ArmResult) -> ArmResult:
    valid, reason = _validate_result_excerpt(task, result.result_excerpt)
    result.valid = valid
    result.validity_reason = reason
    if not valid:
        result.ok = False
    return result


def _parse_agent_env(entries: list[str] | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for entry in entries or []:
        key, sep, value = entry.partition("=")
        if not sep or not key:
            raise ValueError(f"invalid --agent-env entry: {entry!r}; expected KEY=VALUE")
        parsed[key] = value
    return parsed


def _env_file_candidates() -> tuple[Path, ...]:
    return (
        REPO_ROOT / ".env",
        REPO_ROOT / "benchmarks" / "vix_eval" / ".env",
    )


def _parse_env_assignment(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[7:].strip()
    key, sep, value = stripped.partition("=")
    if not sep or not key:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return key.strip(), value


def _resolve_host_env_value(name: str) -> str | None:
    if name in os.environ:
        return os.environ[name]
    for path in _env_file_candidates():
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            parsed = _parse_env_assignment(line)
            if parsed is None:
                continue
            key, value = parsed
            if key == name:
                return value
    return None


def _parse_agent_env_from_host(entries: list[str] | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for entry in entries or []:
        dest, sep, source = entry.partition("=")
        if not sep or not dest or not source:
            raise ValueError(f"invalid --agent-env-from-host entry: {entry!r}; expected DEST_KEY=SOURCE_ENV")
        value = _resolve_host_env_value(source)
        if value is None:
            raise ValueError(f"missing host environment variable for --agent-env-from-host: {source}")
        parsed[dest] = value
    return parsed


def _as_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value or 0.0)
    raise TypeError(f"cannot convert {type(value).__name__} to float")


def run_arm(
    task: Task,
    arm: str,
    rep: int,
    model: str,
    out_dir: Path,
    timeout: int,
    agent_command: str = "claude",
    transport: str = "cli",
    cli_driver: str = "claude",
    api_provider: str = "ollama",
    api_base_url: str | None = None,
    api_key_env: str | None = None,
    agent_env: dict[str, str] | None = None,
    cli_extra_args: list[str] | tuple[str, ...] = (),
) -> ArmResult:
    assert arm in VALID_ARMS
    ws = prepare_workspace(task)
    if transport == "api":
        try:
            result = run_api_arm(
                task,
                arm,
                rep,
                model,
                ws,
                timeout,
                api_provider=api_provider,
                api_base_url=api_base_url,
                api_key_env=api_key_env,
            )
            return _apply_result_validity(task, result)
        finally:
            shutil.rmtree(ws, ignore_errors=True)
    if transport != "cli":
        raise ValueError(f"unknown transport {transport}")
    if cli_driver not in CLI_DRIVERS:
        raise ValueError(f"unsupported cli driver: {cli_driver}")
    flow_path = out_dir / f"{task.id}_{arm}_rep{rep}.flow"
    proxy_supported = cli_driver == "claude"
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
            raise RuntimeError("mitmdump did not start")
        env = dict(os.environ)
        env.update(agent_env or {})
        if proxy_supported:
            env["HTTPS_PROXY"] = f"http://127.0.0.1:{port}"
            env["HTTP_PROXY"] = f"http://127.0.0.1:{port}"
            env["NODE_EXTRA_CA_CERTS"] = str(CA_CERT)
        temp_paths: list[Path] = []
        if cli_driver == "claude":
            cmd = build_vix_cli_command(
                cli_driver=cli_driver,
                prompt=task.prompt(),
                model=model,
                workspace=str(ws),
                agent_command=agent_command,
                extra_args=cli_extra_args,
            )
            if arm == "atelier":
                cmd += [
                    "--strict-mcp-config",
                    "--mcp-config",
                    json.dumps(_atelier_mcp_config("claude")),
                ]
                (ws / _instruction_filename(cli_driver)).write_text(ATELIER_CLAUDE_MD)
                baseline_cfg = _make_baseline_config()
                temp_paths.append(baseline_cfg)
                env["CLAUDE_CONFIG_DIR"] = str(baseline_cfg)
            elif arm == "baseline":
                cmd += ["--strict-mcp-config", "--mcp-config", json.dumps(EMPTY_MCP)]
                baseline_cfg = _make_baseline_config()
                temp_paths.append(baseline_cfg)
                env["CLAUDE_CONFIG_DIR"] = str(baseline_cfg)
        elif cli_driver == "copilot":
            cmd = build_vix_cli_command(
                cli_driver=cli_driver,
                prompt=task.prompt(),
                model=model,
                workspace=str(ws),
                extra_args=cli_extra_args,
            )
            if arm in {"baseline", "atelier"}:
                cmd.append("--disable-builtin-mcps")
            if arm == "baseline":
                cmd.append("--no-custom-instructions")
            if arm == "atelier":
                cmd.extend(["--additional-mcp-config", json.dumps(_atelier_mcp_config("copilot"))])
                (ws / _instruction_filename(cli_driver)).write_text(ATELIER_CLAUDE_MD)
        elif cli_driver == "codex":
            cmd = build_vix_cli_command(
                cli_driver=cli_driver,
                prompt=task.prompt(),
                model=model,
                workspace=str(ws),
                extra_args=cli_extra_args,
            )
            if arm in {"baseline", "atelier"}:
                codex_home = _make_codex_home(enable_atelier=arm == "atelier")
                temp_paths.append(codex_home)
                env["CODEX_HOME"] = str(codex_home)
                cmd.append("--ignore-rules")
            if arm == "atelier":
                (ws / _instruction_filename(cli_driver)).write_text(ATELIER_CLAUDE_MD)
        else:
            cmd = build_vix_cli_command(
                cli_driver=cli_driver,
                prompt=task.prompt(),
                model=model,
                workspace=str(ws),
                extra_args=cli_extra_args,
            )
            if arm in {"baseline", "atelier"}:
                opencode_home = _make_opencode_home(enable_atelier=arm == "atelier", workspace=ws)
                temp_paths.append(opencode_home)
                env["OPENCODE_CONFIG_HOME"] = str(opencode_home)
                cmd.append("--pure")
            if arm == "atelier":
                cmd.extend(["--agent", "atelier"])
        started = time.time()
        proc = subprocess.run(
            cmd,
            cwd=str(ws),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        wall_duration_ms = int((time.time() - started) * 1000)
        res = _parse_cli_result(proc.stdout, flow_path, task.id, arm, rep, cli_driver, wall_duration_ms)
        if not res.ok and not proc.stdout.strip():
            res.result_excerpt = (proc.stderr or "")[:200]
        return _apply_result_validity(task, res)
    finally:
        if mitm is not None:
            mitm.terminate()
            with contextlib.suppress(Exception):
                mitm.wait(timeout=5)
        shutil.rmtree(ws, ignore_errors=True)
        for temp_path in locals().get("temp_paths", []):
            shutil.rmtree(temp_path, ignore_errors=True)


def run_api_arm(
    task: Task,
    arm: str,
    rep: int,
    model: str,
    workspace: Path,
    timeout: int,
    *,
    api_provider: str,
    api_base_url: str | None,
    api_key_env: str | None,
) -> ArmResult:
    prompt = task.prompt()
    if arm == "atelier":
        prompt = f"{ATELIER_CLAUDE_MD}\n\n{prompt}"
    elif arm == "baseline":
        prompt = f"Work in this repository path: {workspace}\n\n{prompt}"
    base_url = (api_base_url or API_DEFAULT_BASE_URLS[api_provider]).rstrip("/")
    api_key = os.environ.get(api_key_env or _default_api_key_env(api_provider), "")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=data,
        headers=_api_headers(api_provider, api_key),
        method="POST",
    )
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
        duration_ms = int((time.time() - started) * 1000)
        parsed = json.loads(body)
        choice = (parsed.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        result = str(message.get("content", ""))
        usage = parsed.get("usage") or {}
        return ArmResult(
            task=task.id,
            arm=arm,
            rep=rep,
            ok=True,
            cost_usd=0.0,
            duration_ms=duration_ms,
            duration_api_ms=duration_ms,
            num_turns=1,
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            cache_read_tokens=0,
            cache_creation_tokens=0,
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            models=[str(parsed.get("model") or model)],
            is_error=False,
            result_excerpt=result[:4000],
            flow_path="",
        )
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
        duration_ms = int((time.time() - started) * 1000)
        return ArmResult(
            task=task.id,
            arm=arm,
            rep=rep,
            ok=False,
            cost_usd=0.0,
            duration_ms=duration_ms,
            duration_api_ms=duration_ms,
            num_turns=1,
            input_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            output_tokens=0,
            models=[model],
            is_error=True,
            result_excerpt=f"api error: {exc}"[:200],
            flow_path="",
        )


def _default_api_key_env(api_provider: str) -> str:
    if api_provider == "openai":
        return "OPENAI_API_KEY"
    if api_provider == "litellm":
        return "LITELLM_API_KEY"
    return "OLLAMA_API_KEY"


def _api_headers(api_provider: str, api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key or api_provider != "ollama":
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _chat_completion(
    *,
    prompt: str,
    model: str,
    timeout: int,
    api_provider: str,
    api_base_url: str | None,
    api_key_env: str | None,
) -> tuple[str, dict[str, object], int]:
    base_url = (api_base_url or API_DEFAULT_BASE_URLS[api_provider]).rstrip("/")
    api_key = os.environ.get(api_key_env or _default_api_key_env(api_provider), "")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=_api_headers(api_provider, api_key),
        method="POST",
    )
    started = time.time()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    duration_ms = int((time.time() - started) * 1000)
    parsed = json.loads(body)
    choice = (parsed.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return str(message.get("content", "")), parsed, duration_ms


def _claude_completion(
    *,
    prompt: str,
    model: str,
    timeout: int,
    agent_command: str,
    agent_env: dict[str, str] | None = None,
) -> tuple[str, dict[str, object], int]:
    started = time.time()
    completed = subprocess.run(
        [
            *shlex.split(agent_command),
            "-p",
            prompt,
            "--model",
            model,
            "--output-format",
            "json",
            "--permission-mode",
            "bypassPermissions",
        ],
        cwd=str(REPO_ROOT),
        env={**os.environ, **(agent_env or {})},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    duration_ms = int((time.time() - started) * 1000)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "").strip()[:300])
    payload = json.loads(completed.stdout)
    return str(payload.get("result", "")), payload, duration_ms


def _task_description(task: Task) -> str:
    config_path = task.prompt_path().parent / "config.yaml"
    if not config_path.exists():
        return ""
    return config_path.read_text(encoding="utf-8")[:2000]


def _judge_prompt(task: Task, result: ArmResult) -> str:
    return f"""You are grading a VIX benchmark response.

Return ONLY compact JSON with these keys:
{{"correct": boolean, "score": number, "reason": string}}

Scoring:
- 1.0 means the response fully satisfies the task.
- 0.7 means mostly correct but incomplete or missing verification details.
- 0.4 means partially relevant but unlikely to solve the task.
- 0.0 means wrong, empty, or not responsive.

Task id: {task.id}
Task language: {task.language}
Task config:
{_task_description(task)}

Task prompt:
{task.prompt()}

Candidate response:
{result.result_excerpt}
"""


def judge_results(
    results: list[ArmResult],
    *,
    judge_transport: str,
    judge_provider: str,
    judge_model: str,
    judge_agent_command: str,
    judge_api_base_url: str | None,
    judge_api_key_env: str | None,
    timeout: int,
    agent_env: dict[str, str] | None = None,
) -> None:
    for result in results:
        if not result.ok:
            result.correct = False
            result.score = 0.0
            result.judge_model = judge_model
            result.judge_reason = "transport/runtime failure"
            continue
        task = BY_ID.get(result.task)
        if task is None:
            result.correct = False
            result.score = 0.0
            result.judge_model = judge_model
            result.judge_reason = f"unknown task {result.task}"
            continue
        try:
            prompt = _judge_prompt(task, result)
            if judge_transport == "cli":
                text, _payload, _duration_ms = _claude_completion(
                    prompt=prompt,
                    model=judge_model,
                    timeout=timeout,
                    agent_command=judge_agent_command,
                    agent_env=agent_env,
                )
            else:
                text, _payload, _duration_ms = _chat_completion(
                    prompt=prompt,
                    model=judge_model,
                    timeout=timeout,
                    api_provider=judge_provider,
                    api_base_url=judge_api_base_url,
                    api_key_env=judge_api_key_env,
                )
            parsed = _parse_judge_json(text)
            result.correct = bool(parsed.get("correct", False))
            result.score = max(0.0, min(1.0, _as_float(parsed.get("score", 0.0) or 0.0)))
            result.judge_model = judge_model
            result.judge_reason = str(parsed.get("reason", ""))[:300]
        except Exception as exc:
            result.correct = False
            result.score = 0.0
            result.judge_model = judge_model
            result.judge_reason = f"judge error: {exc}"[:300]


def _parse_judge_json(text: str) -> dict[str, object]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end >= start:
        stripped = stripped[start : end + 1]
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise ValueError("judge returned non-object JSON")
    return parsed


def _agg(results: list[ArmResult], arm: str) -> dict[str, float | int]:
    rs = [r for r in results if r.arm == arm]
    judged = [r for r in rs if r.score is not None]
    return {
        "runs": len(rs),
        "ok": sum(1 for r in rs if r.ok),
        "valid": sum(1 for r in rs if r.valid),
        "correct": sum(1 for r in rs if r.correct is True),
        "avg_score": round(sum(float(r.score or 0.0) for r in judged) / len(judged), 3) if judged else 0.0,
        "cost_usd": round(sum(r.cost_usd for r in rs), 4),
        "duration_ms": sum(r.duration_ms for r in rs),
        "output_tokens": sum(r.output_tokens for r in rs),
        "input_tokens": sum(r.input_tokens for r in rs),
    }


def report(results: list[ArmResult]) -> str:
    arms = _ordered_arms(results)
    aggregates = {arm: _agg(results, arm) for arm in arms}
    baseline = aggregates.get("baseline")
    lines = [
        "",
        "=== vix-eval head-to-head ===",
        f"{'metric':<16}" + "".join(f"{arm:>14}" for arm in arms),
    ]

    def row(label: str, values: list[float]) -> str:
        rendered = [f"{value:,.4f}" for value in values]
        return f"{label:<16}" + "".join(f"{value:>14}" for value in rendered)

    lines.append(row("cost_usd", [_as_float(aggregates[arm]["cost_usd"]) for arm in arms]))
    lines.append(row("duration_ms", [_as_float(aggregates[arm]["duration_ms"]) for arm in arms]))
    lines.append(row("input_tokens", [_as_float(aggregates[arm]["input_tokens"]) for arm in arms]))
    lines.append(row("output_tokens", [_as_float(aggregates[arm]["output_tokens"]) for arm in arms]))
    if baseline:
        lines.append("")
        for arm in arms:
            if arm == "baseline":
                continue
            current = aggregates[arm]
            cost_save = _savings_pct(_as_float(baseline["cost_usd"]), _as_float(current["cost_usd"]))
            time_save = _savings_pct(
                _as_float(baseline["duration_ms"]),
                _as_float(current["duration_ms"]),
            )
            lines.append(f"{arm} cost saving : {cost_save:+.1f}%  (Vix target ~47-50%)")
            lines.append(f"{arm} time saving : {time_save:+.1f}%  (Vix target ~40%)")
    ok_parts = [f"{arm} {aggregates[arm]['ok']}/{aggregates[arm]['runs']}" for arm in arms]
    lines.append(f"Runs ok     : {'  '.join(ok_parts)}")
    valid_parts = [f"{arm} {aggregates[arm]['valid']}/{aggregates[arm]['runs']}" for arm in arms]
    lines.append(f"Valid       : {'  '.join(valid_parts)}")
    if any(result.valid is False for result in results):
        lines.append("Validity    : invalid/off-topic runs detected; cost/token comparisons are not meaningful.")
    if any(result.score is not None for result in results):
        score_parts = [
            f"{arm} {aggregates[arm]['correct']}/{aggregates[arm]['runs']} avg={aggregates[arm]['avg_score']}"
            for arm in arms
        ]
        lines.append(f"Correct     : {'  '.join(score_parts)}")
    return "\n".join(lines)


def _detail_rows(results: list[ArmResult]) -> list[dict[str, object]]:
    return [asdict(result) for result in results]


def _summary_rows(results: list[ArmResult]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    baseline = _summary_row(results, "baseline") if any(result.arm == "baseline" for result in results) else None
    for arm in _ordered_arms(results):
        row = _summary_row(results, arm)
        if baseline is None:
            row.update(_empty_savings_columns())
        else:
            row.update(
                {
                    "cost_savings_vs_baseline_pct": _savings_pct(
                        _as_float(baseline["cost_usd"]),
                        _as_float(row["cost_usd"]),
                    ),
                    "duration_savings_vs_baseline_pct": _savings_pct(
                        _as_float(baseline["duration_ms"]),
                        _as_float(row["duration_ms"]),
                    ),
                    "input_token_savings_vs_baseline_pct": _savings_pct(
                        _as_float(baseline["input_tokens"]),
                        _as_float(row["input_tokens"]),
                    ),
                    "output_token_savings_vs_baseline_pct": _savings_pct(
                        _as_float(baseline["output_tokens"]),
                        _as_float(row["output_tokens"]),
                    ),
                }
            )
        rows.append(row)
    return rows


def _ordered_arms(results: list[ArmResult]) -> list[str]:
    seen = {result.arm for result in results}
    ordered = [arm for arm in VALID_ARMS if arm in seen]
    ordered.extend(sorted(seen - set(VALID_ARMS)))
    return ordered


def _summary_row(results: list[ArmResult], arm: str) -> dict[str, object]:
    arm_results = [result for result in results if result.arm == arm]
    return {
        "arm": arm,
        "runs": len(arm_results),
        "ok_runs": sum(1 for result in arm_results if result.ok),
        "failed_runs": sum(1 for result in arm_results if not result.ok),
        "valid_runs": sum(1 for result in arm_results if result.valid),
        "correct_runs": sum(1 for result in arm_results if result.correct is True),
        "avg_score": (
            round(sum(float(result.score or 0.0) for result in judged) / len(judged), 3)
            if (judged := [result for result in arm_results if result.score is not None])
            else ""
        ),
        "cost_usd": round(sum(result.cost_usd for result in arm_results), 4),
        "duration_ms": sum(result.duration_ms for result in arm_results),
        "duration_api_ms": sum(result.duration_api_ms for result in arm_results),
        "input_tokens": sum(result.input_tokens for result in arm_results),
        "cache_read_tokens": sum(result.cache_read_tokens for result in arm_results),
        "cache_creation_tokens": sum(result.cache_creation_tokens for result in arm_results),
        "output_tokens": sum(result.output_tokens for result in arm_results),
    }


def _empty_savings_columns() -> dict[str, object]:
    return {
        "cost_savings_vs_baseline_pct": "",
        "duration_savings_vs_baseline_pct": "",
        "input_token_savings_vs_baseline_pct": "",
        "output_token_savings_vs_baseline_pct": "",
    }


def _savings_pct(baseline: float, current: float) -> float:
    return round((1 - current / baseline) * 100, 1) if baseline else 0.0


def _write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _result_key(result: ArmResult) -> tuple[str, str, int]:
    return (result.task, result.arm, result.rep)


def _load_existing_results(run_dir: Path) -> list[ArmResult]:
    results_path = run_dir / "results.jsonl"
    if not results_path.exists():
        return []
    return [
        ArmResult(**json.loads(line)) for line in results_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def write_csv_artifacts(run_dir: Path, results: list[ArmResult]) -> None:
    _write_csv(
        run_dir / "results.csv",
        _detail_rows(results),
        [
            "task",
            "arm",
            "rep",
            "ok",
            "cost_usd",
            "duration_ms",
            "duration_api_ms",
            "num_turns",
            "input_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "output_tokens",
            "models",
            "is_error",
            "result_excerpt",
            "flow_path",
            "valid",
            "validity_reason",
            "correct",
            "score",
            "judge_model",
            "judge_reason",
        ],
    )
    _write_csv(
        run_dir / "summary.csv",
        _summary_rows(results),
        [
            "arm",
            "runs",
            "ok_runs",
            "failed_runs",
            "valid_runs",
            "correct_runs",
            "avg_score",
            "cost_usd",
            "duration_ms",
            "duration_api_ms",
            "input_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "output_tokens",
            "cost_savings_vs_baseline_pct",
            "duration_savings_vs_baseline_pct",
            "input_token_savings_vs_baseline_pct",
            "output_token_savings_vs_baseline_pct",
        ],
    )


def _run_task_rep(
    task_id: str,
    rep: int,
    *,
    arms: list[str],
    model: str,
    out_dir: Path,
    timeout: int,
    agent_command: str,
    transport: str,
    cli_driver: str,
    api_provider: str,
    api_base_url: str | None,
    api_key_env: str | None,
    agent_env: dict[str, str] | None,
    cli_extra_args: list[str] | tuple[str, ...],
) -> list[ArmResult]:
    task = BY_ID[task_id]
    results: list[ArmResult] = []
    for arm in arms:
        print(f"[run] {task_id} {arm} rep{rep} (model={model}, driver={cli_driver}) ...", flush=True)
        t0 = time.time()
        try:
            result = run_arm(
                task,
                arm,
                rep,
                model,
                out_dir,
                timeout,
                agent_command,
                transport,
                cli_driver,
                api_provider,
                api_base_url,
                api_key_env,
                agent_env,
                cli_extra_args,
            )
        except Exception as exc:
            result = ArmResult(
                task_id,
                arm,
                rep,
                False,
                0.0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                [],
                True,
                f"harness error: {exc}"[:200],
                "",
            )
            result = _apply_result_validity(task, result)
        wall = time.time() - t0
        print(
            f"     -> ok={result.ok} cost=${result.cost_usd:.4f} dur={result.duration_ms}ms wall={wall:.0f}s turns={result.num_turns} {result.result_excerpt[:60]!r}",
            flush=True,
        )
        results.append(result)
    return results


def _run_single_arm(
    task_id: str,
    rep: int,
    arm: str,
    *,
    model: str,
    out_dir: Path,
    timeout: int,
    agent_command: str,
    transport: str,
    cli_driver: str,
    api_provider: str,
    api_base_url: str | None,
    api_key_env: str | None,
    agent_env: dict[str, str] | None,
    cli_extra_args: list[str] | tuple[str, ...],
) -> ArmResult:
    return _run_task_rep(
        task_id,
        rep,
        arms=[arm],
        model=model,
        out_dir=out_dir,
        timeout=timeout,
        agent_command=agent_command,
        transport=transport,
        cli_driver=cli_driver,
        api_provider=api_provider,
        api_base_url=api_base_url,
        api_key_env=api_key_env,
        agent_env=agent_env,
        cli_extra_args=cli_extra_args,
    )[0]


def main() -> int:
    p = argparse.ArgumentParser(description="vix-eval head-to-head runner")
    p.add_argument("--tasks", nargs="*", default=["all"], help="task ids or 'all'")
    p.add_argument("--arms", nargs="*", default=["baseline", "atelier"])
    p.add_argument("--reps", type=int, default=1)
    p.add_argument("--model", default="sonnet")
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--transport", choices=["cli", "api"], default="cli")
    p.add_argument("--cli-driver", choices=CLI_DRIVERS, default="claude")
    p.add_argument("--jobs", type=int, default=1, help="Parallel task/rep workers; arms stay serial per worker")
    p.add_argument(
        "--parallel-scope",
        choices=["task", "arm"],
        default="task",
        help="Use 'arm' only for throughput experiments; 'task' preserves fair per-task comparisons.",
    )
    p.add_argument("--api-provider", choices=["openai", "litellm", "ollama"], default="ollama")
    p.add_argument("--api-base-url", default=None)
    p.add_argument("--api-key-env", default=None)
    p.add_argument("--launch-ollama", action="store_true", help="Start 'ollama serve' before API runs")
    p.add_argument("--judge", action="store_true", help="Score correctness with an LLM judge")
    p.add_argument("--judge-transport", choices=["cli", "api"], default=None)
    p.add_argument("--judge-provider", choices=["openai", "litellm", "ollama"], default=None)
    p.add_argument("--judge-model", default=None)
    p.add_argument("--judge-agent-command", default=None)
    p.add_argument("--judge-api-base-url", default=None)
    p.add_argument("--judge-api-key-env", default=None)
    p.add_argument("--agent-command", default="claude", help="Claude-compatible command to run each arm")
    p.add_argument(
        "--agent-env",
        action="append",
        default=[],
        help="Environment override for CLI transport in KEY=VALUE form; repeatable.",
    )
    p.add_argument(
        "--agent-env-from-host",
        action="append",
        default=[],
        help="Copy a host env var into CLI transport env as DEST_KEY=SOURCE_ENV; repeatable.",
    )
    p.add_argument(
        "--cli-extra-arg",
        action="append",
        default=[],
        help="Extra CLI argument passed to the selected driver; repeatable.",
    )
    p.add_argument("--bridge-command", default=None, help="Optional background bridge command to launch first")
    p.add_argument("--bridge-wait", type=float, default=3.0, help="Seconds to wait after launching the bridge")
    p.add_argument("--out", type=Path, default=None, help="directory for run artifacts")
    p.add_argument("--resume", action="store_true", help="append to existing out dir and skip done runs")
    p.add_argument("--report", default=None, help="path to a results dir to re-report")
    args = p.parse_args()
    agent_env = {
        **_parse_agent_env(args.agent_env),
        **_parse_agent_env_from_host(args.agent_env_from_host),
    }
    judge_transport = args.judge_transport or args.transport
    judge_provider = args.judge_provider or args.api_provider
    judge_model = args.judge_model or args.model
    judge_agent_command = args.judge_agent_command or args.agent_command
    if args.report:
        rdir = Path(args.report)
        report_results = _load_existing_results(rdir)
        if args.judge:
            judge_results(
                report_results,
                judge_transport=judge_transport,
                judge_provider=judge_provider,
                judge_model=judge_model,
                judge_agent_command=judge_agent_command,
                judge_api_base_url=args.judge_api_base_url or args.api_base_url,
                judge_api_key_env=args.judge_api_key_env or args.api_key_env,
                timeout=args.timeout,
                agent_env=agent_env,
            )
            (rdir / "results.jsonl").write_text(
                "".join(json.dumps(asdict(result)) + "\n" for result in report_results),
                encoding="utf-8",
            )
        write_csv_artifacts(rdir, report_results)
        rep_txt = report(report_results)
        (rdir / "report.txt").write_text(rep_txt)
        print(rep_txt)
        return 0
    task_ids = [t.id for t in TASKS] if args.tasks == ["all"] else args.tasks
    run_dir = args.out if args.out is not None else RESULTS_ROOT / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    unknown_arms = [arm for arm in args.arms if arm not in VALID_ARMS]
    if unknown_arms:
        p.error(f"unknown arm(s): {', '.join(unknown_arms)}")
    if args.jobs < 1:
        p.error("--jobs must be >= 1")
    bridge_command = args.bridge_command
    if args.launch_ollama and bridge_command is None:
        bridge_command = "ollama serve"
    bridge = subprocess.Popen(shlex.split(bridge_command), cwd=str(REPO_ROOT)) if bridge_command else None
    if bridge is not None and args.bridge_wait > 0:
        time.sleep(args.bridge_wait)
    results = _load_existing_results(run_dir) if args.resume else []
    completed = {_result_key(result) for result in results}
    jl_mode = "a" if args.resume else "w"
    jl = (run_dir / "results.jsonl").open(jl_mode, encoding="utf-8")
    try:
        pending_trials: list[tuple[str, int, list[str]]] = []
        pending_arms: list[tuple[str, int, str]] = []
        for tid in task_ids:
            for rep in range(args.reps):
                missing_arms = [arm for arm in args.arms if (tid, arm, rep) not in completed]
                if not missing_arms:
                    for arm in args.arms:
                        print(f"[skip] {tid} {arm} rep{rep} already recorded", flush=True)
                    continue
                for arm in args.arms:
                    if (tid, arm, rep) in completed:
                        print(f"[skip] {tid} {arm} rep{rep} already recorded", flush=True)
                pending_trials.append((tid, rep, missing_arms))
                pending_arms.extend((tid, rep, arm) for arm in missing_arms)

        if args.jobs == 1 and args.parallel_scope == "task":
            for tid, rep, pending_arms in pending_trials:
                trial_results = _run_task_rep(
                    tid,
                    rep,
                    arms=pending_arms,
                    model=args.model,
                    out_dir=run_dir,
                    timeout=args.timeout,
                    agent_command=args.agent_command,
                    transport=args.transport,
                    cli_driver=args.cli_driver,
                    api_provider=args.api_provider,
                    api_base_url=args.api_base_url,
                    api_key_env=args.api_key_env,
                    agent_env=agent_env,
                    cli_extra_args=args.cli_extra_arg,
                )
                for res in trial_results:
                    if _result_key(res) in completed:
                        continue
                    results.append(res)
                    completed.add(_result_key(res))
                    jl.write(json.dumps(asdict(res)) + "\n")
                    jl.flush()
        elif args.parallel_scope == "task":
            with ThreadPoolExecutor(max_workers=args.jobs) as executor:
                futures = {
                    executor.submit(
                        _run_task_rep,
                        tid,
                        rep,
                        arms=pending_arms,
                        model=args.model,
                        out_dir=run_dir,
                        timeout=args.timeout,
                        agent_command=args.agent_command,
                        transport=args.transport,
                        cli_driver=args.cli_driver,
                        api_provider=args.api_provider,
                        api_base_url=args.api_base_url,
                        api_key_env=args.api_key_env,
                        agent_env=agent_env,
                        cli_extra_args=args.cli_extra_arg,
                    ): (tid, rep)
                    for tid, rep, pending_arms in pending_trials
                }
                for future in as_completed(futures):
                    for res in future.result():
                        if _result_key(res) in completed:
                            continue
                        results.append(res)
                        completed.add(_result_key(res))
                        jl.write(json.dumps(asdict(res)) + "\n")
                        jl.flush()
        elif args.jobs == 1:
            for tid, rep, arm in pending_arms:
                res = _run_single_arm(
                    tid,
                    rep,
                    arm,
                    model=args.model,
                    out_dir=run_dir,
                    timeout=args.timeout,
                    agent_command=args.agent_command,
                    transport=args.transport,
                    cli_driver=args.cli_driver,
                    api_provider=args.api_provider,
                    api_base_url=args.api_base_url,
                    api_key_env=args.api_key_env,
                    agent_env=agent_env,
                    cli_extra_args=args.cli_extra_arg,
                )
                if _result_key(res) in completed:
                    continue
                results.append(res)
                completed.add(_result_key(res))
                jl.write(json.dumps(asdict(res)) + "\n")
                jl.flush()
        else:
            with ThreadPoolExecutor(max_workers=args.jobs) as executor:
                futures = {
                    executor.submit(
                        _run_single_arm,
                        tid,
                        rep,
                        arm,
                        model=args.model,
                        out_dir=run_dir,
                        timeout=args.timeout,
                        agent_command=args.agent_command,
                        transport=args.transport,
                        cli_driver=args.cli_driver,
                        api_provider=args.api_provider,
                        api_base_url=args.api_base_url,
                        api_key_env=args.api_key_env,
                        agent_env=agent_env,
                        cli_extra_args=args.cli_extra_arg,
                    ): (tid, rep, arm)
                    for tid, rep, arm in pending_arms
                }
                for future in as_completed(futures):
                    res = future.result()
                    if _result_key(res) in completed:
                        continue
                    results.append(res)
                    completed.add(_result_key(res))
                    jl.write(json.dumps(asdict(res)) + "\n")
                    jl.flush()
    finally:
        jl.close()
        if bridge is not None and bridge.poll() is None:
            bridge.terminate()
            with contextlib.suppress(Exception):
                bridge.wait(timeout=10)
    if args.judge:
        judge_results(
            results,
            judge_transport=judge_transport,
            judge_provider=judge_provider,
            judge_model=judge_model,
            judge_agent_command=judge_agent_command,
            judge_api_base_url=args.judge_api_base_url or args.api_base_url,
            judge_api_key_env=args.judge_api_key_env or args.api_key_env,
            timeout=args.timeout,
            agent_env=agent_env,
        )
        (run_dir / "results.jsonl").write_text(
            "".join(json.dumps(asdict(result)) + "\n" for result in results),
            encoding="utf-8",
        )
    write_csv_artifacts(run_dir, results)
    rep_txt = report(results)
    (run_dir / "report.txt").write_text(rep_txt)
    print(rep_txt)
    print(f"\nResults: {run_dir}")
    if any(result.valid is False for result in results):
        return 2
    if any(not result.ok for result in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
