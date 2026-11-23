"""Catalog, detection, recipe and session primitives for interactive previews (Phase 9R item 16)."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from lemoncrow.pro.capabilities.review.capability_catalog import PRESET_SELECTIONS, STATIC_CATALOG, detect_capabilities
from lemoncrow.pro.capabilities.review.interactive_preview_api import remaining_minutes, static_framework
from lemoncrow.pro.capabilities.review.preview_runtime import PreviewRecipeError, load_preview_recipe, static_recipe
from lemoncrow.pro.capabilities.review.preview_sessions import (
    INTERACTIVE_PREVIEW_ENTRY,
    MAX_TTL_SECONDS,
    MIN_SESSION_SECONDS,
    PreviewSession,
    comparability,
    new_token,
    token_digest,
    token_matches,
)
from lemoncrow.pro.capabilities.review.run_plan import BudgetUsage, execution_class
from lemoncrow.pro.environment.execution import EnvironmentFingerprint


def test_preview_catalog_policy_contract() -> None:
    """case: test_catalog_entry_is_deep_manual_only_runtime_work"""
    entry = STATIC_CATALOG[INTERACTIVE_PREVIEW_ENTRY]
    assert entry.family == "visual"
    assert entry.requires == "previewable_app"
    assert entry.executor == "interactive_preview"
    assert entry.minimum_minutes >= 10
    assert execution_class(INTERACTIVE_PREVIEW_ENTRY) == "runtime"
    presets = {name: {row.id: row for row in rows} for name, rows in PRESET_SELECTIONS.items()}
    assert presets["deep"][INTERACTIVE_PREVIEW_ENTRY].when == "manual"
    assert all((INTERACTIVE_PREVIEW_ENTRY not in rows for name, rows in presets.items() if name != "deep"))
    "case: test_declared_command_in_the_trusted_base_is_previewable"
    files = {".lemoncrow/review.yaml": "interactive_preview:\n  command: [npm, start]\n  port: 3000\n"}
    assert detect_capabilities(files, files.get).has("previewable_app")
    "case: test_other_projects_are_not_previewable"
    for files in [
        {"package.json": json.dumps({"dependencies": {"react": "18"}})},
        {".lemoncrow/review.yaml": "interactive_preview:\n  port: 3000\n"},
        {".lemoncrow/review.yaml": "interactive_preview: yes\n"},
        {"src/app.py": "print('hi')\n"},
    ]:
        assert not detect_capabilities(files, files.get).has("previewable_app")


def test_preview_recipe_contract() -> None:
    """case: test_recipe_parses_and_bounds_the_trusted_base_block"""
    recipe = load_preview_recipe(
        "interactive_preview:\n  command: [npm, run, start]\n  port: 3000\n  root: web\n  ttl_minutes: 60\n  start_seconds: 9999\n  ready_path: /health\n"
    )
    assert recipe is not None
    assert recipe.mode == "command" and recipe.command == ("npm", "run", "start") and (recipe.port == 3000)
    assert recipe.root == "web" and recipe.ready_path == "/health"
    assert recipe.ttl_cap_seconds == MAX_TTL_SECONDS
    assert recipe.start_seconds <= 300
    assert load_preview_recipe("review:\n  effort: auto\n") is None
    assert load_preview_recipe(None) is None
    "case: test_invalid_recipes_are_refused"
    for block in [
        "interactive_preview:\n  port: 3000\n",
        "interactive_preview:\n  command: [npm, start]\n  port: 80\n",
        "interactive_preview:\n  command: [npm, start]\n  port: 3000\n  ready_path: http://169.254.169.254/\n",
        "interactive_preview:\n  command: [npm, start]\n  port: 3000\n  root: ../outside\n",
        "interactive_preview: [npm, start]\n",
    ]:
        with pytest.raises(PreviewRecipeError):
            load_preview_recipe(block)
    "case: test_recipe_digest_identifies_the_recipe"
    one = static_recipe("vite")
    assert one.digest == static_recipe("vite").digest
    assert one.digest != static_recipe("next").digest
    "case: test_static_framework_prefers_next_then_astro_then_vite"
    assert static_framework(json.dumps({"dependencies": {"vite": "5", "next": "14"}})) == "next"
    assert static_framework(json.dumps({"devDependencies": {"vite": "5"}})) == "vite"
    assert static_framework("not json") == ""
    assert static_framework(None) == ""


def test_preview_session_contract() -> None:
    """case: test_same_environment_fingerprint_is_comparable_otherwise_labeled"""
    same = EnvironmentFingerprint(image_digest="host:sha256:" + "1" * 64, lockfile_digest="a").to_wire()
    assert comparability({"old": same, "new": same})[0] is True
    other = EnvironmentFingerprint(image_digest="host:sha256:" + "1" * 64, lockfile_digest="b").to_wire()
    comparable, why = comparability({"old": same, "new": other})
    assert comparable is False and "lockfile" in why
    assert comparability({"old": same})[0] is False
    "case: test_token_is_bound_to_its_session"
    token = new_token()
    session = PreviewSession(
        id="ips_one",
        review_id="r1",
        revision_id="v1",
        viewer_id="alice",
        token_digest=token_digest("ips_one", token),
        created_at=0.0,
        expires_at=MIN_SESSION_SECONDS,
        ttl_seconds=MIN_SESSION_SECONDS,
        state="running",
        run_on="local",
        executor_class="author_machine",
    )
    assert token_matches(session, token)
    assert not token_matches(session, token + "x")
    assert not token_matches(session, "")
    assert not token_matches(replace(session, id="ips_two"), token)
    assert token_digest("ips_two", token) != session.token_digest
    assert token not in json.dumps(session.to_wire(durable=True))
    assert "token_digest" not in session.to_wire()
    "case: test_remaining_minutes_uses_the_one_visible_budget"
    usage = BudgetUsage(review_minutes=4.0, monthly_minutes=95.0)
    assert remaining_minutes({}, usage) is None
    assert remaining_minutes({"per_review_minutes": 10}, usage) == pytest.approx(6.0)
    assert remaining_minutes({"per_review_minutes": 10, "monthly_minutes": 100}, usage) == pytest.approx(5.0)


@pytest.mark.parametrize("framework", ["next", "astro", "vite"])
def test_static_web_frameworks_in_the_trusted_base_are_previewable(framework: str) -> None:
    files = {"web/package.json": json.dumps({"devDependencies": {framework: "1.0.0"}})}
    detected = detect_capabilities(files, files.get)
    assert detected.has("previewable_app")
    assert detected.evidence["previewable_app"] == "web/package.json static web build"
