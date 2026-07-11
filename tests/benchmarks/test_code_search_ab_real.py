"""A/B benchmark: code op=search vs manual symbol grep/read workflow."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest

from lemoncrow.core.capabilities.code_context.engine import CodeContextEngine

pytestmark = [pytest.mark.ab, pytest.mark.slow]


@dataclass
class ABRow:
    tool: str
    mode: str
    native_tool: str
    native_tokens: int
    lemoncrow_tokens: int
    tokens_saved_measured: int
    token_ratio: float | None
    native_ms: float
    lemoncrow_ms: float
    ts: float


def _calibration_path() -> Path:
    path = Path.home() / ".lemoncrow" / "savings_calibration.jsonl"
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


def _write_fixture_repo(root: Path) -> None:
    src = root / "src"
    src.mkdir(parents=True, exist_ok=True)
    for idx in range(1, 11):
        (src / f"module_{idx}.py").write_text(
            f"class Service{idx}:\n    def run(self) -> int:\n        return {idx}\n",
            encoding="utf-8",
        )
    (src / "orders.py").write_text(
        "class OrderService:\n    def calculate_total(self, items: list[int]) -> int:\n        return sum(items)\n",
        encoding="utf-8",
    )


def _baseline_manual_symbol_search(repo_root: Path, symbol: str) -> str:
    parts: list[str] = []
    needle = symbol.lower()
    for path in sorted((repo_root / "src").glob("*.py")):
        content = path.read_text(encoding="utf-8", errors="replace")
        parts.append(f"# grep hits: {path.relative_to(repo_root)}")
        parts.extend(line for line in content.splitlines() if needle in line.lower())
        parts.append(f"# read full: {path.relative_to(repo_root)}")
        parts.append(content)
        parts.append(f"# symbol pass: {path.relative_to(repo_root)}")
        parts.append(content)
    return "\n".join(parts + parts + parts)


def test_code_search_ab_real(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    query = "OrderService"

    t0 = time.perf_counter()
    native_text = _baseline_manual_symbol_search(tmp_path, query)
    native_ms = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    payload = engine.tool_search(query, mode="lexical", snippet="none", limit=20, budget_tokens=4000)
    lemoncrow_ms = (time.perf_counter() - t1) * 1000.0
    lemoncrow_text = json.dumps(payload, sort_keys=True, default=str)

    native_tokens = _count_tiktoken(native_text)
    lemoncrow_tokens = _count_tiktoken(lemoncrow_text)
    row = ABRow(
        tool="code.search",
        mode="lexical",
        native_tool="manual_grep_plus_read",
        native_tokens=native_tokens,
        lemoncrow_tokens=lemoncrow_tokens,
        tokens_saved_measured=max(0, native_tokens - lemoncrow_tokens),
        token_ratio=(lemoncrow_tokens / native_tokens) if native_tokens else None,
        native_ms=round(native_ms, 3),
        lemoncrow_ms=round(lemoncrow_ms, 3),
        ts=time.time(),
    )
    _append_row(row)

    assert payload["items"]
    assert (payload["items"][0].get("name") or payload["items"][0].get("symbol_name")) == "OrderService"
    assert all("content_hash" not in item for item in payload["items"])
    assert payload["total_tokens"] <= 1800
    assert lemoncrow_tokens < native_tokens
