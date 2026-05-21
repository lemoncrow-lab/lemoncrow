"""Real A/B benchmark: mcp__atelier__read vs native cat/read on real files.

This test is NOT a unit test — it measures actual chars/tokens delivered by both
branches on real repository files and persists the deltas to
``~/.atelier/savings_calibration.jsonl``. Rolling medians from that file are
intended to replace the magic ``LIVE_*_TOKENS_PER_CALL`` constants in
``src/atelier/core/capabilities/plugin_runtime.py``.

Marked ``ab`` so it runs under ``make bench-ab`` and is skipped by default in
``pytest`` to keep CI fast. Toggle locally with::

    uv run pytest tests/benchmarks/test_read_ab_real.py -v -m ab

Do NOT delete this file when refactoring. It is the seed measurement that
legitimizes (or invalidates) every per-tool savings claim Atelier ships.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest

from atelier.core.capabilities.semantic_file_memory import SemanticFileMemoryCapability

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

# Two fixture groups so per-language calibration is honest:
# 1. REAL repo files (Python) — measures the real-world workload
# 2. SYNTHETIC fixtures for languages the repo doesn't ship (Go/Rust/Java/Markdown)
FIXTURES_REAL_PY: tuple[Path, ...] = (
    REPO_ROOT / "src/atelier/core/capabilities/code_context/engine.py",
    REPO_ROOT / "src/atelier/core/capabilities/plugin_runtime.py",
    REPO_ROOT / "src/atelier/core/capabilities/pricing.py",
    REPO_ROOT / "src/atelier/gateway/adapters/mcp_server.py",
)
FIXTURES_SYNTHETIC: tuple[Path, ...] = (
    FIXTURE_DIR / "sample.go",
    FIXTURE_DIR / "sample.rs",
    FIXTURE_DIR / "Sample.java",
    REPO_ROOT / "docs/specs/optimization-autopilot.md",  # real big markdown
)
FIXTURES: tuple[Path, ...] = FIXTURES_REAL_PY + FIXTURES_SYNTHETIC


@dataclass
class ABRow:
    """One A/B measurement persisted to the calibration store."""

    tool: str  # 'read'
    mode: str  # atelier read mode: 'outline' | 'range' | 'full'
    language: str  # 'python' | 'go' | 'rust' | 'java' | 'markdown' | ...
    path: str  # relative path inside the repo
    native_chars: int  # len(Path.read_text())
    atelier_chars: int  # len of what atelier actually delivered
    native_tokens: int  # tiktoken count of native_text
    atelier_tokens: int  # tiktoken count of atelier-delivered text
    ratio: float  # atelier_chars / native_chars (1.0 = no saving)
    token_ratio: float  # atelier_tokens / native_tokens
    chars_saved: int  # native_chars - atelier_chars
    tokens_saved_measured: int  # native_tokens - atelier_tokens (from tiktoken)
    tokens_saved_reported: int  # what the tool itself claims
    native_ms: float
    atelier_ms: float
    ts: float


def _calibration_path() -> Path:
    root = Path(os.environ.get("ATELIER_ROOT") or (Path.home() / ".atelier"))
    root.mkdir(parents=True, exist_ok=True)
    return root / "savings_calibration.jsonl"


def _append_row(row: ABRow) -> None:
    path = _calibration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(row), sort_keys=True) + "\n")


def _atelier_root() -> Path:
    return Path(os.environ.get("ATELIER_ROOT") or (Path.home() / ".atelier"))


def _count_tiktoken(text: str) -> int:
    """Independent token count for verifying what the tool reports."""
    if not text:
        return 0
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text, disallowed_special=()))
    except Exception:
        return len(text) // 4


def _try_relative(p: Path) -> str:
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


@pytest.mark.ab
@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.name)
def test_read_ab_real(fixture: Path) -> None:
    """Run both branches, persist the delta, assert only honest invariants."""
    if not fixture.is_file():
        pytest.xfail(f"fixture missing: {fixture}")

    # --- native branch (the thing we compare against)
    t0 = time.perf_counter()
    native_text = fixture.read_text(encoding="utf-8")
    native_ms = (time.perf_counter() - t0) * 1000.0
    native_chars = len(native_text)
    native_tokens = _count_tiktoken(native_text)

    # --- atelier branch (default outline-first behavior)
    cap = SemanticFileMemoryCapability(_atelier_root())
    t1 = time.perf_counter()
    payload = cap.smart_read(fixture, range_spec=None, expand=False)
    atelier_ms = (time.perf_counter() - t1) * 1000.0

    mode = str(payload.get("mode") or "unknown")
    language = str(payload.get("language") or "unknown")
    delivered_parts = [str(payload.get("content") or "")]
    outline = payload.get("outline")
    if outline:
        delivered_parts.append(json.dumps(outline) if not isinstance(outline, str) else outline)
    atelier_chars = sum(len(part) for part in delivered_parts)
    atelier_tokens = _count_tiktoken("".join(delivered_parts))
    tokens_saved = int(payload.get("tokens_saved", 0) or 0)

    row = ABRow(
        tool="read",
        mode=mode,
        language=language,
        path=_try_relative(fixture),
        native_chars=native_chars,
        atelier_chars=atelier_chars,
        native_tokens=native_tokens,
        atelier_tokens=atelier_tokens,
        ratio=(atelier_chars / native_chars) if native_chars else 1.0,
        token_ratio=(atelier_tokens / native_tokens) if native_tokens else 1.0,
        chars_saved=native_chars - atelier_chars,
        tokens_saved_measured=max(0, native_tokens - atelier_tokens),
        tokens_saved_reported=tokens_saved,
        native_ms=round(native_ms, 3),
        atelier_ms=round(atelier_ms, 3),
        ts=time.time(),
    )
    _append_row(row)

    # Honest invariants only:
    assert native_chars > 0, "native read returned no bytes"
    assert atelier_chars > 0, "atelier returned no bytes"
    assert atelier_chars <= native_chars, (
        f"atelier returned {atelier_chars} chars vs native {native_chars} on {fixture.name}"
    )
    if tokens_saved > 0:
        assert row.chars_saved > 0, (
            f"{fixture.name}: tool reports tokens_saved={tokens_saved} but chars_saved={row.chars_saved}"
        )
    # Reported tokens_saved (from the tool, via tiktoken now) should agree
    # with our independent tiktoken count within 10% on either side.
    if tokens_saved > 100 and row.tokens_saved_measured > 100:
        ratio = tokens_saved / row.tokens_saved_measured
        assert 0.9 <= ratio <= 1.1, (
            f"{fixture.name}: tool tokens_saved={tokens_saved} disagrees with "
            f"independent tiktoken count {row.tokens_saved_measured} (ratio {ratio:.2f})"
        )


@pytest.mark.ab
@pytest.mark.parametrize(
    "fixture",
    [FIXTURE_DIR / "sample.go", FIXTURE_DIR / "sample.rs", FIXTURE_DIR / "Sample.java"],
    ids=lambda p: p.name,
)
def test_generic_outline_compresses_large_files(fixture: Path, tmp_path: Path) -> None:
    """Force the generic outline to fire by inflating fixtures past 200 LOC.

    The small synthetic fixtures (130-150 LOC) fall under the production
    outline_threshold of 200, so they get full reads in test_read_ab_real.
    This test triples each fixture so the generic outline code path actually
    runs, and persists the per-language compression ratio to the calibration
    store under a separate path so production-vs-synthetic numbers stay
    distinguishable.
    """
    if not fixture.is_file():
        pytest.xfail(f"fixture missing: {fixture}")

    src = fixture.read_text(encoding="utf-8")
    # 3× concatenation, in a tmp file so we don't pollute the test corpus.
    big = tmp_path / fixture.name
    big.write_text(src + "\n\n" + src + "\n\n" + src, encoding="utf-8")

    cap = SemanticFileMemoryCapability(_atelier_root())
    payload = cap.smart_read(big, range_spec=None, expand=False)
    mode = str(payload.get("mode"))
    language = str(payload.get("language"))
    outline = payload.get("outline")

    # Generic outline must fire for these languages once they cross the threshold.
    assert mode == "outline", (
        f"{fixture.name} (3x = {len(big.read_text())} chars) returned mode={mode}, expected outline"
    )
    assert isinstance(outline, dict) and outline.get("kind") == "generic", (
        f"{fixture.name}: expected generic outline, got {outline}"
    )

    native_chars = len(big.read_text())
    outline_text = str(outline.get("text") or "")
    atelier_chars = len(outline_text)
    native_tokens = _count_tiktoken(big.read_text())
    atelier_tokens = _count_tiktoken(outline_text)

    row = ABRow(
        tool="read_generic_outline",
        mode=mode,
        language=language,
        path=f"synthetic-3x:{fixture.name}",
        native_chars=native_chars,
        atelier_chars=atelier_chars,
        native_tokens=native_tokens,
        atelier_tokens=atelier_tokens,
        ratio=atelier_chars / native_chars,
        token_ratio=atelier_tokens / native_tokens if native_tokens else 1.0,
        chars_saved=native_chars - atelier_chars,
        tokens_saved_measured=max(0, native_tokens - atelier_tokens),
        tokens_saved_reported=int(payload.get("tokens_saved", 0) or 0),
        native_ms=0.0,
        atelier_ms=0.0,
        ts=time.time(),
    )
    _append_row(row)

    # Sanity: generic outline must save at least 25% (the production gate).
    assert atelier_chars <= int(native_chars * 0.75), (
        f"{fixture.name}: generic outline only saved "
        f"{(1 - atelier_chars / native_chars) * 100:.1f}% — below 25% gate"
    )


@pytest.mark.ab
def test_calibration_file_grows() -> None:
    """After the parametrized A/B runs, the calibration file should have rows.

    Sanity check: the seed run must produce N >= len(FIXTURES) measurement
    rows in ``savings_calibration.jsonl``. Otherwise the harness is silently
    broken and we're back to magic constants.
    """
    path = _calibration_path()
    assert path.exists(), f"no calibration file at {path}"
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    read_rows = [r for r in rows if r.get("tool") == "read"]
    assert len(read_rows) >= len(FIXTURES), (
        f"expected >= {len(FIXTURES)} read rows, found {len(read_rows)}"
    )
