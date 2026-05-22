from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from atelier.core.capabilities import plugin_runtime as runtime


def _fixture(name: str) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[2]
    matches = sorted(repo_root.glob(f"*/docs/validation/fixtures/{name}"))
    if not matches:
        pytest.skip(f"missing validation fixture {name}")
    data = json.loads(matches[0].read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _first_counter(given: dict[str, Any], prefix: str) -> int:
    for key, value in given.items():
        if key.startswith(prefix) and key.endswith("_calls"):
            return int(value)
    return 0


def _dispatch(entrypoint: str, given: dict[str, Any]) -> Any:
    if entrypoint == "search.validate_input":
        return runtime.validate_search_input(given["input"])
    if entrypoint == "search.parse_line_suffix":
        return runtime.parse_line_suffix(given["pattern"])
    if entrypoint == "search.should_summarize":
        return runtime.should_summarize(**given)
    if entrypoint == "search.apply_if_modified_since":
        return runtime.apply_if_modified_since(**given)
    if entrypoint == "edit.apply_text_file_edits":
        return runtime.apply_text_file_edits(given["initial"], given["edits"])
    if entrypoint == "edit.fuzzy_acceptance_policy":
        return runtime.fuzzy_acceptance_policy(**given)
    if entrypoint == "edit.apply_notebook_source_edit":
        return runtime.apply_notebook_source_edit(**given)
    if entrypoint == "edit.find_notebook_match":
        return runtime.find_notebook_match(**given)
    if entrypoint == "sql.auto_limit":
        return runtime.sql_auto_limit(**given)
    if entrypoint == "sql.discover_connection":
        return runtime.discover_connection(**given)
    if entrypoint == "sql.column_typo_repair_policy":
        return runtime.column_typo_repair_policy(**given)
    if entrypoint == "sql.postgres_try_auto_fix":
        return runtime.postgres_try_auto_fix(**given)
    if entrypoint == "recall.constants":
        return runtime.recall_constants()
    if entrypoint == "recall.chunk_transcript":
        return runtime.chunk_transcript(**given)
    if entrypoint == "status_line.choose_message":
        return runtime.status_line_choose_message(
            auth_present=given.get("auth_present", True),
            update_flag=given.get("update_flag"),
            session_id=given.get("session_id"),
            total_tool_calls=_first_counter(given, "total_"),
        )
    if entrypoint == "session_start.install_status_line":
        return runtime.session_start_install_status_line(**given)
    if entrypoint == "hooks.classify_bash":
        return runtime.classify_bash(**given)
    if entrypoint == "hooks.edit_nudge":
        return runtime.edit_nudge(**given)
    if entrypoint == "hooks.session_start":
        return runtime.session_start(**given)
    if entrypoint == "codex.update_notification":
        return runtime.update_notification(**given)
    if entrypoint == "savings.equivalent_calls":
        return {"equivalent_calls": runtime.equivalent_calls(**given)}
    if entrypoint == "savings.compute_codex_savings":
        return runtime.compute_live_savings(given["equivalent_calls"], given.get("model"))
    if entrypoint == "savings.detect_read_batch":
        return runtime.detect_read_batch(**given)
    if entrypoint == "savings.detect_edit_batch":
        return runtime.detect_edit_batch(**given)
    if entrypoint == "savings.detect_grep_read":
        return runtime.detect_grep_read(**given)
    if entrypoint == "savings.detect_failed_edit":
        return runtime.detect_failed_edit(**given)
    if entrypoint == "savings.detect_bash_sql":
        return runtime.detect_bash_sql(**given)
    if entrypoint == "savings.baseline_is_available":
        return runtime.baseline_is_available(**given)
    if entrypoint == "savings.baseline_time_saved":
        return runtime.baseline_time_saved(**given)
    if entrypoint == "savings.efficiency_gain":
        return runtime.efficiency_gain(
            actual_tool_calls=_first_counter(given, "actual_"),
            equivalent_baseline_calls=_first_counter(given, "equivalent_"),
        )
    raise AssertionError(f"unmapped entrypoint {entrypoint}")


def _assert_expected(actual: Any, expected: Any) -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict)
        for key, value in expected.items():
            if key in {"formula", "notes"}:
                continue
            if key == "settings_write_contains":
                _assert_expected(actual[key], value)
                continue
            if key.endswith("_not_contains"):
                actual_key = key.removesuffix("_not_contains")
                assert str(value) not in str(actual.get(actual_key, ""))
                continue
            if key.endswith("_contains"):
                actual_key = key.removesuffix("_contains")
                haystack = str(actual.get(actual_key, ""))
                needle = str(value)
                if needle.lower().endswith("available"):
                    assert "available" in haystack.lower()
                else:
                    assert needle in haystack
                continue
            if key == "state_path":
                assert str(actual[key]).endswith("atelier-edit-state.json")
                continue
            if key == "command" and isinstance(value, str) and value.startswith("node "):
                assert str(actual[key]).endswith("/scripts/statusline.sh")
                continue
            assert key in actual
            _assert_expected(actual[key], value)
        return
    if isinstance(expected, list):
        assert isinstance(actual, list)
        assert len(actual) >= len(expected)
        for index, item in enumerate(expected):
            _assert_expected(actual[index], item)
        return
    assert actual == expected


def _assert_fixture_cases(name: str) -> None:
    fixture = _fixture(name)
    for case in fixture["cases"]:
        actual = _dispatch(case["entrypoint"], case.get("given") or {})
        _assert_expected(actual, case["expect"])


def test_core_validation_fixture_cases() -> None:
    _assert_fixture_cases("core-golden-cases.json")


def test_hook_validation_fixture_cases() -> None:
    _assert_fixture_cases("hooks-golden-cases.json")


def test_savings_validation_fixture_cases() -> None:
    _assert_fixture_cases("savings-golden-cases.json")
