"""Load, persist, and apply LemonCrow settings.

Backed by the same ``<root>/plugin_settings.json`` file the Claude plugin
runtime already uses for its 8 boolean toggles (see
``lemoncrow.core.capabilities.plugin_runtime``); this module just adds
additional dotted keys to that file for every entry in
:mod:`lemoncrow.core.settings_registry`, without disturbing the existing
keys other subsystems (recall config, live reviewer, savings) already read
directly from it.

``apply_settings_env`` is called once, as early as possible, from
``lemoncrow/__init__.py``: it seeds ``os.environ`` from any persisted
overrides via ``setdefault``, so an explicitly-exported environment variable
always wins over a value stored via ``lc settings set``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from lemoncrow.core.settings_registry import CATEGORIES, SETTINGS, SettingSpec

logger = logging.getLogger(__name__)

_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in SETTINGS}

__all__ = [
    "CATEGORIES",
    "SettingSpec",
    "all_settings",
    "apply_settings_env",
    "coerce",
    "find_by_key",
    "load_raw",
    "load_settings",
    "settings_path",
    "unset_setting",
    "write_setting",
]


def all_settings() -> list[SettingSpec]:
    return list(SETTINGS)


def find_by_key(key: str) -> SettingSpec | None:
    return _BY_KEY.get(key)


def settings_path(root: str | Path) -> Path:
    return Path(root) / "plugin_settings.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                nested = data.get("lemoncrow")
                if isinstance(nested, dict):
                    merged = dict(data)
                    merged.update(nested)
                    return merged
                return data
    except (OSError, ValueError):
        logger.warning("Failed to read settings from %s", path, exc_info=True)
    return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def coerce(spec: SettingSpec, raw: Any) -> Any:
    """Coerce a raw persisted/CLI value to ``spec.type``. Raises ValueError/TypeError on bad input."""
    if spec.type == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if spec.type == "int":
        return int(str(raw).strip())
    if spec.type == "float":
        return float(str(raw).strip())
    return str(raw)


def _env_repr(spec: SettingSpec, value: Any) -> str:
    if spec.type == "bool":
        return "true" if value else "false"
    return str(value)


def load_raw(root: str | Path) -> dict[str, Any]:
    """Return the persisted overrides, keyed by dotted/legacy setting key."""
    return _read_json(settings_path(root))


def load_settings(root: str | Path, *, category: str | None = None) -> dict[str, Any]:
    """Return every known setting's effective value (persisted override, else default)."""
    raw = load_raw(root)
    out: dict[str, Any] = {}
    for spec in SETTINGS:
        if category is not None and spec.category != category:
            continue
        if spec.key in raw:
            try:
                out[spec.key] = coerce(spec, raw[spec.key])
                continue
            except (TypeError, ValueError):
                logger.warning("Invalid stored value for %s; using default", spec.key)
        out[spec.key] = spec.default
    return out


def write_setting(root: str | Path, key: str, raw_value: Any) -> Any:
    spec = _BY_KEY.get(key)
    if spec is None:
        raise ValueError(f"unknown setting: {key}")
    if not spec.settable:
        hint = f"set {spec.env_var} instead" if spec.env_var else "it is managed by LemonCrow at runtime"
        raise ValueError(f"{key} is read-only ({hint})")
    try:
        value = coerce(spec, raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid value for {key} (expected {spec.type}): {raw_value!r}") from exc
    raw = load_raw(root)
    raw[key] = value
    _write_json(settings_path(root), raw)
    return value


def unset_setting(root: str | Path, key: str) -> None:
    if key not in _BY_KEY:
        raise ValueError(f"unknown setting: {key}")
    raw = load_raw(root)
    if key in raw:
        del raw[key]
        _write_json(settings_path(root), raw)


def _resolve_root() -> Path:
    configured = os.environ.get("LEMONCROW_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".lemoncrow"


def apply_settings_env(root: str | Path | None = None, *, env: dict[str, str] | None = None) -> None:
    """Seed process env vars from persisted settings.

    Only fills in vars not already present in *env* — an explicit environment
    variable always takes precedence over a value persisted via ``lc
    settings set``. Safe to call repeatedly; never raises (settings must never
    block a plain ``import lemoncrow``).
    """
    target = env if env is not None else os.environ
    try:
        resolved_root = Path(root) if root is not None else _resolve_root()
        raw = load_raw(resolved_root)
    except Exception:  # noqa: BLE001 - settings must never block a plain `import lemoncrow`
        return
    for key, value in raw.items():
        spec = _BY_KEY.get(key)
        if spec is None or spec.env_var is None or not spec.settable:
            continue
        if spec.env_var in target:
            continue
        try:
            coerced = coerce(spec, value)
        except (TypeError, ValueError):
            continue
        target[spec.env_var] = _env_repr(spec, coerced)
