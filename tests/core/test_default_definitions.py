from __future__ import annotations

import json
from pathlib import Path

from atelier.core.capabilities.default_definitions import build_default_registry
from atelier.core.capabilities.workflow_defaults import bootstrap_default_definitions

ROOT = Path(__file__).resolve().parents[2]
HOST_FACING_ROLES = {"code", "explore", "execute", "plan", "research", "review", "solve"}
REQUIRED_ROLES = HOST_FACING_ROLES | {"general"}
EXPECTED_ROLE_MODELS = {
    "code": "claude-opus-4.8",
    "general": "claude-opus-4.8",
    "explore": "claude-sonnet-4.6",
    "plan": "claude-sonnet-4.6",
    "execute": "claude-opus-4.8",
    "review": "claude-sonnet-4.6",
    "research": "claude-sonnet-4.6",
    "solve": "claude-opus-4.8",
}


def test_default_registry_contains_required_roles() -> None:
    registry = build_default_registry(ROOT)

    assert REQUIRED_ROLES <= set(registry.roles)

    general = registry.roles["general"]
    assert general.prompt_source is not None
    assert general.host_projections != ()
    assert general.model_default


def test_role_defaults_stay_workload_aware() -> None:
    registry = build_default_registry(ROOT)

    for role_id, expected_model in EXPECTED_ROLE_MODELS.items():
        role = registry.roles[role_id]
        assert role.model_default == expected_model


def test_host_facing_roles_stay_sourced_from_mode_docs() -> None:
    registry = build_default_registry(ROOT)

    for role_id in sorted(HOST_FACING_ROLES):
        role = registry.roles[role_id]
        assert role.prompt_source is not None
        assert role.prompt_source.as_posix().endswith(f"integrations/agents/{role_id}.md")
        body = registry.render_prompt(role_id, ROOT)
        assert "Eval" not in body
        # Mode docs now compose from shared discipline partials (substituted at
        # host-generation time), so the raw doc carries the partial placeholders.
        assert "{{CORE_DISCIPLINE}}" in body


def test_registry_exposes_owned_workflows_and_solver_contracts() -> None:
    registry = build_default_registry(ROOT)

    review = registry.roles["review"]
    assert review.workflow_usage == ("owned-execute-review-loop", "owned-benchmark-solver")
    assert review.read_mode_hint == "exact"
    assert review.review_contract is not None
    assert review.review_contract.require_first_hand_evidence is True
    assert review.review_contract.verdict_format == "json-block"
    assert review.review_contract.default_verdict == "NEEDS_FIX"
    assert {"edit", "write", "delete"} <= set(review.tool_policy.denied_actions)

    owned_loop = registry.workflows["owned-execute-review-loop"]
    assert owned_loop.stem_prompt_id == "owned-stem-system"
    assert [step.step_id for step in owned_loop.steps] == [
        "explore",
        "plan",
        "critique",
        "refine",
        "execute",
        "review",
        "fix",
    ]
    assert owned_loop.steps[1].fork_from == "explore"
    assert owned_loop.steps[2].fork_from == "plan"
    assert owned_loop.steps[3].fork_from == "critique"
    assert owned_loop.steps[4].fork_from == "refine"
    assert owned_loop.steps[5].fork_from == "refine"
    assert owned_loop.steps[6].fork_from == "review"
    assert owned_loop.steps[4].requires_plan_review is True
    assert owned_loop.steps[0].read_mode_hint == "compact"
    assert owned_loop.steps[4].read_mode_hint == "exact"
    assert owned_loop.steps[0].effort == "adaptive"
    assert owned_loop.steps[4].effort in {"medium", "high"}

    solver_loop = registry.workflows["owned-benchmark-solver"]
    assert solver_loop.stem_prompt_id == "owned-stem-system"
    assert [step.step_id for step in solver_loop.steps] == [
        "explore",
        "plan",
        "critique",
        "refine",
        "execute",
        "review",
    ]
    assert solver_loop.steps[5].fork_from == "refine"

    profile = registry.benchmark_profiles["terminalbench-owned-solver"]
    assert profile.role_id == "solve"
    assert profile.workflow_id == "owned-benchmark-solver"
    assert profile.retry_limit == 2
    assert any("isolated and disposable" in rule.lower() for rule in profile.command_rules)
    assert any("hidden evaluator" in rule.lower() for rule in profile.command_rules)
    assert any("security and ctf" in rule.lower() for rule in profile.command_rules)
    assert any("stderr" in rule.lower() for rule in profile.command_rules)
    assert any("generator" in rule.lower() for rule in profile.command_rules)
    assert any("failed command" in rule.lower() for rule in profile.command_rules)


def test_solve_role_is_general_and_benchmark_policy_is_profile_scoped() -> None:
    registry = build_default_registry(ROOT)
    solve = registry.render_prompt("solve", ROOT)

    assert "repository's validation entrypoints" in solve
    assert "terminal-bench" not in solve.lower()
    assert "hidden evaluator" not in solve.lower()
    assert "isolated and disposable" not in solve.lower()


def test_owned_runtime_prompts_stay_sharp_and_phase_bound() -> None:
    registry = build_default_registry(ROOT)

    stem = registry.render_named_prompt("owned-stem-system", ROOT)
    assert "prompt caches stay warm" in stem
    assert "Do not broaden the task" in stem
    # Confirmation policy and read mechanics are hoisted into the stem once.
    assert "local, reversible reads, edits, and tests" in stem
    assert "shared-state" in stem
    assert "retry_with" in stem

    explore = registry.render_named_prompt("owned-explore-phase", ROOT)
    assert "Read only" in explore
    assert "Do not plan" in explore
    assert "Do not edit" in explore
    assert "Do not re-read the same file" in explore

    plan = registry.render_named_prompt("owned-plan-phase", ROOT)
    assert "Do not edit" in plan
    assert "exact files" in plan.lower()
    assert "exact build/test commands" in plan
    assert "bundled steps" in plan

    critique = registry.render_named_prompt("owned-critique-phase", ROOT)
    assert "Do not edit" in critique
    assert "Attack the plan" in critique
    assert "ungrounded file, function, or utility names" in critique
    assert "significant changes with no verification" in critique

    refine = registry.render_named_prompt("owned-refine-plan-phase", ROOT)
    assert "complete final plan" in refine
    assert "not a diff" in refine

    execute = registry.render_named_prompt("owned-execute-phase", ROOT)
    assert "approved plan sequentially" in execute
    assert "Change only files named by the plan" in execute
    assert "Stop after self-verification" in execute

    review = registry.render_named_prompt("owned-review-phase", ROOT)
    assert "Do not trust the implementer's summary" in review
    assert "If evidence is missing or ambiguous, use NEEDS_FIX" in review
    assert "JSON verdict block" in review

    fix = registry.render_named_prompt("owned-fix-phase", ROOT)
    assert "FIX PHASE" in fix
    assert "Fix only cited gaps" in fix


def test_registry_host_projections_match_current_surface_set() -> None:
    registry = build_default_registry(ROOT)

    surfaced = {"auto", "bare", "code", "explore", "execute", "plan", "research", "review", "solve", "general"}
    assert set(registry.surfaced_role_ids("shared_skill")) == surfaced
    assert set(registry.surfaced_role_ids("claude_agent")) == surfaced
    assert set(registry.surfaced_role_ids("opencode_agent")) == surfaced
    assert set(registry.surfaced_role_ids("antigravity_agent")) == surfaced


def test_bootstrap_default_definitions_creates_then_skips_missing_defaults(tmp_path: Path) -> None:
    first = bootstrap_default_definitions(tmp_path, repo_root=ROOT)
    first_statuses = {entry.status for entry in first.entries}
    assert "created" in first_statuses

    manifest_path = tmp_path / "defaults" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "roles" in manifest
    assert "owned-execute-review-loop" in manifest["workflows"]
    assert "terminalbench-owned-solver" in manifest["benchmark_profiles"]

    second = bootstrap_default_definitions(tmp_path, repo_root=ROOT)
    assert second.entries
    assert {entry.status for entry in second.entries} == {"skipped"}


def test_bootstrap_default_definitions_reports_changed_and_invalid_targets(tmp_path: Path) -> None:
    bootstrap_default_definitions(tmp_path, repo_root=ROOT)

    manifest_path = tmp_path / "defaults" / "manifest.json"
    manifest_path.write_text('{"user":"changed"}\n', encoding="utf-8")

    changed = bootstrap_default_definitions(tmp_path, repo_root=ROOT)
    assert any(entry.path == manifest_path and entry.status == "changed" for entry in changed.entries)

    invalid_root = tmp_path / "not-a-directory"
    invalid_root.write_text("x", encoding="utf-8")
    invalid = bootstrap_default_definitions(invalid_root, repo_root=ROOT)
    assert any(entry.status == "invalid" for entry in invalid.entries)


def _seed_mode_docs(repo_root: Path, *, malformed: str) -> None:
    modes_dir = repo_root / "integrations" / "agents"
    modes_dir.mkdir(parents=True, exist_ok=True)
    for role_id in sorted(HOST_FACING_ROLES):
        title = role_id.replace("-", " ").title()
        (modes_dir / f"{role_id}.md").write_text(
            "---\n"
            f"mode: {role_id}\n"
            f'skill_description: "{title} skill"\n'
            f'agent_description: "{title} agent"\n'
            "---\n"
            f"# {title} mode\n\nBody.\n",
            encoding="utf-8",
        )
    (modes_dir / f"{malformed}.md").write_text("no frontmatter here\n", encoding="utf-8")


def test_build_default_registry_raises_on_malformed_mode_doc(tmp_path: Path) -> None:
    _seed_mode_docs(tmp_path, malformed="code")

    # Descriptions are single-sourced from the mode docs, so a malformed doc is a
    # hard build failure -- there is no silent fallback to stale built-in metadata.
    try:
        build_default_registry(tmp_path)
    except ValueError:
        pass
    else:
        raise AssertionError("build_default_registry must raise on a malformed mode doc")


def test_load_mode_docs_strict_raises_on_malformed_doc(tmp_path: Path) -> None:
    from atelier.core.capabilities.default_definitions import load_mode_docs

    _seed_mode_docs(tmp_path, malformed="code")

    try:
        load_mode_docs(tmp_path)
    except ValueError:
        pass
    else:
        raise AssertionError("strict load_mode_docs must raise on a malformed doc")
