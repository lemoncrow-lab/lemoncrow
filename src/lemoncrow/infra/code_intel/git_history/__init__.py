"""Bootstrap helpers for the isolated git-history substrate."""

from __future__ import annotations

from types import ModuleType

_PYGIT2: ModuleType | None


class GitHistoryBootstrapError(RuntimeError):
    """Raised when the git-history substrate cannot load its required backend."""


try:
    import pygit2 as _PYGIT2
except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
    _PYGIT2 = None
    _PYGIT2_IMPORT_ERROR: ImportError | None = exc
else:
    _PYGIT2_IMPORT_ERROR = None


def require_pygit2() -> ModuleType:
    """Return the pinned pygit2 module or raise a clear bootstrap error."""

    if _PYGIT2 is None:
        raise GitHistoryBootstrapError(
            "pygit2 is required for lemoncrow.infra.code_intel.git_history; "
            "install the pinned Phase 4 dependency and retry. "
            "GitPython and subprocess fallbacks are intentionally unsupported."
        ) from _PYGIT2_IMPORT_ERROR
    return _PYGIT2


__all__ = ["GitHistoryBootstrapError", "require_pygit2"]
