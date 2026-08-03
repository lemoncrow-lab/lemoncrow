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
    core_discipline_body,
    format_native_names_and_verb,
    replace_inline_tool_names,
    rewrite_agent_model,
)
from lemoncrow.core.environment import skill_installed_by_default
from lemoncrow.core.persona_partials import markdown_section

CODING_GUIDELINES_PATH = ROOT / "integrations/agents/shared/coding-guidelines.md"
CORE_DISCIPLINE_PATH = ROOT / "integrations/agents/shared/core-discipline.md"
CHANGE_DISCIPLINE_PATH = ROOT / "integrations/agents/shared/change-discipline.md"
DESTRUCTIVE_GUARD_PATH = ROOT / "integrations/agents/shared/destructive-guard.md"
TOOL_DISCIPLINE_PATH = ROOT / "integrations/agents/shared/tool-discipline.md"
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
    "{{RESPONSE_ECONOMY}}": REPLY_REGISTER_PATH,
    "{{TOOL_DISCIPLINE}}": TOOL_DISCIPLINE_PATH,
    "{{TOOL_DISCIPLINE_READ}}": TOOL_DISCIPLINE_PATH,
    "{{REPLY_REGISTER}}": REPLY_REGISTER_PATH,
    "{{AGENT_RULE}}": AGENT_RULE_PATH,
}
SHARED_SECTION_NAMES = {
    "{{RESPONSE_ECONOMY}}": "invariants",
    "{{TOOL_DISCIPLINE}}": "write",
    "{{TOOL_DISCIPLINE_READ}}": "read-only",
    "{{REPLY_REGISTER}}": "ultra",
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


# Bare user-scope server name ("lc", registered by install_claude.sh) — the
# canonical local install. The marketplace plugin shape is
# "mcp__plugin_lemoncrow_lc__"; runtime consumers (hooks, session parsers)
# accept both, and the deny-list covers both (see _claude_disallowed_tools).
_CLAUDE_TOOL_PREFIX = "mcp__lc__"
_OPENCODE_TOOL_PREFIX = "lc_"
_CODEX_TOOL_PREFIX = "lc."

# Cursor registers MCP tools as `mcp-<server>-<tool>` -- observed verbatim in
# its own transcript store (cursorDiskKV `toolFormerData.name`), e.g.
# "mcp-lemoncrow-read" alongside the natives "read_file_v2"/"edit_file_v2".
#
# Without this prefix the rule said "use `read`" -- which in Cursor names its
# OWN read tool, so the directive meant to route work to LemonCrow was read as
# an endorsement of the built-in. The prefix is what makes the instruction name
# a tool the model can actually call.
_CURSOR_TOOL_PREFIX = "mcp-lemoncrow-"

# Claude Code folds the MCP server's `instructions` field (SERVER_INSTRUCTIONS
# in mcp_server.py, which carries the full generic tool discipline) into every
# context — main agent and subagents alike. So claude personas ship only what
# that host-agnostic server string cannot carry: the host tool-name mapping.
# Every other surface (codex — openai/codex#6148 closed not-planned — opencode,
# copilot, cursor, and owned lanes, none of which receive MCP instructions)
# keeps the full shared block.
# Claude Code folds the MCP server's `instructions` field (SERVER_INSTRUCTIONS
# in mcp_server.py, which carries the full generic tool discipline) into every
# context -- main agent and subagents alike. So claude personas ship only what
# that host-agnostic server string cannot carry: the host tool-name mapping.
# Every other surface (codex, opencode, copilot, cursor, and owned lanes, none
# of which receive MCP instructions) keeps the full shared block.
#
# No host names its own built-ins any more. Earlier revisions did, two ways, and
# both backfired: "Host tools disabled" on hosts that disable nothing is a claim
# the agent can check and find false, which discounts the whole block; and
# "native X are fallback-only" on Codex/OpenCode advertises a fallback, which is
# an invitation to take it. The closing line in tool-discipline.md is now an
# unconditional directive that names only the lc tools, so the per-host
# rewrites (and their native-name lists) are gone.
# The not-connected clause cannot live in SERVER_INSTRUCTIONS: those ship FROM
# the MCP server, so they are absent in exactly the case they would govern.
# It has to be in the persona text, which the host loads regardless.
_CLAUDE_NO_TOOLS = (
    " lc tools absent or erroring on every call → refuse to proceed: never fall back "
    'to host tools, report "LemonCrow MCP not connected" and halt.'
)
_CLAUDE_TOOL_DISCIPLINE = "Always use lc: `bash`, `read`, `edit`, `code_search`." + _CLAUDE_NO_TOOLS
_CLAUDE_TOOL_DISCIPLINE_READ = (
    "- **Read-only role — `bash` never mutates.** Inspection and validation only, "
    "no redirects into the tree, no `sed -i`/`tee`, no git state changes.\n"
    "\n"
    "Always use lc: `bash`, `read`, `code_search`." + _CLAUDE_NO_TOOLS
)
_CLAUDE_SHARED_OVERRIDES = {
    "{{TOOL_DISCIPLINE}}": _CLAUDE_TOOL_DISCIPLINE,
    "{{TOOL_DISCIPLINE_READ}}": _CLAUDE_TOOL_DISCIPLINE_READ,
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


# The ONE mode rule Cursor loads on every request. Override with
# LEMONCROW_CURSOR_MODE=<role> before `make sync-agent-context` (e.g. auto).
#
# Exactly one, never two. There is no persona-agnostic baseline file any more: a
# mode rule already composes core, change, coding, tool and reply-register
# discipline, so a second always-on rule alongside it re-billed the same bullets
# on every round-trip. The same trap applies to invoking a second mode by name
# while one is stamped always-on -- both personas would ship together. Treat the
# mode rules as pick-one: stamp the mode you actually run, leave the rest
# description-only so Cursor can surface them on demand at no standing cost.
CURSOR_ALWAYS_ON_ROLE = os.environ.get("LEMONCROW_CURSOR_MODE", "code").strip() or "code"


# Cursor loads MCP tool schemas LAZILY: until the model calls `get_mcp_tools`,
# the LemonCrow tools are not in its context at all, so "use `read`" can only
# resolve to Cursor's own built-in. Measured over 61 composer-2.5 sessions in
# Cursor's own transcript store (cursorDiskKV `toolFormerData`):
#
#   * whichever family the FIRST tool call belongs to decides the whole
#     session -- native->LemonCrow transitions were 0 out of 845, while
#     LemonCrow->LemonCrow ran 97.8%. It is an absorbing state, not a
#     preference, and no amount of later rule text recovers from it.
#   * of 19 sessions that opened on a LemonCrow tool, 17 had called
#     `get_mcp_tools` first; of 38 that opened native, 28 never called it at
#     all. Discovery is the fork in the road.
#
# So the one instruction that can change the outcome has to fire BEFORE the
# first tool call, and it has to name the discovery step rather than the tools
# (which cannot be referenced until discovery has run). Repeated at the end as
# well: lost-in-the-middle means a lead-only directive is the one most likely
# to be skimmed past.
# Discover by SERVER, never by `pattern`. Measured over the 87 `get_mcp_tools`
# calls in Cursor's own transcript store, success is entirely a function of
# which argument is used:
#
#   server=      63 calls, 0 empty     (modes single_tool / server)
#   no args      11 calls, 0 empty     (mode catalog)
#   pattern=     22 calls, 18 empty    (mode search)
#
# `pattern` fails 82% of the time -- including searches that spelled out every
# LemonCrow tool name -- so a rule that tells the model to grep for us mostly
# teaches it we do not exist, and a native first call locks the session out.
# `user-lemoncrow` is the name a user-scope ~/.cursor/mcp.json install gets (35
# calls, 0 failures); workspace and plugin installs land on
# `project-0-<workspace>-lemoncrow` / `plugin-lemoncrow-lemoncrow`, which is
# what the argument-less catalog fallback is for.
# The opening move is the whole ballgame. Same transcript store, grouped by
# which tool a session called first:
#
#   opened on mcp-lemoncrow-bash   -> 10..52 LemonCrow calls,  0..3 native
#   opened on edit_file_v2         ->  0..6  LemonCrow calls, 18..315 native
#
# There is no middle. Native->LemonCrow transitions were 0 of 845 across the
# whole store, so the first call does not express a preference, it sets one
# permanently. A task that begins by editing therefore never uses LemonCrow at
# all, which is exactly the shape of an "apply this fix" prompt.
#
# So the rule cannot merely rank tools -- it has to put a LemonCrow call before
# the model's first instinct. Hence a mandated orientation call, which is also
# what lean-ctx does with its "ctx_compose, call FIRST" rule: the point is not
# what that call returns, it is that the session starts on the MCP side.
_CURSOR_DISCOVERY_LEAD = (
    "**Before anything else, in order:**\n"
    '1. `get_mcp_tools` with `server: "user-lemoncrow"`. Not listed → call it with no '
    "arguments, take the name ending in `lemoncrow`. Never search by `pattern` — "
    "returns empty most of the time.\n"
    "2. First working call must be LemonCrow: `code_search` on the subject, or `bash`. "
    "Even when the task names the file and an edit looks obvious.\n\n"
    "Whichever family you call first is the one you keep — open on a built-in and "
    "LemonCrow goes unused all session. No recovery mid-session. "
    "`serverStatus: error` → say so, continue with built-ins."
)

_CURSOR_DISCOVERY_TAIL = (
    "Reminder: `get_mcp_tools` server `user-lemoncrow`, then LemonCrow `code_search`/`bash` " "before any edit."
)


def render_cursor_role_rule(role: DefaultRole, mode_doc: ModeDoc) -> str:
    header = [
        "---",
        f"description: LemonCrow {role.role_id} mode reference for Cursor.",
    ]
    always_on = role.role_id == CURSOR_ALWAYS_ON_ROLE
    if always_on:
        header.append("alwaysApply: true")
    header.append("---")
    body = replace_inline_tool_names(render_mode_body(mode_doc), _CURSOR_TOOL_PREFIX)
    # Only the always-on rule can be trusted to arrive before the first tool
    # call; a description-only rule the agent pulls in later has already missed
    # the moment this directive exists to catch.
    if always_on:
        body = f"{_CURSOR_DISCOVERY_LEAD}\n\n{body}\n\n{_CURSOR_DISCOVERY_TAIL}"
    return "\n".join([*header, "", body]).rstrip() + "\n"


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
    body = replace_inline_tool_names(render_mode_body(mode_doc), _CODEX_TOOL_PREFIX)
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
    """Expand one standalone or consolidated shared partial section."""
    if token == "{{CORE_DISCIPLINE}}":
        return core_discipline_body(source_path.parent)
    section_name = SHARED_SECTION_NAMES.get(token)
    if section_name:
        return markdown_section(source_path, section_name)
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
    # every role is a legitimate Task-tool dispatch target (e.g. an external
    # harness mounting --plugin-dir straight from here and selecting via
    # --agent lemoncrow:<role>). Only a caller that explicitly passes
    # claude_plugin_role_ids= (see main()'s --claude-plugin-roles flag /
    # LEMONCROW_CLAUDE_PLUGIN_ROLES env var, typically combined with a scratch
    # `root` for a trimmed single-role build) gets a reduced roster --
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
            profile=HostInstructionProfile(tool_prefix=_OPENCODE_TOOL_PREFIX),
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
            "shipped, e.g. a harness driving --agent lemoncrow:auto: "
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
