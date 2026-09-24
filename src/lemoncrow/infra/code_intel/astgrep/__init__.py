"""The main package's ast-grep: managed bootstrap on top of :mod:`lemoncrow_client.kit.astgrep`."""

from lemoncrow.infra.code_intel.astgrep.adapter import astgrep_adapter, resolve_astgrep_binary
from lemoncrow.infra.code_intel.astgrep.binaries import (
    ManagedAstGrepAsset,
    bootstrap_managed_astgrep,
    discover_astgrep_binary,
)

__all__ = [
    "ManagedAstGrepAsset",
    "astgrep_adapter",
    "bootstrap_managed_astgrep",
    "discover_astgrep_binary",
    "resolve_astgrep_binary",
]
