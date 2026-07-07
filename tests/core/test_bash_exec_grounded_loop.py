from __future__ import annotations

from atelier.core.capabilities.tool_supervision.bash_exec import classify_command


def test_classify_plain_rg_prefers_search_first_grounding() -> None:
    decision = classify_command("rg OrderService src")

    assert decision.action == "rewrite"
    assert decision.rewrite_target == "search"
    assert decision.rewrite_payload == {
        "query": "OrderService",
        "path": "src",
    }


def test_classify_flagged_rg_stays_on_grep_for_explicit_pattern_search() -> None:
    decision = classify_command("rg -i OrderService src")

    assert decision.action == "rewrite"
    assert decision.rewrite_target == "grep"
    assert decision.rewrite_payload == {
        "file_path": "src",
        "content_regex": "OrderService",
        "ignore_case": True,
        "output_mode": "file_paths_with_content",
        "lines_after": 0,
        "lines_before": 0,
    }
