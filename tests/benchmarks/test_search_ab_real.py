"""Real A/B benchmark: mcp__atelier__search and mcp__atelier__grep vs naive workflows.

This benchmark measures what the MCP ranked-search and grep tools actually
deliver, then persists rows to
``~/.atelier/savings_calibration.jsonl`` for later calibration work.

Marked ``ab`` so it runs under focused local benchmark commands and stays out
of normal CI runs by default.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest

from atelier.gateway.adapters.mcp_server import tool_grep, tool_smart_search

pytestmark = pytest.mark.ab

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


@dataclass
class ABRow:
    """One persisted benchmark measurement."""

    tool: str
    mode: str
    native_tool: str
    query: str | None
    path: str
    native_chars: int
    atelier_chars: int
    native_tokens: int
    atelier_tokens: int
    ratio: float | None
    token_ratio: float | None
    chars_saved: int
    tokens_saved_measured: int
    tokens_saved_reported: int
    native_ms: float
    atelier_ms: float
    cache_hit: bool | None
    backend: str | None
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


def _write_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "frontend").mkdir(parents=True, exist_ok=True)

    (root / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n"
        "\n"
        "def helper() -> OrderService:\n"
        "    return OrderService()\n",
        encoding="utf-8",
    )
    (root / "src" / "checkout.py").write_text(
        "from src.orders import OrderService\n"
        "\n"
        "def checkout(items: list[int]) -> int:\n"
        "    return OrderService().calculate_total(items)\n",
        encoding="utf-8",
    )
    (root / "src" / "payments.py").write_text(
        "from src.checkout import checkout\n"
        "\n"
        "def charge(items: list[int]) -> int:\n"
        "    return checkout(items)\n",
        encoding="utf-8",
    )
    (root / "src" / "ledger.py").write_text(
        "import decimal\n"
        "\n"
        "class Ledger:\n"
        "    def __init__(self) -> None:\n"
        "        self._entries: list[decimal.Decimal] = []\n"
        "\n"
        "    def append(self, value: decimal.Decimal) -> None:\n"
        "        self._entries.append(value)\n"
        "\n"
        "def open_ledger() -> Ledger:\n"
        "    return Ledger()\n"
        "\n"
        "def close_ledger(ledger: Ledger) -> int:\n"
        "    return len(ledger._entries)\n",
        encoding="utf-8",
    )
    (root / "docs" / "release.md").write_text(
        "alpha\n" "NEEDLE_TOKEN appears in release notes\n" "omega\n",
        encoding="utf-8",
    )

    sample_ts = FIXTURE_DIR / "sample.ts"
    if sample_ts.is_file():
        (root / "frontend" / "sample.ts").write_text(
            sample_ts.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    else:
        (root / "frontend" / "sample.ts").write_text(
            "export function issueAccessToken(userId: string): string {\n" "  return `session:${userId}`;\n" "}\n",
            encoding="utf-8",
        )


def _configure_workspace(monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(repo_root))
    monkeypatch.setenv("ATELIER_ROOT", str(repo_root / ".atelier-store"))
    monkeypatch.chdir(repo_root)


def _flatten_smart_payload(payload: dict[str, object]) -> str:
    mode = str(payload.get("mode") or "")
    if mode == "map":
        map_parts = [str(payload.get("outline") or "")]
        ranked_files = payload.get("ranked_files")
        if isinstance(ranked_files, list):
            map_parts.extend(str(item) for item in ranked_files if item)
        return "\n".join(part for part in map_parts if part)

    parts: list[str] = []
    matches = payload.get("matches")
    if not isinstance(matches, list):
        return ""
    for match in matches:
        if not isinstance(match, dict):
            continue
        parts.append(str(match.get("path") or ""))
        if match.get("content"):
            parts.append(str(match["content"]))
        for snippet in match.get("snippets", []):
            if isinstance(snippet, dict):
                parts.append(str(snippet.get("text") or ""))
        if match.get("outline"):
            parts.append(json.dumps(match["outline"], sort_keys=True, default=str))
    return "\n".join(part for part in parts if part)


def _flatten_native_payload(payload: dict[str, object]) -> str:
    parts: list[str] = []
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
        elif item.get("type") == "image":
            parts.append(f"[image:{item.get('mimeType', 'application/octet-stream')}]")
    return "\n".join(part for part in parts if part)


def _persist_row(
    *,
    tool: str,
    mode: str,
    native_tool: str,
    query: str | None,
    path: str,
    native_text: str,
    atelier_text: str,
    native_ms: float,
    atelier_ms: float,
    tokens_saved_reported: int = 0,
    cache_hit: bool | None = None,
    backend: str | None = None,
) -> ABRow:
    native_chars = len(native_text)
    atelier_chars = len(atelier_text)
    native_tokens = _count_tiktoken(native_text)
    atelier_tokens = _count_tiktoken(atelier_text)
    row = ABRow(
        tool=tool,
        mode=mode,
        native_tool=native_tool,
        query=query,
        path=path,
        native_chars=native_chars,
        atelier_chars=atelier_chars,
        native_tokens=native_tokens,
        atelier_tokens=atelier_tokens,
        ratio=(atelier_chars / native_chars) if native_chars else None,
        token_ratio=(atelier_tokens / native_tokens) if native_tokens else None,
        chars_saved=native_chars - atelier_chars,
        tokens_saved_measured=max(0, native_tokens - atelier_tokens),
        tokens_saved_reported=tokens_saved_reported,
        native_ms=round(native_ms, 3),
        atelier_ms=round(atelier_ms, 3),
        cache_hit=cache_hit,
        backend=backend,
        ts=time.time(),
    )
    _append_row(row)
    return row


def _text_candidates(base: Path) -> list[Path]:
    allowed = {".py", ".md", ".ts", ".tsx", ".js", ".jsx", ".txt"}
    return sorted(path for path in base.rglob("*") if path.is_file() and path.suffix.lower() in allowed)


def _glob_candidates(base: Path, patterns: list[str]) -> list[Path]:
    found: dict[Path, None] = {}
    for pattern in patterns:
        for path in base.glob(pattern):
            if path.is_file():
                found[path.resolve()] = None
    return sorted(found)


def _render_windows(lines: list[str], hit_lines: list[int], before: int, after: int) -> list[str]:
    rendered: list[str] = []
    seen: set[tuple[int, int]] = set()
    for line_no in hit_lines:
        start = max(1, line_no - before)
        end = min(len(lines), line_no + after)
        if (start, end) in seen:
            continue
        seen.add((start, end))
        rendered.append(f"@@ {start}-{end}")
        rendered.extend(lines[start - 1 : end])
    return rendered


def _baseline_smart_chunks(repo_root: Path, query: str, max_files: int) -> str:
    parts: list[str] = []
    needle = query.lower()
    files_seen = 0
    for file_path in _text_candidates(repo_root / "src"):
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        hit_lines = [idx for idx, line in enumerate(lines, start=1) if needle in line.lower()]
        if not hit_lines:
            continue
        parts.append(str(file_path.relative_to(repo_root)))
        parts.extend(_render_windows(lines, hit_lines[:1], before=1, after=1))
        files_seen += 1
        if files_seen >= max_files:
            break
    return "\n".join(parts)


def _baseline_repo_map(repo_root: Path) -> str:
    parts: list[str] = []
    for file_path in sorted((repo_root / "src").glob("**/*.py")):
        parts.append(str(file_path.relative_to(repo_root)))
        parts.append(file_path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def _baseline_regex(repo_root: Path, pattern: str, globs: list[str], *, before: int = 0, after: int = 0) -> str:
    compiled = re.compile(pattern)
    parts: list[str] = []
    for file_path in _glob_candidates(repo_root / "docs", globs):
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        hit_lines = [idx for idx, line in enumerate(lines, start=1) if compiled.search(line)]
        if not hit_lines:
            continue
        parts.append(str(file_path.relative_to(repo_root)))
        parts.extend(_render_windows(lines, hit_lines, before=before, after=after))
    return "\n".join(parts)


def _baseline_glob_paths(repo_root: Path, path: str, globs: list[str]) -> str:
    base = repo_root / path
    return "\n".join(str(file_path.relative_to(repo_root)) for file_path in _glob_candidates(base, globs))


def _baseline_full_glob_read(repo_root: Path, path: str, globs: list[str]) -> str:
    base = repo_root / path
    parts: list[str] = []
    for file_path in _glob_candidates(base, globs):
        parts.append(str(file_path.relative_to(repo_root)))
        parts.append(file_path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


@pytest.mark.parametrize(
    ("tool_name", "mode", "native_tool", "baseline_builder"),
    [
        ("search.smart_chunks", "chunks", "grep_plus_snippets", _baseline_smart_chunks),
        (
            "search.smart_map",
            "map",
            "read_all_source_files",
            lambda repo_root, _query, _max_files: _baseline_repo_map(repo_root),
        ),
    ],
)
def test_search_ab_smart_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    mode: str,
    native_tool: str,
    baseline_builder: Callable[[Path, str | None, int], str],
) -> None:
    _write_fixture_repo(tmp_path)
    _configure_workspace(monkeypatch, tmp_path)

    query = "OrderService"
    tool_args: dict[str, object] = {"query": query, "path": "src", "budget_tokens": 4000}
    if mode != "chunks":
        tool_args["mode"] = mode
    if mode == "map":
        tool_args["seed_files"] = ["src/payments.py"]

    t0 = time.perf_counter()
    native_text = baseline_builder(tmp_path, query, 3)
    native_ms = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    payload = tool_smart_search(tool_args)
    atelier_ms = (time.perf_counter() - t1) * 1000.0
    atelier_text = _flatten_smart_payload(payload)

    row = _persist_row(
        tool=tool_name,
        mode=mode,
        native_tool=native_tool,
        query=query,
        path="src",
        native_text=native_text,
        atelier_text=atelier_text,
        native_ms=native_ms,
        atelier_ms=atelier_ms,
        tokens_saved_reported=int(payload.get("tokens_saved_vs_naive", 0) or 0),
        cache_hit=bool(payload.get("cache_hit")) if "cache_hit" in payload else None,
        backend=str(payload.get("backend")) if payload.get("backend") else None,
    )

    assert atelier_text, f"{mode}: atelier payload was empty"
    assert payload["mode"] == mode
    if mode == "map":
        assert "OrderService" in atelier_text
    else:
        assert payload.get("matches"), f"{mode}: expected at least one match"
    assert row.atelier_tokens > 0


def test_search_ab_native_regex_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_fixture_repo(tmp_path)
    _configure_workspace(monkeypatch, tmp_path)

    tool_args = {
        "path": "docs",
        "content_regex": "NEEDLE_TOKEN",
        "file_glob_patterns": ["**/*.md"],
        "output_mode": "file_paths_with_content",
        "include_meta": True,
    }

    t0 = time.perf_counter()
    native_text = _baseline_regex(tmp_path, "NEEDLE_TOKEN", ["**/*.md"])
    native_ms = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    payload = tool_grep(tool_args)
    atelier_ms = (time.perf_counter() - t1) * 1000.0
    atelier_text = _flatten_native_payload(payload)

    row = _persist_row(
        tool="grep.native_regex",
        mode="native_regex",
        native_tool="python_regex_scan",
        query="NEEDLE_TOKEN",
        path="docs",
        native_text=native_text,
        atelier_text=atelier_text,
        native_ms=native_ms,
        atelier_ms=atelier_ms,
    )

    assert payload["_meta"]["fileMatchCount"] == 1
    assert "NEEDLE_TOKEN" in atelier_text
    assert row.atelier_tokens > 0


def test_search_ab_native_glob_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_fixture_repo(tmp_path)
    _configure_workspace(monkeypatch, tmp_path)

    globs = ["**/*.py", "**/*.ts"]
    tool_args = {
        "path": ".",
        "file_glob_patterns": globs,
        "output_mode": "file_paths_only",
        "include_meta": True,
    }

    t0 = time.perf_counter()
    native_text = _baseline_glob_paths(tmp_path, ".", globs)
    native_ms = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    payload = tool_grep(tool_args)
    atelier_ms = (time.perf_counter() - t1) * 1000.0
    atelier_text = _flatten_native_payload(payload)

    row = _persist_row(
        tool="grep.native_glob",
        mode="native_glob",
        native_tool="python_glob",
        query=None,
        path=".",
        native_text=native_text,
        atelier_text=atelier_text,
        native_ms=native_ms,
        atelier_ms=atelier_ms,
    )

    assert payload["_meta"]["fileMatchCount"] >= 4
    assert "src/orders.py" in atelier_text
    assert "frontend/sample.ts" in atelier_text
    assert row.atelier_tokens > 0


def test_search_ab_native_context_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_fixture_repo(tmp_path)
    _configure_workspace(monkeypatch, tmp_path)

    tool_args = {
        "path": "docs",
        "content_regex": "NEEDLE_TOKEN",
        "file_glob_patterns": ["**/*.md"],
        "output_mode": "file_paths_with_content",
        "lines_before": 1,
        "lines_after": 1,
    }

    t0 = time.perf_counter()
    native_text = _baseline_regex(tmp_path, "NEEDLE_TOKEN", ["**/*.md"], before=1, after=1)
    native_ms = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    payload = tool_grep(tool_args)
    atelier_ms = (time.perf_counter() - t1) * 1000.0
    atelier_text = _flatten_native_payload(payload)

    row = _persist_row(
        tool="grep.native_context",
        mode="native_context",
        native_tool="python_regex_scan_with_context",
        query="NEEDLE_TOKEN",
        path="docs",
        native_text=native_text,
        atelier_text=atelier_text,
        native_ms=native_ms,
        atelier_ms=atelier_ms,
    )

    assert "alpha" in atelier_text
    assert "omega" in atelier_text
    assert row.atelier_tokens > 0


def test_search_ab_cache_hit_second_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_fixture_repo(tmp_path)
    _configure_workspace(monkeypatch, tmp_path)

    tool_args = {"query": "OrderService", "path": "src", "budget_tokens": 4000, "include_meta": True}
    native_text = _baseline_smart_chunks(tmp_path, "OrderService", 3)
    native_ms = 0.0

    t0 = time.perf_counter()
    first = tool_smart_search(tool_args)
    first_ms = (time.perf_counter() - t0) * 1000.0
    t1 = time.perf_counter()
    second = tool_smart_search(tool_args)
    second_ms = (time.perf_counter() - t1) * 1000.0

    first_text = _flatten_smart_payload(first)
    second_text = _flatten_smart_payload(second)

    _persist_row(
        tool="search.smart_chunks_cache_miss",
        mode="chunks",
        native_tool="grep_plus_snippets",
        query="OrderService",
        path="src",
        native_text=native_text,
        atelier_text=first_text,
        native_ms=native_ms,
        atelier_ms=first_ms,
        tokens_saved_reported=int(first.get("tokens_saved_vs_naive", 0) or 0),
        cache_hit=bool(first.get("cache_hit")),
        backend=str(first.get("backend")) if first.get("backend") else None,
    )
    _persist_row(
        tool="search.smart_chunks_cache_hit",
        mode="chunks",
        native_tool="grep_plus_snippets",
        query="OrderService",
        path="src",
        native_text=native_text,
        atelier_text=second_text,
        native_ms=native_ms,
        atelier_ms=second_ms,
        tokens_saved_reported=int(second.get("tokens_saved_vs_naive", 0) or 0),
        cache_hit=bool(second.get("cache_hit")),
        backend=str(second.get("backend")) if second.get("backend") else None,
    )

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert first_text == second_text


def test_search_ab_summary_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_fixture_repo(tmp_path)
    _configure_workspace(monkeypatch, tmp_path)

    globs = ["**/*.py", "**/*.ts"]
    tool_args = {
        "path": ".",
        "file_glob_patterns": globs,
        "summary": True,
        "output_mode": "file_paths_with_content",
    }

    t0 = time.perf_counter()
    native_text = _baseline_full_glob_read(tmp_path, ".", globs)
    native_ms = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    payload = tool_grep(tool_args)
    atelier_ms = (time.perf_counter() - t1) * 1000.0
    atelier_text = _flatten_native_payload(payload)

    row = _persist_row(
        tool="grep.native_summary",
        mode="native_summary",
        native_tool="full_glob_read",
        query=None,
        path=".",
        native_text=native_text,
        atelier_text=atelier_text,
        native_ms=native_ms,
        atelier_ms=atelier_ms,
    )

    assert "ClassDef: OrderService" in atelier_text
    assert "FunctionDef: checkout" in atelier_text
    assert row.atelier_tokens < row.native_tokens


def test_search_calibration_file_grows() -> None:
    path = _calibration_path()
    assert path.exists(), f"no calibration file at {path}"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    benchmark_rows = [row for row in rows if str(row.get("tool", "")).startswith(("search.", "grep."))]
    assert len(benchmark_rows) >= 9, f"expected >= 9 search/grep rows, found {len(benchmark_rows)}"
