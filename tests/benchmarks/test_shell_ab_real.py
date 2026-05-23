"""A/B benchmark: shell tool truncation vs native full shell output."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest

from atelier.gateway.adapters.mcp_server import _reset_runtime_cache_for_testing, tool_shell

pytestmark = pytest.mark.ab


@dataclass
class ABRow:
    tool: str
    mode: str
    native_tool: str
    native_tokens: int
    atelier_tokens: int
    tokens_saved_measured: int
    token_ratio: float | None
    native_ms: float
    atelier_ms: float
    ts: float


def _calibration_path() -> Path:
    path = Path.home() / ".atelier" / "savings_calibration.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append_row(row: ABRow) -> None:
    with _calibration_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(row), sort_keys=True) + "\n")


def _count_tiktoken(text: str) -> int:
    if not text:
        return 0
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text, disallowed_special=()))
    except Exception:
        return len(text) // 4


@pytest.fixture(autouse=True)
def _isolate_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path / ".atelier-runtime"))
    _reset_runtime_cache_for_testing()
    yield
    _reset_runtime_cache_for_testing()


def test_shell_ab_real(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    command = "seq 1 2000"

    t0 = time.perf_counter()
    native_proc = subprocess.run(command, shell=True, check=False, text=True, capture_output=True)
    native_ms = (time.perf_counter() - t0) * 1000.0
    native_text = f"{native_proc.stdout}\n{native_proc.stderr}"

    t1 = time.perf_counter()
    payload = tool_shell({"command": command, "timeout": 30, "cwd": str(tmp_path), "max_lines": 120})
    atelier_ms = (time.perf_counter() - t1) * 1000.0
    atelier_text = json.dumps(payload, sort_keys=True, default=str)

    native_tokens = _count_tiktoken(native_text)
    atelier_tokens = _count_tiktoken(atelier_text)
    row = ABRow(
        tool="shell",
        mode="truncated",
        native_tool="raw_shell_full_output",
        native_tokens=native_tokens,
        atelier_tokens=atelier_tokens,
        tokens_saved_measured=max(0, native_tokens - atelier_tokens),
        token_ratio=(atelier_tokens / native_tokens) if native_tokens else None,
        native_ms=round(native_ms, 3),
        atelier_ms=round(atelier_ms, 3),
        ts=time.time(),
    )
    _append_row(row)

    assert payload["truncated"] is True
    assert atelier_tokens < native_tokens
