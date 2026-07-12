#!/usr/bin/env python3
"""Generate host instruction surfaces from the live Agent OS docs."""

from __future__ import annotations

# ruff: noqa: E402
import json
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import takewhile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lemoncrow.core.capabilities.default_definitions import (
    HOST_ROLE_IDS,
    DefaultRole,
    HostProjection,
    ModeDoc,
    build_default_registry,
    load_mode_docs,
)
from lemoncrow.core.capabilities.model_settings import (
    CANONICAL_COPILOT_AGENT_MODEL,
    normalize_model_for_host,
    resolve_explicit_host_model,
    resolve_host_model,
)
from lemoncrow.core.capabilities.workspace_host_overrides import (
    CODEX_NATIVE_FALLBACK_NAMES_READ,
    codex_tool_discipline_body,
    core_discipline_body,
    format_native_names_and_verb,
    replace_inline_tool_names,
    rewrite_agent_model,
    swap_tool_discipline_lead_in,
)
from lemoncrow.core.environment import skill_installed_by_default

CODING_GUIDELINES_PATH = ROOT / "integrations/agents/shared/coding-guidelines.md"
CORE_DISCIPLINE_PATH = ROOT / "integrations/agents/shared/core-discipline.md"
CHANGE_DISCIPLINE_PATH = ROOT / "integrations/agents/shared/change-discipline.md"
DESTRUCTIVE_GUARD_PATH = ROOT / "integrations/agents/shared/destructive-guard.md"
RESPONSE_ECONOMY_PATH = ROOT / "integrations/agents/shared/response-economy.md"
TOOL_DISCIPLINE_PATH = ROOT / "integrations/agents/shared/tool-discipline.md"
TOOL_DISCIPLINE_READ_PATH = ROOT / "integrations/agents/shared/tool-discipline-read.md"
REPLY_REGISTER_PATH = ROOT / "integrations/agents/shared/reply-register.md"
AGENT_RULE_PATH = ROOT / "integrations/agents/shared/agent-rule.md"
AGENTS_GUIDE_PATH = ROOT / "integrations/AGENTS.lemoncrow.md"

# Bare ``{{TOKEN}}`` placeholders a mode doc may embed; each expands verbatim
# from one canonical partial. A mode opts in by including the token anywhere
# in its body.
SHARED_SECTIONS: dict[str, Path] = {
    "{{CODING_GUIDELINES}}": CODING_GUIDELINES_PATH,
    "{{CORE_DISCIPLINE}}": CORE_DISCIPLINE_PATH,
    "{{CHANGE_DISCIPLINE}}": CHANGE_DISCIPLINE_PATH,
    "{{DESTRUCTIVE_GUARD}}": DESTRUCTIVE_GUARD_PATH,
    "{{RESPONSE_ECONOMY}}": RESPONSE_ECONOMY_PATH,
    "{{TOOL_DISCIPLINE}}": TOOL_DISCIPLINE_PATH,
    "{{TOOL_DISCIPLINE_READ}}": TOOL_DISCIPLINE_READ_PATH,
    "{{REPLY_REGISTER}}": REPLY_REGISTER_PATH,
    "{{AGENT_RULE}}": AGENT_RULE_PATH,
}
HOST_SKILL_DIRS = {
    "claude": ROOT / "integrations" / "claude" / "plugin" / "skills",
    "codex": ROOT / "integrations" / "codex" / "plugin" / "skills",
    "antigravity": ROOT / "integrations" / "antigravity" / "skills",
}
# Hosts where role-level skills are the primary injection mechanism.
# Hosts with a native session-agent concept (Claude, Antigravity) use agents
# for mode-switching and don't need role skills — only non-role extras go there.
ROLE_SKILL_HOSTS: frozenset[str] = frozenset({"codex"})


def _strip_leading_title(text: str) -> str:
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    return "\n".join(lines).rstrip()


def _markdown_body(path: Path) -> str:
    return _strip_leading_title(path.read_text(encoding="utf-8"))


def coding_guidelines_section() -> str:
    return "\n".join(["## Coding Guidelines", "", _markdown_body(CODING_GUIDELINES_PATH)])


# Bare user-scope server name ("lc", registered by install_claude.sh) — the
# canonical local install. The marketplace plugin shape is
# "mcp__plugin_lemoncrow_lc__"; runtime consumers (hooks, session parsers)
# accept both, and the deny-list covers both (see _claude_disallowed_tools).
_CLAUDE_TOOL_PREFIX = "mcp__lc__"
_OPENCODE_TOOL_PREFIX = "lc_"
_CODEX_TOOL_PREFIX = "lc."

# Claude Code folds the MCP server's `instructions` field (SERVER_INSTRUCTIONS
# in mcp_server.py, which carries the full generic tool discipline) into every
# context — main agent and subagents alike. So claude personas ship only what
# that host-agnostic server string cannot carry: the host tool-name mapping.
# Every other surface (codex — openai/codex#6148 closed not-planned — opencode,
# copilot, cursor, and owned lanes, none of which receive MCP instructions)
# keeps the full shared block.
_CLAUDE_TOOL_DISCIPLINE = "Host tools disabled — use lc: `bash`, `read`, `edit`, `code_search`."
_CLAUDE_TOOL_DISCIPLINE_READ = (
    "- **Read-only role — `bash` never mutates.** Inspection and validation only, "
    "no redirects into the tree, no `sed -i`/`tee`, no git state changes.\n"
    "\n"
    "Host tools disabled — use lc: `bash`, `read`, `code_search`."
)
_CLAUDE_SHARED_OVERRIDES = {
    "{{TOOL_DISCIPLINE}}": _CLAUDE_TOOL_DISCIPLINE,
    "{{TOOL_DISCIPLINE_READ}}": _CLAUDE_TOOL_DISCIPLINE_READ,
}


# OpenCode's own native tools (read/grep/bash/edit/patch) stay fully enabled --
# no permission-deny for them (see scripts/install_opencode.sh's opencode.json,
# only "lc_*": "allow" is ever set) -- so "Host tools disabled" is as
# inaccurate for OpenCode as it is for Codex. Same swap-the-lead-in fix,
# keeping the "use lc: ..." clause (same shared helper Codex uses).
#
# Unlike Codex's apply_patch/exec_command, these names (read/bash/edit) DO
# collide with INLINE_TOOL_NAMES -- render_agent's replace_inline_tool_names
# pass would mangle them into lc_read/lc_bash/lc_edit (they'd
# read as OpenCode's own native tools, not LemonCrow's). So the {{NATIVE_FALLBACK_NAMES}}
# marker below is deliberately left unresolved here and filled in by render_agent
# *after* that rewrite pass runs (see profile.native_fallback_names).
_OPENCODE_NATIVE_FALLBACK_NAMES: tuple[str, ...] = ("read", "grep", "bash", "edit", "patch")
_OPENCODE_TOOL_DISCIPLINE = swap_tool_discipline_lead_in(
    _markdown_body(TOOL_DISCIPLINE_PATH),
    "Native OpenCode {{NATIVE_FALLBACK_NAMES}} are fallback-only "
    "(use them only when the LemonCrow equivalent is hidden, unavailable, or returns noop)",
)
_OPENCODE_SHARED_OVERRIDES = {"{{TOOL_DISCIPLINE}}": _OPENCODE_TOOL_DISCIPLINE}
# Codex has no tool-permission-deny mechanism either (native apply_patch/
# exec_command stay fully callable; only a reactive PostToolUse nudge exists --
# see plugin_runtime._codex_native_tool_replacement), so the generic "Host
# tools disabled" closing line is inaccurate for Codex the same way it was for
# OpenCode. codex_tool_discipline_body names Codex's real native tools instead.
_CODEX_SHARED_OVERRIDES = {
    "{{TOOL_DISCIPLINE}}": codex_tool_discipline_body(TOOL_DISCIPLINE_PATH.parent),
    "{{TOOL_DISCIPLINE_READ}}": codex_tool_discipline_body(
        TOOL_DISCIPLINE_PATH.parent,
        source_name="tool-discipline-read.md",
        native_fallback_names=CODEX_NATIVE_FALLBACK_NAMES_READ,
    ),
}


@dataclass(frozen=True)
class HostInstructionProfile:
    """Per-host knobs consumed by render_agent.

    Bundles what used to be separate render_agent kwargs so a host's tool
    identity travels as one object instead of a growing parameter list.

    tool_prefix : str
        Prefix LemonCrow MCP tools are registered under by the host, e.g.
        ``lc_`` (OpenCode), ``mcp__lc__`` (Claude Code user-scope server).
    overrides : dict[str, str] | None
        Shared-section token overrides (e.g. ``{{TOOL_DISCIPLINE}}``), expanded by
        render_mode_body before the prefix rewrite.
    native_fallback_names : tuple[str, ...]
        The host's own native tool names, filled into a literal
        ``{{NATIVE_FALLBACK_NAMES}}`` marker an override may embed. Resolved
        *after* replace_inline_tool_names -- required whenever a native name
        collides with INLINE_TOOL_NAMES (e.g. OpenCode's read/bash/edit), since
        baking it into the override string directly would get it wrongly
        rewritten with tool_prefix during that pass. Codex's apply_patch/
        exec_command don't collide, so codex_tool_discipline_body bakes them in
        directly instead of using this marker -- use whichever fits.
    host_instruction : str
        Extra host-only instruction appended verbatim after the rendered body.
    """

    tool_prefix: str
    overrides: dict[str, str] | None = None
    native_fallback_names: tuple[str, ...] = ()
    host_instruction: str = ""


def agent_guide() -> str:
    return AGENTS_GUIDE_PATH.read_text(encoding="utf-8").strip()


def render_managed_context(existing: str) -> str:
    block_start = "<!-- LEMONCROW START -->"
    block_end = "<!-- LEMONCROW END -->"
    body = agent_guide()
    managed = "\n".join([block_start, body, block_end])
    existing = existing.rstrip()

    if existing.strip() == body:
        updated = managed
    elif block_start in existing:
        before, _, remainder = existing.partition(block_start)
        _, found_end, after = remainder.partition(block_end)
        if not found_end:
            raise ValueError(f"missing {block_end} in managed instruction file")
        updated = f"{before}{managed}{after}".rstrip()
    elif block_end in existing:
        raise ValueError(f"missing {block_start} in managed instruction file")
    elif existing:
        updated = f"{existing}\n\n---\n\n{managed}"
    else:
        updated = managed

    return updated + "\n"


def _copilot_native_tools(role_id: str) -> list[str]:
    base = [
        "lemoncrow/*",
        "search/codebase",
        "web/fetch",
        "findTestFiles",
        "web/githubRepo",
        "read/problems",
        "read/getTaskOutput",
        "search",
        "searchResults",
        "read/terminalLastCommand",
        "read/terminalSelection",
        "search/usages",
        "vscode/vscodeAPI",
    ]
    if role_id in {"code", "execute", "solve", "auto", "bare", "general"}:
        base[1:1] = [
            "changes",
            "edit/editFiles",
            "execute/getTerminalOutput",
            "execute/runInTerminal",
            "execute/createAndRunTask",
            "execute/runTask",
            "execute/runTests",
            "execute/testFailure",
        ]
    return base


def render_copilot_agent(role: DefaultRole, mode_doc: ModeDoc, projection: HostProjection) -> str:
    tools = "\n".join(f'    "{tool}",' for tool in _copilot_native_tools(role.role_id))
    return (
        "\n".join(
            [
                "---",
                f'description: "{role.agent_description}"',
                f"model: {CANONICAL_COPILOT_AGENT_MODEL}",
                "tools:",
                "  [",
                tools,
                "  ]",
                "---",
                "",
                f"# lemoncrow:{role.role_id}",
                "",
                f"You are operating as *lemoncrow:{role.role_id}*.",
                "",
                render_mode_body(mode_doc),
            ]
        ).rstrip()
        + "\n"
    )


def render_cursor_coding_rules() -> str:
    return (
        "\n".join(
            [
                "---",
                "description: Behavioral guidelines to reduce common LLM coding mistakes."
                " Use when writing, reviewing, or refactoring code to avoid overcomplication,"
                " make surgical changes, surface assumptions, and define verifiable success criteria.",
                "alwaysApply: true",
                "---",
                "",
                coding_guidelines_section().strip(),
            ]
        ).rstrip()
        + "\n"
    )


def render_cursor_role_rule(role: DefaultRole, mode_doc: ModeDoc) -> str:
    return (
        "\n".join(
            [
                "---",
                f"description: LemonCrow {role.role_id} mode reference for Cursor.",
                "---",
                "",
                render_mode_body(mode_doc),
            ]
        ).rstrip()
        + "\n"
    )


def _already_active_guard(skill_name: str) -> str:
    """One-line blockquote that tells the model the skill is already loaded."""
    return f'> **Active** — do not call `Skill("lemoncrow:{skill_name}")` again.'


def _inject_active_guard(content: str, skill_name: str) -> str:
    """Insert the already-active guard after the YAML frontmatter block."""
    guard = _already_active_guard(skill_name)
    lines = content.splitlines(keepends=True)
    in_fm = False
    end_idx: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == "---":
            if not in_fm:
                in_fm = True
            else:
                end_idx = i
                break
    if end_idx is None:
        return guard + "\n\n" + content
    before = "".join(lines[: end_idx + 1])
    after_lines = lines[end_idx + 1 :]
    # Strip only the contiguous leading blank lines that follow the frontmatter close.
    skip = sum(1 for _ in takewhile(lambda ln: not ln.strip(), after_lines))
    after = "".join(after_lines[skip:])
    return before + "\n" + guard + "\n\n" + after


def render_shared_skill(role: DefaultRole, mode_doc: ModeDoc) -> str:
    body = replace_inline_tool_names(render_mode_body(mode_doc, _CODEX_SHARED_OVERRIDES), _CODEX_TOOL_PREFIX)
    return (
        "\n".join(
            [
                "---",
                f"name: {role.role_id}",
                f"description: {role.skill_description}",
                "---",
                "",
                _already_active_guard(role.role_id),
                "",
                body,
            ]
        ).rstrip()
        + "\n"
    )


def render_mode_body(mode_doc: ModeDoc, overrides: dict[str, str] | None = None) -> str:
    body = _strip_leading_title(mode_doc.body)
    for token, source_path in SHARED_SECTIONS.items():
        if token in body:
            replacement = (overrides or {}).get(token) or _shared_section_body(token, source_path)
            body = body.replace(token, replacement)
    return body


def _shared_section_body(token: str, source_path: Path) -> str:
    """Expand one shared partial. ``{{CORE_DISCIPLINE}}`` also carries the
    response-economy directive (byte-exact technical content + expand-for-safety)."""
    if token == "{{CORE_DISCIPLINE}}":
        return core_discipline_body(source_path.parent)
    return _markdown_body(source_path)


def _format_frontmatter_value(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render_frontmatter(items: list[tuple[str, Any]]) -> str:
    lines = ["---"]
    for key, value in items:
        lines.append(f"{key}: {_format_frontmatter_value(value)}")
    lines.append("---")
    return "\n".join(lines)


def _inject_description(frontmatter: tuple[tuple[str, Any], ...], description: str) -> list[tuple[str, Any]]:
    rendered: list[tuple[str, Any]] = []
    for key, value in frontmatter:
        rendered.append((key, description if key == "description" and value == "" else value))
    return rendered


def render_claude_agent(role: DefaultRole, mode_doc: ModeDoc, projection: HostProjection) -> str:
    frontmatter = _inject_description(projection.frontmatter, role.agent_description)
    body = replace_inline_tool_names(render_mode_body(mode_doc, _CLAUDE_SHARED_OVERRIDES), _CLAUDE_TOOL_PREFIX)
    return "\n".join([render_frontmatter(frontmatter), "", body]).rstrip() + "\n"


def render_simple_agent(role: DefaultRole, mode_doc: ModeDoc, projection: HostProjection) -> str:
    identity_block = ["You are operating as *lemoncrow:code*.", ""] if role.role_id == "code" else []
    return (
        "\n".join(
            [
                render_frontmatter(_inject_description(projection.frontmatter, role.agent_description)),
                "",
                *identity_block,
                render_mode_body(mode_doc),
            ]
        ).rstrip()
        + "\n"
    )


def render_agent(
    role: DefaultRole,
    mode_doc: ModeDoc,
    projection: HostProjection,
    *,
    profile: HostInstructionProfile,
) -> str:
    """Host agent renderer driven by a HostInstructionProfile.

    Different MCP hosts expose LemonCrow tools under different name prefixes.
    This renderer expands shared sections and rewrites bare tool names to the
    host's prefix so agents know the exact tool names to call.
    """
    p = profile.tool_prefix
    identity_block = ["You are operating as *lemoncrow:code*.", ""] if role.role_id == "code" else []
    body = replace_inline_tool_names(render_mode_body(mode_doc, profile.overrides), p)
    if profile.native_fallback_names:
        names, _verb = format_native_names_and_verb(profile.native_fallback_names)
        body = body.replace("{{NATIVE_FALLBACK_NAMES}}", names)
    if profile.host_instruction:
        body = f"{body}\n\n{profile.host_instruction}"
    return (
        "\n".join(
            [
                render_frontmatter(_inject_description(projection.frontmatter, role.agent_description)),
                "",
                *identity_block,
                body,
            ]
        ).rstrip()
        + "\n"
    )


def _extra_shared_skill_paths(repo_root: Path, generated_role_ids: set[str]) -> dict[str, Path]:
    skills_root = repo_root / "integrations" / "skills"
    extras: dict[str, Path] = {}
    if not skills_root.exists():
        return extras
    for skill_dir in sorted(skills_root.iterdir()):
        skill_path = skill_dir / "SKILL.md"
        if not skill_dir.is_dir() or not skill_path.is_file():
            continue
        if skill_dir.name in generated_role_ids:
            continue
        if not skill_installed_by_default(skill_dir.name):
            continue
        extras[skill_dir.name] = skill_path
    return extras


def build_mode_outputs(
    root: Path | None = None, *, claude_plugin_role_ids: Iterable[str] | None = None
) -> dict[Path, str]:
    repo_root = ROOT if root is None else root
    registry = build_default_registry(repo_root)
    mode_docs = load_mode_docs(repo_root)
    outputs: dict[Path, str] = {}
    generated_role_ids = set(registry.surfaced_role_ids("shared_skill"))

    # The canonical, git-tracked plugin agents dir stays the FULL catalog by
    # default (test_plugin_agent_set_matches_canonical_registry pins this) --
    # every role is a legitimate Task-tool dispatch target (e.g. a benchmark
    # harness mounting --plugin-dir straight from here and selecting via
    # --agent lemoncrow:<role>). Only a caller that explicitly passes
    # claude_plugin_role_ids= (see main()'s --claude-plugin-roles flag /
    # LEMONCROW_CLAUDE_PLUGIN_ROLES env var, typically combined with a scratch
    # `root` for a trimmed benchmark-only build) gets a reduced roster --
    # never the canonical in-repo directory.
    claude_plugin_roles = set(claude_plugin_role_ids) if claude_plugin_role_ids is not None else set(HOST_ROLE_IDS)

    for role_id in sorted(generated_role_ids):
        role = registry.roles[role_id]
        mode_doc = mode_docs[role_id]

        if role_id in claude_plugin_roles:
            stable_projection = registry.projection(role_id, "claude_agent")
            stable_path = (
                repo_root / "integrations" / "claude" / "plugin" / "agents" / f"{stable_projection.output_name}.md"
            )
            outputs[stable_path] = rewrite_agent_model(
                render_claude_agent(role, mode_doc, stable_projection),
                normalize_model_for_host(
                    "claude", resolve_explicit_host_model("claude", role_id, workspace_root=repo_root)
                ),
            )

        antigravity_projection = registry.projection(role_id, "antigravity_agent")
        antigravity_path = (
            repo_root
            / "integrations"
            / "antigravity"
            / "plugin"
            / "agents"
            / f"{antigravity_projection.output_name}.md"
        )
        outputs[antigravity_path] = render_simple_agent(role, mode_doc, antigravity_projection)

        opencode_projection = registry.projection(role_id, "opencode_agent")
        opencode_path = repo_root / "integrations" / "opencode" / "agents" / f"{opencode_projection.output_name}.md"
        outputs[opencode_path] = render_agent(
            role,
            mode_doc,
            opencode_projection,
            profile=HostInstructionProfile(
                tool_prefix=_OPENCODE_TOOL_PREFIX,
                overrides=_OPENCODE_SHARED_OVERRIDES,
                native_fallback_names=_OPENCODE_NATIVE_FALLBACK_NAMES,
            ),
        )

        copilot_projection = registry.projection(role_id, "copilot_agent")
        copilot_path = repo_root / "integrations" / "copilot" / "agents" / f"{copilot_projection.output_name}.agent.md"
        outputs[copilot_path] = render_copilot_agent(role, mode_doc, copilot_projection)

        cursor_path = repo_root / "integrations" / "cursor" / "rules" / f"lemoncrow.{role_id}.mdc"
        outputs[cursor_path] = render_cursor_role_rule(role, mode_doc)

        shared_skill = render_shared_skill(role, mode_doc)
        for host, host_dir in HOST_SKILL_DIRS.items():
            if host in ROLE_SKILL_HOSTS:
                outputs[host_dir / role_id / "SKILL.md"] = shared_skill

    for skill_name, skill_path in _extra_shared_skill_paths(repo_root, generated_role_ids).items():
        content = _inject_active_guard(skill_path.read_text(encoding="utf-8"), skill_name)
        for host_dir in HOST_SKILL_DIRS.values():
            host_skill_path = host_dir / skill_name / "SKILL.md"
            outputs[host_skill_path] = content

    for output_path, content in outputs.items():
        if "{{" in content:
            raise ValueError(f"unexpanded template token in generated surface: {output_path}")
    return outputs


def build_outputs(*, claude_plugin_role_ids: Iterable[str] | None = None) -> dict[Path, str]:
    registry = build_default_registry(ROOT)
    mode_outputs = build_mode_outputs(ROOT, claude_plugin_role_ids=claude_plugin_role_ids)
    agents_path = ROOT / "AGENTS.md"
    copilot_path = ROOT / ".github/copilot-instructions.md"
    existing_agents = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
    existing_copilot = copilot_path.read_text(encoding="utf-8") if copilot_path.exists() else ""
    outputs = {
        agents_path: render_managed_context(existing_agents),
        copilot_path: render_managed_context(existing_copilot),
        ROOT / "integrations/copilot/COPILOT_INSTRUCTIONS.lemoncrow.md": agent_guide() + "\n",
        ROOT / "integrations/cursor/rules/coding-guidelines.mdc": render_cursor_coding_rules(),
    }
    for role_id in registry.surfaced_role_ids("copilot_agent"):
        projection = registry.projection(role_id, "copilot_agent")
        integration_path = ROOT / "integrations" / "copilot" / "agents" / f"{projection.output_name}.agent.md"
        outputs[ROOT / ".github" / "agents" / f"{projection.output_name}.agent.md"] = rewrite_agent_model(
            mode_outputs[integration_path],
            resolve_host_model("copilot", role_id, workspace_root=ROOT, fallback=CANONICAL_COPILOT_AGENT_MODEL),
        )
    outputs.update(mode_outputs)
    return outputs


def write_output(path: Path, expected: str) -> None:
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if current == expected:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(expected, encoding="utf-8")


def _prune_stale_claude_plugin_agents(repo_root: Path, kept_role_ids: set[str]) -> None:
    """Delete stable-path agent .md files for roles no longer in the shipped set.

    build_mode_outputs only ever writes/updates files for the currently
    configured role set (DEFAULT_ROLE_IDS unless --claude-plugin-roles /
    LEMONCROW_CLAUDE_PLUGIN_ROLES overrides it) -- without this, shrinking the
    set would leave stale .md files from a previously larger role set sitting
    in the plugin bundle forever, and Claude Code auto-discovers plugin agents
    straight from this directory (see test_new_claude_plugin_json_no_manifest_keys),
    so a stale file is not just repo clutter -- it actually ships.
    """
    agents_dir = repo_root / "integrations" / "claude" / "plugin" / "agents"
    if not agents_dir.is_dir():
        return
    for stale_role in set(HOST_ROLE_IDS) - kept_role_ids:
        (agents_dir / f"{stale_role}.md").unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--claude-plugin-roles",
        default=os.environ.get("LEMONCROW_CLAUDE_PLUGIN_ROLES", ""),
        help=(
            "Comma-separated role ids the Claude plugin bundle ships (default: "
            "DEFAULT_ROLE_IDS, i.e. 'code'). Also settable via "
            "LEMONCROW_CLAUDE_PLUGIN_ROLES. Use when a build needs a different agent "
            "shipped, e.g. a benchmark harness driving --agent lemoncrow:auto: "
            "--claude-plugin-roles=auto."
        ),
    )
    args = parser.parse_args(argv)
    role_ids = tuple(r.strip() for r in args.claude_plugin_roles.split(",") if r.strip()) or None
    kept_roles = set(role_ids) if role_ids is not None else set(HOST_ROLE_IDS)
    for path, content in build_outputs(claude_plugin_role_ids=role_ids).items():
        write_output(path, content)
    _prune_stale_claude_plugin_agents(ROOT, kept_roles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
