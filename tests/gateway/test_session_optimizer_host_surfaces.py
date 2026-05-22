from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

HOST_CONFIGS = ["claude", "codex", "copilot", "antigravity", "opencode"]

HOST_SURFACES = [
    ROOT / "integrations/claude/AGENTS.atelier.md",
    ROOT / "integrations/claude/plugin/agents/code.md",
    ROOT / "integrations/codex/AGENTS.atelier.md",
    ROOT / "integrations/copilot/COPILOT_INSTRUCTIONS.atelier.md",
    ROOT / "integrations/copilot/chatmodes/atelier.chatmode.md",
    ROOT / "integrations/antigravity/AGENTS.atelier.md",
    ROOT / "integrations/opencode/agents/atelier.md",
]


def test_all_host_configs_include_session_optimization_template() -> None:
    for host in HOST_CONFIGS:
        path = ROOT / f"src/atelier/gateway/hosts/configs/{host}.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        templates = {item["name"]: item["template"] for item in data["prompt_templates"]}

        assert "session-optimization" in templates
        assert "smallest viable plan" in templates["session-optimization"]
        assert "do not retry a third time" in templates["session-optimization"]


def test_direct_host_surfaces_include_budget_optimizer_guardrails() -> None:
    for path in HOST_SURFACES:
        text = path.read_text(encoding="utf-8")
        assert "Budget optimizer" in text or "Budget Optimizer" in text
        assert "smallest viable plan" in text
        assert "under 10 bullets" in text
        assert "do not retry a third time" in text


def test_direct_host_surfaces_keep_native_read_search_fallbacks() -> None:
    for path in HOST_SURFACES:
        text = path.read_text(encoding="utf-8")
        assert "`noop`" in text
        assert "Always return findings" in text
        assert "native" in text.lower()
