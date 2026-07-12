from __future__ import annotations

import importlib.resources
import json
import re
import shutil
from collections.abc import Sequence
from pathlib import Path

from lemoncrow.core.capabilities.default_definitions import (
    DEFAULT_ROLE_IDS,
    SURFACED_ROLE_IDS,
    build_default_registry,
    load_mode_docs,
)
from lemoncrow.core.capabilities.model_settings import (
    CANONICAL_COPILOT_AGENT_MODEL,
    normalize_model_for_host,
    resolve_explicit_host_model,
    resolve_host_model,
)
from lemoncrow.core.environment import skill_installed_by_default
from lemoncrow.core.reply_register import apply_reply_register_level, reply_register_body

LEMONCROW_REPO_ROOT = Path(__file__).resolve().parents[4]
LEMONCROW_CODE_BLOCK_START = "<!-- LEMONCROW START -->"
LEMONCROW_CODE_BLOCK_END = "<!-- LEMONCROW END -->"
CODEX_AGENTS_BLOCK_START = "# LEMONCROW:CODEX AGENTS START"
CODEX_AGENTS_BLOCK_END = "# LEMONCROW:CODEX AGENTS END"


def workspace_copilot_agent_text(
    role_id: str,
    workspace_root: str | Path,
    *,
    repo_root: str | Path | None = None,
) -> str:
    agent_path = _integration_resource(repo_root, "copilot", "agents", _copilot_agent_filename(role_id))
    text = apply_reply_register_level(
        agent_path.read_text(encoding="utf-8"), _integration_resource(repo_root, "agents", "shared")
    )
    model = resolve_host_model(
        "copilot",
        role_id,
        workspace_root=workspace_root,
        fallback=CANONICAL_COPILOT_AGENT_MODEL,
    )
    return rewrite_agent_model(text, model)


def workspace_claude_agent_text(
    role_id: str,
    workspace_root: str | Path,
    *,
    repo_root: str | Path | None = None,
) -> str:
    agent_path = _integration_resource(repo_root, "claude", "plugin", "agents", f"{role_id}.md")
    text = apply_reply_register_level(
        agent_path.read_text(encoding="utf-8"), _integration_resource(repo_root, "agents", "shared")
    )
    model = _claude_explicit_host_model(role_id, workspace_root)
    return rewrite_agent_name(rewrite_agent_model(text, model), f"lc:{role_id}")


def write_workspace_copilot_agents(
    workspace_root: str | Path,
    *,
    repo_root: str | Path | None = None,
) -> list[Path]:
    workspace = Path(workspace_root).expanduser().resolve()
    target_dir = workspace / ".github" / "agents"
    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for stale_name in ["lemoncrow.agent.md", *(f"lemoncrow.{role_id}.agent.md" for role_id in SURFACED_ROLE_IDS)]:
        stale_path = target_dir / stale_name
        if stale_path.exists():
            stale_path.unlink()

    for role_id in SURFACED_ROLE_IDS:
        target = target_dir / _copilot_agent_filename(role_id)
        target.write_text(workspace_copilot_agent_text(role_id, workspace, repo_root=repo_root), encoding="utf-8")
        written.append(target)

    written.append(_write_copilot_vscode_settings(workspace))
    return written


def write_workspace_agents_md(
    workspace_root: str | Path,
    *,
    repo_root: str | Path | None = None,
) -> Path:
    """Create or update the project AGENTS.md with the generic LemonCrow block."""
    workspace = Path(workspace_root).expanduser().resolve()
    target = workspace / "AGENTS.md"
    source = _agents_md_source(repo_root).read_text(encoding="utf-8").strip()
    source = _strip_managed_block(source)
    managed = f"{LEMONCROW_CODE_BLOCK_START}\n{source}\n{LEMONCROW_CODE_BLOCK_END}"
    if target.exists():
        existing = target.read_text(encoding="utf-8").rstrip()
        updated = _upsert_managed_block(existing, source, managed)
    else:
        updated = managed
    target.write_text(updated.rstrip() + "\n", encoding="utf-8")
    return target


def write_workspace_claude_overrides(
    workspace_root: str | Path,
    *,
    repo_root: str | Path | None = None,
    role_ids: Sequence[str] | None = None,
    skill_names: Sequence[str] | None = None,
) -> list[Path]:
    workspace = Path(workspace_root).expanduser().resolve()
    root = _resolve_repo_root(repo_root)
    written: list[Path] = []
    ids = DEFAULT_ROLE_IDS if role_ids is None else tuple(role_ids)

    source_agents = root / "integrations" / "claude" / "plugin" / "agents"
    target_agents = workspace / ".claude" / "agents"
    target_agents.mkdir(parents=True, exist_ok=True)
    for stale_name in (
        [f"{role_id}.md" for role_id in SURFACED_ROLE_IDS]
        + [f"lc:{role_id}.md" for role_id in SURFACED_ROLE_IDS]
        + [f"lemoncrow.{role_id}.md" for role_id in SURFACED_ROLE_IDS]
    ):
        stale_path = target_agents / stale_name
        if stale_path.exists():
            stale_path.unlink()
    for source in sorted(source_agents.glob("*.md")):
        if source.stem not in ids:
            continue
        target = target_agents / f"lemoncrow.{source.stem}.md"
        target.write_text(
            workspace_claude_agent_text(source.stem, workspace, repo_root=root),
            encoding="utf-8",
        )
        written.append(target)

    # Canonical packaged skill source. The claude plugin bundle deliberately
    # carries ONLY the default `lemoncrow` discovery skill (optional public
    # skills are install-time opt-ins), so it cannot serve as the source here.
    source_skills = root / "integrations" / "skills"
    target_skills = workspace / ".claude" / "skills"
    if target_skills.exists():
        shutil.rmtree(target_skills)
    allowed_skills = None if skill_names is None else frozenset(skill_names)
    for source in sorted(source_skills.glob("*/SKILL.md")):
        skill_name = source.parent.name
        if skill_name in SURFACED_ROLE_IDS:
            continue
        # Default-shipped skills (currently just `lemoncrow`, the on-demand
        # install discovery skill) always ship regardless of an explicit
        # skill_names override -- callers pass their own previously-installed
        # *optional* set through here (see agents_skills.py), which must never
        # be able to accidentally drop a skill the user never opted out of.
        if not skill_installed_by_default(skill_name):
            if allowed_skills is None or skill_name not in allowed_skills:
                continue
        relative = source.relative_to(source_skills)
        target = target_skills / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(target)

    settings_local = workspace / ".claude" / "settings.local.json"
    current = _read_json(settings_local)
    raw_env = current.get("env")
    env = raw_env if isinstance(raw_env, dict) else {}
    current["env"] = env
    env["CLAUDE_WORKSPACE_ROOT"] = str(workspace)
    current["agent"] = "lc:code"
    settings_local.parent.mkdir(parents=True, exist_ok=True)
    settings_local.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    written.append(settings_local)
    return written


def write_opencode_agents(
    target_dir: str | Path,
    *,
    model_workspace: str | Path | None = None,
    repo_root: str | Path | None = None,
    role_ids: Sequence[str] | None = None,
) -> list[Path]:
    """Write standalone per-role OpenCode agent markdown files into target_dir.

    Mirrors :func:`write_codex_agents`'s pattern: generic ``target_dir`` used
    for both global installs (``$OPENCODE_CONFIG_HOME/agents``) and workspace
    installs (``<repo>/.opencode/agents``). ``model_workspace`` scopes
    explicit per-role host model overrides; absent pins inherit the OpenCode
    session model. Stale legacy filenames (bare ``lemoncrow.md`` -- the code
    role's pre-rename filename --, bare role names, and any current
    ``lemoncrow.<role>.md``) are removed first so the set always matches the
    current roles.
    """
    root = _resolve_repo_root(repo_root)
    target = Path(target_dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    ids = DEFAULT_ROLE_IDS if role_ids is None else tuple(role_ids)

    for stale_name in ["lemoncrow.md", *(f"{role_id}.md" for role_id in SURFACED_ROLE_IDS)] + [
        f"lemoncrow.{role_id}.md" for role_id in SURFACED_ROLE_IDS
    ]:
        stale_path = target / stale_name
        if stale_path.exists():
            stale_path.unlink()

    source_dir = root / "integrations" / "opencode" / "agents"
    for source in sorted(source_dir.glob("*.md")):
        role_id = source.stem
        if role_id not in ids:
            continue
        out = target / f"lemoncrow.{role_id}.md"
        model = normalize_model_for_host(
            "opencode",
            resolve_explicit_host_model("opencode", role_id, workspace_root=model_workspace),
        )
        body = apply_reply_register_level(
            source.read_text(encoding="utf-8"), _integration_resource(root, "agents", "shared")
        )
        out.write_text(rewrite_agent_model(body, model), encoding="utf-8")
        written.append(out)
    return written


def write_workspace_opencode_agents(
    workspace_root: str | Path,
    *,
    repo_root: str | Path | None = None,
    role_ids: Sequence[str] | None = None,
) -> list[Path]:
    workspace = Path(workspace_root).expanduser().resolve()
    return write_opencode_agents(
        workspace / ".opencode" / "agents",
        model_workspace=workspace,
        repo_root=repo_root,
        role_ids=role_ids,
    )


def write_workspace_cursor_rules(
    workspace_root: str | Path,
    *,
    repo_root: str | Path | None = None,
) -> list[Path]:
    """Copy generated Cursor role rules (lemoncrow.*.mdc) into the workspace .cursor/rules/ dir."""
    workspace = Path(workspace_root).expanduser().resolve()
    root = _resolve_repo_root(repo_root)
    source_dir = root / "integrations" / "cursor" / "rules"
    target_dir = workspace / ".cursor" / "rules"
    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for stale_path in list(target_dir.glob("lemoncrow.*.mdc")):
        stale_path.unlink()

    shared_dir = _integration_resource(root, "agents", "shared")
    for source_path in sorted(source_dir.glob("lemoncrow.*.mdc")):
        target = target_dir / source_path.name
        target.write_text(
            apply_reply_register_level(source_path.read_text(encoding="utf-8"), shared_dir), encoding="utf-8"
        )
        written.append(target)
    return written


def write_codex_agents(
    target_dir: str | Path,
    *,
    model_workspace: str | Path | None = None,
    repo_root: str | Path | None = None,
    role_ids: Sequence[str] | None = None,
) -> list[Path]:
    """Write standalone per-role Codex agent TOMLs into target_dir.

    Used for both global installs (``$CODEX_HOME/agents``) and workspace installs
    (``<repo>/.codex/agents``). ``model_workspace`` scopes per-role model
    overrides to a workspace ``settings.json``; pass ``None`` for a global
    install to use global/default model settings. Stale ``lemoncrow.*.toml`` files
    in the target are removed first so the set always matches the current roles.
    ``role_ids`` defaults to ``DEFAULT_ROLE_IDS``; a future on-demand install
    feature can pass a superset (e.g. ``SURFACED_ROLE_IDS``) to write more.
    """
    root = _resolve_repo_root(repo_root)
    registry = build_default_registry(root)
    mode_docs = load_mode_docs(root)
    target = Path(target_dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    ids = DEFAULT_ROLE_IDS if role_ids is None else tuple(role_ids)

    for stale_path in target.glob("lemoncrow.*.toml"):
        stale_path.unlink()

    for role_id in ids:
        role = registry.roles[role_id]
        mode_doc = mode_docs[role_id]
        path = target / f"lemoncrow.{role_id}.toml"
        model = normalize_model_for_host(
            "codex",
            resolve_explicit_host_model("codex", role_id, workspace_root=model_workspace),
        )
        instructions = _render_codex_mode_body(mode_doc.body, root)
        path.write_text(
            _render_codex_agent_toml(role_id, role.agent_description, instructions, model), encoding="utf-8"
        )
        written.append(path)
    return written


def write_codex_agent_config(
    config_path: str | Path,
    agents_dir: str | Path,
    *,
    repo_root: str | Path | None = None,
) -> Path:
    """Remove legacy per-agent config tables.

    Current Codex discovers custom agents directly from ``agents/*.toml``. Keep
    this compatibility entrypoint because older callers invoke it during
    ``lc init``, but make it cleanup-only so init cannot reintroduce the
    obsolete ``[agents.lemoncrow_*]`` tables.
    """
    del agents_dir, repo_root
    config = Path(config_path).expanduser().resolve()
    if not config.exists():
        return config

    original = config.read_text(encoding="utf-8")
    cleaned = _remove_legacy_codex_agent_sections(original)
    if cleaned != original:
        if cleaned.strip():
            config.write_text(cleaned.rstrip() + "\n", encoding="utf-8")
        else:
            config.unlink()
    return config


def write_workspace_codex_agents(
    workspace_root: str | Path,
    *,
    repo_root: str | Path | None = None,
) -> list[Path]:
    workspace = Path(workspace_root).expanduser().resolve()
    return write_codex_agents(workspace / ".codex" / "agents", model_workspace=workspace, repo_root=repo_root)


def write_workspace_codex_agent_config(
    workspace_root: str | Path,
    *,
    repo_root: str | Path | None = None,
) -> Path:
    workspace = Path(workspace_root).expanduser().resolve()
    return write_codex_agent_config(
        workspace / ".codex" / "config.toml",
        workspace / ".codex" / "agents",
        repo_root=repo_root,
    )


def rewrite_agent_model(text: str, model: str | None) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end < 0:
        return text
    frontmatter_lines = text[4:end].splitlines()
    body = text[end + len("\n---\n") :]
    has_model_line = any(line.strip().startswith("model:") for line in frontmatter_lines)

    rendered: list[str] = []
    inserted = False
    for raw_line in frontmatter_lines:
        stripped = raw_line.strip()
        if stripped.startswith("model:"):
            if model:
                rendered.append(f"model: {model}")
            continue
        rendered.append(raw_line)
        if model and stripped.startswith("description:") and not inserted and not has_model_line:
            rendered.append(f"model: {model}")
            inserted = True
    if model and not inserted and not has_model_line:
        rendered.append(f"model: {model}")
    return "---\n" + "\n".join(rendered) + "\n---\n" + body


def rewrite_agent_name(text: str, name: str) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end < 0:
        return text
    frontmatter_lines = text[4:end].splitlines()
    body = text[end + len("\n---\n") :]
    has_name_line = any(line.strip().startswith("name:") for line in frontmatter_lines)

    rendered: list[str] = []
    inserted = False
    for raw_line in frontmatter_lines:
        stripped = raw_line.strip()
        if stripped.startswith("name:"):
            rendered.append(f"name: {name}")
            inserted = True
            continue
        rendered.append(raw_line)
    if not inserted and not has_name_line:
        rendered.insert(0, f"name: {name}")
    return "---\n" + "\n".join(rendered) + "\n---\n" + body


def _claude_explicit_host_model(role_id: str, workspace_root: str | Path) -> str | None:
    """Return the model for a Claude agent file, or None to inherit session model."""
    return normalize_model_for_host(
        "claude", resolve_explicit_host_model("claude", role_id, workspace_root=workspace_root)
    )


def _read_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _resolve_repo_root(repo_root: str | Path | None) -> Path:
    if repo_root is not None:
        return Path(repo_root).expanduser().resolve()
    if (LEMONCROW_REPO_ROOT / "integrations").is_dir():
        return LEMONCROW_REPO_ROOT
    packaged_root = Path(str(importlib.resources.files("lemoncrow")))
    if (packaged_root / "integrations").is_dir():
        return packaged_root
    return LEMONCROW_REPO_ROOT


def _agents_md_source(repo_root: str | Path | None) -> Path:
    generic = _integration_resource(repo_root, "AGENTS.lemoncrow.md")
    if generic.exists():
        return generic
    return _resolve_repo_root(repo_root) / "AGENTS.md"


def _integration_resource(repo_root: str | Path | None, *parts: str) -> Path:
    """Resolve an ``integrations/`` asset from a checkout or installed wheel."""
    repo_candidate = _resolve_repo_root(repo_root).joinpath("integrations", *parts)
    if repo_candidate.exists():
        return repo_candidate
    packaged = importlib.resources.files("lemoncrow").joinpath("integrations", *parts)
    if packaged.is_file() or packaged.is_dir():
        return Path(str(packaged))
    return repo_candidate


def _copilot_agent_filename(role_id: str) -> str:
    return f"lemoncrow.{role_id}.agent.md"


def _write_copilot_vscode_settings(workspace_root: Path) -> Path:
    target = workspace_root / ".vscode" / "settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    current = _read_json(target)
    current["github.copilot.chat.defaultAgent"] = "lemoncrow.code"
    target.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def _strip_managed_block(text: str) -> str:
    if text.startswith(LEMONCROW_CODE_BLOCK_START) and text.endswith(LEMONCROW_CODE_BLOCK_END):
        return text[len(LEMONCROW_CODE_BLOCK_START) : -len(LEMONCROW_CODE_BLOCK_END)].strip()
    return text.strip()


def _upsert_managed_block(existing: str, source: str, managed: str) -> str:
    pattern = re.compile(
        rf"{re.escape(LEMONCROW_CODE_BLOCK_START)}.*?{re.escape(LEMONCROW_CODE_BLOCK_END)}\n?",
        re.DOTALL,
    )
    if existing.strip() == source:
        return managed
    if pattern.search(existing):
        return pattern.sub(managed, existing, count=1).rstrip()
    if existing:
        return f"{existing}\n\n---\n\n{managed}"
    return managed


def _remove_legacy_codex_agent_sections(existing: str) -> str:
    """Remove only LemonCrow-owned legacy Codex agent registration sections."""
    kept: list[str] = []
    in_managed_block = False
    skip_agent_section = False
    removed = False

    for line in existing.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == CODEX_AGENTS_BLOCK_START:
            in_managed_block = True
            skip_agent_section = False
            removed = True
            continue
        if stripped == CODEX_AGENTS_BLOCK_END:
            in_managed_block = False
            skip_agent_section = False
            removed = True
            continue
        if in_managed_block:
            removed = True
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            skip_agent_section = bool(re.fullmatch(r"\[agents\.lemoncrow_[A-Za-z0-9_-]+\]", stripped))
            if skip_agent_section:
                removed = True
                continue
        if skip_agent_section:
            removed = True
            continue
        kept.append(line)

    if not removed:
        return existing
    cleaned = re.sub(r"\n{3,}", "\n\n", "".join(kept)).strip()
    return cleaned + ("\n" if cleaned else "")


def _upsert_codex_agents_block(existing: str, managed: str) -> str:
    """Backward-compatible helper used only by older callers/tests."""
    stripped = _remove_legacy_codex_agent_sections(existing)
    if stripped and managed:
        return f"{stripped}\n\n{managed}"
    return stripped or managed


def _markdown_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    return "\n".join(lines).rstrip()


def format_native_names_and_verb(names: tuple[str, ...]) -> tuple[str, str]:
    """Backtick-quote native tool names into a listed clause + matching verb
    ("is" for one, "are" for two-plus, Oxford comma only at three-plus)."""
    quoted = [f"`{name}`" for name in names]
    if len(quoted) > 2:
        return f"{', '.join(quoted[:-1])}, and {quoted[-1]}", "are"
    if len(quoted) == 2:
        return f"{quoted[0]} and {quoted[1]}", "are"
    return quoted[0], "is"


def swap_tool_discipline_lead_in(body: str, lead_in: str) -> str:
    """Swap the lead-in of tool-discipline*.md's closing '<lead-in> — use
    LemonCrow: ...' line for a host-specific one, keeping the '— use LemonCrow: ...'
    clause verbatim (its bare tool names still get prefixed by
    replace_inline_tool_names downstream, same as every other host).
    """
    bullets, _, tail = body.rpartition("\n\n")
    _disabled_clause, _, use_clause = tail.partition(" — use LemonCrow:")
    return f"{bullets}\n\n{lead_in} — use LemonCrow:{use_clause}"


# Codex's real native tool-call names (session_parsers/codex.py's
# function_call.name values) -- see plugin_runtime._codex_native_tool_replacement.
# apply_patch is edit-only (dropped for read-only roles, which have no edit
# tool to fall back to); exec_command applies to every role.
CODEX_NATIVE_FALLBACK_NAMES: tuple[str, ...] = ("apply_patch", "exec_command")
CODEX_NATIVE_FALLBACK_NAMES_READ: tuple[str, ...] = ("exec_command",)


def codex_tool_discipline_body(
    shared_dir: Path,
    *,
    source_name: str = "tool-discipline.md",
    native_fallback_names: tuple[str, ...] = CODEX_NATIVE_FALLBACK_NAMES,
) -> str:
    """tool-discipline*.md, with its closing "Host tools disabled" line's lead-in
    swapped for Codex's own native tool names -- "Host tools disabled" is generic
    host-agnostic phrasing; Codex's actual native tools are apply_patch/
    exec_command, and Codex has no tool-permission-deny mechanism to make that
    phrasing literally true (see _codex_native_tool_replacement's reactive-only
    PostToolUse nudge), so name them directly and say what happens instead.

    Shared by both Codex render paths: sync_agent_context.py's SKILL.md
    generation and this module's _render_codex_mode_body (the installed
    agent TOMLs written by write_codex_agents), so the two can't drift.
    """
    body = _markdown_body(shared_dir / source_name)
    names, verb = format_native_names_and_verb(native_fallback_names)
    return swap_tool_discipline_lead_in(body, f"Native Codex {names} {verb} disallowed")


def core_discipline_body(shared_dir: Path) -> str:
    """Expand ``{{CORE_DISCIPLINE}}``: core-discipline plus the telegraphic-default
    bullet (split into its own partial so the reply-register level machinery can
    strip it for lite/off — see lemoncrow.core.reply_register) plus the
    response-economy directive (byte-exact + expand-for-safety invariants that
    bound how terse a reply may get). Always renders the strict/full text;
    level application happens downstream via apply_reply_register_level."""
    body = _markdown_body(shared_dir / "core-discipline.md")
    telegraphic = _markdown_body(shared_dir / "telegraphic-default.md")
    return f"{body}\n{telegraphic}\n{_markdown_body(shared_dir / 'response-economy.md')}"


# Bare tool names referenced as inline code (`` `read` ``) in shared mode-doc
# sources; hosts that prefix tool names get the prefixed form. Deliberately
# excludes `grep`: the partials mention `grep` only in the "Shell `grep`/`rg`/
# `cat`" phrase, where prefixing would invert the sentence's meaning.
INLINE_TOOL_NAMES: frozenset[str] = frozenset(
    {"codemod", "code_search", "edit", "glob", "memory", "read", "search", "bash", "sql", "web_fetch"}
)


def replace_inline_tool_names(body: str, prefix: str) -> str:
    """Replace backtick-quoted bare tool names with ``<prefix><tool>`` spans."""

    def _replacer(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in INLINE_TOOL_NAMES:
            return f"`{prefix}{name}`"
        return m.group(0)

    return re.sub(r"`(\w+)`", _replacer, body)


def _render_codex_mode_body(body: str, repo_root: Path) -> str:
    shared_dir = repo_root / "integrations" / "agents" / "shared"
    shared = {
        "{{CORE_DISCIPLINE}}": core_discipline_body(shared_dir),
        "{{AGENT_RULE}}": _markdown_body(shared_dir / "agent-rule.md"),
        "{{CHANGE_DISCIPLINE}}": _markdown_body(shared_dir / "change-discipline.md"),
        "{{DESTRUCTIVE_GUARD}}": _markdown_body(shared_dir / "destructive-guard.md"),
        "{{RESPONSE_ECONOMY}}": _markdown_body(shared_dir / "response-economy.md"),
        "{{CODING_GUIDELINES}}": _markdown_body(shared_dir / "coding-guidelines.md"),
        "{{TOOL_DISCIPLINE}}": codex_tool_discipline_body(shared_dir),
        "{{TOOL_DISCIPLINE_READ}}": codex_tool_discipline_body(
            shared_dir, source_name="tool-discipline-read.md", native_fallback_names=CODEX_NATIVE_FALLBACK_NAMES_READ
        ),
        "{{REPLY_REGISTER}}": reply_register_body(shared_dir),
    }
    rendered = body.rstrip()
    for token, text in shared.items():
        if token in rendered:
            rendered = rendered.replace(token, text)
    while "\n\n\n" in rendered:  # level "off" expands {{REPLY_REGISTER}} to ""
        rendered = rendered.replace("\n\n\n", "\n\n")
    # Strip the telegraphic-default bullet (inside {{CORE_DISCIPLINE}}) for
    # lite/off; no-op at strict, and the register itself is already level-aware.
    rendered = apply_reply_register_level(rendered, shared_dir)
    if "{{" in rendered:
        raise ValueError("unexpanded template token in Codex agent instructions")
    # Codex registers LemonCrow tools under the ``lc.`` prefix; rewrite bare
    # inline tool names so the instructions cite callable names.
    return replace_inline_tool_names(rendered, "lc.")


def _toml_basic_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _render_codex_agent_toml(role_id: str, description: str, instructions: str, model: str | None) -> str:
    desc = _toml_basic_escape(description).replace("\r", " ").replace("\n", " ")
    body = _toml_basic_escape(instructions.strip())
    rendered = f'name = "lemoncrow.{role_id}"\ndescription = "{desc}"\n'
    if model:
        rendered += f'model = "{_toml_basic_escape(model)}"\n'
    rendered += f'developer_instructions = """\n{body}\n"""\n'
    return rendered


__all__ = [
    "core_discipline_body",
    "rewrite_agent_model",
    "rewrite_agent_name",
    "workspace_claude_agent_text",
    "workspace_copilot_agent_text",
    "write_codex_agent_config",
    "write_codex_agents",
    "write_opencode_agents",
    "write_workspace_agents_md",
    "write_workspace_claude_overrides",
    "write_workspace_codex_agent_config",
    "write_workspace_codex_agents",
    "write_workspace_copilot_agents",
    "write_workspace_cursor_rules",
    "write_workspace_opencode_agents",
]

# Private module helpers (not exported but discoverable)
