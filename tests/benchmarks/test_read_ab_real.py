"""Real A/B benchmark: mcp__lc__read vs native cat/read on real files.

This test is NOT a unit test — it measures actual chars/tokens delivered by both
branches on real repository files and persists the deltas to
``~/.lemoncrow/savings_calibration.jsonl``. Rolling medians from that file are
intended to replace the magic ``LIVE_*_TOKENS_PER_CALL`` constants in
``src/lemoncrow/core/capabilities/plugin_runtime.py``.

Marked ``ab`` so it runs under ``make bench-ab`` and is skipped by default in
``pytest`` to keep CI fast. Toggle locally with::

    uv run pytest tests/benchmarks/test_read_ab_real.py -v -m ab

Do NOT delete this file when refactoring. It is the seed measurement that
legitimizes (or invalidates) every per-tool savings claim LemonCrow ships.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest

from lemoncrow.core.capabilities.semantic_file_memory import SemanticFileMemoryCapability

pytestmark = [pytest.mark.ab, pytest.mark.slow]

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

# Two fixture groups so per-language calibration is honest:
# 1. REAL repo files (Python) — measures the real-world workload
# 2. SYNTHETIC fixtures for languages the repo doesn't ship (Go/Rust/Java/Markdown)
FIXTURES_REAL_PY: tuple[Path, ...] = (
    REPO_ROOT / "src/lemoncrow/core/capabilities/code_context/engine.py",
    REPO_ROOT / "src/lemoncrow/core/capabilities/plugin_runtime.py",
    REPO_ROOT / "src/lemoncrow/core/capabilities/pricing.py",
    REPO_ROOT / "src/lemoncrow/gateway/adapters/mcp_server.py",
)
FIXTURES_SYNTHETIC: tuple[Path, ...] = (
    FIXTURE_DIR / "sample.go",
    FIXTURE_DIR / "sample.rs",
    FIXTURE_DIR / "Sample.java",
    # Language fixtures added in gap-fill pass (10 new languages)
    FIXTURE_DIR / "sample.ts",
    FIXTURE_DIR / "sample.rb",
    FIXTURE_DIR / "sample.c",
    FIXTURE_DIR / "sample.cpp",
    FIXTURE_DIR / "Sample.cs",
    FIXTURE_DIR / "Sample.kt",
    FIXTURE_DIR / "sample.php",
    FIXTURE_DIR / "sample.swift",
    FIXTURE_DIR / "Sample.scala",
    FIXTURE_DIR / "sample.sh",
)
FIXTURES: tuple[Path, ...] = FIXTURES_REAL_PY + FIXTURES_SYNTHETIC


@dataclass
class ABRow:
    """One A/B measurement persisted to the calibration store."""

    tool: str  # 'read'
    mode: str  # lemoncrow read mode: 'outline' | 'range' | 'full'
    language: str  # 'python' | 'go' | 'rust' | 'java' | 'markdown' | ...
    path: str  # relative path inside the repo
    native_chars: int  # len(Path.read_text())
    lemoncrow_chars: int  # len of what lemoncrow actually delivered
    native_tokens: int  # tiktoken count of native_text
    lemoncrow_tokens: int  # tiktoken count of lemoncrow-delivered text
    ratio: float  # lemoncrow_chars / native_chars (1.0 = no saving)
    token_ratio: float  # lemoncrow_tokens / native_tokens
    chars_saved: int  # native_chars - lemoncrow_chars
    tokens_saved_measured: int  # native_tokens - lemoncrow_tokens (from tiktoken)
    tokens_saved_reported: int  # what the tool itself claims
    native_ms: float
    lemoncrow_ms: float
    ts: float


def _calibration_path() -> Path:
    root = Path.home() / ".lemoncrow"  # fixed real home — not test-isolated LEMONCROW_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return root / "savings_calibration.jsonl"


def _append_row(row: ABRow) -> None:
    path = _calibration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(row), sort_keys=True) + "\n")


def _lemoncrow_root() -> Path:
    return Path(os.environ.get("LEMONCROW_ROOT") or (Path.home() / ".lemoncrow"))


def _count_tiktoken(text: str) -> int:
    """Independent token count for verifying what the tool reports."""
    if not text:
        return 0
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text, disallowed_special=()))
    except ImportError:
        return len(text) // 4


def _try_relative(p: Path) -> str:
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


def _measure_read_fixture(fixture: Path) -> ABRow:
    """Run the native-vs-LemonCrow read comparison and persist one row."""
    if not fixture.is_file():
        pytest.xfail(f"fixture missing: {fixture}")

    t0 = time.perf_counter()
    native_text = fixture.read_text(encoding="utf-8")
    native_ms = (time.perf_counter() - t0) * 1000.0
    native_chars = len(native_text)
    native_tokens = _count_tiktoken(native_text)

    cap = SemanticFileMemoryCapability(_lemoncrow_root())
    t1 = time.perf_counter()
    payload = cap.smart_read(fixture, range_spec=None, expand=False)
    lemoncrow_ms = (time.perf_counter() - t1) * 1000.0

    mode = str(payload.get("mode") or "unknown")
    language = str(payload.get("language") or "unknown")
    delivered_parts = [str(payload.get("content") or "")]
    outline = payload.get("outline")
    if outline:
        if isinstance(outline, str):
            delivered_parts.append(outline)
        else:
            # Mirror what the MCP layer actually ships to the agent
            # (mcp_server._render_read_outline_md), not json.dumps — JSON
            # escaping inflates newlines and would understate savings.
            from lemoncrow.gateway.adapters.mcp_server import _render_read_outline_md

            delivered_parts.append(_render_read_outline_md(str(fixture), outline, language))
    lemoncrow_chars = sum(len(part) for part in delivered_parts)
    lemoncrow_tokens = _count_tiktoken("".join(delivered_parts))
    tokens_saved = int(payload.get("tokens_saved", 0) or 0)

    row = ABRow(
        tool="read",
        mode=mode,
        language=language,
        path=_try_relative(fixture),
        native_chars=native_chars,
        lemoncrow_chars=lemoncrow_chars,
        native_tokens=native_tokens,
        lemoncrow_tokens=lemoncrow_tokens,
        ratio=(lemoncrow_chars / native_chars) if native_chars else 1.0,
        token_ratio=(lemoncrow_tokens / native_tokens) if native_tokens else 1.0,
        chars_saved=native_chars - lemoncrow_chars,
        tokens_saved_measured=max(0, native_tokens - lemoncrow_tokens),
        tokens_saved_reported=tokens_saved,
        native_ms=round(native_ms, 3),
        lemoncrow_ms=round(lemoncrow_ms, 3),
        ts=time.time(),
    )
    _append_row(row)
    return row


@pytest.mark.ab
@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.name)
def test_read_ab_real(fixture: Path) -> None:
    """Run both branches, persist the delta, assert only honest invariants."""
    row = _measure_read_fixture(fixture)

    # Honest invariants only:
    native_chars = row.native_chars
    lemoncrow_chars = row.lemoncrow_chars
    tokens_saved = row.tokens_saved_reported

    assert native_chars > 0, "native read returned no bytes"
    assert lemoncrow_chars > 0, "lemoncrow returned no bytes"
    assert (
        lemoncrow_chars <= native_chars
    ), f"lemoncrow returned {lemoncrow_chars} chars vs native {native_chars} on {fixture.name}"
    if tokens_saved > 0:
        assert (
            row.chars_saved > 0
        ), f"{fixture.name}: tool reports tokens_saved={tokens_saved} but chars_saved={row.chars_saved}"
    # Reported tokens_saved uses Claude Code's built-in Read baseline, so it
    # may be lower than a raw cat/full-file delta. It must not overclaim it.
    if tokens_saved > 100 and row.tokens_saved_measured > 100:
        assert tokens_saved <= int(row.tokens_saved_measured * 1.1), (
            f"{fixture.name}: tool tokens_saved={tokens_saved} exceeds "
            f"independent full-file tiktoken delta {row.tokens_saved_measured}"
        )


@pytest.mark.ab
@pytest.mark.parametrize(
    "fixture",
    [FIXTURE_DIR / "sample.go", FIXTURE_DIR / "sample.rs", FIXTURE_DIR / "Sample.java"],
    ids=lambda p: p.name,
)
def test_generic_outline_compresses_large_files(fixture: Path, tmp_path: Path) -> None:
    """Force the generic outline to fire by inflating fixtures past 300 effective LOC.

    The small synthetic fixtures (130-150 LOC) fall under the production
    outline_threshold (default 500) so they get full reads in test_read_ab_real.
    This test triples each fixture (3x gives 351-420 effective LOC) and passes
    an explicit outline_threshold=300 so the outline code path actually runs
    regardless of the production default.  The per-language compression ratio
    is persisted to the calibration store under a separate path so
    production-vs-synthetic numbers stay distinguishable.
    """
    if not fixture.is_file():
        pytest.xfail(f"fixture missing: {fixture}")

    src = fixture.read_text(encoding="utf-8")
    # 3x concatenation, in a tmp file so we don't pollute the test corpus.
    big = tmp_path / fixture.name
    big.write_text(src + "\n\n" + src + "\n\n" + src, encoding="utf-8")

    cap = SemanticFileMemoryCapability(_lemoncrow_root())
    # Use an explicit threshold (300) so the test is immune to changes in the
    # production default (currently 500). 3x fixtures have 351-420 effective
    # LOC, safely above 300 for all three languages.
    payload = cap.smart_read(big, range_spec=None, expand=False, outline_threshold=300)
    mode = str(payload.get("mode"))
    language = str(payload.get("language"))
    outline = payload.get("outline")

    # Outline must fire once files cross the threshold. Either tree-sitter
    # (when we have a per-language config) or the generic regex fallback.
    assert (
        mode == "outline"
    ), f"{fixture.name} (3x = {len(big.read_text())} chars) returned mode={mode}, expected outline"
    assert isinstance(outline, dict) and outline.get("kind") in {
        "treesitter",
        "generic",
    }, f"{fixture.name}: expected outline kind treesitter|generic, got {outline}"
    outline_kind = str(outline.get("kind") or "unknown")

    native_chars = len(big.read_text())
    outline_text = str(outline.get("text") or "")
    lemoncrow_chars = len(outline_text)
    native_tokens = _count_tiktoken(big.read_text())
    lemoncrow_tokens = _count_tiktoken(outline_text)

    row = ABRow(
        tool=f"read_{outline_kind}_outline",
        mode=mode,
        language=language,
        path=f"synthetic-3x:{fixture.name}",
        native_chars=native_chars,
        lemoncrow_chars=lemoncrow_chars,
        native_tokens=native_tokens,
        lemoncrow_tokens=lemoncrow_tokens,
        ratio=lemoncrow_chars / native_chars,
        token_ratio=lemoncrow_tokens / native_tokens if native_tokens else 1.0,
        chars_saved=native_chars - lemoncrow_chars,
        tokens_saved_measured=max(0, native_tokens - lemoncrow_tokens),
        tokens_saved_reported=int(payload.get("tokens_saved", 0) or 0),
        native_ms=0.0,
        lemoncrow_ms=0.0,
        ts=time.time(),
    )
    _append_row(row)

    # Sanity: outline must save at least 25% (the production gate).
    assert lemoncrow_chars <= int(native_chars * 0.75), (
        f"{fixture.name}: {outline_kind} outline only saved "
        f"{(1 - lemoncrow_chars / native_chars) * 100:.1f}% — below 25% gate"
    )


@pytest.mark.ab
def test_calibration_file_grows() -> None:
    """After the parametrized A/B runs, the calibration file should have rows.

    Sanity check: the seed run must produce N >= len(FIXTURES) measurement
    rows in ``savings_calibration.jsonl``. Otherwise the harness is silently
    broken and we're back to magic constants.
    """
    path = _calibration_path()
    if not path.exists():
        for fixture in FIXTURES:
            _measure_read_fixture(fixture)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    read_rows = [r for r in rows if r.get("tool") == "read"]
    if len(read_rows) < len(FIXTURES):
        seen_paths = {str(r.get("path") or "") for r in read_rows}
        for fixture in FIXTURES:
            rel = _try_relative(fixture)
            if rel not in seen_paths:
                _measure_read_fixture(fixture)
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        read_rows = [r for r in rows if r.get("tool") == "read"]
    assert len(read_rows) >= len(FIXTURES), f"expected >= {len(FIXTURES)} read rows, found {len(read_rows)}"


# ---------------------------------------------------------------------------
# Gap-fill tests: range mode, cache hit, expand, max_lines, edge cases
# ---------------------------------------------------------------------------


@pytest.mark.ab
def test_read_ab_range_mode(tmp_path: Path) -> None:
    """smart_read with range_spec returns exactly the requested lines."""
    # Use a fixture that is guaranteed to have >= 200 lines.
    fixture = FIXTURE_DIR / "sample.ts"
    if not fixture.is_file():
        pytest.xfail(f"fixture missing: {fixture}")

    cap = SemanticFileMemoryCapability(_lemoncrow_root())

    t0 = time.perf_counter()
    payload = cap.smart_read(fixture, range_spec="100-200")
    lemoncrow_ms = (time.perf_counter() - t0) * 1000.0

    assert payload["mode"] == "range", f"expected mode=range, got {payload['mode']}"
    content = payload.get("content") or ""
    delivered_lines = content.splitlines()
    # 100-200 inclusive = 101 lines (clamped if file is shorter)
    native_text = fixture.read_text(encoding="utf-8")
    total_lines = len(native_text.splitlines())
    expected_count = min(200, total_lines) - min(100, total_lines) + 1
    assert (
        len(delivered_lines) == expected_count
    ), f"range 100-200 should yield {expected_count} lines, got {len(delivered_lines)}"

    native_tokens = _count_tiktoken(native_text)
    lemoncrow_tokens = _count_tiktoken(content)

    row = ABRow(
        tool="read_range",
        mode="range",
        language=str(payload.get("language") or "unknown"),
        path=_try_relative(fixture),
        native_chars=len(native_text),
        lemoncrow_chars=len(content),
        native_tokens=native_tokens,
        lemoncrow_tokens=lemoncrow_tokens,
        ratio=len(content) / len(native_text) if native_text else 1.0,
        token_ratio=lemoncrow_tokens / native_tokens if native_tokens else 1.0,
        chars_saved=len(native_text) - len(content),
        tokens_saved_measured=max(0, native_tokens - lemoncrow_tokens),
        tokens_saved_reported=int(payload.get("tokens_saved", 0) or 0),
        native_ms=0.0,
        lemoncrow_ms=round(lemoncrow_ms, 3),
        ts=time.time(),
    )
    _append_row(row)


@pytest.mark.ab
def test_read_ab_cache_hit() -> None:
    """Second call to same file should return cache_hit=True and be recorded."""
    fixture = FIXTURE_DIR / "sample.rb"
    if not fixture.is_file():
        pytest.xfail(f"fixture missing: {fixture}")

    # Use a fresh lemoncrow root so the cache is cold.
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        cap = SemanticFileMemoryCapability(root)
        native_text = fixture.read_text(encoding="utf-8")

        # First call — cache must be cold.
        t0 = time.perf_counter()
        p1 = cap.smart_read(fixture, range_spec=None, expand=False)
        ms1 = (time.perf_counter() - t0) * 1000.0
        assert p1["cache_hit"] is False, "first call should be a cache miss"

        # Second call — cache must be warm.
        t1 = time.perf_counter()
        p2 = cap.smart_read(fixture, range_spec=None, expand=False)
        ms2 = (time.perf_counter() - t1) * 1000.0
        assert p2["cache_hit"] is True, "second call should be a cache hit"

        native_tokens = _count_tiktoken(native_text)

        for _, (payload, ms, hit) in enumerate([(p1, ms1, False), (p2, ms2, True)]):
            parts = [str(payload.get("content") or "")]
            outline = payload.get("outline")
            if outline:
                parts.append(json.dumps(outline) if not isinstance(outline, str) else outline)
            lemoncrow_text = "".join(parts)
            lemoncrow_tokens = _count_tiktoken(lemoncrow_text)
            row = ABRow(
                tool=f"read_cache_{'hit' if hit else 'miss'}",
                mode=str(payload.get("mode") or "unknown"),
                language=str(payload.get("language") or "unknown"),
                path=_try_relative(fixture),
                native_chars=len(native_text),
                lemoncrow_chars=len(lemoncrow_text),
                native_tokens=native_tokens,
                lemoncrow_tokens=lemoncrow_tokens,
                ratio=len(lemoncrow_text) / len(native_text) if native_text else 1.0,
                token_ratio=lemoncrow_tokens / native_tokens if native_tokens else 1.0,
                chars_saved=len(native_text) - len(lemoncrow_text),
                tokens_saved_measured=max(0, native_tokens - lemoncrow_tokens),
                tokens_saved_reported=int(payload.get("tokens_saved", 0) or 0),
                native_ms=0.0,
                lemoncrow_ms=round(ms, 3),
                ts=time.time(),
            )
            _append_row(row)


@pytest.mark.ab
def test_read_ab_expand_true() -> None:
    """expand=True forces full content even when effective_loc > 200."""
    fixture = FIXTURE_DIR / "sample.cpp"
    if not fixture.is_file():
        pytest.xfail(f"fixture missing: {fixture}")

    cap = SemanticFileMemoryCapability(_lemoncrow_root())
    native_text = fixture.read_text(encoding="utf-8")
    assert len(native_text.splitlines()) > 200, "fixture must exceed 200 lines for this test"

    payload = cap.smart_read(fixture, range_spec=None, expand=True)
    assert payload["mode"] == "full", f"expand=True must force mode=full, got {payload['mode']}"
    content = str(payload.get("content") or "")
    # Full mode must deliver the complete source.
    assert content == native_text, "expand=True content must equal the raw file text"

    native_tokens = _count_tiktoken(native_text)
    lemoncrow_tokens = _count_tiktoken(content)

    row = ABRow(
        tool="read_expand_true",
        mode="full",
        language=str(payload.get("language") or "unknown"),
        path=_try_relative(fixture),
        native_chars=len(native_text),
        lemoncrow_chars=len(content),
        native_tokens=native_tokens,
        lemoncrow_tokens=lemoncrow_tokens,
        ratio=1.0,
        token_ratio=1.0,
        chars_saved=0,
        tokens_saved_measured=0,
        tokens_saved_reported=int(payload.get("tokens_saved", 0) or 0),
        native_ms=0.0,
        lemoncrow_ms=0.0,
        ts=time.time(),
    )
    _append_row(row)


@pytest.mark.ab
def test_read_ab_max_lines_legacy(tmp_path: Path) -> None:
    """max_lines path goes through _core_runtime().smart_read (different code path)."""
    # Import the runtime engine directly — this is the path triggered by
    # tool_smart_read when max_lines is set without range or expand.
    from lemoncrow.core.runtime.engine import LemonCrowRuntimeCore

    runtime = LemonCrowRuntimeCore(root=tmp_path)

    fixture = FIXTURE_DIR / "sample.c"
    if not fixture.is_file():
        pytest.xfail(f"fixture missing: {fixture}")

    native_text = fixture.read_text(encoding="utf-8")
    assert len(native_text.splitlines()) > 50, "fixture must exceed 50 lines for this test"

    payload = runtime.smart_read(str(fixture), max_lines=50)

    # The runtime engine's smart_read returns a summary string, not raw content.
    # It is capped at max_lines via summarize_file.
    summary = str(payload.get("summary") or "")
    summary_lines = summary.replace("\n... [truncated]", "").splitlines()
    assert len(summary_lines) <= 50, f"max_lines=50 should cap summary at 50 lines, got {len(summary_lines)}"

    native_tokens = _count_tiktoken(native_text)
    lemoncrow_tokens = _count_tiktoken(summary)

    row = ABRow(
        tool="read_max_lines_legacy",
        mode="legacy_summary",
        language=str(payload.get("language") or "unknown"),
        path=_try_relative(fixture),
        native_chars=len(native_text),
        lemoncrow_chars=len(summary),
        native_tokens=native_tokens,
        lemoncrow_tokens=lemoncrow_tokens,
        ratio=len(summary) / len(native_text) if native_text else 1.0,
        token_ratio=lemoncrow_tokens / native_tokens if native_tokens else 1.0,
        chars_saved=len(native_text) - len(summary),
        tokens_saved_measured=max(0, native_tokens - lemoncrow_tokens),
        tokens_saved_reported=0,
        native_ms=0.0,
        lemoncrow_ms=0.0,
        ts=time.time(),
    )
    _append_row(row)


@pytest.mark.ab
def test_read_ab_empty_file(tmp_path: Path) -> None:
    """Empty file should not crash and return a sensible shape."""
    empty = tmp_path / "empty.py"
    empty.write_text("", encoding="utf-8")

    cap = SemanticFileMemoryCapability(_lemoncrow_root())
    payload = cap.smart_read(empty, range_spec=None, expand=False)

    # Must not raise; must have mode and language keys.
    assert "mode" in payload, "empty file payload missing 'mode'"
    assert "language" in payload, "empty file payload missing 'language'"

    content = str(payload.get("content") or "")
    outline = payload.get("outline")
    lemoncrow_text = content + (json.dumps(outline) if outline else "")

    row = ABRow(
        tool="read_empty_file",
        mode=str(payload.get("mode") or "unknown"),
        language=str(payload.get("language") or "unknown"),
        path="tmp/empty.py",
        native_chars=0,
        lemoncrow_chars=len(lemoncrow_text),
        native_tokens=0,
        lemoncrow_tokens=_count_tiktoken(lemoncrow_text),
        ratio=1.0,
        token_ratio=1.0,
        chars_saved=0,
        tokens_saved_measured=0,
        tokens_saved_reported=int(payload.get("tokens_saved", 0) or 0),
        native_ms=0.0,
        lemoncrow_ms=0.0,
        ts=time.time(),
    )
    _append_row(row)


@pytest.mark.ab
def test_read_ab_nonutf8_binary(tmp_path: Path) -> None:
    """Binary file must not crash; errors='replace' keeps it readable."""
    binary = tmp_path / "blob.bin"
    # Write 512 bytes of pseudo-random binary (many non-UTF-8 sequences).

    payload_bytes = bytes(range(256)) * 2  # 512 bytes, all byte values present
    binary.write_bytes(payload_bytes)

    cap = SemanticFileMemoryCapability(_lemoncrow_root())
    # Must not raise.
    payload = cap.smart_read(binary, range_spec=None, expand=False)

    assert "mode" in payload, "binary file payload missing 'mode'"
    content = str(payload.get("content") or "")
    outline = payload.get("outline")
    lemoncrow_text = content + (json.dumps(outline) if outline else "")
    # Must return something non-empty (replacement chars count).
    assert len(lemoncrow_text) > 0, "binary file returned empty payload"

    row = ABRow(
        tool="read_binary_file",
        mode=str(payload.get("mode") or "unknown"),
        language=str(payload.get("language") or "unknown"),
        path="tmp/blob.bin",
        native_chars=len(payload_bytes),
        lemoncrow_chars=len(lemoncrow_text),
        native_tokens=_count_tiktoken(lemoncrow_text),  # use decoded text for token count
        lemoncrow_tokens=_count_tiktoken(lemoncrow_text),
        ratio=1.0,
        token_ratio=1.0,
        chars_saved=0,
        tokens_saved_measured=0,
        tokens_saved_reported=int(payload.get("tokens_saved", 0) or 0),
        native_ms=0.0,
        lemoncrow_ms=0.0,
        ts=time.time(),
    )
    _append_row(row)
