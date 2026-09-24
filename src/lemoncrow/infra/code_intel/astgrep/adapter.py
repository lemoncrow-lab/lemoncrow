"""The main package's ast-grep: the kit's adapter, bootstrapping a managed binary.

:mod:`lemoncrow_client.kit.astgrep` runs ast-grep for the thin client and the
main package alike. Only the main package may download a pinned ast-grep when
none is installed; this module plugs that in.
"""

from __future__ import annotations

from pathlib import Path

from lemoncrow_client.kit.astgrep import AstGrepAdapter, AstGrepToolUnavailable

from lemoncrow.infra.code_intel.astgrep.binaries import discover_astgrep_binary


def resolve_astgrep_binary(repo_root: Path) -> Path:
    """An installed ast-grep, else the managed one; raises when neither is available."""
    resolution = discover_astgrep_binary(repo_root, allow_bootstrap=True)
    if not resolution.available or resolution.path is None:
        raise AstGrepToolUnavailable(resolution.to_payload())
    return resolution.path


def astgrep_adapter(repo_root: str | Path, *, timeout: float = 120.0) -> AstGrepAdapter:
    """The kit's adapter for ``repo_root``, resolving ast-grep with the managed bootstrap."""
    return AstGrepAdapter(repo_root, timeout=timeout, resolve_binary=resolve_astgrep_binary)


__all__ = ["astgrep_adapter", "resolve_astgrep_binary"]
