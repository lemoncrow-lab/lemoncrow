"""Central runtime visibility policy.

This module owns the always-on public tool/skill surface for LemonCrow runtime
code. Keep hardcoded hidden lists here so MCP, HTTP, CLI, and UI-facing
metadata stay consistent without a separate dev-mode branch.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

MEMORY_BACKEND_ENV_VAR = "LEMONCROW_MEMORY_BACKEND"
TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
MEMORY_BACKENDS = frozenset({"sqlite", "letta", "openmemory"})

HIDDEN_LLM_TOOLS = frozenset(
    {
        # Single-primary retrieval surface: `explore` (ranked source + call-graph
        # relations + blast-radius in one call) and `read` are the only advertised
        # retrieval tools. `grep` and `relations` stay registered and callable
        # (escape hatch / internal routing / drill-in) but are hidden so the agent
        # leads with `explore` instead of flailing on regex grep.
        "grep",
        "relations",
        # Skill-only / orchestration tools: named MCP tools not surfaced to agents.
        "agent",
        "workflow",
        # Internal / CLI-only IPC and lifecycle tools.
        "statusline_segment",
        "rescue",
        "verify",
        "trace",
        "compact",
        "context",
        # WS4 graph analytics (blast radius / dead code / cycles / coupling /
        # symbol centrality): registered and callable by name, but kept off the
        # advertised surface to preserve the lean public tool set.
        "graph",
        # WS8 G11 security scan (SAST first iteration): callable by name but kept
        # off the advertised surface to preserve the lean public tool set.
        "scan",
        # WS12 N8 on-demand tool-usage playbook: callable by name so the
        # orientation guidance lives in one fetch, but kept off the advertised
        # surface to preserve the lean public tool set.
        "orient",
        # Repo/admin code-intel ops: callable by name (tests, CLI, power use)
        # but not surfaced to agents.
        "index",
        "blame",
        # Code-intel cache admin (status + invalidate) folded into one tool.
        "cache",
        # Semantic/embedding search: registered and callable, but hidden until an
        # embedding backend is wired up. Deterministic search (regex/glob, symbol
        # locate/relations, repo-map) lives on `grep`; when embeddings land,
        # remove `search` here to surface it as the 6th visible tool.
        "search",
        # MCP proxy for OTHER configured stdio MCP servers: registered and
        # callable by name (tests, CLI, adopt-mode plumbing) but off the
        # advertised surface — shadow mode shrinks host-lane MCP outputs via
        # hooks and needs no visible tool. Remove here when adopt mode lands
        # so the proxy becomes the advertised route to adopted servers.
        "mcp",
        # Power/admin surfaces kept off the lean agent surface: durable memory
        # writes, raw SQL, and AST-shape codemod. Callable by name (tests, CLI,
        # power use) but not advertised to agents.
        "memory",
        "sql",
        "codemod",
    }
)
HIDDEN_SKILLS: frozenset[str] = frozenset()
# Public skills that ship by default when installing a host. `lc` (the
# on-demand install/remove/list discovery skill, integrations/skills/lemoncrow/)
# is the sole default-shipped entry -- it is how a user discovers the rest of
# the opt-in surface in the first place. The 6 optional public skills
# (benchmark/orchestrate/perf-review/recall/swarm/ux-review) stay out of this
# set -- distinct from HIDDEN_SKILLS, which is permanently excluded regardless
# of any opt-in. `lc skill install <name>` passes an explicit superset
# (see agents_skills.py) to opt individual public skills into a given host.
DEFAULT_SKILLS: frozenset[str] = frozenset({"lemoncrow"})


def bool_env(name: str, default: bool = False, env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    raw = values.get(name, "")
    if not raw:
        return default
    return raw.strip().lower() in TRUE_ENV_VALUES


def mcp_tool_description(tool_name: str, description: str | None) -> str:
    return str(description or "")


def _extra_hidden_tools(env: Mapping[str, str] | None = None) -> frozenset[str]:
    # Opt-in lean surface: LEMONCROW_HIDE_TOOLS=node,sql,... hides extra tools from
    # the LLM surface (smaller per-turn schema, less tool-choice deliberation)
    # without touching the always-hidden baseline set.
    values = os.environ if env is None else env
    raw = values.get("LEMONCROW_HIDE_TOOLS", "")
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def mcp_tool_visible_to_llm(tool_name: str) -> bool:
    # Bench-off overrides the normal public surface — the baseline arm must not
    # see LemonCrow MCP tools. Imported lazily so reading runtime config does not
    # couple core to the optional bench package at module load time.
    from lemoncrow.bench.mode import is_off as _bench_is_off

    if _bench_is_off():
        return False
    if tool_name in HIDDEN_LLM_TOOLS:
        return False
    return tool_name not in _extra_hidden_tools()


def mcp_tool_mode(tool_name: str) -> str:
    return "hidden" if tool_name in HIDDEN_LLM_TOOLS else "active"


def skill_visible(skill_name: str) -> bool:
    return skill_name not in HIDDEN_SKILLS


def skill_installed_by_default(skill_name: str, *, default_skills: frozenset[str] | None = None) -> bool:
    """Whether a skill should render/copy during a default install.

    Hidden (permanently dev-only) skills are always excluded, regardless of
    `default_skills`. Otherwise gated by `DEFAULT_SKILLS` unless a caller
    passes an explicit superset -- the hook a future on-demand installer uses
    to opt individual public skills in.
    """
    if not skill_visible(skill_name):
        return False
    allowed = DEFAULT_SKILLS if default_skills is None else default_skills
    return skill_name in allowed


def resolve_install_profile(env: Mapping[str, str] | None = None) -> str:
    return "stable"


def install_profile_warning(profile: str | None = None, env: Mapping[str, str] | None = None) -> str | None:
    return None


def resolve_memory_backend(
    *,
    root: str | Path | None = None,
    prefer: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    values = os.environ if env is None else env

    env_backend = values.get(MEMORY_BACKEND_ENV_VAR, "").strip().lower()
    if env_backend:
        return _validated_memory_backend(env_backend)

    if root is not None and tomllib is not None:
        config_path = Path(root) / "config.toml"
        if config_path.exists():
            try:
                data = tomllib.loads(config_path.read_text(encoding="utf-8"))
                memory = data.get("memory", {}) if isinstance(data, dict) else {}
                config_backend = str(memory.get("backend", "")).strip().lower()
                if config_backend:
                    return _validated_memory_backend(config_backend)
            except (tomllib.TOMLDecodeError, OSError, ValueError):
                # Keep runtime robust; invalid config falls back to defaults.
                logger.warning("Invalid config.toml; falling back to defaults", exc_info=True)

    fallback = (prefer or "sqlite").strip().lower()
    return _validated_memory_backend(fallback)


def _validated_memory_backend(value: str) -> str:
    if value not in MEMORY_BACKENDS:
        allowed = ", ".join(sorted(MEMORY_BACKENDS))
        raise ValueError(f"memory backend must be one of: {allowed}")
    return value
