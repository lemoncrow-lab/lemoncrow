"""BYO-competitor arm support for the CodeBench A/B engine.

Lets a head-to-head run include a third arm built from an arbitrary GitHub repo
-- a rival code-context tool, Claude Code skill, MCP server, or plugin -- so a
user can measure **baseline vs LemonCrow vs <that tool>** on their own repo and
prompts and see who actually saves more tokens/cost.

The competitor runs *vanilla Claude Code on the same model as every other arm*,
with only the competitor's own tooling wired in. So any cost/token/turn delta is
attributable to the tool, not to a different model or driver -- the same A/B
contract the baseline/lemoncrow arms already hold.

A competitor is described by a small JSON manifest the caller writes after
reading the repo's README (how to install it, how to invoke it). Supported
wiring, any combination:

- ``mcp``:        inject the competitor's MCP server(s) via ``--mcp-config`` (+ ``--strict-mcp-config``)
- ``plugin_dir``: inject a Claude Code plugin directory via ``--plugin-dir``
- ``skill_file``: append a skill / system-prompt file via ``--append-system-prompt``
- ``agent``:      pin an ``--agent`` persona the plugin provides
- ``env``:        extra environment variables for the agent subprocess

Install runs **once** per repo clone (cached under ``CODEBENCH_COMPETITOR_ROOT``,
default ``<tmp>/codebench_competitors``); every ``(task, rep)`` call thereafter
reuses the prepared clone. In every string field the token ``${CLONE}`` expands
to the absolute clone directory.

This module is deliberately runner-agnostic: it produces a :class:`PreparedCompetitor`
(resolved config), and ``benchmarks.codebench.run`` turns that into an ``ArmSpec``.
So there is no import dependency back on the runner.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Arm names reserved by the built-in runner; a competitor manifest may not reuse them.
RESERVED_ARM_NAMES = frozenset({"baseline", "lemoncrow", "execute", "solve", "auto"})
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_INSTALL_MARKER = ".codebench_competitor_ready"


def competitor_root() -> Path:
    """Cache root for competitor clones (override with ``CODEBENCH_COMPETITOR_ROOT``)."""
    return Path(
        os.environ.get(
            "CODEBENCH_COMPETITOR_ROOT",
            str(Path(tempfile.gettempdir()) / "codebench_competitors"),
        )
    )


@dataclass(frozen=True)
class CompetitorSpec:
    """Declarative description of a GitHub tool to benchmark as an arm.

    Only ``name`` and ``repo`` are required. ``name`` is both the arm label in
    the report and the on-disk clone directory, so it must be a safe token.
    """

    name: str
    repo: str
    ref: str | None = None
    install: tuple[str, ...] = ()
    mcp: Mapping[str, Any] | None = None
    plugin_dir: str | None = None
    skill_file: str | None = None
    agent: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedCompetitor:
    """A competitor whose repo has been cloned + installed and config resolved."""

    name: str
    clone_dir: Path
    mcp_config: dict[str, Any] | None = None
    plugin_dir: str | None = None
    system_prompt: str | None = None
    agent: str | None = None
    env: dict[str, str] = field(default_factory=dict)


def _require_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("competitor manifest is missing 'name'")
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"competitor name {name!r} is not a safe arm token "
            "(use letters, digits, '.', '-', '_'; must start alphanumeric)"
        )
    if name in RESERVED_ARM_NAMES:
        raise ValueError(f"competitor name {name!r} collides with a built-in arm; pick another")
    return name


def load_competitor_spec(path: str | Path) -> CompetitorSpec:
    """Parse and validate a competitor manifest JSON file.

    Cheap: no clone or network access -- callers use this to learn the arm name
    for the cost estimate before deciding to spend.
    """
    manifest_path = Path(path).expanduser()
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"competitor manifest not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"competitor manifest {manifest_path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"competitor manifest {manifest_path} must be a JSON object")
    name = _require_name(str(raw.get("name", "")))
    repo = str(raw.get("repo", "")).strip()
    if not repo:
        raise ValueError(f"competitor manifest {manifest_path} is missing 'repo'")
    install_raw = raw.get("install", ())
    if isinstance(install_raw, str):
        install: tuple[str, ...] = (install_raw,)
    elif isinstance(install_raw, Sequence):
        install = tuple(str(cmd) for cmd in install_raw)
    else:
        raise ValueError("competitor 'install' must be a string or list of strings")
    mcp = raw.get("mcp")
    if mcp is not None and not isinstance(mcp, dict):
        raise ValueError("competitor 'mcp' must be a JSON object")
    env_raw = raw.get("env", {})
    if not isinstance(env_raw, dict):
        raise ValueError("competitor 'env' must be a JSON object of KEY: VALUE strings")
    return CompetitorSpec(
        name=name,
        repo=repo,
        ref=(str(raw["ref"]).strip() or None) if raw.get("ref") else None,
        install=install,
        mcp=mcp,
        plugin_dir=(str(raw["plugin_dir"]) or None) if raw.get("plugin_dir") else None,
        skill_file=(str(raw["skill_file"]) or None) if raw.get("skill_file") else None,
        agent=(str(raw["agent"]).strip() or None) if raw.get("agent") else None,
        env={str(k): str(v) for k, v in env_raw.items()},
    )


def _subst(value: Any, clone_dir: Path) -> Any:
    """Recursively expand ``${CLONE}`` in strings within nested JSON data."""
    clone = str(clone_dir)
    if isinstance(value, str):
        return value.replace("${CLONE}", clone)
    if isinstance(value, dict):
        return {k: _subst(v, clone_dir) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v, clone_dir) for v in value]
    return value


def _normalize_mcp(mcp: Mapping[str, Any], name: str, clone_dir: Path) -> dict[str, Any]:
    """Return a full ``{"mcpServers": {...}}`` config with ``${CLONE}`` expanded.

    Accepts either a full config (already has ``mcpServers``) or a single-server
    spec (e.g. ``{"command": ..., "args": [...]}``), which is wrapped under the
    competitor's name.
    """
    resolved = _subst(dict(mcp), clone_dir)
    if "mcpServers" in resolved:
        return resolved
    return {"mcpServers": {name: resolved}}


def prepare_competitor(
    spec: CompetitorSpec,
    root: Path | None = None,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    clone_timeout: int = 900,
    install_timeout: int = 1800,
) -> PreparedCompetitor:
    """Clone (idempotent) + install (once) the competitor and resolve its config.

    The clone lives under *root* / ``spec.name`` and is reused across runs. Install
    commands run in the clone dir the first time only, gated by a marker file, so
    repeated benchmark invocations do not reinstall. Every ``(task, rep)`` call
    later reuses the returned :class:`PreparedCompetitor` unchanged.
    """
    base = (root or competitor_root()).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    clone_dir = base / spec.name
    if not (clone_dir / ".git").is_dir():
        clone_cmd = ["git", "clone", "--quiet"]
        if spec.ref:
            clone_cmd += ["--branch", spec.ref]
        clone_cmd += [spec.repo, str(clone_dir)]
        proc = runner(clone_cmd, capture_output=True, text=True, timeout=clone_timeout)
        if proc.returncode != 0:
            # A --branch clone fails for a commit SHA; retry as a plain clone + checkout.
            if spec.ref:
                _clone_and_checkout(spec, clone_dir, runner, clone_timeout)
            else:
                raise RuntimeError(
                    f"git clone failed for competitor {spec.name!r} ({spec.repo}): "
                    f"{(proc.stderr or proc.stdout or '').strip()[:400]}"
                )
    marker = clone_dir / _INSTALL_MARKER
    if spec.install and not marker.exists():
        for cmd in spec.install:
            resolved = _subst(cmd, clone_dir)
            proc = runner(
                resolved,
                shell=True,
                cwd=str(clone_dir),
                capture_output=True,
                text=True,
                timeout=install_timeout,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"competitor {spec.name!r} install step failed: {resolved!r}: "
                    f"{(proc.stderr or proc.stdout or '').strip()[:400]}"
                )
        marker.write_text("ok", encoding="utf-8")

    mcp_config = _normalize_mcp(spec.mcp, spec.name, clone_dir) if spec.mcp else None
    plugin_dir = str(Path(_subst(spec.plugin_dir, clone_dir))) if spec.plugin_dir else None
    system_prompt = None
    if spec.skill_file:
        skill_path = Path(_subst(spec.skill_file, clone_dir))
        if not skill_path.is_file():
            raise RuntimeError(f"competitor {spec.name!r} skill_file not found: {skill_path}")
        system_prompt = skill_path.read_text(encoding="utf-8").strip()
    return PreparedCompetitor(
        name=spec.name,
        clone_dir=clone_dir,
        mcp_config=mcp_config,
        plugin_dir=plugin_dir,
        system_prompt=system_prompt,
        agent=spec.agent,
        env={k: _subst(v, clone_dir) for k, v in spec.env.items()},
    )


def _clone_and_checkout(
    spec: CompetitorSpec,
    clone_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    timeout: int,
) -> None:
    proc = runner(
        ["git", "clone", "--quiet", spec.repo, str(clone_dir)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git clone failed for competitor {spec.name!r} ({spec.repo}): "
            f"{(proc.stderr or proc.stdout or '').strip()[:400]}"
        )
    checkout = runner(
        ["git", "-C", str(clone_dir), "checkout", "--quiet", str(spec.ref)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if checkout.returncode != 0:
        raise RuntimeError(
            f"git checkout {spec.ref!r} failed for competitor {spec.name!r}: "
            f"{(checkout.stderr or checkout.stdout or '').strip()[:400]}"
        )
