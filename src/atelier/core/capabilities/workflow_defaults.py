from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from atelier.core.capabilities.default_definitions import DefaultRegistry, build_default_registry


@dataclass(frozen=True)
class BootstrapEntry:
    path: Path
    status: str
    kind: str


@dataclass(frozen=True)
class BootstrapReceipt:
    entries: tuple[BootstrapEntry, ...]


def bootstrap_default_definitions(target_root: Path, *, repo_root: Path | None = None) -> BootstrapReceipt:
    if target_root.exists() and not target_root.is_dir():
        return BootstrapReceipt(entries=(BootstrapEntry(path=target_root, status="invalid", kind="target_root"),))

    defaults_root = target_root / "defaults"
    defaults_root.mkdir(parents=True, exist_ok=True)

    registry = build_default_registry(repo_root)
    files: list[tuple[Path, str, str]] = [
        (defaults_root / "manifest.json", _json_text(_manifest_index(registry)), "manifest"),
    ]

    for role_id, role in registry.roles.items():
        files.append((defaults_root / "roles" / f"{role_id}.json", _json_text(role.to_dict()), "role"))
    for prompt_id, prompt in registry.prompts.items():
        files.append((defaults_root / "prompts" / f"{prompt_id}.md", prompt.render(repo_root), "prompt"))
    for workflow_id, workflow in registry.workflows.items():
        files.append(
            (
                defaults_root / "workflows" / f"{workflow_id}.json",
                _json_text(workflow.to_dict()),
                "workflow",
            )
        )
    for profile_id, profile in registry.benchmark_profiles.items():
        files.append(
            (
                defaults_root / "benchmark_profiles" / f"{profile_id}.json",
                _json_text(profile.to_dict()),
                "benchmark_profile",
            )
        )
    for template_id, template in registry.mcp_templates.items():
        files.append(
            (
                defaults_root / "mcp_templates" / f"{template_id}.json",
                _json_text(template.to_dict()),
                "mcp_template",
            )
        )

    entries: list[BootstrapEntry] = []
    for path, content, kind in files:
        entries.append(_write_if_missing(path, content, kind))
    return BootstrapReceipt(entries=tuple(entries))


def _manifest_index(registry: DefaultRegistry) -> dict[str, object]:
    return {
        "roles": sorted(registry.roles),
        "prompts": sorted(registry.prompts),
        "workflows": sorted(registry.workflows),
        "benchmark_profiles": sorted(registry.benchmark_profiles),
        "mcp_templates": sorted(registry.mcp_templates),
    }


def _json_text(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _write_if_missing(path: Path, content: str, kind: str) -> BootstrapEntry:
    if path.exists():
        if path.read_text(encoding="utf-8") == content:
            return BootstrapEntry(path=path, status="skipped", kind=kind)
        return BootstrapEntry(path=path, status="changed", kind=kind)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return BootstrapEntry(path=path, status="created", kind=kind)


__all__ = ["BootstrapEntry", "BootstrapReceipt", "bootstrap_default_definitions"]
