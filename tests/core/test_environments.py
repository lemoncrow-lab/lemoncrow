"""Tests for environments loading and matching."""

from __future__ import annotations

from atelier.core.foundation.environments import (
    find_forbidden_violations,
    load_packaged_environments,
    match_environments,
)


def test_packaged_environments_load_all_six() -> None:
    envs = load_packaged_environments()
    ids = {e.id for e in envs}
    assert {
        "env_coding_general",
        "env_debugging_loop",
        "env_state_change_safety",
        "env_source_of_truth_change",
        "env_change_gate",
        "env_knowledge_authoring",
    }.issubset(ids)


def test_state_change_env_blocks_forbidden_slug_plan() -> None:
    envs = [e for e in load_packaged_environments() if e.id == "env_state_change_safety"]
    plan = ["Resolve target from URL slug alone before the update"]
    violations = find_forbidden_violations(plan, envs)
    assert violations
    env, _step, phrase = violations[0]
    assert env.id == "env_state_change_safety"
    assert "slug" in phrase


def test_match_environments_by_domain_prefix() -> None:
    envs = load_packaged_environments()
    matches = match_environments(
        task="Apply a live config deploy to an external system",
        domain="state.change.deploy",
        environments=envs,
    )
    assert any(e.id == "env_state_change_safety" for e in matches)
