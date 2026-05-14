"""Central runtime environment policy.

This module owns the dev/passive mode boundary for Atelier runtime code. Keep
tool visibility, passive fallback messages, and dev-only skill lists here so
MCP, HTTP, CLI, and UI-facing metadata stay consistent.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

DEV_MODE_ENV_VAR = "ATELIER_DEV_MODE"
TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
INSTALL_PROFILES = frozenset({"stable", "dev"})

NON_DEV_LLM_TOOLS = frozenset({"task", "memory", "read", "search", "compact", "route", "rescue", "verify", "trace"})
DEV_ONLY_SKILLS = frozenset(
    {
        "analyze-failures",
        "benchmark",
        "context",
        "evals",
        "rescue",
        "savings",
        "settings",
        "status",
        "task",
        "trace",
    }
)

PASSIVE_TOOL_DESCRIPTION_SUFFIX = (
    " In non-development runtime mode this tool is passive: it is visible to MCP clients, "
    "captures the call, and returns a no-op/pass result without affecting agent decisions."
)


def bool_env(name: str, default: bool = False, env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    raw = values.get(name, "")
    if not raw:
        return default
    return raw.strip().lower() in TRUE_ENV_VALUES


def is_dev_mode(env: Mapping[str, str] | None = None) -> bool:
    return bool_env(DEV_MODE_ENV_VAR, False, env)


def passive_tool_message(tool_name: str) -> str:
    return "noop"


def cli_dev_disabled_message(command_name: str) -> str:
    return "noop"


def mcp_tool_description(description: str | None, *, is_dev: bool) -> str:
    text = str(description or "")
    if not is_dev or PASSIVE_TOOL_DESCRIPTION_SUFFIX in text:
        return text
    return f"{text}{PASSIVE_TOOL_DESCRIPTION_SUFFIX}"


def mcp_tool_visible_to_llm(tool_name: str, *, is_dev: bool) -> bool:
    if is_dev_mode():
        return True
    return tool_name in NON_DEV_LLM_TOOLS


def mcp_tool_mode(*, is_dev: bool) -> str:
    return "active" if is_dev_mode() or not is_dev else "passive"


def skill_visible(skill_name: str) -> bool:
    return is_dev_mode() or skill_name not in DEV_ONLY_SKILLS


def resolve_install_profile(env: Mapping[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    requested = values.get("ATELIER_PROFILE", "").strip()
    if requested:
        if requested not in INSTALL_PROFILES:
            raise ValueError("ATELIER_PROFILE must be 'stable' or 'dev'")
        return requested
    return "dev" if is_dev_mode(values) else "stable"


def install_profile_warning(profile: str | None = None, env: Mapping[str, str] | None = None) -> str | None:
    resolved = profile or resolve_install_profile(env)
    if resolved == "dev" and not is_dev_mode(env):
        return (
            f"ATELIER_PROFILE=dev selected without {DEV_MODE_ENV_VAR}=1; installer will stage "
            "dev artifacts, but runtime-gated dev tools remain disabled until "
            f"{DEV_MODE_ENV_VAR}=1 is set."
        )
    return None
