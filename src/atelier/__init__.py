"""Atelier — Agent Reasoning Runtime.

A reasoning/procedure runtime for coding and product agents. Combines:

1. Playbook-based reasoning reuse (retrieve known procedures before/during runs).
2. Failure-driven improvement (record traces, detect recurring failures).
3. Rubric-style verification (check plans/outputs against expert rubrics).

This is NOT memory. It stores observable traces, explicit procedures,
failures, validation results, and reusable lessons — never hidden chain-of-thought
or user preferences.
"""

from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from atelier.core.foundation.models import (
        PlanCheckResult,
        Playbook,
        RescueResult,
        Rubric,
        RubricResult,
        Trace,
        TraceLearning,
    )


_LAZY_EXPORTS = {
    "PlanCheckResult": ("atelier.core.foundation.models", "PlanCheckResult"),
    "Playbook": ("atelier.core.foundation.models", "Playbook"),
    "RescueResult": ("atelier.core.foundation.models", "RescueResult"),
    "Rubric": ("atelier.core.foundation.models", "Rubric"),
    "RubricResult": ("atelier.core.foundation.models", "RubricResult"),
    "Trace": ("atelier.core.foundation.models", "Trace"),
    "TraceLearning": ("atelier.core.foundation.models", "TraceLearning"),
}

# The canonical version is in pyproject.toml.
# At runtime we read the installed package metadata so they never drift.

try:
    from importlib.metadata import version as _version

    __version__ = _version(__name__.split(".")[0])
except Exception:  # noqa: BLE001 — metadata may be missing in dev/bundle contexts
    # Fallback: read pyproject.toml directly for dev/uninstalled usage.
    _pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if _pyproject.exists():
        import re

        _match = re.search(r'^version\s*=\s*"([^"]+)"', _pyproject.read_text(), re.M)
        __version__ = _match.group(1) if _match else "0.0.0"
    else:
        __version__ = "0.0.0"


# Seed process env vars from persisted `atelier settings set` overrides before
# anything else reads them. Cheap (small local JSON, stdlib-only import) and
# must never block a plain `import atelier` — see apply_settings_env().
try:
    from atelier.core.settings import apply_settings_env as _apply_settings_env

    _apply_settings_env()
except Exception:  # noqa: BLE001 - settings must never block import
    pass


def __getattr__(name: str) -> Any:
    lazy_export = _LAZY_EXPORTS.get(name)
    if lazy_export is not None:
        module_name, attr_name = lazy_export
        value = getattr(import_module(module_name), attr_name)
        globals()[name] = value
        return value
    if name in {"hosts", "integrations", "sdk"}:
        return import_module(f"atelier.gateway.{name}")
    if name in {"AtelierClient", "LocalClient", "MCPClient", "RemoteClient"}:
        mod = import_module("atelier.gateway.sdk")
        return getattr(mod, name)
    if name == "storage":
        return import_module("atelier.infra.storage")
    if name == "service":
        return import_module("atelier.core.service")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AtelierClient",
    "LocalClient",
    "MCPClient",
    "PlanCheckResult",
    "Playbook",
    "RemoteClient",
    "RescueResult",
    "Rubric",
    "RubricResult",
    "Trace",
    "TraceLearning",
    "__version__",
    "hosts",
    "integrations",
    "sdk",
    "service",
    "storage",
]
