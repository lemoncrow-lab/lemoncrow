"""Atelier — Agent Reasoning Runtime.

A reasoning/procedure runtime for coding and product agents. Combines:

1. ReasonBlocks-style reasoning reuse (retrieve known procedures before/during runs).
2. Lemma-style failure improvement (record traces, detect recurring failures).
3. Rubric-style verification (check plans/outputs against expert rubrics).

This is NOT memory. It stores observable traces, explicit procedures,
failures, validation results, and reusable lessons — never hidden chain-of-thought
or user preferences.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from atelier.core.foundation.models import (
        FailureCluster,
        PlanCheckResult,
        ReasonBlock,
        RescueResult,
        Rubric,
        RubricResult,
        Trace,
    )


_LAZY_EXPORTS = {
    "FailureCluster": ("atelier.core.foundation.models", "FailureCluster"),
    "PlanCheckResult": ("atelier.core.foundation.models", "PlanCheckResult"),
    "ReasonBlock": ("atelier.core.foundation.models", "ReasonBlock"),
    "RescueResult": ("atelier.core.foundation.models", "RescueResult"),
    "Rubric": ("atelier.core.foundation.models", "Rubric"),
    "RubricResult": ("atelier.core.foundation.models", "RubricResult"),
    "Trace": ("atelier.core.foundation.models", "Trace"),
}

# The canonical version is in pyproject.toml.
# At runtime we read the installed package metadata so they never drift.
try:
    from importlib.metadata import version as _version

    __version__ = _version(__name__.split(".")[0])
except Exception:
    # Fallback for development / uninstalled usage.
    __version__ = "0.1.0"


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
    "FailureCluster",
    "LocalClient",
    "MCPClient",
    "PlanCheckResult",
    "ReasonBlock",
    "RemoteClient",
    "RescueResult",
    "Rubric",
    "RubricResult",
    "Trace",
    "__version__",
    "hosts",
    "integrations",
    "sdk",
    "service",
    "storage",
]
