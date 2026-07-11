from __future__ import annotations

from lemoncrow.core.foundation.extractor import extract_candidate
from lemoncrow.core.foundation.models import Trace, ValidationResult


def test_extract_minimal_trace_uses_fallback_procedure() -> None:
    trace = Trace(
        id="t",
        agent="a",
        domain="coding",
        task="Fix import bug",
        status="failed",
    )
    cand = extract_candidate(trace)
    assert cand.block.title.startswith("Fix import bug")
    assert cand.block.procedure  # non-empty per validator
    assert cand.confidence == 0.40


def test_extract_high_confidence_for_validated_success() -> None:
    trace = Trace(
        id="t",
        agent="a",
        domain="coding",
        task="Fix bug",
        status="success",
        files_touched=["src/foo/bar.py"],
        commands_run=["pytest -k bar"],
        diff_summary="rename baz -> qux",
        output_summary="all green",
        validation_results=[
            ValidationResult(name="unit", passed=True),
            ValidationResult(name="lint", passed=True),
        ],
    )
    cand = extract_candidate(trace)
    # 0.40 + 0.20 (success) + 0.20 (2 validations passed, capped) + 0.10 (files+validations)
    assert cand.confidence >= 0.85
    assert "unit" in cand.block.verification
    assert "src/foo/**" in cand.block.file_patterns


def test_extract_derives_triggers_and_tool_patterns_from_runtime_signals() -> None:
    trace = Trace(
        id="t2",
        agent="a",
        domain="coding",
        task="Fix context retrieval ranking for parser errors",
        status="success",
        files_touched=["src/lemoncrow/core/foundation/parser.py"],
        commands_run=["uv run pytest tests/core/test_parser.py -q"],
    )
    cand = extract_candidate(trace)
    assert "parser" in cand.block.triggers
    assert "retrieval" in cand.block.triggers
    assert "pytest" in cand.block.tool_patterns
    assert "src/lemoncrow/core/foundation/parser.py" in cand.block.file_patterns
