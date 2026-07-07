"""A/B benchmark: edit tool response vs manual patch/diff workflow."""

from __future__ import annotations

import difflib
import json
import time
from collections.abc import Generator
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest

from atelier.gateway.adapters.mcp_server import _reset_runtime_cache_for_testing, tool_smart_edit

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
    except (ImportError, ValueError):
        return len(text) // 4


@pytest.fixture(autouse=True)
def _isolate_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path / ".atelier-ws"))
    _reset_runtime_cache_for_testing()
    yield
    _reset_runtime_cache_for_testing()


def test_edit_ab_real(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    target = src / "app.py"
    # A representative module, not a 3-line toy: the edit tool intentionally
    # echoes the unified diff so the caller never re-reads the file, so its
    # fixed response overhead only loses to the before+diff+after baseline on
    # degenerately tiny inputs. A realistic file is what this A/B should size.
    before = (
        "VALUE = 1\n"
        "SCALE = 10\n"
        "OFFSET = 3\n"
        "\n"
        "def run() -> int:\n"
        "    total = 0\n"
        "    for index in range(SCALE):\n"
        "        total += index * VALUE + OFFSET\n"
        "    return total\n"
        "\n"
        "def describe() -> str:\n"
        '    return f"value={VALUE} scale={SCALE} offset={OFFSET}"\n'
        "\n"
        "def reset() -> None:\n"
        "    global VALUE\n"
        "    VALUE = 0\n"
        "\n"
        "def scaled(factor: int) -> int:\n"
        "    return run() * factor + VALUE\n"
    )
    target.write_text(before, encoding="utf-8")
    after = before.replace("VALUE = 1", "VALUE = 2")

    t0 = time.perf_counter()
    native_diff = "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile="a/src/app.py",
            tofile="b/src/app.py",
            lineterm="",
        )
    )
    native_text = f"{before}\n{native_diff}\n{after}"
    native_ms = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    payload = tool_smart_edit(
        {
            "edits": [{"file_path": "src/app.py", "old_string": "VALUE = 1", "new_string": "VALUE = 2"}],
            "atomic": True,
            "post_edit_hooks": False,
        }
    )
    atelier_ms = (time.perf_counter() - t1) * 1000.0
    atelier_text = json.dumps(payload, sort_keys=True, default=str)

    native_tokens = _count_tiktoken(native_text)
    atelier_tokens = _count_tiktoken(atelier_text)
    row = ABRow(
        tool="edit",
        mode="replace",
        native_tool="manual_patch_plus_diff",
        native_tokens=native_tokens,
        atelier_tokens=atelier_tokens,
        tokens_saved_measured=max(0, native_tokens - atelier_tokens),
        token_ratio=(atelier_tokens / native_tokens) if native_tokens else None,
        native_ms=round(native_ms, 3),
        atelier_ms=round(atelier_ms, 3),
        ts=time.time(),
    )
    _append_row(row)

    assert payload.get("failed") in ([], None)
    assert target.read_text(encoding="utf-8").startswith("VALUE = 2")
    assert atelier_tokens < native_tokens
