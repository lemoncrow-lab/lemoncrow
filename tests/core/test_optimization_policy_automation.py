from __future__ import annotations

from pathlib import Path

import yaml

from atelier.core.capabilities.optimization.policy import (
    AutomationConfig,
    BenchmarkEvidence,
    load_optimization_config,
    preset_policy,
    save_automation_config,
    save_policy,
)


def test_save_policy_and_automation_preserve_unrelated_keys(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    root.mkdir()
    (root / "optimization.yaml").write_text(
        yaml.safe_dump(
            {
                "optimization": {
                    "preset": "balanced",
                    "shadow_consent_at": "2026-01-01T00:00:00+00:00",
                    "automation": {"enabled": False},
                },
                "routing": {"policy": "cheap_first"},
                "custom_section": {"keep": "me"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    save_policy(root, preset_policy("economy"))
    save_automation_config(
        root,
        AutomationConfig(
            enabled=True,
            minimum_projected_tokens_saved=4321,
            benchmark_evidence=BenchmarkEvidence(
                runs_path="runs.jsonl",
                baseline_cost_usd=10.0,
                candidate_cost_usd=8.0,
            ),
        ),
    )

    config = load_optimization_config(root)
    assert config["custom_section"] == {"keep": "me"}
    assert config["optimization"]["shadow_consent_at"] == "2026-01-01T00:00:00+00:00"
    assert config["optimization"]["preset"] == "economy"
    assert config["optimization"]["automation"]["enabled"] is True
    assert config["optimization"]["automation"]["minimum_projected_tokens_saved"] == 4321
