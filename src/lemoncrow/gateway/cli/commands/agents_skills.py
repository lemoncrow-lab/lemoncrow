"""On-demand install/remove/list for non-default LemonCrow agent roles and skills.

Phase 2 of the "minimal default install" feature (Phase 1: default install is
just the ``code`` agent role and zero skills). This module lets a user opt
specific extra roles (explore/execute/plan/research/review/solve/auto/bare/
general) and public skills (benchmark/orchestrate/perf-review/recall/swarm/
ux-review) into a given host, per global or --workspace scope.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import click

from lemoncrow.core.capabilities.default_definitions import (
    DEFAULT_ROLE_IDS,
    SURFACED_ROLE_IDS,
    build_default_registry,
    parse_frontmatter,
)
from lemoncrow.core.capabilities.workspace_host_overrides import (
    write_codex_agents,
    write_workspace_claude_overrides,
    write_workspace_opencode_agents,
)
from lemoncrow.core.environment import skill_visible
from lemoncrow.core.foundation.retriever import count_tokens
from lemoncrow.gateway.integrations.openmemory_lifecycle import project_root as _project_root

# Hosts that support on-demand agent roles vs. skills are not identical:
# OpenCode has no skills concept; Antigravity has no per-role agent concept.
AGENT_HOSTS: tuple[str, ...] = ("claude", "codex", "opencode")
SKILL_HOSTS: tuple[str, ...] = ("claude", "codex", "antigravity")
ALL_HOSTS: tuple[str, ...] = ("claude", "codex", "opencode", "antigravity")

INSTALLABLE_ROLE_IDS: tuple[str, ...] = tuple(r for r in SURFACED_ROLE_IDS if r not in DEFAULT_ROLE_IDS)

# The 6 public skills (integrations/skills/<name>/SKILL.md). Hardcoded rather
# than derived from environment.HIDDEN_SKILLS (currently empty -- see that
# module's docstring) so a hidden dev-only skill can never become installable
# here even if that frozenset drifts.
PUBLIC_SKILL_NAMES: tuple[str, ...] = ("benchmark", "orchestrate", "perf-review", "recall", "swarm", "ux-review")


def _repo_root() -> Path:
    return _project_root()


def _resolve_workspace(workspace: Path | None) -> Path | None:
    return workspace.expanduser().resolve() if workspace is not None else None


def _scope_label(workspace: Path | None) -> str:
    return "workspace" if workspace is not None else "global"


# --------------------------------------------------------------------------- #
# Live install locations
# --------------------------------------------------------------------------- #


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()


def _opencode_config_home() -> Path:
    override = os.environ.get("OPENCODE_CONFIG_HOME")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "opencode"


def _agent_dir(host: str, workspace: Path | None) -> Path:
    if workspace is not None:
        if host == "claude":
            return workspace / ".claude" / "agents"
        if host == "codex":
            return workspace / ".codex" / "agents"
        if host == "opencode":
            return workspace / ".opencode" / "agents"
        raise click.ClickException(f"unsupported agent host: {host}")
    if host == "claude":
        return Path.home() / ".lemoncrow" / "claude-plugin" / "agents"
    if host == "codex":
        return _codex_home() / "agents"
    if host == "opencode":
        return _opencode_config_home() / "agents"
    raise click.ClickException(f"unsupported agent host: {host}")


def _agent_file(host: str, workspace: Path | None, role_id: str) -> Path:
    agent_dir = _agent_dir(host, workspace)
    if host == "codex":
        return agent_dir / f"lemoncrow.{role_id}.toml"
    if host == "claude" and workspace is None:
        return agent_dir / f"{role_id}.md"  # global staging uses bare filenames
    return agent_dir / f"lemoncrow.{role_id}.md"  # claude workspace, opencode (both scopes)


def _skill_dir(host: str, workspace: Path | None) -> Path:
    if host == "opencode":
        raise click.ClickException("OpenCode has no skills concept")
    if workspace is not None:
        if host != "claude":
            raise click.ClickException(f"skill install for {host} is only supported at global scope (no --workspace)")
        return workspace / ".claude" / "skills"
    if host == "claude":
        return Path.home() / ".lemoncrow" / "claude-plugin" / "skills"
    if host == "codex":
        return _codex_home() / "plugins" / "lemoncrow" / "skills"
    if host == "antigravity":
        return Path.home() / ".gemini" / "antigravity-cli" / "skills"
    raise click.ClickException(f"unsupported skill host: {host}")


def _skill_file(host: str, workspace: Path | None, name: str) -> Path:
    return _skill_dir(host, workspace) / name / "SKILL.md"


# --------------------------------------------------------------------------- #
# Installed-set detection
# --------------------------------------------------------------------------- #


def _installed_role_ids(host: str, workspace: Path | None) -> tuple[str, ...]:
    return tuple(r for r in INSTALLABLE_ROLE_IDS if _agent_file(host, workspace, r).exists())


def _installed_skill_names(host: str, workspace: Path | None) -> tuple[str, ...]:
    if host == "opencode":
        return ()
    return tuple(n for n in PUBLIC_SKILL_NAMES if _skill_file(host, workspace, n).exists())


# --------------------------------------------------------------------------- #
# Token-cost accounting -- the standing per-turn tax of shipping a role/skill
# description in every tool-choice/agent-selection prompt.
# --------------------------------------------------------------------------- #


def _role_cost(role_id: str, repo_root: Path) -> int:
    registry = build_default_registry(repo_root)
    role = registry.roles[role_id]
    return count_tokens(f"{role_id}: {role.agent_description}")


def _skill_description(name: str, repo_root: Path) -> str:
    skill_md = repo_root / "integrations" / "skills" / name / "SKILL.md"
    if not skill_md.exists():
        raise click.ClickException(f"unknown skill: {name}")
    meta, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    return meta.get("description", "")


def _skill_cost(name: str, repo_root: Path) -> int:
    return count_tokens(f"{name}: {_skill_description(name, repo_root)}")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _validate_installable_role(role_id: str) -> None:
    if role_id not in SURFACED_ROLE_IDS:
        raise click.ClickException(f"unknown role: {role_id} (choices: {', '.join(INSTALLABLE_ROLE_IDS)})")
    if role_id in DEFAULT_ROLE_IDS:
        raise click.ClickException(f"{role_id} is installed by default already")


def _validate_removable_role(role_id: str) -> None:
    if role_id in DEFAULT_ROLE_IDS:
        raise click.ClickException(f"{role_id} is always installed, use `lc uninstall` to remove LemonCrow entirely")
    if role_id not in SURFACED_ROLE_IDS:
        raise click.ClickException(f"unknown role: {role_id} (choices: {', '.join(INSTALLABLE_ROLE_IDS)})")


def _validate_installable_skill(name: str) -> None:
    if name not in PUBLIC_SKILL_NAMES or not skill_visible(name):
        raise click.ClickException(
            f"unknown or non-installable skill: {name} (choices: {', '.join(PUBLIC_SKILL_NAMES)})"
        )


# --------------------------------------------------------------------------- #
# Host detection
# --------------------------------------------------------------------------- #


def _detect_host(candidates_pool: tuple[str, ...], workspace: Path | None, *, label: str) -> str:
    if workspace is not None:
        from lemoncrow.gateway.cli.commands.admin import _detected_workspace_hosts

        candidates = tuple(h for h in _detected_workspace_hosts(workspace) if h in candidates_pool)
    else:
        candidates = tuple(h for h in candidates_pool if shutil.which(h))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise click.ClickException(
            f"no supported {label} host detected; pass --host explicitly ({'|'.join(candidates_pool)})"
        )
    raise click.ClickException(f"multiple {label} hosts detected ({', '.join(candidates)}); pass --host explicitly")


def _detect_agent_host(workspace: Path | None) -> str:
    return _detect_host(AGENT_HOSTS, workspace, label="agent")


def _detect_skill_host(workspace: Path | None) -> str:
    return _detect_host(SKILL_HOSTS, workspace, label="skill")


# --------------------------------------------------------------------------- #
# Apply (install/remove) a full desired set
# --------------------------------------------------------------------------- #


def _apply_agent_role_set(host: str, workspace: Path | None, role_ids: tuple[str, ...], repo_root: Path) -> None:
    all_ids = DEFAULT_ROLE_IDS + tuple(r for r in role_ids if r not in DEFAULT_ROLE_IDS)
    if workspace is not None:
        if host == "claude":
            # write_workspace_claude_overrides manages both agents and skills in
            # one pass; pass the currently-installed skill set through so an
            # agent-only change doesn't reset skills back to the default (empty).
            current_skills = _installed_skill_names("claude", workspace)
            write_workspace_claude_overrides(
                workspace, repo_root=repo_root, role_ids=all_ids, skill_names=current_skills
            )
        elif host == "codex":
            write_codex_agents(
                workspace / ".codex" / "agents", model_workspace=workspace, repo_root=repo_root, role_ids=all_ids
            )
        elif host == "opencode":
            write_workspace_opencode_agents(workspace, repo_root=repo_root, role_ids=all_ids)
        else:
            raise click.ClickException(f"unsupported agent host: {host}")
        return
    # Global scope: re-invoke the full install script so plugin/marketplace
    # registration stays consistent (it recomputes the staged bundle from
    # scratch every run). Thread through the currently-installed skill set too
    # (claude/codex only) so an agent-only change doesn't wipe skills that were
    # added via a separate `lc skill install` call.
    script = repo_root / "scripts" / f"install_{host}.sh"
    if not script.exists():
        raise click.ClickException(f"install script not found: {script}")
    # NB: the install scripts parse only the space-separated form (--roles X).
    cmd = ["bash", str(script), "--roles", ",".join(all_ids)]
    if host in ("claude", "codex"):
        skills = _installed_skill_names(host, None)
        if skills:
            cmd += ["--include-skills", ",".join(skills)]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(f"install_{host}.sh failed with exit code {exc.returncode}") from exc


def _apply_skill_name_set(host: str, workspace: Path | None, names: tuple[str, ...], repo_root: Path) -> None:
    if host == "opencode":
        raise click.ClickException("OpenCode has no skills concept")
    if workspace is not None:
        if host != "claude":
            raise click.ClickException(f"skill install for {host} is only supported at global scope (no --workspace)")
        # Symmetric to _apply_agent_role_set above: pass the currently-installed
        # role set through so a skill-only change doesn't reset agents back to
        # the default (code only).
        current_roles = DEFAULT_ROLE_IDS + _installed_role_ids("claude", workspace)
        write_workspace_claude_overrides(workspace, repo_root=repo_root, role_ids=current_roles, skill_names=names)
        return
    # Global scope: write directly into the live skills directory. Unlike
    # agents, this needs no host CLI binary and doesn't touch the agents dir,
    # so it can't clobber a separately-managed role set.
    dest = _skill_dir(host, None)
    dest.mkdir(parents=True, exist_ok=True)
    script = repo_root / "scripts" / "build_host_skills.sh"
    cmd = ["bash", str(script), "--host", host, "--dest", str(dest)]
    if names:
        cmd.append(f"--include-skills={','.join(names)}")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(f"build_host_skills.sh failed with exit code {exc.returncode}") from exc


def reapply_installed_agents(repo_root: Path | None = None) -> int:
    """Re-render installed agent personas for every wired global host.

    Used by ``lc settings set cli.telegraphic`` — the reply-register
    level is baked into the persona files, so a level change must re-stage
    them. Returns the number of hosts regenerated. Cursor/antigravity have no
    global agent-dir marker here; they pick the level up on their next install.
    """
    root = repo_root if repo_root is not None else _repo_root()
    applied = 0
    for host in ("claude", "codex", "opencode"):
        try:
            roles = _installed_role_ids(host, None)
            # `code` is the always-installed default role, never in the
            # installable-extras list — its file marks the host as wired.
            host_wired = bool(roles) or _agent_file(host, None, "code").exists()
        except click.ClickException:
            continue
        if not host_wired:
            continue
        click.echo(f"regenerating {host} agents ({', '.join(('code', *roles))}) …")
        _apply_agent_role_set(host, None, roles, root)
        applied += 1
    return applied


# --------------------------------------------------------------------------- #
# CLI: lc agent list/install/remove
# --------------------------------------------------------------------------- #


@click.group("agent")
def agent_group() -> None:
    """Install, remove, or list on-demand LemonCrow agent roles for a host."""


@agent_group.command("list")
@click.option("--host", type=click.Choice(AGENT_HOSTS), default=None, help="Host to inspect. Auto-detected if omitted.")
@click.option("--workspace", type=click.Path(path_type=Path), default=None, help="Workspace scope instead of global.")
@click.option("--json", "as_json", is_flag=True)
def agent_list_cmd(host: str | None, workspace: Path | None, as_json: bool) -> None:
    """List non-default agent roles: installed vs available, with token cost."""
    repo_root = _repo_root()
    ws = _resolve_workspace(workspace)
    resolved_host = host or _detect_agent_host(ws)
    installed = set(_installed_role_ids(resolved_host, ws))
    rows = [
        {"role_id": r, "installed": r in installed, "token_cost": _role_cost(r, repo_root)}
        for r in INSTALLABLE_ROLE_IDS
    ]
    if as_json:
        click.echo(json.dumps({"host": resolved_host, "scope": _scope_label(ws), "roles": rows}, indent=2))
        return
    click.echo(f"host: {resolved_host}  scope: {_scope_label(ws)}")
    for row in rows:
        marker = "installed" if row["installed"] else "available"
        click.echo(f"  [{marker:9}] {row['role_id']:<10} ~{row['token_cost']} tok/turn standing cost")


@agent_group.command("install")
@click.argument("role_id")
@click.option(
    "--host", type=click.Choice(AGENT_HOSTS), default=None, help="Host to install into. Auto-detected if omitted."
)
@click.option("--workspace", type=click.Path(path_type=Path), default=None, help="Workspace scope instead of global.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def agent_install_cmd(role_id: str, host: str | None, workspace: Path | None, yes: bool) -> None:
    """Install ROLE_ID (an agent role not in the default set) for a host."""
    _validate_installable_role(role_id)
    repo_root = _repo_root()
    ws = _resolve_workspace(workspace)
    resolved_host = host or _detect_agent_host(ws)
    installed = set(_installed_role_ids(resolved_host, ws))
    if role_id in installed:
        click.echo(f"{role_id} is already installed for {resolved_host} ({_scope_label(ws)})")
        return
    cost = _role_cost(role_id, repo_root)
    click.echo(
        f"Will install '{role_id}' for {resolved_host} ({_scope_label(ws)}) -- adds ~{cost} tokens/turn standing cost."
    )
    if not yes and not click.confirm("Proceed?", default=True):
        raise click.Abort()
    new_set = tuple(sorted(installed | {role_id}))
    _apply_agent_role_set(resolved_host, ws, new_set, repo_root)
    click.echo(f"installed {role_id} (~{cost} tokens/turn)")


@agent_group.command("remove")
@click.argument("role_id")
@click.option(
    "--host", type=click.Choice(AGENT_HOSTS), default=None, help="Host to remove from. Auto-detected if omitted."
)
@click.option("--workspace", type=click.Path(path_type=Path), default=None, help="Workspace scope instead of global.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def agent_remove_cmd(role_id: str, host: str | None, workspace: Path | None, yes: bool) -> None:
    """Remove ROLE_ID (a previously on-demand-installed agent role) from a host."""
    _validate_removable_role(role_id)
    repo_root = _repo_root()
    ws = _resolve_workspace(workspace)
    resolved_host = host or _detect_agent_host(ws)
    installed = set(_installed_role_ids(resolved_host, ws))
    if role_id not in installed:
        click.echo(f"{role_id} is not installed for {resolved_host} ({_scope_label(ws)})")
        return
    if not yes and not click.confirm(f"Remove '{role_id}' from {resolved_host} ({_scope_label(ws)})?", default=True):
        raise click.Abort()
    new_set = tuple(sorted(installed - {role_id}))
    _apply_agent_role_set(resolved_host, ws, new_set, repo_root)
    click.echo(f"removed {role_id}")


# --------------------------------------------------------------------------- #
# CLI: lc skill list/install/remove
# --------------------------------------------------------------------------- #


@click.group("skill")
def skill_group() -> None:
    """Install, remove, or list on-demand LemonCrow public skills for a host."""


@skill_group.command("list")
@click.option("--host", type=click.Choice(ALL_HOSTS), default=None, help="Host to inspect. Auto-detected if omitted.")
@click.option("--workspace", type=click.Path(path_type=Path), default=None, help="Workspace scope instead of global.")
@click.option("--json", "as_json", is_flag=True)
def skill_list_cmd(host: str | None, workspace: Path | None, as_json: bool) -> None:
    """List public skills: installed vs available, with token cost."""
    if host == "opencode":
        raise click.ClickException("OpenCode has no skills concept")
    repo_root = _repo_root()
    ws = _resolve_workspace(workspace)
    resolved_host = host or _detect_skill_host(ws)
    installed = set(_installed_skill_names(resolved_host, ws))
    rows = [
        {"name": n, "installed": n in installed, "token_cost": _skill_cost(n, repo_root)} for n in PUBLIC_SKILL_NAMES
    ]
    if as_json:
        click.echo(json.dumps({"host": resolved_host, "scope": _scope_label(ws), "skills": rows}, indent=2))
        return
    click.echo(f"host: {resolved_host}  scope: {_scope_label(ws)}")
    for row in rows:
        marker = "installed" if row["installed"] else "available"
        click.echo(f"  [{marker:9}] {row['name']:<12} ~{row['token_cost']} tok/turn standing cost")


@skill_group.command("install")
@click.argument("name")
@click.option(
    "--host", type=click.Choice(ALL_HOSTS), default=None, help="Host to install into. Auto-detected if omitted."
)
@click.option("--workspace", type=click.Path(path_type=Path), default=None, help="Workspace scope instead of global.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def skill_install_cmd(name: str, host: str | None, workspace: Path | None, yes: bool) -> None:
    """Install skill NAME (one of the 6 public skills) for a host."""
    _validate_installable_skill(name)
    if host == "opencode":
        raise click.ClickException("OpenCode has no skills concept")
    repo_root = _repo_root()
    ws = _resolve_workspace(workspace)
    resolved_host = host or _detect_skill_host(ws)
    installed = set(_installed_skill_names(resolved_host, ws))
    if name in installed:
        click.echo(f"{name} is already installed for {resolved_host} ({_scope_label(ws)})")
        return
    cost = _skill_cost(name, repo_root)
    click.echo(
        f"Will install skill '{name}' for {resolved_host} ({_scope_label(ws)}) "
        f"-- adds ~{cost} tokens/turn standing cost."
    )
    if not yes and not click.confirm("Proceed?", default=True):
        raise click.Abort()
    new_set = tuple(sorted(installed | {name}))
    _apply_skill_name_set(resolved_host, ws, new_set, repo_root)
    click.echo(f"installed {name} (~{cost} tokens/turn)")


@skill_group.command("remove")
@click.argument("name")
@click.option(
    "--host", type=click.Choice(ALL_HOSTS), default=None, help="Host to remove from. Auto-detected if omitted."
)
@click.option("--workspace", type=click.Path(path_type=Path), default=None, help="Workspace scope instead of global.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def skill_remove_cmd(name: str, host: str | None, workspace: Path | None, yes: bool) -> None:
    """Remove skill NAME (a previously on-demand-installed skill) from a host."""
    if host == "opencode":
        raise click.ClickException("OpenCode has no skills concept")
    repo_root = _repo_root()
    ws = _resolve_workspace(workspace)
    resolved_host = host or _detect_skill_host(ws)
    installed = set(_installed_skill_names(resolved_host, ws))
    if name not in installed:
        click.echo(f"{name} is not installed for {resolved_host} ({_scope_label(ws)})")
        return
    if not yes and not click.confirm(f"Remove skill '{name}' from {resolved_host} ({_scope_label(ws)})?", default=True):
        raise click.Abort()
    new_set = tuple(sorted(installed - {name}))
    _apply_skill_name_set(resolved_host, ws, new_set, repo_root)
    click.echo(f"removed {name}")


# --------------------------------------------------------------------------- #
# CLI: lc install optionals
# --------------------------------------------------------------------------- #


@click.group("install")
def install_group() -> None:
    """Bulk-install optional (non-default) LemonCrow agents and skills."""


@install_group.command("optionals")
@click.option("--host", default=None, help="Restrict to one host. Default: every detected host.")
@click.option("--workspace", type=click.Path(path_type=Path), default=None, help="Workspace scope instead of global.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def install_optionals_cmd(host: str | None, workspace: Path | None, yes: bool) -> None:
    """Install every non-default agent role and every public skill.

    Applies to the given --host, or every detected host if omitted.
    """
    repo_root = _repo_root()
    ws = _resolve_workspace(workspace)

    agent_hosts: tuple[str, ...]
    skill_hosts: tuple[str, ...]
    if host is not None:
        if host not in ALL_HOSTS:
            raise click.ClickException(f"unknown host: {host} (choices: {', '.join(ALL_HOSTS)})")
        agent_hosts = (host,) if host in AGENT_HOSTS else ()
        skill_hosts = (host,) if host in SKILL_HOSTS else ()
    else:
        if ws is not None:
            from lemoncrow.gateway.cli.commands.admin import _detected_workspace_hosts

            detected = _detected_workspace_hosts(ws)
        else:
            detected = tuple(h for h in ALL_HOSTS if shutil.which(h))
        agent_hosts = tuple(h for h in detected if h in AGENT_HOSTS)
        skill_hosts = tuple(h for h in detected if h in SKILL_HOSTS)

    # Skill install at workspace scope is only wired up for claude (see
    # _skill_dir) -- codex/antigravity skills are global-only. Drop them here
    # rather than let _installed_skill_names blow up mid-plan for a host that
    # was only ever offered agent roles at this scope.
    if ws is not None:
        skill_hosts = tuple(h for h in skill_hosts if h == "claude")

    total_cost = 0
    plan: list[str] = []
    agent_missing: dict[str, tuple[str, ...]] = {}
    for h in agent_hosts:
        installed = set(_installed_role_ids(h, ws))
        missing = tuple(r for r in INSTALLABLE_ROLE_IDS if r not in installed)
        if missing:
            agent_missing[h] = missing
            total_cost += sum(_role_cost(r, repo_root) for r in missing)
            plan.append(f"  agents  @ {h}: {', '.join(missing)}")

    skill_missing: dict[str, tuple[str, ...]] = {}
    for h in skill_hosts:
        installed = set(_installed_skill_names(h, ws))
        missing = tuple(n for n in PUBLIC_SKILL_NAMES if n not in installed)
        if missing:
            skill_missing[h] = missing
            total_cost += sum(_skill_cost(n, repo_root) for n in missing)
            plan.append(f"  skills  @ {h}: {', '.join(missing)}")

    if not plan:
        click.echo("nothing to install -- every optional agent/skill is already installed for the detected host(s)")
        return

    click.echo("Will install:")
    for line in plan:
        click.echo(line)
    click.echo(f"Total added standing cost: ~{total_cost} tokens/turn")
    if not yes and not click.confirm("Proceed?", default=True):
        raise click.Abort()

    for h, missing in agent_missing.items():
        new_set = tuple(sorted(set(_installed_role_ids(h, ws)) | set(missing)))
        _apply_agent_role_set(h, ws, new_set, repo_root)
    for h, missing in skill_missing.items():
        new_set = tuple(sorted(set(_installed_skill_names(h, ws)) | set(missing)))
        _apply_skill_name_set(h, ws, new_set, repo_root)
    click.echo("done")


# --------------------------------------------------------------------------- #
# Staleness nudge -- Phase 5. An installed OPTIONAL (non-default) role or
# skill unused for --stale-days is worth flagging for removal. Reuses the
# exact _role_cost/_skill_cost standing-cost calculation `list` already shows
# (never recomputed independently) and the usage log
# plugin_runtime.record_optional_use writes to on every optional agent/skill
# invocation. `code` and the `lc` skill never appear here: they're
# excluded by construction (INSTALLABLE_ROLE_IDS / PUBLIC_SKILL_NAMES already
# omit them).
# --------------------------------------------------------------------------- #

STALE_NUDGE_DAYS_ENV = "LEMONCROW_STALE_NUDGE_DAYS"
DEFAULT_STALE_NUDGE_DAYS = 7.0
_MS_PER_DAY = 86_400_000


def _stale_nudge_days() -> float:
    raw = os.environ.get(STALE_NUDGE_DAYS_ENV, "")
    try:
        return float(raw) if raw else DEFAULT_STALE_NUDGE_DAYS
    except ValueError:
        return DEFAULT_STALE_NUDGE_DAYS


def _default_lemoncrow_root() -> Path:
    raw = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    return Path(raw).expanduser() if raw else Path.home() / ".lemoncrow"


def stale_optional_items(
    host: str,
    workspace: Path | None,
    *,
    root: Path | None = None,
    repo_root: Path | None = None,
    now_ms: int | None = None,
    threshold_days: float | None = None,
) -> list[dict[str, Any]]:
    """Installed optional (non-default) agents/skills unused for >= threshold days.

    One dict per stale item: ``{"kind", "name", "days_unused", "token_cost"}``.
    ``days_unused`` is ``None`` when the item has never been used (still
    stale -- installed with no recorded use at all). Shared by the
    `stale-nudge` CLI command (Claude statusline) and the OpenCode nudge
    plugin so both hosts apply the identical threshold/cost logic.
    """
    from lemoncrow.core.capabilities.plugin_runtime import last_optional_use_ms

    resolved_root = root if root is not None else _default_lemoncrow_root()
    resolved_repo_root = repo_root if repo_root is not None else _repo_root()
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    threshold_ms = (threshold_days if threshold_days is not None else _stale_nudge_days()) * _MS_PER_DAY

    items: list[dict[str, Any]] = []
    if host in AGENT_HOSTS:
        for role_id in _installed_role_ids(host, workspace):
            last = last_optional_use_ms(resolved_root, "agent", role_id)
            if last is not None and now - last < threshold_ms:
                continue
            days_unused = None if last is None else (now - last) // _MS_PER_DAY
            items.append(
                {
                    "kind": "agent",
                    "name": role_id,
                    "days_unused": days_unused,
                    "token_cost": _role_cost(role_id, resolved_repo_root),
                }
            )
    if host in SKILL_HOSTS:
        for name in _installed_skill_names(host, workspace):
            last = last_optional_use_ms(resolved_root, "skill", name)
            if last is not None and now - last < threshold_ms:
                continue
            days_unused = None if last is None else (now - last) // _MS_PER_DAY
            items.append(
                {
                    "kind": "skill",
                    "name": name,
                    "days_unused": days_unused,
                    "token_cost": _skill_cost(name, resolved_repo_root),
                }
            )
    return items


def format_stale_nudge(item: dict[str, Any]) -> str:
    """Render one stale item as the generic wording shared by every host."""
    unused = "never used" if item["days_unused"] is None else f"unused {item['days_unused']}d"
    return (
        f"{item['name']} installed, {unused} — remove: /lemoncrow remove {item['name']} "
        f"(saves ~{item['token_cost']} tok/turn)"
    )


@click.command("stale-nudge")
@click.option("--host", type=click.Choice(ALL_HOSTS), required=True, help="Host to check (no auto-detect).")
@click.option("--workspace", type=click.Path(path_type=Path), default=None, help="Workspace scope instead of global.")
def stale_nudge_cmd(host: str, workspace: Path | None) -> None:
    """Print one `kind|name|days_unused|token_cost` line per stale optional agent/skill.

    `days_unused` is empty when never used. Silent (no output, exit 0) when
    nothing is stale. Internal plumbing for the Claude statusline tip and the
    OpenCode nudge plugin -- not meant for interactive use.
    """
    ws = _resolve_workspace(workspace)
    for item in stale_optional_items(host, ws):
        days = "" if item["days_unused"] is None else str(item["days_unused"])
        click.echo(f"{item['kind']}|{item['name']}|{days}|{item['token_cost']}")


__all__ = [
    "agent_group",
    "install_group",
    "skill_group",
    "stale_nudge_cmd",
]
