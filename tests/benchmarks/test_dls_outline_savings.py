from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest

from atelier.core.capabilities.semantic_file_memory import SemanticFileMemoryCapability
from atelier.core.capabilities.semantic_file_memory.treesitter_ast import outline_text

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "languages"
DLS_FIXTURES = {
    "bash": FIXTURE_DIR / "sample.sh",
    "yaml": FIXTURE_DIR / "config.yaml",
    "toml": FIXTURE_DIR / "config.toml",
    "json": FIXTURE_DIR / "config.json",
    "sql": FIXTURE_DIR / "sample.sql",
}


@dataclass(frozen=True)
class OutlineSavingsRow:
    language: str
    fixture: str
    full_tokens: int
    generic_tokens: int
    dedicated_tokens: int
    dedicated_vs_full_saved_pct: float
    dedicated_vs_generic_saved_pct: float
    guard_passed: bool
    mode_observed: str
    outline_kind_observed: str


def _count_tokens(text: str) -> int:
    if not text:
        return 0
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text, disallowed_special=()))
    except Exception:
        return max(1, len(text) // 4)


def _saved_pct(returned_tokens: int, full_tokens: int) -> float:
    if full_tokens <= 0:
        return 0.0
    return round(((full_tokens - returned_tokens) / full_tokens) * 100, 2)


def measure_outline_savings(language: str, fixture: Path, cache_root: Path) -> OutlineSavingsRow:
    source = fixture.read_text(encoding="utf-8")
    cap = SemanticFileMemoryCapability(cache_root)
    generic = cap._generic_outline_text(source, language)
    dedicated = outline_text(language, source)
    payload = cap.smart_read(fixture, outline_threshold=0)
    outline = payload.get("outline")
    outline_kind = outline.get("kind", "") if isinstance(outline, dict) else ""

    full_tokens = _count_tokens(source)
    generic_tokens = _count_tokens(generic)
    dedicated_tokens = _count_tokens(dedicated)
    guard_passed = bool(dedicated and len(dedicated) <= int(len(source) * 0.75))

    return OutlineSavingsRow(
        language=language,
        fixture=str(fixture.relative_to(FIXTURE_DIR.parents[1])),
        full_tokens=full_tokens,
        generic_tokens=generic_tokens,
        dedicated_tokens=dedicated_tokens,
        dedicated_vs_full_saved_pct=_saved_pct(dedicated_tokens, full_tokens),
        dedicated_vs_generic_saved_pct=_saved_pct(dedicated_tokens, generic_tokens),
        guard_passed=guard_passed,
        mode_observed=str(payload.get("mode", "")),
        outline_kind_observed=outline_kind,
    )


@pytest.mark.ab
@pytest.mark.parametrize("language", tuple(DLS_FIXTURES), ids=tuple(DLS_FIXTURES))
def test_dls_outline_savings_are_honest(language: str, tmp_path: Path) -> None:
    row = measure_outline_savings(language, DLS_FIXTURES[language], tmp_path)

    assert row.full_tokens > 0
    assert row.dedicated_tokens > 0
    assert row.dedicated_tokens <= row.full_tokens
    if row.guard_passed:
        assert row.mode_observed == "outline"
        assert row.outline_kind_observed == "treesitter"
        assert row.dedicated_vs_full_saved_pct >= 25.0
    else:
        assert row.outline_kind_observed != "treesitter"


def test_dls_outline_savings_artifact_matches_measurements(tmp_path: Path) -> None:
    artifact = Path("reports/2026-W22/dls-outline-savings.json")
    if not artifact.exists():
        pytest.skip("committed DLS outline savings artifact not present")

    measured = [asdict(measure_outline_savings(language, path, tmp_path)) for language, path in DLS_FIXTURES.items()]
    recorded = json.loads(artifact.read_text(encoding="utf-8"))["rows"]

    assert recorded == measured
