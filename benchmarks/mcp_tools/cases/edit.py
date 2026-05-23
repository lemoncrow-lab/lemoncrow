"""Benchmark cases for the `edit` MCP tool.

Edit's savings come from batching: one call handles multi-file atomic edits,
rollback on failure, and post-edit hook diagnostics vs N separate Write calls.

Baseline estimates:
  - single-file: one separate Edit call (~200 tokens overhead + file content)
  - multi-file:  N separate Edit calls, each with framing (~200 * N tokens)
  - create:      Write call + diagnostic pass (~300 tokens)
  - rollback:    naive: broken file stays; atelier rolls back automatically

EDIT_WORKSPACE env var must point to a writable scratch directory.
"""

from __future__ import annotations

from typing import Any

from benchmarks.mcp_tools.harness import BenchCase

# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


def _assert_edit_applied(result: dict[str, Any]) -> None:
    assert "applied" in result, f"edit response must have 'applied', got: {list(result)}"
    assert isinstance(result["applied"], list), "'applied' must be a list"
    assert len(result["applied"]) >= 1, "at least one edit must be applied"
    assert not result.get("failed"), f"no edits should fail, got failed={result.get('failed')}"
    assert not result.get("rolled_back"), f"no rollback expected, got rolled_back={result.get('rolled_back')}"


def _assert_multi_file_atomic(result: dict[str, Any]) -> None:
    _assert_edit_applied(result)
    assert len(result["applied"]) >= 2, (
        f"multi-file edit must apply >=2 descriptors, got {len(result['applied'])}"
    )


def _assert_create_file(result: dict[str, Any]) -> None:
    assert "applied" in result, f"create response must have 'applied', got: {list(result)}"
    assert not result.get("failed"), f"create must not fail, got: {result.get('failed')}"


def _assert_rollback(result: dict[str, Any]) -> None:
    # When one descriptor fails in atomic mode, all changes are rolled back
    assert result.get("rolled_back") is True or result.get("failed"), (
        f"expected rollback or failure when one descriptor is invalid, got: {result}"
    )


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

EDIT_CASES: list[BenchCase] = [
    BenchCase(
        op="edit",
        label="edit/single-replace",
        args={
            "edits": [
                {
                    "file_path": "__EDIT_FILE_A__",
                    "old_string": "PLACEHOLDER_ALPHA",
                    "new_string": "REPLACED_ALPHA",
                }
            ],
            "post_edit_hooks": False,  # no hooks in bench env
        },
        assert_keys=["applied"],
        custom_assert=_assert_edit_applied,
        # baseline: one native Edit call with file read + framing
        baseline_tokens=200,
    ),
    BenchCase(
        op="edit",
        label="edit/multi-file-atomic",
        args={
            "edits": [
                {
                    "file_path": "__EDIT_FILE_A__",
                    "old_string": "PLACEHOLDER_ALPHA",
                    "new_string": "REPLACED_ALPHA",
                },
                {
                    "file_path": "__EDIT_FILE_B__",
                    "old_string": "PLACEHOLDER_BETA",
                    "new_string": "REPLACED_BETA",
                },
            ],
            "atomic": True,
            "post_edit_hooks": False,
        },
        assert_keys=["applied"],
        custom_assert=_assert_multi_file_atomic,
        # baseline: 2 separate Edit calls, each ~200 tokens
        baseline_tokens=400,
    ),
    BenchCase(
        op="edit",
        label="edit/create-file",
        args={
            "edits": [
                {
                    "file_path": "__EDIT_FILE_NEW__",
                    "new_string": "# created by bench\nresult = 42\n",
                    "overwrite": True,
                }
            ],
            "post_edit_hooks": False,
        },
        assert_keys=["applied"],
        custom_assert=_assert_create_file,
        # baseline: Write call (~300 tokens)
        baseline_tokens=300,
    ),
    BenchCase(
        op="edit",
        label="edit/atomic-rollback",
        args={
            "edits": [
                {
                    "file_path": "__EDIT_FILE_A__",
                    "old_string": "PLACEHOLDER_ALPHA",
                    "new_string": "REPLACED_ALPHA",
                },
                {
                    # This descriptor will fail: old_string not present after first edit
                    "file_path": "__EDIT_FILE_A__",
                    "old_string": "THIS_STRING_DOES_NOT_EXIST_XYZ",
                    "new_string": "SHOULD_NOT_APPEAR",
                },
            ],
            "atomic": True,
            "post_edit_hooks": False,
        },
        assert_keys=[],
        custom_assert=_assert_rollback,
        # no token baseline — correctness only
        baseline_tokens=0,
    ),
]
