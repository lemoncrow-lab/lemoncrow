from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.paths import DEFAULT_STORE_DIRNAME, default_store_root

logger = logging.getLogger(__name__)

RUNTIME_ROLE_IDS = ("code", "general", "explore", "plan", "execute", "review", "research", "solve")
HOST_ROLE_IDS = ("code", "explore", "plan", "execute", "research", "review", "solve")
HOST_IDS = ("default", "copilot", "claude", "codex", "opencode", "antigravity", "cursor", "hermes")

CANONICAL_COPILOT_AGENT_MODEL = "gpt-5.4"
TOP_MODEL_CHOICES = (
    "claude-opus-4.8",
    "claude-sonnet-4.6",
    "gpt-5.5",
    "gpt-5.4",
)

DEFAULT_RUNTIME_MODELS = {
    "code": "claude-opus-4.8",
    "general": "claude-opus-4.8",
    "explore": "claude-sonnet-4.6",
    "plan": "claude-sonnet-4.6",
    "execute": "claude-opus-4.8",
    "review": "claude-sonnet-4.6",
    "research": "claude-sonnet-4.6",
    "solve": "claude-opus-4.8",
}

# Per-host default model pins for roles that should NOT inherit the session
# model out of the box. Read-only exploration/research run on a cheap model
# (mirrors the built-in Explore=haiku); coding/judgment roles inherit. Users
# override any entry via ``lc init`` (writes models.hosts.<host>.roles).
# Claude entries use Claude Code's bare model aliases ("sonnet"/"opus"/"haiku"),
# not versioned ids. Claude Code itself resolves an alias to its current model
# on every invocation, so these pins never go stale as new versions ship --
# unlike a versioned id, which would need a code change + release to bump.
# Codex has no equivalent alias mechanism, so these values are a last-resort
# fallback only -- see ``_discover_codex_mini_model`` below, which asks a
# locally installed `codex` CLI for its actual current mini-tier model.
DEFAULT_HOST_ROLE_MODELS: dict[str, dict[str, str]] = {
    "claude": {"explore": "haiku", "research": "haiku"},
    "codex": {"explore": "gpt-5.4-mini", "research": "gpt-5.4-mini"},
}

_CLAUDE_DOT_VERSION_RE = re.compile(r"(\d)\.(?=\d)")
_CODEX_MODEL_DISCOVERY_TIMEOUT_SECONDS = 3.0


@lru_cache(maxsize=1)
def _discover_codex_mini_model() -> str | None:
    """Best-effort live lookup of Codex's current "mini"-tier model slug.

    Codex has no runtime alias mechanism (unlike Claude Code's sonnet/opus/
    haiku), so a hardcoded slug in ``DEFAULT_HOST_ROLE_MODELS`` goes stale as
    OpenAI ships new Codex model generations. When the ``codex`` CLI is
    installed locally, ask it directly instead of trusting the shipped
    default -- ``codex debug models`` reports the exact catalog for the
    installed version, so this reflects the user's actual toolchain. Cached
    for the process lifetime since this shells out. Returns ``None`` on any
    failure (not installed, non-zero exit, bad JSON, no mini-tier entry) so
    callers fall back to the static pin.
    """
    codex_bin = shutil.which("codex")
    if not codex_bin:
        # Expected for anyone not using the codex host -- not an error, don't log.
        return None
    try:
        result = subprocess.run(
            [codex_bin, "debug", "models", "--bundled"],
            capture_output=True,
            text=True,
            timeout=_CODEX_MODEL_DISCOVERY_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            logger.debug(
                "lemoncrow.model_settings: `codex debug models` exited %d; falling back to static pin. stderr=%r",
                result.returncode,
                result.stderr.strip(),
            )
            return None
        catalog = json.loads(result.stdout)
        models = catalog.get("models")
        if not isinstance(models, list):
            logger.debug(
                "lemoncrow.model_settings: unexpected `codex debug models` catalog shape; falling back to static pin"
            )
            return None
        candidates = [
            entry
            for entry in models
            if isinstance(entry, dict)
            and isinstance(entry.get("slug"), str)
            and entry["slug"].endswith("-mini")
            and entry.get("visibility") == "list"
        ]
        if not candidates:
            logger.debug(
                "lemoncrow.model_settings: no mini-tier model found in codex catalog; falling back to static pin"
            )
            return None

        def _priority(entry: dict[str, Any]) -> int:
            value = entry.get("priority")
            return value if isinstance(value, int) else 1 << 30

        candidates.sort(key=_priority)
        return str(candidates[0]["slug"])
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.debug("lemoncrow.model_settings: codex model discovery failed (%s); falling back to static pin", exc)
        return None


def _shipped_host_default(host: str, role_id: str) -> str | None:
    shipped = DEFAULT_HOST_ROLE_MODELS.get(host, {}).get(role_id)
    if not shipped:
        return None
    if host == "codex":
        return _discover_codex_mini_model() or shipped
    return shipped


def global_model_settings_path() -> Path:
    return default_store_root() / "settings.json"


def workspace_model_settings_path(workspace_root: str | Path) -> Path:
    return Path(workspace_root).expanduser().resolve() / DEFAULT_STORE_DIRNAME / "settings.json"


def load_model_settings(workspace_root: str | Path | None = None) -> dict[str, Any]:
    settings = _normalized_settings(_read_json(global_model_settings_path()))
    if workspace_root is None:
        return settings
    local_path = workspace_model_settings_path(workspace_root)
    local = _normalized_settings(_read_json(local_path))
    return _deep_merge(settings, local)


def write_workspace_model_settings(workspace_root: str | Path, payload: dict[str, Any]) -> Path:
    path = workspace_model_settings_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def normalize_model_for_host(host: str, model: str | None) -> str | None:
    candidate = str(model or "").strip()
    if not candidate:
        return None
    if host == "claude" and candidate.startswith("claude-"):
        return _CLAUDE_DOT_VERSION_RE.sub(r"\1-", candidate)
    if host == "opencode" and "/" not in candidate:
        if candidate.startswith("claude-"):
            return "anthropic/" + _CLAUDE_DOT_VERSION_RE.sub(r"\1-", candidate)
        if candidate.startswith("gpt-"):
            return "openai/" + candidate
    return candidate


def resolve_runtime_model(role_id: str, workspace_root: str | Path | None = None) -> str:
    default = DEFAULT_RUNTIME_MODELS.get(role_id)
    if default is None:
        raise KeyError(f"unknown runtime role: {role_id}")
    settings = load_model_settings(workspace_root)
    raw = settings.get("models", {}).get("runtime", {}).get("roles", {}).get(role_id)
    candidate = str(raw or "").strip()
    return default if not candidate or candidate == "auto" else candidate


def resolve_host_model(
    host: str,
    role_id: str,
    *,
    workspace_root: str | Path | None = None,
    fallback: str | None = None,
) -> str | None:
    settings = load_model_settings(workspace_root)
    hosts = settings.get("models", {}).get("hosts", {})
    for host_key in (host, "default"):
        host_settings = hosts.get(host_key, {})
        roles = host_settings.get("roles", {})
        if not isinstance(roles, dict):
            continue
        if _is_legacy_auto_host_stub(roles):
            continue
        for key in (role_id, "*"):
            raw = roles.get(key)
            candidate = str(raw or "").strip()
            if candidate:
                return None if candidate == "auto" else candidate
    shipped = _shipped_host_default(host, role_id)
    if shipped:
        return shipped
    try:
        return resolve_runtime_model(role_id, workspace_root)
    except KeyError:
        return fallback


def resolve_explicit_host_model(
    host: str,
    role_id: str,
    *,
    workspace_root: str | Path | None = None,
) -> str | None:
    """Model for a host *agent file*, or None to inherit the host session model.

    Unlike :func:`resolve_host_model`, this never falls back to the runtime
    default -- an absent pin means the agent file omits ``model:`` and inherits
    the session model. Resolution: explicit settings (``models.hosts.<host>`` /
    ``default``) -> shipped ``DEFAULT_HOST_ROLE_MODELS`` -> ``None``. An explicit
    ``"auto"`` resolves to ``None`` and overrides the shipped default.
    """
    settings = load_model_settings(workspace_root)
    hosts = settings.get("models", {}).get("hosts", {})
    for host_key in (host, "default"):
        host_settings = hosts.get(host_key, {})
        roles = host_settings.get("roles", {})
        if not isinstance(roles, dict):
            continue
        if _is_legacy_auto_host_stub(roles):
            continue
        for key in (role_id, "*"):
            candidate = str(roles.get(key) or "").strip()
            if candidate:
                return None if candidate == "auto" else candidate
    return _shipped_host_default(host, role_id)


def build_runtime_settings_payload(models: dict[str, str]) -> dict[str, Any]:
    return {"models": {"runtime": {"roles": dict(models)}}}


def set_host_role_models(
    payload: dict[str, Any],
    *,
    host: str,
    models: dict[str, str],
) -> dict[str, Any]:
    updated = _deep_merge({}, payload)
    model_root = updated.setdefault("models", {})
    hosts = model_root.setdefault("hosts", {})
    host_entry = hosts.setdefault(host, {})
    host_entry["roles"] = dict(models)
    return updated


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _normalized_settings(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    models = data.get("models")
    if not isinstance(models, dict):
        return {}
    runtime = models.get("runtime")
    hosts = models.get("hosts")
    normalized: dict[str, Any] = {"models": {}}
    if isinstance(runtime, dict):
        normalized["models"]["runtime"] = {"roles": _normalized_role_map(runtime.get("roles"), allow_auto=False)}
    if isinstance(hosts, dict):
        normalized_hosts: dict[str, Any] = {}
        for host_key, host_value in hosts.items():
            if str(host_key) not in HOST_IDS:
                continue
            if not isinstance(host_value, dict):
                continue
            normalized_hosts[str(host_key)] = {
                "roles": _normalized_role_map(host_value.get("roles"), allow_auto=True),
            }
        normalized["models"]["hosts"] = normalized_hosts
    return normalized


def _normalized_role_map(raw: Any, *, allow_auto: bool) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    allowed_keys = set(RUNTIME_ROLE_IDS if not allow_auto else HOST_ROLE_IDS) | {"*"}
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        role_id = str(key).strip()
        if role_id not in allowed_keys:
            continue
        candidate = str(value or "").strip()
        if not candidate:
            continue
        if candidate == "auto" and not allow_auto:
            continue
        normalized[role_id] = candidate
    return normalized


def _is_legacy_auto_host_stub(roles: dict[str, Any]) -> bool:
    role_keys = {str(key).strip() for key in roles}
    if role_keys != set(HOST_ROLE_IDS):
        return False
    return all(str(roles.get(role_id) or "").strip() == "auto" for role_id in HOST_ROLE_IDS)


def _deep_merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


__all__ = [
    "CANONICAL_COPILOT_AGENT_MODEL",
    "DEFAULT_HOST_ROLE_MODELS",
    "DEFAULT_RUNTIME_MODELS",
    "HOST_IDS",
    "HOST_ROLE_IDS",
    "RUNTIME_ROLE_IDS",
    "TOP_MODEL_CHOICES",
    "build_runtime_settings_payload",
    "global_model_settings_path",
    "load_model_settings",
    "normalize_model_for_host",
    "resolve_explicit_host_model",
    "resolve_host_model",
    "resolve_runtime_model",
    "set_host_role_models",
    "workspace_model_settings_path",
    "write_workspace_model_settings",
]
