"""Tests for the MCP output-shrink shadow-mode PostToolUse hook.

The hook is a standalone script that reads a JSON payload on stdin and prints
an optional JSON decision on stdout, so it is exercised as a subprocess with
crafted payloads -- isolating both LemonCrow state and the spill directory under
per-test tmp_path locations (matching tests/integrations/test_loop_discipline_hooks.py).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[2] / "integrations" / "claude" / "plugin" / "hooks"
HOOK = HOOKS / "mcp_output_shrink.py"


def _run(
    payload: dict, tmp_path: Path, env_extra: dict | None = None, stdin_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "LEMONCROW_ROOT": str(tmp_path / ".lemoncrow"),
        "LEMONCROW_MCP_SPILL_DIR": str(tmp_path / "spill"),
        **(env_extra or {}),
    }
    stdin = stdin_text if stdin_text is not None else json.dumps(payload)
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    return proc


def _spill_path_from_notice(text: str) -> Path:
    match = re.search(r"full: (\S+\.txt)\]", text)
    assert match is not None, text
    return Path(match.group(1))


def test_small_output_passes_through_untouched(tmp_path: Path) -> None:
    payload = {
        "tool_name": "mcp__someserver__sometool",
        "tool_response": "a small result",
    }
    proc = _run(payload, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_large_string_tool_response_is_shrunk_and_spilled(tmp_path: Path) -> None:
    big = "X" * 300_000
    payload = {"tool_name": "mcp__someserver__bigtool", "tool_response": big}
    proc = _run(payload, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "expected updatedToolOutput on stdout"

    out = json.loads(proc.stdout)
    updated = out["hookSpecificOutput"]["updatedToolOutput"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "[lc: shrunk" in updated
    assert len(updated) < len(big)

    spill_path = _spill_path_from_notice(updated)
    assert spill_path.read_text(encoding="utf-8") == big


def test_content_blocks_shape_tool_response_is_handled(tmp_path: Path) -> None:
    big = "Y" * 300_000
    payload = {
        "tool_name": "mcp__otherserver__searchtool",
        "tool_response": {"content": [{"type": "text", "text": big}]},
    }
    proc = _run(payload, tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    updated = out["hookSpecificOutput"]["updatedToolOutput"]
    spill_path = _spill_path_from_notice(updated)
    assert spill_path.read_text(encoding="utf-8") == big


def test_content_blocks_list_shape_tool_response_is_handled(tmp_path: Path) -> None:
    """tool_response may be a bare list of content blocks (no wrapping dict)."""
    big = "Z" * 300_000
    payload = {
        "tool_name": "mcp__otherserver__searchtool",
        "tool_response": [{"type": "text", "text": big}],
    }
    proc = _run(payload, tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    updated = out["hookSpecificOutput"]["updatedToolOutput"]
    spill_path = _spill_path_from_notice(updated)
    assert spill_path.read_text(encoding="utf-8") == big


def test_lemoncrow_bare_tool_name_is_skipped(tmp_path: Path) -> None:
    payload = {"tool_name": "mcp__lc__bash", "tool_response": "X" * 300_000}
    proc = _run(payload, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_lemoncrow_plugin_namespaced_tool_name_is_skipped(tmp_path: Path) -> None:
    payload = {"tool_name": "mcp__plugin_lemoncrow_lc__read", "tool_response": "X" * 300_000}
    proc = _run(payload, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_non_mcp_tool_name_is_skipped(tmp_path: Path) -> None:
    """Non-MCP tools (Bash, Edit, ...) are never this hook's concern."""
    payload = {"tool_name": "Bash", "tool_response": "X" * 300_000}
    proc = _run(payload, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_kill_switch_env_skips_even_large_non_lemoncrow_output(tmp_path: Path) -> None:
    payload = {"tool_name": "mcp__someserver__bigtool", "tool_response": "X" * 300_000}
    proc = _run(payload, tmp_path, env_extra={"LEMONCROW_SHADOW_SHRINK": "0"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_threshold_env_override_shrinks_below_default_threshold(tmp_path: Path) -> None:
    payload = {"tool_name": "mcp__someserver__smallish", "tool_response": "X" * 5000}
    proc = _run(payload, tmp_path, env_extra={"LEMONCROW_SHADOW_SHRINK_CHARS": "1000"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip()
    out = json.loads(proc.stdout)
    assert "[lc: shrunk" in out["hookSpecificOutput"]["updatedToolOutput"]


def test_threshold_env_zero_disables_shrinking(tmp_path: Path) -> None:
    payload = {"tool_name": "mcp__someserver__bigtool", "tool_response": "X" * 300_000}
    proc = _run(payload, tmp_path, env_extra={"LEMONCROW_SHADOW_SHRINK_CHARS": "0"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_malformed_stdin_exits_zero_with_no_output(tmp_path: Path) -> None:
    proc = _run({}, tmp_path, stdin_text="not json at all {{{")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_empty_stdin_exits_zero_with_no_output(tmp_path: Path) -> None:
    proc = _run({}, tmp_path, stdin_text="")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_unrecognized_tool_response_shape_fails_open(tmp_path: Path) -> None:
    """A dict tool_response with neither 'content' nor 'text' is left untouched."""
    payload = {"tool_name": "mcp__someserver__weird", "tool_response": {"foo": "bar" * 100_000}}
    proc = _run(payload, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_lemoncrow_tail_feature_keeps_protected_tool_untouched(tmp_path: Path) -> None:
    payload = {"tool_name": "mcp__lc__read", "tool_response": "X" * 300_000}
    proc = _run(payload, tmp_path, env_extra={"LEMONCROW_HEADROOM_MCP_TAIL": "1"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_lemoncrow_tail_feature_skips_short_bash_output(tmp_path: Path) -> None:
    payload = {"tool_name": "mcp__lc__bash", "tool_response": "short result"}
    proc = _run(
        payload,
        tmp_path,
        env_extra={
            "LEMONCROW_HEADROOM_MCP_TAIL": "1",
            "LEMONCROW_HEADROOM_TAIL_MIN_CHARS": "1000",
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_lemoncrow_tail_compresses_only_fresh_tool_result_with_recovery_pointer(tmp_path: Path) -> None:
    fake_site = tmp_path / "fake-headroom"
    (fake_site / "headroom" / "providers").mkdir(parents=True)
    (fake_site / "headroom" / "transforms").mkdir(parents=True)
    (fake_site / "headroom" / "integrations" / "mcp").mkdir(parents=True)
    for package in [
        fake_site / "headroom" / "__init__.py",
        fake_site / "headroom" / "providers" / "__init__.py",
        fake_site / "headroom" / "transforms" / "__init__.py",
        fake_site / "headroom" / "integrations" / "__init__.py",
        fake_site / "headroom" / "integrations" / "mcp" / "__init__.py",
    ]:
        package.write_text("", encoding="utf-8")
    (fake_site / "headroom" / "providers" / "anthropic.py").write_text(
        "class _Counter:\n"
        "    def count_text(self, text): return max(1, len(text)//4)\n"
        "class AnthropicProvider:\n"
        "    def get_token_counter(self, model): return _Counter()\n",
        encoding="utf-8",
    )
    (fake_site / "headroom" / "transforms" / "content_detector.py").write_text(
        "from enum import Enum\n"
        "class ContentType(Enum):\n"
        "    BUILD_OUTPUT='build'\n"
        "    JSON_ARRAY='json_array'\n"
        "class _Detected:\n"
        "    content_type=ContentType.BUILD_OUTPUT; confidence=0.9\n"
        "def detect_content_type(text): return _Detected()\n",
        encoding="utf-8",
    )
    (fake_site / "headroom" / "transforms" / "log_compressor.py").write_text(
        "class LogCompressorConfig:\n"
        "    def __init__(self, **kwargs): self.kwargs=kwargs\n"
        "class _Result:\n"
        "    compressed='COMPRESSED fresh tool tail'\n"
        "class LogCompressor:\n"
        "    def __init__(self, config=None): self.config=config\n"
        "    def compress(self, text, context=''): return _Result()\n",
        encoding="utf-8",
    )

    original = "INFO build output line\n" * 500
    payload = {
        "tool_name": "mcp__lc__bash",
        "tool_input": {"command": "make test"},
        "tool_response": original,
    }
    stats = tmp_path / "headroom-tail-stats.jsonl"
    proc = _run(
        payload,
        tmp_path,
        env_extra={
            "LEMONCROW_HEADROOM_MCP_TAIL_MODE": "apply",
            "LEMONCROW_HEADROOM_TAIL_MIN_CHARS": "100",
            "LEMONCROW_HEADROOM_SITE_PACKAGES": str(fake_site),
            "LEMONCROW_HEADROOM_TAIL_STATS": str(stats),
        },
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    updated = out["hookSpecificOutput"]["updatedToolOutput"]
    assert updated.startswith("COMPRESSED fresh tool tail")
    assert "compacted:headroom-tail" in updated
    spill_path = _spill_path_from_notice(updated)
    assert spill_path.read_text(encoding="utf-8") == original
    stat = json.loads(stats.read_text(encoding="utf-8").strip())
    assert stat["tool"] == "mcp__lc__bash"
    assert stat["strategy"] == "log"
    assert stat["content_type"] == "build"
    assert stat["tokens_before"] > stat["tokens_after"]


def test_lemoncrow_tail_feature_fails_open_without_headroom(tmp_path: Path) -> None:
    payload = {"tool_name": "mcp__lc__bash", "tool_response": "X" * 5000}
    proc = _run(
        payload,
        tmp_path,
        env_extra={
            "LEMONCROW_HEADROOM_MCP_TAIL_MODE": "apply",
            "LEMONCROW_HEADROOM_TAIL_MIN_CHARS": "100",
            "LEMONCROW_HEADROOM_SITE_PACKAGES": str(tmp_path / "missing-headroom"),
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def _write_fake_headroom(tmp_path: Path) -> Path:
    fake_site = tmp_path / "fake-headroom-shared"
    (fake_site / "headroom" / "providers").mkdir(parents=True)
    (fake_site / "headroom" / "transforms").mkdir(parents=True)
    (fake_site / "headroom" / "integrations" / "mcp").mkdir(parents=True)
    for package in [
        fake_site / "headroom" / "__init__.py",
        fake_site / "headroom" / "providers" / "__init__.py",
        fake_site / "headroom" / "transforms" / "__init__.py",
        fake_site / "headroom" / "integrations" / "__init__.py",
        fake_site / "headroom" / "integrations" / "mcp" / "__init__.py",
    ]:
        package.write_text("", encoding="utf-8")
    (fake_site / "headroom" / "providers" / "anthropic.py").write_text(
        "class _Counter:\n"
        "    def count_text(self, text): return max(1, len(text)//4)\n"
        "class AnthropicProvider:\n"
        "    def get_token_counter(self, model): return _Counter()\n",
        encoding="utf-8",
    )
    (fake_site / "headroom" / "transforms" / "content_detector.py").write_text(
        "from enum import Enum\n"
        "class ContentType(Enum):\n"
        "    BUILD_OUTPUT='build'\n"
        "    JSON_ARRAY='json_array'\n"
        "    SEARCH_RESULTS='search'\n"
        "class _Detected:\n"
        "    content_type=ContentType.BUILD_OUTPUT; confidence=0.9\n"
        "def detect_content_type(text): return _Detected()\n",
        encoding="utf-8",
    )
    (fake_site / "headroom" / "transforms" / "log_compressor.py").write_text(
        "class LogCompressorConfig:\n"
        "    def __init__(self, **kwargs): self.kwargs=kwargs\n"
        "class _Result:\n"
        "    compressed='IMPORTANT ERROR retained\\n[repetitive log lines omitted]'\n"
        "class LogCompressor:\n"
        "    def __init__(self, config=None): self.config=config\n"
        "    def compress(self, text, context=''): return _Result()\n",
        encoding="utf-8",
    )
    return fake_site


def test_lemoncrow_web_fetch_is_never_headroom_compressed(tmp_path: Path) -> None:
    payload = {"tool_name": "mcp__lc__web_fetch", "tool_response": "X" * 100_000}
    proc = _run(
        payload,
        tmp_path,
        env_extra={"LEMONCROW_HEADROOM_MCP_TAIL_MODE": "apply"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_lemoncrow_bash_already_compacted_is_shadowed_without_headroom(tmp_path: Path) -> None:
    stats = tmp_path / "stats.jsonl"
    text = ("build line\n" * 1000) + "[lc: compacted 11000→8000; full: /tmp/spill.txt]"
    payload = {
        "tool_name": "mcp__lc__bash",
        "tool_input": {"command": "pytest -vv"},
        "tool_response": text,
    }
    proc = _run(
        payload,
        tmp_path,
        env_extra={
            "LEMONCROW_HEADROOM_MCP_TAIL_MODE": "shadow",
            "LEMONCROW_HEADROOM_TAIL_STATS": str(stats),
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    row = json.loads(stats.read_text(encoding="utf-8").strip())
    assert row["decision"] == "skip_lc_compacted"
    assert row["lc_already_compacted"] is True
    assert row["command_family"] == "pytest"


def test_lemoncrow_bash_shadow_records_worthwhile_candidate_without_rewrite(tmp_path: Path) -> None:
    fake_site = _write_fake_headroom(tmp_path)
    stats = tmp_path / "stats.jsonl"
    payload = {
        "tool_name": "mcp__lc__bash",
        "tool_input": {"command": "docker build ."},
        "tool_response": "INFO repetitive build output\n" * 1000,
    }
    proc = _run(
        payload,
        tmp_path,
        env_extra={
            "LEMONCROW_HEADROOM_MCP_TAIL_MODE": "shadow",
            "LEMONCROW_HEADROOM_SITE_PACKAGES": str(fake_site),
            "LEMONCROW_HEADROOM_TAIL_STATS": str(stats),
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    row = json.loads(stats.read_text(encoding="utf-8").strip())
    assert row["decision"] == "shadow_worthwhile"
    assert row["worthwhile"] is True
    assert row["command_family"] == "docker"
    assert row["tokens_saved"] >= 1000


def test_legacy_headroom_boolean_is_shadow_only(tmp_path: Path) -> None:
    fake_site = _write_fake_headroom(tmp_path)
    stats = tmp_path / "stats.jsonl"
    payload = {
        "tool_name": "mcp__lc__bash",
        "tool_input": {"command": "pytest -vv"},
        "tool_response": "INFO repetitive test output\n" * 1000,
    }
    proc = _run(
        payload,
        tmp_path,
        env_extra={
            "LEMONCROW_HEADROOM_MCP_TAIL": "1",
            "LEMONCROW_HEADROOM_SITE_PACKAGES": str(fake_site),
            "LEMONCROW_HEADROOM_TAIL_STATS": str(stats),
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    row = json.loads(stats.read_text(encoding="utf-8").strip())
    assert row["mode"] == "shadow"


def test_foreign_mcp_prefers_headroom_when_materially_better(tmp_path: Path) -> None:
    fake_site = _write_fake_headroom(tmp_path)
    stats = tmp_path / "stats.jsonl"
    original = "INFO repetitive provider log output\n" * 2000
    payload = {
        "tool_name": "mcp__observability__logs",
        "tool_input": {"query": "errors"},
        "tool_response": original,
    }
    proc = _run(
        payload,
        tmp_path,
        env_extra={
            "LEMONCROW_HEADROOM_MCP_TAIL_MODE": "apply",
            "LEMONCROW_HEADROOM_SITE_PACKAGES": str(fake_site),
            "LEMONCROW_HEADROOM_TAIL_STATS": str(stats),
        },
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    updated = out["hookSpecificOutput"]["updatedToolOutput"]
    assert updated.startswith("IMPORTANT ERROR retained")
    assert "compacted:headroom" in updated
    spill_path = _spill_path_from_notice(updated)
    assert spill_path.read_text(encoding="utf-8") == original
    row = json.loads(stats.read_text(encoding="utf-8").strip())
    assert row["mode"] == "apply"
    assert row["decision"] == "foreign_applied"


def test_foreign_mcp_shadow_never_rewrites_headroom_candidate(tmp_path: Path) -> None:
    fake_site = _write_fake_headroom(tmp_path)
    stats = tmp_path / "foreign-shadow.jsonl"
    original = "INFO repetitive provider log output\n" * 2000
    payload = {
        "tool_name": "mcp__observability__logs",
        "tool_input": {"query": "errors"},
        "tool_response": original,
    }
    proc = _run(
        payload,
        tmp_path,
        env_extra={
            "LEMONCROW_HEADROOM_MCP_TAIL_MODE": "shadow",
            "LEMONCROW_HEADROOM_SITE_PACKAGES": str(fake_site),
            "LEMONCROW_HEADROOM_TAIL_STATS": str(stats),
        },
    )
    assert proc.returncode == 0, proc.stderr
    # Headroom itself is observational in shadow mode; the existing deterministic
    # foreign-MCP shrink may still replace the oversized output afterwards.
    out = json.loads(proc.stdout)
    assert "[lc: shrunk" in out["hookSpecificOutput"]["updatedToolOutput"]
    row = json.loads(stats.read_text(encoding="utf-8").strip())
    assert row["mode"] == "shadow"
    assert row["decision"] == "foreign_shadow_worthwhile"
    assert row["worthwhile"] is True
