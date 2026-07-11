from __future__ import annotations

from lemoncrow.core.capabilities.lesson_promotion.draft import draft_lesson_candidate
from lemoncrow.core.foundation.models import Playbook, Trace, ValidationResult


def _trace(
    trace_id: str,
    *,
    errors: list[str],
    validations: list[ValidationResult] | None = None,
    command: str = "pytest",
) -> Trace:
    return Trace(
        id=trace_id,
        agent="codex",
        domain="coding",
        task="fix failing tests",
        status="failed",
        commands_run=[command],
        errors_seen=errors,
        validation_results=validations or [],
    )


def test_draft_prefers_new_rubric_check_when_failed_check_matches() -> None:
    traces = [
        _trace(
            "t1",
            errors=["timeout while publishing"],
            validations=[ValidationResult(name="publish_gate", passed=False)],
        ),
        _trace(
            "t2",
            errors=["timeout while publishing"],
            validations=[ValidationResult(name="publish_gate", passed=False)],
        ),
        _trace(
            "t3",
            errors=["timeout while publishing"],
            validations=[ValidationResult(name="publish_gate", passed=False)],
        ),
    ]

    candidate = draft_lesson_candidate(
        traces=traces,
        domain="coding",
        cluster_fingerprint="timeout while publishing",
        embedding=[0.1, 0.2],
        existing_blocks=[],
    )

    assert candidate.kind == "new_rubric_check"
    assert candidate.proposed_rubric_check is not None


def test_draft_uses_new_block_for_long_shared_error_substring() -> None:
    shared = "database write lock timeout"
    traces = [
        _trace("t1", errors=[f"fatal: {shared} during update"]),
        _trace("t2", errors=[f"retry failed: {shared} at step 2"]),
        _trace("t3", errors=[f"cannot proceed: {shared} observed"]),
    ]

    candidate = draft_lesson_candidate(
        traces=traces,
        domain="coding",
        cluster_fingerprint=shared,
        embedding=[0.1, 0.2],
        existing_blocks=[],
    )

    assert candidate.kind == "new_block"
    assert candidate.proposed_block is not None
    assert any(shared in dead_end for dead_end in candidate.proposed_block.dead_ends)


def test_draft_falls_back_to_edit_block_with_overlap_target() -> None:
    traces = [
        _trace("t1", errors=["oops", "permission denied writing cache"]),
        _trace("t2", errors=["oops", "permission denied writing state"]),
        _trace("t3", errors=["oops", "permission denied writing lockfile"]),
    ]
    existing = [
        Playbook(
            id="rb-cache",
            title="Cache write precheck",
            domain="coding",
            triggers=["cache"],
            situation="Cache writes can fail without writable directory.",
            dead_ends=["permission denied"],
            procedure=["Check path permissions before writing cache artifacts."],
        )
    ]

    candidate = draft_lesson_candidate(
        traces=traces,
        domain="coding",
        cluster_fingerprint="oops",
        embedding=[0.1, 0.2],
        existing_blocks=existing,
    )

    assert candidate.kind == "edit_block"
    assert candidate.target_id == "rb-cache"
