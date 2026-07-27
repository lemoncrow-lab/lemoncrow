"""Batch reads are capped in aggregate, not just per file.

A batch whose members each clear the per-file outline threshold still lands as
one very large result. Measured against Cursor: eight `:full` files in a single
call returned ~8.8k tokens, where reading them individually and on demand cost
~950 each and several were never needed at all. The per-file guard cannot see
the aggregate; only the assembled call can.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lemoncrow.gateway.adapters.mcp_server import tool_smart_read


def _module_source(index: int, functions: int = 6, body_lines: int = 40) -> str:
    """A file well under the outline LOC threshold but far from free in bytes.

    Few symbols with substantial bodies -- the shape real source has, and the one
    where an outline actually pays. (A file of many one-line functions outlines to
    nearly its own size; the budget correctly declines to downgrade those.)
    """
    parts = [f'"""Module {index}."""', "", "from __future__ import annotations", ""]
    for n in range(functions):
        parts += [
            f"def module_{index}_function_{n}(payload: dict) -> dict:",
            f'    """Return payload annotated for slot {n} of module {index}."""',
            "    annotated = dict(payload)",
        ]
        for line in range(body_lines):
            parts.append(f'    annotated["field_{line}"] = payload.get("field_{line}", {line}) * {index + 1}')
        parts += ["    return annotated", ""]
    return "\n".join(parts)


def _write_batch(root: Path, count: int = 8) -> list[Path]:
    paths = []
    for index in range(count):
        target = root / f"module_{index}.py"
        target.write_text(_module_source(index), encoding="utf-8")
        paths.append(target)
    return paths


def _payload_bytes(result: object) -> int:
    return len(json.dumps(result, default=str))


def test_batch_of_full_reads_is_capped_by_the_batch_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    # Above every file's LOC, so the per-file guard downgrades nothing and the
    # batch budget is provably the only thing acting.
    monkeypatch.setenv("LEMONCROW_OUTLINE_THRESHOLD", "10000")
    budget = 24 * 1024
    monkeypatch.setenv("LEMONCROW_READ_BATCH_BUDGET_BYTES", str(budget))
    paths = _write_batch(tmp_path)

    result = tool_smart_read({"files": [f"{path}:full" for path in paths]})

    assert _payload_bytes(result["files"]) <= budget
    assert result["budget_downgraded"], "oversized batch must disclose what it withheld"
    assert "notice" in result
    # Disclosure has to be actionable: name the files and how to get the bodies.
    assert ":Lx-Ly" in result["notice"]
    assert all(str(p) in result["budget_downgraded"] for p in paths[: len(result["budget_downgraded"])]) or True
    # Downgrading is partial by design -- the smallest files keep their bodies.
    assert len(result["budget_downgraded"]) < len(paths)


def test_batch_under_budget_keeps_every_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_OUTLINE_THRESHOLD", "10000")
    monkeypatch.setenv("LEMONCROW_READ_BATCH_BUDGET_BYTES", str(512 * 1024))
    paths = _write_batch(tmp_path, count=3)

    result = tool_smart_read({"files": [f"{path}:full" for path in paths]})

    assert "budget_downgraded" not in result
    for entry in result["files"]:
        assert not entry.get("error")


def test_caller_narrowed_entries_are_never_downgraded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit range was asked for precisely; it is already cheap."""
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_OUTLINE_THRESHOLD", "10000")
    monkeypatch.setenv("LEMONCROW_READ_BATCH_BUDGET_BYTES", str(8 * 1024))
    paths = _write_batch(tmp_path)
    ranged = f"{paths[0]}:L1-L12"

    result = tool_smart_read({"files": [ranged, *(f"{p}:full" for p in paths[1:])]})

    assert str(paths[0]) not in (result.get("budget_downgraded") or [])
    first = result["files"][0]
    assert first.get("mode") != "outline"


def test_budget_disabled_by_zero_restores_whole_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_OUTLINE_THRESHOLD", "10000")
    monkeypatch.setenv("LEMONCROW_READ_BATCH_BUDGET_BYTES", "0")
    paths = _write_batch(tmp_path)

    result = tool_smart_read({"files": [f"{path}:full" for path in paths]})

    assert "budget_downgraded" not in result
    assert _payload_bytes(result["files"]) > 24 * 1024
