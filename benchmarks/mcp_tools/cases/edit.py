"""Benchmark cases for the `edit` MCP tool.

Edit's savings come from batching: one call handles multi-file atomic edits,
rollback on failure, and post-edit hook diagnostics vs N separate Write calls.
"""

from __future__ import annotations

from typing import Any

from benchmarks.mcp_tools.harness import BenchCase


def _assert_edit_applied(result: dict[str, Any]) -> None:
    assert "applied" in result, f"edit response must have 'applied', got: {list(result)}"
    assert isinstance(result["applied"], list), "'applied' must be a list"
    assert len(result["applied"]) >= 1, "at least one edit must be applied"
    assert not result.get("failed"), f"no edits should fail, got failed={result.get('failed')}"
    assert not result.get("rolled_back"), f"no rollback expected, got rolled_back={result.get('rolled_back')}"


def _assert_multi_file_atomic(result: dict[str, Any]) -> None:
    _assert_edit_applied(result)
    assert len(result["applied"]) >= 2, f"multi-file edit must apply >=2 descriptors, got {len(result['applied'])}"


def _assert_create_file(result: dict[str, Any]) -> None:
    assert "applied" in result, f"create response must have 'applied', got: {list(result)}"
    assert not result.get("failed"), f"create must not fail, got: {result.get('failed')}"


def _assert_rollback(result: dict[str, Any]) -> None:
    assert result.get("rolled_back") is True or result.get(
        "failed"
    ), f"expected rollback or failure when one descriptor is invalid, got: {result}"


def _single_replace_case(index: int, target: str, old: str, new: str) -> BenchCase:
    return BenchCase(
        op="edit",
        label=f"edit/single-replace/{index:02d}",
        args={
            "edits": [{"file_path": target, "old_string": old, "new_string": new}],
            "post_edit_hooks": False,
        },
        assert_keys=["applied"],
        custom_assert=_assert_edit_applied,
        baseline_tokens=200,
    )


def _line_replace_case(index: int, target: str, old: str, new: str) -> BenchCase:
    return BenchCase(
        op="edit",
        label=f"edit/line-replace/{index:02d}",
        args={
            "edits": [{"file_path": target, "old_string": old, "new_string": new}],
            "post_edit_hooks": False,
        },
        assert_keys=["applied"],
        custom_assert=_assert_edit_applied,
        baseline_tokens=220,
    )


def _multi_file_case(index: int, new_alpha: str, new_beta: str) -> BenchCase:
    return BenchCase(
        op="edit",
        label=f"edit/multi-file-atomic/{index:02d}",
        args={
            "edits": [
                {
                    "file_path": "__EDIT_FILE_A__",
                    "old_string": "PLACEHOLDER_ALPHA",
                    "new_string": new_alpha,
                },
                {
                    "file_path": "__EDIT_FILE_B__",
                    "old_string": "PLACEHOLDER_BETA",
                    "new_string": new_beta,
                },
            ],
            "atomic": True,
            "post_edit_hooks": False,
        },
        assert_keys=["applied"],
        custom_assert=_assert_multi_file_atomic,
        baseline_tokens=400,
    )


def _create_case(index: int, content: str) -> BenchCase:
    return BenchCase(
        op="edit",
        label=f"edit/create-file/{index:02d}",
        args={
            "edits": [{"file_path": "__EDIT_FILE_NEW__", "new_string": content, "overwrite": True}],
            "post_edit_hooks": False,
        },
        assert_keys=["applied"],
        custom_assert=_assert_create_file,
        baseline_tokens=300,
    )


def _rollback_case(index: int, missing_text: str) -> BenchCase:
    return BenchCase(
        op="edit",
        label=f"edit/atomic-rollback/{index:02d}",
        args={
            "edits": [
                {
                    "file_path": "__EDIT_FILE_A__",
                    "old_string": "PLACEHOLDER_ALPHA",
                    "new_string": f"ROLLED_BACK_ALPHA_{index}",
                },
                {
                    "file_path": "__EDIT_FILE_A__",
                    "old_string": missing_text,
                    "new_string": "SHOULD_NOT_APPEAR",
                },
            ],
            "atomic": True,
            "post_edit_hooks": False,
        },
        assert_keys=[],
        custom_assert=_assert_rollback,
        baseline_tokens=0,
    )


EDIT_CASES: list[BenchCase] = []

for index, (target, old, new) in enumerate(
    [
        ("__EDIT_FILE_A__", "PLACEHOLDER_ALPHA", "REPLACED_ALPHA"),
        ("__EDIT_FILE_A__", "PLACEHOLDER_ALPHA = 1", "PLACEHOLDER_ALPHA = 7"),
        ("__EDIT_FILE_B__", "PLACEHOLDER_BETA", "REPLACED_BETA"),
        ("__EDIT_FILE_B__", "PLACEHOLDER_BETA = 2", "PLACEHOLDER_BETA = 9"),
        ("__EDIT_FILE_A__", "# scratch file A", "# scratch file A / bench"),
        ("__EDIT_FILE_B__", "# scratch file B", "# scratch file B / bench"),
    ],
    start=1,
):
    EDIT_CASES.append(_single_replace_case(index, target, old, new))

for index, (target, old, new) in enumerate(
    [
        ("__EDIT_FILE_A__#1-2", "PLACEHOLDER_ALPHA = 1", "PLACEHOLDER_ALPHA = 11"),
        ("__EDIT_FILE_A__#1-2", "# scratch file A", "# scratch file A / ranged"),
        ("__EDIT_FILE_B__#1-2", "PLACEHOLDER_BETA = 2", "PLACEHOLDER_BETA = 12"),
        ("__EDIT_FILE_B__#1-2", "# scratch file B", "# scratch file B / ranged"),
    ],
    start=1,
):
    EDIT_CASES.append(_line_replace_case(index, target, old, new))

for index, (new_alpha, new_beta) in enumerate(
    [
        ("REPLACED_ALPHA", "REPLACED_BETA"),
        ("ALPHA_VARIANT_ONE", "BETA_VARIANT_ONE"),
        ("ALPHA_VARIANT_TWO", "BETA_VARIANT_TWO"),
        ("ALPHA_VARIANT_THREE", "BETA_VARIANT_THREE"),
        ("ALPHA_VARIANT_FOUR", "BETA_VARIANT_FOUR"),
        ("ALPHA_VARIANT_FIVE", "BETA_VARIANT_FIVE"),
    ],
    start=1,
):
    EDIT_CASES.append(_multi_file_case(index, new_alpha, new_beta))

for index, content in enumerate(
    [
        "# created by bench\nresult = 42\n",
        "# created by bench\nanswer = 'ok'\n",
        "from __future__ import annotations\n\nVALUE = 7\n",
        "class Created:\n    value = 3\n",
    ],
    start=1,
):
    EDIT_CASES.append(_create_case(index, content))

for index, missing_text in enumerate(
    [
        "THIS_STRING_DOES_NOT_EXIST_XYZ",
        "ABSENT_ALPHA_SENTINEL",
        "MISSING_SECOND_EDIT_MARKER",
        "NO_SUCH_PLACEHOLDER",
    ],
    start=1,
):
    EDIT_CASES.append(_rollback_case(index, missing_text))
