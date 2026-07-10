from __future__ import annotations

import json
from pathlib import Path

from atelier.core.capabilities.knowledge_extract import (
    extract_rules,
    gather_sources,
    merge_into_overlay,
    parse_rules,
    preflight_cost_usd,
)


def _lessons(repo: Path, *texts: str) -> None:
    blocks = repo / ".atelier" / "lessons" / "blocks"
    blocks.mkdir(parents=True)
    for i, text in enumerate(texts):
        (blocks / f"l{i}.md").write_text(text, encoding="utf-8")


def test_gather_sources_reads_lessons(tmp_path: Path) -> None:
    _lessons(tmp_path, "# Lesson A\nbody", "# Lesson B\nbody")
    assert len(gather_sources(tmp_path)) == 2


def test_gather_sources_none(tmp_path: Path) -> None:
    assert gather_sources(tmp_path) == []


def test_parse_rules_json_array_dedup() -> None:
    out = parse_rules('noise ["check authz", "no globals", "check authz"] tail')
    assert out == ["check authz", "no globals"]


def test_parse_rules_bullets_fallback() -> None:
    out = parse_rules("- first rule here\n* second rule here\n1. third rule here")
    assert "first rule here" in out and "third rule here" in out


def test_estimate_cost_free_for_ollama() -> None:
    assert preflight_cost_usd("x" * 1000, "ollama", "llama3") == 0.0


def test_estimate_cost_paid_for_auto() -> None:
    assert preflight_cost_usd("x" * 4000, "auto", "") > 0.0


def test_merge_into_overlay_dedups(tmp_path: Path) -> None:
    (tmp_path / "review_overlay.json").write_text(
        json.dumps({"notes": ["existing"], "boost": [], "suppress": []}), encoding="utf-8"
    )
    added = merge_into_overlay(tmp_path, ["existing", "brand new rule"])
    assert added == 1
    data = json.loads((tmp_path / "review_overlay.json").read_text(encoding="utf-8"))
    assert "brand new rule" in data["notes"]
    assert data["notes"].count("existing") == 1


def test_merge_into_overlay_reports_capped_persisted_count(tmp_path: Path) -> None:
    # Pre-fill well past the overlay bound and add many distinct new rules. The
    # overlay is capped (merge _OVERLAY_NOTES_CAP; load also truncates), so the
    # reported count must reflect what actually fit, not len(added).
    prefill = [f"rule {i}" for i in range(100)]
    (tmp_path / "review_overlay.json").write_text(
        json.dumps({"notes": prefill, "boost": [], "suppress": []}), encoding="utf-8"
    )
    new_rules = [f"new rule {i}" for i in range(100)]
    persisted = merge_into_overlay(tmp_path, new_rules)
    # The bug returned len(new_rules)=100; the fix returns the capped count.
    assert 0 <= persisted < len(new_rules)


def test_extract_rules_applies_with_stub_runner(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _lessons(repo, "# Always validate inputs at boundaries")
    root = tmp_path / "root"
    root.mkdir()

    def runner(prompt: str, *, host: str, model: str, root: object) -> str:
        return '["Validate inputs at boundaries", "Prefer dependency injection"]'

    result = extract_rules(root, repo, host="ollama", model="llama3", runner=runner)
    assert result["applied"] == 2
    assert result["scope"] == "repo"
    assert "Validate inputs at boundaries" in result["rules"]
    # Default scope=repo writes the team overlay in the repo (committable/shared).
    overlay = json.loads((repo / ".atelier" / "review.json").read_text(encoding="utf-8"))
    assert "Prefer dependency injection" in overlay["notes"]
    # A managed allow-list is emitted so the team overlay is committable.
    assert (repo / ".atelier" / ".gitignore").exists()


def test_extract_rules_personal_scope_writes_user_overlay(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _lessons(repo, "# A lesson about error handling here")
    root = tmp_path / "root"
    root.mkdir()
    result = extract_rules(
        root, repo, host="ollama", model="m", scope="personal", runner=lambda *a, **k: '["personal rule here"]'
    )
    assert result["scope"] == "personal"
    assert (root / "review_overlay.json").exists()
    assert not (repo / ".atelier" / "review.json").exists()


def test_extract_rules_dry_run_does_not_write(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _lessons(repo, "# lesson content here for distillation")
    root = tmp_path / "root"
    root.mkdir()
    result = extract_rules(
        root, repo, host="ollama", model="m", dry_run=True, runner=lambda *a, **k: '["rule one here"]'
    )
    assert result["rules"] == ["rule one here"]
    assert result["applied"] == 0
    assert not (root / "review_overlay.json").exists()


def test_extract_rules_spend_cap_aborts_before_spending(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _lessons(repo, "x" * 5000)
    root = tmp_path / "root"
    root.mkdir()
    called = {"n": 0}

    def runner(prompt: str, *, host: str, model: str, root: object) -> str:
        called["n"] += 1
        return "[]"

    result = extract_rules(root, repo, host="auto", model="claude-opus-4", max_spend_usd=0.0, runner=runner)
    assert "exceeds cap" in result.get("reason", "")
    assert called["n"] == 0


def test_extract_rules_no_sources(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    result = extract_rules(root, tmp_path / "empty", runner=lambda *a, **k: "[]")
    assert result["reason"] == "no .atelier/lessons/blocks found"
